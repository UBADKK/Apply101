import json
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
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
)
from backend.app.database import Base, get_db
from backend.app.security import create_access_token

# Same import-time pattern as the other router test modules: a synthetic
# key only while importing, restored immediately afterward. Every
# OpenAI-reaching call below is patched at the module seam.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-job-visibility-0123456789"

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

NONEXISTENT_JOB_ID = 999999


def _mock_response(payload):
    response = MagicMock()
    response.output_text = json.dumps(payload)
    return response


class _BaseJobVisibilityTestCase(unittest.TestCase):
    """Throwaway synthetic SQLite database (never apply101.db), only
    jobs.router mounted, synthetic JWT secret."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_job_visibility_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)

        self.session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(jobs.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self.user_a_id = self._create_user("visibility.a@example.com")
        self.user_b_id = self._create_user("visibility.b@example.com")
        self.admin_id = self._create_user("visibility.admin@example.com", is_admin=True)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_user(self, mail, is_admin=False):
        session = self.session_factory()
        try:
            user = models.User(name="Synthetic User", mail=mail, is_admin=is_admin)
            session.add(user)
            session.commit()
            return user.user_id
        finally:
            session.close()

    def _create_job(self, slug, owner_id=None, description_text="synthetic description"):
        session = self.session_factory()
        try:
            job = models.Job(
                title=f"Synthetic Job {slug}",
                url=f"https://example.com/job/{slug}",
                description_text=description_text,
                created_by_user_id=owner_id,
            )
            session.add(job)
            session.commit()
            return job.job_id
        finally:
            session.close()

    def _seed_completed_analysis(self, job_id):
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

    def _headers(self, user_id):
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def _get_ids(self, path, user_id):
        response = self.client.get(path, headers=self._headers(user_id))
        self.assertEqual(response.status_code, 200, response.text)
        return [item["job_id"] for item in response.json()]


class JobListVisibilityTests(_BaseJobVisibilityTestCase):
    def setUp(self):
        super().setUp()
        self.ownerless_id = self._create_job("ownerless")
        self.a_job_id = self._create_job("owned-by-a", owner_id=self.user_a_id)
        self.b_job_id = self._create_job("owned-by-b", owner_id=self.user_b_id)
        for job_id in (self.ownerless_id, self.a_job_id, self.b_job_id):
            self._seed_completed_analysis(job_id)

    def test_get_jobs_visibility_per_user(self):
        self.assertEqual(
            self._get_ids("/jobs/", self.user_a_id),
            [self.a_job_id, self.ownerless_id],
        )
        self.assertEqual(
            self._get_ids("/jobs/", self.user_b_id),
            [self.b_job_id, self.ownerless_id],
        )
        self.assertEqual(
            self._get_ids("/jobs/", self.admin_id),
            [self.b_job_id, self.a_job_id, self.ownerless_id],
        )

    def test_get_jobs_response_shape_unchanged(self):
        response = self.client.get("/jobs/", headers=self._headers(self.user_a_id))
        self.assertEqual(
            set(response.json()[0].keys()),
            {"job_id", "title", "company_name", "location", "url", "description_text"},
        )

    def test_get_analyzed_jobs_visibility_per_user(self):
        self.assertEqual(
            self._get_ids("/jobs/analyzed", self.user_a_id),
            [self.a_job_id, self.ownerless_id],
        )
        self.assertEqual(
            self._get_ids("/jobs/analyzed", self.user_b_id),
            [self.b_job_id, self.ownerless_id],
        )
        self.assertEqual(
            self._get_ids("/jobs/analyzed", self.admin_id),
            [self.b_job_id, self.a_job_id, self.ownerless_id],
        )


class AnalyzedJobDetailVisibilityTests(_BaseJobVisibilityTestCase):
    def setUp(self):
        super().setUp()
        self.ownerless_id = self._create_job("ownerless")
        self.a_job_id = self._create_job("owned-by-a", owner_id=self.user_a_id)
        self.a_unanalyzed_job_id = self._create_job(
            "owned-by-a-unanalyzed", owner_id=self.user_a_id
        )
        self._seed_completed_analysis(self.ownerless_id)
        self._seed_completed_analysis(self.a_job_id)

    def _get(self, job_id, user_id):
        return self.client.get(
            f"/jobs/analyzed/{job_id}", headers=self._headers(user_id)
        )

    def test_foreign_owned_job_404_is_identical_to_nonexistent(self):
        nonexistent = self._get(NONEXISTENT_JOB_ID, self.user_b_id)
        foreign = self._get(self.a_job_id, self.user_b_id)
        foreign_unanalyzed = self._get(self.a_unanalyzed_job_id, self.user_b_id)

        self.assertEqual(nonexistent.status_code, 404)
        self.assertEqual(nonexistent.json(), {"detail": "Job not found."})
        for response in (foreign, foreign_unanalyzed):
            self.assertEqual(response.status_code, nonexistent.status_code)
            self.assertEqual(response.content, nonexistent.content)
            self.assertEqual(
                response.headers.get("content-type"),
                nonexistent.headers.get("content-type"),
            )

    def test_owner_and_admin_can_read_owned_job(self):
        for user_id in (self.user_a_id, self.admin_id):
            response = self._get(self.a_job_id, user_id)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job_id"], self.a_job_id)

    def test_ownerless_job_visible_to_everyone(self):
        for user_id in (self.user_a_id, self.user_b_id, self.admin_id):
            response = self._get(self.ownerless_id, user_id)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job_id"], self.ownerless_id)

    def test_visible_unanalyzed_job_keeps_existing_message(self):
        for user_id in (self.user_a_id, self.admin_id):
            response = self._get(self.a_unanalyzed_job_id, user_id)
            self.assertEqual(response.status_code, 404)
            self.assertIn(
                "No current completed analysis", response.json()["detail"]
            )


class PaginationAfterVisibilityFilterTests(_BaseJobVisibilityTestCase):
    def setUp(self):
        super().setUp()
        # Interleaved so that post-filtering after offset/limit would give
        # a different page than filtering in SQL.
        self.ownerless_1 = self._create_job("ownerless-1")
        self.b_owned_1 = self._create_job("b-owned-1", owner_id=self.user_b_id)
        self.ownerless_2 = self._create_job("ownerless-2")
        self.b_owned_2 = self._create_job("b-owned-2", owner_id=self.user_b_id)
        self.ownerless_3 = self._create_job("ownerless-3")
        for job_id in (
            self.ownerless_1, self.b_owned_1, self.ownerless_2,
            self.b_owned_2, self.ownerless_3,
        ):
            self._seed_completed_analysis(job_id)

    def test_get_jobs_filter_applied_before_offset_and_limit(self):
        self.assertEqual(
            self._get_ids("/jobs/?limit=2&offset=0", self.user_a_id),
            [self.ownerless_3, self.ownerless_2],
        )
        self.assertEqual(
            self._get_ids("/jobs/?limit=2&offset=2", self.user_a_id),
            [self.ownerless_1],
        )
        self.assertEqual(
            self._get_ids("/jobs/?limit=2&offset=4", self.user_a_id), []
        )
        # Admin still sees the unfiltered ordering.
        self.assertEqual(
            self._get_ids("/jobs/?limit=2&offset=2", self.admin_id),
            [self.ownerless_2, self.b_owned_1],
        )

    def test_get_analyzed_jobs_filter_applied_before_offset_and_limit(self):
        self.assertEqual(
            self._get_ids("/jobs/analyzed?limit=2&offset=0", self.user_a_id),
            [self.ownerless_3, self.ownerless_2],
        )
        self.assertEqual(
            self._get_ids("/jobs/analyzed?limit=2&offset=2", self.user_a_id),
            [self.ownerless_1],
        )
        self.assertEqual(
            self._get_ids("/jobs/analyzed?limit=2&offset=2", self.admin_id),
            [self.ownerless_2, self.b_owned_1],
        )


class AdminBatchSelectionExcludesOwnedJobsTests(_BaseJobVisibilityTestCase):
    def setUp(self):
        super().setUp()
        # Never a real client: the OpenAI seams are patched per test.
        client_patcher = patch.object(jobs, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def _post(self, path):
        return self.client.post(path, headers=self._headers(self.admin_id))

    def test_analyze_missing_selects_only_ownerless_jobs(self):
        ownerless_id = self._create_job("ownerless")
        self._create_job("owned-by-a", owner_id=self.user_a_id)
        self._create_job("owned-by-b", owner_id=self.user_b_id)

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_response(VALID_JOB_ANALYSIS_PAYLOAD),
        ) as mock_openai:
            response = self._post("/jobs/analyze-missing?limit=10")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["selected_job_count"], 1)
        self.assertEqual([r["job_id"] for r in body["results"]], [ownerless_id])
        self.assertEqual(mock_openai.call_count, 1)

    def test_analyze_missing_only_owned_jobs_is_no_op_without_openai(self):
        self._create_job("owned-by-a", owner_id=self.user_a_id)

        with patch.object(jobs, "client", None), \
             patch.dict(os.environ, {}, clear=False), \
             patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            os.environ.pop("OPENAI_API_KEY", None)
            response = self._post("/jobs/analyze-missing?limit=10")

        # No selectable job -> the no-op path runs before
        # _require_openai_client, so a missing key is not a 503 here.
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "no_jobs_to_analyze")
        mock_openai.assert_not_called()

    def test_analyze_sample_automatic_selects_only_ownerless_jobs(self):
        self._create_job("owned-by-a", owner_id=self.user_a_id)
        ownerless_id = self._create_job("ownerless")
        self._create_job("owned-by-b", owner_id=self.user_b_id)

        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            return_value=_mock_response(VALID_JOB_SAMPLE_PAYLOAD),
        ) as mock_openai:
            response = self._post("/jobs/analyze-sample?limit=10")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [r["job_id"] for r in response.json()["results"]], [ownerless_id]
        )
        self.assertEqual(mock_openai.call_count, 1)

    def test_analyze_sample_automatic_only_owned_jobs_is_no_op_without_openai(self):
        self._create_job("owned-by-a", owner_id=self.user_a_id)

        with patch.object(jobs, "client", None), \
             patch.dict(os.environ, {}, clear=False), \
             patch("backend.routers.jobs._create_job_sample_analysis_response") as mock_openai:
            os.environ.pop("OPENAI_API_KEY", None)
            response = self._post("/jobs/analyze-sample?limit=10")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_NO_JOBS")
        mock_openai.assert_not_called()

    def test_analyze_sample_explicit_ids_still_accept_owned_job(self):
        owned_id = self._create_job("owned-by-a", owner_id=self.user_a_id)

        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            return_value=_mock_response(VALID_JOB_SAMPLE_PAYLOAD),
        ) as mock_openai:
            response = self._post(f"/jobs/analyze-sample?job_id_list={owned_id}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [r["job_id"] for r in response.json()["results"]], [owned_id]
        )
        self.assertEqual(mock_openai.call_count, 1)


if __name__ == "__main__":
    unittest.main()
