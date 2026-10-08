"""POST /jobs/manual: an authenticated user adds a private job by hand.

- Any authenticated user (admins too) may create one; the owner is always
  the token user and source is always "manual".
- The url must be a real http(s) url and is stored exactly as submitted.
- The same user posting the same url again gets a 409 that reveals no job
  id; a catalog job or another user's job with the same url does not block.
- No OpenAI call and no analysis happen during creation.

Uses a throwaway temp-dir SQLite database (never apply101.db), mounts only
jobs.router, and never imports backend.app.main.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app import models, schemas
from backend.app.database import Base, get_db
from backend.app.security import create_access_token

# jobs.py may construct an OpenAI client at import time; the key is synthetic
# and only present for the duration of the import. No OpenAI call is made.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-manual-jobs-tests-0123456789"

MANUAL_PATH = "/jobs/manual"
JOB_RESPONSE_KEYS = {
    "job_id", "title", "company_name", "location", "url", "description_text",
}
DUPLICATE_DETAIL = {
    "error_code": "ERR_JOB_URL_ALREADY_EXISTS",
    "message": "You already have a job with this URL.",
}

VALID_DESCRIPTION = (
    "Synthetic manual job description used only by automated tests. "
    "It is long enough to pass validation."
)
VALID_URL = "https://example.com/jobs/manual-1"


def _valid_body(**overrides):
    body = {
        "title": "Synthetic Manual Job",
        "company_name": "Synthetic Co",
        "location": "Berlin",
        "url": VALID_URL,
        "description_text": VALID_DESCRIPTION,
    }
    body.update(overrides)
    return body


def _naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _BaseManualJobTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_manual_jobs_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        # create_all on a fresh database creates the partial unique indexes
        # uq_jobs_catalog_url / uq_jobs_owner_url.
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

        self.user_a_id = self._create_user("manual.a@example.com")
        self.user_b_id = self._create_user("manual.b@example.com")
        self.admin_id = self._create_user("manual.admin@example.com", is_admin=True)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

    def _create_user(self, mail, is_admin=False):
        session = self.session_factory()
        try:
            user = models.User(name="Synthetic User", mail=mail, is_admin=is_admin)
            session.add(user)
            session.commit()
            return user.user_id
        finally:
            session.close()

    def _headers(self, user_id):
        session = self.session_factory()
        try:
            token_key = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one().token_key
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    def _insert_job(self, url, owner_id=None, title="Pre-existing Synthetic Job"):
        session = self.session_factory()
        try:
            job = models.Job(
                title=title,
                url=url,
                description_text="pre-existing synthetic description",
                source="fixture",
                created_by_user_id=owner_id,
            )
            session.add(job)
            session.commit()
            return job.job_id
        finally:
            session.close()

    def _count(self, model):
        session = self.session_factory()
        try:
            return session.query(model).count()
        finally:
            session.close()

    def _job_count(self):
        return self._count(models.Job)

    def _get_job(self, job_id):
        session = self.session_factory()
        try:
            job = session.query(models.Job).filter(
                models.Job.job_id == job_id
            ).one()
            session.expunge(job)
            return job
        finally:
            session.close()

    def _row_snapshot(self, job_id):
        job = self._get_job(job_id)
        return {
            column.name: getattr(job, column.name)
            for column in models.Job.__table__.columns
        }

    def _post(self, body, user_id=None, headers=None):
        if headers is None and user_id is not None:
            headers = self._headers(user_id)
        return self.client.post(MANUAL_PATH, json=body, headers=headers or {})


class ManualJobAuthTests(_BaseManualJobTestCase):
    def test_missing_token_is_401_and_writes_nothing(self):
        response = self._post(_valid_body())
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self._job_count(), 0)

    def test_invalid_token_is_401_and_writes_nothing(self):
        response = self._post(
            _valid_body(),
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json(), {"detail": "Could not validate credentials."})
        self.assertEqual(self._job_count(), 0)


class ManualJobValidationTests(_BaseManualJobTestCase):
    def _assert_rejected(self, body):
        before = self._job_count()
        response = self._post(body, user_id=self.user_a_id)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._job_count(), before)

    def test_missing_required_fields(self):
        for field in ("title", "url", "description_text"):
            with self.subTest(field=field):
                body = _valid_body()
                del body[field]
                self._assert_rejected(body)

    def test_title_and_description_lengths(self):
        cases = {
            "whitespace_title": _valid_body(title="   \t "),
            "empty_title": _valid_body(title=""),
            "title_301": _valid_body(title="t" * 301),
            "whitespace_description": _valid_body(description_text=" " * 80),
            "description_49": _valid_body(description_text="d" * 49),
            "description_49_padded": _valid_body(
                description_text="   " + "d" * 49 + "   "
            ),
            "description_12001": _valid_body(description_text="d" * 12001),
            "company_201": _valid_body(company_name="c" * 201),
            "location_201": _valid_body(location="l" * 201),
            "company_201_padded_inner": _valid_body(
                company_name="  " + "c" * 201 + "  "
            ),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self._assert_rejected(body)

    def test_invalid_urls(self):
        cases = {
            "ftp": "ftp://example.com/x",
            "javascript": "javascript:alert(1)",
            "no_scheme": "example.com/job",
            "no_host": "https:///x",
            "broken_ipv6": "http://[::1",
            "inner_space": "https://example.com/a b",
            "tab": "https://example.com/a\tb",
            "newline": "https://example.com/a\nb",
            "carriage_return": "https://example.com/a\rb",
            "nul": "https://example.com/a\x00b",
            "del": "https://example.com/a\x7fb",
            "leading_space": " https://example.com/x",
            "trailing_space": "https://example.com/x ",
            "too_long": "https://example.com/" + "a" * (2049 - len("https://example.com/")),
            "empty": "",
        }
        self.assertEqual(len(cases["too_long"]), 2049)
        for name, url in cases.items():
            with self.subTest(case=name):
                self._assert_rejected(_valid_body(url=url))

    def test_invalid_ports_are_rejected(self):
        cases = {
            "non_numeric_port": "http://example.com:abc/",
            "out_of_range_port": "https://example.com:99999/",
        }
        for name, url in cases.items():
            with self.subTest(case=name):
                self._assert_rejected(_valid_body(url=url))
        self.assertEqual(self._job_count(), 0)

    def test_valid_port_is_accepted_and_stored_unchanged(self):
        url = "https://example.com:8443/jobs/with-port"
        response = self._post(_valid_body(url=url), user_id=self.user_a_id)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["url"], url)
        self.assertEqual(self._get_job(response.json()["job_id"]).url, url)

    def test_url_must_be_a_string(self):
        self._assert_rejected(_valid_body(url=12345))

    def test_extra_keys_are_rejected(self):
        cases = {
            "created_by_user_id": self.user_b_id,
            "source": "arbeitnow",
            "job_id": 99999,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self._assert_rejected(_valid_body(**{key: value}))

    def test_boundary_values_are_accepted(self):
        url_2048 = "https://example.com/" + "a" * (2048 - len("https://example.com/"))
        self.assertEqual(len(url_2048), 2048)
        cases = [
            _valid_body(title="t" * 300, url="https://example.com/b1"),
            _valid_body(description_text="d" * 50, url="https://example.com/b2"),
            _valid_body(description_text="d" * 12000, url="https://example.com/b3"),
            _valid_body(company_name="c" * 200, location="l" * 200,
                        url="https://example.com/b4"),
            _valid_body(url=url_2048),
            _valid_body(url="http://example.com/plain-http"),
        ]
        for index, body in enumerate(cases):
            with self.subTest(case=index):
                response = self._post(body, user_id=self.user_a_id)
                self.assertEqual(response.status_code, 201, response.text)
                job = self._get_job(response.json()["job_id"])
                self.assertEqual(job.url, body["url"])
        self.assertEqual(self._job_count(), len(cases))


class ManualJobCreateTests(_BaseManualJobTestCase):
    def test_creates_owned_manual_job(self):
        raw_url = "HTTPS://Example.COM/Path?q=1#frag"
        body = _valid_body(
            title="  Backend Engineer  ",
            company_name="  Synthetic GmbH ",
            location="\tBerlin\n",
            url=raw_url,
            description_text="\n  " + VALID_DESCRIPTION + "  \n",
        )

        before = _naive_utc_now()
        response = self._post(body, user_id=self.user_a_id)
        after = _naive_utc_now()

        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(set(data.keys()), JOB_RESPONSE_KEYS)
        self.assertEqual(data["title"], "Backend Engineer")
        self.assertEqual(data["company_name"], "Synthetic GmbH")
        self.assertEqual(data["location"], "Berlin")
        self.assertEqual(data["url"], raw_url)
        self.assertEqual(data["description_text"], VALID_DESCRIPTION)

        job = self._get_job(data["job_id"])
        self.assertEqual(job.created_by_user_id, self.user_a_id)
        self.assertEqual(job.source, "manual")
        self.assertEqual(job.url, raw_url)
        self.assertEqual(job.url.encode("utf-8"), raw_url.encode("utf-8"))
        self.assertEqual(job.title, "Backend Engineer")
        self.assertEqual(job.company_name, "Synthetic GmbH")
        self.assertEqual(job.location, "Berlin")
        self.assertEqual(job.description_text, VALID_DESCRIPTION)
        self.assertIsNone(job.source_job_id)
        self.assertIsNone(job.source_created_at)
        self.assertIsNone(job.source_updated_at)
        self.assertEqual(job.job_status, "unknown")

        timestamps = [job.fetched_at, job.last_seen_at, job.created_at, job.updated_at]
        for value in timestamps:
            self.assertIsNotNone(value)
            self.assertIsNone(value.tzinfo)
            self.assertLessEqual(before, value)
            self.assertLessEqual(value, after)
        self.assertEqual(len(set(timestamps)), 1)

        self.assertEqual(self._job_count(), 1)

    def test_empty_optional_fields_are_stored_as_none(self):
        cases = [
            ({"company_name": "", "location": "   "}, "https://example.com/o1"),
            ({"company_name": None, "location": None}, "https://example.com/o2"),
            ({}, "https://example.com/o3"),
        ]
        for overrides, url in cases:
            with self.subTest(overrides=overrides):
                body = _valid_body(url=url, **overrides)
                if not overrides:
                    del body["company_name"]
                    del body["location"]
                response = self._post(body, user_id=self.user_a_id)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertIsNone(response.json()["company_name"])
                self.assertIsNone(response.json()["location"])
                job = self._get_job(response.json()["job_id"])
                self.assertIsNone(job.company_name)
                self.assertIsNone(job.location)

    def test_admin_can_create_and_owns_the_job(self):
        response = self._post(_valid_body(), user_id=self.admin_id)
        self.assertEqual(response.status_code, 201, response.text)
        job = self._get_job(response.json()["job_id"])
        self.assertEqual(job.created_by_user_id, self.admin_id)
        self.assertEqual(job.source, "manual")


class ManualJobDuplicateTests(_BaseManualJobTestCase):
    def _assert_duplicate_response(self, response):
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual(set(body.keys()), {"detail"})
        self.assertEqual(body["detail"], DUPLICATE_DETAIL)
        self.assertEqual(set(body["detail"].keys()), {"error_code", "message"})
        self.assertNotIn("job_id", response.text)

    def test_same_user_same_url_is_409_without_job_id(self):
        first = self._post(_valid_body(), user_id=self.user_a_id)
        self.assertEqual(first.status_code, 201, first.text)
        existing_id = first.json()["job_id"]

        second = self._post(
            _valid_body(title="Another Title"), user_id=self.user_a_id
        )
        self._assert_duplicate_response(second)
        self.assertNotIn(str(existing_id), second.text)
        self.assertEqual(self._job_count(), 1)

    def test_url_differing_only_in_case_is_a_different_url(self):
        first = self._post(_valid_body(url="https://example.com/Case"), user_id=self.user_a_id)
        self.assertEqual(first.status_code, 201, first.text)
        second = self._post(_valid_body(url="https://example.com/case"), user_id=self.user_a_id)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(self._job_count(), 2)

    def test_catalog_and_other_users_job_with_same_url_do_not_block(self):
        catalog_id = self._insert_job(VALID_URL, owner_id=None)
        other_id = self._insert_job(VALID_URL, owner_id=self.user_b_id)
        catalog_before = self._row_snapshot(catalog_id)
        other_before = self._row_snapshot(other_id)

        response = self._post(_valid_body(), user_id=self.user_a_id)

        self.assertEqual(response.status_code, 201, response.text)
        new_id = response.json()["job_id"]
        self.assertNotIn(new_id, (catalog_id, other_id))
        self.assertEqual(self._get_job(new_id).created_by_user_id, self.user_a_id)
        self.assertEqual(self._row_snapshot(catalog_id), catalog_before)
        self.assertEqual(self._row_snapshot(other_id), other_before)
        self.assertEqual(self._job_count(), 3)

    def test_commit_time_unique_violation_becomes_same_409(self):
        # Another request committed the same (owner, url) after our
        # pre-check: the first lookup sees nothing, the real
        # uq_jobs_owner_url index then rejects the INSERT.
        self._insert_job(VALID_URL, owner_id=self.user_a_id)
        real_lookup = jobs._find_owned_job_id_by_url
        calls = []

        def racing_lookup(db, owner_id, url):
            calls.append((owner_id, url))
            if len(calls) == 1:
                return None
            return real_lookup(db, owner_id, url)

        with patch.object(jobs, "_find_owned_job_id_by_url", side_effect=racing_lookup):
            response = self._post(_valid_body(), user_id=self.user_a_id)

        self._assert_duplicate_response(response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], (self.user_a_id, VALID_URL))
        self.assertEqual(calls[1], (self.user_a_id, VALID_URL))
        self.assertEqual(self._job_count(), 1)


class _UnrelatedIntegrityErrorDbStub:
    """Commit fails with an IntegrityError that is not the (owner, url)
    uniqueness: both the pre-check and the post-rollback re-query find
    nothing, so the original error must propagate rather than become 409."""

    def __init__(self):
        self.rolled_back = False
        self.query_calls = 0
        self.added = []

    def query(self, *args, **kwargs):
        self.query_calls += 1

        class _Query:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return None

        return _Query()

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        raise IntegrityError(
            "NOT NULL constraint failed: jobs.title",
            params=None,
            orig=Exception("NOT NULL constraint failed: jobs.title"),
        )

    def rollback(self):
        self.rolled_back = True

    def refresh(self, obj):
        raise AssertionError("refresh must not be reached")


class ManualJobUnrelatedIntegrityErrorTests(unittest.TestCase):
    def test_unrelated_integrity_error_is_reraised(self):
        fake_db = _UnrelatedIntegrityErrorDbStub()
        payload = schemas.JobCreate(
            title="Irrelevant",
            url=VALID_URL,
            description_text=VALID_DESCRIPTION,
        )
        current_user = models.User(user_id=4242, name="Stub", mail="stub@example.com")

        with self.assertRaises(IntegrityError):
            jobs.create_manual_job(payload=payload, current_user=current_user, db=fake_db)

        self.assertTrue(fake_db.rolled_back)
        self.assertEqual(fake_db.query_calls, 2)
        self.assertEqual(len(fake_db.added), 1)


class ManualJobVisibilityTests(_BaseManualJobTestCase):
    def _list_ids(self, user_id):
        response = self.client.get("/jobs/", headers=self._headers(user_id))
        self.assertEqual(response.status_code, 200, response.text)
        return [item["job_id"] for item in response.json()]

    def test_created_job_is_visible_to_owner_and_admin_only(self):
        response = self._post(_valid_body(), user_id=self.user_a_id)
        self.assertEqual(response.status_code, 201, response.text)
        job_id = response.json()["job_id"]

        self.assertIn(job_id, self._list_ids(self.user_a_id))
        self.assertNotIn(job_id, self._list_ids(self.user_b_id))
        self.assertIn(job_id, self._list_ids(self.admin_id))

    def test_admin_created_job_is_not_visible_to_normal_user(self):
        response = self._post(_valid_body(), user_id=self.admin_id)
        self.assertEqual(response.status_code, 201, response.text)
        job_id = response.json()["job_id"]

        self.assertIn(job_id, self._list_ids(self.admin_id))
        self.assertNotIn(job_id, self._list_ids(self.user_a_id))


class ManualJobNoAnalysisTests(_BaseManualJobTestCase):
    def test_no_openai_or_analysis_during_creation(self):
        fake_client = MagicMock()
        patchers = {
            name: patch.object(jobs, name)
            for name in (
                "create_openai_client_or_none",
                "_create_job_analysis_response",
                "_create_job_sample_analysis_response",
                "_require_openai_client",
                "_analyze_job_impl",
                "try_acquire_job_analysis_guard",
            )
        }
        mocks = {}
        with patch.object(jobs, "client", fake_client):
            for name, patcher in patchers.items():
                mocks[name] = patcher.start()
                self.addCleanup(patcher.stop)

            response = self._post(_valid_body(), user_id=self.user_a_id)

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(fake_client.mock_calls, [])
        for name, mock in mocks.items():
            with self.subTest(name=name):
                mock.assert_not_called()
        self.assertEqual(self._count(models.JobAnalysis), 0)
        self.assertEqual(self._count(models.AnalysisGuard), 0)

    def test_works_without_openai_client(self):
        with patch.object(jobs, "client", None), \
                patch.object(jobs, "create_openai_client_or_none") as factory:
            response = self._post(_valid_body(), user_id=self.user_a_id)

        self.assertEqual(response.status_code, 201, response.text)
        factory.assert_not_called()
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(self._count(models.JobAnalysis), 0)
        self.assertEqual(self._count(models.AnalysisGuard), 0)


if __name__ == "__main__":
    unittest.main()
