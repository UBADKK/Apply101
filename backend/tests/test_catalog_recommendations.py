"""Catalog browse and "jobs for me": active catalog jobs only, filters and
pagination, ranking with the existing rule-based matching, reported
coverage, no writes and no analysis calls. Throwaway SQLite, the
deterministic fake provider, no network."""

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

from backend.app import catalog_sync, models
from backend.app.database import Base, get_db
from backend.app.fake_analysis import FakeAnalysisClient
from backend.app.job_sources import BoardFetch, NormalizedPosting
from backend.app.security import create_access_token, hash_password

with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"}, clear=False):
    from backend.routers import catalog, jobs, profiles


SYNTHETIC_SECRET = "synthetic-test-secret-for-recommendations-0123456789ab"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class _CountingFake(FakeAnalysisClient):
    calls = 0

    def with_options(self, **kwargs):
        _CountingFake.calls += 1
        return super().with_options(**kwargs)


def posting(key, title, description, **extra):
    return NormalizedPosting(
        source="greenhouse", source_key=f"acme:{key}", title=title,
        url=f"https://example.test/jobs/{key}", description_text=description,
        company_name="Acme", location=extra.pop("location", "Berlin, Germany"), **extra,
    )


class CatalogRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_recommendations_test_")
        self.engine = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        for router in (profiles.router, jobs.router, catalog.router):
            app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        env = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.fake = _CountingFake()
        for module in (profiles, jobs):
            patcher = patch.object(module, "client", self.fake)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.admin_id, self.admin = self._user("admin@example.com", True)
        self.user_id, self.user = self._user("user@example.com", False)
        self.other_id, self.other = self._user("other@example.com", False)

        db = self.Session()
        board, _ = catalog_sync.create_board(db, company_name="Acme", source="greenhouse", board_token="acme", region="global", now=NOW)
        catalog_sync.apply_postings(db, source="greenhouse", board=board, now=NOW, board_company=db.get(models.Company, board.company_id), fetch=BoardFetch(complete=True, postings=[
            posting(1, "Backend Developer", "Build APIs with Python, FastAPI and SQL. Fluent English (C1) is required.", work_mode="remote", country_code="DE"),
            posting(2, "Backend Developer (German)", "Python and SQL. German C1 is required.", country_code="DE"),
            posting(3, "Office Manager", "Organize the office.", location="Istanbul, Turkey", country_code="TR", work_mode="onsite"),
            posting(4, "Closed role", "Python.", country_code="DE"),
        ]))
        db.commit()
        catalog_sync.apply_postings(db, source="greenhouse", board=board, now=NOW, board_company=db.get(models.Company, board.company_id), fetch=BoardFetch(complete=True, postings=[
            posting(1, "Backend Developer", "Build APIs with Python, FastAPI and SQL. Fluent English (C1) is required.", work_mode="remote", country_code="DE"),
            posting(2, "Backend Developer (German)", "Python and SQL. German C1 is required.", country_code="DE"),
            posting(3, "Office Manager", "Organize the office.", location="Istanbul, Turkey", country_code="TR", work_mode="onsite"),
        ]))
        db.commit()
        self.job_ids = {job.title: job.job_id for job in db.query(models.Job).all()}
        db.add(models.Job(title="Someone's private job", url="https://example.test/private", description_text="Python",
                          source="manual", created_by_user_id=self.other_id))
        db.commit()
        db.close()

        for title in ("Backend Developer", "Backend Developer (German)", "Closed role"):
            response = self.client.post(f"/jobs/{self.job_ids[title]}/analyze", headers=self.admin)
            self.assertEqual(response.status_code, 200, response.text)

        profile = self.client.post(f"/users/{self.user_id}/profiles", headers=self.user, json={
            "self_description": "Backend developer.", "target_role": "Backend Developer",
            "visa_sponsorship_needed": False, "work_authorization_status": "eu_eea",
            "years_of_experience": 3, "student_status": "not_enrolled", "current_residence_country": "Germany",
            "languages": [{"language_code": "en", "language_name": "English", "proficiency_level": "C1", "proficiency_scale": "CEFR", "is_primary": True}],
        })
        self.assertEqual(profile.status_code, 200, profile.text)
        self.profile_id = profile.json()["profile_id"]

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _user(self, mail, is_admin):
        db = self.Session()
        user = models.User(name="U", mail=mail, password_hash=hash_password("a-valid-synthetic-password"), is_admin=is_admin)
        db.add(user)
        db.commit()
        result = user.user_id, {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}
        db.close()
        return result

    def _analyze_profile(self):
        response = self.client.post(f"/users/{self.user_id}/profiles/{self.profile_id}/analyze", headers=self.user)
        self.assertEqual(response.status_code, 200, response.text)

    def test_catalog_lists_active_catalog_jobs_with_filters(self):
        body = self.client.get("/catalog/jobs", headers=self.user).json()
        titles = {item["title"] for item in body["items"]}
        self.assertEqual(body["total"], 3)
        self.assertEqual(titles, {"Backend Developer", "Backend Developer (German)", "Office Manager"})
        item = next(i for i in body["items"] if i["title"] == "Backend Developer")
        self.assertTrue(item["has_current_analysis"])
        self.assertEqual(item["source_label"], "Greenhouse Job Board")
        self.assertIn("countries", item["remote_note"])
        self.assertFalse(next(i for i in body["items"] if i["title"] == "Office Manager")["has_current_analysis"])

        self.assertEqual(self.client.get("/catalog/jobs?work_mode=remote", headers=self.user).json()["total"], 1)
        self.assertEqual(self.client.get("/catalog/jobs?country=tr", headers=self.user).json()["total"], 1)
        self.assertEqual(self.client.get("/catalog/jobs?q=office", headers=self.user).json()["total"], 1)
        self.assertEqual(self.client.get("/catalog/jobs?source=lever", headers=self.user).json()["total"], 0)
        page = self.client.get("/catalog/jobs?page=2&page_size=2", headers=self.user).json()
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(self.client.get("/catalog/jobs?page_size=51", headers=self.user).status_code, 422)
        self.assertEqual(self.client.get("/catalog/jobs?work_mode=moon", headers=self.user).status_code, 422)
        self.assertEqual(self.client.get("/catalog/jobs").status_code, 401)
        filters = self.client.get("/catalog/filters", headers=self.user).json()
        self.assertEqual(filters["sources"][0]["source"], "greenhouse")

    def test_browsing_starts_no_analysis(self):
        before = _CountingFake.calls
        self.client.get("/catalog/jobs", headers=self.user)
        self._analyze_profile()
        after_profile = _CountingFake.calls
        self.client.get(f"/users/{self.user_id}/profiles/{self.profile_id}/recommendations", headers=self.user)
        self.assertEqual(after_profile - before, 1)  # only the explicit profile analysis
        self.assertEqual(_CountingFake.calls, after_profile)

    def test_recommendations_need_profile_analysis(self):
        response = self.client.get(f"/users/{self.user_id}/profiles/{self.profile_id}/recommendations", headers=self.user)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING")

    def test_ranking_coverage_and_no_writes(self):
        self._analyze_profile()
        url = f"/users/{self.user_id}/profiles/{self.profile_id}/recommendations"
        body = self.client.get(url, headers=self.user).json()
        coverage = body["coverage"]
        self.assertEqual((coverage["active_jobs"], coverage["analyzed_jobs"], coverage["evaluated_jobs"], coverage["not_analyzed_jobs"]), (3, 2, 2, 1))
        self.assertFalse(coverage["capped"])
        self.assertEqual(coverage["status_counts"]["ineligible"], 1)
        self.assertEqual([item["title"] for item in body["items"]], ["Backend Developer"])
        self.assertEqual(body["items"][0]["eligibility_status"], "eligible")

        with_ineligible = self.client.get(url + "?include_ineligible=true", headers=self.user).json()
        self.assertEqual([item["eligibility_status"] for item in with_ineligible["items"]], ["eligible", "ineligible"])
        codes = [reason["code"] for reason in with_ineligible["items"][1]["hard_fail_reasons"]]
        self.assertIn("required_language_missing", codes)

        db = self.Session()
        try:
            self.assertEqual(db.query(models.JobMatch).count(), 0)
        finally:
            db.close()

    def test_content_change_drops_job_from_recommendations(self):
        self._analyze_profile()
        db = self.Session()
        board = db.query(models.JobSourceBoard).one()
        catalog_sync.apply_postings(db, source="greenhouse", board=board, now=NOW, board_company=db.get(models.Company, board.company_id), fetch=BoardFetch(complete=False, postings=[
            posting(1, "Backend Developer", "Now also German C1 required.", work_mode="remote", country_code="DE"),
        ]))
        db.commit()
        db.close()
        body = self.client.get(f"/users/{self.user_id}/profiles/{self.profile_id}/recommendations?include_ineligible=true", headers=self.user).json()
        self.assertNotIn("Backend Developer", [item["title"] for item in body["items"]])
        self.assertEqual(body["coverage"]["analyzed_jobs"], 1)

    def test_other_users_cannot_read_my_recommendations(self):
        self._analyze_profile()
        response = self.client.get(f"/users/{self.user_id}/profiles/{self.profile_id}/recommendations", headers=self.other)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
