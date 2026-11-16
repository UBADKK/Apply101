"""System-wide rolling quota of paid provider calls (W17).

Every provider call triggered by a non-admin requester -- profile analysis,
owner job analysis, cover letter create and regenerate -- reserves one row
in analysis_quota_reservations with operation_type
PROVIDER_CALL_GLOBAL_OPERATION_TYPE under the sentinel user_id
PROVIDER_CALL_GLOBAL_USER_ID (0), in the same transaction as the guard /
lease and after the per-user quota. At PROVIDER_CALL_GLOBAL_QUOTA_MAX_ATTEMPTS
(default 100) per rolling PROVIDER_CALL_GLOBAL_QUOTA_WINDOW_SECONDS (default
86400) every non-admin gets 429 with Retry-After and nothing is written.
Admin-triggered calls are never limited and never touch a per-user or the
global quota; each one leaves one record-only PROVIDER_CALL_ADMIN_OPERATION_TYPE
row under the admin's id.

Throwaway SQLite only (temp dir per test); backend.app.main is never
imported, .env is never loaded, apply101.db is never touched and every
provider seam is patched (or the deterministic fake client is used). Only
helper functions are imported from other test modules, so nothing is
collected twice.
"""

import json
import math
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app import analysis_guard, cover_letter, models
from backend.app.analysis_contract import PROFILE_ANALYSIS_MODEL, PROFILE_ANALYSIS_PROMPT_VERSION
from backend.app.analysis_guard import (
    JOB_USER_OPERATION_TYPE,
    PROVIDER_CALL_ADMIN_OPERATION_TYPE,
    PROVIDER_CALL_GLOBAL_OPERATION_TYPE,
    PROVIDER_CALL_GLOBAL_USER_ID,
    USER_OPERATION_TYPE,
    AcquireOutcome,
    AnalysisGuardConfigError,
    ProviderCallAccounting,
    ProviderCallQuotaConfig,
    load_config,
    load_provider_call_quota_config,
    try_acquire_profile_analysis_guard,
)
from backend.app.database import Base, get_db
from backend.app.fake_analysis import FakeAnalysisClient
from backend.app.security import create_access_token, hash_password
from backend.tests.test_job_analysis_user_guard import _FakeClock
from backend.tests.test_job_analysis_user_quota import _mock_sample_response
from backend.tests.test_owner_manual_job_analysis import _mock_job_response
from backend.tests.test_profile_analysis_quota import _spend_limit_error, _valid_response

with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"}, clear=False):
    from backend.routers import cover_letters, jobs, profiles, users


SYNTHETIC_SECRET = "synthetic-test-secret-for-global-provider-quota-0123456789"
GLOBAL_ENVS = ("PROVIDER_CALL_GLOBAL_QUOTA_MAX_ATTEMPTS", "PROVIDER_CALL_GLOBAL_QUOTA_WINDOW_SECONDS")
LIMIT = 100
WINDOW = 86400
LETTER_LETTER_QUOTA = "cover_letter_user"
TOO_MANY = {"detail": "Too many requests. Please try again later."}

PROFILE_SEAM = "backend.routers.profiles._create_profile_analysis_response"
JOB_SEAM = "backend.routers.jobs._create_job_analysis_response"
SAMPLE_SEAM = "backend.routers.jobs._create_job_sample_analysis_response"
LETTER_SEAM = "backend.routers.cover_letters._create_cover_letter_response"

_INSERT_SQL = (
    "INSERT INTO analysis_quota_reservations (operation_type, user_id, job_id, reserved_at) "
    "VALUES (:op, :uid, :jid, :at)"
)


def _letter_response():
    response = MagicMock()
    response.output_text = json.dumps(
        {"letter_text": "Synthetic draft [placeholder].", "used_fact_ids": [], "missing_information": []}
    )
    return response


def _env_without_global():
    return {k: v for k, v in os.environ.items() if k not in GLOBAL_ENVS}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_global_quota_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        for router in (users.router, profiles.router, jobs.router, cover_letters.router):
            app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env = patch.dict(os.environ, _env_without_global(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        os.environ.update({"JWT_SECRET_KEY": SYNTHETIC_SECRET, "COVER_LETTER_DAILY_LIMIT": "10"})
        for module in (profiles, jobs, cover_letters):
            patcher = patch.object(module, "client", MagicMock())
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- data -------------------------------------------------------------------

    def _add(self, obj):
        db = self.Session()
        try:
            db.add(obj)
            db.commit()
            db.refresh(obj)
            return obj
        finally:
            db.close()

    def _user(self, mail, is_admin=False):
        user = self._add(models.User(name="Synthetic", mail=mail, is_admin=is_admin,
                                     password_hash=hash_password("a-valid-synthetic-password")))
        return user.user_id, {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}

    def _profile(self, user_id):
        return self._add(models.CandidateProfile(
            user_id=user_id, target_role="Backend Developer", self_description="synthetic profile",
        )).profile_id

    def _job(self, slug, owner_id=None):
        return self._add(models.Job(
            title=f"Synthetic Job {slug}", company_name="Acme", url=f"https://example.test/{slug}",
            description_text="Build APIs with Python.", source="manual" if owner_id else "greenhouse",
            job_status="active", created_by_user_id=owner_id,
        )).job_id

    def _rows(self, operation_type=None, user_id=None):
        sql, params, clauses = "SELECT operation_type, user_id, job_id, reserved_at FROM analysis_quota_reservations", {}, []
        if operation_type is not None:
            clauses.append("operation_type = :op")
            params["op"] = operation_type
        if user_id is not None:
            clauses.append("user_id = :uid")
            params["uid"] = user_id
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.engine.connect() as connection:
            return connection.execute(text(sql + " ORDER BY id"), params).fetchall()

    def _global(self):
        return self._rows(PROVIDER_CALL_GLOBAL_OPERATION_TYPE)

    def _admin(self):
        return [(r.user_id, r.job_id) for r in self._rows(PROVIDER_CALL_ADMIN_OPERATION_TYPE)]

    def _seed(self, reserved_ats, operation_type=PROVIDER_CALL_GLOBAL_OPERATION_TYPE,
              user_id=PROVIDER_CALL_GLOBAL_USER_ID):
        with self.engine.begin() as connection:
            for at in reserved_ats:
                connection.execute(text(_INSERT_SQL), {"op": operation_type, "uid": user_id, "jid": None, "at": at})

    def _clear_guards(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM analysis_guards"))

    def _snapshot(self):
        with self.engine.connect() as connection:
            return (
                connection.execute(text("SELECT * FROM analysis_quota_reservations ORDER BY id")).fetchall(),
                connection.execute(text("SELECT * FROM analysis_guards ORDER BY operation_type, resource_id")).fetchall(),
                connection.execute(text("SELECT * FROM profile_analysis ORDER BY analysis_id")).fetchall(),
                connection.execute(text("SELECT * FROM job_analysis ORDER BY analysis_id")).fetchall(),
                connection.execute(text("SELECT * FROM cover_letters ORDER BY letter_id")).fetchall(),
            )

    # -- requests ---------------------------------------------------------------

    def _analyze_profile(self, user_id, profile_id, headers, force=True, **seam):
        seam = seam or {"return_value": _valid_response()}
        with patch(PROFILE_SEAM, **seam) as mock:
            response = self.client.post(f"/users/{user_id}/profiles/{profile_id}/analyze",
                                        params={"force_reanalyze": "true"} if force else None, headers=headers)
        self._clear_guards()
        return response, mock

    def _analyze_job(self, job_id, headers, **seam):
        seam = seam or {"return_value": _mock_job_response()}
        with patch(JOB_SEAM, **seam) as mock:
            response = self.client.post(f"/jobs/{job_id}/analyze", headers=headers)
        self._clear_guards()
        return response, mock

    def _create_letter(self, user_id, profile_id, job_id, headers, language="en", **seam):
        seam = seam or {"return_value": _letter_response()}
        with patch(LETTER_SEAM, **seam) as mock:
            response = self.client.post(f"/users/{user_id}/profiles/{profile_id}/jobs/{job_id}/cover-letters",
                                        headers=headers, json={"language": language})
        return response, mock

    def _regenerate(self, user_id, letter_id, headers, **seam):
        seam = seam or {"return_value": _letter_response()}
        with patch(LETTER_SEAM, **seam) as mock:
            response = self.client.post(f"/users/{user_id}/cover-letters/{letter_id}/regenerate", headers=headers)
        return response, mock


# ---------------------------------------------------------------------------


class GlobalQuotaConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, _env_without_global(), clear=True):
            config = load_provider_call_quota_config()
        self.assertEqual((config.max_attempts, config.window_seconds), (LIMIT, WINDOW))
        self.assertEqual(ProviderCallQuotaConfig(), config)

    def test_overrides_and_blank(self):
        with patch.dict(os.environ, {GLOBAL_ENVS[0]: "3", GLOBAL_ENVS[1]: "600"}):
            config = load_provider_call_quota_config()
        self.assertEqual((config.max_attempts, config.window_seconds), (3, 600))
        config = load_provider_call_quota_config({GLOBAL_ENVS[0]: "", GLOBAL_ENVS[1]: "7"})
        self.assertEqual((config.max_attempts, config.window_seconds), (LIMIT, 7))

    def test_invalid_values_rejected(self):
        for name in GLOBAL_ENVS:
            for bad in ("0", "-1", "not-a-number", "1.5"):
                with self.subTest(env=name, value=bad), patch.dict(os.environ, {name: bad}):
                    with self.assertRaises(AnalysisGuardConfigError):
                        load_provider_call_quota_config()

    def test_other_quota_envs_do_not_change_it(self):
        with patch.dict(os.environ, _env_without_global(), clear=True), patch.dict(
            os.environ, {"PROFILE_ANALYSIS_USER_QUOTA_MAX_ATTEMPTS": "3", "COVER_LETTER_DAILY_LIMIT": "2"}
        ):
            self.assertEqual(load_provider_call_quota_config().max_attempts, LIMIT)


class OneRowPerProviderCallTests(_Base):
    def setUp(self):
        super().setUp()
        self.owner_id, self.owner = self._user("owner@example.com")
        self.admin_id, self.admin = self._user("admin@example.com", is_admin=True)
        self.profile_id = self._profile(self.owner_id)
        self.catalog_job = self._job("catalog")

    def test_non_admin_profile_job_and_letter_calls_each_take_one_global_row(self):
        response, mock = self._analyze_profile(self.owner_id, self.profile_id, self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()
        self.assertEqual([(r.user_id, r.job_id) for r in self._global()], [(0, None)])

        own_job = self._job("own", owner_id=self.owner_id)
        response, mock = self._analyze_job(own_job, self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()
        self.assertEqual([(r.user_id, r.job_id) for r in self._global()], [(0, None), (0, own_job)])

        response, mock = self._create_letter(self.owner_id, self.profile_id, self.catalog_job, self.owner)
        self.assertEqual(response.status_code, 201, response.text)
        mock.assert_called_once()
        response, mock = self._regenerate(self.owner_id, response.json()["letter_id"], self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()

        self.assertEqual(len(self._global()), 4)
        self.assertEqual(self._admin(), [])
        # The per-user quotas are counted as before, once each.
        self.assertEqual(len(self._rows(USER_OPERATION_TYPE, self.owner_id)), 1)
        self.assertEqual(len(self._rows(JOB_USER_OPERATION_TYPE, self.owner_id)), 1)
        self.assertEqual(len(self._rows(LETTER_LETTER_QUOTA, self.owner_id)), 2)

    def test_admin_calls_record_one_admin_row_each_and_touch_no_quota(self):
        # The owner's per-user quotas and the global quota are all used up.
        now = time.time()
        self._seed([now - 10] * LIMIT)
        self._seed([now - 10] * 10, USER_OPERATION_TYPE, self.owner_id)
        self._seed([now - 10] * 10, LETTER_LETTER_QUOTA, self.owner_id)
        quota_rows = self._rows()

        response, mock = self._analyze_profile(self.owner_id, self.profile_id, self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()
        admin_profile = self._profile(self.admin_id)
        response, _ = self._analyze_profile(self.admin_id, admin_profile, self.admin)
        self.assertEqual(response.status_code, 200, response.text)

        response, mock = self._analyze_job(self.catalog_job, self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()

        # A letter for another user (create and regenerate), although that
        # user's own letter quota is full (owner decision: admins exempt).
        response, mock = self._create_letter(self.owner_id, self.profile_id, self.catalog_job, self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        mock.assert_called_once()
        response, mock = self._regenerate(self.owner_id, response.json()["letter_id"], self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()

        self.assertEqual(
            self._admin(),
            [(self.admin_id, None), (self.admin_id, None), (self.admin_id, self.catalog_job),
             (self.admin_id, None), (self.admin_id, None)],
        )
        non_admin_rows = [r for r in self._rows() if r.operation_type != PROVIDER_CALL_ADMIN_OPERATION_TYPE]
        self.assertEqual(non_admin_rows, quota_rows)

        # The owner is still blocked.
        response, mock = self._create_letter(self.owner_id, self.profile_id, self.catalog_job, self.owner, language="de")
        self.assertEqual(response.status_code, 429, response.text)
        mock.assert_not_called()

    def test_batch_routes_record_one_admin_row_per_provider_call(self):
        second = self._job("catalog-2")
        with patch(JOB_SEAM, side_effect=lambda *a, **k: _mock_job_response()) as mock:
            response = self.client.post("/jobs/analyze-missing", params={"limit": 5}, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(sorted(self._admin()), sorted([(self.admin_id, self.catalog_job), (self.admin_id, second)]))
        self._clear_guards()

        with patch(SAMPLE_SEAM, side_effect=lambda *a, **k: _mock_sample_response()) as mock:
            response = self.client.post("/jobs/analyze-sample", params={"job_id_list": [self.catalog_job, second]},
                                        headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(len(self._admin()), 4)
        self.assertEqual(self._global(), [])

    def test_batch_jobs_whose_acquisition_is_refused_make_no_provider_call(self):
        """If a per-job acquisition were ever refused for quota, the batch
        starts no provider call for that job: analyze-missing reports that
        job as failed and continues, analyze-sample aborts (all or
        nothing), both as before for cooldowns."""
        exceeded = analysis_guard.AcquireResult(outcome=AcquireOutcome.QUOTA_EXCEEDED, retry_after_seconds=77)
        with patch.object(jobs, "try_acquire_job_analysis_guard", return_value=exceeded), \
                patch(JOB_SEAM) as job_seam, patch(SAMPLE_SEAM) as sample_seam:
            missing = self.client.post("/jobs/analyze-missing", params={"limit": 1}, headers=self.admin)
            self._clear_guards()  # the batch cooldown of analyze-missing
            sample = self.client.post("/jobs/analyze-sample", params={"job_id_list": [self.catalog_job]},
                                      headers=self.admin)
        job_seam.assert_not_called()
        sample_seam.assert_not_called()
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertEqual(missing.json()["results"][0]["status"], "failed")
        self.assertEqual(sample.status_code, 429, sample.text)
        self.assertEqual(sample.headers["Retry-After"], "77")

        unknown = analysis_guard.AcquireResult(outcome=MagicMock(name="unknown"))
        self._clear_guards()
        with patch.object(jobs, "try_acquire_job_analysis_guard", return_value=unknown), patch(SAMPLE_SEAM) as seam:
            sample = self.client.post("/jobs/analyze-sample", params={"job_id_list": [self.catalog_job]},
                                      headers=self.admin)
        seam.assert_not_called()
        self.assertEqual(sample.status_code, 503, sample.text)


class GlobalLimitTests(_Base):
    def setUp(self):
        super().setUp()
        self.users = [self._user(f"user{i}@example.com") for i in range(3)]
        self.admin_id, self.admin = self._user("admin@example.com", is_admin=True)
        self.catalog_job = self._job("catalog")

    def _assert_bounds(self, retry_after, oldest, start, end):
        self.assertGreaterEqual(retry_after, math.ceil(oldest + WINDOW - end))
        self.assertLessEqual(retry_after, math.ceil(oldest + WINDOW - start))

    def test_full_global_quota_refuses_every_non_admin_with_retry_after(self):
        start = time.time()
        oldest = start - WINDOW + 1000.5
        self._seed([oldest] + [start - 5] * (LIMIT - 1))
        (u0, h0), (u1, h1), (u2, h2) = self.users
        p0, p2 = self._profile(u0), self._profile(u2)
        own_job = self._job("own", owner_id=u1)
        before = self._snapshot()

        response, mock = self._analyze_profile(u0, p0, h0)
        end = time.time()
        self.assertEqual((response.status_code, response.json()), (429, TOO_MANY))
        mock.assert_not_called()
        self._assert_bounds(int(response.headers["Retry-After"]), oldest, start, end)

        response, mock = self._analyze_job(own_job, h1)
        end = time.time()
        self.assertEqual((response.status_code, response.json()), (429, TOO_MANY))
        mock.assert_not_called()
        self._assert_bounds(int(response.headers["Retry-After"]), oldest, start, end)

        response, mock = self._create_letter(u2, p2, self.catalog_job, h2)
        end = time.time()
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_COVER_LETTER_SERVICE_LIMIT")
        self.assertNotIn("daily limit of letter drafts", response.text)
        mock.assert_not_called()
        self._assert_bounds(int(response.headers["Retry-After"]), oldest, start, end)

        # Nothing written: no per-user slot, no lease, no analysis, no letter.
        self.assertEqual(self._snapshot(), before)

        # An admin still succeeds, recorded but not counted.
        response, mock = self._analyze_profile(u0, p0, self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self._global()), LIMIT)
        self.assertEqual(self._admin(), [(self.admin_id, None)])

    def test_rolling_window_with_a_fake_clock(self):
        clock = _FakeClock()
        t0 = clock()
        oldest = t0 - WINDOW + 123.5
        self._seed([oldest] + [t0 - 5] * (LIMIT - 1))
        user_id = self.users[0][0]
        accounting = ProviderCallAccounting(user_id, False, ProviderCallQuotaConfig())

        def acquire(profile_id):
            db = self.Session()
            try:
                return try_acquire_profile_analysis_guard(
                    db, profile_id=profile_id, owner_user_id=user_id, config=load_config(),
                    clock=clock, count_user_quota=True, provider_call=accounting,
                )
            finally:
                db.close()

        result = acquire(1)
        self.assertEqual((result.outcome, result.retry_after_seconds), (AcquireOutcome.QUOTA_EXCEEDED, 124))
        self.assertEqual(self._rows(USER_OPERATION_TYPE), [])
        clock.advance(123)
        self.assertEqual(acquire(1).retry_after_seconds, 1)
        clock.advance(0.5)  # now == oldest + window: that slot is free
        result = acquire(1)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        reserved = [r.reserved_at for r in self._global()]
        self.assertEqual(len(reserved), LIMIT)
        self.assertNotIn(oldest, reserved)  # pruned
        self.assertIn(clock(), reserved)
        self.assertEqual(len(self._rows(USER_OPERATION_TYPE, user_id)), 1)

    def test_cover_letter_window_and_lowered_config_with_a_fake_time(self):
        t0 = 2_000_000.0
        self._seed([t0 - 50, t0 - 40, t0 - 30])
        user_id = self.users[0][0]
        small = ProviderCallAccounting(user_id, False, ProviderCallQuotaConfig(max_attempts=3, window_seconds=100))
        db = self.Session()
        try:
            with self.assertRaises(cover_letter.LetterGlobalQuotaExceeded) as ctx:
                cover_letter.acquire_generation(db, user_id, t0, provider_call=small)
            self.assertEqual(ctx.exception.retry_after_seconds, 50)
            self.assertEqual(self._rows(LETTER_LETTER_QUOTA), [])
            token = cover_letter.acquire_generation(db, user_id, t0 + 50, provider_call=small)
            cover_letter.release_generation(db, user_id, token)
        finally:
            db.close()
        self.assertEqual(len(self._rows(LETTER_LETTER_QUOTA, user_id)), 1)
        self.assertEqual([r.reserved_at for r in self._global()], [t0 - 40, t0 - 30, t0 + 50])

    def test_invalid_global_config_is_a_500_before_anything_is_written(self):
        (u0, h0) = self.users[0]
        p0 = self._profile(u0)
        own_job = self._job("own", owner_id=u0)
        before = self._snapshot()
        with patch.dict(os.environ, {GLOBAL_ENVS[0]: "0"}):
            profile, profile_seam = self._analyze_profile(u0, p0, h0)
            job, job_seam = self._analyze_job(own_job, h0)
            letter, letter_seam = self._create_letter(u0, p0, self.catalog_job, h0)
        self.assertEqual([profile.status_code, job.status_code, letter.status_code], [500, 500, 500])
        self.assertEqual(letter.json()["detail"]["error_code"], "ERR_COVER_LETTER_CONFIG")
        for seam in (profile_seam, job_seam, letter_seam):
            seam.assert_not_called()
        self.assertEqual(self._snapshot(), before)


class PerUserVersusGlobalTests(_Base):
    def setUp(self):
        super().setUp()
        self.user_id, self.headers = self._user("user@example.com")
        self.profile_id = self._profile(self.user_id)
        self.own_job = self._job("own", owner_id=self.user_id)
        self.catalog_job = self._job("catalog")

    def test_per_user_quota_still_applies_and_its_429_takes_no_global_slot(self):
        now = time.time()
        self._seed([now - 10] * 10, USER_OPERATION_TYPE, self.user_id)
        self._seed([now - 10] * 10, JOB_USER_OPERATION_TYPE, self.user_id)
        self._seed([now - 10] * 10, LETTER_LETTER_QUOTA, self.user_id)
        before = self._snapshot()
        response, mock = self._analyze_profile(self.user_id, self.profile_id, self.headers)
        self.assertEqual(response.status_code, 429)
        mock.assert_not_called()
        response, mock = self._analyze_job(self.own_job, self.headers)
        self.assertEqual(response.status_code, 429)
        mock.assert_not_called()
        response, mock = self._create_letter(self.user_id, self.profile_id, self.catalog_job, self.headers)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_COVER_LETTER_QUOTA")
        mock.assert_not_called()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._global(), [])

    def test_global_429_takes_no_per_user_slot(self):
        self._seed([time.time() - 10] * LIMIT)
        for request in (
            lambda: self._analyze_profile(self.user_id, self.profile_id, self.headers),
            lambda: self._analyze_job(self.own_job, self.headers),
            lambda: self._create_letter(self.user_id, self.profile_id, self.catalog_job, self.headers),
        ):
            response, mock = request()
            self.assertEqual(response.status_code, 429, response.text)
            mock.assert_not_called()
        self.assertEqual(self._rows(user_id=self.user_id), [])

    def test_last_slots_of_both_are_taken_together(self):
        now = time.time()
        self._seed([now - 10] * (LIMIT - 1))
        self._seed([now - 10] * 9, USER_OPERATION_TYPE, self.user_id)
        response, _ = self._analyze_profile(self.user_id, self.profile_id, self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((len(self._global()), len(self._rows(USER_OPERATION_TYPE))), (LIMIT, 10))
        response, mock = self._analyze_job(self.own_job, self.headers)
        self.assertEqual(response.status_code, 429)
        mock.assert_not_called()


class NotCountedTests(_Base):
    def setUp(self):
        super().setUp()
        self.user_id, self.headers = self._user("user@example.com")
        self.other_id, self.other = self._user("other@example.com")
        self.profile_id = self._profile(self.user_id)
        self.catalog_job = self._job("catalog")

    def test_refused_and_cached_requests_leave_no_global_row(self):
        # Cache hit.
        self._add(models.ProfileAnalysis(
            profile_id=self.profile_id, analysis_status="completed", analysis_json="{}",
            analysis_model=PROFILE_ANALYSIS_MODEL, analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
            is_current=True,
        ))
        response, mock = self._analyze_profile(self.user_id, self.profile_id, self.headers, force=False)
        self.assertEqual(response.json()["status"], "cached")
        mock.assert_not_called()

        cases = []
        cases.append(("401", self._analyze_profile(self.user_id, self.profile_id, {})))
        cases.append(("404 other profile", self._analyze_profile(self.user_id, self.profile_id, self.other)))
        others_job = self._job("others", owner_id=self.other_id)
        cases.append(("404 other job", self._analyze_job(others_job, self.headers)))
        with patch.object(profiles, "client", None), patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            cases.append(("503 not configured", self._analyze_profile(self.user_id, self.profile_id, self.headers)))
        with patch.object(cover_letters, "client", None):
            cases.append(("503 letter", self._create_letter(self.user_id, self.profile_id, self.catalog_job, self.headers)))
        # Guard refusals: in-flight 409, cooldown 429, letter busy 409, target changed 404.
        self._add(models.AnalysisGuard(operation_type=USER_OPERATION_TYPE, resource_id=self.user_id,
                                       owner_token="other", lock_expires_at=time.time() + 600))
        with patch(PROFILE_SEAM) as seam:
            response = self.client.post(f"/users/{self.user_id}/profiles/{self.profile_id}/analyze",
                                        params={"force_reanalyze": "true"}, headers=self.headers)
        cases.append(("409", (response, seam)))
        self._clear_guards()
        self._add(models.AnalysisGuard(operation_type=USER_OPERATION_TYPE, resource_id=self.user_id,
                                       cooldown_until=time.time() + 600))
        with patch(PROFILE_SEAM) as seam:
            response = self.client.post(f"/users/{self.user_id}/profiles/{self.profile_id}/analyze",
                                        params={"force_reanalyze": "true"}, headers=self.headers)
        cases.append(("cooldown 429", (response, seam)))
        self._clear_guards()
        db = self.Session()
        cover_letter.acquire_generation(db, self.user_id)  # a letter in flight
        db.close()
        cases.append(("letter busy", self._create_letter(self.user_id, self.profile_id, self.catalog_job, self.headers)))
        self._clear_guards()
        real_acquire = profiles.try_acquire_profile_analysis_guard
        with patch.object(profiles, "try_acquire_profile_analysis_guard",
                          side_effect=lambda *a, **k: real_acquire(*a, **{**k, "precondition": lambda s: False})):
            cases.append(("target changed", self._analyze_profile(self.user_id, self.profile_id, self.headers)))

        expected = {"401": 401, "404 other profile": 404, "404 other job": 404, "503 not configured": 503,
                    "503 letter": 503, "409": 409, "cooldown 429": 429, "letter busy": 409, "target changed": 404}
        for name, (response, seam) in cases:
            with self.subTest(case=name):
                self.assertEqual(response.status_code, expected[name], response.text)
                seam.assert_not_called()
        self.assertEqual(self._global(), [])
        self.assertEqual(self._admin(), [])


class CountedFailuresTests(_Base):
    def test_provider_failures_still_use_their_global_slot(self):
        user_id, headers = self._user("user@example.com")
        profile_id = self._profile(user_id)
        own_job = self._job("own", owner_id=user_id)
        catalog_job = self._job("catalog")
        bad_json, bad_schema = MagicMock(), MagicMock()
        bad_json.output_text = "not valid json{{{"
        bad_schema.output_text = json.dumps({"unexpected": 1})
        statuses = []
        for seam in ({"side_effect": RuntimeError("synthetic")}, {"return_value": bad_json},
                     {"return_value": bad_schema}, {"side_effect": _spend_limit_error()}):
            response, mock = self._analyze_profile(user_id, profile_id, headers, **seam)
            mock.assert_called_once()
            statuses.append(response.status_code)
        response, mock = self._analyze_job(own_job, headers, side_effect=RuntimeError("synthetic"))
        mock.assert_called_once()
        statuses.append(response.status_code)
        for seam in ({"side_effect": RuntimeError("synthetic")}, {"side_effect": _spend_limit_error()},
                     {"return_value": bad_json}):
            response, mock = self._create_letter(user_id, profile_id, catalog_job, headers, **seam)
            mock.assert_called_once()
            statuses.append(response.status_code)
        self.assertEqual(statuses, [500, 500, 500, 503, 500, 502, 503, 502])
        self.assertEqual(len(self._global()), 8)

    def test_fake_provider_calls_are_counted_like_real_ones(self):
        user_id, headers = self._user("user@example.com")
        profile_id = self._profile(user_id)
        with patch.object(profiles, "client", FakeAnalysisClient()):
            response = self.client.post(f"/users/{user_id}/profiles/{profile_id}/analyze", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self._global()), 1)


class ConcurrencyTests(_Base):
    def test_two_users_at_ninety_nine_get_exactly_one_call(self):
        self._seed([time.time() - 100] * (LIMIT - 1))
        pairs = []
        for name in ("one", "two"):
            user_id, headers = self._user(f"{name}@example.com")
            pairs.append((user_id, self._profile(user_id), headers))
        barrier = threading.Barrier(2, timeout=10)
        results = {}

        def worker(user_id, profile_id, headers):
            barrier.wait()
            results[user_id] = self.client.post(f"/users/{user_id}/profiles/{profile_id}/analyze", headers=headers)

        with patch(PROFILE_SEAM, side_effect=lambda *a, **k: _valid_response()) as mock:
            threads = [threading.Thread(target=worker, args=pair) for pair in pairs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(sorted(r.status_code for r in results.values()), [200, 429])
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(len(self._global()), LIMIT)
        self.assertEqual(len(self._rows(USER_OPERATION_TYPE)), 1)


class GlobalReservationLockTests(_Base):
    """At global 99, the global count runs -- and the 100th slot is reserved
    -- while the transaction that also holds the guard / lease already holds
    SQLite's write lock, and is committed with it: no other connection can
    insert a reservation or acquire in between. The probe runs right BEFORE
    the real global reservation (so before its own insert could take the
    lock)."""

    def _probe(self, outcomes, competing=None):
        blocked = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False, "timeout": 0.1})
        self.addCleanup(blocked.dispose)
        real_reserve = analysis_guard._reserve_quota_slot

        def probe_then_reserve(session, quota, now):
            if quota.operation_type == PROVIDER_CALL_GLOBAL_OPERATION_TYPE and not outcomes:
                try:
                    with blocked.begin() as connection:
                        connection.execute(text(_INSERT_SQL), {"op": PROVIDER_CALL_GLOBAL_OPERATION_TYPE,
                                                               "uid": 0, "jid": None, "at": time.time()})
                    outcomes["insert"] = "committed"
                except OperationalError as exc:
                    outcomes["insert"] = "locked" if "database is locked" in str(exc) else repr(exc)
                if competing is not None:
                    outcomes["competing"] = competing(sessionmaker(bind=blocked))
            return real_reserve(session, quota, now)

        return patch.object(analysis_guard, "_reserve_quota_slot", side_effect=probe_then_reserve)

    def test_profile_guard_transaction_holds_the_lock(self):
        self._seed([time.time() - 100] * (LIMIT - 1))
        user_id, headers = self._user("one@example.com")
        other_id, other_headers = self._user("two@example.com")
        profile_id, other_profile = self._profile(user_id), self._profile(other_id)
        outcomes = {}

        def competing(factory):
            db = factory()
            try:
                return try_acquire_profile_analysis_guard(
                    db, profile_id=other_profile, owner_user_id=other_id, config=load_config(),
                    count_user_quota=True,
                    provider_call=ProviderCallAccounting(other_id, False, load_provider_call_quota_config()),
                ).outcome
            finally:
                db.close()

        with self._probe(outcomes, competing):
            response, mock = self._analyze_profile(user_id, profile_id, headers)
        self.assertEqual(outcomes, {"insert": "locked", "competing": AcquireOutcome.BACKEND_UNAVAILABLE})
        self.assertEqual(response.status_code, 200, response.text)
        mock.assert_called_once()
        self.assertEqual(len(self._global()), LIMIT)
        response, mock = self._analyze_profile(other_id, other_profile, other_headers)
        self.assertEqual(response.status_code, 429)
        mock.assert_not_called()

    def test_cover_letter_lease_transaction_holds_the_lock(self):
        self._seed([time.time() - 100] * (LIMIT - 1))
        user_id, headers = self._user("one@example.com")
        other_id, other_headers = self._user("two@example.com")
        catalog_job = self._job("catalog")
        outcomes = {}
        with self._probe(outcomes):
            response, mock = self._create_letter(user_id, self._profile(user_id), catalog_job, headers)
        self.assertEqual(outcomes, {"insert": "locked"})
        self.assertEqual(response.status_code, 201, response.text)
        mock.assert_called_once()
        self.assertEqual(len(self._global()), LIMIT)
        response, mock = self._create_letter(other_id, self._profile(other_id), catalog_job, other_headers)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_COVER_LETTER_SERVICE_LIMIT")
        mock.assert_not_called()


class MissingQuotaTableRouteTests(_Base):
    """Without analysis_quota_reservations, admin calls cannot write their
    record row either: nothing reaches the provider and nothing is kept."""

    def setUp(self):
        super().setUp()
        self.admin_id, self.admin = self._user("admin@example.com", is_admin=True)
        self.user_id, self.headers = self._user("user@example.com")
        self.profile_id = self._profile(self.user_id)
        self.catalog_job = self._job("catalog")
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE analysis_quota_reservations"))

    def _tables(self):
        with self.engine.connect() as connection:
            return (
                connection.execute(text("SELECT * FROM analysis_guards")).fetchall(),
                connection.execute(text("SELECT * FROM job_analysis")).fetchall(),
                connection.execute(text("SELECT * FROM cover_letters")).fetchall(),
            )

    def test_admin_single_job_analysis_is_503_without_a_provider_call(self):
        response, mock = self._analyze_job(self.catalog_job, self.admin)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "Service temporarily unavailable. Please try again shortly."})
        mock.assert_not_called()
        self.assertEqual(self._tables(), ([], [], []))

    def test_cover_letter_fails_with_a_database_error_and_keeps_nothing(self):
        # Current behavior (unchanged by W17): acquire_generation does not
        # catch the database error, so the request ends in an unhandled
        # 500; the lease write is never committed.
        client = TestClient(self.client.app, raise_server_exceptions=False)
        for headers in (self.headers, self.admin):
            with self.subTest(admin=headers is self.admin), patch(LETTER_SEAM) as mock:
                response = client.post(
                    f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{self.catalog_job}/cover-letters",
                    headers=headers, json={"language": "en"},
                )
            self.assertEqual(response.status_code, 500)
            mock.assert_not_called()
            self.assertEqual(self._tables(), ([], [], []))


class DeleteUserKeepsGlobalRowsTests(_Base):
    def test_deleting_and_re_registering_refunds_no_global_slot(self):
        user_id, headers = self._user("leaver@example.com")
        profile_id = self._profile(user_id)
        self._seed([time.time() - 10] * (LIMIT - 1))
        response, _ = self._analyze_profile(user_id, profile_id, headers)
        self.assertEqual(response.status_code, 200, response.text)
        global_rows = self._global()
        self.assertEqual(len(global_rows), LIMIT)

        response = self.client.delete(f"/users/{user_id}", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._rows(user_id=user_id), [])
        self.assertEqual(self._global(), global_rows)

        # The same id again (SQLite reuses it), with a fresh account.
        new_id, new_headers = self._user("leaver.again@example.com")
        response, mock = self._analyze_profile(new_id, self._profile(new_id), new_headers)
        self.assertEqual(response.status_code, 429, response.text)
        mock.assert_not_called()
        self.assertEqual(self._global(), global_rows)


if __name__ == "__main__":
    unittest.main()
