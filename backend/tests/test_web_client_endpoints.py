"""W4 read endpoints for the web client.

- GET /jobs/?owned=true: only the caller's own jobs (admins included).
- GET /jobs/{job_id}: a job visible to the caller; another user's job gets
  the same 404 as a nonexistent id. /jobs/analyzed and /jobs/fetch-runs
  are still routed to their own handlers.
- GET /users/{user_id}/languages: the user's languages, primary first.
- GET /meta: public; the selected analysis provider and a demo flag.

Self-contained base. Throwaway SQLite and temp directories only;
backend.app.main is never imported, no OpenAI call is made, and
apply101.db / uploads/ are never touched.
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app import config, models, openai_client
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password

# Same synthetic-key import pattern as test_owner_manual_job_analysis.py:
# jobs.py and profiles.py build their OpenAI client at import time; the key
# is synthetic and only set for the import. Nothing here reaches OpenAI.
with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"},
    clear=False,
):
    from backend.routers import jobs, meta, profiles


SYNTHETIC_SECRET = "synthetic-test-secret-for-web-client-endpoints-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"


class _BaseWebClientEndpointsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_web_client_endpoints_test_")
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
        app.include_router(profiles.router)
        app.include_router(jobs.router)
        app.include_router(meta.router)
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

    def _create_job(self, slug, owner_id=None):
        session = self.session_factory()
        try:
            job = models.Job(
                title=f"Synthetic Job {slug}",
                company_name="Synthetic Company",
                location="Synthetic City",
                url=f"https://example.com/job/{slug}",
                description_text="synthetic description",
                source="manual" if owner_id is not None else "arbeitnow",
                created_by_user_id=owner_id,
            )
            session.add(job)
            session.commit()
            return job.job_id
        finally:
            session.close()

    def _add_language(self, user_id, code, name, level=None, scale="CEFR", is_primary=False):
        session = self.session_factory()
        try:
            session.add(models.UserLanguage(
                user_id=user_id,
                language_code=code,
                language_name=name,
                proficiency_level=level,
                proficiency_scale=scale,
                is_primary=is_primary,
            ))
            session.commit()
        finally:
            session.close()

    def _auth_headers(self, user_id):
        session = self.session_factory()
        try:
            token_key = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one().token_key
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    def _nonexistent_job_id(self):
        with self.engine.connect() as connection:
            max_id = connection.execute(
                text("SELECT COALESCE(MAX(job_id), 0) FROM jobs")
            ).scalar()
        return max_id + 1000


class OwnedJobsListTests(_BaseWebClientEndpointsTestCase):
    def setUp(self):
        super().setUp()
        self.user_a = self._create_user("owned.a@example.com")
        self.user_b = self._create_user("owned.b@example.com")
        self.admin = self._create_user("owned.admin@example.com", is_admin=True)
        self.catalog_job = self._create_job("catalog")
        self.job_a = self._create_job("a", owner_id=self.user_a)
        self.job_b = self._create_job("b", owner_id=self.user_b)
        self.job_admin = self._create_job("admin", owner_id=self.admin)

    def _job_ids(self, user_id, query=""):
        response = self.client.get(f"/jobs/{query}", headers=self._auth_headers(user_id))
        self.assertEqual(response.status_code, 200)
        return [job["job_id"] for job in response.json()]

    def test_default_keeps_the_visibility_rules(self):
        self.assertEqual(self._job_ids(self.user_a), [self.job_a, self.catalog_job])
        self.assertEqual(self._job_ids(self.user_a, "?owned=false"), [self.job_a, self.catalog_job])
        self.assertEqual(
            self._job_ids(self.admin),
            [self.job_admin, self.job_b, self.job_a, self.catalog_job],
        )

    def test_owned_returns_only_the_callers_jobs(self):
        self.assertEqual(self._job_ids(self.user_a, "?owned=true"), [self.job_a])
        self.assertEqual(self._job_ids(self.user_b, "?owned=true"), [self.job_b])

    def test_owned_admin_gets_only_own_jobs(self):
        self.assertEqual(self._job_ids(self.admin, "?owned=true"), [self.job_admin])

    def test_owned_with_no_own_jobs_is_empty(self):
        user_c = self._create_user("owned.c@example.com")
        self.assertEqual(self._job_ids(user_c, "?owned=true"), [])

    def test_owned_respects_limit_and_offset(self):
        newer_job_a = self._create_job("a2", owner_id=self.user_a)
        self.assertEqual(self._job_ids(self.user_a, "?owned=true&limit=1"), [newer_job_a])
        self.assertEqual(
            self._job_ids(self.user_a, "?owned=true&limit=1&offset=1"), [self.job_a]
        )

    def test_owned_requires_auth(self):
        self.assertEqual(self.client.get("/jobs/?owned=true").status_code, 401)


class GetJobTests(_BaseWebClientEndpointsTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self._create_user("job.owner@example.com")
        self.other = self._create_user("job.other@example.com")
        self.admin = self._create_user("job.admin@example.com", is_admin=True)
        self.owned_job = self._create_job("owned", owner_id=self.owner)
        self.catalog_job = self._create_job("catalog")

    def _expected_job(self, job_id, slug):
        expected = {
            "job_id": job_id,
            "title": f"Synthetic Job {slug}",
            "company_name": "Synthetic Company",
            "location": "Synthetic City",
            "url": f"https://example.com/job/{slug}",
            "description_text": "synthetic description",
            # Added with the shared pool: a catalog job carries its source's
            # attribution (Arbeitnow requires a link back); personal jobs none.
            "source_label": None,
            "attribution": None,
            "attribution_url": None,
        }
        if slug == "catalog":
            expected.update(
                source_label="Arbeitnow",
                attribution="Job listing from Arbeitnow (arbeitnow.com); apply on the original posting.",
                attribution_url="https://www.arbeitnow.com",
            )
        return expected

    def test_owner_sees_the_job(self):
        response = self.client.get(
            f"/jobs/{self.owned_job}", headers=self._auth_headers(self.owner)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self._expected_job(self.owned_job, "owned"))

    def test_catalog_job_is_visible_to_a_normal_user(self):
        response = self.client.get(
            f"/jobs/{self.catalog_job}", headers=self._auth_headers(self.other)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self._expected_job(self.catalog_job, "catalog"))

    def test_admin_sees_another_users_job(self):
        response = self.client.get(
            f"/jobs/{self.owned_job}", headers=self._auth_headers(self.admin)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self._expected_job(self.owned_job, "owned"))

    def test_other_users_job_and_missing_id_get_identical_404(self):
        headers = self._auth_headers(self.other)
        foreign = self.client.get(f"/jobs/{self.owned_job}", headers=headers)
        missing = self.client.get(f"/jobs/{self._nonexistent_job_id()}", headers=headers)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(foreign.json(), missing.json())
        self.assertEqual(foreign.json(), {"detail": "Job not found."})

    def test_requires_auth(self):
        self.assertEqual(self.client.get(f"/jobs/{self.owned_job}").status_code, 401)

    def test_static_get_paths_still_reach_their_handlers(self):
        user_headers = self._auth_headers(self.owner)
        admin_headers = self._auth_headers(self.admin)

        analyzed = self.client.get("/jobs/analyzed", headers=user_headers)
        self.assertEqual(analyzed.status_code, 200)
        self.assertEqual(analyzed.json(), [])

        analyzed_one = self.client.get(f"/jobs/analyzed/{self.owned_job}", headers=user_headers)
        self.assertEqual(analyzed_one.status_code, 404)
        self.assertIn("No current completed analysis", analyzed_one.json()["detail"])

        # Admin-only: a normal user gets 403, not the job lookup's 422/404.
        self.assertEqual(
            self.client.get("/jobs/fetch-runs", headers=user_headers).status_code, 403
        )
        fetch_runs = self.client.get("/jobs/fetch-runs", headers=admin_headers)
        self.assertEqual(fetch_runs.status_code, 200)
        self.assertEqual(fetch_runs.json(), [])


class UserLanguagesTests(_BaseWebClientEndpointsTestCase):
    def setUp(self):
        super().setUp()
        self.user = self._create_user("lang.user@example.com")
        self.other = self._create_user("lang.other@example.com")
        self.admin = self._create_user("lang.admin@example.com", is_admin=True)

    def test_own_languages_primary_first_then_by_name(self):
        self._add_language(self.user, "es", "Spanish", "A2")
        self._add_language(self.user, "en", "English", "C1", is_primary=True)
        self._add_language(self.user, "de", "German", None, scale="ILR")
        self._add_language(self.other, "fr", "French", "B2", is_primary=True)

        response = self.client.get(
            f"/users/{self.user}/languages", headers=self._auth_headers(self.user)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"language_code": "en", "language_name": "English", "proficiency_level": "C1",
             "proficiency_scale": "CEFR", "is_primary": True},
            {"language_code": "de", "language_name": "German", "proficiency_level": None,
             "proficiency_scale": "ILR", "is_primary": False},
            {"language_code": "es", "language_name": "Spanish", "proficiency_level": "A2",
             "proficiency_scale": "CEFR", "is_primary": False},
        ])

    def test_no_languages_is_an_empty_list(self):
        response = self.client.get(
            f"/users/{self.user}/languages", headers=self._auth_headers(self.user)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_another_user_gets_the_require_user_access_denial(self):
        self._add_language(self.user, "en", "English", "C1", is_primary=True)
        headers = self._auth_headers(self.other)

        languages = self.client.get(f"/users/{self.user}/languages", headers=headers)
        profiles_route = self.client.get(f"/users/{self.user}/profiles", headers=headers)

        self.assertEqual(languages.status_code, 404)
        self.assertEqual(languages.json(), {"detail": "User not found."})
        self.assertEqual(languages.json(), profiles_route.json())

    def test_admin_sees_another_users_languages(self):
        self._add_language(self.user, "en", "English", "C1", is_primary=True)
        response = self.client.get(
            f"/users/{self.user}/languages", headers=self._auth_headers(self.admin)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["language_code"] for item in response.json()], ["en"])

    def test_languages_written_by_profile_create_are_returned(self):
        payload = {
            "languages": [
                {"language_code": "tr", "language_name": "Turkish",
                 "proficiency_level": "C2", "is_primary": True},
                {"language_code": "en", "language_name": "English",
                 "proficiency_level": "B2"},
            ],
        }
        headers = self._auth_headers(self.user)
        created = self.client.post(f"/users/{self.user}/profiles", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200)

        response = self.client.get(f"/users/{self.user}/languages", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"language_code": "tr", "language_name": "Turkish", "proficiency_level": "C2",
             "proficiency_scale": "CEFR", "is_primary": True},
            {"language_code": "en", "language_name": "English", "proficiency_level": "B2",
             "proficiency_scale": "CEFR", "is_primary": False},
        ])

    def test_requires_auth(self):
        self.assertEqual(self.client.get(f"/users/{self.user}/languages").status_code, 401)


class MetaTests(_BaseWebClientEndpointsTestCase):
    def _provider_env(self, **values):
        names = (openai_client.ANALYSIS_PROVIDER_ENV, config.DATABASE_URL_ENV, config.UPLOAD_DIR_ENV)
        env = {name: v for name, v in os.environ.items() if name not in names}
        env.update(values)
        return patch.dict(os.environ, env, clear=True)

    def test_default_provider_is_openai_and_not_demo(self):
        with self._provider_env():
            response = self.client.get("/meta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"analysis_provider": "openai", "demo": False})

    def test_fake_provider_is_demo(self):
        demo_db = os.path.join(self.tmp_dir, "demo.db").replace("\\", "/")
        demo_uploads = os.path.join(self.tmp_dir, "demo_uploads")
        with self._provider_env(
            APPLY101_DATABASE_URL=f"sqlite:///{demo_db}",
            APPLY101_UPLOAD_DIR=demo_uploads,
            APPLY101_ANALYSIS_PROVIDER="fake",
        ):
            response = self.client.get("/meta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"analysis_provider": "fake", "demo": True})
        self.assertFalse(os.path.exists(demo_db))
        self.assertFalse(os.path.exists(demo_uploads))

    def test_needs_no_auth_header(self):
        with self._provider_env():
            response = self.client.get("/meta", headers={"Authorization": "Bearer not-a-token"})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
