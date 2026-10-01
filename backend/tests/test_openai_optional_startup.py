import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from openai import OpenAI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)
from backend.app.database import Base, get_db
from backend.app.openai_client import (
    ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL,
    create_openai_client_or_none,
)
from backend.app.security import create_access_token, hash_password

# Imported with a synthetic key, exactly like the other router test
# modules, so whichever module imports the routers first leaves them in the
# same state regardless of discovery order. Each test below then sets
# profiles.client / jobs.client and OPENAI_API_KEY explicitly (and restores
# both afterward), so no test here depends on import-time state. The real
# "import without a key" behavior is verified in a fresh subprocess.
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
# Every environment credential the installed OpenAI SDK accepts; all are
# stripped for the "no key" scenarios so a developer's shell cannot leak in.
OPENAI_CREDENTIAL_ENVS = (OPENAI_API_KEY_ENV, "OPENAI_ADMIN_KEY")
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"
SYNTHETIC_OPENAI_ADMIN_KEY = "sk-admin-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {OPENAI_API_KEY_ENV: SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs, profiles, users


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTHETIC_SECRET = "synthetic-test-secret-for-openai-optional-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"

VALID_PROFILE_ANALYSIS_PAYLOAD = {
    "candidate_summary": "synthetic summary",
    "current_role_family": "other",
    "target_role_families": ["other"],
    "target_role_tags": ["other"],
    "target_roles": [],
    "excluded_roles": [],
    "strong_skills": [],
    "moderate_skills": [],
    "weak_or_basic_skills": [],
    "tools": [],
    "industries": [],
    "years_of_experience": None,
    "seniority_level": "unknown",
    "education_level": "unknown",
    "field_of_study": "unknown",
    "languages": [],
    "visa_sponsorship_needed": "unknown",
    "work_authorization_status": "unknown",
    "relocation_preference": "unknown",
    "current_residence_country": "unknown",
    "student_status": "unknown",
    "match_notes": [],
}

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


def _mock_response(payload):
    response = MagicMock()
    response.output_text = json.dumps(payload)
    return response


def _make_minimal_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _environ_without_openai_key():
    return {k: v for k, v in os.environ.items() if k not in OPENAI_CREDENTIAL_ENVS}


class OpenAIClientHelperTests(unittest.TestCase):
    def _assert_none_without_constructing(self, extra_env):
        env = _environ_without_openai_key()
        env.update(extra_env)
        with patch.dict(os.environ, env, clear=True), \
             patch("backend.app.openai_client.OpenAI") as mock_openai:
            self.assertIsNone(create_openai_client_or_none())
        mock_openai.assert_not_called()

    def test_returns_none_when_key_missing(self):
        self._assert_none_without_constructing({})

    def test_returns_none_when_key_empty(self):
        self._assert_none_without_constructing({OPENAI_API_KEY_ENV: ""})

    def test_returns_none_when_key_whitespace_only(self):
        self._assert_none_without_constructing({OPENAI_API_KEY_ENV: "   \t"})

    def test_returns_none_when_only_admin_key_is_set(self):
        # The SDK itself would accept OPENAI_ADMIN_KEY alone, but the
        # Responses API used by the analysis seams does not authenticate
        # with it.
        self._assert_none_without_constructing({"OPENAI_ADMIN_KEY": SYNTHETIC_OPENAI_ADMIN_KEY})

    def test_returns_client_when_key_present_without_network(self):
        env = _environ_without_openai_key()
        env[OPENAI_API_KEY_ENV] = SYNTHETIC_OPENAI_API_KEY
        with patch.dict(os.environ, env, clear=True):
            client = create_openai_client_or_none()
        # Real SDK construction (no request is sent at construction time).
        self.assertIsInstance(client, OpenAI)


class ImportWithoutKeyTests(unittest.TestCase):
    """Runs in a fresh interpreter so module import caching in this test
    process cannot hide an import-time failure. Only the routers are
    imported (never backend.app.main, whose create_all would target the
    real apply101.db); no DB connection is opened.
    """

    def test_routers_import_and_docs_work_without_openai_key(self):
        code = (
            "from fastapi import FastAPI\n"
            "from fastapi.testclient import TestClient\n"
            "from backend.routers import auth, jobs, matches, profiles, users\n"
            "assert profiles.client is None\n"
            "assert jobs.client is None\n"
            "app = FastAPI()\n"
            "for r in (users, profiles, jobs, matches, auth):\n"
            "    app.include_router(r.router)\n"
            "c = TestClient(app)\n"
            "assert c.get('/docs').status_code == 200\n"
            "assert c.get('/openapi.json').status_code == 200\n"
            "print('IMPORT_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=_environ_without_openai_key(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("IMPORT_OK", result.stdout)


class _BaseNoKeyTestCase(unittest.TestCase):
    """Throwaway synthetic SQLite database (never apply101.db), users +
    profiles + jobs routers mounted, a synthetic JWT secret, OPENAI_API_KEY
    removed from the environment and both router clients set to None
    (i.e. the state after starting the app without a key). All of it is
    restored after each test.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_openai_optional_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(users.router)
        app.include_router(profiles.router)
        app.include_router(jobs.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env = _environ_without_openai_key()
        env["JWT_SECRET_KEY"] = SYNTHETIC_SECRET
        env_patcher = patch.dict(os.environ, env, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        for module in (profiles, jobs):
            client_patcher = patch.object(module, "client", None)
            client_patcher.start()
            self.addCleanup(client_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_user(self, mail, is_admin=False):
        session = self.session_factory()
        try:
            user = models.User(
                name="Synthetic User",
                mail=mail,
                password_hash=hash_password(VALID_PASSWORD),
                is_admin=is_admin,
            )
            session.add(user)
            session.commit()
            return user.user_id
        finally:
            session.close()

    def _create_profile(self, user_id):
        session = self.session_factory()
        try:
            profile = models.CandidateProfile(user_id=user_id, self_description="synthetic profile")
            session.add(profile)
            session.commit()
            return profile.profile_id
        finally:
            session.close()

    def _create_job(self, url="https://example.com/job/synthetic-1"):
        session = self.session_factory()
        try:
            job = models.Job(title="Synthetic Job", url=url, description_text="synthetic description")
            session.add(job)
            session.commit()
            return job.job_id
        finally:
            session.close()

    def _seed_profile_analysis(self, profile_id):
        session = self.session_factory()
        try:
            session.add(models.ProfileAnalysis(
                profile_id=profile_id,
                analysis_status="completed",
                analysis_json=json.dumps(VALID_PROFILE_ANALYSIS_PAYLOAD),
                analysis_model=PROFILE_ANALYSIS_MODEL,
                analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
                is_current=True,
            ))
            session.commit()
        finally:
            session.close()

    def _seed_job_analysis(self, job_id):
        session = self.session_factory()
        try:
            session.add(models.JobAnalysis(
                job_id=job_id,
                analysis_status="completed",
                analysis_json=json.dumps(VALID_JOB_ANALYSIS_PAYLOAD),
                analysis_model=JOB_ANALYSIS_MODEL,
                analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
                is_current=True,
            ))
            session.commit()
        finally:
            session.close()

    def _auth_headers(self, user_id):
        # Mint with the user's real stored token key, exactly as login does.
        session = self.session_factory()
        try:
            user = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one()
            token_key = user.token_key
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    def _count(self, table):
        with self.engine.connect() as connection:
            return connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()

    def _job_analysis_rows(self):
        with self.engine.connect() as connection:
            return connection.execute(text(
                "SELECT analysis_id, job_id, analysis_status, analysis_json, "
                "analysis_prompt_version, is_current FROM job_analysis ORDER BY analysis_id"
            )).fetchall()

    def _assert_not_configured(self, response):
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL})


class NonAnalysisEndpointsWithoutKeyTests(_BaseNoKeyTestCase):
    def test_job_listing_and_profile_crud_work_without_key(self):
        user_id = self._create_user("no.key.reader@example.com")
        headers = self._auth_headers(user_id)

        self.assertEqual(self.client.get("/jobs/", headers=headers).status_code, 200)

        created = self.client.post(
            f"/users/{user_id}/profiles",
            json={"profile_name": "synthetic"},
            headers=headers,
        )
        self.assertEqual(created.status_code, 200, created.text)

        listed = self.client.get(f"/users/{user_id}/profiles", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)

    def test_cv_upload_works_without_key(self):
        user_id = self._create_user("no.key.upload@example.com")
        profile_id = self._create_profile(user_id)

        # The route writes to a relative "uploads" directory: run it inside
        # a temp workdir so the real repository uploads/ is never touched.
        workdir = os.path.join(self.tmp_dir, "workdir")
        os.makedirs(workdir)
        original_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/upload-cv",
                files={"file": ("cv.pdf", _make_minimal_pdf_bytes(), "application/pdf")},
                headers=self._auth_headers(user_id),
            )
        finally:
            os.chdir(original_cwd)

        self.assertEqual(response.status_code, 200, response.text)


class AnalysisEndpointsWithoutKeyTests(_BaseNoKeyTestCase):
    def test_profile_analyze_returns_503_before_guard_or_db_writes(self):
        user_id = self._create_user("no.key.profile@example.com")
        profile_id = self._create_profile(user_id)

        with patch("backend.routers.profiles.try_acquire_profile_analysis_guard") as mock_acquire, \
             patch("backend.routers.profiles._create_profile_analysis_response") as mock_seam:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self._assert_not_configured(response)
        mock_acquire.assert_not_called()
        mock_seam.assert_not_called()
        self.assertEqual(self._count("analysis_guards"), 0)
        self.assertEqual(self._count("profile_analysis"), 0)

    def test_profile_force_reanalyze_over_cache_returns_503(self):
        user_id = self._create_user("no.key.profile.force@example.com")
        profile_id = self._create_profile(user_id)
        self._seed_profile_analysis(profile_id)

        response = self.client.post(
            f"/users/{user_id}/profiles/{profile_id}/analyze?force_reanalyze=true",
            headers=self._auth_headers(user_id),
        )

        self._assert_not_configured(response)
        self.assertEqual(self._count("analysis_guards"), 0)
        self.assertEqual(self._count("profile_analysis"), 1)

    def test_single_job_analyze_returns_503_before_guard_or_db_writes(self):
        admin_id = self._create_user("no.key.job@example.com", is_admin=True)
        job_id = self._create_job()

        with patch("backend.routers.jobs.try_acquire_job_analysis_guard") as mock_acquire, \
             patch("backend.routers.jobs._create_job_analysis_response") as mock_seam:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self._assert_not_configured(response)
        mock_acquire.assert_not_called()
        mock_seam.assert_not_called()
        self.assertEqual(self._count("analysis_guards"), 0)
        self.assertEqual(self._count("job_analysis"), 0)

    def test_analyze_missing_returns_503_before_batch_guard(self):
        admin_id = self._create_user("no.key.batch@example.com", is_admin=True)
        self._create_job()

        with patch("backend.routers.jobs.try_acquire_job_batch_guard") as mock_batch, \
             patch("backend.routers.jobs._create_job_analysis_response") as mock_seam:
            response = self.client.post(
                "/jobs/analyze-missing", headers=self._auth_headers(admin_id)
            )

        self._assert_not_configured(response)
        mock_batch.assert_not_called()
        mock_seam.assert_not_called()
        self.assertEqual(self._count("analysis_guards"), 0)
        self.assertEqual(self._count("job_analysis"), 0)

    def test_job_force_reanalyze_over_cache_returns_503_and_keeps_cache(self):
        admin_id = self._create_user("no.key.job.force@example.com", is_admin=True)
        job_id = self._create_job()
        self._seed_job_analysis(job_id)
        before = self._job_analysis_rows()

        with patch("backend.routers.jobs.try_acquire_job_analysis_guard") as mock_acquire,              patch("backend.routers.jobs._create_job_analysis_response") as mock_seam:
            response = self.client.post(
                f"/jobs/{job_id}/analyze?force_reanalyze=true",
                headers=self._auth_headers(admin_id),
            )

        self._assert_not_configured(response)
        mock_acquire.assert_not_called()
        mock_seam.assert_not_called()
        self.assertEqual(self._count("analysis_guards"), 0)
        self.assertEqual(self._job_analysis_rows(), before)
        self.assertEqual(len(before), 1)

    def test_admin_key_only_is_treated_as_not_configured(self):
        # Only a (fake) OPENAI_ADMIN_KEY: the SDK would build a client, but
        # the route must still stop with 503 before any guard/DB work.
        admin_id = self._create_user("admin.key.only@example.com", is_admin=True)
        job_id = self._create_job()

        with patch.dict(os.environ, {"OPENAI_ADMIN_KEY": SYNTHETIC_OPENAI_ADMIN_KEY}, clear=False),              patch("backend.routers.jobs.try_acquire_job_analysis_guard") as mock_acquire,              patch("backend.routers.jobs._create_job_analysis_response") as mock_seam:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self._assert_not_configured(response)
        mock_acquire.assert_not_called()
        mock_seam.assert_not_called()
        self.assertIsNone(jobs.client)
        self.assertEqual(self._count("analysis_guards"), 0)
        self.assertEqual(self._count("job_analysis"), 0)

    def test_analyze_sample_returns_503_before_batch_guard(self):
        admin_id = self._create_user("no.key.sample@example.com", is_admin=True)
        self._create_job()

        with patch("backend.routers.jobs.try_acquire_job_batch_guard") as mock_batch, \
             patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_seam:
            response = self.client.post(
                "/jobs/analyze-sample", headers=self._auth_headers(admin_id)
            )

        self._assert_not_configured(response)
        mock_batch.assert_not_called()
        mock_seam.assert_not_called()
        self.assertEqual(self._count("analysis_guards"), 0)

    def test_cached_profile_analysis_still_returned_without_key(self):
        user_id = self._create_user("no.key.profile.cache@example.com")
        profile_id = self._create_profile(user_id)
        self._seed_profile_analysis(profile_id)

        response = self.client.post(
            f"/users/{user_id}/profiles/{profile_id}/analyze",
            headers=self._auth_headers(user_id),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cached")
        self.assertEqual(self._count("analysis_guards"), 0)

    def test_cached_job_analysis_still_returned_without_key(self):
        admin_id = self._create_user("no.key.job.cache@example.com", is_admin=True)
        job_id = self._create_job()
        self._seed_job_analysis(job_id)

        response = self.client.post(
            f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cached")
        self.assertEqual(self._count("analysis_guards"), 0)

    def test_analyze_missing_with_nothing_to_analyze_is_still_a_no_op(self):
        admin_id = self._create_user("no.key.batch.noop@example.com", is_admin=True)
        job_id = self._create_job()
        self._seed_job_analysis(job_id)

        response = self.client.post(
            "/jobs/analyze-missing", headers=self._auth_headers(admin_id)
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "no_jobs_to_analyze")


class AnalysisWithKeyAddedAfterStartupTests(_BaseNoKeyTestCase):
    """The key is absent at import (client None) but present at request
    time: the client is created lazily and the existing seams are still the
    only OpenAI call sites.
    """

    def setUp(self):
        super().setUp()
        key_patcher = patch.dict(os.environ, {OPENAI_API_KEY_ENV: SYNTHETIC_OPENAI_API_KEY}, clear=False)
        key_patcher.start()
        self.addCleanup(key_patcher.stop)

    def test_profile_analyze_uses_existing_seam(self):
        user_id = self._create_user("with.key.profile@example.com")
        profile_id = self._create_profile(user_id)

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_response(VALID_PROFILE_ANALYSIS_PAYLOAD),
        ) as mock_seam:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_seam.assert_called_once()
        self.assertIsNotNone(profiles.client)

    def test_single_job_analyze_uses_existing_seam(self):
        admin_id = self._create_user("with.key.job@example.com", is_admin=True)
        job_id = self._create_job()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_response(VALID_JOB_ANALYSIS_PAYLOAD),
        ) as mock_seam:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(admin_id)
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_seam.assert_called_once()
        self.assertIsNotNone(jobs.client)


if __name__ == "__main__":
    unittest.main()
