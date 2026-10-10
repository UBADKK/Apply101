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
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query, sessionmaker

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
    from backend.routers import jobs, users


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


class OwnerReplacedMidRequestTests(_BaseOwnerManualAnalysisTestCase):
    """Owner A's request is authenticated, then -- before it finishes -- A
    is deleted through the real users.delete_user and a NEW user B gets A's
    user_id (users.user_id/jobs.job_id have no AUTOINCREMENT; SQLite FKs
    are not enforced) with a fresh token_key, plus a manual job with A's
    job_id and url (and, where stated, its own current completed
    analysis). Every replacement step is committed in its own separate
    session. A's request must then get the nonexistent-id 404 and must
    neither reveal nor modify anything of B's: users, jobs, job_analysis
    (every column, incl. is_current) and analysis_guards stay exactly as
    they were right after the replacement.

    A's own guard row for the job (scenario 3 acquires it before the
    OpenAI call) is deleted together with A's jobs by delete_user, and the
    release in _analyze_job_impl's finally is gated by A's owner_token, so
    afterwards there is no guard row for the job_id at all."""

    B_SUMMARY = "synthetic private summary of replacement owner b"
    INVALID_JSON_TEXT = "not valid json{{{"
    UPSTREAM_ERROR = "synthetic upstream failure"

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("replaced.owner.a@example.com")
        self.headers = self._auth_headers(self.owner_id)
        self.owner_token_key = self._token_key(self.owner_id)
        self.job_slug = "replaced-owner-manual"
        self.job_url = f"https://example.com/job/{self.job_slug}"
        self.job_id = self._create_job(
            self.job_slug, owner_id=self.owner_id, source="manual"
        )
        # Bystander data that must never be touched either (covered by the
        # full snapshot comparison).
        bystander = self._create_user("replaced.bystander@example.com")
        self._seed_completed_job_analysis(
            self._create_job("replaced-bystander", owner_id=bystander, source="manual")
        )
        self.replaced = False
        self.b_guard = None
        self.b_analysis_id = None
        self.after_replacement = None

    # -- helpers ----------------------------------------------------------

    def _token_key(self, user_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT token_key FROM users WHERE user_id = :uid"),
                {"uid": user_id},
            ).scalar()

    def _full_snapshot(self):
        snapshot = self._snapshot()
        snapshot["users"] = self._rows("users", "user_id")
        return snapshot

    def _delete_owner_a(self, session_factory=None):
        session = (session_factory or self.session_factory)()
        try:
            user_a = session.query(models.User).filter(
                models.User.user_id == self.owner_id
            ).one()
            result = users.delete_user(
                user_id=self.owner_id, current_user=user_a, db=session
            )
            self.assertEqual(result["status"], "deleted")
        finally:
            session.close()

    def _replace_owner_elsewhere(self, with_b_analysis=True, b_guard=None):
        """b_guard: None, "lease" (B holds a live analysis lease on the
        job_id) or "cooldown" (an active cooldown on the job_id)."""
        self.assertFalse(self.replaced, "replacement must run exactly once")
        self.replaced = True
        self.b_guard = b_guard

        self._delete_owner_a()

        session = self.session_factory()
        try:
            session.add(models.User(
                user_id=self.owner_id,
                name="Replacement User B",
                mail="replacement.owner.b@example.com",
            ))
            session.commit()
        finally:
            session.close()

        session = self.session_factory()
        try:
            session.add(models.Job(
                job_id=self.job_id,
                title="Replacement Job of B",
                url=self.job_url,
                description_text="replacement description of b",
                source="manual",
                created_by_user_id=self.owner_id,
            ))
            session.commit()
        finally:
            session.close()

        if with_b_analysis:
            session = self.session_factory()
            try:
                analysis = models.JobAnalysis(
                    job_id=self.job_id,
                    analysis_status="completed",
                    analysis_json=json.dumps(
                        {**VALID_JOB_ANALYSIS_PAYLOAD, "summary": self.B_SUMMARY}
                    ),
                    role_tags_json='["other"]',
                    analysis_model=JOB_ANALYSIS_MODEL,
                    analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
                    is_current=True,
                )
                session.add(analysis)
                session.commit()
                self.b_analysis_id = analysis.analysis_id
            finally:
                session.close()

        if b_guard is not None:
            now = time.time()
            session = self.session_factory()
            try:
                session.add(models.AnalysisGuard(
                    operation_type=JOB_OPERATION_TYPE,
                    resource_id=self.job_id,
                    owner_token="synthetic-owner-token-of-b" if b_guard == "lease" else None,
                    lock_expires_at=now + 600 if b_guard == "lease" else None,
                    cooldown_until=now + 600 if b_guard == "cooldown" else None,
                ))
                session.commit()
            finally:
                session.close()

        b_token_key = self._token_key(self.owner_id)
        self.assertTrue(b_token_key)
        self.assertNotEqual(b_token_key, self.owner_token_key)
        self.after_replacement = self._full_snapshot()

    def _assert_replaced_404(self, response):
        self.assertTrue(self.replaced, "the replacement seam was never reached")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), _not_found_body(self.job_id))
        self.assertNotIn(self.B_SUMMARY, response.text)
        self.assertEqual(self._full_snapshot(), self.after_replacement)
        if self.b_analysis_id is not None:
            self.assertEqual(
                [(row.analysis_id, row.analysis_status, row.is_current)
                 for row in self._rows("job_analysis", "analysis_id")
                 if row.job_id == self.job_id],
                [(self.b_analysis_id, "completed", 1)],
            )
        else:
            self.assertEqual(
                [row for row in self._rows("job_analysis", "analysis_id")
                 if row.job_id == self.job_id],
                [],
            )
        if self.b_guard is None:
            # No guard row and so no cooldown for the job_id.
            self.assertIsNone(self._job_guard_row(self.job_id))

    # -- 1. replaced between authentication and the first job query --------

    def _post_with_replacement_before_impl(self, force_reanalyze=False, with_b_analysis=True):
        real_impl = jobs._analyze_job_impl

        def replace_then_impl(**kwargs):
            self._replace_owner_elsewhere(with_b_analysis=with_b_analysis)
            return real_impl(**kwargs)

        with patch.object(jobs, "_analyze_job_impl", side_effect=replace_then_impl):
            return self._post_with_seams_spied(
                self.job_id, self.headers, force_reanalyze=force_reanalyze
            )

    def test_replaced_before_first_access_does_not_return_b_cached_analysis(self):
        response, spies = self._post_with_replacement_before_impl()
        self._assert_replaced_404(response)
        self._assert_no_seam_called(spies)

    def test_replaced_before_first_access_force_reanalyze_is_404_not_403(self):
        response, spies = self._post_with_replacement_before_impl(force_reanalyze=True)
        self._assert_replaced_404(response)
        self._assert_no_seam_called(spies)

    def test_replaced_before_first_access_without_b_analysis_never_analyzes_b_job(self):
        response, spies = self._post_with_replacement_before_impl(with_b_analysis=False)
        self._assert_replaced_404(response)
        self._assert_no_seam_called(spies)

    # -- 2. replaced after the first job query, before the cache lookup ----

    def test_replaced_before_cache_lookup_does_not_return_b_cached_analysis(self):
        real_first = Query.first
        queried_entities = []

        def first_with_replacement(query):
            entity = query.column_descriptions[0].get("entity")
            queried_entities.append(entity)
            if not self.replaced and entity is models.JobAnalysis:
                self._replace_owner_elsewhere()
            return real_first(query)

        with patch.object(Query, "first", first_with_replacement):
            response, spies = self._post_with_seams_spied(self.job_id, self.headers)

        # The first job query (after authentication) ran before the swap.
        self.assertIn(models.Job, queried_entities[:queried_entities.index(models.JobAnalysis)])
        self._assert_replaced_404(response)
        self._assert_no_seam_called(spies)

    # -- 3. replaced during the OpenAI call (every write path) --------------

    def _post_with_replacement_during_openai(self, outcome):
        def side_effect(*args, **kwargs):
            self._replace_owner_elsewhere()
            return outcome()

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=side_effect,
        ) as mock_openai:
            response = self._post_analyze(self.job_id, headers=self.headers)
        mock_openai.assert_called_once()
        return response

    def test_replaced_during_openai_success_does_not_touch_b_analysis(self):
        response = self._post_with_replacement_during_openai(_mock_job_response)
        self._assert_replaced_404(response)

    def test_replaced_during_openai_invalid_json_writes_no_failed_row(self):
        def invalid_json():
            response = MagicMock()
            response.output_text = self.INVALID_JSON_TEXT
            return response

        response = self._post_with_replacement_during_openai(invalid_json)
        self._assert_replaced_404(response)

    def test_replaced_during_openai_exception_writes_no_failed_row(self):
        def raise_upstream():
            raise RuntimeError(self.UPSTREAM_ERROR)

        response = self._post_with_replacement_during_openai(raise_upstream)
        self._assert_replaced_404(response)

    # -- 4. replaced after the route's last check, before the guard commit --

    def _post_with_replacement_before_guard(self, b_guard=None):
        real_acquire = jobs.try_acquire_job_analysis_guard

        def replace_then_acquire(*args, **kwargs):
            self._replace_owner_elsewhere(with_b_analysis=False, b_guard=b_guard)
            return real_acquire(*args, **kwargs)

        with patch.object(
            jobs, "try_acquire_job_analysis_guard", side_effect=replace_then_acquire
        ) as mock_acquire, patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            response = self._post_analyze(self.job_id, headers=self.headers)

        mock_acquire.assert_called_once()
        return response, mock_openai

    def test_replaced_before_guard_acquires_nothing_and_sets_no_cooldown(self):
        response, mock_openai = self._post_with_replacement_before_guard()
        self._assert_replaced_404(response)
        mock_openai.assert_not_called()

    def test_replaced_before_guard_with_b_live_lease_is_404_not_409(self):
        response, mock_openai = self._post_with_replacement_before_guard(b_guard="lease")
        self._assert_replaced_404(response)
        mock_openai.assert_not_called()
        self.assertEqual(
            self._job_guard_row(self.job_id),
            [row for row in self.after_replacement["analysis_guards"]
             if row.operation_type == JOB_OPERATION_TYPE
             and row.resource_id == self.job_id][0],
        )

    def test_replaced_before_guard_with_b_cooldown_is_404_not_429(self):
        response, mock_openai = self._post_with_replacement_before_guard(b_guard="cooldown")
        self._assert_replaced_404(response)
        mock_openai.assert_not_called()
        self.assertEqual(
            self._job_guard_row(self.job_id),
            [row for row in self.after_replacement["analysis_guards"]
             if row.operation_type == JOB_OPERATION_TYPE
             and row.resource_id == self.job_id][0],
        )

    # -- 5. no window between the in-transaction check and the guard commit -

    def _post_with_delete_attempt_after_guard_precondition(self):
        """Wraps the REAL jobs._job_unchanged so that, right after it has
        passed inside the guard acquisition (the precondition, run on the
        guard session after its upsert), A's deletion is attempted from
        another connection with a short busy timeout. The upsert already
        took SQLite's RESERVED write lock, so the delete must fail with
        "database is locked" -- nothing can commit between the check and
        the guard's commit."""
        blocked_engine = create_engine(
            f"sqlite:///{self.engine.url.database}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        blocked_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=blocked_engine
        )
        real_acquire = jobs.try_acquire_job_analysis_guard
        real_job_unchanged = jobs._job_unchanged
        in_acquire = []
        outcomes = []

        def acquire_marking(*args, **kwargs):
            in_acquire.append(True)
            try:
                return real_acquire(*args, **kwargs)
            finally:
                in_acquire.pop()

        def job_unchanged_then_try_delete(db, **kwargs):
            result = real_job_unchanged(db, **kwargs)
            if in_acquire and result and not outcomes:
                try:
                    self._delete_owner_a(blocked_factory)
                    outcomes.append("delete committed")
                except OperationalError as exc:
                    outcomes.append("locked" if "locked" in str(exc) else repr(exc))
            return result

        try:
            with patch.object(
                jobs, "try_acquire_job_analysis_guard", side_effect=acquire_marking
            ), patch.object(
                jobs, "_job_unchanged", side_effect=job_unchanged_then_try_delete
            ), patch(
                "backend.routers.jobs._create_job_analysis_response",
                return_value=_mock_job_response(),
            ) as mock_openai:
                response = self._post_analyze(self.job_id, headers=self.headers)
        finally:
            # Disposed here, not via addCleanup (which runs after tearDown's
            # rmtree and would leave the temp dir behind on Windows).
            blocked_engine.dispose()
        return response, outcomes, mock_openai

    def _assert_owner_a_survived(self):
        self.assertEqual(self._token_key(self.owner_id), self.owner_token_key)
        self.assertEqual(
            [(row.job_id, row.url, row.created_by_user_id)
             for row in self._rows("jobs", "job_id") if row.job_id == self.job_id],
            [(self.job_id, self.job_url, self.owner_id)],
        )

    def test_delete_cannot_commit_between_guard_precondition_and_guard_commit(self):
        self.assertIsNone(self._job_guard_row(self.job_id))

        response, outcomes, mock_openai = (
            self._post_with_delete_attempt_after_guard_precondition()
        )

        self.assertEqual(outcomes, ["locked"])
        self._assert_owner_a_survived()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        mock_openai.assert_called_once()

    def test_write_lock_held_even_when_upsert_changes_no_row(self):
        # Another in-flight request of A holds a live lease, so the guard
        # upsert's WHERE is false and it changes nothing -- the INSERT still
        # began the write transaction and holds the RESERVED lock.
        result = self._acquire_job_guard_directly(self.job_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        guard_before = self._job_guard_row(self.job_id)

        response, outcomes, mock_openai = (
            self._post_with_delete_attempt_after_guard_precondition()
        )

        self.assertEqual(outcomes, ["locked"])
        self._assert_owner_a_survived()
        self.assertEqual(response.status_code, 409, response.text)
        mock_openai.assert_not_called()
        self.assertEqual(self._job_guard_row(self.job_id), guard_before)


class JobGuardPreconditionTests(_BaseOwnerManualAnalysisTestCase):
    """The optional precondition hook of try_acquire_job_analysis_guard:
    run on the guard session after the upsert, before the blocker
    evaluation and the commit."""

    def setUp(self):
        super().setUp()
        self.owner_id = self._create_user("precondition.owner@example.com")
        self.job_id = self._create_job(
            "precondition-manual", owner_id=self.owner_id, source="manual"
        )

    def _acquire(self, precondition):
        session = self.session_factory()
        try:
            return try_acquire_job_analysis_guard(
                session,
                job_id=self.job_id,
                config=load_job_analysis_config(),
                precondition=precondition,
            ), session
        finally:
            session.close()

    def test_true_grants_and_runs_on_a_separate_guard_session(self):
        seen = []
        result, caller_session = self._acquire(lambda s: seen.append(s) or True)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertTrue(result.owner_token)
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], caller_session)
        self.assertEqual(self._job_guard_row(self.job_id).owner_token, result.owner_token)

    def test_false_returns_target_changed_and_writes_nothing(self):
        result, _ = self._acquire(lambda s: False)
        self.assertEqual(result.outcome, AcquireOutcome.TARGET_CHANGED)
        self.assertIsNone(result.owner_token)
        self.assertIsNone(self._job_guard_row(self.job_id))

    def test_false_wins_over_active_lease_and_cooldown(self):
        for state in ("lease", "cooldown"):
            with self.subTest(state=state):
                now = time.time()
                session = self.session_factory()
                try:
                    session.query(models.AnalysisGuard).delete()
                    session.add(models.AnalysisGuard(
                        operation_type=JOB_OPERATION_TYPE,
                        resource_id=self.job_id,
                        owner_token="synthetic-other-owner" if state == "lease" else None,
                        lock_expires_at=now + 600 if state == "lease" else None,
                        cooldown_until=now + 600 if state == "cooldown" else None,
                    ))
                    session.commit()
                finally:
                    session.close()
                before = self._job_guard_row(self.job_id)

                result, _ = self._acquire(lambda s: False)
                self.assertEqual(result.outcome, AcquireOutcome.TARGET_CHANGED)
                self.assertIsNone(result.owner_token)
                self.assertEqual(self._job_guard_row(self.job_id), before)

    def test_raising_fails_closed_as_backend_unavailable_and_writes_nothing(self):
        def boom(session):
            raise RuntimeError("synthetic precondition failure")

        result, _ = self._acquire(boom)
        self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        self.assertIsNone(result.owner_token)
        self.assertIsNone(self._job_guard_row(self.job_id))

    def test_admin_route_passes_no_precondition_and_still_gets_409(self):
        admin_id = self._create_user("precondition.admin@example.com", is_admin=True)
        self.assertEqual(
            self._acquire_job_guard_directly(self.job_id).outcome,
            AcquireOutcome.GRANTED,
        )
        guard_before = self._job_guard_row(self.job_id)

        response, (mock_openai, _, mock_acquire) = self._post_with_seams_spied(
            self.job_id, self._auth_headers(admin_id)
        )

        self.assertEqual(response.status_code, 409)
        mock_openai.assert_not_called()
        mock_acquire.assert_called_once()
        self.assertNotIn("precondition", mock_acquire.call_args.kwargs)
        self.assertEqual(self._job_guard_row(self.job_id), guard_before)


if __name__ == "__main__":
    unittest.main()
