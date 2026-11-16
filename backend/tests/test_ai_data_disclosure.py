"""Drift guard for the "How Apply101 uses AI" notice (PR-5).

The web client tells users which of their data each AI action sends
(frontend/src/pages/AiDataPage.tsx and components/AiDataNote.tsx, summarised
in docs/ONLINE_MVP_PROGRESS.md). These tests capture what the real routes
actually hand to the provider seam -- the profile- and job-analysis prompt
data labels (also the admin-only analyze-sample) and the motivation-letter
fact ids -- and compare them with the
list the notice was written from. A failure means the data sent changed:
update the notice, the inline notes and the docs, then this list.

Throwaway SQLite, the deterministic fake client in place of the module
clients; backend.app.main is never imported, no .env, no network.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import cover_letter, models
from backend.app.analysis_contract import JOB_ANALYSIS_MODEL, JOB_ANALYSIS_PROMPT_VERSION
from backend.app.database import Base, get_db
from backend.app.fake_analysis import FakeAnalysisClient
from backend.app.security import create_access_token, hash_password

with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"}, clear=False):
    from backend.routers import cover_letters, jobs, profiles


SYNTHETIC_SECRET = "synthetic-test-secret-for-ai-data-disclosure-012345"
UPDATE_HINT = (
    "The data sent to the AI provider changed. Update the user-facing notice "
    "(frontend/src/pages/AiDataPage.tsx, frontend/src/components/AiDataNote.tsx) "
    "and docs/ONLINE_MVP_PROGRESS.md, then this expected list."
)

# Labels of the profile-analysis prompt's data section, in prompt order.
EXPECTED_PROFILE_LABELS = [
    "User basic information",
    "Name",
    "Email",
    "Skills from user record",
    "Experience years from user record",
    "Major",
    "Has master degree",
    "Has PhD",
    "Has Abitur",
    "Profile information",
    "Profile name",
    "Target role",
    "Secondary target role",
    "Preferred roles JSON",
    "Excluded roles JSON",
    "Target location",
    "Target locations JSON",
    "Preferred work type",
    "Preferred technologies",
    "Extra preferences",
    "Visa and relocation",
    "Visa sponsorship needed",
    "Work authorization status",
    "Relocation preference",
    "Current residence country",
    "Student status",
    "Experience and seniority",
    "Years of experience",
    "Seniority target",
    "Languages",
    "Self description",
    "CV text",
]

# Labels of the job-analysis prompt's data section: the posting only.
EXPECTED_JOB_LABELS = ["Job title", "Company", "Location", "Job description"]

# Motivation-letter facts for a job-pool posting with every fact present.
EXPECTED_LETTER_FACTS = [
    ("profile.target_role", "Target role"),
    ("profile.experience", "Years of experience"),
    ("profile.about", "About me (written by the user)"),
    ("profile.technologies", "Preferred technologies"),
    ("profile.skills", "Skills and tools (from the profile analysis)"),
    ("profile.languages", "Languages"),
    ("cv.text", "CV text (uploaded by the user)"),
    ("job.title", "Job title"),
    ("job.company", "Company named in the posting"),
    ("job.location", "Job location"),
    ("job.description", "Job posting text"),
    ("job.required_skills", "Required skills (from the job analysis)"),
    ("company.name", "Company"),
    ("company.website", "Company website (company list)"),
    ("company.about", "About the company (published by the company)"),
]

_LABEL = re.compile(r"^([A-Z][A-Za-z ]*):")


def _data_labels(prompt: str, start: str, end: str | None = None) -> list[str]:
    """Labels ("Label:" at a line start) from the prompt's data section on.
    The synthetic values below never start a line with such a label."""
    lines = [line.strip() for line in prompt.splitlines()]
    first = lines.index(start)
    last = lines.index(end, first) if end else len(lines)
    return [match.group(1) for line in lines[first:last] if (match := _LABEL.match(line))]


class _RecordingClient(FakeAnalysisClient):
    def __init__(self):
        super().__init__()
        self.prompts = {}
        real_create = self.responses.create

        def create(**kwargs):
            self.prompts[kwargs["text"]["format"]["name"]] = kwargs["input"]
            return real_create(**kwargs)

        self.responses.create = create


class AiDataDisclosureDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_ai_data_disclosure_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        for router in (profiles.router, jobs.router, cover_letters.router):
            app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        env = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET, "COVER_LETTER_DAILY_LIMIT": "5"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.fake = _RecordingClient()
        for module in (profiles, jobs, cover_letters):
            patcher = patch.object(module, "client", self.fake)
            patcher.start()
            self.addCleanup(patcher.stop)

        db = self.Session()
        user = models.User(
            name="synthetic tester", mail="synthetic.disclosure@example.com",
            password_hash=hash_password("a-valid-synthetic-password"),
            skills="python", experience_years=3, major="computer science", master=True,
        )
        db.add(user)
        db.commit()
        self.user_id = user.user_id
        self.headers = {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}
        db.close()

    def _profile(self) -> int:
        response = self.client.post(f"/users/{self.user_id}/profiles", headers=self.headers, json={
            "target_role": "Backend Developer",
            "years_of_experience": 3,
            "self_description": "synthetic backend developer who builds APIs.",
            "preferred_technologies": "Python, FastAPI",
            "languages": [{
                "language_code": "en", "language_name": "English", "proficiency_level": "C1",
                "proficiency_scale": "CEFR", "is_primary": True,
            }],
        })
        self.assertEqual(response.status_code, 200, response.text)
        profile_id = response.json()["profile_id"]
        db = self.Session()
        db.get(models.CandidateProfile, profile_id).cv_text = "synthetic CV with Python, FastAPI and SQL."
        db.commit()
        db.close()
        return profile_id

    def test_profile_and_job_analysis_send_the_disclosed_labels(self):
        profile_id = self._profile()
        analyzed = self.client.post(f"/users/{self.user_id}/profiles/{profile_id}/analyze", headers=self.headers)
        self.assertEqual(analyzed.status_code, 200, analyzed.text)
        job = self.client.post("/jobs/manual", headers=self.headers, json={
            "title": "Synthetic Backend Developer",
            "company_name": "Example GmbH",
            "location": "Berlin",
            "url": "https://example.com/jobs/synthetic-disclosure",
            "description_text": "synthetic posting. You build APIs with Python, FastAPI and SQL in a small team.",
        })
        self.assertEqual(job.status_code, 201, job.text)
        analyzed_job = self.client.post(f"/jobs/{job.json()['job_id']}/analyze", headers=self.headers)
        self.assertEqual(analyzed_job.status_code, 200, analyzed_job.text)

        profile_prompt = self.fake.prompts["profile_analysis"]
        self.assertEqual(_data_labels(profile_prompt, "User basic information:"), EXPECTED_PROFILE_LABELS, UPDATE_HINT)
        job_prompt = self.fake.prompts["job_analysis"]
        self.assertEqual(_data_labels(job_prompt, "Job title:"), EXPECTED_JOB_LABELS, UPDATE_HINT)
        # The job prompt carries no account or profile data.
        for personal in ("synthetic.disclosure@example.com", "synthetic tester", "synthetic CV"):
            self.assertNotIn(personal, job_prompt)

    def test_admin_analyze_sample_sends_only_the_posting(self):
        db = self.Session()
        admin = models.User(
            name="synthetic admin", mail="synthetic.admin@example.com",
            password_hash=hash_password("a-valid-synthetic-password"), is_admin=True,
        )
        job = models.Job(
            title="Synthetic Data Engineer", company_name="Example GmbH", location="Hamburg",
            url="https://example.test/sample/1", description_text="synthetic posting about pipelines with SQL.",
            source="manual", created_by_user_id=self.user_id,
        )
        db.add_all([admin, job])
        db.commit()
        admin_headers = {"Authorization": f"Bearer {create_access_token(admin.user_id, admin.token_key)}"}
        job_id = job.job_id
        db.close()

        prompts = []

        def record(prompt, **_kwargs):
            prompts.append(prompt)
            response = MagicMock()
            response.output_text = json.dumps({
                "summary": "synthetic", "role_family": "other", "role_subfamily": "other",
                "normalized_role_title": "synthetic", "required_skills": [], "preferred_skills": [],
                "responsibilities": [], "seniority_level": "unknown", "language_requirements": [],
                "visa_sponsorship": "unknown", "work_type": "unknown", "employment_type": "unknown",
                "dealbreakers": [],
            })
            return response

        with patch.object(jobs, "_create_job_sample_analysis_response", side_effect=record):
            response = self.client.post("/jobs/analyze-sample", headers=admin_headers, params={"job_id_list": [job_id]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(prompts), 1)
        labels = _data_labels(prompts[0], "Job title:", "Return ONLY valid JSON in this exact structure:")
        self.assertEqual(labels, EXPECTED_JOB_LABELS, UPDATE_HINT)
        for personal in ("synthetic.disclosure@example.com", "synthetic tester", "synthetic.admin@example.com"):
            self.assertNotIn(personal, prompts[0])

    def test_motivation_letter_sends_the_disclosed_facts(self):
        profile_id = self._profile()
        analyzed = self.client.post(f"/users/{self.user_id}/profiles/{profile_id}/analyze", headers=self.headers)
        self.assertEqual(analyzed.status_code, 200, analyzed.text)
        db = self.Session()
        company = models.Company(
            name="Acme", normalized_name="acme", website="https://acme.example",
            about_text="Acme builds tools for logistics teams.",
            about_source_url="https://boards-api.greenhouse.io/v1/boards/acme",
        )
        db.add(company)
        db.flush()
        pool_job = models.Job(
            title="Backend Developer", company_name="Acme", location="Berlin", url="https://example.test/j/1",
            description_text="Build APIs with Python and SQL.", source="greenhouse", job_status="active",
            company_id=company.company_id,
        )
        db.add(pool_job)
        db.flush()
        db.add(models.JobAnalysis(
            job_id=pool_job.job_id, analysis_status="completed", analysis_model=JOB_ANALYSIS_MODEL,
            analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION, is_current=True,
            required_skills_json='["python", "sql"]',
        ))
        db.commit()
        job_id = pool_job.job_id
        db.close()

        created = self.client.post(
            f"/users/{self.user_id}/profiles/{profile_id}/jobs/{job_id}/cover-letters",
            headers=self.headers, json={"language": "en"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        prompt = self.fake.prompts["cover_letter"]
        data = prompt[prompt.index(cover_letter.LETTER_DATA_MARKER):]
        facts = json.loads(data.split("Facts JSON:\n", 1)[1].split("\nKnown gaps JSON:", 1)[0])
        self.assertEqual([(fact["id"], fact["label"]) for fact in facts], EXPECTED_LETTER_FACTS, UPDATE_HINT)
        # Name and email are not separate letter facts (the CV text may still contain them).
        self.assertNotIn("synthetic.disclosure@example.com", prompt)
        self.assertNotIn("synthetic tester", prompt)


if __name__ == "__main__":
    unittest.main()
