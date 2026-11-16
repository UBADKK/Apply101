"""Per-user rolling quota for profile analysis.

A normal (non-admin) user analyzing one of their own profiles through POST
/users/{user_id}/profiles/{profile_id}/analyze (the only path that runs a
profile analysis) gets at most PROFILE_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS
(default 10) counted attempts per rolling
PROFILE_ANALYSIS_USER_QUOTA_WINDOW_SECONDS (default 86400), shared by all of
their profiles. The route passes count_user_quota=True to
try_acquire_profile_analysis_guard, which reserves one row in
analysis_quota_reservations (operation_type USER_OPERATION_TYPE, job_id
NULL) inside the guard's own write transaction, right before the guard
commit and so right before the provider call; reservations are never
refunded. A used-up quota returns QUOTA_EXCEEDED, writes nothing, and the
route answers 429 with the existing "Too many requests" body and a
Retry-After of ceil(oldest counted reservation + window - now). This quota
is separate from the owner job-analysis quota (JOB_USER_OPERATION_TYPE).
Admins are exempt, on their own and on other users' profiles.

Throwaway SQLite only (the base below creates and removes its own temp
dir); backend.app.main is never imported, .env is never loaded,
apply101.db is never touched and the provider is never called (the seam is
patched, or profiles.client is the deterministic FakeAnalysisClient). The
base classes imported below define no tests, so nothing is collected twice.
The quota table is only accessed through raw SQL here.
"""

import json
import math
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
import openai
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app import analysis_guard, models
from backend.app.analysis_guard import (
    AcquireOutcome,
    AcquireResult,
    AnalysisGuardConfig,
    AnalysisGuardConfigError,
    JOB_USER_OPERATION_TYPE,
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
    load_config,
    load_job_analysis_config,
    release_profile_analysis_guard,
    try_acquire_job_analysis_guard,
    try_acquire_profile_analysis_guard,
)
from backend.app.fake_analysis import FakeAnalysisClient
from backend.app.openai_client import ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL
from backend.tests.test_inflight_profile_delete_writes import (
    PROFILE_NOT_FOUND,
    VALID_ANALYSIS_PAYLOAD,
    _BaseInflightDeleteTestCase,
    _openai_response,
    profiles,
)
from backend.tests.test_job_analysis_user_guard import _FakeClock


QUOTA_TABLE = "analysis_quota_reservations"
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_WINDOW_SECONDS = 86400
QUOTA_ENVS = (
    "PROFILE_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS",
    "PROFILE_ANALYSIS_USER_QUOTA_WINDOW_SECONDS",
)
# Removed from the environment for the "not configured" case, so that
# _require_openai_client cannot build a client (real or fake) again.
ANALYSIS_CLIENT_ENVS = ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "APPLY101_ANALYSIS_PROVIDER")

TOO_MANY_REQUESTS_BODY = {"detail": "Too many requests. Please try again later."}
ALREADY_IN_PROGRESS_BODY = {"detail": "An analysis for this profile is already in progress."}
SERVICE_UNAVAILABLE_BODY = {"detail": "Service temporarily unavailable. Please try again shortly."}
CONFIG_ERROR_BODY = {"detail": "Analysis protection is not correctly configured."}
SEAM = "backend.routers.profiles._create_profile_analysis_response"

_INSERT_RESERVATION_SQL = (
    "INSERT INTO analysis_quota_reservations "
    "(operation_type, user_id, job_id, reserved_at) "
    "VALUES (:op, :uid, :jid, :at)"
)


def _profile_config(**overrides) -> AnalysisGuardConfig:
    # Deliberately the 7 pre-existing fields only: the quota fields come
    # from their defaults.
    defaults = dict(
        openai_timeout_seconds=60,
        openai_max_retries=0,
        lease_ttl_seconds=300,
        profile_success_cooldown_seconds=300,
        profile_failure_cooldown_seconds=30,
        user_success_cooldown_seconds=60,
        user_failure_cooldown_seconds=30,
    )
    defaults.update(overrides)
    return AnalysisGuardConfig(**defaults)


def _env_without(names):
    return {k: v for k, v in os.environ.items() if k not in names}


def _valid_response():
    return _openai_response(json.dumps(VALID_ANALYSIS_PAYLOAD))


def _spend_limit_error():
    # Built like the SDK builds a 429 RateLimitError; synthetic text only,
    # no network call.
    err = {
        "message": "synthetic spend limit reached",
        "type": "insufficient_quota",
        "param": None,
        "code": "project_spend_limit_exceeded",
    }
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        json={"error": err},
    )
    return openai.RateLimitError("Error code: 429", response=response, body=err)


class _BaseProfileQuotaTestCase(_BaseInflightDeleteTestCase):
    # -- raw reads ------------------------------------------------------------

    def _rows(self, table, order_by):
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT * FROM {table} ORDER BY {order_by}")
            ).fetchall()

    def _reservations(self, user_id=None, operation_type=None):
        sql = "SELECT id, operation_type, user_id, job_id, reserved_at FROM " + QUOTA_TABLE
        clauses, params = [], {}
        if user_id is not None:
            clauses.append("user_id = :uid")
            params["uid"] = user_id
        if operation_type is not None:
            clauses.append("operation_type = :op")
            params["op"] = operation_type
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).fetchall()

    def _count(self, user_id):
        """Counted profile analyses of user_id (the profile quota only)."""
        return len(self._reservations(user_id, USER_OPERATION_TYPE))

    def _state(self):
        return {
            "profile_analysis": self._rows("profile_analysis", "analysis_id"),
            "analysis_guards": self._rows("analysis_guards", "operation_type, resource_id"),
            QUOTA_TABLE: self._rows(QUOTA_TABLE, "id"),
        }

    def _guard_keys(self):
        return {
            (row.operation_type, row.resource_id)
            for row in self._rows("analysis_guards", "operation_type, resource_id")
        }

    # -- raw writes -----------------------------------------------------------

    def _seed_reservations(self, user_id, reserved_ats, operation_type=USER_OPERATION_TYPE,
                           job_id=None):
        with self.engine.begin() as connection:
            for reserved_at in reserved_ats:
                connection.execute(
                    text(_INSERT_RESERVATION_SQL),
                    {"op": operation_type, "uid": user_id, "jid": job_id, "at": reserved_at},
                )

    def _clear_cooldowns(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE analysis_guards SET cooldown_until = NULL"))

    def _reset_guards(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM analysis_guards"))

    def _put_guard(self, operation_type, resource_id, state, now=None):
        now = time.time() if now is None else now
        self._add(models.AnalysisGuard(
            operation_type=operation_type,
            resource_id=resource_id,
            owner_token="synthetic-other-owner-token" if state == "lease" else None,
            lock_expires_at=now + 600 if state == "lease" else None,
            cooldown_until=now + 600 if state == "cooldown" else None,
        ))

    # -- requests ---------------------------------------------------------------

    def _route_clock(self, clock):
        """Injects `clock` into the route's real guard acquisition."""
        real_acquire = profiles.try_acquire_profile_analysis_guard

        def acquire_with_clock(*args, **kwargs):
            kwargs["clock"] = clock
            return real_acquire(*args, **kwargs)

        return patch.object(
            profiles, "try_acquire_profile_analysis_guard", side_effect=acquire_with_clock
        )

    def _post_raw(self, user_id, profile_id, headers="owner", force=False):
        if headers == "owner":
            headers = self._headers(user_id)
        params = {"force_reanalyze": "true"} if force else None
        return self.client.post(
            f"/users/{user_id}/profiles/{profile_id}/analyze", params=params, headers=headers
        )

    def _post(self, user_id, profile_id, headers="owner", force=False, **seam_kwargs):
        if not seam_kwargs:
            seam_kwargs = {"return_value": _valid_response()}
        with patch(SEAM, **seam_kwargs) as mock_openai:
            response = self._post_raw(user_id, profile_id, headers=headers, force=force)
        return response, mock_openai

    def _post_ok(self, user_id, profile_id, headers="owner", force=False):
        response, mock_openai = self._post(user_id, profile_id, headers=headers, force=force)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_openai.assert_called_once()
        return response

    def _assert_quota_429(self, user_id, profile_id, headers="owner", force=False):
        before = self._state()
        response, mock_openai = self._post(user_id, profile_id, headers=headers, force=force)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
        mock_openai.assert_not_called()
        self.assertEqual(self._state(), before)
        return int(response.headers["Retry-After"])

    def _assert_not_counted(self, user_id, profile_id, expected_status, headers="owner",
                            force=False, expected_body=None):
        before = self._state()
        response, mock_openai = self._post(user_id, profile_id, headers=headers, force=force)
        self.assertEqual(response.status_code, expected_status, response.text)
        if expected_body is not None:
            self.assertEqual(response.json(), expected_body)
        mock_openai.assert_not_called()
        self.assertEqual(self._state(), before)
        return response


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ProfileQuotaConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, _env_without(QUOTA_ENVS), clear=True):
            config = load_config()
        self.assertEqual(config.user_quota_max_attempts, DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(config.user_quota_window_seconds, DEFAULT_WINDOW_SECONDS)

    def test_read_from_env(self):
        with patch.dict(
            os.environ,
            {
                "PROFILE_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS": "3",
                "PROFILE_ANALYSIS_USER_QUOTA_WINDOW_SECONDS": "600",
            },
            clear=False,
        ):
            config = load_config()
        self.assertEqual(config.user_quota_max_attempts, 3)
        self.assertEqual(config.user_quota_window_seconds, 600)

    def test_job_quota_envs_do_not_change_the_profile_quota(self):
        with patch.dict(os.environ, _env_without(QUOTA_ENVS), clear=True), patch.dict(
            os.environ,
            {
                "JOB_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS": "3",
                "JOB_ANALYSIS_USER_QUOTA_WINDOW_SECONDS": "600",
            },
        ):
            config = load_config()
        self.assertEqual(config.user_quota_max_attempts, DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(config.user_quota_window_seconds, DEFAULT_WINDOW_SECONDS)

    def test_invalid_values_rejected(self):
        for env_name in QUOTA_ENVS:
            for bad_value in ("0", "-1", "not-a-number", "1.5"):
                with self.subTest(env_var=env_name, value=bad_value):
                    with patch.dict(os.environ, {env_name: bad_value}, clear=False):
                        with self.assertRaises(AnalysisGuardConfigError):
                            load_config()

    def test_seven_field_constructor_gets_quota_defaults(self):
        config = _profile_config()
        self.assertEqual(config.user_quota_max_attempts, DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(config.user_quota_window_seconds, DEFAULT_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# Guard level (fake clock)
# ---------------------------------------------------------------------------


class ProfileQuotaGuardLevelTests(_BaseProfileQuotaTestCase):
    USER = 10

    def setUp(self):
        super().setUp()
        self.clock = _FakeClock()
        self.config = _profile_config()
        self.next_profile_id = 1

    def _acquire(self, profile_id, user_id=USER, count=True, config=None, **kwargs):
        if count is not None:
            kwargs["count_user_quota"] = count
        session = self.session_factory()
        try:
            return try_acquire_profile_analysis_guard(
                session, profile_id=profile_id, owner_user_id=user_id,
                config=config or self.config, clock=self.clock, **kwargs
            )
        finally:
            session.close()

    def _release(self, profile_id, owner_token, user_id=USER, succeeded=True):
        session = self.session_factory()
        try:
            return release_profile_analysis_guard(
                session, profile_id=profile_id, owner_user_id=user_id,
                owner_token=owner_token, succeeded=succeeded, config=self.config,
                clock=self.clock,
            )
        finally:
            session.close()

    def _new_profile_id(self):
        profile_id = self.next_profile_id
        self.next_profile_id += 1
        return profile_id

    def _counted_attempt(self, user_id=USER):
        """One granted counted acquisition on a fresh profile id, released,
        then the clock moves past the user's success cooldown (60 s)."""
        profile_id = self._new_profile_id()
        result = self._acquire(profile_id, user_id=user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertTrue(self._release(profile_id, result.owner_token, user_id=user_id))
        self.clock.advance(60)

    def _profile_guard_row(self, profile_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT * FROM analysis_guards WHERE operation_type = :op "
                     "AND resource_id = :rid"),
                {"op": PROFILE_OPERATION_TYPE, "rid": profile_id},
            ).fetchone()

    # -- counting ---------------------------------------------------------------

    def test_granted_counted_acquisition_reserves_one_slot(self):
        result = self._acquire(7)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        rows = self._reservations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0].operation_type, rows[0].user_id, rows[0].job_id, rows[0].reserved_at),
            (USER_OPERATION_TYPE, self.USER, None, self.clock()),
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
        profile_id = self._new_profile_id()
        result = self._acquire(profile_id)

        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertIsNone(result.owner_token)
        self.assertEqual(
            result.retry_after_seconds,
            math.ceil(start + DEFAULT_WINDOW_SECONDS - self.clock()),
        )
        self.assertEqual(result.retry_after_seconds, DEFAULT_WINDOW_SECONDS - 600)
        self.assertEqual(self._state(), before)
        self.assertIsNone(self._profile_guard_row(profile_id))

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

        self.clock.advance(0.5)  # now == oldest + window: the slot is free
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

    def test_lowered_limit_and_window_from_config(self):
        t0 = self.clock()
        self._seed_reservations(self.USER, [t0 - 50, t0 - 40, t0 - 30])
        config = _profile_config(user_quota_max_attempts=3, user_quota_window_seconds=100)
        result = self._acquire(1, config=config)
        self.assertEqual(result.outcome, AcquireOutcome.QUOTA_EXCEEDED)
        self.assertEqual(result.retry_after_seconds, 50)
        # The default limit (10) would still have room.
        self.assertEqual(self._acquire(1).outcome, AcquireOutcome.GRANTED)

    def test_count_user_quota_false_never_reserves_even_at_the_limit(self):
        self._seed_reservations(self.USER, [self.clock() - 1] * DEFAULT_MAX_ATTEMPTS)
        before = self._reservations()
        for profile_id, count in ((1, False), (2, None)):  # explicit False, and omitted
            with self.subTest(count=count):
                result = self._acquire(profile_id, count=count)
                self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
                self.assertTrue(self._release(profile_id, result.owner_token))
                self.clock.advance(60)
        self.assertEqual(self._reservations(), before)

    def test_profile_and_job_quotas_are_independent(self):
        t0 = self.clock()
        # User 10: job quota full -> profile analysis still allowed.
        self._seed_reservations(self.USER, [t0 - 1] * 10, operation_type=JOB_USER_OPERATION_TYPE,
                                job_id=99)
        job_rows = self._reservations(self.USER, JOB_USER_OPERATION_TYPE)
        self.assertEqual(self._acquire(1).outcome, AcquireOutcome.GRANTED)
        self.assertEqual(self._count(self.USER), 1)
        self.assertEqual(self._reservations(self.USER, JOB_USER_OPERATION_TYPE), job_rows)

        # User 20: profile quota full -> owner job analysis still allowed.
        self._seed_reservations(20, [t0 - 1] * 10)
        profile_rows = self._reservations(20, USER_OPERATION_TYPE)
        session = self.session_factory()
        try:
            job = try_acquire_job_analysis_guard(
                session, job_id=5, config=load_job_analysis_config(), clock=self.clock,
                owner_user_id=20,
            )
        finally:
            session.close()
        self.assertEqual(job.outcome, AcquireOutcome.GRANTED)
        self.assertEqual(len(self._reservations(20, JOB_USER_OPERATION_TYPE)), 1)
        self.assertEqual(self._reservations(20, USER_OPERATION_TYPE), profile_rows)
        # ...while its own profile quota is still used up.
        self.assertEqual(self._acquire(2, user_id=20).outcome, AcquireOutcome.QUOTA_EXCEEDED)

    # -- not counted --------------------------------------------------------------

    def test_blocked_or_target_changed_acquisitions_reserve_nothing(self):
        expected_by_case = {
            "target_changed": AcquireOutcome.TARGET_CHANGED,
            "profile_lease": AcquireOutcome.ALREADY_IN_PROGRESS,
            "user_lease": AcquireOutcome.ALREADY_IN_PROGRESS,
            "profile_cooldown": AcquireOutcome.COOLDOWN_ACTIVE,
            "user_cooldown": AcquireOutcome.COOLDOWN_ACTIVE,
        }
        for full_quota in (False, True):
            for case, expected in expected_by_case.items():
                with self.subTest(full_quota=full_quota, case=case):
                    self._reset_guards()
                    with self.engine.begin() as connection:
                        connection.execute(text("DELETE FROM " + QUOTA_TABLE))
                    if full_quota:
                        self._seed_reservations(self.USER, [self.clock() - 1] * 10)
                    kwargs = {}
                    if case == "target_changed":
                        # Wins over a lease too.
                        self._put_guard(USER_OPERATION_TYPE, self.USER, "lease", self.clock())
                        kwargs["precondition"] = lambda guard_session: False
                    elif case.startswith("profile_"):
                        self._put_guard(PROFILE_OPERATION_TYPE, 1, case[8:], self.clock())
                    else:
                        self._put_guard(USER_OPERATION_TYPE, self.USER, case[5:], self.clock())
                    before = self._state()

                    result = self._acquire(1, **kwargs)

                    self.assertEqual(result.outcome, expected)
                    self.assertIsNone(result.owner_token)
                    if expected is AcquireOutcome.COOLDOWN_ACTIVE:
                        self.assertEqual(result.retry_after_seconds, 600)
                    self.assertEqual(self._state(), before)


# ---------------------------------------------------------------------------
# Route level: POST /users/{user_id}/profiles/{profile_id}/analyze
# ---------------------------------------------------------------------------


class ProfileRouteQuotaTests(_BaseProfileQuotaTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("profile.quota.owner@example.com")
        self.profile_id = self._create_profile(self.owner_id)

    def test_ten_counted_then_eleventh_is_429_with_retry_after(self):
        for attempt in range(DEFAULT_MAX_ATTEMPTS):
            self._post_ok(self.owner_id, self.profile_id, force=True)
            self.assertEqual(self._count(self.owner_id), attempt + 1)
            self._clear_cooldowns()

        before = self._state()
        start = time.time()
        response, mock_openai = self._post(self.owner_id, self.profile_id, force=True)
        end = time.time()

        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
        mock_openai.assert_not_called()
        self.assertEqual(self._state(), before)
        self.assertEqual(len(self._rows("profile_analysis", "analysis_id")), 10)

        rows = self._reservations(self.owner_id)
        self.assertEqual(
            {(row.operation_type, row.job_id) for row in rows}, {(USER_OPERATION_TYPE, None)}
        )
        self.assertEqual(len(rows), 10)
        oldest = min(row.reserved_at for row in rows)
        retry_after = int(response.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, math.ceil(oldest + DEFAULT_WINDOW_SECONDS - end))
        self.assertLessEqual(retry_after, math.ceil(oldest + DEFAULT_WINDOW_SECONDS - start))

    def test_exact_retry_after_and_rolling_window_with_fake_clock(self):
        clock = _FakeClock()
        t0 = clock()
        oldest = t0 - DEFAULT_WINDOW_SECONDS + 123.5
        self._seed_reservations(self.owner_id, [oldest] + [t0 - 5] * 9)

        before = self._state()
        with self._route_clock(clock):
            self.assertEqual(self._assert_quota_429(self.owner_id, self.profile_id), 124)
            self.assertEqual(self._state(), before)

            clock.advance(122.5)
            self.assertEqual(self._assert_quota_429(self.owner_id, self.profile_id), 1)
            self.assertEqual(self._state(), before)

            clock.advance(1)
            self._post_ok(self.owner_id, self.profile_id)

        reserved_ats = [row.reserved_at for row in self._reservations(self.owner_id)]
        self.assertEqual(len(reserved_ats), 10)
        self.assertNotIn(oldest, reserved_ats)
        self.assertIn(clock(), reserved_ats)

    def test_all_profiles_of_a_user_share_one_quota(self):
        self._seed_reservations(self.owner_id, [time.time() - 10] * 9)
        self._post_ok(self.owner_id, self.profile_id)
        self.assertEqual(self._count(self.owner_id), 10)
        self._clear_cooldowns()

        new_profile = self._create_profile(self.owner_id, description="second profile")
        self.assertGreater(self._assert_quota_429(self.owner_id, new_profile), 600)
        self.assertEqual(self._count(self.owner_id), 10)

    def test_force_reanalyze_is_counted_and_cached_hits_are_not(self):
        self._seed_profile_analysis(self.profile_id)
        self._seed_reservations(self.owner_id, [time.time() - 10] * 9)

        response = self._assert_not_counted(self.owner_id, self.profile_id, 200)
        self.assertEqual(response.json()["status"], "cached")
        self.assertEqual(self._count(self.owner_id), 9)

        self._post_ok(self.owner_id, self.profile_id, force=True)
        self.assertEqual(self._count(self.owner_id), 10)
        self._clear_cooldowns()

        # At the limit: a cache hit is still served and still not counted...
        response = self._assert_not_counted(self.owner_id, self.profile_id, 200)
        self.assertEqual(response.json()["status"], "cached")
        # ...but force_reanalyze cannot bypass the quota.
        self.assertGreater(self._assert_quota_429(self.owner_id, self.profile_id, force=True), 600)
        self.assertEqual(self._count(self.owner_id), 10)

    def test_different_users_have_independent_quotas(self):
        other_id = self._create_user("profile.quota.other@example.com")
        other_profile = self._create_profile(other_id)
        now = time.time()
        self._seed_reservations(self.owner_id, [now - 10] * 10)
        self._seed_reservations(other_id, [now - 10] * 9)
        owner_rows = self._reservations(self.owner_id)

        self._assert_quota_429(self.owner_id, self.profile_id)
        self._post_ok(other_id, other_profile)
        self.assertEqual(self._count(other_id), 10)
        self.assertEqual(self._reservations(self.owner_id), owner_rows)
        self._clear_cooldowns()
        self._assert_quota_429(other_id, other_profile, force=True)

    def test_counted_outcomes(self):
        cases = (
            ("success", {"return_value": _valid_response()}, 200, None),
            ("provider_error", {"side_effect": RuntimeError("synthetic upstream failure")},
             500, "ERR_PROFILE_ANALYSIS_FAILED"),
            ("invalid_json", {"return_value": _openai_response("not valid json{{{")},
             500, "ERR_PROFILE_ANALYSIS_FAILED"),
            ("schema_invalid", {"return_value": _openai_response(json.dumps({"unexpected": 1}))},
             500, "ERR_PROFILE_ANALYSIS_FAILED"),
            ("spend_limit", {"side_effect": _spend_limit_error()},
             503, "ERR_PROFILE_ANALYSIS_PROVIDER_UNAVAILABLE"),
        )
        for index, (name, seam_kwargs, status, error_code) in enumerate(cases):
            with self.subTest(case=name):
                response, mock_openai = self._post(
                    self.owner_id, self.profile_id, force=True, **seam_kwargs
                )
                mock_openai.assert_called_once()
                self.assertEqual(response.status_code, status, response.text)
                if error_code is not None:
                    self.assertEqual(response.json()["detail"]["error_code"], error_code)
                rows = self._reservations(self.owner_id)
                self.assertEqual(len(rows), index + 1)
                self.assertEqual(
                    (rows[-1].operation_type, rows[-1].job_id), (USER_OPERATION_TYPE, None)
                )
                self._clear_cooldowns()

    def test_rejected_requests_are_not_counted(self):
        self._seed_reservations(self.owner_id, [time.time() - 10] * 3)
        other_id = self._create_user("profile.quota.notcounted.other@example.com")
        other_profile = self._create_profile(other_id)

        with self.subTest(case="unauthenticated"):
            self._assert_not_counted(self.owner_id, self.profile_id, 401, headers=None)

        with self.subTest(case="other_users_profile"):
            self._assert_not_counted(
                other_id, other_profile, 404, headers=self._headers(self.owner_id),
                expected_body=PROFILE_NOT_FOUND,
            )

        with self.subTest(case="profile_data_missing"):
            empty_profile = self._create_profile(self.owner_id, description=None)
            response = self._assert_not_counted(self.owner_id, empty_profile, 400)
            self.assertEqual(response.json()["detail"]["error_code"], "ERR_PROFILE_DATA_MISSING")

        with self.subTest(case="not_configured"):
            with patch.dict(os.environ, _env_without(ANALYSIS_CLIENT_ENVS), clear=True), \
                    patch.object(profiles, "client", None):
                self._assert_not_counted(
                    self.owner_id, self.profile_id, 503,
                    expected_body={"detail": ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL},
                )

        for env_name, bad_value in (
            ("PROFILE_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS", "0"),
            ("PROFILE_ANALYSIS_USER_QUOTA_WINDOW_SECONDS", "not-a-number"),
            ("PROFILE_ANALYSIS_OPENAI_TIMEOUT_SECONDS", "0"),
        ):
            with self.subTest(case="invalid_config", env=env_name):
                with patch.dict(os.environ, {env_name: bad_value}, clear=False):
                    self._assert_not_counted(
                        self.owner_id, self.profile_id, 500, expected_body=CONFIG_ERROR_BODY
                    )

        with self.subTest(case="in_flight_409"):
            self._put_guard(USER_OPERATION_TYPE, self.owner_id, "lease")
            response = self._assert_not_counted(
                self.owner_id, self.profile_id, 409, expected_body=ALREADY_IN_PROGRESS_BODY
            )
            self.assertNotIn("Retry-After", response.headers)
            self._reset_guards()

        with self.subTest(case="cooldown_429"):
            self._put_guard(PROFILE_OPERATION_TYPE, self.profile_id, "cooldown")
            response = self._assert_not_counted(
                self.owner_id, self.profile_id, 429, expected_body=TOO_MANY_REQUESTS_BODY
            )
            self.assertLessEqual(int(response.headers["Retry-After"]), 600)
            self._reset_guards()

        with self.subTest(case="target_changed_404"):
            real_acquire = profiles.try_acquire_profile_analysis_guard

            def acquire_with_failing_precondition(*args, **kwargs):
                kwargs["precondition"] = lambda guard_session: False
                return real_acquire(*args, **kwargs)

            with patch.object(
                profiles, "try_acquire_profile_analysis_guard",
                side_effect=acquire_with_failing_precondition,
            ) as mock_acquire:
                self._assert_not_counted(
                    self.owner_id, self.profile_id, 404, expected_body=PROFILE_NOT_FOUND
                )
            mock_acquire.assert_called_once()
            self.assertIs(mock_acquire.call_args.kwargs["count_user_quota"], True)

        self.assertEqual(self._count(self.owner_id), 3)
        self.assertEqual(self._guard_keys(), set())

    def test_quota_429_is_not_counted(self):
        self._seed_reservations(self.owner_id, [time.time() - 10] * 10)
        before = self._reservations()
        retry_after = self._assert_quota_429(self.owner_id, self.profile_id)
        self.assertGreater(retry_after, 0)
        self.assertEqual(self._reservations(), before)
        self.assertEqual(self._guard_keys(), set())

    def test_in_flight_409_on_another_profile_is_not_counted(self):
        second_profile = self._create_profile(self.owner_id, description="second profile")
        inner = {}
        calls = []

        def seam(prompt, *, timeout_seconds, max_retries):
            calls.append(prompt)
            if len(calls) == 1:
                inner["count_during_call"] = self._count(self.owner_id)
                inner["response"] = self._post_raw(self.owner_id, second_profile)
                inner["count_after_inner"] = self._count(self.owner_id)
            return _valid_response()

        response, mock_openai = self._post(self.owner_id, self.profile_id, side_effect=seam)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(inner["response"].status_code, 409, inner["response"].text)
        self.assertEqual(inner["response"].json(), ALREADY_IN_PROGRESS_BODY)
        mock_openai.assert_called_once()
        self.assertEqual(inner["count_during_call"], 1)
        self.assertEqual(inner["count_after_inner"], 1)
        self.assertEqual(self._count(self.owner_id), 1)

    def test_unknown_guard_outcome_never_reaches_openai(self):
        unknown = AcquireResult(outcome=MagicMock(name="unknown-outcome"))
        with patch.object(
            profiles, "try_acquire_profile_analysis_guard", return_value=unknown
        ), patch.object(profiles, "release_profile_analysis_guard") as mock_release:
            before = self._state()
            response, mock_openai = self._post(self.owner_id, self.profile_id)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), SERVICE_UNAVAILABLE_BODY)
        mock_openai.assert_not_called()
        mock_release.assert_not_called()
        self.assertEqual(self._state(), before)

    def test_quota_exceeded_outcome_maps_to_429_without_release(self):
        exceeded = AcquireResult(outcome=AcquireOutcome.QUOTA_EXCEEDED, retry_after_seconds=1234)
        with patch.object(
            profiles, "try_acquire_profile_analysis_guard", return_value=exceeded
        ), patch.object(profiles, "release_profile_analysis_guard") as mock_release:
            response, mock_openai = self._post(self.owner_id, self.profile_id)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
        self.assertEqual(response.headers["Retry-After"], "1234")
        mock_openai.assert_not_called()
        mock_release.assert_not_called()

    def test_fake_provider_path_is_counted_too(self):
        self._seed_reservations(self.owner_id, [time.time() - 10] * 9)
        with patch.object(profiles, "client", FakeAnalysisClient()):
            response = self._post_raw(self.owner_id, self.profile_id)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "created")
            self.assertEqual(self._count(self.owner_id), 10)
            self._clear_cooldowns()

            before = self._state()
            response = self._post_raw(self.owner_id, self.profile_id, force=True)
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(response.json(), TOO_MANY_REQUESTS_BODY)
            self.assertEqual(self._state(), before)


class ProfileQuotaAdminExemptionTests(_BaseProfileQuotaTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self._create_user("profile.quota.admin@example.com", is_admin=True)
        self.admin_headers = self._headers(self.admin_id)
        self.owner_id = self._create_user("profile.quota.admin.owner@example.com")
        self.profile_id = self._create_profile(self.owner_id)
        # The owner's profile quota is full.
        self._seed_reservations(self.owner_id, [time.time() - 10] * DEFAULT_MAX_ATTEMPTS)
        self.full_quota = self._reservations()

    def test_admin_on_a_blocked_users_profile_succeeds_and_is_not_counted(self):
        self._assert_quota_429(self.owner_id, self.profile_id)

        self._post_ok(self.owner_id, self.profile_id, headers=self.admin_headers)
        self.assertEqual(self._reservations(), self.full_quota)
        self._clear_cooldowns()

        # The owner is still blocked afterwards.
        self.assertGreater(self._assert_quota_429(self.owner_id, self.profile_id, force=True), 600)
        self.assertEqual(self._reservations(), self.full_quota)

    def test_admin_own_profile_analyses_are_not_counted(self):
        admin_profile = self._create_profile(self.admin_id)
        with patch.object(
            profiles, "try_acquire_profile_analysis_guard",
            side_effect=profiles.try_acquire_profile_analysis_guard,
        ) as mock_acquire:
            for _ in range(DEFAULT_MAX_ATTEMPTS + 1):
                self._post_ok(self.admin_id, admin_profile, force=True)
                self._clear_cooldowns()
        self.assertEqual(mock_acquire.call_count, DEFAULT_MAX_ATTEMPTS + 1)
        for call in mock_acquire.call_args_list:
            self.assertIs(call.kwargs["count_user_quota"], False)
        self.assertEqual(self._reservations(self.admin_id), [])
        self.assertEqual(self._reservations(), self.full_quota)
        self._assert_quota_429(self.owner_id, self.profile_id)


# ---------------------------------------------------------------------------
# Concurrency (real threads, temp-file SQLite)
# ---------------------------------------------------------------------------


class ProfileQuotaConcurrencyTests(_BaseProfileQuotaTestCase):
    def test_same_user_two_profiles_at_nine_counts_exactly_one(self):
        # End-to-end outcome only: these two requests are serialized by the
        # per-user lease (USER_OPERATION_TYPE), so this would pass even if
        # the reservation itself were not atomic. The quota's own atomicity
        # (reserved under the guard transaction's write lock) is proven by
        # ProfileQuotaReservationLockTests below.
        owner_id = self._create_user("profile.quota.race.owner@example.com")
        profile_a = self._create_profile(owner_id, description="profile a")
        profile_b = self._create_profile(owner_id, description="profile b")
        self._seed_reservations(owner_id, [time.time() - 100 - i for i in range(9)])
        headers = self._headers(owner_id)

        start_barrier = threading.Barrier(2, timeout=10)
        in_call = threading.Event()
        release_call = threading.Event()
        loser_done = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        def seam(prompt, *, timeout_seconds, max_retries):
            with calls_lock:
                calls.append(prompt)
            in_call.set()
            release_call.wait(timeout=10)
            return _valid_response()

        results = {}

        def worker(name, profile_id):
            start_barrier.wait()
            results[name] = self._post_raw(owner_id, profile_id, headers=headers)
            if results[name].status_code != 200:
                loser_done.set()

        threads = [
            threading.Thread(target=worker, args=("a", profile_a)),
            threading.Thread(target=worker, args=("b", profile_b)),
        ]
        with patch(SEAM, side_effect=seam):
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(in_call.wait(timeout=10))
                self.assertTrue(loser_done.wait(timeout=10))
                # While the winner is still in flight: exactly one slot taken.
                self.assertEqual(self._count(owner_id), 10)
            finally:
                release_call.set()
                for thread in threads:
                    thread.join(timeout=10)

        statuses = sorted(response.status_code for response in results.values())
        self.assertEqual(len(statuses), 2)
        self.assertEqual(statuses[0], 200, statuses)
        self.assertIn(statuses[1], (409, 429), statuses)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._count(owner_id), 10)
        self.assertEqual(len(self._rows("profile_analysis", "analysis_id")), 1)

        # And the quota is now used up for both profiles.
        self._clear_cooldowns()
        for profile_id in (profile_a, profile_b):
            self._assert_quota_429(owner_id, profile_id, force=True)
        self.assertEqual(self._count(owner_id), 10)

    def test_two_users_at_nine_concurrently_both_succeed(self):
        users_profiles = []
        for name in ("one", "two"):
            user_id = self._create_user(f"profile.quota.race.{name}@example.com")
            users_profiles.append((user_id, self._create_profile(user_id)))
            self._seed_reservations(user_id, [time.time() - 100 - i for i in range(9)])
        headers = {user_id: self._headers(user_id) for user_id, _ in users_profiles}

        start_barrier = threading.Barrier(2, timeout=10)
        # Both provider calls must be in flight at the same time, so both
        # requests held their guards (and reserved) simultaneously.
        in_call_barrier = threading.Barrier(2, timeout=10)

        def seam(prompt, *, timeout_seconds, max_retries):
            in_call_barrier.wait()
            return _valid_response()

        results = {}

        def worker(user_id, profile_id):
            start_barrier.wait()
            results[user_id] = self._post_raw(user_id, profile_id, headers=headers[user_id])

        threads = [threading.Thread(target=worker, args=pair) for pair in users_profiles]
        with patch(SEAM, side_effect=seam) as mock_openai:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(mock_openai.call_count, 2)
        for user_id, _ in users_profiles:
            self.assertEqual(results[user_id].status_code, 200, results[user_id].text)
            self.assertEqual(self._count(user_id), 10)


class ProfileQuotaReservationLockTests(_BaseProfileQuotaTestCase):
    """At count 9, the route's profile guard transaction reserves the 10th
    slot and keeps SQLite's write lock until its commit: nothing else can
    insert a reservation (or run a competing acquisition) in between."""

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("profile.quota.lock.owner@example.com")
        self.profile_1 = self._create_profile(self.owner_id, description="lock profile 1")
        self.profile_2 = self._create_profile(self.owner_id, description="lock profile 2")

    def test_only_one_slot_can_be_reserved_at_nine(self):
        now = time.time()
        self._seed_reservations(self.owner_id, [now - 100 - i for i in range(9)])

        blocked_engine = create_engine(
            f"sqlite:///{self.db_path}",
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
                            {"op": USER_OPERATION_TYPE, "uid": self.owner_id, "jid": None,
                             "at": time.time()},
                        )
                    outcomes["insert"] = "committed"
                except OperationalError as exc:
                    outcomes["insert"] = (
                        "locked" if "database is locked" in str(exc) else repr(exc)
                    )
                session = blocked_factory()
                try:
                    outcomes["competing_acquire"] = try_acquire_profile_analysis_guard(
                        session, profile_id=self.profile_2, owner_user_id=self.owner_id,
                        config=load_config(), count_user_quota=True,
                    ).outcome
                finally:
                    session.close()
            return result

        try:
            with patch.object(
                analysis_guard, "_reserve_quota_slot", side_effect=reserve_then_probe
            ) as mock_reserve:
                response, mock_openai = self._post(self.owner_id, self.profile_1)
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
        self.assertNotIn((PROFILE_OPERATION_TYPE, self.profile_2), self._guard_keys())

        self._clear_cooldowns()
        self._assert_quota_429(self.owner_id, self.profile_2)
        self.assertEqual(self._count(self.owner_id), 10)


class ProfileQuotaMissingTableTests(_BaseProfileQuotaTestCase):
    """Without analysis_quota_reservations (neither migration phase8 nor
    create_all ran), a counted profile analysis fails closed with 503
    before any provider call; admins, who are not counted, are unaffected."""

    def _state_without_quota_table(self):
        return {
            "profile_analysis": self._rows("profile_analysis", "analysis_id"),
            "analysis_guards": self._rows("analysis_guards", "operation_type, resource_id"),
        }

    def test_missing_quota_table_fails_closed_for_non_admins_only(self):
        owner_id = self._create_user("profile.quota.notable.owner@example.com")
        profile_id = self._create_profile(owner_id)
        admin_id = self._create_user("profile.quota.notable.admin@example.com", is_admin=True)
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE " + QUOTA_TABLE))

        before = self._state_without_quota_table()
        response, mock_openai = self._post(owner_id, profile_id)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), SERVICE_UNAVAILABLE_BODY)
        mock_openai.assert_not_called()
        self.assertEqual(self._state_without_quota_table(), before)

        self._post_ok(owner_id, profile_id, headers=self._headers(admin_id))
        rows = self._rows("profile_analysis", "analysis_id")
        self.assertEqual(
            [(row.profile_id, row.analysis_status) for row in rows], [(profile_id, "completed")]
        )


# ---------------------------------------------------------------------------
# users.delete_user cleanup and user_id reuse
# ---------------------------------------------------------------------------


class ProfileQuotaDeleteUserTests(_BaseProfileQuotaTestCase):
    def test_delete_removes_profile_reservations_and_reused_id_gets_a_full_quota(self):
        user_a = self._create_user("profile.quota.cleanup.a@example.com")
        profile_a = self._create_profile(user_a)
        bystander = self._create_user("profile.quota.cleanup.bystander@example.com")
        now = time.time()
        self._seed_reservations(user_a, [now - 10] * DEFAULT_MAX_ATTEMPTS)
        self._seed_reservations(user_a, [now - 10] * 2, operation_type=JOB_USER_OPERATION_TYPE,
                                job_id=1)
        self._seed_reservations(bystander, [now - 20, now - 30])
        bystander_rows = self._reservations(bystander)

        self._assert_quota_429(user_a, profile_a)

        self._delete_user_elsewhere(user_a)
        self.assertEqual(self._reservations(user_a), [])
        self.assertEqual(self._reservations(bystander), bystander_rows)

        self._recreate_same_ids_elsewhere(
            user_a, profile_a, "profile.quota.cleanup.b@example.com"
        )
        self._post_ok(user_a, profile_a)
        rows = self._reservations(user_a)
        self.assertEqual(
            [(row.operation_type, row.job_id) for row in rows], [(USER_OPERATION_TYPE, None)]
        )


if __name__ == "__main__":
    unittest.main()
