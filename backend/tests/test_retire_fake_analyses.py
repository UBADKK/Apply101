"""backend.scripts.retire_fake_analyses and the fake-row classifiers in
backend/app/fake_analysis.py.

The fake rows are made by the real profile, job and match routes with the
fake client, so a change to the fake provider's output that the
classifiers no longer recognize fails here. Throwaway SQLite only;
backend.app.main is never imported, apply101.db is never touched and no
OpenAI request can be made.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import config, models
from backend.app.analysis_contract import MATCH_MODEL, MATCH_VERSION
from backend.app.database import Base, get_db
from backend.app.fake_analysis import (
    ANALYSIS_AMBIGUOUS,
    ANALYSIS_FAKE,
    ANALYSIS_NONE,
    FAKE_ANALYSIS_NOTE,
    FAKE_ANALYSIS_SUMMARY,
    FakeAnalysisClient,
    classify_job_analysis,
    classify_profile_analysis,
    fake_job_analysis,
    fake_profile_analysis,
    is_fake_cover_letter_grounding,
)
from backend.app.security import create_access_token, hash_password
from backend.routers import jobs, matches, profiles
from backend.scripts import retire_fake_analyses


SYNTHETIC_SECRET = "synthetic-test-secret-for-retire-fake-0123456789"
REAL_PROFILE_SUMMARY = "Synthetic backend developer with Python and SQL experience."
REAL_JOB_SUMMARY = "Synthetic backend role building Python APIs."

PROFILE_PROMPT = """
User basic information:
Name: Synthetic
Skills from user record: Python

Profile information:
Target role: Backend Developer

CV text:
Python and SQL.
"""
JOB_PROMPT = """
Job title:
Synthetic Backend Developer

Job description:
Python and SQL.
"""


def _stored(payload):
    return json.dumps(payload, ensure_ascii=False)


class ClassifierTests(unittest.TestCase):
    def fake_profile(self):
        return fake_profile_analysis(PROFILE_PROMPT)

    def fake_job(self):
        return fake_job_analysis(JOB_PROMPT)

    def test_fake_payloads_are_fake(self):
        profile = self.fake_profile()
        self.assertEqual(
            classify_profile_analysis("completed", profile["candidate_summary"], _stored(profile)),
            ANALYSIS_FAKE,
        )
        job = self.fake_job()
        self.assertEqual(classify_job_analysis("completed", _stored(job), job["role_family"]), ANALYSIS_FAKE)

    def test_real_looking_payloads_are_none(self):
        profile = dict(self.fake_profile(), candidate_summary=REAL_PROFILE_SUMMARY, match_notes=[])
        self.assertEqual(
            classify_profile_analysis("completed", REAL_PROFILE_SUMMARY, _stored(profile)), ANALYSIS_NONE
        )
        job = dict(self.fake_job(), summary=REAL_JOB_SUMMARY)
        self.assertEqual(classify_job_analysis("completed", _stored(job), "other"), ANALYSIS_NONE)

    def test_partial_or_copied_markers_are_ambiguous(self):
        fake = self.fake_profile()
        quoted = f"Real summary quoting: {FAKE_ANALYSIS_SUMMARY}"
        cases = {
            "summary without the match note": (
                "completed", FAKE_ANALYSIS_SUMMARY, _stored(dict(fake, match_notes=[])),
            ),
            "extra match note": (
                "completed", FAKE_ANALYSIS_SUMMARY, _stored(dict(fake, match_notes=[FAKE_ANALYSIS_NOTE, "x"])),
            ),
            "marker inside a real summary": (
                "completed", quoted, _stored(dict(fake, candidate_summary=quoted, match_notes=[])),
            ),
            "only the note": (
                "completed", REAL_PROFILE_SUMMARY,
                _stored(dict(fake, candidate_summary=REAL_PROFILE_SUMMARY)),
            ),
            "summary column differs": (
                "completed", REAL_PROFILE_SUMMARY, _stored(fake),
            ),
            "malformed json with a marker": (
                "completed", FAKE_ANALYSIS_SUMMARY, _stored(fake)[:-5],
            ),
            "json that is not an object": (
                "completed", FAKE_ANALYSIS_SUMMARY, _stored([fake]),
            ),
            "not completed": ("failed", FAKE_ANALYSIS_SUMMARY, _stored(fake)),
            "marker only in the column": ("pending", FAKE_ANALYSIS_SUMMARY, None),
        }
        for name, row in cases.items():
            with self.subTest(name):
                self.assertEqual(classify_profile_analysis(*row), ANALYSIS_AMBIGUOUS)

        job = self.fake_job()
        job_cases = {
            "role tags differ": ("completed", _stored(dict(job, role_tags=["backend"])), "other"),
            "role family differs": ("completed", _stored(dict(job, role_family="software_engineering")), "other"),
            "role family column differs": ("completed", _stored(job), "software_engineering"),
            "subfamily differs": ("completed", _stored(dict(job, role_subfamily="backend")), "other"),
            "marker inside a real summary": (
                "completed", _stored(dict(job, summary=f"Real: {FAKE_ANALYSIS_SUMMARY}")), "other",
            ),
            "note text in the payload": (
                "completed", _stored(dict(job, summary=REAL_JOB_SUMMARY, dealbreakers=[FAKE_ANALYSIS_NOTE])), "other",
            ),
            "malformed json with a marker": ("completed", _stored(job)[:-5], "other"),
            "not completed": ("failed", _stored(job), "other"),
        }
        for name, row in job_cases.items():
            with self.subTest(name):
                self.assertEqual(classify_job_analysis(*row), ANALYSIS_AMBIGUOUS)

    def test_rows_without_markers_are_none(self):
        self.assertEqual(classify_profile_analysis("failed", None, None), ANALYSIS_NONE)
        self.assertEqual(classify_profile_analysis("failed", None, "not json"), ANALYSIS_NONE)
        self.assertEqual(classify_job_analysis("failed", None, None), ANALYSIS_NONE)
        self.assertEqual(classify_job_analysis("pending", "{}", None), ANALYSIS_NONE)

    def test_cover_letter_grounding(self):
        self.assertTrue(is_fake_cover_letter_grounding(json.dumps({
            "missing_information": ["something else", FAKE_ANALYSIS_NOTE + ": fake demo draft."],
        })))
        for grounding in (
            None, "", "not json", "[]",
            json.dumps({"missing_information": ["Real note mentioning " + FAKE_ANALYSIS_NOTE]}),
            json.dumps({"missing_information": FAKE_ANALYSIS_NOTE}),
            json.dumps({"facts": []}),
        ):
            with self.subTest(grounding=grounding):
                self.assertFalse(is_fake_cover_letter_grounding(grounding))


class _Response:
    def __init__(self, output_text):
        self.output_text = output_text


class _RealLikeClient(FakeAnalysisClient):
    """The fake payloads with normal summaries and no match note: valid for
    the schemas and the sanitizer, with no fake marker."""

    def __init__(self):
        super().__init__()
        self.calls = 0
        fake_create = self.responses.create

        def create(**kwargs):
            self.calls += 1
            payload = json.loads(fake_create(**kwargs).output_text)
            if "candidate_summary" in payload:
                payload.update(candidate_summary=REAL_PROFILE_SUMMARY, match_notes=[])
            else:
                payload["summary"] = REAL_JOB_SUMMARY
            return _Response(json.dumps(payload))

        self.responses.create = create


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RetireFakeAnalysesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_retire_fake_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
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
        self.script_env = {config.DATABASE_URL_ENV: f"sqlite:///{self.db_path}"}

    # -- helpers ---------------------------------------------------------

    def use_client(self, analysis_client):
        for module in (profiles, jobs):
            patcher = patch.object(module, "client", analysis_client)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add(self, row):
        session = self.session_factory()
        try:
            session.add(row)
            session.commit()
            return row.__mapper__.primary_key_from_instance(row)[0]
        finally:
            session.close()

    def create_user(self, tag):
        session = self.session_factory()
        try:
            user = models.User(
                name=f"Synthetic {tag}",
                mail=f"synthetic.{tag}@example.com",
                password_hash=hash_password("a-valid-synthetic-password"),
                is_admin=False,
            )
            session.add(user)
            session.commit()
            return user.user_id, {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}
        finally:
            session.close()

    def create_profile_and_job(self, tag, user_id, headers):
        profile = self.client.post(f"/users/{user_id}/profiles", headers=headers, json={
            "self_description": "Synthetic backend developer.",
            "target_role": "Backend Developer",
            "visa_sponsorship_needed": False,
            "work_authorization_status": "eu_eea",
            "years_of_experience": 3,
            "student_status": "not_enrolled",
            "current_residence_country": "Germany",
        })
        self.assertEqual(profile.status_code, 200, profile.text)
        profile_id = profile.json()["profile_id"]
        session = self.session_factory()
        try:
            session.get(models.CandidateProfile, profile_id).cv_text = "Synthetic CV: Python, FastAPI and SQL."
            session.commit()
        finally:
            session.close()
        job = self.client.post("/jobs/manual", headers=headers, json={
            "title": "Synthetic Backend Developer",
            "url": f"https://example.com/jobs/synthetic-retire-{tag}",
            "description_text": "Synthetic posting. You build APIs with Python, FastAPI and SQL.",
        })
        self.assertEqual(job.status_code, 201, job.text)
        return profile_id, job.json()["job_id"]

    def analyze(self, user_id, profile_id, job_id, headers, expected="created"):
        profile = self.client.post(f"/users/{user_id}/profiles/{profile_id}/analyze", headers=headers)
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["status"], expected)
        job = self.client.post(f"/jobs/{job_id}/analyze", headers=headers)
        self.assertEqual(job.status_code, 200, job.text)
        self.assertEqual(job.json()["status"], expected)
        return profile.json()["analysis_id"], job.json()["analysis_id"]

    def match(self, user_id, profile_id, job_id, headers):
        return self.client.post(f"/users/{user_id}/profiles/{profile_id}/jobs/{job_id}/match", headers=headers)

    def listed_match_ids(self, user_id, profile_id, headers):
        response = self.client.get(f"/users/{user_id}/profiles/{profile_id}/matches", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return {row["match_id"] for row in response.json()["results"]}

    def copy_analysis(self, model, analysis_id, **changes):
        session = self.session_factory()
        try:
            source = session.get(model, analysis_id)
            values = {
                column.key: getattr(source, column.key)
                for column in model.__table__.columns
                if column.key != "analysis_id"
            }
        finally:
            session.close()
        values.update(changes)
        return self.add(model(**values))

    def stored_match(self, profile_id, job_id, profile_analysis_id, job_analysis_id):
        return self.add(models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis_id,
            job_analysis_id=job_analysis_id,
            match_status="completed",
            match_json=json.dumps({"eligibility_status": "review_required"}),
            overall_score=50,
            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_VERSION,
            is_current=True,
        ))

    def dump(self):
        """Every row of every table, by table."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            return {
                table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                for table in tables
            }

    def current_flags(self, table, id_column):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return dict(conn.execute(f"SELECT {id_column}, is_current FROM {table}"))

    def without_is_current(self, dump):
        result = {}
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            for table, rows in dump.items():
                columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
                if "is_current" in columns:
                    index = columns.index("is_current")
                    rows = [row[:index] + row[index + 1:] for row in rows]
                result[table] = rows
        return result

    def run_script(self, argv, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = retire_fake_analyses.main(argv, environ=self.script_env if env is None else env)
        output = out.getvalue()
        report = json.loads(output[output.index("{"):]) if code == 0 else None
        return code, output, report

    def seed(self):
        """User A: fake analyses and match via the routes. User B: real-like
        analyses and match via the routes. Direct rows: an older fake
        analysis of each kind and current matches on them, an ambiguous
        profile analysis and job analysis, a current match on the ambiguous
        analysis, a current match without analysis ids, a cover letter with
        a fake and an edited version."""
        s = {}
        self.fake_client = FakeAnalysisClient()
        self.real_client = _RealLikeClient()

        s["user_a"], s["headers_a"] = self.create_user("a")
        s["profile_a"], s["job_a"] = self.create_profile_and_job("a", s["user_a"], s["headers_a"])
        with patch.object(profiles, "client", self.fake_client), patch.object(jobs, "client", self.fake_client):
            s["pa_a"], s["ja_a"] = self.analyze(s["user_a"], s["profile_a"], s["job_a"], s["headers_a"])
        created = self.match(s["user_a"], s["profile_a"], s["job_a"], s["headers_a"])
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["status"], "created")
        s["match_a"] = created.json()["match_id"]

        s["user_b"], s["headers_b"] = self.create_user("b")
        s["profile_b"], s["job_b"] = self.create_profile_and_job("b", s["user_b"], s["headers_b"])
        with patch.object(profiles, "client", self.real_client), patch.object(jobs, "client", self.real_client):
            s["pa_b"], s["ja_b"] = self.analyze(s["user_b"], s["profile_b"], s["job_b"], s["headers_b"])
        created = self.match(s["user_b"], s["profile_b"], s["job_b"], s["headers_b"])
        self.assertEqual(created.json()["status"], "created", created.text)
        s["match_b"] = created.json()["match_id"]

        # Older fake analyses, no longer current, still referenced by
        # current matches.
        s["pa_a_old"] = self.copy_analysis(models.ProfileAnalysis, s["pa_a"], is_current=False)
        s["ja_b_old"] = self.copy_analysis(models.JobAnalysis, s["ja_a"], job_id=s["job_b"], is_current=False)
        # Fake beats a missing id: retired.
        s["match_a_old"] = self.stored_match(s["profile_a"], s["job_a"], s["pa_a_old"], None)
        s["match_b_old"] = self.stored_match(s["profile_b"], s["job_b"], s["pa_b"], s["ja_b_old"])

        # Profile C: a current analysis quoting the marker in a real summary.
        s["user_c"], _ = self.create_user("c")
        s["profile_c"] = self.add(models.CandidateProfile(user_id=s["user_c"], target_role="Backend Developer"))
        quoted = f"Real summary quoting: {FAKE_ANALYSIS_SUMMARY}"
        s["pa_c"] = self.copy_analysis(
            models.ProfileAnalysis, s["pa_b"], profile_id=s["profile_c"], candidate_summary=quoted,
            analysis_json=json.dumps({"candidate_summary": quoted, "match_notes": []}),
        )
        # A failed job analysis whose broken payload carries the note.
        s["ja_b_amb"] = self.add(models.JobAnalysis(
            job_id=s["job_b"], analysis_status="failed", is_current=False,
            analysis_json='{"summary": "x", "notes": ["' + FAKE_ANALYSIS_NOTE,
        ))
        s["match_c"] = self.stored_match(s["profile_c"], s["job_b"], s["pa_c"], s["ja_b"])
        s["match_null"] = self.stored_match(s["profile_c"], s["job_a"], None, None)

        s["letter"] = self.add(models.CoverLetter(
            user_id=s["user_a"], profile_id=s["profile_a"], job_id=s["job_a"], language="en",
            job_title="Synthetic Backend Developer",
        ))
        for version_no, kind, grounding in (
            (1, "generated", {"facts": [], "used_fact_ids": [], "missing_information": [
                FAKE_ANALYSIS_NOTE + ": fake demo draft, not written by a language model."
            ]}),
            (2, "edited", None),
        ):
            self.add(models.CoverLetterVersion(
                letter_id=s["letter"], version_no=version_no, kind=kind, body_text="Synthetic letter.",
                grounding_json=json.dumps(grounding) if grounding else None, user_id=s["user_a"],
            ))
        return s

    def expected_report(self, s):
        return {
            "profile_analyses": {
                "current_fake_to_retire": 1,
                "ambiguous_ids": [s["pa_c"]],
                "other_current_untouched": 2,  # pa_b, pa_c
            },
            "job_analyses": {
                "current_fake_to_retire": 1,
                "ambiguous_ids": [s["ja_b_amb"]],
                "other_current_untouched": 1,  # ja_b
            },
            "matches": {
                "current_to_retire": 3,  # match_a, match_a_old, match_b_old
                "ambiguous_untouched_ids": [s["match_c"]],
                "missing_analysis_id_untouched_ids": [s["match_null"]],
            },
            "cover_letter_versions_with_fake_marker": 1,
            "profiles_needing_reanalysis": [s["profile_a"]],
            "jobs_needing_reanalysis": [s["job_a"]],
        }

    def assert_report(self, report, s, mode):
        self.assertEqual(report["mode"], mode)
        self.assertTrue(config._same_path(report["target"], self.db_path))
        for key, value in self.expected_report(s).items():
            self.assertEqual(report[key], value, key)

    # -- tests -----------------------------------------------------------

    def test_stored_fake_rows_classify_as_fake_and_real_like_rows_as_none(self):
        s = self.seed()
        session = self.session_factory()
        try:
            def kind(model, analysis_id):
                row = session.get(model, analysis_id)
                if model is models.ProfileAnalysis:
                    return classify_profile_analysis(row.analysis_status, row.candidate_summary, row.analysis_json)
                return classify_job_analysis(row.analysis_status, row.analysis_json, row.role_family)

            self.assertEqual(kind(models.ProfileAnalysis, s["pa_a"]), ANALYSIS_FAKE)
            self.assertEqual(kind(models.JobAnalysis, s["ja_a"]), ANALYSIS_FAKE)
            self.assertEqual(kind(models.ProfileAnalysis, s["pa_b"]), ANALYSIS_NONE)
            self.assertEqual(kind(models.JobAnalysis, s["ja_b"]), ANALYSIS_NONE)
            self.assertEqual(kind(models.ProfileAnalysis, s["pa_c"]), ANALYSIS_AMBIGUOUS)
            self.assertEqual(kind(models.JobAnalysis, s["ja_b_amb"]), ANALYSIS_AMBIGUOUS)
        finally:
            session.close()

    def test_dry_run_reports_and_writes_nothing(self):
        s = self.seed()
        self.engine.dispose()
        before = _sha256(self.db_path)
        code, output, report = self.run_script([])
        self.assertEqual(code, 0, output)
        self.assertIn("DRY RUN: nothing was written", output)
        self.assert_report(report, s, "dry_run")
        self.assertNotIn("retired", report)
        self.assertEqual(_sha256(self.db_path), before)
        # Ids and counts only: no analysis text, CV text, names or mails.
        for secret in (FAKE_ANALYSIS_SUMMARY, REAL_PROFILE_SUMMARY, "Synthetic CV", "synthetic.a@example.com", "Synthetic a"):
            self.assertNotIn(secret, output)

    def test_apply_retires_fake_analyses_and_their_matches_only(self):
        s = self.seed()
        self.assertEqual(self.listed_match_ids(s["user_a"], s["profile_a"], s["headers_a"]), {s["match_a"], s["match_a_old"]})
        self.assertEqual(self.listed_match_ids(s["user_b"], s["profile_b"], s["headers_b"]), {s["match_b"], s["match_b_old"]})
        before = self.dump()
        flags_before = {
            "profile_analysis": self.current_flags("profile_analysis", "analysis_id"),
            "job_analysis": self.current_flags("job_analysis", "analysis_id"),
            "job_matches": self.current_flags("job_matches", "match_id"),
        }

        code, output, report = self.run_script(["--yes"])
        self.assertEqual(code, 0, output)
        self.assertTrue(output.startswith("Database: "), output)
        self.assertIn("APPLIED", output)
        self.assert_report(report, s, "apply")
        self.assertEqual(report["retired"], {"profile_analyses": 1, "job_analyses": 1, "matches": 3})

        after = self.dump()
        # No row added or deleted anywhere, and only is_current changed.
        self.assertEqual({t: len(r) for t, r in after.items()}, {t: len(r) for t, r in before.items()})
        for table in ("users", "candidate_profiles", "jobs", "cover_letters", "cover_letter_versions",
                      "analysis_guards", "analysis_quota_reservations"):
            self.assertEqual(after[table], before[table], table)
        self.assertEqual(self.without_is_current(after), self.without_is_current(before))
        retired = {
            "profile_analysis": {s["pa_a"]},
            "job_analysis": {s["ja_a"]},
            "job_matches": {s["match_a"], s["match_a_old"], s["match_b_old"]},
        }
        for table, id_column in (("profile_analysis", "analysis_id"), ("job_analysis", "analysis_id"),
                                 ("job_matches", "match_id")):
            flags = self.current_flags(table, id_column)
            expected = {
                row_id: (0 if row_id in retired[table] else flag)
                for row_id, flag in flags_before[table].items()
            }
            self.assertEqual(flags, expected, table)
        for row_id in retired["job_matches"]:
            self.assertEqual(flags_before["job_matches"][row_id], 1)
        for row_id in (s["match_b"], s["match_c"], s["match_null"]):
            self.assertEqual(self.current_flags("job_matches", "match_id")[row_id], 1)

        # Idempotent: a second run retires nothing and changes nothing.
        code, output, again = self.run_script(["--yes"])
        self.assertEqual(code, 0, output)
        self.assertEqual(again["retired"], {"profile_analyses": 0, "job_analyses": 0, "matches": 0})
        self.assertEqual(again["profile_analyses"]["current_fake_to_retire"], 0)
        self.assertEqual(again["matches"]["current_to_retire"], 0)
        # Still listed until they are analyzed again.
        self.assertEqual(again["profiles_needing_reanalysis"], [s["profile_a"]])
        self.assertEqual(again["jobs_needing_reanalysis"], [s["job_a"]])
        self.assertEqual(self.dump(), after)

    def test_retired_rows_are_not_reused(self):
        s = self.seed()
        code, output, _ = self.run_script(["--yes"])
        self.assertEqual(code, 0, output)

        # (a) The retired matches are no longer listed; the real-like one is.
        self.assertEqual(self.listed_match_ids(s["user_a"], s["profile_a"], s["headers_a"]), set())
        self.assertEqual(self.listed_match_ids(s["user_b"], s["profile_b"], s["headers_b"]), {s["match_b"]})

        # (b) Matching does not return the cached fake match.
        refused = self.match(s["user_a"], s["profile_a"], s["job_a"], s["headers_a"])
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(refused.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING")

        # (c) Analysis runs the provider again instead of using the cache.
        # The cooldowns of the first analyses are expired here, as they
        # would be long before a real switch of provider.
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE analysis_guards SET cooldown_until = NULL")
            conn.commit()
        self.use_client(self.real_client)
        calls = self.real_client.calls
        pa_new, ja_new = self.analyze(s["user_a"], s["profile_a"], s["job_a"], s["headers_a"], expected="created")
        self.assertEqual(self.real_client.calls, calls + 2)
        self.assertNotIn(pa_new, (s["pa_a"], s["pa_a_old"]))
        self.assertNotEqual(ja_new, s["ja_a"])

        created = self.match(s["user_a"], s["profile_a"], s["job_a"], s["headers_a"])
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["status"], "created")
        self.assertEqual((body["profile_analysis_id"], body["job_analysis_id"]), (pa_new, ja_new))
        cached = self.match(s["user_a"], s["profile_a"], s["job_a"], s["headers_a"])
        self.assertEqual(cached.json()["status"], "cached", cached.text)
        self.assertEqual(cached.json()["match_id"], body["match_id"])
        self.assertNotIn(body["match_id"], (s["match_a"], s["match_a_old"]))
        self.assertEqual(self.listed_match_ids(s["user_a"], s["profile_a"], s["headers_a"]), {body["match_id"]})

        session = self.session_factory()
        try:
            for match_id in (s["match_a"], s["match_a_old"]):
                self.assertIs(session.get(models.JobMatch, match_id).is_current, False)
        finally:
            session.close()

        # Re-analyzed: no longer reported as needing it.
        code, output, report = self.run_script([])
        self.assertEqual(code, 0, output)
        self.assertEqual(report["profiles_needing_reanalysis"], [])
        self.assertEqual(report["jobs_needing_reanalysis"], [])

    def test_locked_database_is_refused_and_unchanged(self):
        self.seed()
        self.engine.dispose()
        before = _sha256(self.db_path)
        holder = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            # Waits for sqlite3's default 5 s busy timeout.
            code, output, _ = self.run_script(["--yes"])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(code, 1)
        self.assertIn("Refusing to continue:", output)
        self.assertIn("database is locked", output)
        self.assertNotIn("APPLIED", output)
        self.assertEqual(_sha256(self.db_path), before)

    def test_failed_apply_is_rolled_back(self):
        self.seed()
        self.engine.dispose()
        before = _sha256(self.db_path)
        real_retire = retire_fake_analyses._retire

        def failing_retire(conn, table, id_column, ids):
            if table.name == "job_matches":
                raise sqlite3.OperationalError("synthetic failure")
            return real_retire(conn, table, id_column, ids)

        with patch.object(retire_fake_analyses, "_retire", failing_retire):
            code, output, _ = self.run_script(["--yes"])
        self.assertEqual(code, 1)
        self.assertIn("Refusing to continue: synthetic failure", output)
        self.assertNotIn("APPLIED", output)
        self.assertEqual(_sha256(self.db_path), before)


class RefusalTests(unittest.TestCase):
    """Runs in a temp working directory with a fake repository root, so the
    default-location checks never resolve the real repository's files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apply101_retire_fake_refusal_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = self.tmp / "cwd"
        self.fake_repo = self.tmp / "fake_repo"
        self.cwd.mkdir()
        self.fake_repo.mkdir()
        original_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, original_cwd)
        root_patcher = patch.object(config, "REPO_ROOT", self.fake_repo)
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

    def make_db(self, path):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE notes (value TEXT)")
            conn.commit()
        return _sha256(path)

    def refuse(self, env):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for argv in ([], ["--yes"]):
                self.assertEqual(retire_fake_analyses.main(argv, environ=env), 1)
        self.assertIn("Refusing to continue:", out.getvalue())
        return out.getvalue()

    def assert_target_printed_first(self, output):
        self.assertIn("Database: ", output)
        self.assertLess(output.index("Database: "), output.index("Refusing to continue:"))

    def test_unset_or_blank_url(self):
        for env in ({}, {config.DATABASE_URL_ENV: "  "}):
            with self.subTest(env=env):
                self.refuse(env)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_sqlite_uri_and_non_file_urls(self):
        for url in (
            "sqlite:///file:apply101.db?uri=true",
            f"sqlite:///{self.tmp / 'x.db'}?uri=true",
            "sqlite://",
            "sqlite:///:memory:",
            "postgresql://user@localhost/apply101",
        ):
            with self.subTest(url=url):
                self.refuse({config.DATABASE_URL_ENV: url})
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertFalse((self.tmp / "x.db").exists())

    def test_missing_file_is_not_created(self):
        missing = self.tmp / "missing.db"
        output = self.refuse({config.DATABASE_URL_ENV: f"sqlite:///{missing}"})
        self.assert_target_printed_first(output)
        self.assertFalse(missing.exists())

    def test_repository_database(self):
        real = self.fake_repo / "apply101.db"
        digest = self.make_db(real)
        output = self.refuse({config.DATABASE_URL_ENV: f"sqlite:///{real}"})
        self.assert_target_printed_first(output)
        self.assertIn("real apply101.db", output)
        self.assertEqual(_sha256(real), digest)

    def test_dry_run_connection_cannot_write(self):
        path = self.tmp / "readonly.db"
        digest = self.make_db(path)
        engine = retire_fake_analyses._engine(path, writable=False)
        try:
            with engine.connect() as conn, self.assertRaises(Exception) as caught:
                conn.exec_driver_sql("INSERT INTO notes VALUES ('x')")
        finally:
            engine.dispose()
        self.assertIn("readonly", str(caught.exception).lower())
        self.assertEqual(_sha256(path), digest)

    def test_default_relative_database(self):
        local = self.cwd / "apply101.db"
        digest = self.make_db(local)
        for url in ("sqlite:///./apply101.db", "sqlite:///apply101.db", f"sqlite:///{local}"):
            with self.subTest(url=url):
                output = self.refuse({config.DATABASE_URL_ENV: url})
                self.assert_target_printed_first(output)
        self.assertEqual(_sha256(local), digest)


if __name__ == "__main__":
    unittest.main()
