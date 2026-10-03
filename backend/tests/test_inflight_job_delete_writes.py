"""Job analysis must not write anything for a job that was deleted while the
OpenAI call was in flight -- neither an orphan job_analysis row for the
deleted job_id nor a row (or an is_current demotion) attached to a NEW job
that reused the same job_id (jobs.job_id has no AUTOINCREMENT and SQLite
foreign keys are not enforced).

The only job-deletion path is users.delete_user (owned jobs go with their
owner). The deletion is made deterministic, without threads: the OpenAI
seam _create_job_analysis_response is patched with a side effect that
deletes the owner through the real users.delete_user in a separate,
independently committed session, optionally inserts a new job under the
same job_id, and then returns (or raises) the OpenAI outcome.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query, sessionmaker

from backend.app import models
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
)
from backend.app.analysis_guard import JOB_OPERATION_TYPE
from backend.app.database import Base, get_db
from backend.app.security import create_access_token
from backend.tests.test_authorization_jobs import VALID_JOB_ANALYSIS_PAYLOAD

# Synthetic key only while importing (jobs.py builds its client at import
# time); every OpenAI-reaching call below is patched.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs, users


SYNTHETIC_SECRET = "synthetic-test-secret-for-inflight-job-delete-0123456789"
INVALID_JSON_TEXT = "not valid json{{{"
UPSTREAM_ERROR = "synthetic upstream failure"


def _openai_response(output_text):
    response = MagicMock()
    response.output_text = output_text
    return response


def _valid():
    return _openai_response(json.dumps(VALID_JOB_ANALYSIS_PAYLOAD))


def _invalid_json():
    return _openai_response(INVALID_JSON_TEXT)


def _raise():
    raise RuntimeError(UPSTREAM_ERROR)


class _BaseInflightJobDeleteTestCase(unittest.TestCase):
    """Throwaway synthetic SQLite database (never apply101.db); only
    users.router and jobs.router mounted; synthetic JWT secret."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_inflight_job_delete_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
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

        # Never a real client; _create_job_analysis_response is patched in
        # every test that reaches it.
        client_patcher = patch.object(jobs, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

        self.admin_id = self._create_user("inflight.job.admin@example.com", is_admin=True)
        self.headers = self._headers(self.admin_id)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # --- seeding ---------------------------------------------------------

    def _add(self, obj):
        session = self.session_factory()
        try:
            session.add(obj)
            session.commit()
            session.refresh(obj)
            return obj
        finally:
            session.close()

    def _create_user(self, mail, is_admin=False):
        return self._add(models.User(
            name="Synthetic User", mail=mail, is_admin=is_admin
        )).user_id

    def _create_job(self, slug, owner_id=None, job_id=None, url=None):
        return self._add(models.Job(
            job_id=job_id,
            title=f"Synthetic Job {slug}",
            url=url or f"https://example.com/job/{slug}",
            description_text="synthetic description",
            created_by_user_id=owner_id,
        )).job_id

    def _seed_job_analysis(self, job_id):
        return self._add(models.JobAnalysis(
            job_id=job_id,
            analysis_status="completed",
            analysis_json=json.dumps(VALID_JOB_ANALYSIS_PAYLOAD),
            role_tags_json='["other"]',
            analysis_model=JOB_ANALYSIS_MODEL,
            analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
            is_current=True,
        )).analysis_id

    def _headers(self, user_id):
        # Minted with the user's real stored token key, exactly as login does.
        session = self.session_factory()
        try:
            token_key = session.query(models.User.token_key).filter(
                models.User.user_id == user_id
            ).scalar()
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    # --- the concurrent request, in its own independently committed session

    def _delete_user_elsewhere(self, user_id, session_factory=None):
        session = (session_factory or self.session_factory)()
        try:
            user = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one()
            result = users.delete_user(user_id=user_id, current_user=user, db=session)
            self.assertEqual(result["status"], "deleted")
        finally:
            session.close()

    # --- reads -------------------------------------------------------------

    def _job_analysis_rows(self, job_id):
        session = self.session_factory()
        try:
            return sorted(
                (row.analysis_id, row.analysis_status, row.is_current)
                for row in session.query(models.JobAnalysis).filter(
                    models.JobAnalysis.job_id == job_id
                ).all()
            )
        finally:
            session.close()

    def _job_analysis_snapshot(self, analysis_id):
        session = self.session_factory()
        try:
            row = session.get(models.JobAnalysis, analysis_id)
            return {
                column.name: getattr(row, column.name)
                for column in models.JobAnalysis.__table__.columns
            }
        finally:
            session.close()

    def _job_row(self, job_id):
        session = self.session_factory()
        try:
            job = session.get(models.Job, job_id)
            return None if job is None else (job.url, job.created_by_user_id)
        finally:
            session.close()

    def _guard_row(self, job_id):
        session = self.session_factory()
        try:
            return session.query(models.AnalysisGuard.owner_token).filter(
                models.AnalysisGuard.operation_type == JOB_OPERATION_TYPE,
                models.AnalysisGuard.resource_id == job_id,
            ).first()
        finally:
            session.close()

    def _assert_guard_not_left_locked(self, job_id):
        row = self._guard_row(job_id)
        if row is not None:
            self.assertIsNone(row[0])

    def _post_analyze(self, job_id, **params):
        return self.client.post(
            f"/jobs/{job_id}/analyze", params=params, headers=self.headers
        )

    def _expected_job_not_found(self, job_id):
        return {
            "detail": {
                "error_code": "ERR_JOB_NOT_FOUND",
                "message": f"Job with id {job_id} was not found.",
            }
        }


class JobAnalysisInflightDeleteTests(_BaseInflightJobDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("inflight.job.owner@example.com")
        self.job_id = self._create_job("owned", owner_id=self.owner_id)
        # Deleted together with the owner; force_reanalyze reaches OpenAI.
        self._seed_job_analysis(self.job_id)

        # Unrelated jobs whose current analyses must stay untouched: one
        # ownerless, one owned by another (live) user.
        self.other_user_id = self._create_user("inflight.job.other@example.com")
        self.ownerless_job_id = self._create_job("ownerless")
        self.ownerless_analysis_id = self._seed_job_analysis(self.ownerless_job_id)
        self.other_job_id = self._create_job("other-owned", owner_id=self.other_user_id)
        self.other_analysis_id = self._seed_job_analysis(self.other_job_id)
        self.other_snapshots = {
            analysis_id: self._job_analysis_snapshot(analysis_id)
            for analysis_id in (self.ownerless_analysis_id, self.other_analysis_id)
        }

    def _analyze(self, side_effect):
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=side_effect,
        ) as mock_call, self.assertNoLogs("backend.routers.jobs", level="WARNING"):
            response = self._post_analyze(self.job_id, force_reanalyze="true")
        # The delete really happened during the OpenAI call.
        mock_call.assert_called_once()
        return response

    def _delete_then(self, outcome):
        def side_effect(*args, **kwargs):
            self._delete_user_elsewhere(self.owner_id)
            return outcome()
        return side_effect

    def _delete_reuse_then(self, outcome, url=None, owner_id=None):
        # A different job that got the deleted job's job_id (SQLite reuses
        # the max rowid without AUTOINCREMENT), already with its own
        # current completed analysis.
        def side_effect(*args, **kwargs):
            self._delete_user_elsewhere(self.owner_id)
            self._create_job(
                "replacement",
                job_id=self.job_id,
                owner_id=owner_id,
                url=url or "https://example.com/job/replacement",
            )
            self.new_job_analysis_id = self._seed_job_analysis(self.job_id)
            self.new_job_analysis_snapshot = self._job_analysis_snapshot(
                self.new_job_analysis_id
            )
            return outcome()
        return side_effect

    def _assert_other_jobs_untouched(self):
        self.assertEqual(
            self._job_analysis_rows(self.ownerless_job_id),
            [(self.ownerless_analysis_id, "completed", True)],
        )
        self.assertEqual(
            self._job_analysis_rows(self.other_job_id),
            [(self.other_analysis_id, "completed", True)],
        )
        for analysis_id, snapshot in self.other_snapshots.items():
            self.assertEqual(self._job_analysis_snapshot(analysis_id), snapshot)

    def _assert_404_and_nothing_written(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), self._expected_job_not_found(self.job_id))
        self.assertIsNone(self._job_row(self.job_id))
        self.assertEqual(self._job_analysis_rows(self.job_id), [])
        self._assert_other_jobs_untouched()
        self._assert_guard_not_left_locked(self.job_id)

    def _assert_404_and_new_job_untouched(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), self._expected_job_not_found(self.job_id))
        # The is_current demotion (success path) ran for this job_id before
        # the check and must have been rolled back with everything else.
        self.assertEqual(
            self._job_analysis_rows(self.job_id),
            [(self.new_job_analysis_id, "completed", True)],
        )
        self.assertEqual(
            self._job_analysis_snapshot(self.new_job_analysis_id),
            self.new_job_analysis_snapshot,
        )
        self._assert_other_jobs_untouched()
        self._assert_guard_not_left_locked(self.job_id)

    # --- owner deleted, job_id not reused ----------------------------------

    def test_success_after_owner_delete_returns_404_and_writes_nothing(self):
        response = self._analyze(self._delete_then(_valid))
        self._assert_404_and_nothing_written(response)

    def test_invalid_json_after_owner_delete_returns_404_and_writes_no_failed_row(self):
        response = self._analyze(self._delete_then(_invalid_json))
        self._assert_404_and_nothing_written(response)

    def test_exception_after_owner_delete_returns_404_and_writes_no_failed_row(self):
        response = self._analyze(self._delete_then(_raise))
        self._assert_404_and_nothing_written(response)

    # --- owner deleted, job_id reused by a different job --------------------

    def test_success_after_job_id_reuse_keeps_new_jobs_current_analysis(self):
        response = self._analyze(self._delete_reuse_then(_valid))
        self._assert_404_and_new_job_untouched(response)
        self.assertEqual(
            self._job_row(self.job_id), ("https://example.com/job/replacement", None)
        )

    def test_invalid_json_after_job_id_reuse_attaches_no_failed_row(self):
        response = self._analyze(self._delete_reuse_then(_invalid_json))
        self._assert_404_and_new_job_untouched(response)

    def test_exception_after_job_id_reuse_attaches_no_failed_row(self):
        response = self._analyze(self._delete_reuse_then(_raise))
        self._assert_404_and_new_job_untouched(response)

    def test_success_after_reuse_with_same_url_but_other_owner_returns_404(self):
        # Same job_id AND same url (the old row is gone, so UNIQUE allows
        # it), but a different owner: only the owner predicate detects it.
        same_url = "https://example.com/job/owned"
        response = self._analyze(self._delete_reuse_then(
            _valid, url=same_url, owner_id=self.other_user_id
        ))
        self._assert_404_and_new_job_untouched(response)
        self.assertEqual(self._job_row(self.job_id), (same_url, self.other_user_id))


class JobAnalysisUnchangedBehaviorTests(_BaseInflightJobDeleteTestCase):
    """The same request against a job that still exists behaves exactly as
    before, for both the owned (non-NULL owner) and the ownerless branch."""

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("inflight.job.live.owner@example.com")
        self.owned_job_id = self._create_job("live-owned", owner_id=self.owner_id)
        self.ownerless_job_id = self._create_job("live-ownerless")
        self.job_ids = {"owned": self.owned_job_id, "ownerless": self.ownerless_job_id}

    def _assert_job_survived(self, job_id):
        self.assertIsNotNone(self._job_row(job_id))

    def test_success_creates_and_demotes_previous_current(self):
        for label, job_id in self.job_ids.items():
            with self.subTest(job=label):
                prior_id = self._seed_job_analysis(job_id)

                with patch(
                    "backend.routers.jobs._create_job_analysis_response",
                    return_value=_valid(),
                ) as mock_call:
                    response = self._post_analyze(job_id, force_reanalyze="true")

                mock_call.assert_called_once()
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(
                    set(body),
                    {"status", "job_id", "analysis_id", "analysis_model",
                     "analysis_prompt_version", "analysis"},
                )
                self.assertEqual(body["status"], "created")
                self.assertEqual(body["job_id"], job_id)
                self.assertEqual(body["analysis_model"], JOB_ANALYSIS_MODEL)
                self.assertEqual(body["analysis_prompt_version"], JOB_ANALYSIS_PROMPT_VERSION)
                self.assertEqual(
                    self._job_analysis_rows(job_id),
                    [(prior_id, "completed", False), (body["analysis_id"], "completed", True)],
                )
                # Released (into the success cooldown), not left locked.
                guard = self._guard_row(job_id)
                self.assertIsNotNone(guard)
                self.assertIsNone(guard[0])

    def test_invalid_json_writes_failed_row(self):
        for label, job_id in self.job_ids.items():
            with self.subTest(job=label):
                with patch(
                    "backend.routers.jobs._create_job_analysis_response",
                    return_value=_invalid_json(),
                ):
                    response = self._post_analyze(job_id)

                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"detail": {
                    "error_code": "ERR_AI_INVALID_JSON",
                    "message": "AI response was not valid JSON.",
                    "job_id": job_id,
                    "raw_response": INVALID_JSON_TEXT,
                }})
                rows = self._job_analysis_rows(job_id)
                self.assertEqual([(s, c) for _, s, c in rows], [("failed", False)])
                guard = self._guard_row(job_id)
                self.assertIsNotNone(guard)
                self.assertIsNone(guard[0])

    def test_exception_writes_failed_row(self):
        for label, job_id in self.job_ids.items():
            with self.subTest(job=label):
                with patch(
                    "backend.routers.jobs._create_job_analysis_response",
                    side_effect=RuntimeError(UPSTREAM_ERROR),
                ):
                    response = self._post_analyze(job_id)

                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"detail": {
                    "error_code": "ERR_JOB_ANALYSIS_FAILED",
                    "message": "Job analysis failed.",
                    "job_id": job_id,
                    "error": UPSTREAM_ERROR,
                }})
                rows = self._job_analysis_rows(job_id)
                self.assertEqual([(s, c) for _, s, c in rows], [("failed", False)])
                guard = self._guard_row(job_id)
                self.assertIsNotNone(guard)
                self.assertIsNone(guard[0])

    # --- write lock between the re-check and the commit ----------------------

    def _post_with_delete_attempt_after_check(self, job_id, outcome):
        """Wraps the REAL write check (jobs._verify_job_unchanged_or_404)
        so that, right after its job SELECT has returned its (fully
        fetched) row -- i.e. anywhere between the check and our commit --
        the owner's deletion is attempted from another connection with a
        short busy timeout. With the flush issued before the SELECT, this
        connection already holds SQLite's write lock, so the delete must
        fail with "database is locked"; a check issued before the first
        write would let it commit and leave an orphan row."""
        blocked_engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        self.addCleanup(blocked_engine.dispose)
        blocked_factory = sessionmaker(autocommit=False, autoflush=False, bind=blocked_engine)
        real_verify = jobs._verify_job_unchanged_or_404
        real_first = Query.first
        outcomes = []
        in_write_check = []

        def verify_marking_write_check(db, **kwargs):
            in_write_check.append(True)
            try:
                return real_verify(db, **kwargs)
            finally:
                in_write_check.pop()

        def first_then_try_delete(query):
            row = real_first(query)
            if (
                in_write_check
                and not outcomes
                and query.column_descriptions[0].get("entity") is models.Job
            ):
                in_write_check.clear()  # no re-entry from the delete itself
                try:
                    self._delete_user_elsewhere(self.owner_id, blocked_factory)
                    outcomes.append("delete committed")
                except OperationalError as exc:
                    outcomes.append("locked" if "locked" in str(exc) else repr(exc))
                finally:
                    in_write_check.append(True)
            return row

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=lambda *args, **kwargs: outcome(),
        ), patch.object(
            jobs, "_verify_job_unchanged_or_404", side_effect=verify_marking_write_check
        ), patch.object(Query, "first", first_then_try_delete):
            response = self._post_analyze(job_id, force_reanalyze="true")

        self.assertEqual(outcomes, ["locked"])
        # The owner and job survived, so whatever was committed is a
        # legitimate row of a live job, not an orphan.
        self._assert_job_survived(job_id)
        return response

    def test_concurrent_delete_cannot_commit_between_check_and_commit_on_invalid_json(self):
        # After the OpenAI call nothing was written yet, so the failed row's
        # flush inside the check is the only thing that takes the write
        # lock -- this proves the flush-then-check order matters.
        response = self._post_with_delete_attempt_after_check(
            self.owned_job_id, _invalid_json
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_AI_INVALID_JSON")
        rows = self._job_analysis_rows(self.owned_job_id)
        self.assertEqual([(s, c) for _, s, c in rows], [("failed", False)])

    def test_concurrent_delete_cannot_commit_between_check_and_commit_on_exception(self):
        response = self._post_with_delete_attempt_after_check(self.owned_job_id, _raise)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_JOB_ANALYSIS_FAILED")
        rows = self._job_analysis_rows(self.owned_job_id)
        self.assertEqual([(s, c) for _, s, c in rows], [("failed", False)])

    def test_concurrent_delete_cannot_commit_between_check_and_commit_on_success(self):
        response = self._post_with_delete_attempt_after_check(self.owned_job_id, _valid)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")
        self.assertEqual(
            self._job_analysis_rows(self.owned_job_id),
            [(response.json()["analysis_id"], "completed", True)],
        )


if __name__ == "__main__":
    unittest.main()
