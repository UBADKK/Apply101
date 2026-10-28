"""A job analysis that runs while a catalog sync changes the job's text is
not written (409), so an analysis never describes text it was not computed
from. Unchanged text still writes the analysis. Throwaway SQLite, the
deterministic fake provider, no network."""

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
from backend.app.database import Base, get_db
from backend.app.fake_analysis import FakeAnalysisClient
from backend.app.security import create_access_token, hash_password

with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"}, clear=False):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-content-change-0123456789abcdef"


class _MutatingResponses:
    def __init__(self, inner, on_call):
        self.inner = inner
        self.on_call = on_call

    def create(self, **kwargs):
        self.on_call()
        return self.inner.create(**kwargs)


class _MutatingClient:
    """FakeAnalysisClient whose provider call first runs on_call (standing
    in for a concurrent catalog sync)."""

    def __init__(self, on_call):
        self.fake = FakeAnalysisClient()
        self.on_call = on_call

    def with_options(self, **kwargs):
        inner = self.fake.with_options(**kwargs)
        wrapper = type("Wrapped", (), {})()
        wrapper.responses = _MutatingResponses(inner.responses, self.on_call)
        return wrapper


class JobAnalysisContentChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_content_change_test_")
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(jobs.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        env = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False)
        env.start()
        self.addCleanup(env.stop)

        db = self.Session()
        admin = models.User(name="A", mail="admin@example.com", password_hash=hash_password("a-valid-synthetic-password"), is_admin=True)
        db.add(admin)
        job = models.Job(title="Backend Engineer", company_name="Acme", location="Berlin",
                         url="https://example.test/j/1", description_text="Python and FastAPI.",
                         source="greenhouse", job_status="active")
        db.add(job)
        db.commit()
        self.headers = {"Authorization": f"Bearer {create_access_token(admin.user_id, admin.token_key)}"}
        self.job_id = job.job_id
        db.close()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _change_text(self):
        db = self.Session()
        try:
            job = db.get(models.Job, self.job_id)
            job.description_text = "Python, FastAPI and German C1."
            db.commit()
        finally:
            db.close()

    def test_text_changed_during_analysis_is_not_written(self):
        with patch.object(jobs, "client", _MutatingClient(self._change_text)):
            response = self.client.post(f"/jobs/{self.job_id}/analyze", headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_JOB_CHANGED_DURING_ANALYSIS")
        db = self.Session()
        try:
            self.assertEqual(db.query(models.JobAnalysis).filter_by(job_id=self.job_id).count(), 0)
        finally:
            db.close()

    def test_unchanged_text_writes_the_analysis(self):
        with patch.object(jobs, "client", _MutatingClient(lambda: None)):
            response = self.client.post(f"/jobs/{self.job_id}/analyze", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")


if __name__ == "__main__":
    unittest.main()


class MatchWhileAnalysisChangesTests(unittest.TestCase):
    """A match computed while a catalog sync marks the job analysis not
    current is not written (409)."""

    def setUp(self):
        from backend.routers import matches, profiles

        self.matches = matches
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_match_change_test_")
        self.engine = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        for router in (jobs.router, profiles.router, matches.router):
            app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        env = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for module in (jobs, profiles):
            patcher = patch.object(module, "client", FakeAnalysisClient())
            patcher.start()
            self.addCleanup(patcher.stop)

        db = self.Session()
        admin = models.User(name="A", mail="admin@example.com", password_hash=hash_password("a-valid-synthetic-password"), is_admin=True)
        user = models.User(name="U", mail="user@example.com", password_hash=hash_password("a-valid-synthetic-password"))
        job = models.Job(title="Backend Developer", company_name="Acme", location="Berlin", url="https://example.test/j/1",
                         description_text="Build APIs with Python and SQL.", source="greenhouse", job_status="active")
        db.add_all([admin, user, job])
        db.commit()
        self.admin = {"Authorization": f"Bearer {create_access_token(admin.user_id, admin.token_key)}"}
        self.user = {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}
        self.user_id, self.job_id = user.user_id, job.job_id
        db.close()
        self.assertEqual(self.client.post(f"/jobs/{self.job_id}/analyze", headers=self.admin).status_code, 200)
        profile = self.client.post(f"/users/{self.user_id}/profiles", headers=self.user, json={"target_role": "Backend Developer", "years_of_experience": 3})
        self.profile_id = profile.json()["profile_id"]
        self.assertEqual(self.client.post(f"/users/{self.user_id}/profiles/{self.profile_id}/analyze", headers=self.user).status_code, 200)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_match_is_not_written_when_job_analysis_becomes_stale(self):
        original = self.matches.calculate_backend_match

        def compute_then_sync(**kwargs):
            result = original(**kwargs)
            db = self.Session()
            try:
                db.query(models.JobAnalysis).update({models.JobAnalysis.is_current: False})
                db.commit()
            finally:
                db.close()
            return result

        with patch.object(self.matches, "calculate_backend_match", compute_then_sync):
            response = self.client.post(f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{self.job_id}/match", headers=self.user)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_JOB_ANALYSIS_CHANGED")
        db = self.Session()
        try:
            self.assertEqual(db.query(models.JobMatch).count(), 0)
        finally:
            db.close()
