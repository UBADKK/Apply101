"""Profile analysis must not acquire -- and later put a cooldown on -- the
guard rows of a DIFFERENT user who took over the analyzed profile's
user_id (and possibly profile_id) after the route's last identity check and
before the guard was committed.

users/candidate_profiles ids have no AUTOINCREMENT and SQLite foreign keys
are not enforced, so a deleted owner A's user_id/profile_id can be reused by
the next user B. The guard is keyed by those plain ids, so the route passes
a precondition to try_acquire_profile_analysis_guard that re-checks the
captured owner identity (token_key) on the guard session, under the guard's
write lock; a mismatch is TARGET_CHANGED and becomes the generic 404 before
anything is acquired, released or sent to OpenAI.

The replacement is made deterministic, without threads: the route's
try_acquire_profile_analysis_guard is wrapped so that A's deletion (the real
users.delete_user) and B's creation are committed in separate sessions
right before the real acquisition runs.
"""

import json
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.analysis_guard import (
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
    AcquireOutcome,
    load_config,
    try_acquire_profile_analysis_guard,
)
from backend.tests.test_inflight_profile_delete_writes import (
    PROFILE_NOT_FOUND,
    VALID_ANALYSIS_PAYLOAD,
    _BaseInflightDeleteTestCase,
    _openai_response,
    profiles,
)


def _valid_openai_response():
    return _openai_response(json.dumps(VALID_ANALYSIS_PAYLOAD))


class _BaseProfileGuardRaceTestCase(_BaseInflightDeleteTestCase):
    """Owner A with one profile (no cached analysis, so the route reaches
    the guard), plus an admin that survives A's deletion."""

    def setUp(self):
        super().setUp()
        self.admin_id = self._create_user("guard.race.admin@example.com", is_admin=True)
        self.user_id = self._create_user("guard.race.owner.a@example.com")
        self.profile_id = self._create_profile(self.user_id, description="A's profile")
        self.owner_token_key = self._token_key(self.user_id)
        self.assertTrue(self.owner_token_key)
        self.replaced = False
        self.after_replacement = None

    def _url(self):
        return f"/users/{self.user_id}/profiles/{self.profile_id}/analyze"

    # -- state, read straight from the temp SQLite file ------------------

    def _rows(self, table, order_by):
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT * FROM {table} ORDER BY {order_by}")
            ).fetchall()

    def _snapshot(self):
        return {
            "users": self._rows("users", "user_id"),
            "candidate_profiles": self._rows("candidate_profiles", "profile_id"),
            "profile_analysis": self._rows("profile_analysis", "analysis_id"),
            "analysis_guards": self._rows("analysis_guards", "operation_type, resource_id"),
        }

    def _guard_row(self, operation_type, resource_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM analysis_guards "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"op": operation_type, "rid": resource_id},
            ).fetchone()

    def _token_key(self, user_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT token_key FROM users WHERE user_id = :uid"),
                {"uid": user_id},
            ).scalar()

    def _add_guard_row(self, operation_type, resource_id, state):
        now = time.time()
        self._add(models.AnalysisGuard(
            operation_type=operation_type,
            resource_id=resource_id,
            owner_token="synthetic-owner-token-of-b" if state == "lease" else None,
            lock_expires_at=now + 600 if state == "lease" else None,
            cooldown_until=now + 600 if state == "cooldown" else None,
        ))

    def _acquire_directly(self, profile_id, user_id):
        session = self.session_factory()
        try:
            return try_acquire_profile_analysis_guard(
                session, profile_id=profile_id, owner_user_id=user_id, config=load_config()
            )
        finally:
            session.close()

    # -- the replacement, in separate independently committed sessions ---

    def _replace_owner_elsewhere(self, with_b_profile=True, b_guard=None):
        """Deletes A (real users.delete_user) and creates B with A's user_id
        and its own fresh token_key; with_b_profile also gives B a profile
        with A's profile_id. b_guard: None, "lease" or "cooldown" held by B
        on its user resource -- and on the profile resource when B's
        profile reuses the profile_id."""
        self.assertFalse(self.replaced, "replacement must run exactly once")
        self.replaced = True
        self.b_guard = b_guard

        self._delete_user_elsewhere(self.user_id)
        self.assertIsNone(self._guard_row(USER_OPERATION_TYPE, self.user_id))
        self.assertIsNone(self._guard_row(PROFILE_OPERATION_TYPE, self.profile_id))

        if with_b_profile:
            self._recreate_same_ids_elsewhere(
                self.user_id, self.profile_id, "guard.race.user.b@example.com"
            )
        else:
            self._add(models.User(
                user_id=self.user_id, name="Replacement User B",
                mail="guard.race.user.b@example.com",
            ))

        if b_guard is not None:
            self._add_guard_row(USER_OPERATION_TYPE, self.user_id, b_guard)
            if with_b_profile:
                self._add_guard_row(PROFILE_OPERATION_TYPE, self.profile_id, b_guard)

        b_token_key = self._token_key(self.user_id)
        self.assertTrue(b_token_key)
        self.assertNotEqual(b_token_key, self.owner_token_key)
        self.after_replacement = self._snapshot()

    def _post_with_replacement_before_guard(self, headers, with_b_profile=True, b_guard=None):
        real_acquire = profiles.try_acquire_profile_analysis_guard

        def replace_then_acquire(*args, **kwargs):
            self._replace_owner_elsewhere(with_b_profile=with_b_profile, b_guard=b_guard)
            return real_acquire(*args, **kwargs)

        with patch.object(
            profiles, "try_acquire_profile_analysis_guard", side_effect=replace_then_acquire
        ) as mock_acquire, patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_valid_openai_response(),
        ) as mock_openai:
            response = self.client.post(self._url(), headers=headers)

        mock_acquire.assert_called_once()
        return response, mock_openai

    def _assert_replaced_404(self, response, mock_openai):
        self.assertTrue(self.replaced, "the replacement seam was never reached")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertNotIn("Retry-After", response.headers)
        mock_openai.assert_not_called()
        self.assertEqual(self._snapshot(), self.after_replacement)
        if self.b_guard is None:
            # Nothing acquired, so no lease and no cooldown for B either.
            self.assertIsNone(self._guard_row(PROFILE_OPERATION_TYPE, self.profile_id))
            self.assertIsNone(self._guard_row(USER_OPERATION_TYPE, self.user_id))


class ReplacedBeforeGuardCommitTests(_BaseProfileGuardRaceTestCase):
    """1./2. A is replaced by B after the route's last identity check and
    before the guard transaction."""

    def test_owner_reused_user_and_profile_ids_acquires_nothing(self):
        response, mock_openai = self._post_with_replacement_before_guard(
            self._headers(self.user_id), with_b_profile=True
        )
        self._assert_replaced_404(response, mock_openai)

    def test_owner_reused_user_id_only_acquires_nothing(self):
        response, mock_openai = self._post_with_replacement_before_guard(
            self._headers(self.user_id), with_b_profile=False
        )
        self._assert_replaced_404(response, mock_openai)

    def test_admin_reused_user_and_profile_ids_acquires_nothing(self):
        response, mock_openai = self._post_with_replacement_before_guard(
            self._headers(self.admin_id), with_b_profile=True
        )
        self._assert_replaced_404(response, mock_openai)

    def test_admin_reused_user_id_only_acquires_nothing(self):
        response, mock_openai = self._post_with_replacement_before_guard(
            self._headers(self.admin_id), with_b_profile=False
        )
        self._assert_replaced_404(response, mock_openai)

    def _assert_b_guard_rows_untouched(self, with_b_profile):
        keys = [(USER_OPERATION_TYPE, self.user_id)]
        if with_b_profile:
            keys.append((PROFILE_OPERATION_TYPE, self.profile_id))
        before = {
            (row.operation_type, row.resource_id): row
            for row in self.after_replacement["analysis_guards"]
        }
        for key in keys:
            self.assertIn(key, before)
            self.assertEqual(self._guard_row(*key), before[key])

    def test_b_live_lease_is_404_not_409(self):
        for caller in ("owner", "admin"):
            for with_b_profile in (True, False):
                with self.subTest(caller=caller, with_b_profile=with_b_profile):
                    self._reset_to_owner_a()
                    caller_id = self.user_id if caller == "owner" else self.admin_id
                    response, mock_openai = self._post_with_replacement_before_guard(
                        self._headers(caller_id), with_b_profile=with_b_profile, b_guard="lease"
                    )
                    self._assert_replaced_404(response, mock_openai)
                    self._assert_b_guard_rows_untouched(with_b_profile)

    def test_b_active_cooldown_is_404_not_429(self):
        for caller in ("owner", "admin"):
            for with_b_profile in (True, False):
                with self.subTest(caller=caller, with_b_profile=with_b_profile):
                    self._reset_to_owner_a()
                    caller_id = self.user_id if caller == "owner" else self.admin_id
                    response, mock_openai = self._post_with_replacement_before_guard(
                        self._headers(caller_id), with_b_profile=with_b_profile, b_guard="cooldown"
                    )
                    self._assert_replaced_404(response, mock_openai)
                    self._assert_b_guard_rows_untouched(with_b_profile)

    def _reset_to_owner_a(self):
        """Fresh A (same ids) for each subTest: removes B and its guard rows
        left by the previous iteration."""
        if self.replaced:
            self._delete_user_elsewhere(self.user_id)
            session = self.session_factory()
            try:
                session.query(models.AnalysisGuard).delete()
                session.commit()
            finally:
                session.close()
            self.user_id = self._create_user(
                "guard.race.owner.a@example.com", user_id=self.user_id
            )
            self.profile_id = self._create_profile(
                self.user_id, profile_id=self.profile_id, description="A's profile"
            )
            self.owner_token_key = self._token_key(self.user_id)
            self.replaced = False
            self.after_replacement = None


class GuardPreconditionLockTests(_BaseProfileGuardRaceTestCase):
    """3. Owner unchanged: right after the route's real precondition passed
    inside the guard transaction, A's deletion from another connection must
    fail with "database is locked" -- nothing can commit between the
    re-check and the guard's commit."""

    def _post_with_delete_attempt_after_guard_precondition(self, headers):
        blocked_engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        blocked_factory = sessionmaker(autocommit=False, autoflush=False, bind=blocked_engine)
        real_acquire = profiles.try_acquire_profile_analysis_guard
        outcomes = []

        def acquire_wrapping_precondition(*args, **kwargs):
            precondition = kwargs.get("precondition")
            if precondition is not None:
                def precondition_then_try_delete(guard_db):
                    result = precondition(guard_db)
                    if result and not outcomes:
                        try:
                            self._delete_user_elsewhere(self.user_id, blocked_factory)
                            outcomes.append("delete committed")
                        except OperationalError as exc:
                            outcomes.append(
                                "locked" if "database is locked" in str(exc) else repr(exc)
                            )
                    return result

                kwargs["precondition"] = precondition_then_try_delete
            return real_acquire(*args, **kwargs)

        try:
            with patch.object(
                profiles,
                "try_acquire_profile_analysis_guard",
                side_effect=acquire_wrapping_precondition,
            ), patch(
                "backend.routers.profiles._create_profile_analysis_response",
                return_value=_valid_openai_response(),
            ) as mock_openai:
                response = self.client.post(self._url(), headers=headers)
        finally:
            # Disposed here, not via addCleanup (which runs after tearDown's
            # rmtree and would leave the temp dir behind on Windows).
            blocked_engine.dispose()
        return response, outcomes, mock_openai

    def _assert_owner_a_survived(self):
        self.assertEqual(self._token_key(self.user_id), self.owner_token_key)
        self._assert_profile_survived()

    def test_delete_cannot_commit_between_guard_precondition_and_guard_commit(self):
        for caller in ("owner", "admin"):
            with self.subTest(caller=caller):
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).delete()
                    session.query(models.ProfileAnalysis).delete()
                    session.commit()
                finally:
                    session.close()
                self.assertIsNone(self._guard_row(PROFILE_OPERATION_TYPE, self.profile_id))
                caller_id = self.user_id if caller == "owner" else self.admin_id

                response, outcomes, mock_openai = (
                    self._post_with_delete_attempt_after_guard_precondition(
                        self._headers(caller_id)
                    )
                )

                self.assertEqual(outcomes, ["locked"])
                self._assert_owner_a_survived()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "created")
                mock_openai.assert_called_once()

    def test_write_lock_held_even_when_upsert_changes_no_row(self):
        # Another in-flight request of A holds a live lease on both
        # resources, so the upserts' WHERE is false and they change nothing
        # -- the INSERT still began the write transaction.
        result = self._acquire_directly(self.profile_id, self.user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        guards_before = self._rows("analysis_guards", "operation_type, resource_id")

        response, outcomes, mock_openai = (
            self._post_with_delete_attempt_after_guard_precondition(
                self._headers(self.user_id)
            )
        )

        self.assertEqual(outcomes, ["locked"])
        self._assert_owner_a_survived()
        self.assertEqual(response.status_code, 409, response.text)
        mock_openai.assert_not_called()
        self.assertEqual(
            self._rows("analysis_guards", "operation_type, resource_id"), guards_before
        )


class ProfileGuardPreconditionTests(_BaseProfileGuardRaceTestCase):
    """4. The precondition hook of try_acquire_profile_analysis_guard."""

    def _acquire(self, **kwargs):
        session = self.session_factory()
        try:
            return try_acquire_profile_analysis_guard(
                session,
                profile_id=self.profile_id,
                owner_user_id=self.user_id,
                config=load_config(),
                **kwargs,
            ), session
        finally:
            session.close()

    def _assert_no_guard_rows(self):
        self.assertIsNone(self._guard_row(PROFILE_OPERATION_TYPE, self.profile_id))
        self.assertIsNone(self._guard_row(USER_OPERATION_TYPE, self.user_id))

    def test_true_grants_and_runs_on_a_separate_guard_session(self):
        seen = []
        result, caller_session = self._acquire(precondition=lambda s: seen.append(s) or True)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertTrue(result.owner_token)
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], caller_session)
        for key in ((PROFILE_OPERATION_TYPE, self.profile_id), (USER_OPERATION_TYPE, self.user_id)):
            self.assertEqual(self._guard_row(*key).owner_token, result.owner_token)

    def test_false_returns_target_changed_and_writes_nothing(self):
        result, _ = self._acquire(precondition=lambda s: False)
        self.assertEqual(result.outcome, AcquireOutcome.TARGET_CHANGED)
        self.assertIsNone(result.owner_token)
        self._assert_no_guard_rows()

    def test_false_wins_over_active_lease_and_cooldown(self):
        for state in ("lease", "cooldown"):
            for operation_type, resource_id in (
                (PROFILE_OPERATION_TYPE, self.profile_id),
                (USER_OPERATION_TYPE, self.user_id),
            ):
                with self.subTest(state=state, operation_type=operation_type):
                    session = self.session_factory()
                    try:
                        session.query(models.AnalysisGuard).delete()
                        session.commit()
                    finally:
                        session.close()
                    self._add_guard_row(operation_type, resource_id, state)
                    before = self._rows("analysis_guards", "operation_type, resource_id")

                    result, _ = self._acquire(precondition=lambda s: False)

                    self.assertEqual(result.outcome, AcquireOutcome.TARGET_CHANGED)
                    self.assertIsNone(result.owner_token)
                    self.assertEqual(
                        self._rows("analysis_guards", "operation_type, resource_id"), before
                    )

    def test_raising_fails_closed_as_backend_unavailable_and_writes_nothing(self):
        def boom(session):
            raise RuntimeError("synthetic precondition failure")

        result, _ = self._acquire(precondition=boom)
        self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        self.assertIsNone(result.owner_token)
        self._assert_no_guard_rows()

    def test_none_behaves_as_before(self):
        for kwargs in ({}, {"precondition": None}):
            with self.subTest(kwargs=kwargs):
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).delete()
                    session.commit()
                finally:
                    session.close()
                result, _ = self._acquire(**kwargs)
                self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
                second, _ = self._acquire(**kwargs)
                self.assertEqual(second.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)


class UnchangedOwnerRouteTests(_BaseProfileGuardRaceTestCase):
    """5. An unchanged profile/owner analyzes exactly as before; the route
    passes its identity re-check to the guard for owner and admin."""

    def _post_spied(self, headers):
        real_acquire = profiles.try_acquire_profile_analysis_guard
        with patch.object(
            profiles, "try_acquire_profile_analysis_guard", side_effect=real_acquire
        ) as mock_acquire, patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_valid_openai_response(),
        ) as mock_openai:
            response = self.client.post(self._url(), headers=headers)
        return response, mock_acquire, mock_openai

    def test_owner_and_admin_create_with_a_precondition(self):
        for caller in ("owner", "admin"):
            with self.subTest(caller=caller):
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).delete()
                    session.query(models.ProfileAnalysis).delete()
                    session.commit()
                finally:
                    session.close()
                caller_id = self.user_id if caller == "owner" else self.admin_id

                response, mock_acquire, mock_openai = self._post_spied(self._headers(caller_id))

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "created")
                mock_openai.assert_called_once()
                mock_acquire.assert_called_once()
                self.assertTrue(callable(mock_acquire.call_args.kwargs.get("precondition")))
                # Released with the success cooldown, owner_token cleared.
                for key in (
                    (PROFILE_OPERATION_TYPE, self.profile_id),
                    (USER_OPERATION_TYPE, self.user_id),
                ):
                    row = self._guard_row(*key)
                    self.assertIsNone(row.owner_token)
                    self.assertIsNotNone(row.cooldown_until)

    def test_cached_analysis_never_touches_the_guard(self):
        self._seed_profile_analysis(self.profile_id)
        response, mock_acquire, mock_openai = self._post_spied(self._headers(self.user_id))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cached")
        mock_acquire.assert_not_called()
        mock_openai.assert_not_called()

    def test_live_lease_is_still_409_and_cooldown_still_429(self):
        result = self._acquire_directly(self.profile_id, self.user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        response, _, mock_openai = self._post_spied(self._headers(self.user_id))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("Retry-After", response.headers)
        mock_openai.assert_not_called()

        session = self.session_factory()
        try:
            session.query(models.AnalysisGuard).delete()
            session.commit()
        finally:
            session.close()
        self._add_guard_row(USER_OPERATION_TYPE, self.user_id, "cooldown")
        response, _, mock_openai = self._post_spied(self._headers(self.admin_id))
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("Retry-After", response.headers)
        mock_openai.assert_not_called()

    def test_precondition_failure_is_503_and_acquires_nothing(self):
        with patch.object(
            profiles,
            "profile_owner_unchanged",
            side_effect=RuntimeError("synthetic identity re-check failure"),
        ):
            response, _, mock_openai = self._post_spied(self._headers(self.user_id))
        self.assertEqual(response.status_code, 503, response.text)
        mock_openai.assert_not_called()
        self.assertIsNone(self._guard_row(PROFILE_OPERATION_TYPE, self.profile_id))
        self.assertIsNone(self._guard_row(USER_OPERATION_TYPE, self.user_id))


if __name__ == "__main__":
    unittest.main()
