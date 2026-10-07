"""Arbeitnow fetch dedup is scoped to ownerless catalog jobs only.

POST /jobs/fetch and POST /jobs/fetch-pages must look up an existing job
for an Arbeitnow url only among catalog jobs (created_by_user_id IS NULL).
A personal job that happens to share the url must never be updated, counted
as a duplicate, or block the creation of the catalog row (the two partial
unique indexes on jobs.url allow one catalog row plus personal rows).

Uses a throwaway temp-dir SQLite database (never apply101.db), mounts only
jobs.router, and patches requests.get / time.sleep so no network is used.
"""

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

# jobs.py may construct an OpenAI client at import time; the key is synthetic
# and only present for the duration of the import. No OpenAI call is made.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-fetch-catalog-scope-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"

SHARED_URL = "https://example.com/job/shared-url-1"
PAYLOAD_CREATED_AT = 1700000000
EXISTING_SOURCE_CREATED_AT = 1600000000

FETCH_RESPONSE_KEYS = {
    "saved_count",
    "skipped_count",
    "jobs_seen_count",
    "sample_saved_jobs",
    "sample_skipped_jobs",
}

FETCH_PAGES_RESPONSE_KEYS = {
    "fetch_run_id",
    "alljobs",
    "maxpage",
    "thispage",
    "pages_checked",
    "last_checked_page",
    "stopped_reason",
    "error_message",
    "jobs_seen_count",
    "saved_count",
    "skipped_count",
    "sample_saved_jobs",
    "sample_skipped_jobs",
}


def _arbeitnow_item(url=SHARED_URL):
    return {
        "url": url,
        "title": "Fetched Synthetic Title",
        "company_name": "Fetched Synthetic Co",
        "location": "Berlin",
        "description": "<p>some text</p>",
        "created_at": PAYLOAD_CREATED_AT,
    }


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


class FetchCatalogScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_fetch_scope_test_")
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

        self.admin_id = self._create_user("fetch.scope.admin@example.com", is_admin=True)
        self.owner_id = self._create_user("fetch.scope.owner@example.com", is_admin=False)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

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
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _auth_headers(self, user_id):
        session = self.session_factory()
        try:
            user = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one()
            token_key = user.token_key
        finally:
            session.close()
        token = create_access_token(user_id, token_key)
        return {"Authorization": f"Bearer {token}"}

    def _create_job(self, owner_id, title, source_created_at=None):
        session = self.session_factory()
        try:
            job = models.Job(
                title=title,
                company_name="Fixed Synthetic Co",
                location="Fixed Location",
                url=SHARED_URL,
                description_text=f"fixed description for {title}",
                source_created_at=source_created_at,
                last_seen_at=None,
                created_by_user_id=owner_id,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return job.job_id
        finally:
            session.close()

    def _create_personal_job(self):
        return self._create_job(self.owner_id, "Personal Synthetic Job")

    def _create_catalog_job(self, source_created_at=None):
        return self._create_job(
            None, "Catalog Synthetic Job", source_created_at=source_created_at
        )

    def _snapshot(self, job_id):
        session = self.session_factory()
        try:
            job = session.query(models.Job).filter(
                models.Job.job_id == job_id
            ).one()
            return {
                column.name: getattr(job, column.key)
                for column in models.Job.__table__.columns
            }
        finally:
            session.close()

    def _rows_for_url(self):
        session = self.session_factory()
        try:
            return [
                (job.job_id, job.created_by_user_id, job.source)
                for job in session.query(models.Job)
                .filter(models.Job.url == SHARED_URL)
                .order_by(models.Job.job_id)
                .all()
            ]
        finally:
            session.close()

    def _fetch_run(self, run_id):
        session = self.session_factory()
        try:
            run = session.query(models.JobFetchRun).filter(
                models.JobFetchRun.run_id == run_id
            ).one()
            return {
                "new_jobs_count": run.new_jobs_count,
                "duplicate_jobs_count": run.duplicate_jobs_count,
            }
        finally:
            session.close()

    def _post_fetch(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": [_arbeitnow_item()]}

        with patch(
            "backend.routers.jobs.requests.get", return_value=mock_response
        ) as mock_get:
            response = self.client.post(
                "/jobs/fetch", headers=self._auth_headers(self.admin_id)
            )

        mock_get.assert_called_once()
        return response

    def _post_fetch_pages(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [_arbeitnow_item()]}

        with patch(
            "backend.routers.jobs.requests.get", return_value=mock_response
        ) as mock_get, patch("backend.routers.jobs.time.sleep") as mock_sleep:
            response = self.client.post(
                "/jobs/fetch-pages?alljobs=false&thispage=1",
                headers=self._auth_headers(self.admin_id),
            )

        mock_get.assert_called_once()
        mock_sleep.assert_called_once()
        self.assertEqual(response.json()["stopped_reason"], "REQUESTED_PAGES_COMPLETED")
        return response

    # ---- scenario A: only a personal job shares the url ------------------

    def _assert_scenario_a(self, post, expected_keys):
        personal_id = self._create_personal_job()
        before = self._snapshot(personal_id)

        response = post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body.keys()), expected_keys)
        self.assertEqual(body["saved_count"], 1)
        self.assertEqual(body["skipped_count"], 0)
        self.assertEqual(body["sample_skipped_jobs"], [])

        rows = self._rows_for_url()
        self.assertEqual(len(rows), 2, rows)
        new_job_id, new_owner, new_source = rows[1]
        self.assertGreater(new_job_id, personal_id)
        self.assertIsNone(new_owner)
        self.assertEqual(new_source, "arbeitnow")

        self.assertEqual(self._snapshot(personal_id), before)
        return body

    def test_fetch_personal_only_creates_catalog_row(self):
        self._assert_scenario_a(self._post_fetch, FETCH_RESPONSE_KEYS)

    def test_fetch_pages_personal_only_creates_catalog_row(self):
        body = self._assert_scenario_a(self._post_fetch_pages, FETCH_PAGES_RESPONSE_KEYS)
        self.assertEqual(
            self._fetch_run(body["fetch_run_id"]),
            {"new_jobs_count": 1, "duplicate_jobs_count": 0},
        )

    # ---- scenario B: personal (lower id) and catalog share the url -------

    def _assert_scenario_b(self, post):
        personal_id = self._create_personal_job()
        catalog_id = self._create_catalog_job(source_created_at=None)
        self.assertLess(personal_id, catalog_id)
        personal_before = self._snapshot(personal_id)
        self.assertIsNone(self._snapshot(catalog_id)["last_seen_at"])

        response = post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["saved_count"], 0)
        self.assertEqual(body["skipped_count"], 1)
        self.assertEqual(body["sample_skipped_jobs"][0]["reason"], "DUPLICATE_IN_DB")

        rows = self._rows_for_url()
        self.assertEqual([row[0] for row in rows], [personal_id, catalog_id])

        catalog_after = self._snapshot(catalog_id)
        self.assertIsNotNone(catalog_after["last_seen_at"])
        self.assertEqual(catalog_after["source_created_at"], PAYLOAD_CREATED_AT)

        self.assertEqual(self._snapshot(personal_id), personal_before)
        return body

    def test_fetch_personal_and_catalog_updates_only_catalog(self):
        self._assert_scenario_b(self._post_fetch)

    def test_fetch_pages_personal_and_catalog_updates_only_catalog(self):
        body = self._assert_scenario_b(self._post_fetch_pages)
        self.assertEqual(
            self._fetch_run(body["fetch_run_id"]),
            {"new_jobs_count": 0, "duplicate_jobs_count": 1},
        )

    # ---- scenario C: only a catalog job has the url ----------------------

    def _assert_scenario_c(self, post, expected_keys):
        catalog_id = self._create_catalog_job(
            source_created_at=EXISTING_SOURCE_CREATED_AT
        )

        response = post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body.keys()), expected_keys)
        self.assertEqual(body["saved_count"], 0)
        self.assertEqual(body["skipped_count"], 1)
        self.assertEqual(body["sample_skipped_jobs"][0]["reason"], "DUPLICATE_IN_DB")

        rows = self._rows_for_url()
        self.assertEqual([row[0] for row in rows], [catalog_id])

        catalog_after = self._snapshot(catalog_id)
        self.assertIsNotNone(catalog_after["last_seen_at"])
        self.assertEqual(catalog_after["source_created_at"], EXISTING_SOURCE_CREATED_AT)
        return body

    def test_fetch_catalog_only_is_duplicate(self):
        self._assert_scenario_c(self._post_fetch, FETCH_RESPONSE_KEYS)

    def test_fetch_pages_catalog_only_is_duplicate(self):
        body = self._assert_scenario_c(self._post_fetch_pages, FETCH_PAGES_RESPONSE_KEYS)
        self.assertEqual(
            self._fetch_run(body["fetch_run_id"]),
            {"new_jobs_count": 0, "duplicate_jobs_count": 1},
        )


if __name__ == "__main__":
    unittest.main()
