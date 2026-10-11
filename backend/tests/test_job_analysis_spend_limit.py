"""Provider spend-limit errors during job analysis.

When OpenAI rejects a job analysis with a 429 RateLimitError whose error
code says the project/organization spend or credit limit is exhausted,
POST /jobs/{job_id}/analyze returns a fixed 503
ERR_JOB_ANALYSIS_PROVIDER_UNAVAILABLE (no provider text, no code, no
job_id), writes no failed JobAnalysis row and releases its guards with the
failure cooldown. POST /jobs/analyze-missing stops at the first such error
and returns the same 503. Every other error (including a transient 429
"rate_limit_exceeded") keeps today's behavior.

Self-contained base (no TestCase imported from another test module).
Throwaway SQLite only; backend.app.main is never imported, apply101.db is
never touched and no network call is made: the fakes are built exactly the
way the openai SDK builds them, and the OpenAI seam
backend.routers.jobs._create_job_analysis_response is always patched.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
import openai
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
    JOB_BATCH_OPERATION_TYPE,
    JOB_BATCH_RESOURCE_ID,
    JOB_OPERATION_TYPE,
    JOB_USER_OPERATION_TYPE,
)
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password

# Same synthetic-key import pattern as test_owner_manual_job_analysis.py.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import jobs


SYNTHETIC_SECRET = "synthetic-test-secret-for-job-analysis-spend-limit-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"

SPEND_LIMIT_CODES = (
    "project_spend_limit_exceeded",
    "organization_spend_limit_exceeded",
    "credit_balance_exhausted",
    "organization_usage_limit_exceeded",
)

PROVIDER_UNAVAILABLE_BODY = {
    "detail": {
        "error_code": "ERR_JOB_ANALYSIS_PROVIDER_UNAVAILABLE",
        "message": "Job analysis is temporarily unavailable. Please try again later.",
    }
}
TOO_MANY_REQUESTS_BODY = {"detail": "Too many requests. Please try again later."}

# Default cooldowns (analysis_guard.load_job_analysis_config /
# load_job_analysis_batch_config).
JOB_FAILURE_COOLDOWN = 30
USER_FAILURE_COOLDOWN = 30
BATCH_FAILURE_COOLDOWN = 30
BATCH_SUCCESS_COOLDOWN = 60

# Exactly the fields of schemas.JobAnalysisStructured (extra="forbid"),
# copied from test_owner_manual_job_analysis.py.
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

_sentinel_counter = {"n": 0}


def _new_sentinel():
    _sentinel_counter["n"] += 1
    return f"SYNTHETIC-PROVIDER-TEXT-{_sentinel_counter['n']}-c0ffee"


def _mock_job_response(payload=None):
    response = MagicMock()
    response.output_text = json.dumps(payload or VALID_JOB_ANALYSIS_PAYLOAD)
    return response


_MISSING = object()


def _sdk_status_error(exc_class, status_code, code=_MISSING, sentinel=None):
    """Builds an openai status error exactly as the SDK does
    (_base_client._make_status_error_from_response + _client._make_status_error):
    the message embeds the whole decoded JSON body, and `body` (hence
    .code/.type/.param) is the INNER "error" object."""
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    err = {
        "message": sentinel or _new_sentinel(),
        "type": "insufficient_quota",
        "param": None,
    }
    if code is not _MISSING:
        err["code"] = code
    full_body = {"error": err}
    err_msg = f"Error code: {status_code} - {full_body}"
    response = httpx.Response(status_code, request=req, json=full_body)
    return exc_class(err_msg, response=response, body=err)


def _rate_limit_error(code=_MISSING, sentinel=None):
    return _sdk_status_error(openai.RateLimitError, 429, code=code, sentinel=sentinel)


class _BaseSpendLimitTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_job_spend_limit_test_")
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

    def _create_job(self, slug, owner_id=None, source=None):
        session = self.session_factory()
        try:
            job = models.Job(
                title=f"Synthetic Job {slug}",
                url=f"https://example.com/job/{slug}",
                description_text="synthetic description",
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

    def _auth_headers(self, user_id):
        session = self.session_factory()
        try:
            token_key = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one().token_key
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    # -- state (read straight from the temp SQLite file) -----------------

    def _analysis_rows(self, job_id=None):
        sql = (
            "SELECT analysis_id, job_id, analysis_status, analysis_json, "
            "analysis_error, is_current FROM job_analysis"
        )
        params = {}
        if job_id is not None:
            sql += " WHERE job_id = :jid"
            params["jid"] = job_id
        sql += " ORDER BY analysis_id"
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).fetchall()

    def _guard_row(self, operation_type, resource_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT owner_token, lock_expires_at, cooldown_until "
                    "FROM analysis_guards "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"op": operation_type, "rid": resource_id},
            ).fetchone()

    def _reservations(self, user_id):
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT id, operation_type, user_id, job_id, reserved_at "
                    "FROM analysis_quota_reservations WHERE user_id = :uid ORDER BY id"
                ),
                {"uid": user_id},
            ).fetchall()

    def _assert_released_with_cooldown(self, row, cooldown, before, after):
        """Exactly what _release_guard_resources writes: owner_token and
        lock_expires_at cleared, cooldown_until = release-time now + cooldown
        (release-time now lies between `before` and `after`)."""
        self.assertIsNotNone(row)
        self.assertIsNone(row.owner_token)
        self.assertIsNone(row.lock_expires_at)
        self.assertGreaterEqual(row.cooldown_until, before + cooldown)
        self.assertLessEqual(row.cooldown_until, after + cooldown)

    def _assert_no_sentinel_logged(self, records, sentinel):
        for record in records:
            rendered = record.getMessage()
            self.assertNotIn(sentinel, rendered)
            self.assertNotIn(sentinel, repr(record.args))
            self.assertIsNone(record.exc_info)


class SingleAnalyzeSpendLimitTests(_BaseSpendLimitTestCase):
    def _post_spend_limit(self, job_id, headers, code, force_reanalyze=False):
        sentinel = _new_sentinel()
        exc = _rate_limit_error(code=code, sentinel=sentinel)
        params = {"force_reanalyze": "true"} if force_reanalyze else None
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=exc,
        ) as mock_openai:
            before = time.time()
            with self.assertLogs(jobs.logger, level="DEBUG") as logs:
                response = self.client.post(
                    f"/jobs/{job_id}/analyze", params=params, headers=headers
                )
                # Inside the block so a wrong status is the reported failure.
                self.assertEqual(response.status_code, 503, response.text)
            after = time.time()
        return response, mock_openai, logs, sentinel, before, after

    def _assert_fixed_503(self, response, code, sentinel):
        self.assertEqual(response.json(), PROVIDER_UNAVAILABLE_BODY)
        self.assertNotIn(sentinel, response.text)
        self.assertNotIn(code, response.text)

    def test_owner_path_spend_limit_returns_fixed_503(self):
        for index, code in enumerate(SPEND_LIMIT_CODES):
            with self.subTest(code=code):
                owner_id = self._create_user(f"spend.owner.{index}@example.com")
                job_id = self._create_job(
                    f"spend-owner-{index}", owner_id=owner_id, source="manual"
                )
                rows_before = self._analysis_rows()
                self.assertEqual(self._reservations(owner_id), [])

                response, mock_openai, logs, sentinel, before, after = (
                    self._post_spend_limit(job_id, self._auth_headers(owner_id), code)
                )

                self._assert_fixed_503(response, code, sentinel)
                self.assertEqual(mock_openai.call_count, 1)
                self.assertEqual(self._analysis_rows(), rows_before)

                self._assert_released_with_cooldown(
                    self._guard_row(JOB_OPERATION_TYPE, job_id),
                    JOB_FAILURE_COOLDOWN, before, after,
                )
                self._assert_released_with_cooldown(
                    self._guard_row(JOB_USER_OPERATION_TYPE, owner_id),
                    USER_FAILURE_COOLDOWN, before, after,
                )
                # The quota slot reserved at acquisition is not refunded.
                reservations = self._reservations(owner_id)
                self.assertEqual(len(reservations), 1)
                self.assertEqual(reservations[0].job_id, job_id)
                self.assertEqual(reservations[0].operation_type, JOB_USER_OPERATION_TYPE)

                self._assert_no_sentinel_logged(logs.records, sentinel)
                self.assertTrue(
                    any(code in record.getMessage() for record in logs.records)
                )

    def test_admin_force_reanalyze_spend_limit_keeps_current_analysis(self):
        admin_id = self._create_user("spend.admin@example.com", is_admin=True)
        headers = self._auth_headers(admin_id)
        for index, code in enumerate(SPEND_LIMIT_CODES):
            with self.subTest(code=code):
                job_id = self._create_job(f"spend-admin-{index}")
                analysis_id = self._seed_completed_job_analysis(job_id)
                rows_before = self._analysis_rows()
                seeded_before = self._analysis_rows(job_id)

                response, mock_openai, logs, sentinel, before, after = (
                    self._post_spend_limit(job_id, headers, code, force_reanalyze=True)
                )

                self._assert_fixed_503(response, code, sentinel)
                self.assertEqual(mock_openai.call_count, 1)
                self.assertEqual(self._analysis_rows(), rows_before)
                seeded_after = self._analysis_rows(job_id)
                self.assertEqual(seeded_after, seeded_before)
                self.assertEqual(len(seeded_after), 1)
                self.assertEqual(seeded_after[0].analysis_id, analysis_id)
                self.assertTrue(seeded_after[0].is_current)
                self.assertEqual(
                    json.loads(seeded_after[0].analysis_json), VALID_JOB_ANALYSIS_PAYLOAD
                )

                self._assert_released_with_cooldown(
                    self._guard_row(JOB_OPERATION_TYPE, job_id),
                    JOB_FAILURE_COOLDOWN, before, after,
                )
                # Admins take no per-user resource and no quota slot.
                self.assertIsNone(self._guard_row(JOB_USER_OPERATION_TYPE, admin_id))
                self.assertEqual(self._reservations(admin_id), [])

                self._assert_no_sentinel_logged(logs.records, sentinel)

    def test_follow_up_request_gets_existing_cooldown_429(self):
        owner_id = self._create_user("spend.followup@example.com")
        job_id = self._create_job("spend-followup", owner_id=owner_id, source="manual")
        headers = self._auth_headers(owner_id)

        response, _mock, _logs, _sentinel, _before, _after = self._post_spend_limit(
            job_id, headers, "project_spend_limit_exceeded"
        )
        self.assertEqual(response.json(), PROVIDER_UNAVAILABLE_BODY)

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            return_value=_mock_job_response(),
        ) as mock_openai:
            follow_up = self.client.post(f"/jobs/{job_id}/analyze", headers=headers)

        self.assertEqual(follow_up.status_code, 429)
        self.assertEqual(follow_up.json(), TOO_MANY_REQUESTS_BODY)
        retry_after = int(follow_up.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, JOB_FAILURE_COOLDOWN)
        mock_openai.assert_not_called()


class SingleAnalyzeUnchangedErrorTests(_BaseSpendLimitTestCase):
    def _assert_today_500(self, exc):
        owner_id = self._create_user(f"unchanged.{id(exc)}@example.com")
        job_id = self._create_job(f"unchanged-{id(exc)}", owner_id=owner_id, source="manual")

        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=exc,
        ) as mock_openai:
            response = self.client.post(
                f"/jobs/{job_id}/analyze", headers=self._auth_headers(owner_id)
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "error_code": "ERR_JOB_ANALYSIS_FAILED",
                    "message": "Job analysis failed.",
                    "job_id": job_id,
                    "error": str(exc),
                }
            },
        )
        self.assertEqual(mock_openai.call_count, 1)
        rows = self._analysis_rows(job_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].analysis_status, "failed")
        self.assertEqual(rows[0].analysis_error, str(exc))
        self.assertFalse(rows[0].is_current)

    def test_transient_rate_limit_is_unchanged_500(self):
        self._assert_today_500(_rate_limit_error(code="rate_limit_exceeded"))

    def test_rate_limit_without_code_is_unchanged_500(self):
        self._assert_today_500(_rate_limit_error())


class AnalyzeMissingSpendLimitTests(_BaseSpendLimitTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self._create_user("spend.batch.admin@example.com", is_admin=True)
        self.headers = self._auth_headers(self.admin_id)
        # Catalog jobs (no owner). Selection order is job_id DESC, so
        # job3 is processed first, then job2, then job1.
        self.job1 = self._create_job("batch-1")
        self.job2 = self._create_job("batch-2")
        self.job3 = self._create_job("batch-3")

    def _post_batch(self, side_effect):
        with patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=side_effect,
        ) as mock_openai:
            before = time.time()
            response = self.client.post(
                "/jobs/analyze-missing?limit=3", headers=self.headers
            )
            after = time.time()
        return response, mock_openai, before, after

    def test_spend_limit_on_first_job_stops_batch_with_fixed_503(self):
        sentinel = _new_sentinel()
        exc = _rate_limit_error(code="organization_spend_limit_exceeded", sentinel=sentinel)

        response, mock_openai, before, after = self._post_batch(exc)

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), PROVIDER_UNAVAILABLE_BODY)
        self.assertNotIn(sentinel, response.text)
        self.assertEqual(mock_openai.call_count, 1)
        self.assertEqual(self._analysis_rows(), [])

        self._assert_released_with_cooldown(
            self._guard_row(JOB_OPERATION_TYPE, self.job3),
            JOB_FAILURE_COOLDOWN, before, after,
        )
        self.assertIsNone(self._guard_row(JOB_OPERATION_TYPE, self.job2))
        self.assertIsNone(self._guard_row(JOB_OPERATION_TYPE, self.job1))
        self._assert_released_with_cooldown(
            self._guard_row(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID),
            BATCH_FAILURE_COOLDOWN, before, after,
        )

    def test_spend_limit_mid_batch_keeps_earlier_commit_and_stops(self):
        exc = _rate_limit_error(code="credit_balance_exhausted")

        response, mock_openai, before, after = self._post_batch(
            [_mock_job_response(), exc]
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), PROVIDER_UNAVAILABLE_BODY)
        self.assertEqual(mock_openai.call_count, 2)

        rows = self._analysis_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].job_id, self.job3)
        self.assertEqual(rows[0].analysis_status, "completed")
        self.assertTrue(rows[0].is_current)
        self.assertEqual(
            [row for row in rows if row.analysis_status == "failed"], []
        )

        self._assert_released_with_cooldown(
            self._guard_row(JOB_OPERATION_TYPE, self.job2),
            JOB_FAILURE_COOLDOWN, before, after,
        )
        # job1 never reached: no guard row, no analysis row.
        self.assertIsNone(self._guard_row(JOB_OPERATION_TYPE, self.job1))
        self.assertEqual(self._analysis_rows(self.job1), [])
        self._assert_released_with_cooldown(
            self._guard_row(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID),
            BATCH_FAILURE_COOLDOWN, before, after,
        )

    def test_transient_rate_limit_keeps_todays_per_job_failures(self):
        excs = [_rate_limit_error(code="rate_limit_exceeded") for _ in range(3)]

        response, mock_openai, before, after = self._post_batch(excs)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "status", "requested_limit", "max_allowed_limit",
                "selected_job_count", "analyzed_count", "failed_count",
                "automatic_retry_blocked_count", "results",
            },
        )
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["selected_job_count"], 3)
        self.assertEqual(body["analyzed_count"], 0)
        self.assertEqual(body["failed_count"], 3)
        self.assertEqual(
            [result["job_id"] for result in body["results"]],
            [self.job3, self.job2, self.job1],
        )
        for result, exc in zip(body["results"], excs):
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["error_code"], "ERR_JOB_ANALYSIS_FAILED")
            self.assertEqual(result["error"]["error"], str(exc))
        self.assertEqual(mock_openai.call_count, 3)

        rows = self._analysis_rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.analysis_status == "failed" for row in rows))
        self._assert_released_with_cooldown(
            self._guard_row(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID),
            BATCH_SUCCESS_COOLDOWN, before, after,
        )


class ProviderSpendLimitClassifierTests(unittest.TestCase):
    def _classifier(self):
        from backend.app.openai_client import provider_spend_limit_error_code

        return provider_spend_limit_error_code

    def test_returns_each_spend_limit_code(self):
        classify = self._classifier()
        for code in SPEND_LIMIT_CODES:
            with self.subTest(code=code):
                self.assertEqual(classify(_rate_limit_error(code=code)), code)

    def test_returns_none_for_other_rate_limit_errors(self):
        classify = self._classifier()
        cases = {
            "rate_limit_exceeded": _rate_limit_error(code="rate_limit_exceeded"),
            "missing code": _rate_limit_error(),
            "code None": _rate_limit_error(code=None),
            # construct_type passes a non-str code through unchanged.
            "dict code": _rate_limit_error(code={"x": 1}),
            "int code": _rate_limit_error(code=123),
        }
        self.assertIsInstance(cases["dict code"].code, dict)
        for name, exc in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(classify(exc))

    def test_returns_none_for_spend_limit_codes_on_non_429(self):
        classify = self._classifier()
        for code in SPEND_LIMIT_CODES:
            for exc_class, status in (
                (openai.BadRequestError, 400),
                (openai.PermissionDeniedError, 403),
                # A RateLimitError instance carrying a non-429 status.
                (openai.RateLimitError, 400),
            ):
                with self.subTest(code=code, exc_class=exc_class.__name__, status=status):
                    exc = _sdk_status_error(exc_class, status, code=code)
                    self.assertEqual(exc.code, code)
                    self.assertIsNone(classify(exc))

    def test_returns_none_for_non_sdk_objects(self):
        classify = self._classifier()

        class _CodedError(Exception):
            pass

        coded_exception = _CodedError("synthetic")
        coded_exception.code = "project_spend_limit_exceeded"
        coded_exception.status_code = 429

        coded_object = MagicMock()
        coded_object.code = "project_spend_limit_exceeded"
        coded_object.status_code = 429

        for name, value in (
            ("RuntimeError", RuntimeError("project_spend_limit_exceeded")),
            ("exception with .code", coded_exception),
            ("plain object with .code", coded_object),
        ):
            with self.subTest(case=name):
                self.assertIsNone(classify(value))


class ProviderUnavailableExceptionTests(unittest.TestCase):
    def test_is_a_503_http_exception_with_a_fresh_fixed_detail(self):
        from fastapi import HTTPException

        exc_class = getattr(jobs, "_JobAnalysisProviderUnavailableError")
        first = exc_class()
        second = exc_class()
        self.assertIsInstance(first, HTTPException)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.detail, PROVIDER_UNAVAILABLE_BODY["detail"])
        self.assertIsNot(first.detail, second.detail)


if __name__ == "__main__":
    unittest.main()
