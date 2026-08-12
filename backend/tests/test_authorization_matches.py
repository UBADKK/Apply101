import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password
from backend.routers import matches


# matches.py has no OpenAI dependency at all (no `client = OpenAI()` at
# import time, unlike profiles.py/jobs.py), so no synthetic API key is
# needed here.
SYNTHETIC_SECRET = "synthetic-test-secret-for-authz-matches-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"


def build_test_app(engine):
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(matches.router)
    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


class _BaseMatchesAuthorizationTestCase(unittest.TestCase):
    """Shared setup: a throwaway synthetic SQLite database (never
    apply101.db), only matches.router mounted, a synthetic JWT secret, and
    helpers for creating synthetic users/profiles/jobs and minting real
    access tokens without going through /auth/login.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_authz_matches_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)

        self.app, self.session_factory = build_test_app(self.engine)
        self.client = TestClient(self.app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_user(
        self, mail, password=VALID_PASSWORD, is_admin=False, name="Synthetic User"
    ):
        session = self.session_factory()
        try:
            user = models.User(
                name=name,
                mail=mail,
                password_hash=hash_password(password) if password else None,
                is_admin=is_admin,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _create_profile(self, user_id, self_description="synthetic profile"):
        session = self.session_factory()
        try:
            profile = models.CandidateProfile(
                user_id=user_id, self_description=self_description
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            return profile.profile_id
        finally:
            session.close()

    def _create_job(
        self, url="https://example.com/job/synthetic-1", title="Synthetic Job"
    ):
        session = self.session_factory()
        try:
            job = models.Job(
                title=title, url=url, description_text="synthetic description"
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return job.job_id
        finally:
            session.close()

    def _auth_headers(self, user_id):
        token = create_access_token(user_id)
        return {"Authorization": f"Bearer {token}"}


class GetProfileMatchesAuthorizationTests(_BaseMatchesAuthorizationTestCase):
    def test_missing_token_is_401(self):
        user_id = self._create_user("matches.missing@example.com")
        profile_id = self._create_profile(user_id)
        response = self.client.get(f"/users/{user_id}/profiles/{profile_id}/matches")
        self.assertEqual(response.status_code, 401)

    def test_malformed_token_is_401(self):
        user_id = self._create_user("matches.malformed@example.com")
        profile_id = self._create_profile(user_id)
        response = self.client.get(
            f"/users/{user_id}/profiles/{profile_id}/matches",
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
        self.assertEqual(response.status_code, 401)

    def test_owner_own_profile_is_200(self):
        user_id = self._create_user("matches.owner@example.com")
        profile_id = self._create_profile(user_id)
        response = self.client.get(
            f"/users/{user_id}/profiles/{profile_id}/matches",
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 200)

    def test_owner_own_empty_profile_returns_valid_zero_result(self):
        user_id = self._create_user("matches.owner.empty@example.com")
        profile_id = self._create_profile(user_id)
        response = self.client.get(
            f"/users/{user_id}/profiles/{profile_id}/matches",
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["total_count"], 0)
        self.assertEqual(body["returned_count"], 0)
        self.assertEqual(body["results"], [])

    def test_user_a_requesting_user_b_and_b_profile_is_404(self):
        attacker_id = self._create_user("matches.attacker@example.com")
        victim_id = self._create_user("matches.victim@example.com")
        victim_profile_id = self._create_profile(victim_id)
        response = self.client.get(
            f"/users/{victim_id}/profiles/{victim_profile_id}/matches",
            headers=self._auth_headers(attacker_id),
        )
        self.assertEqual(response.status_code, 404)

    def test_user_a_requesting_user_a_and_b_profile_is_404(self):
        attacker_id = self._create_user("matches.attacker.2@example.com")
        victim_id = self._create_user("matches.victim.2@example.com")
        victim_profile_id = self._create_profile(victim_id)
        response = self.client.get(
            f"/users/{attacker_id}/profiles/{victim_profile_id}/matches",
            headers=self._auth_headers(attacker_id),
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_correct_foreign_pair_is_200(self):
        admin_id = self._create_user("matches.admin@example.com", is_admin=True)
        victim_id = self._create_user("matches.admin.target@example.com")
        victim_profile_id = self._create_profile(victim_id)
        response = self.client.get(
            f"/users/{victim_id}/profiles/{victim_profile_id}/matches",
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 200)

    def test_admin_mismatched_pair_is_404(self):
        admin_id = self._create_user("matches.admin.mismatch@example.com", is_admin=True)
        user_a = self._create_user("matches.pair.a@example.com")
        user_b = self._create_user("matches.pair.b@example.com")
        profile_a = self._create_profile(user_a)
        response = self.client.get(
            f"/users/{user_b}/profiles/{profile_a}/matches",
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 404)


class SingleMatchAuthorizationTests(_BaseMatchesAuthorizationTestCase):
    def test_owner_passes_authorization_and_reaches_business_layer(self):
        user_id = self._create_user("single.owner@example.com")
        profile_id = self._create_profile(user_id)
        job_id = self._create_job()

        # No ProfileAnalysis exists -> reaches the existing business 400,
        # which is only reachable if authorization already succeeded.
        response = self.client.post(
            f"/users/{user_id}/profiles/{profile_id}/jobs/{job_id}/match",
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error_code"],
            "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
        )

    def test_user_a_requesting_b_and_b_profile_is_404(self):
        attacker_id = self._create_user("single.attacker@example.com")
        victim_id = self._create_user("single.victim@example.com")
        victim_profile_id = self._create_profile(victim_id)
        job_id = self._create_job()

        with patch("backend.routers.matches.calculate_backend_match") as mock_calc:
            response = self.client.post(
                f"/users/{victim_id}/profiles/{victim_profile_id}/jobs/{job_id}/match",
                headers=self._auth_headers(attacker_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_calc.assert_not_called()

    def test_user_a_requesting_a_and_b_profile_is_404(self):
        attacker_id = self._create_user("single.attacker.2@example.com")
        victim_id = self._create_user("single.victim.2@example.com")
        victim_profile_id = self._create_profile(victim_id)
        job_id = self._create_job()

        with patch("backend.routers.matches.calculate_backend_match") as mock_calc:
            response = self.client.post(
                f"/users/{attacker_id}/profiles/{victim_profile_id}/jobs/{job_id}/match",
                headers=self._auth_headers(attacker_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_calc.assert_not_called()

    def test_admin_correct_foreign_pair_passes_authorization(self):
        admin_id = self._create_user("single.admin@example.com", is_admin=True)
        victim_id = self._create_user("single.admin.target@example.com")
        victim_profile_id = self._create_profile(victim_id)
        job_id = self._create_job()

        response = self.client.post(
            f"/users/{victim_id}/profiles/{victim_profile_id}/jobs/{job_id}/match",
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error_code"],
            "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
        )

    def test_admin_mismatched_pair_is_404(self):
        admin_id = self._create_user("single.admin.mismatch@example.com", is_admin=True)
        user_a = self._create_user("single.pair.a@example.com")
        user_b = self._create_user("single.pair.b@example.com")
        profile_a = self._create_profile(user_a)
        job_id = self._create_job()

        with patch("backend.routers.matches.calculate_backend_match") as mock_calc:
            response = self.client.post(
                f"/users/{user_b}/profiles/{profile_a}/jobs/{job_id}/match",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_calc.assert_not_called()


class BatchMatchAuthorizationTests(_BaseMatchesAuthorizationTestCase):
    def test_owner_passes_authorization(self):
        user_id = self._create_user("batch.owner@example.com")
        profile_id = self._create_profile(user_id)

        # No ProfileAnalysis exists -> reaches the existing business 400,
        # proving authorization already succeeded.
        response = self.client.post(
            f"/users/{user_id}/profiles/{profile_id}/jobs/match-analyzed",
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error_code"],
            "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
        )

    def test_cross_user_is_404(self):
        attacker_id = self._create_user("batch.attacker@example.com")
        victim_id = self._create_user("batch.victim@example.com")
        victim_profile_id = self._create_profile(victim_id)

        with patch(
            "backend.routers.matches._match_profile_with_job_impl"
        ) as mock_impl:
            response = self.client.post(
                f"/users/{victim_id}/profiles/{victim_profile_id}/jobs/match-analyzed",
                headers=self._auth_headers(attacker_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_impl.assert_not_called()

    def test_own_user_id_but_another_users_profile_id_is_404(self):
        attacker_id = self._create_user("batch.attacker.2@example.com")
        victim_id = self._create_user("batch.victim.2@example.com")
        victim_profile_id = self._create_profile(victim_id)

        with patch(
            "backend.routers.matches._match_profile_with_job_impl"
        ) as mock_impl:
            response = self.client.post(
                f"/users/{attacker_id}/profiles/{victim_profile_id}/jobs/match-analyzed",
                headers=self._auth_headers(attacker_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_impl.assert_not_called()

    def test_admin_correct_foreign_pair_passes_authorization(self):
        admin_id = self._create_user("batch.admin@example.com", is_admin=True)
        victim_id = self._create_user("batch.admin.target@example.com")
        victim_profile_id = self._create_profile(victim_id)

        response = self.client.post(
            f"/users/{victim_id}/profiles/{victim_profile_id}/jobs/match-analyzed",
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error_code"],
            "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
        )

    def test_admin_mismatched_pair_is_404(self):
        admin_id = self._create_user("batch.admin.mismatch@example.com", is_admin=True)
        user_a = self._create_user("batch.pair.a@example.com")
        user_b = self._create_user("batch.pair.b@example.com")
        profile_a = self._create_profile(user_a)

        with patch(
            "backend.routers.matches._match_profile_with_job_impl"
        ) as mock_impl:
            response = self.client.post(
                f"/users/{user_b}/profiles/{profile_a}/jobs/match-analyzed",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_impl.assert_not_called()


class InternalHelperDelegationTests(_BaseMatchesAuthorizationTestCase):
    """Proves the structural refactor is real: the single-match route
    delegates to _match_profile_with_job_impl (A), and the batch route calls
    that same internal helper rather than the public, Depends()-protected
    match_profile_with_job route function (B).
    """

    def test_single_match_route_delegates_to_internal_helper(self):
        user_id = self._create_user("delegate.single@example.com")
        profile_id = self._create_profile(user_id)
        job_id = self._create_job()

        sentinel_result = {"status": "created", "match_id": 999}
        with patch(
            "backend.routers.matches._match_profile_with_job_impl",
            return_value=sentinel_result,
        ) as mock_impl:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/jobs/{job_id}/match",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), sentinel_result)
        mock_impl.assert_called_once()

        call_kwargs = mock_impl.call_args.kwargs
        self.assertEqual(call_kwargs["user_id"], user_id)
        self.assertEqual(call_kwargs["profile_id"], profile_id)
        self.assertEqual(call_kwargs["job_id"], job_id)
        self.assertEqual(call_kwargs["profile"].profile_id, profile_id)

    def test_batch_route_calls_internal_helper_not_the_public_route(self):
        # Calling the public route function directly (bypassing Depends())
        # would silently skip authorization for every job in the batch.
        # This proves the batch route calls _match_profile_with_job_impl,
        # never match_profile_with_job, for each job.
        user_id = self._create_user("delegate.batch@example.com")
        profile_id = self._create_profile(user_id)
        job_id = self._create_job()

        session = self.session_factory()
        try:
            session.add(models.ProfileAnalysis(
                profile_id=profile_id,
                analysis_status="completed",
                analysis_model=matches.REQUIRED_PROFILE_ANALYSIS_MODEL,
                analysis_prompt_version=matches.REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION,
                is_current=True,
            ))
            session.add(models.JobAnalysis(
                job_id=job_id,
                analysis_status="completed",
                analysis_model=matches.REQUIRED_JOB_ANALYSIS_MODEL,
                analysis_prompt_version=matches.REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
                is_current=True,
                role_tags_json='["backend_development"]',
            ))
            session.commit()
        finally:
            session.close()

        with patch(
            "backend.routers.matches.match_profile_with_job"
        ) as mock_public_route, patch(
            "backend.routers.matches._match_profile_with_job_impl",
            return_value={
                "status": "created",
                "match_id": 1,
                "overall_score": 50,
                "recommendation": "maybe",
            },
        ) as mock_impl:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/jobs/match-analyzed",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200)
        mock_impl.assert_called_once()
        mock_public_route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
