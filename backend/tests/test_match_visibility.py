"""Job visibility for the match endpoints.

Visibility follows the PROFILE OWNER (profile.user_id), never the caller:
an admin acting on another user's profile gets no bypass. A job the
profile owner can't see must be indistinguishable from a nonexistent
job_id, and must never be matched, listed, or counted.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.analysis_contract import MATCH_MODEL, MATCH_VERSION
from backend.app.database import Base, get_db
from backend.app.security import create_access_token
from backend.routers import matches


# matches.py has no OpenAI dependency (matching is rule-based and every
# analysis below is pre-seeded), so no synthetic API key is needed.
SYNTHETIC_SECRET = "synthetic-test-secret-for-match-visibility-0123456789"
NONEXISTENT_JOB_ID = 999999

PROFILE_ANALYSIS_JSON = {
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


class _BaseMatchVisibilityTestCase(unittest.TestCase):
    """Throwaway synthetic SQLite database (never apply101.db), only
    matches.router mounted, synthetic JWT secret."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_match_visibility_test_")
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
        app.include_router(matches.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self.user_a_id = self._create_user("matchvis.a@example.com")
        self.user_b_id = self._create_user("matchvis.b@example.com")
        self.admin_id = self._create_user("matchvis.admin@example.com", is_admin=True)

        self.profile_a_id = self._create_profile(self.user_a_id)
        self.profile_b_id = self._create_profile(self.user_b_id)
        self.profile_a_analysis_id = self._seed_profile_analysis(self.profile_a_id)
        self.profile_b_analysis_id = self._seed_profile_analysis(self.profile_b_id)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # --- seeding helpers -------------------------------------------------

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
        return self._add(
            models.User(name="Synthetic User", mail=mail, is_admin=is_admin)
        ).user_id

    def _create_profile(self, user_id):
        return self._add(
            models.CandidateProfile(user_id=user_id, self_description="synthetic")
        ).profile_id

    def _seed_profile_analysis(self, profile_id):
        return self._add(models.ProfileAnalysis(
            profile_id=profile_id,
            analysis_status="completed",
            analysis_json=json.dumps(PROFILE_ANALYSIS_JSON),
            target_role_tags_json='["other"]',
            analysis_model=matches.REQUIRED_PROFILE_ANALYSIS_MODEL,
            analysis_prompt_version=matches.REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION,
            is_current=True,
        )).analysis_id

    def _create_job(self, slug, owner_id=None, analyzed=True):
        job_id = self._add(models.Job(
            title=f"Synthetic Job {slug}",
            url=f"https://example.com/job/{slug}",
            description_text="synthetic description",
            created_by_user_id=owner_id,
        )).job_id
        if analyzed:
            self._seed_job_analysis(job_id)
        return job_id

    def _seed_job_analysis(self, job_id):
        return self._add(models.JobAnalysis(
            job_id=job_id,
            analysis_status="completed",
            analysis_json=json.dumps({"hard_requirements": {}}),
            role_tags_json='["other"]',
            analysis_model=matches.REQUIRED_JOB_ANALYSIS_MODEL,
            analysis_prompt_version=matches.REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
            is_current=True,
        )).analysis_id

    def _current_job_analysis_id(self, job_id):
        session = self.session_factory()
        try:
            return session.query(models.JobAnalysis.analysis_id).filter(
                models.JobAnalysis.job_id == job_id,
                models.JobAnalysis.is_current == True,
            ).scalar()
        finally:
            session.close()

    def _seed_match(self, profile_id, profile_analysis_id, job_id, score):
        return self._add(models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis_id,
            job_analysis_id=self._current_job_analysis_id(job_id),
            match_status="completed",
            match_json=json.dumps({"eligibility_status": "eligible"}),
            overall_score=score,
            recommendation="maybe",
            match_model=MATCH_MODEL,
            match_prompt_version=matches.MATCH_PROMPT_VERSION,
            matched_at=datetime.now(timezone.utc),
            is_current=True,
        )).match_id

    def _match_rows(self):
        session = self.session_factory()
        try:
            return sorted(
                (row.match_id, row.profile_id, row.job_id, row.is_current,
                 row.match_status, row.overall_score)
                for row in session.query(models.JobMatch).all()
            )
        finally:
            session.close()

    def _headers(self, user_id):
        # Mint with the user's real stored token key, exactly as login does.
        session = self.session_factory()
        try:
            user = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one()
            token_key = user.token_key
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}


class SingleMatchVisibilityTests(_BaseMatchVisibilityTestCase):
    def setUp(self):
        super().setUp()
        self.ownerless_id = self._create_job("ownerless")
        self.a_job_id = self._create_job("owned-by-a", owner_id=self.user_a_id)
        self.b_job_id = self._create_job("owned-by-b", owner_id=self.user_b_id)

    def _match(self, caller_id, job_id, force_rematch=False):
        return self.client.post(
            f"/users/{self.user_a_id}/profiles/{self.profile_a_id}"
            f"/jobs/{job_id}/match",
            params={"force_rematch": "true"} if force_rematch else None,
            headers=self._headers(caller_id),
        )

    def _expected_not_found_body(self, job_id):
        return {
            "detail": {
                "error_code": "ERR_JOB_NOT_FOUND",
                "message": f"Job with id {job_id} was not found.",
            }
        }

    def test_owner_can_match_own_job(self):
        response = self._match(self.user_a_id, self.a_job_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        self.assertEqual(response.json()["job_id"], self.a_job_id)

    def test_owner_can_match_ownerless_job(self):
        response = self._match(self.user_a_id, self.ownerless_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")

    def test_admin_on_owner_profile_can_match_owner_job(self):
        response = self._match(self.admin_id, self.a_job_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")

    def test_nonexistent_job_body_unchanged(self):
        response = self._match(self.user_a_id, NONEXISTENT_JOB_ID)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(), self._expected_not_found_body(NONEXISTENT_JOB_ID)
        )

    def test_foreign_job_is_404_identical_to_nonexistent_and_writes_nothing(self):
        before = self._match_rows()

        with patch(
            "backend.routers.matches.calculate_backend_match"
        ) as mock_calc:
            for caller_id in (self.user_a_id, self.admin_id):
                for force_rematch in (False, True):
                    response = self._match(caller_id, self.b_job_id, force_rematch)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(
                        response.json(),
                        self._expected_not_found_body(self.b_job_id),
                    )

        mock_calc.assert_not_called()
        self.assertEqual(self._match_rows(), before)

    def test_foreign_job_404_is_byte_identical_once_the_job_is_gone(self):
        # Same job_id, first while it exists (foreign), then after it is
        # removed entirely: the two responses must be byte-identical.
        foreign = self._match(self.user_a_id, self.b_job_id)

        session = self.session_factory()
        try:
            session.query(models.JobAnalysis).filter(
                models.JobAnalysis.job_id == self.b_job_id
            ).delete(synchronize_session=False)
            session.query(models.Job).filter(
                models.Job.job_id == self.b_job_id
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

        missing = self._match(self.user_a_id, self.b_job_id)

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.status_code, missing.status_code)
        self.assertEqual(foreign.content, missing.content)
        self.assertEqual(
            foreign.headers.get("content-type"), missing.headers.get("content-type")
        )

    def test_foreign_job_without_analysis_does_not_leak_analysis_400(self):
        # An unanalyzed foreign job must not reach the job-analysis 400,
        # which would reveal the job exists (and its analysis metadata).
        b_unanalyzed = self._create_job(
            "owned-by-b-unanalyzed", owner_id=self.user_b_id, analyzed=False
        )
        response = self._match(self.user_a_id, b_unanalyzed)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), self._expected_not_found_body(b_unanalyzed))

    def test_foreign_job_with_existing_cached_match_is_still_404(self):
        # Stale data: a current JobMatch for (A's profile, B's job) exists
        # and would otherwise be returned from the cache path.
        self._seed_match(
            self.profile_a_id, self.profile_a_analysis_id, self.b_job_id, 99
        )
        before = self._match_rows()

        for caller_id in (self.user_a_id, self.admin_id):
            for force_rematch in (False, True):
                response = self._match(caller_id, self.b_job_id, force_rematch)
                self.assertEqual(response.status_code, 404)
                # Exact body: nothing from the cached match leaks.
                self.assertEqual(
                    response.json(), self._expected_not_found_body(self.b_job_id)
                )

        # No row created, no is_current flip.
        self.assertEqual(self._match_rows(), before)


class BulkMatchVisibilityTests(_BaseMatchVisibilityTestCase):
    def setUp(self):
        super().setUp()
        # Creation order matters: the bulk route orders by job_id DESC, so
        # interleaving B's jobs means post-filtering after offset/limit
        # would return a different set than filtering in SQL.
        self.ownerless_id = self._create_job("ownerless")
        self.a_job_1 = self._create_job("a-1", owner_id=self.user_a_id)
        self.b_job_1 = self._create_job("b-1", owner_id=self.user_b_id)
        self.a_job_2 = self._create_job("a-2", owner_id=self.user_a_id)
        self.b_job_2 = self._create_job("b-2", owner_id=self.user_b_id)

    def _run(self, caller_id, **params):
        response = self.client.post(
            f"/users/{self.user_a_id}/profiles/{self.profile_a_id}"
            "/jobs/match-analyzed",
            params=params,
            headers=self._headers(caller_id),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _result_job_ids(self, body):
        return sorted(item["job_id"] for item in body["results"])

    def test_owner_run_includes_ownerless_and_own_jobs_only(self):
        for caller_id in (self.user_a_id, self.admin_id):
            body = self._run(caller_id, limit=50)
            self.assertEqual(
                self._result_job_ids(body),
                sorted([self.ownerless_id, self.a_job_1, self.a_job_2]),
            )
            self.assertEqual(body["selected_job_count"], 3)
            self.assertEqual(body["failed_count"], 0)
            for item in body["results"]:
                self.assertIn(item["status"], ("created", "cached"))
            self.assertNotIn("b-1", json.dumps(body))
            self.assertNotIn("b-2", json.dumps(body))

        session = self.session_factory()
        try:
            foreign_matches = session.query(models.JobMatch).filter(
                models.JobMatch.job_id.in_([self.b_job_1, self.b_job_2])
            ).count()
        finally:
            session.close()
        self.assertEqual(foreign_matches, 0)

    def test_visibility_is_applied_before_offset_and_limit(self):
        # Visible jobs, job_id DESC: a_job_2, a_job_1, ownerless.
        for caller_id in (self.user_a_id, self.admin_id):
            first_page = self._run(caller_id, limit=2, offset=0)
            self.assertEqual(
                self._result_job_ids(first_page),
                sorted([self.a_job_2, self.a_job_1]),
            )
            self.assertEqual(first_page["selected_job_count"], 2)

            second_page = self._run(caller_id, limit=2, offset=2)
            self.assertEqual(self._result_job_ids(second_page), [self.ownerless_id])
            self.assertEqual(second_page["selected_job_count"], 1)

            past_end = self._run(caller_id, limit=2, offset=3)
            self.assertEqual(past_end["status"], "no_analyzed_jobs_found")
            self.assertEqual(past_end["results"], [])


class MatchListVisibilityTests(_BaseMatchVisibilityTestCase):
    def setUp(self):
        super().setUp()
        self.ownerless_id = self._create_job("ownerless")
        self.a_job_id = self._create_job("owned-by-a", owner_id=self.user_a_id)
        self.b_job_id = self._create_job("owned-by-b", owner_id=self.user_b_id)

        # Stale foreign match gets the highest score so it would sort first
        # (and shift pagination) if it weren't filtered in SQL.
        self.stale_foreign_match = self._seed_match(
            self.profile_a_id, self.profile_a_analysis_id, self.b_job_id, 99
        )
        self.a_match = self._seed_match(
            self.profile_a_id, self.profile_a_analysis_id, self.a_job_id, 80
        )
        self.ownerless_match = self._seed_match(
            self.profile_a_id, self.profile_a_analysis_id, self.ownerless_id, 60
        )

    def _list(self, caller_id, **params):
        response = self.client.get(
            f"/users/{self.user_a_id}/profiles/{self.profile_a_id}/matches",
            params=params,
            headers=self._headers(caller_id),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_stale_foreign_match_is_excluded_from_results_and_total(self):
        for caller_id in (self.user_a_id, self.admin_id):
            body = self._list(caller_id)
            self.assertEqual(body["total_count"], 2)
            self.assertEqual(body["returned_count"], 2)
            self.assertEqual(
                [item["match_id"] for item in body["results"]],
                [self.a_match, self.ownerless_match],
            )
            self.assertNotIn("owned-by-b", json.dumps(body))
            self.assertEqual(body["match_version"], MATCH_VERSION)

    def test_pagination_counts_and_slices_only_visible_matches(self):
        for caller_id in (self.user_a_id, self.admin_id):
            first = self._list(caller_id, limit=1, offset=0)
            self.assertEqual(first["total_count"], 2)
            self.assertEqual(
                [item["match_id"] for item in first["results"]], [self.a_match]
            )

            second = self._list(caller_id, limit=1, offset=1)
            self.assertEqual(second["total_count"], 2)
            self.assertEqual(
                [item["match_id"] for item in second["results"]],
                [self.ownerless_match],
            )

            third = self._list(caller_id, limit=1, offset=2)
            self.assertEqual(third["total_count"], 2)
            self.assertEqual(third["results"], [])

    def test_stale_foreign_match_row_is_not_deleted(self):
        self._list(self.user_a_id)
        match_ids = [row[0] for row in self._match_rows()]
        self.assertIn(self.stale_foreign_match, match_ids)

    def test_profile_b_owner_sees_own_job_match(self):
        # Sanity check that the filter is owner-scoped rather than simply
        # "ownerless only": B's profile keeps its match against B's job.
        b_match = self._seed_match(
            self.profile_b_id, self.profile_b_analysis_id, self.b_job_id, 70
        )
        response = self.client.get(
            f"/users/{self.user_b_id}/profiles/{self.profile_b_id}/matches",
            headers=self._headers(self.user_b_id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["match_id"] for item in response.json()["results"]], [b_match]
        )


if __name__ == "__main__":
    unittest.main()
