import json
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
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password
from backend.app.analysis_guard import (
    AcquireOutcome,
    AcquireResult,
    JOB_BATCH_OPERATION_TYPE,
    JOB_BATCH_RESOURCE_ID,
    JOB_OPERATION_TYPE,
    RenewOutcome,
    RenewResult,
    load_job_analysis_batch_config,
    load_job_analysis_config,
    release_job_analysis_guard,
    release_job_batch_guard,
    renew_job_batch_guard as real_renew_job_batch_guard,
    try_acquire_job_analysis_guard,
    try_acquire_job_batch_guard,
)

# jobs.py constructs an OpenAI client at import time (`client = OpenAI()`),
# which raises immediately if no API key is configured. OPENAI_API_KEY is
# synthetic only for the duration of this import -- patch.dict restores the
# real environment immediately afterward (same pattern used for the D2
# profiles.py test-isolation fix). Every OpenAI-reaching call in this file
# is mocked, so no real key/network access is ever needed.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-authz-jobs-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"

# Minimal synthetic payload satisfying schemas.JobAnalysisStructured
# (extra="forbid", so this must be exactly the schema's fields).
VALID_JOB_ANALYSIS_PAYLOAD = {
    "summary": "synthetic summary",
    "role_family": "other",
    "role_subfamily": "other",
    "normalized_role_title": "synthetic role",
    "role_tags": ["other"],
    "required_skills": [],
    "preferred_skills": [],
    "responsibilities": [],
    "seniority_level": "unknown",
    "language_requirements": [],
    "visa_sponsorship": "unknown",
    "visa_sponsorship_evidence": None,
    "work_type": "unknown",
    "employment_type": "unknown",
    "hard_requirements": {
        "student_enrollment_required": False,
        "student_enrollment_evidence": None,
        "work_authorization": "unknown",
        "work_authorization_evidence": None,
        "residency": "unknown",
        "residency_locations": [],
        "residency_evidence": None,
        "minimum_years_experience": None,
        "minimum_years_experience_evidence": None,
    },
    "dealbreakers": [],
}

# Minimal payload for analyze-sample's own separate, unstructured/unvalidated
# response shape (no schemas.JobAnalysisStructured involved there).
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


def _mock_job_response(payload=None):
    response = MagicMock()
    response.output_text = json.dumps(payload or VALID_JOB_ANALYSIS_PAYLOAD)
    return response


def _mock_job_sample_response(payload=None):
    response = MagicMock()
    response.output_text = json.dumps(payload or VALID_JOB_SAMPLE_PAYLOAD)
    return response


def build_test_app(engine):
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


class _BaseJobsAuthorizationTestCase(unittest.TestCase):
    """Shared setup: a throwaway synthetic SQLite database (never
    apply101.db), only jobs.router mounted, a synthetic JWT secret, and
    helpers for creating synthetic users/jobs and minting real access
    tokens without going through /auth/login.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_authz_jobs_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)

        self.app, self.session_factory = build_test_app(self.engine)
        self.client = TestClient(self.app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_user(
        self, mail, password=VALID_PASSWORD, is_admin=False, name="Synthetic User"
    ):
        session = self.session_factory()
        try:
            user = models.User(
                name=name,
                mail=mail,
                password_hash=hash_password(password) if password else None,
                is_admin=is_admin,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _create_job(
        self,
        url="https://example.com/job/synthetic-1",
        title="Synthetic Job",
        description_text="synthetic description",
    ):
        session = self.session_factory()
        try:
            job = models.Job(title=title, url=url, description_text=description_text)
            session.add(job)
            session.commit()
            session.refresh(job)
            return job.job_id
        finally:
            session.close()

    def _auth_headers(self, user_id):
        token = create_access_token(user_id)
        return {"Authorization": f"Bearer {token}"}

    def _guard_row(self, operation_type, resource_id):
        # Reads (owner_token, lock_expires_at, cooldown_until) for one
        # dimension directly, independent of the route/guard module.
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT owner_token, lock_expires_at, cooldown_until FROM analysis_guards "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"op": operation_type, "rid": resource_id},
            ).fetchone()

    def _acquire_job_guard_directly(self, job_id):
        session = self.session_factory()
        try:
            return try_acquire_job_analysis_guard(
                session, job_id=job_id, config=load_job_analysis_config(),
            )
        finally:
            session.close()

    def _acquire_batch_guard_directly(self):
        session = self.session_factory()
        try:
            per_job_config = load_job_analysis_config()
            batch_config = load_job_analysis_batch_config(
                job_guard_lease_seconds=per_job_config.lease_ttl_seconds
            )
            return try_acquire_job_batch_guard(session, config=batch_config)
        finally:
            session.close()

    def _seed_completed_job_analysis(self, job_id):
        from backend.app.analysis_contract import (
            JOB_ANALYSIS_MODEL,
            JOB_ANALYSIS_PROMPT_VERSION,
        )

        session = self.session_factory()
        try:
            analysis = models.JobAnalysis(
                job_id=job_id,
                analysis_status="completed",
                analysis_json=json.dumps(VALID_JOB_ANALYSIS_PAYLOAD),
                analysis_model=JOB_ANALYSIS_MODEL,
                analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
                is_current=True,
            )
            session.add(analysis)
            session.commit()
        finally:
            session.close()


class ReadRouteAuthorizationTests(_BaseJobsAuthorizationTestCase):
    def test_get_jobs_missing_token_is_401(self):
        response = self.client.get("/jobs/")
        self.assertEqual(response.status_code, 401)

    def test_get_jobs_malformed_token_is_401(self):
        response = self.client.get(
            "/jobs/", headers={"Authorization": "Bearer not-a-real-jwt"}
        )
        self.assertEqual(response.status_code, 401)

    def test_get_jobs_normal_user_is_200(self):
        user_id = self._create_user("read.normal@example.com")
        response = self.client.get("/jobs/", headers=self._auth_headers(user_id))
        self.assertEqual(response.status_code, 200)

    def test_get_jobs_admin_is_200(self):
        admin_id = self._create_user("read.admin@example.com", is_admin=True)
        response = self.client.get("/jobs/", headers=self._auth_headers(admin_id))
        self.assertEqual(response.status_code, 200)

    def test_get_analyzed_jobs_normal_user_passes_auth(self):
        user_id = self._create_user("read.analyzed@example.com")
        response = self.client.get(
            "/jobs/analyzed", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 200)

    def test_get_analyzed_job_normal_user_passes_auth_reaches_business_layer(self):
        user_id = self._create_user("read.analyzed.single@example.com")
        job_id = self._create_job()

        # Job exists but has no completed analysis -> existing business 404,
        # proving authorization already succeeded.
        response = self.client.get(
            f"/jobs/analyzed/{job_id}", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("No current completed analysis", response.json()["detail"])


class AdminRouteAuthorizationTests(_BaseJobsAuthorizationTestCase):
    def test_fetch_runs_normal_user_is_403(self):
        user_id = self._create_user("admin.fetchruns.normal@example.com")
        response = self.client.get(
            "/jobs/fetch-runs", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 403)

    def test_fetch_missing_token_is_401(self):
        response = self.client.post("/jobs/fetch")
        self.assertEqual(response.status_code, 401)

    def test_fetch_normal_user_is_403(self):
        user_id = self._create_user("admin.fetch.normal@example.com")
        response = self.client.post(
            "/jobs/fetch", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 403)

    def test_fetch_pages_normal_user_is_403(self):
        user_id = self._create_user("admin.fetchpages.normal@example.com")
        response = self.client.post(
            "/jobs/fetch-pages?alljobs=false&maxpage=1",
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 403)

    def test_analyze_job_missing_token_is_401(self):
        job_id = self._create_job()
        response = self.client.post(f"/jobs/{job_id}/analyze")
        self.assertEqual(response.status_code, 401)

    def test_analyze_job_normal_user_is_403(self):
        user_id = self._create_user("admin.analyze.normal@example.com")
        job_id = self._create_job()
        response = self.client.post(
            f"/jobs/{job_id}/analyze", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 403)

    def test_analyze_missing_normal_user_is_403(self):
        user_id = self._create_user("admin.analyzemissing.normal@example.com")
        response = self.client.post(
            "/jobs/analyze-missing", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 403)

    def test_analyze_sample_normal_user_is_403(self):
        user_id = self._create_user("admin.analyzesample.normal@example.com")
        response = self.client.post(
            "/jobs/analyze-sample", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 403)


class DenialBeforeSideEffectTests(_BaseJobsAuthorizationTestCase):
    def test_fetch_denied_before_requests_get(self):
        user_id = self._create_user("denial.fetch.normal@example.com")

        with patch("backend.routers.jobs.requests.get") as mock_get:
            response = self.client.post(
                "/jobs/fetch", headers=self._auth_headers(user_id)
            )

        self.assertEqual(response.status_code, 403)
        mock_get.assert_not_called()

    def test_fetch_missing_token_denied_before_requests_get(self):
        with patch("backend.routers.jobs.requests.get") as mock_get:
            response = self.client.post("/jobs/fetch")

        self.assertEqual(response.status_code, 401)
        mock_get.assert_not_called()

    def test_fetch_pages_denied_before_requests_get_and_sleep(self):
        user_id = self._create_user("denial.fetchpages.normal@example.com")

        with patch("backend.routers.jobs.requests.get") as mock_get, patch(
            "backend.routers.jobs.time.sleep"
        ) as mock_sleep:
            response = self.client.post(
                "/jobs/fetch-pages?alljobs=false&maxpage=1",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 403)
        mock_get.assert_not_called()
        mock_sleep.assert_not_called()

    def test_analyze_job_denied_before_openai_call(self):
        user_id = self._create_user("denial.analyze.normal@example.com")
        job_id = self._create_job()

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(user_id)
            )

        self.assertEqual(response.status_code, 403)
        mock_openai.assert_not_called()

    def test_analyze_missing_denied_before_openai_and_helper(self):
        user_id = self._create_user("denial.analyzemissing.normal@example.com")

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai, patch(
            "backend.routers.jobs._analyze_job_impl"
        ) as mock_impl:
            response = self.client.post(
                "/jobs/analyze-missing", headers=self._auth_headers(user_id)
            )

        self.assertEqual(response.status_code, 403)
        mock_openai.assert_not_called()
        mock_impl.assert_not_called()

    def test_analyze_sample_denied_before_openai_call(self):
        user_id = self._create_user("denial.analyzesample.normal@example.com")

        with patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_openai:
            response = self.client.post(
                "/jobs/analyze-sample", headers=self._auth_headers(user_id)
            )

        self.assertEqual(response.status_code, 403)
        mock_openai.assert_not_called()


class AdminSuccessAuthPassTests(_BaseJobsAuthorizationTestCase):
    def test_fetch_runs_admin_200(self):
        admin_id = self._create_user("pass.fetchruns.admin@example.com", is_admin=True)
        response = self.client.get(
            "/jobs/fetch-runs", headers=self._auth_headers(admin_id)
        )
        self.assertEqual(response.status_code, 200)

    def test_analyze_job_admin_reaches_description_missing_business_error(self):
        admin_id = self._create_user("pass.analyze.admin@example.com", is_admin=True)
        job_id = self._create_job(description_text=None)

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_JOB_DESCRIPTION_MISSING"
        )
        mock_openai.assert_not_called()

    def test_analyze_missing_admin_empty_db_returns_no_jobs_to_analyze(self):
        admin_id = self._create_user(
            "pass.analyzemissing.admin@example.com", is_admin=True
        )

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self.client.post(
                "/jobs/analyze-missing", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_jobs_to_analyze")
        mock_openai.assert_not_called()

    def test_analyze_sample_admin_empty_db_returns_no_jobs_404(self):
        admin_id = self._create_user(
            "pass.analyzesample.admin@example.com", is_admin=True
        )

        with patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_openai:
            response = self.client.post(
                "/jobs/analyze-sample", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_NO_JOBS")
        mock_openai.assert_not_called()

    def test_fetch_admin_with_mocked_deterministic_response(self):
        admin_id = self._create_user("pass.fetch.admin@example.com", is_admin=True)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": []}

        with patch(
            "backend.routers.jobs.requests.get", return_value=mock_response
        ) as mock_get:
            response = self.client.post(
                "/jobs/fetch", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["saved_count"], 0)
        mock_get.assert_called_once()

    def test_fetch_pages_admin_with_mocked_empty_page(self):
        admin_id = self._create_user("pass.fetchpages.admin@example.com", is_admin=True)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        with patch(
            "backend.routers.jobs.requests.get", return_value=mock_response
        ) as mock_get, patch("backend.routers.jobs.time.sleep") as mock_sleep:
            response = self.client.post(
                "/jobs/fetch-pages?alljobs=false&maxpage=1",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stopped_reason"], "EMPTY_PAGE")
        mock_get.assert_called_once()
        # A single empty page breaks the loop before any sleep is reached.
        mock_sleep.assert_not_called()


class InternalHelperDelegationTests(_BaseJobsAuthorizationTestCase):
    """Proves the structural refactor is real: the single-analyze route
    delegates to _analyze_job_impl (A), and analyze_missing_jobs calls that
    same internal helper rather than the public, Depends()-protected
    analyze_job route function (B).
    """

    def test_single_analyze_route_delegates_to_internal_helper(self):
        admin_id = self._create_user("delegate.single.admin@example.com", is_admin=True)
        job_id = self._create_job()

        sentinel_result = {"status": "created", "analysis_id": 999}
        with patch(
            "backend.routers.jobs._analyze_job_impl",
            return_value=sentinel_result,
        ) as mock_impl:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), sentinel_result)
        mock_impl.assert_called_once()

        call_kwargs = mock_impl.call_args.kwargs
        self.assertEqual(call_kwargs["job_id"], job_id)
        self.assertEqual(call_kwargs["force_reanalyze"], False)

    def test_analyze_missing_calls_internal_helper_not_public_route(self):
        # Calling the public route function directly (bypassing Depends())
        # would silently skip authorization for every job in the batch.
        # This proves analyze_missing_jobs calls _analyze_job_impl, never
        # analyze_job, for each job.
        admin_id = self._create_user("delegate.batch.admin@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs.analyze_job"
        ) as mock_public_route, patch(
            "backend.routers.jobs._analyze_job_impl",
            return_value={"status": "created", "job_id": job_id, "analysis_id": 1},
        ) as mock_impl:
            response = self.client.post(
                "/jobs/analyze-missing", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        mock_impl.assert_called_once()
        mock_public_route.assert_not_called()


class JobsOpenAIWrapperTests(unittest.TestCase):
    """_create_job_analysis_response / _create_job_sample_analysis_response
    are the seams E3.3 relies on for applying configured timeout/max_retries
    -- patching client.responses.create directly does NOT work, since
    client.with_options(...) returns a distinct client/resource instance
    (same mechanism verified for E3.2's profiles.py wrapper).
    """

    def test_normal_wrapper_applies_configured_timeout_and_max_retries_without_network(self):
        fake_scoped_client = MagicMock()
        fake_scoped_client.responses.create.return_value = _mock_job_response()

        with patch("backend.routers.jobs.client") as fake_client:
            fake_client.with_options.return_value = fake_scoped_client
            result = jobs._create_job_analysis_response(
                "a synthetic prompt", timeout_seconds=42, max_retries=3,
            )

        fake_client.with_options.assert_called_once_with(max_retries=3)
        _, kwargs = fake_scoped_client.responses.create.call_args
        self.assertEqual(kwargs["timeout"], 42)
        self.assertEqual(result, fake_scoped_client.responses.create.return_value)

    def test_sample_wrapper_applies_configured_timeout_and_max_retries_without_network(self):
        fake_scoped_client = MagicMock()
        fake_scoped_client.responses.create.return_value = _mock_job_sample_response()

        with patch("backend.routers.jobs.client") as fake_client:
            fake_client.with_options.return_value = fake_scoped_client
            result = jobs._create_job_sample_analysis_response(
                "a synthetic prompt", timeout_seconds=17, max_retries=2,
            )

        fake_client.with_options.assert_called_once_with(max_retries=2)
        _, kwargs = fake_scoped_client.responses.create.call_args
        self.assertEqual(kwargs["timeout"], 17)
        self.assertEqual(result, fake_scoped_client.responses.create.return_value)


class ManualAnalyzeGuardTests(_BaseJobsAuthorizationTestCase):
    def test_cache_hit_bypasses_config_guard_and_openai(self):
        admin_id = self._create_user("job.guard.cache.hit@example.com", is_admin=True)
        job_id = self._create_job()
        self._seed_completed_job_analysis(job_id)

        with patch("backend.routers.jobs.load_job_analysis_config") as mock_config, \
             patch("backend.routers.jobs.try_acquire_job_analysis_guard") as mock_acquire, \
             patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cached")
        mock_config.assert_not_called()
        mock_acquire.assert_not_called()
        mock_wrapper.assert_not_called()

    def test_manual_guard_acquired_before_openai_call_begins(self):
        admin_id = self._create_user("job.guard.timing@example.com", is_admin=True)
        job_id = self._create_job()

        guard_state_at_call_time = {}

        def check_guard_and_respond(prompt, *, timeout_seconds, max_retries):
            guard_state_at_call_time["row"] = self._guard_row(JOB_OPERATION_TYPE, job_id)
            return _mock_job_response()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=check_guard_and_respond,
        ):
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        row = guard_state_at_call_time.get("row")
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])  # owner_token already committed

    def test_manual_active_guard_returns_409_no_retry_after(self):
        admin_id = self._create_user("job.guard.active@example.com", is_admin=True)
        job_id = self._create_job()
        result = self._acquire_job_guard_directly(job_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("Retry-After", response.headers)
        mock_wrapper.assert_not_called()

    def test_manual_cooldown_returns_429_with_retry_after(self):
        admin_id = self._create_user("job.guard.cooldown@example.com", is_admin=True)
        job_id = self._create_job()
        acquired = self._acquire_job_guard_directly(job_id)
        self.assertEqual(acquired.outcome, AcquireOutcome.GRANTED)

        session = self.session_factory()
        try:
            release_job_analysis_guard(
                session, job_id=job_id, owner_token=acquired.owner_token,
                succeeded=True, config=load_job_analysis_config(),
            )
        finally:
            session.close()

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "300")
        mock_wrapper.assert_not_called()

    def test_manual_backend_unavailable_returns_503(self):
        admin_id = self._create_user("job.guard.backend.unavailable@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs.try_acquire_job_analysis_guard",
            return_value=AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE),
        ), patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 503)
        mock_wrapper.assert_not_called()

    def test_manual_invalid_config_returns_500_no_guard_mutation_no_failed_row(self):
        admin_id = self._create_user("job.guard.invalid.config@example.com", is_admin=True)
        job_id = self._create_job()

        with patch.dict(
            os.environ, {"JOB_ANALYSIS_OPENAI_TIMEOUT_SECONDS": "0"}, clear=False
        ), patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 500)
        mock_wrapper.assert_not_called()

        self.assertIsNone(self._guard_row(JOB_OPERATION_TYPE, job_id))

        session = self.session_factory()
        try:
            rows = session.query(models.JobAnalysis).filter(
                models.JobAnalysis.job_id == job_id
            ).all()
        finally:
            session.close()
        self.assertEqual(rows, [])

    def test_manual_exactly_one_release_on_success(self):
        admin_id = self._create_user("job.guard.release.count.success@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ), patch(
            "backend.routers.jobs.release_job_analysis_guard",
            wraps=jobs.release_job_analysis_guard,
        ) as mock_release:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_release.call_count, 1)

    def test_manual_exactly_one_release_on_failure(self):
        admin_id = self._create_user("job.guard.release.count.failure@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=RuntimeError("simulated OpenAI failure"),
        ), patch(
            "backend.routers.jobs.release_job_analysis_guard",
            wraps=jobs.release_job_analysis_guard,
        ) as mock_release:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(mock_release.call_count, 1)

    def test_manual_release_failure_does_not_replace_success(self):
        admin_id = self._create_user("job.guard.release.raises.success@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ), patch(
            "backend.routers.jobs.release_job_analysis_guard",
            side_effect=RuntimeError("simulated unexpected release failure"),
        ):
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")

    def test_manual_release_failure_does_not_replace_error(self):
        admin_id = self._create_user("job.guard.release.raises.failure@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=RuntimeError("simulated OpenAI failure"),
        ), patch(
            "backend.routers.jobs.release_job_analysis_guard",
            side_effect=RuntimeError("simulated unexpected release failure"),
        ):
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_JOB_ANALYSIS_FAILED"
        )

    def test_manual_success_releases_into_success_cooldown(self):
        admin_id = self._create_user("job.guard.success.cooldown@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ):
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )
        self.assertEqual(response.status_code, 200)

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            second = self.client.post(
                f"/jobs/{job_id}/analyze?force_reanalyze=true",
                headers=self._auth_headers(admin_id),
            )
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.headers.get("Retry-After"), "300")
        mock_wrapper.assert_not_called()

    def test_manual_failure_releases_into_failure_cooldown(self):
        admin_id = self._create_user("job.guard.failure.cooldown@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=RuntimeError("simulated OpenAI failure"),
        ):
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )
        self.assertEqual(response.status_code, 500)

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            second = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.headers.get("Retry-After"), "30")
        mock_wrapper.assert_not_called()


class BatchGuardRouteTests(_BaseJobsAuthorizationTestCase):
    def test_analyze_missing_empty_selection_bypasses_config_and_guards(self):
        admin_id = self._create_user("job.batch.missing.empty@example.com", is_admin=True)

        with patch("backend.routers.jobs.load_job_analysis_config") as mock_job_config, \
             patch("backend.routers.jobs.load_job_analysis_batch_config") as mock_batch_config, \
             patch("backend.routers.jobs.try_acquire_job_batch_guard") as mock_batch_acquire, \
             patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                "/jobs/analyze-missing", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_jobs_to_analyze")
        mock_job_config.assert_not_called()
        mock_batch_config.assert_not_called()
        mock_batch_acquire.assert_not_called()
        mock_wrapper.assert_not_called()

    def test_analyze_sample_empty_selection_bypasses_config_and_guards(self):
        admin_id = self._create_user("job.batch.sample.empty@example.com", is_admin=True)

        with patch("backend.routers.jobs.load_job_analysis_config") as mock_job_config, \
             patch("backend.routers.jobs.load_job_analysis_batch_config") as mock_batch_config, \
             patch("backend.routers.jobs.try_acquire_job_batch_guard") as mock_batch_acquire, \
             patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_wrapper:
            response = self.client.post(
                "/jobs/analyze-sample", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 404)
        mock_job_config.assert_not_called()
        mock_batch_config.assert_not_called()
        mock_batch_acquire.assert_not_called()
        mock_wrapper.assert_not_called()

    def test_shared_batch_lock_blocks_both_endpoints(self):
        admin_id = self._create_user("job.batch.shared.lock@example.com", is_admin=True)
        job_id = self._create_job()

        result = self._acquire_batch_guard_directly()
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            missing_response = self.client.post(
                "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
            )
        self.assertEqual(missing_response.status_code, 409)
        mock_wrapper.assert_not_called()

        with patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_sample_wrapper:
            sample_response = self.client.post(
                f"/jobs/analyze-sample?job_id_list={job_id}",
                headers=self._auth_headers(admin_id),
            )
        self.assertEqual(sample_response.status_code, 409)
        mock_sample_wrapper.assert_not_called()

    def test_batch_cooldown_returns_429_with_retry_after(self):
        admin_id = self._create_user("job.batch.cooldown@example.com", is_admin=True)
        self._create_job()

        acquired = self._acquire_batch_guard_directly()
        self.assertEqual(acquired.outcome, AcquireOutcome.GRANTED)

        session = self.session_factory()
        try:
            per_job_config = load_job_analysis_config()
            batch_config = load_job_analysis_batch_config(
                job_guard_lease_seconds=per_job_config.lease_ttl_seconds
            )
            release_job_batch_guard(
                session, owner_token=acquired.owner_token, succeeded=True, config=batch_config,
            )
        finally:
            session.close()

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "60")
        mock_wrapper.assert_not_called()

    def test_batch_config_error_returns_500_before_guard_once_jobs_exist(self):
        admin_id = self._create_user("job.batch.invalid.config@example.com", is_admin=True)
        job_id = self._create_job()

        with patch.dict(
            os.environ, {"JOB_ANALYSIS_OPENAI_TIMEOUT_SECONDS": "0"}, clear=False
        ), patch("backend.routers.jobs.try_acquire_job_batch_guard") as mock_batch_acquire, \
           patch("backend.routers.jobs._create_job_analysis_response") as mock_wrapper:
            response = self.client.post(
                "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 500)
        mock_batch_acquire.assert_not_called()
        mock_wrapper.assert_not_called()
        self.assertIsNone(self._guard_row(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID))
        self.assertIsNone(self._guard_row(JOB_OPERATION_TYPE, job_id))

    def test_batch_release_exactly_once_on_success(self):
        admin_id = self._create_user("job.batch.release.count.success@example.com", is_admin=True)
        self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ), patch(
            "backend.routers.jobs.release_job_batch_guard",
            wraps=jobs.release_job_batch_guard,
        ) as mock_release:
            response = self.client.post(
                "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_release.call_count, 1)

    def test_batch_release_failure_does_not_replace_success(self):
        admin_id = self._create_user("job.batch.release.raises.success@example.com", is_admin=True)
        self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ), patch(
            "backend.routers.jobs.release_job_batch_guard",
            side_effect=RuntimeError("simulated unexpected batch release failure"),
        ):
            response = self.client.post(
                "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")

    def test_mid_batch_ownership_lost_stops_further_openai_calls(self):
        admin_id = self._create_user("job.batch.ownership.lost@example.com", is_admin=True)
        job1 = self._create_job(url="https://example.com/job/ownership-lost-1")
        job2 = self._create_job(url="https://example.com/job/ownership-lost-2")

        renew_call_count = {"n": 0}

        def fake_renew(db, *, owner_token, config, clock=time.time):
            renew_call_count["n"] += 1
            if renew_call_count["n"] == 1:
                return real_renew_job_batch_guard(db, owner_token=owner_token, config=config, clock=clock)
            return RenewResult(outcome=RenewOutcome.OWNERSHIP_LOST)

        with patch("backend.routers.jobs.renew_job_batch_guard", side_effect=fake_renew), \
             patch(
                 "backend.routers.jobs._create_job_analysis_response",
                 return_value=_mock_job_response(),
             ) as mock_wrapper, \
             patch(
                 "backend.routers.jobs.release_job_batch_guard",
                 wraps=jobs.release_job_batch_guard,
             ) as mock_batch_release:
            response = self.client.post(
                "/jobs/analyze-missing?limit=2", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(mock_wrapper.call_count, 1)  # only the first job's call happened
        self.assertEqual(mock_batch_release.call_count, 1)  # exactly one batch release

        # The first job (order_by job_id.desc(), so job2 is selected first)
        # remains committed even though the batch as a whole failed.
        session = self.session_factory()
        try:
            completed = session.query(models.JobAnalysis).filter(
                models.JobAnalysis.analysis_status == "completed"
            ).all()
        finally:
            session.close()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].job_id, job2)

        # job1 was never reached -- no guard row exists for it at all.
        self.assertIsNone(self._guard_row(JOB_OPERATION_TYPE, job1))

        # The batch guard released with a FAILURE cooldown, not success.
        now = time.time()
        batch_row = self._guard_row(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID)
        self.assertIsNone(batch_row[0])
        self.assertLess(batch_row[2] - now, 60)  # failure cooldown (30s), not success (60s)

    def test_mid_batch_backend_unavailable_stops_further_openai_calls(self):
        admin_id = self._create_user("job.batch.backend.unavailable@example.com", is_admin=True)
        job1 = self._create_job(url="https://example.com/job/backend-unavailable-1")
        job2 = self._create_job(url="https://example.com/job/backend-unavailable-2")

        renew_call_count = {"n": 0}

        def fake_renew(db, *, owner_token, config, clock=time.time):
            renew_call_count["n"] += 1
            if renew_call_count["n"] == 1:
                return real_renew_job_batch_guard(db, owner_token=owner_token, config=config, clock=clock)
            return RenewResult(outcome=RenewOutcome.BACKEND_UNAVAILABLE)

        with patch("backend.routers.jobs.renew_job_batch_guard", side_effect=fake_renew), \
             patch(
                 "backend.routers.jobs._create_job_analysis_response",
                 return_value=_mock_job_response(),
             ) as mock_wrapper:
            response = self.client.post(
                "/jobs/analyze-missing?limit=2", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(mock_wrapper.call_count, 1)

    def test_analyze_missing_per_job_conflict_reported_as_failed_batch_continues(self):
        admin_id = self._create_user("job.batch.missing.perjob.conflict@example.com", is_admin=True)
        job1 = self._create_job(url="https://example.com/job/perjob-conflict-1")
        job2 = self._create_job(url="https://example.com/job/perjob-conflict-2")

        # job2 is selected first (order_by job_id.desc()) -- seed ITS
        # per-job guard as already active.
        result = self._acquire_job_guard_directly(job2)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_wrapper:
            response = self.client.post(
                "/jobs/analyze-missing?limit=2", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        results_by_job = {r["job_id"]: r for r in body["results"]}
        self.assertEqual(results_by_job[job2]["status"], "failed")
        self.assertEqual(results_by_job[job1]["status"], "created")
        self.assertEqual(mock_wrapper.call_count, 1)  # only job1's call happened

    def test_analyze_sample_per_job_conflict_aborts_the_request(self):
        admin_id = self._create_user("job.batch.sample.perjob.conflict@example.com", is_admin=True)
        job_id = self._create_job()

        result = self._acquire_job_guard_directly(job_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_wrapper:
            response = self.client.post(
                f"/jobs/analyze-sample?job_id_list={job_id}",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 409)
        mock_wrapper.assert_not_called()

    def test_analyze_sample_ordinary_exception_generic_500_no_raw_text_releases_and_no_row(self):
        admin_id = self._create_user("job.batch.sample.ordinary.exception@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            side_effect=RuntimeError("simulated OpenAI failure with sensitive internals"),
        ):
            response = self.client.post(
                f"/jobs/analyze-sample?job_id_list={job_id}",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 500)
        detail = response.json()["detail"]
        self.assertEqual(detail["error_code"], "ERR_JOB_ANALYSIS_FAILED")
        self.assertNotIn("error", detail)
        self.assertNotIn("sensitive internals", response.text)

        now = time.time()
        job_row = self._guard_row(JOB_OPERATION_TYPE, job_id)
        batch_row = self._guard_row(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID)
        self.assertIsNone(job_row[0])
        self.assertLess(job_row[2] - now, 60)  # failure cooldown
        self.assertIsNone(batch_row[0])
        self.assertLess(batch_row[2] - now, 60)  # failure cooldown

        session = self.session_factory()
        try:
            rows = session.query(models.JobAnalysis).filter(
                models.JobAnalysis.job_id == job_id
            ).all()
        finally:
            session.close()
        self.assertEqual(rows, [])

    def test_analyze_sample_invalid_json_keeps_dedicated_error_shape(self):
        admin_id = self._create_user("job.batch.sample.invalid.json@example.com", is_admin=True)
        job_id = self._create_job()

        bad_response = _mock_job_sample_response()
        bad_response.output_text = "not valid json{{{"

        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            return_value=bad_response,
        ):
            response = self.client.post(
                f"/jobs/analyze-sample?job_id_list={job_id}",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 500)
        detail = response.json()["detail"]
        self.assertEqual(detail["error_code"], "ERR_AI_INVALID_JSON")
        self.assertIn("raw_response", detail)  # existing shape, unchanged


class JobConcurrencyTests(_BaseJobsAuthorizationTestCase):
    """Real threads, real temp-file SQLite (via _BaseJobsAuthorizationTestCase's
    engine), genuinely distinct sessions per request. Coordinated via
    threading.Event/Barrier, never sleeps. Wrappers only -- no external calls.
    """

    def test_two_simultaneous_manual_requests_same_job_exactly_one_openai_call(self):
        admin_id = self._create_user("job.concurrency.manual.manual@example.com", is_admin=True)
        job_id = self._create_job()

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()
        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            with call_lock:
                call_count["n"] += 1
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_response()

        results = {}

        def first_request():
            with patch("backend.routers.jobs._create_job_analysis_response", side_effect=slow_call):
                results["first"] = self.client.post(
                    f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
                )

        first_thread = threading.Thread(target=first_request)
        first_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_second:
            second_response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(second_response.status_code, 409)
        mock_second.assert_not_called()

        release_openai_call.set()
        first_thread.join(timeout=10)

        self.assertEqual(results["first"].status_code, 200)
        self.assertEqual(call_count["n"], 1)

    def test_manual_vs_analyze_missing_same_job_exactly_one_openai_call(self):
        admin_id = self._create_user("job.concurrency.manual.missing@example.com", is_admin=True)
        job_id = self._create_job()

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()
        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            with call_lock:
                call_count["n"] += 1
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_response()

        results = {}

        def batch_request():
            with patch("backend.routers.jobs._create_job_analysis_response", side_effect=slow_call):
                results["batch"] = self.client.post(
                    "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
                )

        batch_thread = threading.Thread(target=batch_request)
        batch_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_manual:
            manual_response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(manual_response.status_code, 409)
        mock_manual.assert_not_called()

        release_openai_call.set()
        batch_thread.join(timeout=10)

        self.assertEqual(results["batch"].status_code, 200)
        self.assertEqual(call_count["n"], 1)

    def test_manual_vs_analyze_sample_same_job_exactly_one_openai_call(self):
        admin_id = self._create_user("job.concurrency.manual.sample@example.com", is_admin=True)
        job_id = self._create_job()

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()
        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            with call_lock:
                call_count["n"] += 1
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_sample_response()

        results = {}

        def sample_request():
            with patch("backend.routers.jobs._create_job_sample_analysis_response", side_effect=slow_call):
                results["sample"] = self.client.post(
                    f"/jobs/analyze-sample?job_id_list={job_id}",
                    headers=self._auth_headers(admin_id),
                )

        sample_thread = threading.Thread(target=sample_request)
        sample_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_manual:
            manual_response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(manual_response.status_code, 409)
        mock_manual.assert_not_called()

        release_openai_call.set()
        sample_thread.join(timeout=10)

        self.assertEqual(results["sample"].status_code, 200)
        self.assertEqual(call_count["n"], 1)

    def test_two_concurrent_batch_starts_missing_vs_missing_only_one_admitted(self):
        admin_id = self._create_user("job.concurrency.missing.missing@example.com", is_admin=True)
        self._create_job()

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_response()

        results = {}

        def first_batch():
            with patch("backend.routers.jobs._create_job_analysis_response", side_effect=slow_call):
                results["first"] = self.client.post(
                    "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
                )

        first_thread = threading.Thread(target=first_batch)
        first_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        second_response = self.client.post(
            "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
        )
        self.assertEqual(second_response.status_code, 409)

        release_openai_call.set()
        first_thread.join(timeout=10)
        self.assertEqual(results["first"].status_code, 200)

    def test_two_concurrent_batch_starts_sample_vs_sample_only_one_admitted(self):
        admin_id = self._create_user("job.concurrency.sample.sample@example.com", is_admin=True)
        job_id = self._create_job()

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_sample_response()

        results = {}

        def first_batch():
            with patch("backend.routers.jobs._create_job_sample_analysis_response", side_effect=slow_call):
                results["first"] = self.client.post(
                    f"/jobs/analyze-sample?job_id_list={job_id}",
                    headers=self._auth_headers(admin_id),
                )

        first_thread = threading.Thread(target=first_batch)
        first_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        second_response = self.client.post(
            f"/jobs/analyze-sample?job_id_list={job_id}",
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(second_response.status_code, 409)

        release_openai_call.set()
        first_thread.join(timeout=10)
        self.assertEqual(results["first"].status_code, 200)

    def test_cross_endpoint_missing_vs_sample_only_one_admitted(self):
        admin_id = self._create_user("job.concurrency.missing.sample@example.com", is_admin=True)
        job_id = self._create_job()

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_response()

        results = {}

        def missing_batch():
            with patch("backend.routers.jobs._create_job_analysis_response", side_effect=slow_call):
                results["missing"] = self.client.post(
                    "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
                )

        missing_thread = threading.Thread(target=missing_batch)
        missing_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        sample_response = self.client.post(
            f"/jobs/analyze-sample?job_id_list={job_id}",
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(sample_response.status_code, 409)

        release_openai_call.set()
        missing_thread.join(timeout=10)
        self.assertEqual(results["missing"].status_code, 200)

    def test_different_jobs_remain_independently_analyzable_concurrently(self):
        admin_id = self._create_user("job.concurrency.independent@example.com", is_admin=True)
        job_a = self._create_job(url="https://example.com/job/independent-a")
        job_b = self._create_job(url="https://example.com/job/independent-b")

        both_ready = threading.Barrier(2, timeout=10)
        results = {}

        def analyze(name, job_id):
            both_ready.wait()
            results[name] = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        # A single patch shared across both threads for the whole test --
        # unittest.mock.patch's enter/exit is NOT safe to race from two
        # threads concurrently entering/exiting the SAME target (unlike
        # the other concurrency tests in this file, where the second
        # patch only ever opens/closes while the first thread is fully
        # parked inside an Event.wait(), never itself touching the patch
        # stack at the same time).
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ):
            t1 = threading.Thread(target=analyze, args=("a", job_a))
            t2 = threading.Thread(target=analyze, args=("b", job_b))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

        self.assertEqual(results["a"].status_code, 200)
        self.assertEqual(results["b"].status_code, 200)

    def test_manual_analysis_allowed_during_active_batch_for_non_conflicting_job(self):
        admin_id = self._create_user("job.concurrency.manual.during.batch@example.com", is_admin=True)
        self._create_job(url="https://example.com/job/batch-only")

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()

        def slow_call(prompt, *, timeout_seconds, max_retries):
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_job_response()

        results = {}

        def batch_request():
            with patch("backend.routers.jobs._create_job_analysis_response", side_effect=slow_call):
                results["batch"] = self.client.post(
                    "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
                )

        batch_thread = threading.Thread(target=batch_request)
        batch_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        other_job = self._create_job(url="https://example.com/job/created-during-batch")
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ):
            manual_response = self.client.post(
                f"/jobs/{other_job}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(manual_response.status_code, 200)

        release_openai_call.set()
        batch_thread.join(timeout=10)
        self.assertEqual(results["batch"].status_code, 200)

    def test_stale_batch_lease_recovery(self):
        admin_id = self._create_user("job.concurrency.stale.batch.lease@example.com", is_admin=True)
        self._create_job()

        # Simulate a crashed prior batch: a stale, already-expired lease,
        # inserted directly (bypassing acquire) so no real elapsed time is
        # required for recovery.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO analysis_guards "
                    "(operation_type, resource_id, owner_token, lock_expires_at, cooldown_until) "
                    "VALUES (:op, :rid, :tok, :exp, NULL)"
                ),
                {
                    "op": JOB_BATCH_OPERATION_TYPE,
                    "rid": JOB_BATCH_RESOURCE_ID,
                    "tok": "crashed-owner",
                    "exp": time.time() - 1000,
                },
            )

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ):
            response = self.client.post(
                "/jobs/analyze-missing?limit=1", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")


if __name__ == "__main__":
    unittest.main()
