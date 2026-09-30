"""DELETE /users/{user_id} removes the jobs that user created and every row
depending on them (matches from any profile, job analyses, per-job analysis
guards), without touching ownerless jobs or other users' jobs. SQLite may
reuse the highest user_id, so a new user with the same id must inherit
nothing.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    MATCH_MODEL,
    MATCH_VERSION,
)
from backend.app.analysis_guard import (
    JOB_BATCH_OPERATION_TYPE,
    JOB_BATCH_RESOURCE_ID,
    JOB_OPERATION_TYPE,
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
)
from backend.app.database import Base, get_db
from backend.app.security import create_access_token

# Same import-time pattern as the other router test modules: a synthetic
# key only while importing, restored immediately afterward. No test here
# reaches OpenAI (only DELETE /users and read-only GET /jobs are called).
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs, users


SYNTHETIC_SECRET = "synthetic-test-secret-for-user-delete-cleanup-0123456789"

EXPECTED_DELETE_RESPONSE_KEYS = {
    "status",
    "user_id",
    "deleted_profiles_count",
    "deleted_profile_analyses_count",
    "deleted_matches_count",
    "deleted_languages_count",
}


class _BaseUserDeleteTestCase(unittest.TestCase):
    """Throwaway synthetic SQLite database (never apply101.db), only
    users.router + jobs.router mounted, synthetic JWT secret."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_user_delete_cleanup_test_")
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
        app.include_router(users.router)
        app.include_router(jobs.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _add(self, obj):
        session = self.session_factory()
        try:
            session.add(obj)
            session.commit()
            session.refresh(obj)
            return obj
        finally:
            session.close()

    def _create_user(self, mail, user_id=None, is_admin=False):
        return self._add(models.User(
            user_id=user_id, name="Synthetic User", mail=mail, is_admin=is_admin
        )).user_id

    def _create_profile(self, user_id):
        return self._add(
            models.CandidateProfile(user_id=user_id, self_description="synthetic")
        ).profile_id

    def _create_profile_analysis(self, profile_id):
        return self._add(models.ProfileAnalysis(
            profile_id=profile_id,
            analysis_status="completed",
            is_current=True,
        )).analysis_id

    def _create_job(self, slug, owner_id=None):
        job_id = self._add(models.Job(
            title=f"Synthetic Job {slug}",
            url=f"https://example.com/job/{slug}",
            description_text="synthetic description",
            created_by_user_id=owner_id,
        )).job_id
        analysis_id = self._add(models.JobAnalysis(
            job_id=job_id,
            analysis_status="completed",
            analysis_json=json.dumps({"hard_requirements": {}}),
            role_tags_json='["other"]',
            analysis_model=JOB_ANALYSIS_MODEL,
            analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
            is_current=True,
        )).analysis_id
        return job_id, analysis_id

    def _create_match(self, profile_id, profile_analysis_id, job_id, job_analysis_id):
        return self._add(models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis_id,
            job_analysis_id=job_analysis_id,
            match_status="completed",
            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_VERSION,
            is_current=True,
        )).match_id

    def _create_guard(self, operation_type, resource_id):
        self._add(models.AnalysisGuard(
            operation_type=operation_type,
            resource_id=resource_id,
            owner_token=None,
            lock_expires_at=None,
            cooldown_until=1.0,
        ))

    def _headers(self, user_id):
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def _ids(self, model, column):
        session = self.session_factory()
        try:
            return {value for (value,) in session.query(column).all()}
        finally:
            session.close()

    def _guard_keys(self):
        session = self.session_factory()
        try:
            return {
                (row.operation_type, row.resource_id)
                for row in session.query(models.AnalysisGuard).all()
            }
        finally:
            session.close()


class UserDeleteOwnedJobCleanupTests(_BaseUserDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.user_a_id = self._create_user("cleanup.a@example.com")
        self.user_b_id = self._create_user("cleanup.b@example.com")

        self.profile_a = self._create_profile(self.user_a_id)
        self.profile_b = self._create_profile(self.user_b_id)
        self.pa_a = self._create_profile_analysis(self.profile_a)
        self.pa_b = self._create_profile_analysis(self.profile_b)

        self.ownerless_job, self.ownerless_analysis = self._create_job("ownerless")
        self.a_job_1, self.a_job_1_analysis = self._create_job("a-1", self.user_a_id)
        self.a_job_2, self.a_job_2_analysis = self._create_job("a-2", self.user_a_id)
        self.b_job, self.b_job_analysis = self._create_job("b-1", self.user_b_id)

        # A's profile against A's job and the ownerless job.
        self.m_a_a1 = self._create_match(
            self.profile_a, self.pa_a, self.a_job_1, self.a_job_1_analysis
        )
        self.m_a_ownerless = self._create_match(
            self.profile_a, self.pa_a, self.ownerless_job, self.ownerless_analysis
        )
        # Old data: B's profile matched against A's private job.
        self.m_b_a1 = self._create_match(
            self.profile_b, self.pa_b, self.a_job_1, self.a_job_1_analysis
        )
        self.m_b_a2 = self._create_match(
            self.profile_b, self.pa_b, self.a_job_2, self.a_job_2_analysis
        )
        # Rows that must survive.
        self.m_b_b = self._create_match(
            self.profile_b, self.pa_b, self.b_job, self.b_job_analysis
        )
        self.m_b_ownerless = self._create_match(
            self.profile_b, self.pa_b, self.ownerless_job, self.ownerless_analysis
        )

        for job_id in (self.ownerless_job, self.a_job_1, self.a_job_2, self.b_job):
            self._create_guard(JOB_OPERATION_TYPE, job_id)
        self._create_guard(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID)
        self._create_guard(PROFILE_OPERATION_TYPE, self.profile_b)
        self._create_guard(USER_OPERATION_TYPE, self.user_b_id)

    def _delete_a(self):
        response = self.client.delete(
            f"/users/{self.user_a_id}", headers=self._headers(self.user_a_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_deletes_owned_jobs_and_dependents_only(self):
        body = self._delete_a()

        # Response shape and existing count semantics are unchanged: the
        # match count still reports matches of the deleted user's profiles.
        self.assertEqual(set(body.keys()), EXPECTED_DELETE_RESPONSE_KEYS)
        self.assertEqual(body["status"], "deleted")
        self.assertEqual(body["deleted_profiles_count"], 1)
        self.assertEqual(body["deleted_matches_count"], 2)

        self.assertEqual(
            self._ids(models.Job, models.Job.job_id),
            {self.ownerless_job, self.b_job},
        )
        self.assertEqual(
            self._ids(models.JobAnalysis, models.JobAnalysis.analysis_id),
            {self.ownerless_analysis, self.b_job_analysis},
        )
        self.assertEqual(
            self._ids(models.JobMatch, models.JobMatch.match_id),
            {self.m_b_b, self.m_b_ownerless},
        )
        self.assertEqual(
            self._guard_keys(),
            {
                (JOB_OPERATION_TYPE, self.ownerless_job),
                (JOB_OPERATION_TYPE, self.b_job),
                (JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID),
                (PROFILE_OPERATION_TYPE, self.profile_b),
                (USER_OPERATION_TYPE, self.user_b_id),
            },
        )
        self.assertEqual(
            self._ids(models.User, models.User.user_id), {self.user_b_id}
        )

    def test_no_job_keeps_deleted_owner_id(self):
        self._delete_a()
        session = self.session_factory()
        try:
            remaining = session.query(models.Job).filter(
                models.Job.created_by_user_id == self.user_a_id
            ).count()
        finally:
            session.close()
        self.assertEqual(remaining, 0)

    def test_new_user_reusing_deleted_id_inherits_no_jobs(self):
        self._delete_a()

        reused_id = self._create_user(
            "cleanup.reused@example.com", user_id=self.user_a_id
        )
        self.assertEqual(reused_id, self.user_a_id)

        headers = self._headers(reused_id)
        listed = self.client.get("/jobs/", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["job_id"] for item in listed.json()], [self.ownerless_job]
        )

        analyzed = self.client.get("/jobs/analyzed", headers=headers)
        self.assertEqual(analyzed.status_code, 200)
        self.assertEqual(
            [item["job_id"] for item in analyzed.json()], [self.ownerless_job]
        )

        for job_id in (self.a_job_1, self.a_job_2):
            detail = self.client.get(f"/jobs/analyzed/{job_id}", headers=headers)
            self.assertEqual(detail.status_code, 404)

    def test_other_user_still_sees_own_and_ownerless_jobs(self):
        self._delete_a()
        listed = self.client.get("/jobs/", headers=self._headers(self.user_b_id))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            sorted(item["job_id"] for item in listed.json()),
            sorted([self.ownerless_job, self.b_job]),
        )


class UserDeleteWithoutJobsTests(_BaseUserDeleteTestCase):
    def test_user_without_jobs_is_deleted_as_before(self):
        user_id = self._create_user("cleanup.nojobs@example.com")
        other_id = self._create_user("cleanup.nojobs.other@example.com")
        profile_id = self._create_profile(user_id)
        pa_id = self._create_profile_analysis(profile_id)
        ownerless_job, ownerless_analysis = self._create_job("ownerless")
        other_job, other_analysis = self._create_job("other", other_id)
        self._create_match(profile_id, pa_id, ownerless_job, ownerless_analysis)
        self._create_guard(JOB_OPERATION_TYPE, ownerless_job)
        self._create_guard(JOB_OPERATION_TYPE, other_job)

        response = self.client.delete(
            f"/users/{user_id}", headers=self._headers(user_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "status": "deleted",
                "user_id": user_id,
                "deleted_profiles_count": 1,
                "deleted_profile_analyses_count": 1,
                "deleted_matches_count": 1,
                "deleted_languages_count": 0,
            },
        )
        self.assertEqual(
            self._ids(models.Job, models.Job.job_id), {ownerless_job, other_job}
        )
        self.assertEqual(
            self._ids(models.JobAnalysis, models.JobAnalysis.analysis_id),
            {ownerless_analysis, other_analysis},
        )
        self.assertEqual(self._ids(models.JobMatch, models.JobMatch.match_id), set())
        self.assertEqual(
            self._guard_keys(),
            {(JOB_OPERATION_TYPE, ownerless_job), (JOB_OPERATION_TYPE, other_job)},
        )


if __name__ == "__main__":
    unittest.main()
