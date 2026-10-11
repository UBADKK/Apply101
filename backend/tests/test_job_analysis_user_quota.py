"""Per-user rolling quota for owner manual job analysis.

A normal (non-admin) user analyzing their OWN manual job through POST
/jobs/{job_id}/analyze (the only path that passes owner_user_id to
try_acquire_job_analysis_guard) gets at most
JOB_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS (default 10) counted attempts per
rolling JOB_ANALYSIS_USER_QUOTA_WINDOW_SECONDS (default 86400). An attempt
is counted when a row is inserted into analysis_quota_reservations inside
the guard's own write transaction, right before the guard commit (and so
right before the OpenAI call); reservations are never refunded. When the
quota is used up, the guard returns QUOTA_EXCEEDED, writes nothing, and the
route answers 429 with the existing "Too many requests" body and a
Retry-After of ceil(oldest counted reservation + window - now).

Throwaway SQLite only (the base below creates and removes its own temp
dir); backend.app.main is never imported and apply101.db is never touched.
The base classes imported below define no tests, so nothing is collected
twice. The quota table is only accessed through raw SQL here.
"""

import json
import math
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app import analysis_guard, models
from backend.app.database import get_db
from backend.app.analysis_guard import (
    AcquireOutcome,
    AnalysisGuardConfigError,
    JOB_BATCH_OPERATION_TYPE,
    JOB_OPERATION_TYPE,
    JOB_USER_OPERATION_TYPE,
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
    load_config,
    load_job_analysis_batch_config,
    load_job_analysis_config,
    release_job_analysis_guard,
    try_acquire_job_analysis_guard,
    try_acquire_job_batch_guard,
    try_acquire_profile_analysis_guard,
)
from backend.app.openai_client import ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL
from backend.tests.test_inflight_profile_delete_writes import (
    VALID_ANALYSIS_PAYLOAD as VALID_PROFILE_ANALYSIS_PAYLOAD,
    _openai_response,
    profiles,
)
from backend.tests.test_job_analysis_user_guard import (
    ALREADY_IN_PROGRESS_BODY,
    TOO_MANY_REQUESTS_BODY,
    _BaseJobUserGuardTestCase,
    _FakeClock,
    _job_config,
)
from backend.tests.test_owner_manual_job_analysis import (
    FORCE_REANALYZE_ADMIN_ONLY_BODY,
    OPENAI_CREDENTIAL_ENVS,
    _mock_job_response,
    _not_found_body,
    jobs,
)


QUOTA_TABLE = "analysis_quota_reservations"
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_WINDOW_SECONDS = 86400

_INSERT_RESERVATION_SQL = (
    "INSERT INTO analysis_quota_reservations "
    "(operation_type, user_id, job_id, reserved_at) "
    "VALUES (:op, :uid, :jid, :at)"
)

# analyze-sample's own unstructured response shape.
VALID_JOB_SAMPLE_PAYLOAD = {
    "summary": "synthetic sample summary",
    "role_family": "other",
    "role_subfamily": "other",
    "normalized_role_title": "synthetic role",
    "required_skills": [],
    "preferred_skills": [],
    "responsibilities": [],
    "seniority_level": "unknown",
    "language_requirements": [],
    "visa_sponsorship": "unknown",
    "work_type": "unknown",
    "employment_type": "unknown",
    "dealbreakers": [],
}


def _mock_sample_response():
    response = MagicMock()
    response.output_text = json.dumps(VALID_JOB_SAMPLE_PAYLOAD)
    return response


def _invalid_json_response():
    response = MagicMock()
    response.output_text = "not valid json{{{"
    return response


class _BaseQuotaTestCase(_BaseJobUserGuardTestCase):
    # -- quota table (raw SQL) --------------------------------------------

    def _reservations(self, user_id=None):
        sql = "SELECT id, operation_type, user_id, job_id, reserved_at FROM " + QUOTA_TABLE
        params = {}
        if user_id is not None:
            sql += " WHERE user_id = :uid"
            params["uid"] = user_id
        sql += " ORDER BY id"
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).fetchall()

    def _count(self, user_id):
        return len(self._reservations(user_id))

    def _seed_reservations(self, user_id, reserved_ats, operation_type=JOB_USER_OPERATION_TYPE,
                           job_id=None):
        with self.engine.begin() as connection:
            for reserved_at in reserved_ats:
                connection.execute(
                    text(_INSERT_RESERVATION_SQL),
                    {"op": operation_type, "uid": user_id, "jid": job_id, "at": reserved_at},
                )

    def _state(self):
        state = self._snapshot()
        state[QUOTA_TABLE] = self._rows(QUOTA_TABLE, "id")
        return state

    def _clear_cooldowns(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE analysis_guards SET cooldown_until = NULL"))

    def _route_clock(self, clock):
        """Injects `clock` into the route's real guard acquisition."""
        real_acquire = jobs.try_acquire_job_analysis_guard

        def acquire_with_clock(*args, **kwargs):
            kwargs["clock"] = clock
            return real_acquire(*args, **kwargs)

        return patch.object(jobs, "try_acquire_job_analysis_guard", side_effect=acquire_with_clock)

    def _assert_quota_429(self, job_id, headers):
        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self._post_analyze(job_id, headers=headers)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
        mock_openai.assert_not_called()
        return int(response.headers["Retry-After"])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class QuotaConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = load_job_analysis_config()
        self.assertEqual(config.user_quota_max_attempts, DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(config.user_quota_window_seconds, DEFAULT_WINDOW_SECONDS)

    def test_read_from_env(self):
        with patch.dict(
            os.environ,
            {
                "JOB_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS": "3",
                "JOB_ANALYSIS_USER_QUOTA_WINDOW_SECONDS": "600",
            },
            clear=False,
        ):
            config = load_job_analysis_config()
        self.assertEqual(config.user_quota_max_attempts, 3)
        self.assertEqual(config.user_quota_window_seconds, 600)

    def test_invalid_values_rejected(self):
        for env_name in (
            "JOB_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS",
            "JOB_ANALYSIS_USER_QUOTA_WINDOW_SECONDS",
        ):
            for bad_value in ("0", "-1", "not-a-number"):
                with self.subTest(env_var=env_name, value=bad_value):
                    with patch.dict(os.environ, {env_name: bad_value}, clear=False):
                        with self.assertRaises(AnalysisGuardConfigError):
                            load_job_analysis_config()

    def test_five_field_constructor_gets_quota_defaults(self):
        config = _job_config()
        self.assertEqual(config.user_quota_max_attempts, DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(config.user_quota_window_seconds, DEFAULT_WINDOW_SECONDS)

    def test_quota_exceeded_outcome_value(self):
        self.assertEqual(AcquireOutcome.QUOTA_EXCEEDED.value, "quota_exceeded")


# ---------------------------------------------------------------------------
# Guard level (fake clock)
# ---------------------------------------------------------------------------


class QuotaGuardLevelTests(_BaseQuotaTestCase):
    USER = 10

    def setUp(self):
        super().setUp()
        self.clock = _FakeClock()
        self.config = _job_config()
        self.next_job_id = 1

    def _acquire(self, job_id, owner_user_id=USER, config=None, db_session_factory=None, **kwargs):
        session = (db_session_factory or self.session_factory)()
        try:
            if owner_user_id is not None:
                kwargs["owner_user_id"] = owner_user_id
            return try_acquire_job_analysis_guard(
                session, job_id=job_id, config=config or self.config, clock=self.clock, **kwargs
            )
        finally:
            session.close()

    def _release(self, job_id, owner_token, owner_user_id=USER, succeeded=True):
        session = self.session_factory()
        try:
            return release_job_analysis_guard(
                session, job_id=job_id, owner_token=owner_token, succeeded=succeeded,
                config=self.config, clock=self.clock, owner_user_id=owner_user_id,
            )
        finally:
            session.close()

    def _new_job_id(self):
        job_id = self.next_job_id
        self.next_job_id += 1
        return job_id

    def _counted_attempt(self, user_id=USER):
        """One granted owner acquisition on a fresh job id, released, then
        the clock moves past the user's success cooldown (60 s)."""
        job_id = self._new_job_id()
        result = self._acquire(job_id, owner_user_id=user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertTrue(self._release(job_id, result.owner_token, owner_user_id=user_id))
        self.clock.advance(60)
        return job_id

    def _put_guard(self, operation_type, resource_id, state):
        now = self.clock()
        self._add(models.AnalysisGuard(
            operation_type=operation_type,
            resource_id=resource_id,
            owner_token="synthetic-other-owner-token" if state == "lease" else None,
            lock_expires_at=now + 600 if state == "lease" else None,
            cooldown_until=now + 600 if state == "cooldown" else None,
        ))

    def _reset_guards(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM analysis_guards"))

    # -- counting ------------------------------------------------------------

    def test_granted_owner_acquisition_reserves_one_slot(self):
        result = self._acquire(7)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        rows = self._reservations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0].operation_type, rows[0].user_id, rows[0].job_id, rows[0].reserved_at),
            (JOB_USER_OPERATION_TYPE, self.USER, 7, self.clock()),
        )
        # Release does not refund it.
        self.assertTrue(self._release(7, result.owner_token, succeeded=False))
        self.assertEqual(self._reservations(), rows)

    def test_ten_granted_then_eleventh_quota_exceeded_writes_nothing(self):
        start = self.clock()
        for attempt in range(DEFAULT_MAX_ATTEMPTS):
            self._counted_attempt()
            self.assertEqual(self._count(self.USER), attempt + 1)

        before = self._state()
        job_id = self._new_job_id()
        result = self._acquire(job_id)

        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertIsNone(result.owner_token)
        self.assertEqual(
            result.retry_after_seconds,
            math.ceil(start + DEFAULT_WINDOW_SECONDS - self.clock()),
        )
        self.assertEqual(result.retry_after_seconds, DEFAULT_WINDOW_SECONDS - 600)
        self.assertEqual(self._state(), before)
        self.assertIsNone(self._job_row(job_id))

    def test_rolling_window_boundary(self):
        t0 = self.clock()
        window = DEFAULT_WINDOW_SECONDS
        oldest = t0 - window + 123.5
        second = t0 - window + 200
        self._seed_reservations(self.USER, [oldest, second] + [t0 - 5] * 8)

        result = self._acquire(1)
        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(result.retry_after_seconds, 124)

        self.clock.advance(122.5)  # exactly 1 s before the oldest expires
        result = self._acquire(1)
        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(result.retry_after_seconds, 1)

        self.clock.advance(0.5)  # 0.5 s left -> still rounded up to 1
        self.assertEqual(self._acquire(1).retry_after_seconds, 1)
        self.assertEqual(self._count(self.USER), 10)

        self.clock.advance(0.5)  # now == oldest + window: expired
        granted = self._acquire(1)
        self.assertEqual(granted.outcome, AcquireOutcome.GRANTED)
        reserved_ats = [row.reserved_at for row in self._reservations(self.USER)]
        self.assertEqual(len(reserved_ats), 10)
        self.assertNotIn(oldest, reserved_ats)  # pruned
        self.assertIn(self.clock(), reserved_ats)
        self.assertTrue(self._release(1, granted.owner_token))

        # Full again; the next slot frees when the second-oldest expires.
        self.clock.advance(60)
        result = self._acquire(2)
        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(result.retry_after_seconds, math.ceil(second + window - self.clock()))

    def test_retry_after_uses_the_row_whose_expiry_frees_a_slot(self):
        # More rows than the (lowered) maximum: 5 rows, max 3.
        t0 = self.clock()
        self._seed_reservations(self.USER, [t0 - 50, t0 - 40, t0 - 30, t0 - 20, t0 - 10])
        config = _job_config(user_quota_max_attempts=3, user_quota_window_seconds=100)
        result = self._acquire(1, config=config)
        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(result.retry_after_seconds, 70)

    def test_expired_rows_are_not_pruned_when_quota_is_exceeded(self):
        t0 = self.clock()
        window = DEFAULT_WINDOW_SECONDS
        self._seed_reservations(self.USER, [t0 - window - 5, t0 - window] + [t0 - 1] * 10)
        before = self._state()
        result = self._acquire(1)
        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(result.retry_after_seconds, window - 1)
        self.assertEqual(self._state(), before)

    def test_expired_rows_are_pruned_when_a_slot_is_reserved(self):
        t0 = self.clock()
        window = DEFAULT_WINDOW_SECONDS
        self._seed_reservations(self.USER, [t0 - window - 5, t0 - window] + [t0 - 1] * 9)
        other_user_stale = t0 - window - 100
        self._seed_reservations(20, [other_user_stale])
        result = self._acquire(1)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        reserved_ats = sorted(row.reserved_at for row in self._reservations(self.USER))
        self.assertEqual(reserved_ats, [t0 - 1] * 9 + [t0])
        # Only this user's rows are pruned.
        self.assertEqual([row.reserved_at for row in self._reservations(20)], [other_user_stale])

    def test_quota_is_per_user_and_per_operation_type(self):
        t0 = self.clock()
        self._seed_reservations(self.USER, [t0 - 1] * 10)
        self._seed_reservations(30, [t0 - 1] * 10, operation_type=USER_OPERATION_TYPE)
        self.assertEqual(self._acquire(1).outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(self._acquire(2, owner_user_id=20).outcome, AcquireOutcome.GRANTED)
        self.assertEqual(self._acquire(3, owner_user_id=30).outcome, AcquireOutcome.GRANTED)
        self.assertEqual(self._count(20), 1)
        self.assertEqual(
            len([row for row in self._reservations(30)
                 if row.operation_type == JOB_USER_OPERATION_TYPE]),
            1,
        )

    # -- not counted -----------------------------------------------------------

    def test_blocked_or_target_changed_acquisitions_reserve_nothing(self):
        for full_quota in (False, True):
            for case in ("target_changed", "user_lease", "user_cooldown", "job_lease",
                         "job_cooldown"):
                with self.subTest(full_quota=full_quota, case=case):
                    self._reset_guards()
                    with self.engine.begin() as connection:
                        connection.execute(text("DELETE FROM " + QUOTA_TABLE))
                    if full_quota:
                        self._seed_reservations(self.USER, [self.clock() - 1] * 10)
                    kwargs = {}
                    expected = {
                        "target_changed": AcquireOutcome.TARGET_CHANGED,
                        "user_lease": AcquireOutcome.ALREADY_IN_PROGRESS,
                        "job_lease": AcquireOutcome.ALREADY_IN_PROGRESS,
                        "user_cooldown": AcquireOutcome.COOLDOWN_ACTIVE,
                        "job_cooldown": AcquireOutcome.COOLDOWN_ACTIVE,
                    }[case]
                    if case == "target_changed":
                        # Wins over a lease too.
                        self._put_guard(JOB_USER_OPERATION_TYPE, self.USER, "lease")
                        kwargs["precondition"] = lambda guard_session: False
                    elif case.startswith("user_"):
                        self._put_guard(JOB_USER_OPERATION_TYPE, self.USER, case[5:])
                    else:
                        self._put_guard(JOB_OPERATION_TYPE, 1, case[4:])
                    before = self._state()

                    result = self._acquire(1, **kwargs)

                    self.assertEqual(result.outcome, expected)
                    self.assertIsNone(result.owner_token)
                    if expected is AcquireOutcome.COOLDOWN_ACTIVE:
                        self.assertEqual(result.retry_after_seconds, 600)
                    self.assertEqual(self._state(), before)

    def test_acquisitions_without_owner_user_id_never_reserve(self):
        self._seed_reservations(self.USER, [self.clock() - 1] * 10)
        before = self._reservations()

        admin_style = self._acquire(1, owner_user_id=None)
        self.assertEqual(admin_style.outcome, AcquireOutcome.GRANTED)

        session = self.session_factory()
        try:
            batch = try_acquire_job_batch_guard(
                session,
                config=load_job_analysis_batch_config(job_guard_lease_seconds=300),
                clock=self.clock,
            )
            profile = try_acquire_profile_analysis_guard(
                session, profile_id=5, owner_user_id=self.USER, config=load_config(),
                clock=self.clock,
            )
        finally:
            session.close()
        self.assertEqual(batch.outcome, AcquireOutcome.GRANTED)
        self.assertEqual(profile.outcome, AcquireOutcome.GRANTED)

        self.assertEqual(self._reservations(), before)
        self.assertEqual(
            self._guard_keys(),
            {(JOB_OPERATION_TYPE, 1), (JOB_BATCH_OPERATION_TYPE, 0),
             (PROFILE_OPERATION_TYPE, 5), (USER_OPERATION_TYPE, self.USER)},
        )

    # -- failures fail closed --------------------------------------------------

    def test_reservation_error_is_backend_unavailable_and_writes_nothing(self):
        with patch.object(
            analysis_guard, "_reserve_quota_slot",
            side_effect=RuntimeError("synthetic quota failure"),
        ) as mock_reserve:
            result = self._acquire(1)
        mock_reserve.assert_called_once()
        self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        self.assertIsNone(result.owner_token)
        self.assertEqual(self._guard_keys(), set())
        self.assertEqual(self._reservations(), [])

    def test_missing_quota_table_fails_closed_for_owners_only(self):
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE " + QUOTA_TABLE))

        result = self._acquire(1)
        self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        self.assertEqual(self._guard_keys(), set())

        self.assertEqual(self._acquire(1, owner_user_id=None).outcome, AcquireOutcome.GRANTED)

    def test_commit_failure_rolls_back_the_reservation(self):
        real_guard_session = analysis_guard._guard_session
        real_reserve = analysis_guard._reserve_quota_slot
        reserved = []

        def guard_session_with_failing_commit(db):
            session = real_guard_session(db)

            def failing_commit():
                raise OperationalError("COMMIT", {}, Exception("synthetic commit failure"))

            session.commit = failing_commit
            return session

        def reserve_spy(guard_session, quota, now):
            result = real_reserve(guard_session, quota, now)
            reserved.append(result)
            return result

        with patch.object(
            analysis_guard, "_guard_session", side_effect=guard_session_with_failing_commit
        ), patch.object(analysis_guard, "_reserve_quota_slot", side_effect=reserve_spy):
            result = self._acquire(1)

        self.assertEqual(reserved, [None])  # the slot was reserved in the transaction
        self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._guard_keys(), set())


# ---------------------------------------------------------------------------
# Route level: POST /jobs/{job_id}/analyze by the owner
# ---------------------------------------------------------------------------


class OwnerRouteQuotaTests(_BaseQuotaTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("quota.owner@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.slug_counter = 0

    def _new_job(self, **kwargs):
        self.slug_counter += 1
        return self._create_job(
            f"quota-{self.slug_counter}", owner_id=kwargs.pop("owner_id", self.owner_id),
            source=kwargs.pop("source", "manual"), **kwargs
        )

    def test_ten_counted_then_eleventh_is_429_with_retry_after(self):
        job_ids = [self._new_job() for _ in range(DEFAULT_MAX_ATTEMPTS + 1)]
        for job_id in job_ids[:DEFAULT_MAX_ATTEMPTS]:
            self._post_ok(job_id, self.headers)
            self._clear_cooldowns()

        before = self._snapshot()
        start = time.time()
        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self._post_analyze(job_ids[-1], headers=self.headers)
        end = time.time()

        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
        mock_openai.assert_not_called()
        self.assertEqual(self._snapshot(), before)
        self.assertNotIn(job_ids[-1], [row.job_id for row in self._rows("job_analysis", "analysis_id")])
        self.assertIsNone(self._job_row(job_ids[-1]))

        rows = self._reservations(self.owner_id)
        self.assertEqual(sorted(row.job_id for row in rows), sorted(job_ids[:-1]))
        self.assertTrue(all(row.reserved_at <= start for row in rows))
        oldest = min(row.reserved_at for row in rows)
        retry_after = int(response.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, math.ceil(oldest + DEFAULT_WINDOW_SECONDS - end))
        self.assertLessEqual(retry_after, math.ceil(oldest + DEFAULT_WINDOW_SECONDS - start))

    def test_exact_retry_after_and_rolling_window_with_fake_clock(self):
        clock = _FakeClock()
        t0 = clock()
        oldest = t0 - DEFAULT_WINDOW_SECONDS + 123.5
        self._seed_reservations(self.owner_id, [oldest] + [t0 - 5] * 9)
        job_id = self._new_job()

        before = self._state()
        with self._route_clock(clock):
            self.assertEqual(self._assert_quota_429(job_id, self.headers), 124)
            self.assertEqual(self._state(), before)

            clock.advance(122.5)
            self.assertEqual(self._assert_quota_429(job_id, self.headers), 1)
            self.assertEqual(self._state(), before)

            clock.advance(1)
            self._post_ok(job_id, self.headers)

        reserved_ats = [row.reserved_at for row in self._reservations(self.owner_id)]
        self.assertEqual(len(reserved_ats), 10)
        self.assertNotIn(oldest, reserved_ats)
        self.assertIn(clock(), reserved_ats)

    def test_counted_outcomes(self):
        def changes_job_url_then_succeeds(job_id):
            def seam(prompt, *, timeout_seconds, max_retries):
                with self.engine.begin() as connection:
                    connection.execute(
                        text("UPDATE jobs SET url = :url WHERE job_id = :jid"),
                        {"url": f"https://example.com/changed/{job_id}", "jid": job_id},
                    )
                return _mock_job_response()
            return seam

        cases = (
            ("success", lambda job_id: {"return_value": _mock_job_response()}, 200, None),
            ("openai_exception",
             lambda job_id: {"side_effect": RuntimeError("synthetic upstream failure")},
             500, "ERR_JOB_ANALYSIS_FAILED"),
            ("invalid_json", lambda job_id: {"return_value": _invalid_json_response()},
             500, "ERR_AI_INVALID_JSON"),
            ("identity_mismatch_after_call",
             lambda job_id: {"side_effect": changes_job_url_then_succeeds(job_id)}, 404, None),
        )
        for index, (name, seam_kwargs, status, error_code) in enumerate(cases):
            with self.subTest(case=name):
                job_id = self._new_job()
                with patch(
                    "backend.routers.jobs._create_job_analysis_response", **seam_kwargs(job_id)
                ) as mock_openai:
                    response = self._post_analyze(job_id, headers=self.headers)
                mock_openai.assert_called_once()
                self.assertEqual(response.status_code, status, response.text)
                if error_code is not None:
                    self.assertEqual(response.json()["detail"]["error_code"], error_code)
                if status == 404:
                    self.assertEqual(response.json(), _not_found_body(job_id))
                rows = self._reservations(self.owner_id)
                self.assertEqual(len(rows), index + 1)
                self.assertEqual(rows[-1].job_id, job_id)
                self.assertEqual(rows[-1].operation_type, JOB_USER_OPERATION_TYPE)
                self._clear_cooldowns()

    def _assert_not_counted(self, job_id, expected_status, headers=None, force_reanalyze=False,
                            expected_body=None):
        before = self._state()
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            response = self._post_analyze(
                job_id, headers=headers or self.headers, force_reanalyze=force_reanalyze
            )
        self.assertEqual(response.status_code, expected_status, response.text)
        if expected_body is not None:
            self.assertEqual(response.json(), expected_body)
        mock_openai.assert_not_called()
        self.assertEqual(self._state(), before)
        return response

    def test_rejected_and_cached_requests_are_not_counted(self):
        self._seed_reservations(self.owner_id, [time.time() - 10] * 3)
        other_user = self._create_user("quota.other@example.com")

        cached_job = self._new_job()
        self._seed_completed_job_analysis(cached_job)
        response = self._assert_not_counted(cached_job, 200)
        self.assertEqual(response.json()["status"], "cached")

        for name, job_id in (
            ("other_users_manual_job", self._new_job(owner_id=other_user)),
            ("catalog_job", self._new_job(owner_id=None, source="arbeitnow")),
            ("own_non_manual_job", self._new_job(source="fixture")),
            ("nonexistent", self._nonexistent_job_id()),
        ):
            with self.subTest(case=name):
                self._assert_not_counted(job_id, 404, expected_body=_not_found_body(job_id))

        self._assert_not_counted(
            self._new_job(), 403, force_reanalyze=True,
            expected_body=FORCE_REANALYZE_ADMIN_ONLY_BODY,
        )
        no_description = self._new_job(description_text=None)
        response = self._assert_not_counted(no_description, 400)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_JOB_DESCRIPTION_MISSING")

        env = {k: v for k, v in os.environ.items() if k not in OPENAI_CREDENTIAL_ENVS}
        with patch.dict(os.environ, env, clear=True), patch.object(jobs, "client", None):
            self._assert_not_counted(
                self._new_job(), 503,
                expected_body={"detail": ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL},
            )

        self._add_guard_row(JOB_USER_OPERATION_TYPE, self.owner_id, "lease")
        self._assert_not_counted(self._new_job(), 409, expected_body=ALREADY_IN_PROGRESS_BODY)

        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM analysis_guards"))
        self._add_guard_row(JOB_USER_OPERATION_TYPE, self.owner_id, "cooldown")
        response = self._assert_not_counted(
            self._new_job(), 429, expected_body=TOO_MANY_REQUESTS_BODY
        )
        self.assertLessEqual(int(response.headers["Retry-After"]), 600)

        self.assertEqual(self._count(self.owner_id), 3)

    def test_quota_429_is_not_counted(self):
        self._seed_reservations(self.owner_id, [time.time() - 10] * 10)
        response = self._assert_not_counted(
            self._new_job(), 429, expected_body=TOO_MANY_REQUESTS_BODY
        )
        self.assertGreater(int(response.headers["Retry-After"]), 0)
        self.assertEqual(self._count(self.owner_id), 10)

    def test_unknown_guard_outcome_never_reaches_openai(self):
        job_id = self._new_job()
        unknown = analysis_guard.AcquireResult(outcome=MagicMock(name="unknown-outcome"))
        with patch.object(jobs, "try_acquire_job_analysis_guard", return_value=unknown), patch(
            "backend.routers.jobs._create_job_analysis_response"
        ) as mock_openai, patch("backend.routers.jobs.release_job_analysis_guard") as mock_release:
            response = self._post_analyze(job_id, headers=self.headers)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json(),
            {"detail": "Service temporarily unavailable. Please try again shortly."},
        )
        mock_openai.assert_not_called()
        mock_release.assert_not_called()

    def test_in_flight_409_is_not_counted(self):
        first_job, second_job = self._new_job(), self._new_job()
        inner = {}
        calls = []

        def seam(prompt, *, timeout_seconds, max_retries):
            calls.append(prompt)
            if len(calls) == 1:
                inner["count_during_call"] = self._count(self.owner_id)
                inner["response"] = self._post_analyze(second_job, headers=self.headers)
                inner["count_after_inner"] = self._count(self.owner_id)
            return _mock_job_response()

        with patch(
            "backend.routers.jobs._create_job_analysis_response", side_effect=seam
        ) as mock_openai:
            outer = self._post_analyze(first_job, headers=self.headers)

        self.assertEqual(outer.status_code, 200, outer.text)
        self.assertEqual(inner["response"].status_code, 409, inner["response"].text)
        self.assertEqual(inner["response"].json(), ALREADY_IN_PROGRESS_BODY)
        mock_openai.assert_called_once()
        self.assertEqual(inner["count_during_call"], 1)
        self.assertEqual(inner["count_after_inner"], 1)
        self.assertEqual([row.job_id for row in self._reservations(self.owner_id)], [first_job])


class QuotaReservationLockTests(_BaseQuotaTestCase):
    """At count 9, the route's guard transaction reserves the 10th slot
    and keeps SQLite's write lock until its commit: nothing else can
    insert a reservation (or run a competing acquisition) in between."""

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("quota.lock.owner@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.job_1 = self._create_job("quota-lock-1", owner_id=self.owner_id, source="manual")
        self.job_2 = self._create_job("quota-lock-2", owner_id=self.owner_id, source="manual")

    def test_only_one_slot_can_be_reserved_at_nine(self):
        now = time.time()
        self._seed_reservations(self.owner_id, [now - 100 - i for i in range(9)])

        blocked_engine = create_engine(
            f"sqlite:///{self.engine.url.database}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        blocked_factory = sessionmaker(autocommit=False, autoflush=False, bind=blocked_engine)
        real_reserve = analysis_guard._reserve_quota_slot
        outcomes = {}

        def reserve_then_probe(guard_session, quota, now):
            result = real_reserve(guard_session, quota, now)
            if result is None and not outcomes:
                try:
                    with blocked_engine.begin() as connection:
                        connection.execute(
                            text(_INSERT_RESERVATION_SQL),
                            {"op": JOB_USER_OPERATION_TYPE, "uid": self.owner_id,
                             "jid": self.job_2, "at": time.time()},
                        )
                    outcomes["insert"] = "committed"
                except OperationalError as exc:
                    outcomes["insert"] = (
                        "locked" if "database is locked" in str(exc) else repr(exc)
                    )
                session = blocked_factory()
                try:
                    outcomes["competing_acquire"] = try_acquire_job_analysis_guard(
                        session, job_id=self.job_2, config=load_job_analysis_config(),
                        owner_user_id=self.owner_id,
                    ).outcome
                finally:
                    session.close()
            return result

        try:
            with patch.object(
                analysis_guard, "_reserve_quota_slot", side_effect=reserve_then_probe
            ) as mock_reserve, patch(
                "backend.routers.jobs._create_job_analysis_response",
                return_value=_mock_job_response(),
            ) as mock_openai:
                response = self._post_analyze(self.job_1, headers=self.headers)
        finally:
            # Disposed here, before tearDown's rmtree.
            blocked_engine.dispose()

        self.assertEqual(
            outcomes,
            {"insert": "locked", "competing_acquire": AcquireOutcome.BACKEND_UNAVAILABLE},
        )
        mock_reserve.assert_called_once()
        self.assertEqual(response.status_code, 200, response.text)
        mock_openai.assert_called_once()
        self.assertEqual(self._count(self.owner_id), 10)
        self.assertIsNone(self._job_row(self.job_2))

        self._clear_cooldowns()
        before = self._state()
        self._assert_quota_429(self.job_2, self.headers)
        self.assertEqual(self._state(), before)
        self.assertEqual(self._count(self.owner_id), 10)


class AdminAndBatchNotCountedTests(_BaseQuotaTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self._create_user("quota.admin@example.com", is_admin=True)
        self.admin_headers = self._auth_headers(self.admin_id)
        self.owner_id = self._create_user("quota.admin.owner@example.com")
        self.owner_headers = self._auth_headers(self.owner_id)
        # The owner's quota is full.
        self._seed_reservations(self.owner_id, [time.time() - 10] * DEFAULT_MAX_ATTEMPTS)
        self.full_quota = self._reservations()

    def test_owner_is_blocked_but_admin_analyses_succeed_and_are_not_counted(self):
        owner_job = self._create_job("quota-admin-owner", owner_id=self.owner_id, source="manual")
        self._assert_quota_429(owner_job, self.owner_headers)

        catalog = self._create_job("quota-admin-catalog", source="arbeitnow")
        for job_id in (owner_job, catalog):
            with self.subTest(job_id=job_id):
                self._post_ok(job_id, self.admin_headers)
                self.assertEqual(self._reservations(), self.full_quota)
        self.assertEqual(self._count(self.admin_id), 0)

    def test_analyze_missing_is_not_counted(self):
        self._create_job("quota-missing-catalog", source="arbeitnow")
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            response = self.client.post(
                "/jobs/analyze-missing", params={"limit": 1}, headers=self.admin_headers
            )
        self.assertEqual(response.status_code, 200, response.text)
        mock_openai.assert_called_once()
        self.assertEqual(self._reservations(), self.full_quota)

    def test_analyze_sample_is_not_counted(self):
        owner_job = self._create_job("quota-sample-owner", owner_id=self.owner_id, source="manual")
        catalog = self._create_job("quota-sample-catalog", source="arbeitnow")
        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            side_effect=lambda *args, **kwargs: _mock_sample_response(),
        ) as mock_openai:
            response = self.client.post(
                "/jobs/analyze-sample",
                params={"job_id_list": [owner_job, catalog]},
                headers=self.admin_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(mock_openai.call_count, 2)
        self.assertEqual(self._reservations(), self.full_quota)


class ProfileAnalysisRouteNotCountedTests(_BaseQuotaTestCase):
    """POST /users/{user_id}/profiles/{profile_id}/analyze (the real
    profiles router, mounted on its own app over the same temp database)
    never touches the job-analysis quota."""

    def setUp(self):
        super().setUp()

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        profile_app = FastAPI()
        profile_app.include_router(profiles.router)
        profile_app.dependency_overrides[get_db] = override_get_db
        self.profile_client = TestClient(profile_app)

        # Never a real client; _create_profile_analysis_response is patched.
        client_patcher = patch.object(profiles, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

        self.owner_id = self._create_user("quota.profile.owner@example.com")
        self.headers = self._auth_headers(self.owner_id)
        profile = models.CandidateProfile(
            user_id=self.owner_id, self_description="synthetic profile"
        )
        self._add(profile)
        self.profile_id = self._rows("candidate_profiles", "profile_id")[-1].profile_id

    def test_owner_profile_analysis_reserves_nothing(self):
        # One slot left: a counted profile analysis would use it up.
        self._seed_reservations(
            self.owner_id, [time.time() - 10] * (DEFAULT_MAX_ATTEMPTS - 1)
        )
        before = self._reservations()

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_openai_response(json.dumps(VALID_PROFILE_ANALYSIS_PAYLOAD)),
        ) as mock_openai:
            response = self.profile_client.post(
                f"/users/{self.owner_id}/profiles/{self.profile_id}/analyze",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        mock_openai.assert_called_once()
        self.assertEqual(
            [(row.profile_id, row.analysis_status, row.is_current)
             for row in self._rows("profile_analysis", "analysis_id")],
            [(self.profile_id, "completed", 1)],
        )
        self.assertEqual(self._reservations(), before)

        # The remaining slot is still there for an owner job analysis.
        first_job = self._create_job("quota-profile-1", owner_id=self.owner_id, source="manual")
        self._post_ok(first_job, self.headers)
        rows = self._reservations(self.owner_id)
        self.assertEqual(len(rows), DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(rows[-1].job_id, first_job)

        self._clear_cooldowns()
        second_job = self._create_job("quota-profile-2", owner_id=self.owner_id, source="manual")
        # A quota 429 (not a <= 60 s cooldown): the slot really was the last one.
        self.assertGreater(self._assert_quota_429(second_job, self.headers), 600)


# ---------------------------------------------------------------------------
# users.delete_user cleanup, user_id reuse and the identity race
# ---------------------------------------------------------------------------


class DeleteUserQuotaCleanupTests(_BaseQuotaTestCase):
    def setUp(self):
        super().setUp()
        self.user_a = self._create_user("quota.cleanup.a@example.com")
        self.bystander = self._create_user("quota.cleanup.bystander@example.com")
        now = time.time()
        self._seed_reservations(self.user_a, [now - 10] * DEFAULT_MAX_ATTEMPTS)
        self._seed_reservations(self.bystander, [now - 20, now - 30])
        self.bystander_rows = self._reservations(self.bystander)

    def test_delete_removes_only_the_users_rows_and_reused_id_gets_a_full_quota(self):
        a_job = self._create_job("quota-cleanup-a", owner_id=self.user_a, source="manual")
        self._assert_quota_429(a_job, self._auth_headers(self.user_a))

        result = self._delete_user(self.user_a)
        self.assertEqual(
            set(result),
            {"status", "user_id", "deleted_profiles_count", "deleted_profile_analyses_count",
             "deleted_matches_count", "deleted_languages_count"},
        )
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(self._reservations(self.user_a), [])
        self.assertEqual(self._reservations(self.bystander), self.bystander_rows)

        self._add(models.User(
            user_id=self.user_a, name="Reusing User B", mail="quota.cleanup.b@example.com",
        ))
        b_job = self._create_job("quota-cleanup-b", owner_id=self.user_a, source="manual")
        self._post_ok(b_job, self._auth_headers(self.user_a))
        self.assertEqual([row.job_id for row in self._reservations(self.user_a)], [b_job])

    def test_owner_replaced_before_guard_commit_reserves_nothing_for_the_new_user(self):
        headers = self._auth_headers(self.user_a)
        job_url = "https://example.com/job/quota-replaced"
        job_id = self._create_job("quota-replaced", owner_id=self.user_a, source="manual")
        real_acquire = jobs.try_acquire_job_analysis_guard
        after_replacement = {}

        def replace_then_acquire(*args, **kwargs):
            self.assertEqual(self._delete_user(self.user_a)["status"], "deleted")
            self._add(models.User(
                user_id=self.user_a, name="Replacement User B",
                mail="quota.replaced.b@example.com",
            ))
            self._add(models.Job(
                job_id=job_id, title="Replacement Job of B", url=job_url,
                description_text="replacement description of b", source="manual",
                created_by_user_id=self.user_a,
            ))
            after_replacement["state"] = self._state()
            return real_acquire(*args, **kwargs)

        with patch.object(
            jobs, "try_acquire_job_analysis_guard", side_effect=replace_then_acquire
        ) as mock_acquire, patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            response = self._post_analyze(job_id, headers=headers)

        mock_acquire.assert_called_once()
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), _not_found_body(job_id))
        mock_openai.assert_not_called()
        self.assertEqual(self._state(), after_replacement["state"])
        self.assertEqual(self._reservations(self.user_a), [])
        self.assertEqual(self._reservations(self.bystander), self.bystander_rows)


if __name__ == "__main__":
    unittest.main()
