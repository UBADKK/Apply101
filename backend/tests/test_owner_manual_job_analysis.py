"""POST /jobs/{job_id}/analyze for job owners.

A normal (non-admin) user may analyze only a job they created themselves
with source == "manual". Every other job (another user's manual job, a
catalog job, the user's own non-manual job, a nonexistent id) returns the
exact same ERR_JOB_NOT_FOUND 404 as a nonexistent id, before any cache
read, OpenAI-client check, config load, guard or OpenAI call. Owners may
not use force_reanalyze (403). Admins keep every current power.

Self-contained base (no TestCase imported from another test module), so
no test is collected twice. Throwaway SQLite only; backend.app.main is
never imported and apply101.db is never touched.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
)
from backend.app.analysis_guard import (
    AcquireOutcome,
    JOB_OPERATION_TYPE,
    load_job_analysis_config,
    try_acquire_job_analysis_guard,
)
from backend.app.database import Base, get_db
from backend.app.openai_client import ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL
from backend.app.security import create_access_token, hash_password

# Same synthetic-key import pattern as test_authorization_jobs.py: jobs.py
# builds its OpenAI client at import time; the key is synthetic and only
# set for the import. Every OpenAI-reaching call below is mocked.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-owner-manual-analysis-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"
OPENAI_CREDENTIAL_ENVS = ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY")

# Exactly the fields of schemas.JobAnalysisStructured (extra="forbid").
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

FORCE_REANALYZE_ADMIN_ONLY_BODY = {
    "detail": {
        "error_code": "ERR_FORCE_REANALYZE_ADMIN_ONLY",
        "message": "force_reanalyze is only available to admins.",
    }
}


def _not_found_body(job_id):
    return {
        "detail": {
            "error_code": "ERR_JOB_NOT_FOUND",
            "message": f"Job with id {job_id} was not found.",
        }
    }


def _mock_job_response(payload=None):
    response = MagicMock()
    response.output_text = json.dumps(payload or VALID_JOB_ANALYSIS_PAYLOAD)
    return response


class _BaseOwnerManualAnalysisTestCase(unittest.TestCase):
    """Throwaway SQLite database, only jobs.router mounted, a synthetic
    JWT secret, and jobs.client replaced by a MagicMock (the OpenAI seam
    itself is always patched where it could be reached)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_owner_manual_analysis_test_")
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

        client_patcher = patch.object(jobs, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- data helpers -----------------------------------------------------

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

    def _create_job(
        self,
        slug,
        owner_id=None,
        source=None,
        description_text="synthetic description",
    ):
        session = self.session_factory()
        try:
            job = models.Job(
                title=f"Synthetic Job {slug}",
                url=f"https://example.com/job/{slug}",
                description_text=description_text,
                source=source,
                created_by_user_id=owner_id,
            )
            session.add(job)
            session.commit()
            return job.job_id
        finally:
            session.close()

    def _seed_completed_job_analysis(self, job_id):
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
            return analysis.analysis_id
        finally:
            session.close()

    def _acquire_job_guard_directly(self, job_id):
        session = self.session_factory()
        try:
            return try_acquire_job_analysis_guard(
                session, job_id=job_id, config=load_job_analysis_config(),
            )
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

    # -- state snapshot (read straight from the temp SQLite file) ---------

    def _rows(self, table, order_by):
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT * FROM {table} ORDER BY {order_by}")
            ).fetchall()

    def _snapshot(self):
        return {
            "jobs": self._rows("jobs", "job_id"),
            "job_analysis": self._rows("job_analysis", "analysis_id"),
            "analysis_guards": self._rows(
                "analysis_guards", "operation_type, resource_id"
            ),
        }

    def _job_guard_row(self, job_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT * FROM analysis_guards "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"op": JOB_OPERATION_TYPE, "rid": job_id},
            ).fetchone()

    # -- request helpers ---------------------------------------------------

    def _post_analyze(self, job_id, headers=None, force_reanalyze=False):
        params = {"force_reanalyze": "true"} if force_reanalyze else None
        return self.client.post(
            f"/jobs/{job_id}/analyze", params=params, headers=headers
        )

    def _post_with_seams_spied(self, job_id, headers, force_reanalyze=False):
        """Posts with the OpenAI seam, config load and guard acquire all
        spied (wraps the real function; the OpenAI seam is a MagicMock
        returning a valid payload). Returns (response, spies)."""
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai, patch(
            "backend.routers.jobs.load_job_analysis_config",
            wraps=jobs.load_job_analysis_config,
        ) as mock_config, patch(
            "backend.routers.jobs.try_acquire_job_analysis_guard",
            wraps=jobs.try_acquire_job_analysis_guard,
        ) as mock_acquire:
            response = self._post_analyze(
                job_id, headers=headers, force_reanalyze=force_reanalyze
            )
        return response, (mock_openai, mock_config, mock_acquire)

    def _assert_no_seam_called(self, spies):
        for spy in spies:
            spy.assert_not_called()


class AuthenticationTests(_BaseOwnerManualAnalysisTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("auth.owner@example.com")
        self.job_id = self._create_job(
            "auth-manual", owner_id=self.owner_id, source="manual"
        )

    def test_missing_token_is_401(self):
        before = self._snapshot()
        response, spies = self._post_with_seams_spied(self.job_id, headers=None)
        self.assertEqual(response.status_code, 401)
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)

    def test_invalid_token_is_401(self):
        before = self._snapshot()
        response, spies = self._post_with_seams_spied(
            self.job_id, headers={"Authorization": "Bearer not-a-real-jwt"}
        )
        self.assertEqual(response.status_code, 401)
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)


class NonOwnerGetsNotFoundTests(_BaseOwnerManualAnalysisTestCase):
    """User B gets the nonexistent-id 404 for every job they may not
    analyze, whatever else is true about that job."""

    def setUp(self):
        super().setUp()
        self.user_a = self._create_user("owner.a@example.com")
        self.user_b = self._create_user("requester.b@example.com")
        self.headers_b = self._auth_headers(self.user_b)

    def _hidden_targets(self, prefix, description_text="synthetic description"):
        targets = {
            "other_users_manual_job": self._create_job(
                f"{prefix}-a-manual", owner_id=self.user_a, source="manual",
                description_text=description_text,
            ),
            "catalog_job": self._create_job(
                f"{prefix}-catalog", owner_id=None, source="arbeitnow",
                description_text=description_text,
            ),
            "catalog_job_without_source": self._create_job(
                f"{prefix}-catalog-nosource", owner_id=None, source=None,
                description_text=description_text,
            ),
            "own_job_source_none": self._create_job(
                f"{prefix}-b-none", owner_id=self.user_b, source=None,
                description_text=description_text,
            ),
            "own_job_source_fixture": self._create_job(
                f"{prefix}-b-fixture", owner_id=self.user_b, source="fixture",
                description_text=description_text,
            ),
        }
        targets["nonexistent_id"] = self._nonexistent_job_id()
        return targets

    def _assert_hidden_404(self, job_id, force_reanalyze=False):
        before = self._snapshot()
        response, spies = self._post_with_seams_spied(
            job_id, headers=self.headers_b, force_reanalyze=force_reanalyze
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), _not_found_body(job_id))
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)

    def test_plain_request_is_404(self):
        for name, job_id in self._hidden_targets("plain").items():
            with self.subTest(target=name):
                self._assert_hidden_404(job_id)

    def test_cached_current_analysis_is_still_404(self):
        targets = self._hidden_targets("cached")
        for name, job_id in targets.items():
            if name != "nonexistent_id":
                self._seed_completed_job_analysis(job_id)
        for name, job_id in targets.items():
            with self.subTest(target=name):
                self._assert_hidden_404(job_id)

    def test_force_reanalyze_is_404_not_403(self):
        for name, job_id in self._hidden_targets("force").items():
            with self.subTest(target=name):
                self._assert_hidden_404(job_id, force_reanalyze=True)

    def test_force_reanalyze_with_cached_analysis_is_404(self):
        targets = self._hidden_targets("force-cached")
        for name, job_id in targets.items():
            if name != "nonexistent_id":
                self._seed_completed_job_analysis(job_id)
        for name, job_id in targets.items():
            with self.subTest(target=name):
                self._assert_hidden_404(job_id, force_reanalyze=True)

    def test_missing_openai_client_is_404_not_503(self):
        targets = self._hidden_targets("noclient")
        env = {k: v for k, v in os.environ.items() if k not in OPENAI_CREDENTIAL_ENVS}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(jobs, "client", None):
            for name, job_id in targets.items():
                with self.subTest(target=name):
                    self._assert_hidden_404(job_id)
            self.assertIsNone(jobs.client)

    def test_active_guard_is_404_and_guard_row_unchanged(self):
        targets = self._hidden_targets("guard")
        for job_id in targets.values():
            result = self._acquire_job_guard_directly(job_id)
            self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        for name, job_id in targets.items():
            with self.subTest(target=name):
                guard_before = self._job_guard_row(job_id)
                self.assertIsNotNone(guard_before)
                self._assert_hidden_404(job_id)
                self.assertEqual(self._job_guard_row(job_id), guard_before)

    def test_missing_description_is_404_not_400(self):
        targets = self._hidden_targets("nodesc", description_text=None)
        for name, job_id in targets.items():
            with self.subTest(target=name):
                self._assert_hidden_404(job_id)

    def test_body_matches_nonexistent_id_body(self):
        other_manual = self._create_job(
            "body-a-manual", owner_id=self.user_a, source="manual"
        )
        nonexistent = self._nonexistent_job_id()
        hidden = self._post_analyze(other_manual, headers=self.headers_b)
        missing = self._post_analyze(nonexistent, headers=self.headers_b)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), _not_found_body(nonexistent))
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json(), _not_found_body(other_manual))


class OwnerManualJobAnalysisTests(_BaseOwnerManualAnalysisTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("owner.manual@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.job_id = self._create_job(
            "owner-manual", owner_id=self.owner_id, source="manual"
        )

    def test_owner_first_call_created_second_call_cached(self):
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            first = self._post_analyze(self.job_id, headers=self.headers)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["status"], "created")
            self.assertEqual(first.json()["job_id"], self.job_id)
            self.assertEqual(mock_openai.call_count, 1)

            second = self._post_analyze(self.job_id, headers=self.headers)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["status"], "cached")
            self.assertEqual(
                second.json()["analysis_id"], first.json()["analysis_id"]
            )
            self.assertEqual(mock_openai.call_count, 1)

        rows = self._rows("job_analysis", "analysis_id")
        self.assertEqual(len(rows), 1)

    def test_owner_active_guard_gets_409_like_admin(self):
        result = self._acquire_job_guard_directly(self.job_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        guard_before = self._job_guard_row(self.job_id)

        with patch("backend.routers.jobs._create_job_analysis_response") as mock_openai:
            response = self._post_analyze(self.job_id, headers=self.headers)

        self.assertEqual(response.status_code, 409)
        mock_openai.assert_not_called()
        self.assertEqual(self._job_guard_row(self.job_id), guard_before)

    def test_owner_missing_description_gets_400(self):
        job_id = self._create_job(
            "owner-manual-nodesc", owner_id=self.owner_id, source="manual",
            description_text=None,
        )
        response, spies = self._post_with_seams_spied(job_id, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_JOB_DESCRIPTION_MISSING"
        )
        self._assert_no_seam_called(spies)

    def test_owner_missing_openai_client_gets_503(self):
        env = {k: v for k, v in os.environ.items() if k not in OPENAI_CREDENTIAL_ENVS}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(jobs, "client", None):
            before = self._snapshot()
            response, spies = self._post_with_seams_spied(
                self.job_id, headers=self.headers
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL}
        )
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)

    def test_owner_force_reanalyze_without_cache_is_403_no_side_effects(self):
        before = self._snapshot()
        response, spies = self._post_with_seams_spied(
            self.job_id, headers=self.headers, force_reanalyze=True
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), FORCE_REANALYZE_ADMIN_ONLY_BODY)
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)
        self.assertIsNone(self._job_guard_row(self.job_id))
        self.assertEqual(self._rows("job_analysis", "analysis_id"), [])

    def test_owner_force_reanalyze_with_cache_is_403_cache_unchanged(self):
        self._seed_completed_job_analysis(self.job_id)
        before = self._snapshot()
        self.assertEqual(len(before["job_analysis"]), 1)

        response, spies = self._post_with_seams_spied(
            self.job_id, headers=self.headers, force_reanalyze=True
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), FORCE_REANALYZE_ADMIN_ONLY_BODY)
        self._assert_no_seam_called(spies)
        self.assertEqual(self._snapshot(), before)
        self.assertIsNone(self._job_guard_row(self.job_id))


class AdminKeepsFullAccessTests(_BaseOwnerManualAnalysisTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self._create_user("admin.full@example.com", is_admin=True)
        self.admin_headers = self._auth_headers(self.admin_id)
        self.user_a = self._create_user("owner.for.admin@example.com")
        self.other_manual = self._create_job(
            "admin-other-manual", owner_id=self.user_a, source="manual"
        )

    def test_admin_analyzes_other_users_manual_job(self):
        response, (mock_openai, mock_config, mock_acquire) = self._post_with_seams_spied(
            self.other_manual, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_openai.assert_called_once()
        mock_config.assert_called_once()
        mock_acquire.assert_called_once()

    def test_admin_analyzes_catalog_job(self):
        catalog = self._create_job("admin-catalog", source="arbeitnow")
        response, (mock_openai, _, _) = self._post_with_seams_spied(
            catalog, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_openai.assert_called_once()

    def test_admin_force_reanalyze_on_other_users_manual_job_replaces_cache(self):
        old_analysis_id = self._seed_completed_job_analysis(self.other_manual)

        response, (mock_openai, _, _) = self._post_with_seams_spied(
            self.other_manual, headers=self.admin_headers, force_reanalyze=True
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        self.assertNotEqual(response.json()["analysis_id"], old_analysis_id)
        mock_openai.assert_called_once()

        session = self.session_factory()
        try:
            current = session.query(models.JobAnalysis).filter(
                models.JobAnalysis.job_id == self.other_manual,
                models.JobAnalysis.is_current == True,  # noqa: E712
            ).all()
        finally:
            session.close()
        self.assertEqual([row.analysis_id for row in current],
                         [response.json()["analysis_id"]])

    def test_admin_cached_on_other_users_manual_job(self):
        analysis_id = self._seed_completed_job_analysis(self.other_manual)
        response, spies = self._post_with_seams_spied(
            self.other_manual, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cached")
        self.assertEqual(response.json()["analysis_id"], analysis_id)
        self._assert_no_seam_called(spies)

    def test_admin_missing_openai_client_gets_503(self):
        env = {k: v for k, v in os.environ.items() if k not in OPENAI_CREDENTIAL_ENVS}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(jobs, "client", None):
            response, spies = self._post_with_seams_spied(
                self.other_manual, headers=self.admin_headers
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL}
        )
        self._assert_no_seam_called(spies)

    def test_admin_nonexistent_id_is_same_404(self):
        job_id = self._nonexistent_job_id()
        response = self._post_analyze(job_id, headers=self.admin_headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), _not_found_body(job_id))


if __name__ == "__main__":
    unittest.main()
