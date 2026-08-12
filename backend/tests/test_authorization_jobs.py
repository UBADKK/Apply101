import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password

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

        with patch("backend.routers.jobs.client.responses.create") as mock_openai:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(user_id)
            )

        self.assertEqual(response.status_code, 403)
        mock_openai.assert_not_called()

    def test_analyze_missing_denied_before_openai_and_helper(self):
        user_id = self._create_user("denial.analyzemissing.normal@example.com")

        with patch("backend.routers.jobs.client.responses.create") as mock_openai, patch(
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

        with patch("backend.routers.jobs.client.responses.create") as mock_openai:
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

        with patch("backend.routers.jobs.client.responses.create") as mock_openai:
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

        with patch("backend.routers.jobs.client.responses.create") as mock_openai:
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

        with patch("backend.routers.jobs.client.responses.create") as mock_openai:
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


if __name__ == "__main__":
    unittest.main()
