"""Provider and model-output text never reaches responses, the database or
logs in profile analysis, job analysis (single, analyze-missing,
analyze-sample) and cover letters.

Every case puts a sentinel (a fake key-like string and a fake email) into
the SDK exception (message, body, request URL or chained cause) or into the
model output, and checks that it is absent from the response, from every
table of the throwaway database and from every log record. Failures store
and return the fixed openai_client.ANALYSIS_ERROR_* messages. Profile
analysis now answers an exhausted provider spend limit with a fixed 503,
like job analysis.

Throwaway SQLite only; backend.app.main is never imported, apply101.db is
never touched and no network call is made: the SDK exceptions are built the
way the openai SDK builds them, and the provider seams are always patched.
"""

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import traceback
import unittest
from unittest.mock import MagicMock, patch

import httpx
import openai
from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app import models
from backend.app.analysis_contract import (
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)
from backend.app.analysis_guard import (
    JOB_OPERATION_TYPE,
    JOB_USER_OPERATION_TYPE,
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
)
from backend.app.database import Base, get_db
from backend.app.fake_analysis import FakeAnalysisClient
from backend.app.openai_client import (
    ANALYSIS_ERROR_INTERNAL,
    ANALYSIS_ERROR_INVALID_FORMAT,
    ANALYSIS_ERROR_INVALID_JSON,
    ANALYSIS_ERROR_PROVIDER_CREDENTIALS,
    ANALYSIS_ERROR_PROVIDER_FAILED,
    ANALYSIS_ERROR_PROVIDER_RATE_LIMITED,
    ANALYSIS_ERROR_PROVIDER_STATUS,
    ANALYSIS_ERROR_PROVIDER_TIMEOUT,
    ANALYSIS_ERROR_PROVIDER_UNREACHABLE,
    PROVIDER_SPEND_LIMIT_ERROR_CODES,
    detached_from,
    safe_analysis_error_message,
)
from backend.app.security import create_access_token, hash_password

# Synthetic key only while importing (the routers build their client at
# import time); every provider-reaching call below is patched.
with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"},
    clear=False,
):
    from backend.routers import cover_letters, jobs, profiles


SYNTHETIC_SECRET = "synthetic-test-secret-for-safe-provider-errors-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"

ALL_ANALYSIS_ERROR_MESSAGES = (
    ANALYSIS_ERROR_INVALID_JSON,
    ANALYSIS_ERROR_INVALID_FORMAT,
    ANALYSIS_ERROR_PROVIDER_TIMEOUT,
    ANALYSIS_ERROR_PROVIDER_UNREACHABLE,
    ANALYSIS_ERROR_PROVIDER_RATE_LIMITED,
    ANALYSIS_ERROR_PROVIDER_CREDENTIALS,
    ANALYSIS_ERROR_PROVIDER_STATUS,
    ANALYSIS_ERROR_PROVIDER_FAILED,
    ANALYSIS_ERROR_INTERNAL,
)

# Default failure cooldowns (analysis_guard.load_config and
# load_job_analysis_config).
PROFILE_FAILURE_COOLDOWN = 30
PROFILE_USER_FAILURE_COOLDOWN = 30
JOB_FAILURE_COOLDOWN = 30
JOB_USER_FAILURE_COOLDOWN = 30

PROFILE_PROVIDER_UNAVAILABLE_BODY = {
    "detail": {
        "error_code": "ERR_PROFILE_ANALYSIS_PROVIDER_UNAVAILABLE",
        "message": "Profile analysis is temporarily unavailable. Please try again later.",
    }
}
COVER_LETTER_PROVIDER_UNAVAILABLE_BODY = {
    "detail": {
        "error_code": "ERR_COVER_LETTER_PROVIDER_UNAVAILABLE",
        "message": "Letter drafting is temporarily unavailable. Please try again later.",
    }
}
COVER_LETTER_FAILED_BODY = {
    "detail": {
        "error_code": "ERR_COVER_LETTER_FAILED",
        "message": "The draft could not be written. Please try again.",
    }
}
TOO_MANY_REQUESTS_BODY = {"detail": "Too many requests. Please try again later."}

# Exactly the fields of schemas.ProfileAnalysisStructured (extra="forbid"),
# copied from test_inflight_profile_delete_writes.py.
VALID_PROFILE_ANALYSIS_PAYLOAD = {
    "candidate_summary": "synthetic summary",
    "current_role_family": "other",
    "target_role_families": ["other"],
    "target_role_tags": ["other"],
    "target_roles": [],
    "excluded_roles": [],
    "strong_skills": [],
    "moderate_skills": [],
    "weak_or_basic_skills": [],
    "tools": [],
    "industries": [],
    "years_of_experience": None,
    "seniority_level": "unknown",
    "education_level": "unknown",
    "field_of_study": "unknown",
    "languages": [],
    "visa_sponsorship_needed": "unknown",
    "work_authorization_status": "unknown",
    "relocation_preference": "unknown",
    "current_residence_country": "unknown",
    "student_status": "unknown",
    "match_notes": [],
}

# Exactly the fields of schemas.JobAnalysisStructured (extra="forbid"),
# copied from test_job_analysis_spend_limit.py.
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


# -- sentinels and SDK errors ---------------------------------------------

_sentinel_counter = {"n": 0}


class Sentinel:
    """A fake key-like string plus a fake email; `text` carries both."""

    def __init__(self):
        _sentinel_counter["n"] += 1
        n = _sentinel_counter["n"]
        self.key = f"sk-proj-SENTINEL{n}Qx7Zr4Kp9Wm2"
        self.mail = f"leak.sentinel{n}@private-sentinel.example"
        self.text = f"Incorrect API key provided: {self.key} (contact {self.mail})"
        self.markers = (self.key, self.mail, "SENTINEL", "private-sentinel")


_MISSING = object()


def _request(sentinel):
    # The request URL carries the sentinel too: it is part of the SDK
    # exception (exc.request), never of the fixed messages.
    return httpx.Request(
        "POST", f"https://api.openai.com/v1/responses?trace={sentinel.key}"
    )


def _sdk_status_error(exc_class, status_code, sentinel, code=_MISSING):
    """As the SDK builds it (_base_client._make_status_error_from_response):
    the message embeds the decoded JSON body, `body` is the inner "error"
    object."""
    err = {"message": sentinel.text, "type": "invalid_request_error", "param": None}
    if code is not _MISSING:
        err["code"] = code
    full_body = {"error": err}
    response = httpx.Response(status_code, request=_request(sentinel), json=full_body)
    return exc_class(f"Error code: {status_code} - {full_body}", response=response, body=err)


def _connection_error(sentinel):
    exc = openai.APIConnectionError(message=sentinel.text, request=_request(sentinel))
    exc.__cause__ = httpx.ConnectError(sentinel.text)
    return exc


def _timeout_error(sentinel):
    # The SDK's message is fixed ("Request timed out."); the sentinel sits
    # in the request and in the chained httpx error, as `raise ... from err`.
    exc = openai.APITimeoutError(request=_request(sentinel))
    exc.__cause__ = httpx.ReadTimeout(sentinel.text)
    return exc


def _provider_error_cases():
    """(name, sentinel -> exception, expected fixed message)."""
    return (
        ("AuthenticationError",
         lambda s: _sdk_status_error(openai.AuthenticationError, 401, s, code="invalid_api_key"),
         ANALYSIS_ERROR_PROVIDER_CREDENTIALS),
        # 403 can also mean an unsupported region or no model access.
        ("PermissionDeniedError",
         lambda s: _sdk_status_error(openai.PermissionDeniedError, 403, s),
         ANALYSIS_ERROR_PROVIDER_STATUS),
        ("BadRequestError",
         lambda s: _sdk_status_error(openai.BadRequestError, 400, s),
         ANALYSIS_ERROR_PROVIDER_STATUS),
        ("InternalServerError",
         lambda s: _sdk_status_error(openai.InternalServerError, 500, s),
         ANALYSIS_ERROR_PROVIDER_STATUS),
        ("RateLimitError rate_limit_exceeded",
         lambda s: _sdk_status_error(openai.RateLimitError, 429, s, code="rate_limit_exceeded"),
         ANALYSIS_ERROR_PROVIDER_RATE_LIMITED),
        ("APIConnectionError", _connection_error, ANALYSIS_ERROR_PROVIDER_UNREACHABLE),
        ("APITimeoutError", _timeout_error, ANALYSIS_ERROR_PROVIDER_TIMEOUT),
        ("RuntimeError", lambda s: RuntimeError(s.text), ANALYSIS_ERROR_INTERNAL),
    )


def _raise_database_locked():
    """Raises an OperationalError the way SQLAlchemy does: the DBAPI error
    is raised first (so, inside an `except` block, it picks up the handled
    exception as its __context__) and becomes the __cause__."""
    try:
        raise sqlite3.OperationalError("database is locked")
    except sqlite3.OperationalError as dbapi_exc:
        raise OperationalError("COMMIT", {}, dbapi_exc) from dbapi_exc


def _output(output_text):
    response = MagicMock()
    response.output_text = output_text
    return response


def _invalid_json_output(sentinel):
    return _output(f'{{"candidate_summary": "{sentinel.text}", oops')


def _schema_violation_output(payload, field, sentinel):
    # Valid JSON whose `field` has the wrong type (a list) and carries the
    # sentinel key: pydantic's error text would echo it as input_value
    # (short enough not to be truncated there).
    return _output(json.dumps({**payload, field: [sentinel.key]}))


# -- log capture --------------------------------------------------------------


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _capture_root_logs():
    """Every record reaching the root logger, at DEBUG."""
    root = logging.getLogger()
    handler = _RecordingHandler()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


class _SafeErrorsBase(unittest.TestCase):
    routers = ()

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_safe_provider_errors_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        for router in self.routers:
            app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env_patcher = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- data helpers ---------------------------------------------------------

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

    def _auth_headers(self, user_id):
        session = self.session_factory()
        try:
            token_key = session.get(models.User, user_id).token_key
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    def _create_profile(self, user_id):
        session = self.session_factory()
        try:
            profile = models.CandidateProfile(
                user_id=user_id,
                profile_name="Synthetic profile",
                target_role="Backend Developer",
                cv_text="Python developer at Gamma GmbH since 2023.",
            )
            session.add(profile)
            session.commit()
            return profile.profile_id
        finally:
            session.close()

    def _create_job(self, slug, owner_id=None, source=None):
        session = self.session_factory()
        try:
            job = models.Job(
                title=f"Synthetic Job {slug}",
                company_name="Acme",
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

    # -- state, read straight from the temp SQLite file ------------------------

    def _rows(self, sql, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).fetchall()

    def _guard_row(self, operation_type, resource_id):
        rows = self._rows(
            "SELECT owner_token, lock_expires_at, cooldown_until FROM analysis_guards "
            "WHERE operation_type = :op AND resource_id = :rid",
            op=operation_type, rid=resource_id,
        )
        return rows[0] if rows else None

    def _assert_released_with_cooldown(self, row, cooldown, before, after):
        self.assertIsNotNone(row)
        self.assertIsNone(row.owner_token)
        self.assertIsNone(row.lock_expires_at)
        self.assertGreaterEqual(row.cooldown_until, before + cooldown)
        self.assertLessEqual(row.cooldown_until, after + cooldown)

    def _assert_database_writable(self):
        # Sanity check only: closing the request's session also releases the
        # lock. The guard rows released with the failure cooldown are what
        # shows the route rolled back before the release in `finally`.
        connection = sqlite3.connect(self.db_path, timeout=1, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    # -- sentinel checks --------------------------------------------------------

    def _assert_no_sentinel_in_traceback(self, exc, sentinel):
        rendered = "".join(traceback.format_exception(exc))
        self.assertIn("database is locked", rendered)  # the real error stays
        self._assert_no_sentinel_in(rendered, sentinel, "the server traceback")

    def _record_answered_http_exceptions(self):
        """Every HTTPException the app answers, recorded and then answered as
        usual. Call before the test's first request."""
        answered = []

        async def record(request, exc):
            answered.append(exc)
            return await http_exception_handler(request, exc)

        self.client.app.add_exception_handler(StarletteHTTPException, record)
        return answered

    def _assert_failure_does_not_chain(self, answered, sentinel):
        # A later failure that picks this response's exception up as its
        # context (e.g. a failing rollback in analyze-missing's loop) would
        # print the whole chain in a server traceback.
        self.assertEqual(len(answered), 1)
        self._assert_no_sentinel_in(
            "".join(traceback.format_exception(answered[0])), sentinel,
            "the failure's exception chain",
        )

    def _assert_no_sentinel_in(self, value, sentinel, where):
        for marker in sentinel.markers:
            self.assertNotIn(marker, value, f"sentinel found in {where}")

    def _assert_no_sentinel_in_database(self, sentinel):
        with self.engine.connect() as connection:
            for table in inspect(connection).get_table_names():
                rows = connection.execute(text(f'SELECT * FROM "{table}"')).fetchall()
                self._assert_no_sentinel_in(repr(rows), sentinel, f"table {table}")

    def _assert_no_sentinel_in_logs(self, records, sentinel):
        formatter = logging.Formatter()
        for record in records:
            self._assert_no_sentinel_in(record.getMessage(), sentinel, "a log message")
            self._assert_no_sentinel_in(repr(record.args), sentinel, "log args")
            if record.exc_info:
                self._assert_no_sentinel_in(
                    formatter.formatException(record.exc_info), sentinel, "a logged traceback"
                )

    def _assert_no_sentinel_anywhere(self, response, records, sentinel):
        self._assert_no_sentinel_in(response.text, sentinel, "the response")
        self._assert_no_sentinel_in(repr(dict(response.headers)), sentinel, "the headers")
        self._assert_no_sentinel_in_database(sentinel)
        self._assert_no_sentinel_in_logs(records, sentinel)


class SafeAnalysisErrorMessageTests(unittest.TestCase):
    def test_each_exception_class_maps_to_its_fixed_message(self):
        for name, build, expected in _provider_error_cases():
            with self.subTest(case=name):
                sentinel = Sentinel()
                exc = build(sentinel)
                message = safe_analysis_error_message(exc)
                self.assertEqual(message, expected)
                for marker in sentinel.markers:
                    self.assertNotIn(marker, message)

    def test_sentinels_really_sit_in_the_exceptions(self):
        # Guards the cases above: str(exc), the request or the chained
        # cause would leak them.
        for name, build, _expected in _provider_error_cases():
            with self.subTest(case=name):
                sentinel = Sentinel()
                exc = build(sentinel)
                carried = " ".join([
                    str(exc),
                    str(getattr(exc, "request", "")),
                    str(getattr(exc, "request", None) and exc.request.url),
                    str(exc.__cause__),
                ])
                self.assertIn(sentinel.key, carried)

    def test_model_output_errors(self):
        sentinel = Sentinel()
        with self.assertRaises(json.JSONDecodeError) as decode_ctx:
            json.loads(_invalid_json_output(sentinel).output_text)
        self.assertEqual(safe_analysis_error_message(decode_ctx.exception), ANALYSIS_ERROR_INVALID_JSON)

        from backend.app import schemas

        payload = json.loads(
            _schema_violation_output(VALID_JOB_ANALYSIS_PAYLOAD, "summary", sentinel).output_text
        )
        with self.assertRaises(ValidationError) as validation_ctx:
            schemas.JobAnalysisStructured.model_validate(payload)
        self.assertIn(sentinel.key, str(validation_ctx.exception))  # would leak
        self.assertEqual(
            safe_analysis_error_message(validation_ctx.exception), ANALYSIS_ERROR_INVALID_FORMAT
        )

    def test_other_sdk_and_python_errors(self):
        sentinel = Sentinel()
        cases = (
            (_sdk_status_error(openai.UnprocessableEntityError, 422, sentinel), ANALYSIS_ERROR_PROVIDER_STATUS),
            (_sdk_status_error(openai.APIStatusError, 418, sentinel), ANALYSIS_ERROR_PROVIDER_STATUS),
            (openai.OpenAIError(sentinel.text), ANALYSIS_ERROR_PROVIDER_FAILED),
            (openai.APIError(sentinel.text, _request(sentinel), body=None), ANALYSIS_ERROR_PROVIDER_FAILED),
            (ValueError(sentinel.text), ANALYSIS_ERROR_INTERNAL),
            (KeyError(sentinel.text), ANALYSIS_ERROR_INTERNAL),
        )
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(safe_analysis_error_message(exc), expected)

    def test_detached_from_cuts_the_original_out_of_the_chain(self):
        sentinel = Sentinel()
        original = _sdk_status_error(openai.AuthenticationError, 401, sentinel)
        with self.assertRaises(OperationalError) as ctx:
            try:
                raise original
            except openai.AuthenticationError as e:
                with detached_from(e):
                    _raise_database_locked()
        raised = ctx.exception
        rendered = "".join(traceback.format_exception(raised))
        for marker in sentinel.markers:
            self.assertNotIn(marker, rendered)
        # The real error and its own DBAPI cause are kept.
        self.assertIn("database is locked", rendered)
        self.assertIsInstance(raised.__cause__, sqlite3.OperationalError)
        self.assertIsNone(raised.__cause__.__context__)

        # A block that does not raise is untouched.
        with detached_from(original):
            value = 1
        self.assertEqual(value, 1)

    def test_no_fixed_message_is_an_automatically_retried_failure(self):
        for message in ALL_ANALYSIS_ERROR_MESSAGES:
            with self.subTest(message=message):
                self.assertFalse(jobs.is_recoverable_analysis_failure(message))
        # The historical markers still are.
        self.assertTrue(jobs.is_recoverable_analysis_failure("Invalid role family: x"))


class ProfileAnalysisSafeErrorTests(_SafeErrorsBase):
    routers = (profiles.router,)

    def setUp(self):
        super().setUp()
        client_patcher = patch.object(profiles, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def _new_owner(self):
        user_id = self._create_user(f"profile.owner.{time.time_ns()}@example.com")
        return user_id, self._create_profile(user_id), self._auth_headers(user_id)

    def _analyze(self, user_id, profile_id, headers, params=None, **seam):
        with patch(
            "backend.routers.profiles._create_profile_analysis_response", **seam
        ) as mock_openai, _capture_root_logs() as records:
            before = time.time()
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=headers, params=params,
            )
            after = time.time()
        return response, mock_openai, records, before, after

    def _analysis_rows(self, profile_id):
        return self._rows(
            "SELECT analysis_status, analysis_json, analysis_error, is_current "
            "FROM profile_analysis WHERE profile_id = :pid",
            pid=profile_id,
        )

    def _assert_failed_500(self, sentinel, expected_error, **seam):
        user_id, profile_id, headers = self._new_owner()
        response, mock_openai, records, before, after = self._analyze(
            user_id, profile_id, headers, **seam
        )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json(), {"detail": {
            "error_code": "ERR_PROFILE_ANALYSIS_FAILED",
            "message": "Profile analysis failed.",
            "profile_id": profile_id,
            "error": expected_error,
        }})
        self.assertEqual(mock_openai.call_count, 1)
        rows = self._analysis_rows(profile_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].analysis_status, "failed")
        self.assertIsNone(rows[0].analysis_json)
        self.assertEqual(rows[0].analysis_error, expected_error)
        self.assertFalse(rows[0].is_current)
        self._assert_released_with_cooldown(
            self._guard_row(PROFILE_OPERATION_TYPE, profile_id), PROFILE_FAILURE_COOLDOWN, before, after
        )
        self._assert_released_with_cooldown(
            self._guard_row(USER_OPERATION_TYPE, user_id), PROFILE_USER_FAILURE_COOLDOWN, before, after
        )
        self._assert_no_sentinel_anywhere(response, records, sentinel)

    def test_provider_errors_store_and_return_fixed_messages(self):
        for name, build, expected in _provider_error_cases():
            with self.subTest(case=name):
                sentinel = Sentinel()
                self._assert_failed_500(sentinel, expected, side_effect=build(sentinel))

    def test_invalid_json_output(self):
        sentinel = Sentinel()
        self._assert_failed_500(
            sentinel, ANALYSIS_ERROR_INVALID_JSON, return_value=_invalid_json_output(sentinel)
        )

    def test_schema_violating_output(self):
        sentinel = Sentinel()
        self._assert_failed_500(
            sentinel,
            ANALYSIS_ERROR_INVALID_FORMAT,
            return_value=_schema_violation_output(
                VALID_PROFILE_ANALYSIS_PAYLOAD, "candidate_summary", sentinel
            ),
        )

    def test_spend_limit_returns_fixed_503_and_writes_no_row(self):
        for code in sorted(PROVIDER_SPEND_LIMIT_ERROR_CODES):
            with self.subTest(code=code):
                sentinel = Sentinel()
                user_id, profile_id, headers = self._new_owner()
                exc = _sdk_status_error(openai.RateLimitError, 429, sentinel, code=code)

                response, mock_openai, records, before, after = self._analyze(
                    user_id, profile_id, headers, side_effect=exc
                )

                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json(), PROFILE_PROVIDER_UNAVAILABLE_BODY)
                self.assertNotIn(code, response.text)
                self.assertEqual(mock_openai.call_count, 1)
                self.assertEqual(self._analysis_rows(profile_id), [])
                self._assert_released_with_cooldown(
                    self._guard_row(PROFILE_OPERATION_TYPE, profile_id),
                    PROFILE_FAILURE_COOLDOWN, before, after,
                )
                self._assert_released_with_cooldown(
                    self._guard_row(USER_OPERATION_TYPE, user_id),
                    PROFILE_USER_FAILURE_COOLDOWN, before, after,
                )
                self._assert_no_sentinel_anywhere(response, records, sentinel)
                self.assertTrue(any(
                    record.name == profiles.logger.name and code in record.getMessage()
                    for record in records
                ))

                # The failure cooldown answers the next request, before
                # the provider is reached.
                follow_up, follow_up_mock, _records, _b, _a = self._analyze(
                    user_id, profile_id, headers,
                    return_value=_output(json.dumps(VALID_PROFILE_ANALYSIS_PAYLOAD)),
                )
                self.assertEqual(follow_up.status_code, 429)
                self.assertEqual(follow_up.json(), TOO_MANY_REQUESTS_BODY)
                retry_after = int(follow_up.headers["Retry-After"])
                self.assertGreaterEqual(retry_after, 1)
                self.assertLessEqual(retry_after, PROFILE_FAILURE_COOLDOWN)
                follow_up_mock.assert_not_called()
                self.assertEqual(self._analysis_rows(profile_id), [])

    def test_failure_500_does_not_chain_provider_text(self):
        sentinel = Sentinel()
        user_id, profile_id, headers = self._new_owner()
        answered = self._record_answered_http_exceptions()

        response, _mock, _records, _before, _after = self._analyze(
            user_id, profile_id, headers,
            side_effect=_sdk_status_error(openai.AuthenticationError, 401, sentinel),
        )

        self.assertEqual(response.status_code, 500, response.text)
        self._assert_failure_does_not_chain(answered, sentinel)

    def test_force_reanalyze_spend_limit_keeps_current_analysis(self):
        sentinel = Sentinel()
        user_id, profile_id, headers = self._new_owner()
        session = self.session_factory()
        try:
            session.add(models.ProfileAnalysis(
                profile_id=profile_id,
                analysis_status="completed",
                analysis_json=json.dumps(VALID_PROFILE_ANALYSIS_PAYLOAD),
                analysis_model=PROFILE_ANALYSIS_MODEL,
                analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
                is_current=True,
            ))
            session.commit()
        finally:
            session.close()
        rows_before = self._rows("SELECT * FROM profile_analysis")
        self.assertEqual(len(rows_before), 1)

        response, mock_openai, records, before, after = self._analyze(
            user_id, profile_id, headers,
            params={"force_reanalyze": "true"},
            side_effect=_sdk_status_error(
                openai.RateLimitError, 429, sentinel, code="project_spend_limit_exceeded"
            ),
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), PROFILE_PROVIDER_UNAVAILABLE_BODY)
        self.assertEqual(mock_openai.call_count, 1)
        # Same single row, every column unchanged, still current.
        self.assertEqual(self._rows("SELECT * FROM profile_analysis"), rows_before)
        self.assertEqual(
            [(row.analysis_status, row.is_current) for row in self._analysis_rows(profile_id)],
            [("completed", True)],
        )
        self._assert_released_with_cooldown(
            self._guard_row(PROFILE_OPERATION_TYPE, profile_id), PROFILE_FAILURE_COOLDOWN, before, after
        )
        self._assert_no_sentinel_anywhere(response, records, sentinel)

    def test_failed_row_write_error_does_not_chain_provider_text(self):
        sentinel = Sentinel()
        user_id, profile_id, headers = self._new_owner()
        real_verify = profiles.verify_profile_owner_unchanged_or_404

        def verify_then_fail(db, identity):
            real_verify(db, identity)  # flushes: this connection now holds the write lock
            _raise_database_locked()

        with patch.object(
            profiles, "verify_profile_owner_unchanged_or_404", side_effect=verify_then_fail
        ) as mock_verify, patch(
            "backend.routers.profiles._create_profile_analysis_response",
            side_effect=_sdk_status_error(openai.AuthenticationError, 401, sentinel),
        ), _capture_root_logs() as records:
            before = time.time()
            # Still an unhandled 500, as before (TestClient re-raises it).
            with self.assertRaises(OperationalError) as ctx:
                self.client.post(
                    f"/users/{user_id}/profiles/{profile_id}/analyze", headers=headers
                )
            after = time.time()

        self.assertEqual(mock_verify.call_count, 1)
        self._assert_no_sentinel_in_traceback(ctx.exception, sentinel)
        self.assertEqual(self._analysis_rows(profile_id), [])
        # Rolled back before the guard release: released with the failure
        # cooldown, and the database is writable again.
        self._assert_released_with_cooldown(
            self._guard_row(PROFILE_OPERATION_TYPE, profile_id), PROFILE_FAILURE_COOLDOWN, before, after
        )
        self._assert_released_with_cooldown(
            self._guard_row(USER_OPERATION_TYPE, user_id), PROFILE_USER_FAILURE_COOLDOWN, before, after
        )
        self._assert_database_writable()
        self._assert_no_sentinel_in_database(sentinel)
        self._assert_no_sentinel_in_logs(records, sentinel)


class JobAnalysisSafeErrorTests(_SafeErrorsBase):
    routers = (jobs.router,)

    def setUp(self):
        super().setUp()
        client_patcher = patch.object(jobs, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.admin_id = self._create_user("job.admin@example.com", is_admin=True)
        self.admin_headers = self._auth_headers(self.admin_id)

    def _analysis_rows(self, job_id=None):
        sql = "SELECT job_id, analysis_status, analysis_json, analysis_error, is_current FROM job_analysis"
        if job_id is None:
            return self._rows(sql)
        return self._rows(sql + " WHERE job_id = :jid", jid=job_id)

    def _owner_and_job(self):
        n = time.time_ns()
        owner_id = self._create_user(f"job.owner.{n}@example.com")
        job_id = self._create_job(f"owned-{n}", owner_id=owner_id, source="manual")
        return owner_id, job_id, self._auth_headers(owner_id)

    def _analyze(self, job_id, headers, **seam):
        with patch(
            "backend.routers.jobs._create_job_analysis_response", **seam
        ) as mock_openai, _capture_root_logs() as records:
            before = time.time()
            response = self.client.post(f"/jobs/{job_id}/analyze", headers=headers)
            after = time.time()
        return response, mock_openai, records, before, after

    def _assert_failed(self, sentinel, expected_detail_extra, expected_error, **seam):
        owner_id, job_id, headers = self._owner_and_job()
        response, mock_openai, records, before, after = self._analyze(job_id, headers, **seam)

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json(), {"detail": {**expected_detail_extra, "job_id": job_id}})
        self.assertEqual(mock_openai.call_count, 1)
        rows = self._analysis_rows(job_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].analysis_status, "failed")
        self.assertIsNone(rows[0].analysis_json)
        self.assertEqual(rows[0].analysis_error, expected_error)
        self.assertFalse(rows[0].is_current)
        self._assert_released_with_cooldown(
            self._guard_row(JOB_OPERATION_TYPE, job_id), JOB_FAILURE_COOLDOWN, before, after
        )
        self._assert_released_with_cooldown(
            self._guard_row(JOB_USER_OPERATION_TYPE, owner_id), JOB_USER_FAILURE_COOLDOWN, before, after
        )
        self._assert_no_sentinel_anywhere(response, records, sentinel)

    def test_provider_errors_store_and_return_fixed_messages(self):
        for name, build, expected in _provider_error_cases():
            with self.subTest(case=name):
                sentinel = Sentinel()
                self._assert_failed(
                    sentinel,
                    {
                        "error_code": "ERR_JOB_ANALYSIS_FAILED",
                        "message": "Job analysis failed.",
                        "error": expected,
                    },
                    expected,
                    side_effect=build(sentinel),
                )

    def test_invalid_json_output_has_no_raw_response(self):
        sentinel = Sentinel()
        self._assert_failed(
            sentinel,
            {"error_code": "ERR_AI_INVALID_JSON", "message": "AI response was not valid JSON."},
            ANALYSIS_ERROR_INVALID_JSON,
            return_value=_invalid_json_output(sentinel),
        )

    def test_schema_violating_output(self):
        sentinel = Sentinel()
        self._assert_failed(
            sentinel,
            {
                "error_code": "ERR_JOB_ANALYSIS_FAILED",
                "message": "Job analysis failed.",
                "error": ANALYSIS_ERROR_INVALID_FORMAT,
            },
            ANALYSIS_ERROR_INVALID_FORMAT,
            return_value=_schema_violation_output(VALID_JOB_ANALYSIS_PAYLOAD, "summary", sentinel),
        )

    def test_failure_500_does_not_chain_provider_text(self):
        # The same 500 reaches analyze-missing's loop, whose rollback runs
        # while handling it.
        sentinel = Sentinel()
        _owner_id, job_id, headers = self._owner_and_job()
        answered = self._record_answered_http_exceptions()

        response, _mock, _records, _before, _after = self._analyze(
            job_id, headers,
            side_effect=_sdk_status_error(openai.AuthenticationError, 401, sentinel),
        )

        self.assertEqual(response.status_code, 500, response.text)
        self._assert_failure_does_not_chain(answered, sentinel)

    def test_failed_row_write_error_does_not_chain_provider_text(self):
        sentinel = Sentinel()
        owner_id, job_id, headers = self._owner_and_job()
        real_verify = jobs._verify_job_unchanged_or_404

        def verify_then_fail(db, **kwargs):
            real_verify(db, **kwargs)  # flushes: this connection now holds the write lock
            _raise_database_locked()

        with patch.object(
            jobs, "_verify_job_unchanged_or_404", side_effect=verify_then_fail
        ) as mock_verify, patch(
            "backend.routers.jobs._create_job_analysis_response",
            side_effect=_sdk_status_error(openai.AuthenticationError, 401, sentinel),
        ), _capture_root_logs() as records:
            before = time.time()
            # Still an unhandled 500, as before (TestClient re-raises it).
            with self.assertRaises(OperationalError) as ctx:
                self.client.post(f"/jobs/{job_id}/analyze", headers=headers)
            after = time.time()

        self.assertEqual(mock_verify.call_count, 1)
        self._assert_no_sentinel_in_traceback(ctx.exception, sentinel)
        self.assertEqual(self._analysis_rows(job_id), [])
        # Rolled back before the guard release: released with the failure
        # cooldown, and the database is writable again.
        self._assert_released_with_cooldown(
            self._guard_row(JOB_OPERATION_TYPE, job_id), JOB_FAILURE_COOLDOWN, before, after
        )
        self._assert_released_with_cooldown(
            self._guard_row(JOB_USER_OPERATION_TYPE, owner_id), JOB_USER_FAILURE_COOLDOWN, before, after
        )
        self._assert_database_writable()
        self._assert_no_sentinel_in_database(sentinel)
        self._assert_no_sentinel_in_logs(records, sentinel)

    def test_analyze_missing_provider_errors_are_fixed(self):
        n = time.time_ns()
        job_ids = [self._create_job(f"missing-{n}-{i}") for i in range(2)]
        sentinel = Sentinel()
        excs = [
            _sdk_status_error(openai.AuthenticationError, 401, sentinel, code="invalid_api_key"),
            RuntimeError(sentinel.text),
        ]
        with patch(
            "backend.routers.jobs._create_job_analysis_response", side_effect=excs
        ) as mock_openai, _capture_root_logs() as records:
            response = self.client.post("/jobs/analyze-missing?limit=2", headers=self.admin_headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(mock_openai.call_count, 2)
        results = response.json()["results"]
        # job_id DESC: the newest job meets the first error.
        self.assertEqual([r["job_id"] for r in results], sorted(job_ids, reverse=True))
        for result, expected in zip(
            results, (ANALYSIS_ERROR_PROVIDER_CREDENTIALS, ANALYSIS_ERROR_INTERNAL)
        ):
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"], {
                "error_code": "ERR_JOB_ANALYSIS_FAILED",
                "message": "Job analysis failed.",
                "job_id": result["job_id"],
                "error": expected,
            })
        self._assert_no_sentinel_anywhere(response, records, sentinel)

    def test_analyze_missing_unexpected_exception_is_fixed(self):
        n = time.time_ns()
        job_id = self._create_job(f"missing-generic-{n}")
        sentinel = Sentinel()
        with patch.object(
            jobs, "_analyze_job_impl", side_effect=RuntimeError(sentinel.text)
        ) as mock_impl, _capture_root_logs() as records:
            response = self.client.post("/jobs/analyze-missing?limit=1", headers=self.admin_headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(mock_impl.call_count, 1)
        body = response.json()
        self.assertEqual(body["failed_count"], 1)
        self.assertEqual(body["results"], [{
            "job_id": job_id,
            "title": f"Synthetic Job missing-generic-{n}",
            "company_name": "Acme",
            "status": "failed",
            "error": ANALYSIS_ERROR_INTERNAL,
        }])
        self._assert_no_sentinel_anywhere(response, records, sentinel)

    def test_analyze_sample_invalid_json_has_no_raw_response(self):
        job_id = self._create_job(f"sample-{time.time_ns()}")
        sentinel = Sentinel()
        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            return_value=_invalid_json_output(sentinel),
        ) as mock_openai, _capture_root_logs() as records:
            response = self.client.post(
                f"/jobs/analyze-sample?job_id_list={job_id}", headers=self.admin_headers
            )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json(), {"detail": {
            "error_code": "ERR_AI_INVALID_JSON",
            "message": "AI response was not valid JSON.",
            "job_id": job_id,
        }})
        self.assertEqual(mock_openai.call_count, 1)
        self.assertEqual(self._analysis_rows(), [])
        self._assert_no_sentinel_anywhere(response, records, sentinel)

    def test_analyze_sample_provider_error_has_no_text(self):
        job_id = self._create_job(f"sample-error-{time.time_ns()}")
        sentinel = Sentinel()
        answered = self._record_answered_http_exceptions()
        with patch(
            "backend.routers.jobs._create_job_sample_analysis_response",
            side_effect=_sdk_status_error(openai.AuthenticationError, 401, sentinel),
        ), _capture_root_logs() as records:
            response = self.client.post(
                f"/jobs/analyze-sample?job_id_list={job_id}", headers=self.admin_headers
            )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json(), {"detail": {
            "error_code": "ERR_JOB_ANALYSIS_FAILED",
            "message": "Job analysis failed.",
            "job_id": job_id,
        }})
        self._assert_no_sentinel_anywhere(response, records, sentinel)
        self._assert_failure_does_not_chain(answered, sentinel)


class CoverLetterSafeErrorTests(_SafeErrorsBase):
    routers = (cover_letters.router,)

    def setUp(self):
        super().setUp()
        env_patcher = patch.dict(os.environ, {"COVER_LETTER_DAILY_LIMIT": "50"}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        client_patcher = patch.object(cover_letters, "client", FakeAnalysisClient())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.user_id = self._create_user("letter.owner@example.com")
        self.headers = self._auth_headers(self.user_id)
        self.profile_id = self._create_profile(self.user_id)
        self.job_id = self._create_job("letter")
        self.attempts = 0

    def _create(self, **seam):
        self.attempts += 1
        with patch.object(
            cover_letters, "_create_cover_letter_response", **seam
        ) as mock_provider, _capture_root_logs() as records:
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{self.job_id}/cover-letters",
                headers=self.headers,
                json={"language": "en"},
            )
        self.assertEqual(mock_provider.call_count, 1)
        return response, records

    def _assert_nothing_kept(self):
        self.assertEqual(self._rows("SELECT * FROM cover_letters"), [])
        self.assertEqual(self._rows("SELECT * FROM cover_letter_versions"), [])
        guard = self._rows(
            "SELECT owner_token, lock_expires_at FROM analysis_guards "
            "WHERE operation_type = 'cover_letter_user' AND resource_id = :uid",
            uid=self.user_id,
        )
        self.assertEqual(len(guard), 1)
        self.assertIsNone(guard[0].owner_token)  # released after the failure
        self.assertIsNone(guard[0].lock_expires_at)
        # Each attempt keeps its quota slot, as before.
        reservations = self._rows(
            "SELECT id FROM analysis_quota_reservations "
            "WHERE operation_type = 'cover_letter_user' AND user_id = :uid",
            uid=self.user_id,
        )
        self.assertEqual(len(reservations), self.attempts)

    def test_spend_limit_is_fixed_503(self):
        for code in sorted(PROVIDER_SPEND_LIMIT_ERROR_CODES):
            with self.subTest(code=code):
                sentinel = Sentinel()
                response, records = self._create(
                    side_effect=_sdk_status_error(openai.RateLimitError, 429, sentinel, code=code)
                )
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json(), COVER_LETTER_PROVIDER_UNAVAILABLE_BODY)
                self.assertNotIn(code, response.text)
                self._assert_no_sentinel_anywhere(response, records, sentinel)
                self._assert_nothing_kept()

    def test_provider_errors_are_fixed_502(self):
        for name, build, _expected in _provider_error_cases():
            with self.subTest(case=name):
                sentinel = Sentinel()
                response, records = self._create(side_effect=build(sentinel))
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json(), COVER_LETTER_FAILED_BODY)
                self._assert_no_sentinel_anywhere(response, records, sentinel)
                self._assert_nothing_kept()

    def test_release_error_does_not_chain_provider_text(self):
        # A failing lease release (the finally in _generate) while the 502
        # is on its way out must not chain the provider error into the
        # server traceback.
        sentinel = Sentinel()

        def release_fails(*args, **kwargs):
            _raise_database_locked()

        with patch.object(
            cover_letters.cover_letter, "release_generation", side_effect=release_fails
        ), patch.object(
            cover_letters, "_create_cover_letter_response",
            side_effect=_sdk_status_error(openai.AuthenticationError, 401, sentinel),
        ), _capture_root_logs() as records:
            with self.assertRaises(OperationalError) as ctx:
                self.client.post(
                    f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{self.job_id}/cover-letters",
                    headers=self.headers,
                    json={"language": "en"},
                )
        self._assert_no_sentinel_in_traceback(ctx.exception, sentinel)
        self._assert_no_sentinel_in_database(sentinel)
        self._assert_no_sentinel_in_logs(records, sentinel)
        self.assertEqual(self._rows("SELECT * FROM cover_letters"), [])

    def test_invalid_outputs_are_fixed_502(self):
        for name, build in (
            ("invalid JSON", _invalid_json_output),
            ("schema violation", lambda s: _schema_violation_output(
                {"letter_text": "x", "used_fact_ids": [], "missing_information": []},
                "letter_text", s,
            )),
        ):
            with self.subTest(case=name):
                sentinel = Sentinel()
                response, records = self._create(return_value=build(sentinel))
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json(), COVER_LETTER_FAILED_BODY)
                self._assert_no_sentinel_anywhere(response, records, sentinel)
                self._assert_nothing_kept()


if __name__ == "__main__":
    unittest.main()
