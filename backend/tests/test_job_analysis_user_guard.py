"""Per-user in-flight + cooldown guard for a normal (non-admin) user
analyzing their OWN manual job through POST /jobs/{job_id}/analyze.

On that path only, the route acquires (job_analysis, job_id) AND
(job_analysis_user, user_id) under one owner_token in one guard
transaction, and releases both together: the job row gets the existing
job cooldowns (success/failure), the user row its own
JOB_ANALYSIS_USER_SUCCESS/FAILURE_COOLDOWN_SECONDS (defaults 60/30). So a
user cannot run two paid analyses of their own manual jobs at the same
time (409) or start another one right after the previous one (429 with
Retry-After). Admins, the cached path, analyze-missing and analyze-sample
take no user resource, exactly as before. users.delete_user removes the
user's job_analysis_user row, so a new account reusing the user_id never
inherits it.

Throwaway SQLite only (the base below creates and removes its own temp
dir); backend.app.main is never imported and apply101.db is never touched.
The base class imported below defines no tests, so nothing is collected
twice.
"""

import inspect
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app import analysis_guard, models
from backend.app.analysis_guard import (
    AcquireOutcome,
    AnalysisGuardConfigError,
    JOB_OPERATION_TYPE,
    JobAnalysisConfig,
    load_job_analysis_config,
    release_job_analysis_guard,
    try_acquire_job_analysis_guard,
)
from backend.tests.test_owner_manual_job_analysis import (
    _BaseOwnerManualAnalysisTestCase,
    _mock_job_response,
    _not_found_body,
    jobs,
    users,
)


# Persisted operation_type value of the new user dimension -- pinned here
# as a literal on purpose (it is stored in analysis_guards rows), and
# checked against the production constant below.
JOB_USER_OPERATION_TYPE = "job_analysis_user"

ALREADY_IN_PROGRESS_BODY = {"detail": "An analysis for this job is already in progress."}
TOO_MANY_REQUESTS_BODY = {"detail": "Too many requests. Please try again later."}

# Lower bound for "now" comparisons against REAL-clock guard timestamps
# (float epoch seconds); the guard's own time.time() call can never be
# earlier than one taken before the request.
_TOLERANCE = 1e-3


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _job_config(**overrides):
    # Deliberately the 5 pre-existing fields only, like the helper in
    # test_analysis_guard.py: the user cooldowns come from their defaults.
    defaults = dict(
        openai_timeout_seconds=60,
        openai_max_retries=0,
        lease_ttl_seconds=300,
        success_cooldown_seconds=300,
        failure_cooldown_seconds=30,
    )
    defaults.update(overrides)
    return JobAnalysisConfig(**defaults)


class _BaseJobUserGuardTestCase(_BaseOwnerManualAnalysisTestCase):
    def _guard_row(self, operation_type, resource_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM analysis_guards "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"op": operation_type, "rid": resource_id},
            ).fetchone()

    def _user_guard_row(self, user_id):
        return self._guard_row(JOB_USER_OPERATION_TYPE, user_id)

    def _job_row(self, job_id):
        return self._guard_row(JOB_OPERATION_TYPE, job_id)

    def _guard_keys(self):
        return {
            (row.operation_type, row.resource_id)
            for row in self._rows("analysis_guards", "operation_type, resource_id")
        }

    def _add(self, obj):
        session = self.session_factory()
        try:
            session.add(obj)
            session.commit()
        finally:
            session.close()

    def _add_guard_row(self, operation_type, resource_id, state, owner_token=None):
        now = time.time()
        self._add(models.AnalysisGuard(
            operation_type=operation_type,
            resource_id=resource_id,
            owner_token=(owner_token or "synthetic-other-owner-token") if state == "lease" else None,
            lock_expires_at=now + 600 if state == "lease" else None,
            cooldown_until=now + 600 if state == "cooldown" else None,
        ))

    def _token_key(self, user_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT token_key FROM users WHERE user_id = :uid"),
                {"uid": user_id},
            ).scalar()

    def _delete_user(self, user_id, session_factory=None):
        session = (session_factory or self.session_factory)()
        try:
            user = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one()
            return users.delete_user(user_id=user_id, current_user=user, db=session)
        finally:
            session.close()

    def _post_ok(self, job_id, headers):
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            response = self._post_analyze(job_id, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_openai.assert_called_once()
        return response

    def _post_failing(self, job_id, headers):
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=RuntimeError("synthetic upstream failure"),
        ) as mock_openai:
            response = self._post_analyze(job_id, headers=headers)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_JOB_ANALYSIS_FAILED")
        mock_openai.assert_called_once()
        return response

    def _assert_429_without_openai(self, job_id, headers, max_retry_after):
        before = self._snapshot()
        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self._post_analyze(job_id, headers=headers)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
        retry_after = int(response.headers["Retry-After"])
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, max_retry_after)
        mock_openai.assert_not_called()
        # Nothing acquired, released or cooled down by the rejected request.
        self.assertEqual(self._snapshot(), before)
        return retry_after


# ---------------------------------------------------------------------------
# Guard / config level
# ---------------------------------------------------------------------------


class JobUserGuardConfigTests(unittest.TestCase):
    def test_operation_type_constant(self):
        self.assertEqual(
            getattr(analysis_guard, "JOB_USER_OPERATION_TYPE", None), JOB_USER_OPERATION_TYPE
        )
        self.assertNotEqual(JOB_USER_OPERATION_TYPE, analysis_guard.JOB_OPERATION_TYPE)
        self.assertNotEqual(JOB_USER_OPERATION_TYPE, analysis_guard.USER_OPERATION_TYPE)

    def test_user_cooldown_defaults(self):
        config = load_job_analysis_config()
        self.assertEqual(config.user_success_cooldown_seconds, 60)
        self.assertEqual(config.user_failure_cooldown_seconds, 30)
        # Existing job values unchanged.
        self.assertEqual(config.success_cooldown_seconds, 300)
        self.assertEqual(config.failure_cooldown_seconds, 30)

    def test_user_cooldowns_read_from_env(self):
        with patch.dict(
            "os.environ",
            {
                "JOB_ANALYSIS_USER_SUCCESS_COOLDOWN_SECONDS": "45",
                "JOB_ANALYSIS_USER_FAILURE_COOLDOWN_SECONDS": "7",
            },
            clear=False,
        ):
            config = load_job_analysis_config()
        self.assertEqual(config.user_success_cooldown_seconds, 45)
        self.assertEqual(config.user_failure_cooldown_seconds, 7)

    def test_invalid_user_cooldowns_rejected(self):
        for env_name in (
            "JOB_ANALYSIS_USER_SUCCESS_COOLDOWN_SECONDS",
            "JOB_ANALYSIS_USER_FAILURE_COOLDOWN_SECONDS",
        ):
            for bad_value in ("0", "-1", "not-a-number"):
                with self.subTest(env_var=env_name, value=bad_value):
                    with patch.dict("os.environ", {env_name: bad_value}, clear=False):
                        with self.assertRaises(AnalysisGuardConfigError):
                            load_job_analysis_config()

    def test_existing_five_field_constructor_still_works_with_user_defaults(self):
        config = _job_config()
        self.assertEqual(config.user_success_cooldown_seconds, 60)
        self.assertEqual(config.user_failure_cooldown_seconds, 30)

    def test_owner_user_id_is_a_trailing_keyword_only_optional_parameter(self):
        for func, expected in (
            (try_acquire_job_analysis_guard,
             ["db", "job_id", "config", "clock", "precondition", "owner_user_id"]),
            (release_job_analysis_guard,
             ["db", "job_id", "owner_token", "succeeded", "config", "clock", "owner_user_id"]),
        ):
            with self.subTest(func=func.__name__):
                params = inspect.signature(func).parameters
                self.assertEqual(list(params), expected)
                self.assertIs(params["owner_user_id"].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIsNone(params["owner_user_id"].default)


class JobUserGuardLevelTests(_BaseJobUserGuardTestCase):
    def setUp(self):
        super().setUp()
        self.clock = _FakeClock()
        self.config = _job_config()

    # owner_user_id (when given, including an explicit None) is passed
    # through **kwargs untouched, so "omitted" and "None" are both tested.
    def _acquire(self, job_id, precondition=None, **kwargs):
        session = self.session_factory()
        try:
            if precondition is not None:
                kwargs["precondition"] = precondition
            return try_acquire_job_analysis_guard(
                session, job_id=job_id, config=self.config, clock=self.clock, **kwargs
            )
        finally:
            session.close()

    def _release(self, job_id, owner_token, succeeded, config=None, **kwargs):
        session = self.session_factory()
        try:
            return release_job_analysis_guard(
                session, job_id=job_id, owner_token=owner_token, succeeded=succeeded,
                config=config or self.config, clock=self.clock, **kwargs
            )
        finally:
            session.close()

    def test_owner_user_id_none_keeps_single_job_resource(self):
        for explicit_none in (False, True):
            with self.subTest(explicit_none=explicit_none):
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).delete()
                    session.commit()
                finally:
                    session.close()
                extra = {"owner_user_id": None} if explicit_none else {}
                result = self._acquire(1, **extra)
                self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
                self.assertEqual(self._guard_keys(), {(JOB_OPERATION_TYPE, 1)})

                self.assertTrue(self._release(1, result.owner_token, succeeded=True, **extra))
                self.assertEqual(self._guard_keys(), {(JOB_OPERATION_TYPE, 1)})
                row = self._job_row(1)
                self.assertIsNone(row.owner_token)
                self.assertEqual(row.cooldown_until, self.clock() + 300)

    def test_owner_user_id_acquires_both_rows_under_one_token(self):
        result = self._acquire(1, owner_user_id=10)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertEqual(
            self._guard_keys(),
            {(JOB_OPERATION_TYPE, 1), (JOB_USER_OPERATION_TYPE, 10)},
        )
        for row in (self._job_row(1), self._user_guard_row(10)):
            self.assertEqual(row.owner_token, result.owner_token)
            self.assertEqual(row.lock_expires_at, self.clock() + 300)
            self.assertIsNone(row.cooldown_until)

    def test_release_success_and_failure_cooldowns_per_resource(self):
        cases = (
            # (config, succeeded, expected job cooldown, expected user cooldown)
            (_job_config(), True, 300, 60),
            (_job_config(), False, 30, 30),
            (JobAnalysisConfig(
                openai_timeout_seconds=60, openai_max_retries=0, lease_ttl_seconds=300,
                success_cooldown_seconds=111, failure_cooldown_seconds=22,
                user_success_cooldown_seconds=44, user_failure_cooldown_seconds=5,
            ), True, 111, 44),
            (JobAnalysisConfig(
                openai_timeout_seconds=60, openai_max_retries=0, lease_ttl_seconds=300,
                success_cooldown_seconds=111, failure_cooldown_seconds=22,
                user_success_cooldown_seconds=44, user_failure_cooldown_seconds=5,
            ), False, 22, 5),
        )
        for index, (config, succeeded, job_cooldown, user_cooldown) in enumerate(cases):
            with self.subTest(case=index):
                job_id, user_id = 100 + index, 200 + index
                result = self._acquire(job_id, owner_user_id=user_id)
                self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
                self.assertTrue(self._release(
                    job_id, result.owner_token, succeeded=succeeded,
                    owner_user_id=user_id, config=config,
                ))
                job_row, user_row = self._job_row(job_id), self._user_guard_row(user_id)
                for row in (job_row, user_row):
                    self.assertIsNone(row.owner_token)
                    self.assertIsNone(row.lock_expires_at)
                self.assertEqual(job_row.cooldown_until, self.clock() + job_cooldown)
                self.assertEqual(user_row.cooldown_until, self.clock() + user_cooldown)

    def test_user_lease_blocks_another_job_of_the_same_user(self):
        first = self._acquire(1, owner_user_id=10)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)
        user_row_before = self._user_guard_row(10)

        second = self._acquire(2, owner_user_id=10)
        self.assertEqual(second.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)
        self.assertIsNone(second.owner_token)
        self.assertIsNone(self._job_row(2))  # rolled back, nothing left locked
        self.assertEqual(self._user_guard_row(10), user_row_before)

    def test_user_cooldown_blocks_another_job_until_it_expires(self):
        first = self._acquire(1, owner_user_id=10)
        self._release(1, first.owner_token, succeeded=True, owner_user_id=10)

        blocked = self._acquire(2, owner_user_id=10)
        self.assertEqual(blocked.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(blocked.retry_after_seconds, 60)
        self.assertIsNone(self._job_row(2))

        self.clock.advance(59)
        self.assertEqual(self._acquire(2, owner_user_id=10).retry_after_seconds, 1)

        self.clock.advance(1)
        allowed = self._acquire(2, owner_user_id=10)
        self.assertEqual(allowed.outcome, AcquireOutcome.GRANTED)

    def test_failure_user_cooldown_retry_after_is_30(self):
        first = self._acquire(1, owner_user_id=10)
        self._release(1, first.owner_token, succeeded=False, owner_user_id=10)
        blocked = self._acquire(2, owner_user_id=10)
        self.assertEqual(blocked.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(blocked.retry_after_seconds, 30)

    def test_different_users_do_not_block_each_other(self):
        a = self._acquire(1, owner_user_id=10)
        b = self._acquire(2, owner_user_id=20)
        self.assertEqual(a.outcome, AcquireOutcome.GRANTED)
        self.assertEqual(b.outcome, AcquireOutcome.GRANTED)
        # User 10 now cooling down, user 20 still in flight: a third user
        # is blocked by neither.
        self._release(1, a.owner_token, succeeded=True, owner_user_id=10)
        c = self._acquire(3, owner_user_id=30)
        self.assertEqual(c.outcome, AcquireOutcome.GRANTED)

    def test_without_owner_user_id_user_lease_and_cooldown_are_ignored(self):
        held = self._acquire(1, owner_user_id=10)
        self.assertEqual(held.outcome, AcquireOutcome.GRANTED)
        user_row_before = self._user_guard_row(10)

        admin_style = self._acquire(2)
        self.assertEqual(admin_style.outcome, AcquireOutcome.GRANTED)
        self._release(2, admin_style.owner_token, succeeded=True)
        self.assertEqual(self._user_guard_row(10), user_row_before)

    def test_false_precondition_wins_over_user_lease_and_cooldown_and_writes_nothing(self):
        for state in (None, "lease", "cooldown"):
            with self.subTest(state=state):
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).delete()
                    session.commit()
                finally:
                    session.close()
                if state is not None:
                    self._add_guard_row(JOB_USER_OPERATION_TYPE, 10, state)
                before = self._rows("analysis_guards", "operation_type, resource_id")

                result = self._acquire(1, owner_user_id=10, precondition=lambda s: False)

                self.assertEqual(result.outcome, AcquireOutcome.TARGET_CHANGED)
                self.assertIsNone(result.owner_token)
                self.assertEqual(
                    self._rows("analysis_guards", "operation_type, resource_id"), before
                )


# ---------------------------------------------------------------------------
# Route level: POST /jobs/{job_id}/analyze by the owner
# ---------------------------------------------------------------------------


class OwnerRouteUserGuardTests(_BaseJobUserGuardTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("job.user.guard.owner@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.job_1 = self._create_job("user-guard-1", owner_id=self.owner_id, source="manual")
        self.job_2 = self._create_job("user-guard-2", owner_id=self.owner_id, source="manual")

    def test_owner_route_passes_owner_user_id_to_acquire_and_release(self):
        with patch(
            "backend.routers.jobs.try_acquire_job_analysis_guard",
            wraps=jobs.try_acquire_job_analysis_guard,
        ) as mock_acquire, patch(
            "backend.routers.jobs.release_job_analysis_guard",
            wraps=jobs.release_job_analysis_guard,
        ) as mock_release:
            self._post_ok(self.job_1, self.headers)
        mock_acquire.assert_called_once()
        mock_release.assert_called_once()
        self.assertEqual(mock_acquire.call_args.kwargs.get("owner_user_id"), self.owner_id)
        self.assertTrue(callable(mock_acquire.call_args.kwargs.get("precondition")))
        self.assertEqual(mock_release.call_args.kwargs.get("owner_user_id"), self.owner_id)

    def test_both_rows_leased_under_one_token_during_the_openai_call(self):
        seen = {}

        def seam(prompt, *, timeout_seconds, max_retries):
            seen["job"] = self._job_row(self.job_1)
            seen["user"] = self._user_guard_row(self.owner_id)
            return _mock_job_response()

        with patch("backend.routers.jobs._create_job_analysis_response", side_effect=seam):
            response = self._post_analyze(self.job_1, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)

        self.assertIsNotNone(seen["job"])
        self.assertIsNotNone(seen["user"], "user resource was not acquired")
        self.assertIsNotNone(seen["job"].owner_token)
        self.assertEqual(seen["user"].owner_token, seen["job"].owner_token)
        self.assertEqual(seen["user"].lock_expires_at, seen["job"].lock_expires_at)
        self.assertIsNone(seen["user"].cooldown_until)

    def test_success_releases_both_with_their_own_cooldowns(self):
        start = time.time()
        self._post_ok(self.job_1, self.headers)
        end = time.time()

        job_row, user_row = self._job_row(self.job_1), self._user_guard_row(self.owner_id)
        self.assertIsNotNone(user_row, "user resource was not released into a cooldown")
        for row in (job_row, user_row):
            self.assertIsNone(row.owner_token)
            self.assertIsNone(row.lock_expires_at)
        self.assertGreaterEqual(job_row.cooldown_until, start + 300 - _TOLERANCE)
        self.assertLessEqual(job_row.cooldown_until, end + 300 + _TOLERANCE)
        self.assertGreaterEqual(user_row.cooldown_until, start + 60 - _TOLERANCE)
        self.assertLessEqual(user_row.cooldown_until, end + 60 + _TOLERANCE)

    def test_failure_releases_both_with_failure_cooldowns(self):
        start = time.time()
        self._post_failing(self.job_1, self.headers)
        end = time.time()

        job_row, user_row = self._job_row(self.job_1), self._user_guard_row(self.owner_id)
        self.assertIsNotNone(user_row, "user resource was not released into a cooldown")
        for row in (job_row, user_row):
            self.assertIsNone(row.owner_token)
            self.assertGreaterEqual(row.cooldown_until, start + 30 - _TOLERANCE)
            self.assertLessEqual(row.cooldown_until, end + 30 + _TOLERANCE)

    def test_other_job_right_after_success_is_429_retry_after_at_most_60(self):
        self._post_ok(self.job_1, self.headers)
        self._assert_429_without_openai(self.job_2, self.headers, max_retry_after=60)
        self.assertIsNone(self._job_row(self.job_2))

    def test_other_job_right_after_failure_is_429_retry_after_at_most_30(self):
        self._post_failing(self.job_1, self.headers)
        self._assert_429_without_openai(self.job_2, self.headers, max_retry_after=30)
        self.assertIsNone(self._job_row(self.job_2))

    def test_other_job_allowed_after_user_cooldown_expired(self):
        self._post_ok(self.job_1, self.headers)
        self.assertIsNotNone(self._user_guard_row(self.owner_id))
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE analysis_guards SET cooldown_until = :past "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"past": time.time() - 1, "op": JOB_USER_OPERATION_TYPE, "rid": self.owner_id},
            )
        self._post_ok(self.job_2, self.headers)

    def test_job_level_cooldown_unchanged_for_same_job(self):
        # Failure leaves no cache, so the same job reaches the guard again:
        # job and user rows are both cooling down for 30s.
        self._post_failing(self.job_1, self.headers)
        self._assert_429_without_openai(self.job_1, self.headers, max_retry_after=30)

    def test_cached_job_is_returned_during_user_cooldown_without_touching_guards(self):
        analysis_id = self._seed_completed_job_analysis(self.job_2)
        self._add_guard_row(JOB_USER_OPERATION_TYPE, self.owner_id, "cooldown")
        before = self._snapshot()
        response, spies = self._post_with_seams_spied(self.job_2, self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cached")
        self.assertEqual(response.json()["analysis_id"], analysis_id)
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)


class InFlightRaceTests(_BaseJobUserGuardTestCase):
    """The second request is issued through the REAL route while the first
    one is deterministically parked inside its (patched) OpenAI call --
    i.e. after it committed its guard rows and before it releases them."""

    def setUp(self):
        super().setUp()
        self.user_a = self._create_user("race.user.a@example.com")
        self.user_b = self._create_user("race.user.b@example.com")
        self.headers_a = self._auth_headers(self.user_a)
        self.headers_b = self._auth_headers(self.user_b)
        self.a_job_1 = self._create_job("race-a-1", owner_id=self.user_a, source="manual")
        self.a_job_2 = self._create_job("race-a-2", owner_id=self.user_a, source="manual")
        self.b_job = self._create_job("race-b-1", owner_id=self.user_b, source="manual")

    def _post_with_nested_request(self, outer_job, outer_headers, inner_job, inner_headers):
        calls = []
        inner = {}

        def seam(prompt, *, timeout_seconds, max_retries):
            calls.append(prompt)
            if len(calls) == 1:
                try:
                    inner["response"] = self._post_analyze(inner_job, headers=inner_headers)
                    inner["inner_job_row"] = self._job_row(inner_job)
                    inner["outer_job_row"] = self._job_row(outer_job)
                except BaseException as exc:  # surfaced by the assertions below
                    inner["error"] = exc
            return _mock_job_response()

        with patch(
            "backend.routers.jobs._create_job_analysis_response", side_effect=seam
        ) as mock_openai:
            outer = self._post_analyze(outer_job, headers=outer_headers)

        self.assertNotIn("error", inner, repr(inner.get("error")))
        self.assertIn("response", inner, "the nested request never ran")
        return outer, inner, mock_openai

    def test_same_user_other_job_in_flight_is_409(self):
        outer, inner, mock_openai = self._post_with_nested_request(
            self.a_job_1, self.headers_a, self.a_job_2, self.headers_a
        )

        self.assertEqual(inner["response"].status_code, 409, inner["response"].text)
        self.assertEqual(inner["response"].json(), ALREADY_IN_PROGRESS_BODY)
        self.assertNotIn("Retry-After", inner["response"].headers)
        self.assertIsNone(inner["inner_job_row"])
        self.assertIsNotNone(inner["outer_job_row"].owner_token)
        mock_openai.assert_called_once()  # only the first request's call

        self.assertEqual(outer.status_code, 200, outer.text)
        self.assertEqual(outer.json()["status"], "created")
        self.assertEqual(outer.json()["job_id"], self.a_job_1)
        # Nothing left locked or cooled for the rejected job.
        self.assertIsNone(self._job_row(self.a_job_2))
        self.assertEqual(
            [row.job_id for row in self._rows("job_analysis", "analysis_id")],
            [self.a_job_1],
        )
        user_row = self._user_guard_row(self.user_a)
        self.assertIsNone(user_row.owner_token)
        self.assertIsNotNone(user_row.cooldown_until)

    def test_different_users_are_not_blocked_by_each_other(self):
        outer, inner, mock_openai = self._post_with_nested_request(
            self.a_job_1, self.headers_a, self.b_job, self.headers_b
        )

        self.assertEqual(inner["response"].status_code, 200, inner["response"].text)
        self.assertEqual(inner["response"].json()["status"], "created")
        self.assertEqual(outer.status_code, 200, outer.text)
        self.assertEqual(outer.json()["status"], "created")
        self.assertEqual(mock_openai.call_count, 2)
        for user_id in (self.user_a, self.user_b):
            row = self._user_guard_row(user_id)
            self.assertIsNotNone(row)
            self.assertIsNone(row.owner_token)
            self.assertIsNotNone(row.cooldown_until)


class AdminAndBatchUnchangedTests(_BaseJobUserGuardTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self._create_user("job.user.guard.admin@example.com", is_admin=True)
        self.admin_headers = self._auth_headers(self.admin_id)
        self.owner_id = self._create_user("job.user.guard.admin.owner@example.com")
        self.owner_job = self._create_job(
            "admin-owner-manual", owner_id=self.owner_id, source="manual"
        )

    def _admin_post_spied(self, job_id):
        with patch(
            "backend.routers.jobs.try_acquire_job_analysis_guard",
            wraps=jobs.try_acquire_job_analysis_guard,
        ) as mock_acquire, patch(
            "backend.routers.jobs.release_job_analysis_guard",
            wraps=jobs.release_job_analysis_guard,
        ) as mock_release:
            response = self._post_ok(job_id, self.admin_headers)
        mock_acquire.assert_called_once()
        mock_release.assert_called_once()
        self.assertIsNone(mock_acquire.call_args.kwargs.get("owner_user_id"))
        self.assertIsNone(mock_release.call_args.kwargs.get("owner_user_id"))
        return response

    def test_admin_on_users_manual_job_takes_no_user_resource(self):
        self._admin_post_spied(self.owner_job)
        self.assertIsNone(self._user_guard_row(self.owner_id))
        self.assertIsNone(self._user_guard_row(self.admin_id))
        self.assertEqual(self._guard_keys(), {(JOB_OPERATION_TYPE, self.owner_job)})

    def test_admin_not_blocked_by_owner_user_lease_or_cooldown(self):
        for state in ("lease", "cooldown"):
            with self.subTest(state=state):
                job_id = self._create_job(
                    f"admin-blocked-{state}", owner_id=self.owner_id, source="manual"
                )
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).filter(
                        models.AnalysisGuard.operation_type == JOB_USER_OPERATION_TYPE
                    ).delete()
                    session.commit()
                finally:
                    session.close()
                self._add_guard_row(JOB_USER_OPERATION_TYPE, self.owner_id, state)
                user_row_before = self._user_guard_row(self.owner_id)

                self._admin_post_spied(job_id)

                self.assertEqual(self._user_guard_row(self.owner_id), user_row_before)

    def test_admin_on_catalog_job_takes_no_user_resource(self):
        catalog = self._create_job("admin-catalog-user-guard", source="arbeitnow")
        self._admin_post_spied(catalog)
        self.assertEqual(self._guard_keys(), {(JOB_OPERATION_TYPE, catalog)})

    def test_analyze_missing_takes_no_user_resource(self):
        catalog = self._create_job("missing-catalog-user-guard", source="arbeitnow")
        with patch(
            "backend.routers.jobs.try_acquire_job_analysis_guard",
            wraps=jobs.try_acquire_job_analysis_guard,
        ) as mock_acquire, patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ):
            response = self.client.post(
                "/jobs/analyze-missing", params={"limit": 1}, headers=self.admin_headers
            )
        self.assertEqual(response.status_code, 200, response.text)
        mock_acquire.assert_called_once()
        self.assertIsNone(mock_acquire.call_args.kwargs.get("owner_user_id"))
        self.assertNotIn(
            JOB_USER_OPERATION_TYPE, {op for op, _ in self._guard_keys()}
        )
        self.assertIn((JOB_OPERATION_TYPE, catalog), self._guard_keys())


# ---------------------------------------------------------------------------
# Identity race: owner replaced after the route's last check
# ---------------------------------------------------------------------------


class OwnerReplacedBeforeGuardCommitTests(_BaseJobUserGuardTestCase):
    """A is deleted (real users.delete_user) and B gets A's user_id plus a
    manual job with A's job_id and url, each step committed in its own
    session, right before the route's real guard acquisition runs."""

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("user.guard.replaced.a@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.owner_token_key = self._token_key(self.owner_id)
        self.job_slug = "user-guard-replaced"
        self.job_url = f"https://example.com/job/{self.job_slug}"
        self.job_id = self._create_job(self.job_slug, owner_id=self.owner_id, source="manual")
        self.replaced = False
        self.after_replacement = None

    def _full_snapshot(self):
        snapshot = self._snapshot()
        snapshot["users"] = self._rows("users", "user_id")
        return snapshot

    def _replace_owner_elsewhere(self, b_user_guard=None):
        self.assertFalse(self.replaced, "replacement must run exactly once")
        self.replaced = True

        self.assertEqual(self._delete_user(self.owner_id)["status"], "deleted")
        self.assertIsNone(self._user_guard_row(self.owner_id))
        self.assertIsNone(self._job_row(self.job_id))

        self._add(models.User(
            user_id=self.owner_id, name="Replacement User B",
            mail="user.guard.replaced.b@example.com",
        ))
        self._add(models.Job(
            job_id=self.job_id, title="Replacement Job of B", url=self.job_url,
            description_text="replacement description of b", source="manual",
            created_by_user_id=self.owner_id,
        ))
        if b_user_guard is not None:
            self._add_guard_row(
                JOB_USER_OPERATION_TYPE, self.owner_id, b_user_guard,
                owner_token="synthetic-owner-token-of-b",
            )

        b_token_key = self._token_key(self.owner_id)
        self.assertTrue(b_token_key)
        self.assertNotEqual(b_token_key, self.owner_token_key)
        self.after_replacement = self._full_snapshot()

    def _post_with_replacement_before_guard(self, b_user_guard=None):
        real_acquire = jobs.try_acquire_job_analysis_guard

        def replace_then_acquire(*args, **kwargs):
            self._replace_owner_elsewhere(b_user_guard=b_user_guard)
            return real_acquire(*args, **kwargs)

        with patch.object(
            jobs, "try_acquire_job_analysis_guard", side_effect=replace_then_acquire
        ) as mock_acquire, patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            response = self._post_analyze(self.job_id, headers=self.headers)

        mock_acquire.assert_called_once()
        self.assertTrue(self.replaced)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), _not_found_body(self.job_id))
        self.assertNotIn("Retry-After", response.headers)
        mock_openai.assert_not_called()
        self.assertEqual(self._full_snapshot(), self.after_replacement)
        self.assertIsNone(self._job_row(self.job_id))
        return response

    def test_replaced_before_guard_writes_neither_guard_row(self):
        self._post_with_replacement_before_guard()
        self.assertIsNone(self._user_guard_row(self.owner_id))
        self.assertEqual(self._guard_keys(), set())

    def test_b_user_lease_is_404_not_409_and_untouched(self):
        self._post_with_replacement_before_guard(b_user_guard="lease")
        expected = [
            row for row in self.after_replacement["analysis_guards"]
            if row.operation_type == JOB_USER_OPERATION_TYPE and row.resource_id == self.owner_id
        ]
        self.assertEqual(len(expected), 1)
        self.assertEqual(self._user_guard_row(self.owner_id), expected[0])

    def test_b_user_cooldown_is_404_not_429_and_untouched(self):
        self._post_with_replacement_before_guard(b_user_guard="cooldown")
        expected = [
            row for row in self.after_replacement["analysis_guards"]
            if row.operation_type == JOB_USER_OPERATION_TYPE and row.resource_id == self.owner_id
        ]
        self.assertEqual(len(expected), 1)
        self.assertEqual(self._user_guard_row(self.owner_id), expected[0])


class GuardPreconditionLockTests(_BaseJobUserGuardTestCase):
    """Right after the route's real precondition passed inside the guard
    transaction (which now also upserts the user row), A's deletion from
    another connection must fail with "database is locked"."""

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("user.guard.lock.a@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.owner_token_key = self._token_key(self.owner_id)
        self.job_id = self._create_job("user-guard-lock", owner_id=self.owner_id, source="manual")

    def _post_with_delete_attempt_after_precondition(self):
        blocked_engine = create_engine(
            f"sqlite:///{self.engine.url.database}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        blocked_factory = sessionmaker(autocommit=False, autoflush=False, bind=blocked_engine)
        real_acquire = jobs.try_acquire_job_analysis_guard
        outcomes = []
        acquire_kwargs = []

        def acquire_wrapping_precondition(*args, **kwargs):
            acquire_kwargs.append(dict(kwargs))
            precondition = kwargs["precondition"]

            def precondition_then_try_delete(guard_db):
                result = precondition(guard_db)
                if result and not outcomes:
                    try:
                        self._delete_user(self.owner_id, blocked_factory)
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
                jobs, "try_acquire_job_analysis_guard", side_effect=acquire_wrapping_precondition
            ), patch(
                "backend.routers.jobs._create_job_analysis_response",
                return_value=_mock_job_response(),
            ) as mock_openai:
                response = self._post_analyze(self.job_id, headers=self.headers)
        finally:
            # Disposed here, before tearDown's rmtree.
            blocked_engine.dispose()
        self.assertEqual(len(acquire_kwargs), 1)
        self.assertEqual(acquire_kwargs[0].get("owner_user_id"), self.owner_id)
        return response, outcomes, mock_openai

    def _assert_owner_survived(self):
        self.assertEqual(self._token_key(self.owner_id), self.owner_token_key)
        self.assertEqual(
            [(row.job_id, row.created_by_user_id)
             for row in self._rows("jobs", "job_id") if row.job_id == self.job_id],
            [(self.job_id, self.owner_id)],
        )

    def test_delete_cannot_commit_between_precondition_and_guard_commit(self):
        response, outcomes, mock_openai = self._post_with_delete_attempt_after_precondition()
        self.assertEqual(outcomes, ["locked"])
        self._assert_owner_survived()
        self.assertEqual(response.status_code, 200, response.text)
        mock_openai.assert_called_once()
        user_row = self._user_guard_row(self.owner_id)
        self.assertIsNotNone(user_row)
        self.assertIsNone(user_row.owner_token)
        self.assertIsNotNone(user_row.cooldown_until)

    def test_lock_held_when_only_the_user_row_blocks(self):
        # A's other request holds a live lease on the user row only: the
        # user upsert changes nothing, the lock is still held, and the
        # result is 409 for a job that is itself free.
        self._add_guard_row(
            JOB_USER_OPERATION_TYPE, self.owner_id, "lease", owner_token="synthetic-a-other"
        )
        user_row_before = self._user_guard_row(self.owner_id)

        response, outcomes, mock_openai = self._post_with_delete_attempt_after_precondition()

        self.assertEqual(outcomes, ["locked"])
        self._assert_owner_survived()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), ALREADY_IN_PROGRESS_BODY)
        mock_openai.assert_not_called()
        self.assertEqual(self._user_guard_row(self.owner_id), user_row_before)
        self.assertIsNone(self._job_row(self.job_id))


# ---------------------------------------------------------------------------
# users.delete_user cleanup and user_id reuse
# ---------------------------------------------------------------------------


class DeleteUserCleanupTests(_BaseJobUserGuardTestCase):
    def setUp(self):
        super().setUp()
        self.user_a = self._create_user("cleanup.user.a@example.com")
        self.bystander = self._create_user("cleanup.bystander@example.com")
        self.a_job = self._create_job("cleanup-a", owner_id=self.user_a, source="manual")
        self._add_guard_row(
            JOB_USER_OPERATION_TYPE, self.bystander, "lease", owner_token="bystander-token"
        )
        self.bystander_row = self._user_guard_row(self.bystander)

    def _reuse_id_and_analyze(self):
        self._add(models.User(
            user_id=self.user_a, name="Reusing User B", mail="cleanup.user.b@example.com",
        ))
        b_job = self._create_job("cleanup-b", owner_id=self.user_a, source="manual")
        self.assertTrue(self._token_key(self.user_a))
        self._post_ok(b_job, self._auth_headers(self.user_a))
        return b_job

    def _assert_delete_removes_only_a_row(self):
        result = self._delete_user(self.user_a)
        self.assertEqual(
            set(result),
            {"status", "user_id", "deleted_profiles_count", "deleted_profile_analyses_count",
             "deleted_matches_count", "deleted_languages_count"},
        )
        self.assertEqual(result["status"], "deleted")
        self.assertIsNone(self._user_guard_row(self.user_a))
        self.assertEqual(self._user_guard_row(self.bystander), self.bystander_row)

    def test_cooldown_from_real_analysis_removed_and_reused_id_can_analyze(self):
        self._post_ok(self.a_job, self._auth_headers(self.user_a))
        a_row = self._user_guard_row(self.user_a)
        self.assertIsNotNone(a_row, "A's analysis set no user cooldown")
        self.assertGreater(a_row.cooldown_until, time.time())

        self._assert_delete_removes_only_a_row()
        self._reuse_id_and_analyze()

    def test_in_flight_lease_removed_and_reused_id_can_analyze(self):
        self._add_guard_row(JOB_USER_OPERATION_TYPE, self.user_a, "lease", owner_token="a-token")
        self._assert_delete_removes_only_a_row()
        self._reuse_id_and_analyze()

    def test_cooldown_row_removed_and_reused_id_can_analyze(self):
        self._add_guard_row(JOB_USER_OPERATION_TYPE, self.user_a, "cooldown")
        self._assert_delete_removes_only_a_row()
        self._reuse_id_and_analyze()


if __name__ == "__main__":
    unittest.main()
