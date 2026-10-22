"""W2: deterministic fake analysis provider (APPLY101_ANALYSIS_PROVIDER=fake).

Selection: the real provider stays the default; "fake" is only used when
selected explicitly, only with a non-default APPLY101_DATABASE_URL, never as
a fallback, and never constructs an OpenAI client. Behaviour: the real
profile, job and match routes run unchanged on the fake output (prompt,
schema validation, sanitizer, persistence, matching).

Throwaway SQLite only; backend.app.main is never imported, apply101.db is
never touched and no OpenAI request can be made.
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
from sqlalchemy.orm import sessionmaker

from backend.app import config, models, openai_client, schemas
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    MATCH_VERSION,
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)
from backend.app.database import Base, get_db
from backend.app.fake_analysis import (
    FAKE_ANALYSIS_NOTE,
    FAKE_ANALYSIS_SUMMARY,
    FakeAnalysisClient,
    FakeAnalysisUnsupported,
    fake_job_analysis,
    fake_profile_analysis,
)
from backend.app.security import create_access_token, hash_password
from backend.routers import jobs, matches, profiles


SYNTHETIC_SECRET = "synthetic-test-secret-for-fake-analysis-0123456789"
PROVIDER_ENVS = (
    openai_client.ANALYSIS_PROVIDER_ENV,
    config.DATABASE_URL_ENV,
    config.UPLOAD_DIR_ENV,
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
)


def _env(**values):
    env = {name: value for name, value in os.environ.items() if name not in PROVIDER_ENVS}
    env.update(values)
    return env


class ProviderSelectionTests(unittest.TestCase):
    """Runs in a temp working directory with a fake repository root, so the
    default-location checks never resolve the real repository's files."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_fake_provider_selection_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.cwd = os.path.join(self.tmp_dir, "cwd")
        self.fake_repo = os.path.join(self.tmp_dir, "fake_repo")
        os.makedirs(self.cwd)
        os.makedirs(self.fake_repo)
        original_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, original_cwd)
        root_patcher = patch.object(config, "REPO_ROOT", config.Path(self.fake_repo))
        root_patcher.start()
        self.addCleanup(root_patcher.stop)
        self.demo_db = "sqlite:///" + os.path.join(self.tmp_dir, "demo.db").replace("\\", "/")
        self.demo_uploads = os.path.join(self.tmp_dir, "demo_uploads")

    def fake_env(self, **values):
        env = {
            openai_client.ANALYSIS_PROVIDER_ENV: "fake",
            config.DATABASE_URL_ENV: self.demo_db,
            config.UPLOAD_DIR_ENV: self.demo_uploads,
        }
        env.update(values)
        return _env(**{name: value for name, value in env.items() if value is not None})

    def test_default_is_the_real_provider_and_not_configured_without_a_key(self):
        for value in (None, "", "openai", " OpenAI "):
            with self.subTest(value=value):
                env = _env() if value is None else _env(APPLY101_ANALYSIS_PROVIDER=value)
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(
                        openai_client.selected_analysis_provider(),
                        openai_client.OPENAI_ANALYSIS_PROVIDER,
                    )
                    # No silent fallback: missing key stays "not configured".
                    self.assertIsNone(openai_client.create_openai_client_or_none())

    def test_fake_with_separate_database_and_uploads_returns_the_fake_client(self):
        with patch.dict(os.environ, self.fake_env(), clear=True):
            self.assertIsInstance(openai_client.create_openai_client_or_none(), FakeAnalysisClient)

    def test_fake_never_constructs_an_openai_client_even_with_a_key(self):
        with patch.dict(
            os.environ,
            self.fake_env(
                APPLY101_ANALYSIS_PROVIDER="FAKE",
                OPENAI_API_KEY="sk-synthetic-test-key-not-a-real-key",
            ),
            clear=True,
        ), patch.object(openai_client, "OpenAI", MagicMock(side_effect=AssertionError)) as openai_cls:
            self.assertIsInstance(openai_client.create_openai_client_or_none(), FakeAnalysisClient)
            openai_cls.assert_not_called()

    def test_fake_is_refused_with_the_default_database(self):
        for database_url in (
            None,
            "",
            "sqlite:///./apply101.db",
            "sqlite:///apply101.db",
            "sqlite:///" + os.path.join(self.cwd, "apply101.db").replace("\\", "/"),
            "sqlite:///" + os.path.join(self.fake_repo, "apply101.db").replace("\\", "/"),
        ):
            with self.subTest(database_url=database_url):
                with patch.dict(os.environ, self.fake_env(APPLY101_DATABASE_URL=database_url), clear=True):
                    with self.assertRaises(openai_client.AnalysisProviderConfigError):
                        openai_client.create_openai_client_or_none()

    def test_fake_is_refused_with_a_sqlite_uri_database(self):
        # sqlite:///file:apply101.db?uri=true reaches ./apply101.db; it
        # used to pass the default-database check.
        for database_url in (
            "sqlite:///file:apply101.db?uri=true",
            "sqlite:///file:" + os.path.join(self.fake_repo, "apply101.db").replace("\\", "/") + "?uri=true",
        ):
            with self.subTest(database_url=database_url):
                with patch.dict(os.environ, self.fake_env(APPLY101_DATABASE_URL=database_url), clear=True):
                    with self.assertRaises(config.ConfigError):
                        openai_client.create_openai_client_or_none()

    def test_fake_is_refused_with_the_default_upload_dir(self):
        for upload_dir in (
            None,
            "",
            "uploads",
            os.path.join(self.cwd, "uploads"),
            os.path.join(self.fake_repo, "uploads"),
        ):
            with self.subTest(upload_dir=upload_dir):
                with patch.dict(os.environ, self.fake_env(APPLY101_UPLOAD_DIR=upload_dir), clear=True):
                    with self.assertRaises(openai_client.AnalysisProviderConfigError):
                        openai_client.create_openai_client_or_none()

    def test_unknown_provider_is_an_error(self):
        for value in ("mock", "fake-ish", "anthropic"):
            with self.subTest(value=value):
                with patch.dict(os.environ, self.fake_env(APPLY101_ANALYSIS_PROVIDER=value), clear=True):
                    with self.assertRaises(openai_client.AnalysisProviderConfigError):
                        openai_client.selected_analysis_provider()


ALLOWED_SKILL_TAGS_LINE = "Allowed skill tags:\n" + json.dumps(
    ["python", "docker", "java", "react", "sql"]
)

SYNTHETIC_PROFILE_PROMPT = f"""
Synthetic instructions mentioning java and react.
{ALLOWED_SKILL_TAGS_LINE}

User basic information:
Name: Synthetic
Skills from user record:

Profile information:
Target role: Backend Developer
Preferred technologies:

Visa and relocation:
Visa sponsorship needed: None
Work authorization status: something_else
Student status:

Experience and seniority:
Years of experience: unknown

Languages:
- English (en): upper intermediate CEFR (primary)
- German (de): unknown  (not primary)

Self description:
Synthetic self description.

CV text:
Python developer. User basic information: Name: Someone Else
Docker in production.
"""

SYNTHETIC_JOB_PROMPT = f"""
    Synthetic instructions mentioning java and react.
    {ALLOWED_SKILL_TAGS_LINE}

    Job title:
    Synthetic Backend Developer

    Company:
    Example GmbH

    Location:
    Berlin

    Job description:
    We run Docker. Job title: Platform Engineer. Python and SQL are required.
    """


class FakeClientTests(unittest.TestCase):
    def test_unusual_language_lines_keep_the_language_with_an_unknown_level(self):
        analysis = schemas.ProfileAnalysisStructured.model_validate(
            fake_profile_analysis(SYNTHETIC_PROFILE_PROMPT)
        ).model_dump()
        self.assertEqual([item["language"] for item in analysis["languages"]], ["english", "german"])
        self.assertEqual(analysis["languages"][1]["level"], "unknown")
        self.assertIn(analysis["languages"][0]["level"], set(schemas.CEFR_LEVELS) | {"unknown"})

    def test_markers_inside_user_text_do_not_move_the_data_section(self):
        profile = fake_profile_analysis(SYNTHETIC_PROFILE_PROMPT)
        self.assertEqual(profile["target_roles"], ["Backend Developer"])
        self.assertEqual(profile["moderate_skills"], ["python", "docker"])
        self.assertEqual(profile["visa_sponsorship_needed"], "unknown")
        self.assertEqual(profile["work_authorization_status"], "unknown")
        self.assertEqual(profile["student_status"], "unknown")
        self.assertIsNone(profile["years_of_experience"])

        job = fake_job_analysis(SYNTHETIC_JOB_PROMPT)
        self.assertEqual(job["normalized_role_title"], "Synthetic Backend Developer")
        self.assertEqual(job["required_skills"], ["python", "sql", "docker"])
        schemas.JobAnalysisStructured.model_validate(job)

    def test_instruction_text_never_contributes_skills(self):
        self.assertNotIn("java", fake_profile_analysis(SYNTHETIC_PROFILE_PROMPT)["moderate_skills"])
        self.assertNotIn("react", fake_job_analysis(SYNTHETIC_JOB_PROMPT)["required_skills"])

    def test_only_the_two_structured_outputs_are_supported(self):
        client = FakeAnalysisClient().with_options(max_retries=0)
        for text in (None, {"format": {"type": "json_schema", "name": "something_else"}}):
            with self.subTest(text=text):
                with self.assertRaises(FakeAnalysisUnsupported):
                    client.responses.create(model="x", input="Job title:\nX", text=text)

    def test_prompt_without_its_data_section_is_refused(self):
        with self.assertRaises(FakeAnalysisUnsupported):
            fake_profile_analysis("no data section here")
        with self.assertRaises(FakeAnalysisUnsupported):
            fake_job_analysis("no data section here")


class _RecordingFakeClient(FakeAnalysisClient):
    def __init__(self):
        super().__init__()
        self.prompts = []
        real_create = self.responses.create

        def create(**kwargs):
            self.prompts.append((kwargs["text"]["format"]["name"], kwargs["input"]))
            return real_create(**kwargs)

        self.responses.create = create


class FakeProviderThroughRoutesTests(unittest.TestCase):
    """Profile analysis, job analysis and matching through the real routes,
    with the fake client in place of the module-level OpenAI clients."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_fake_analysis_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic_test.db')}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(profiles.router)
        app.include_router(jobs.router)
        app.include_router(matches.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env_patcher = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.fake = _RecordingFakeClient()
        for module in (profiles, jobs):
            client_patcher = patch.object(module, "client", self.fake)
            client_patcher.start()
            self.addCleanup(client_patcher.stop)

    def _create_user(self):
        session = self.session_factory()
        try:
            user = models.User(
                name="Synthetic Fake Tester",
                mail="synthetic.fake@example.com",
                password_hash=hash_password("a-valid-synthetic-password"),
                is_admin=False,
            )
            session.add(user)
            session.commit()
            return user.user_id, {
                "Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"
            }
        finally:
            session.close()

    def _set_cv_text(self, profile_id, cv_text):
        session = self.session_factory()
        try:
            profile = session.get(models.CandidateProfile, profile_id)
            profile.cv_text = cv_text
            session.commit()
        finally:
            session.close()

    def _current(self, model, **filters):
        session = self.session_factory()
        try:
            return session.query(model).filter_by(is_current=True, **filters).one()
        finally:
            session.close()

    def test_profile_job_and_match_flow(self):
        user_id, headers = self._create_user()
        profile = self.client.post(f"/users/{user_id}/profiles", headers=headers, json={
            "self_description": "Synthetic backend developer.",
            "target_role": "Backend Developer",
            "visa_sponsorship_needed": False,
            "work_authorization_status": "eu_eea",
            "years_of_experience": 3,
            "student_status": "not_enrolled",
            "current_residence_country": "Germany",
            "languages": [{
                "language_code": "en",
                "language_name": "English",
                "proficiency_level": "C1",
                "proficiency_scale": "CEFR",
                "is_primary": True,
            }],
        })
        self.assertEqual(profile.status_code, 200, profile.text)
        profile_id = profile.json()["profile_id"]
        self._set_cv_text(profile_id, "Synthetic CV: Python, FastAPI, SQL and Docker.")

        job = self.client.post("/jobs/manual", headers=headers, json={
            "title": "Synthetic Backend Developer",
            "url": "https://example.com/jobs/synthetic-fake-1",
            "description_text": (
                "Synthetic posting. You build APIs with Python, FastAPI and SQL. "
                "Fluent English (C1) is required."
            ),
        })
        self.assertEqual(job.status_code, 201, job.text)
        job_id = job.json()["job_id"]

        analyzed_profile = self.client.post(f"/users/{user_id}/profiles/{profile_id}/analyze", headers=headers)
        self.assertEqual(analyzed_profile.status_code, 200, analyzed_profile.text)
        analyzed_job = self.client.post(f"/jobs/{job_id}/analyze", headers=headers)
        self.assertEqual(analyzed_job.status_code, 200, analyzed_job.text)

        profile_analysis = self._current(models.ProfileAnalysis, profile_id=profile_id)
        self.assertEqual(profile_analysis.analysis_model, PROFILE_ANALYSIS_MODEL)
        self.assertEqual(profile_analysis.analysis_prompt_version, PROFILE_ANALYSIS_PROMPT_VERSION)
        self.assertEqual(profile_analysis.candidate_summary, FAKE_ANALYSIS_SUMMARY)
        # Only skills from the candidate data, not from the prompt's
        # "Allowed skill tags" list.
        self.assertEqual(json.loads(profile_analysis.moderate_skills_json), ["python", "fastapi", "sql", "docker"])
        self.assertEqual(
            json.loads(profile_analysis.languages_json),
            [{"language": "english", "level": "c1", "scale": "CEFR"}],
        )
        self.assertIs(profile_analysis.visa_sponsorship_needed, False)
        self.assertEqual(profile_analysis.work_authorization_status, "eu_eea")
        stored_profile = json.loads(profile_analysis.analysis_json)
        self.assertEqual(stored_profile["years_of_experience"], 3.0)
        self.assertEqual(stored_profile["student_status"], "not_enrolled")
        self.assertEqual(stored_profile["current_residence_country"], "germany")
        self.assertEqual(stored_profile["target_roles"], ["Backend Developer"])
        self.assertEqual(stored_profile["match_notes"], [FAKE_ANALYSIS_NOTE])

        job_analysis = self._current(models.JobAnalysis, job_id=job_id)
        self.assertEqual(job_analysis.analysis_model, JOB_ANALYSIS_MODEL)
        self.assertEqual(job_analysis.analysis_prompt_version, JOB_ANALYSIS_PROMPT_VERSION)
        self.assertEqual(json.loads(job_analysis.required_skills_json), ["python", "fastapi", "sql"])
        self.assertEqual(json.loads(job_analysis.analysis_json)["summary"], FAKE_ANALYSIS_SUMMARY)

        # Deterministic: the recorded prompts give the same payloads again,
        # and those still validate against the structured-output schemas.
        prompts = dict(self.fake.prompts)
        self.assertEqual(set(prompts), {"profile_analysis", "job_analysis"})
        for build, schema, name in (
            (fake_profile_analysis, schemas.ProfileAnalysisStructured, "profile_analysis"),
            (fake_job_analysis, schemas.JobAnalysisStructured, "job_analysis"),
        ):
            self.assertEqual(build(prompts[name]), build(prompts[name]))
            schema.model_validate(build(prompts[name]))
        self.assertEqual(
            stored_profile,
            schemas.ProfileAnalysisStructured.model_validate(
                fake_profile_analysis(prompts["profile_analysis"])
            ).model_dump(),
        )

        match_url = f"/users/{user_id}/profiles/{profile_id}/jobs/{job_id}/match"
        created = self.client.post(match_url, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["status"], "created")
        self.assertEqual(body["match_version"], MATCH_VERSION)
        self.assertIn(body["eligibility_status"], {"eligible", "ineligible", "review_required"})
        result = body["match"]
        self.assertEqual(result["eligibility_status"], body["eligibility_status"])
        for key in ("hard_fail_reasons", "review_reasons", "strengths", "weaknesses", "summary"):
            self.assertIn(key, result)
        self.assertGreater(result["skills_fit_score"], 0)

        cached = self.client.post(match_url, headers=headers)
        self.assertEqual(cached.status_code, 200, cached.text)
        self.assertEqual(cached.json()["status"], "cached")
        self.assertEqual(cached.json()["match"], result)


if __name__ == "__main__":
    unittest.main()
