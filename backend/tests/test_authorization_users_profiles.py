import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password
from backend.app.analysis_guard import (
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
    AcquireOutcome,
    AcquireResult,
    load_config as load_analysis_guard_config,
    try_acquire_profile_analysis_guard,
)

# profiles.py constructs an OpenAI client at import time (`client = OpenAI()`),
# which raises immediately if no API key is configured. OPENAI_API_KEY is
# synthetic only for the duration of this import -- patch.dict restores the
# real environment (including a real OPENAI_API_KEY, if any) immediately
# afterward. The already-constructed profiles.client keeps this synthetic
# key, which is fine: every OpenAI-reaching call in this file is mocked.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import profiles, users


SYNTHETIC_SECRET = "synthetic-test-secret-for-authz-users-profiles-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"

# Minimal synthetic payload satisfying schemas.ProfileAnalysisStructured
# (extra="forbid", so this must be exactly the schema's fields).
VALID_ANALYSIS_PAYLOAD = {
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


def _mock_openai_response(payload=None):
    response = MagicMock()
    response.output_text = json.dumps(payload or VALID_ANALYSIS_PAYLOAD)
    return response


def build_test_app(engine):
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(users.router)
    app.include_router(profiles.router)
    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


class _BaseAuthorizationTestCase(unittest.TestCase):
    """Shared setup: a throwaway synthetic SQLite database (never
    apply101.db), only users.router + profiles.router mounted, a synthetic
    JWT secret, and helpers for creating synthetic users/profiles and
    minting real access tokens without going through /auth/login.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_authz_users_profiles_test_")
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

    def _create_profile_bare(self, user_id):
        # No self_description/target_role/cv_text/preferred_technologies --
        # deliberately fails the existing has_profile_text business check,
        # so the route reaches ERR_PROFILE_DATA_MISSING without OpenAI.
        return self._create_profile(user_id, self_description=None)

    def _auth_headers(self, user_id):
        token = create_access_token(user_id)
        return {"Authorization": f"Bearer {token}"}

    def _seed_completed_analysis(self, profile_id):
        from backend.app.analysis_contract import (
            PROFILE_ANALYSIS_MODEL,
            PROFILE_ANALYSIS_PROMPT_VERSION,
        )

        session = self.session_factory()
        try:
            analysis = models.ProfileAnalysis(
                profile_id=profile_id,
                analysis_status="completed",
                analysis_json=json.dumps(VALID_ANALYSIS_PAYLOAD),
                analysis_model=PROFILE_ANALYSIS_MODEL,
                analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
                is_current=True,
            )
            session.add(analysis)
            session.commit()
            session.refresh(analysis)
            return analysis.analysis_id
        finally:
            session.close()

    def _guard_row(self, operation_type, resource_id):
        # Reads (owner_token, lock_expires_at, cooldown_until) for one
        # dimension directly, independent of the route/guard module, so
        # tests can verify BOTH dimensions' persisted state precisely.
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT owner_token, lock_expires_at, cooldown_until FROM analysis_guards "
                    "WHERE operation_type = :op AND resource_id = :rid"
                ),
                {"op": operation_type, "rid": resource_id},
            ).fetchone()

    def _acquire_guard_directly(self, profile_id, owner_user_id):
        # Seeds an active guard lease exactly as the route would, but
        # bypassing the route/HTTP layer entirely -- used to deterministically
        # put a guard "already in progress" before the request under test.
        session = self.session_factory()
        try:
            result = try_acquire_profile_analysis_guard(
                session,
                profile_id=profile_id,
                owner_user_id=owner_user_id,
                config=load_analysis_guard_config(),
            )
            return result
        finally:
            session.close()


class UsersAuthorizationTests(_BaseAuthorizationTestCase):
    def test_list_users_without_token_is_401(self):
        response = self.client.get("/users/")
        self.assertEqual(response.status_code, 401)

    def test_list_users_normal_user_is_403(self):
        user_id = self._create_user("normal.list@example.com")
        response = self.client.get("/users/", headers=self._auth_headers(user_id))
        self.assertEqual(response.status_code, 403)

    def test_list_users_admin_is_200(self):
        admin_id = self._create_user("admin.list@example.com", is_admin=True)
        response = self.client.get("/users/", headers=self._auth_headers(admin_id))
        self.assertEqual(response.status_code, 200)

    def test_create_user_normal_user_is_403(self):
        user_id = self._create_user("normal.create@example.com")
        response = self.client.post(
            "/users/",
            json={"name": "New User", "mail": "new.create@example.com"},
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 403)

    def test_create_user_admin_succeeds(self):
        admin_id = self._create_user("admin.create@example.com", is_admin=True)
        response = self.client.post(
            "/users/",
            json={"name": "New User", "mail": "new.byadmin@example.com"},
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mail"], "new.byadmin@example.com")

    def test_delete_own_user_succeeds(self):
        user_id = self._create_user("delete.self@example.com")
        response = self.client.delete(
            f"/users/{user_id}", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 200)

    def test_delete_another_user_as_normal_user_is_404(self):
        attacker_id = self._create_user("deleter@example.com")
        victim_id = self._create_user("delete.victim@example.com")
        response = self.client.delete(
            f"/users/{victim_id}", headers=self._auth_headers(attacker_id)
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_another_user_as_admin_succeeds(self):
        admin_id = self._create_user("admin.deleter@example.com", is_admin=True)
        victim_id = self._create_user("admin.delete.target@example.com")
        response = self.client.delete(
            f"/users/{victim_id}", headers=self._auth_headers(admin_id)
        )
        self.assertEqual(response.status_code, 200)

    def test_delete_user_removes_only_that_users_analysis_guard_rows(self):
        victim_id = self._create_user("guard.delete.victim@example.com")
        victim_profile_id = self._create_profile(victim_id, self_description="v")
        other_id = self._create_user("guard.delete.other@example.com")
        other_profile_id = self._create_profile(other_id, self_description="o")

        victim_result = self._acquire_guard_directly(victim_profile_id, victim_id)
        self.assertEqual(victim_result.outcome, AcquireOutcome.GRANTED)
        other_result = self._acquire_guard_directly(other_profile_id, other_id)
        self.assertEqual(other_result.outcome, AcquireOutcome.GRANTED)

        response = self.client.delete(
            f"/users/{victim_id}", headers=self._auth_headers(victim_id)
        )
        self.assertEqual(response.status_code, 200)

        session = self.session_factory()
        try:
            remaining_keys = {
                (row.operation_type, row.resource_id)
                for row in session.query(models.AnalysisGuard).all()
            }
        finally:
            session.close()

        self.assertNotIn((PROFILE_OPERATION_TYPE, victim_profile_id), remaining_keys)
        self.assertNotIn((USER_OPERATION_TYPE, victim_id), remaining_keys)
        self.assertIn((PROFILE_OPERATION_TYPE, other_profile_id), remaining_keys)
        self.assertIn((USER_OPERATION_TYPE, other_id), remaining_keys)


class ProfilesUserLevelAuthorizationTests(_BaseAuthorizationTestCase):
    def test_get_own_profiles_succeeds(self):
        user_id = self._create_user("own.profiles@example.com")
        self._create_profile(user_id)
        response = self.client.get(
            f"/users/{user_id}/profiles", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_get_another_users_profiles_is_404(self):
        attacker_id = self._create_user("profiles.requester@example.com")
        victim_id = self._create_user("profiles.victim@example.com")
        self._create_profile(victim_id)
        response = self.client.get(
            f"/users/{victim_id}/profiles", headers=self._auth_headers(attacker_id)
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_get_another_users_profiles_succeeds(self):
        admin_id = self._create_user("admin.viewer@example.com", is_admin=True)
        victim_id = self._create_user("profiles.admin.target@example.com")
        self._create_profile(victim_id)
        response = self.client.get(
            f"/users/{victim_id}/profiles", headers=self._auth_headers(admin_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_post_own_profile_succeeds(self):
        user_id = self._create_user("post.own@example.com")
        response = self.client.post(
            f"/users/{user_id}/profiles",
            json={"self_description": "hi"},
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 200)

    def test_post_profile_for_another_user_as_normal_user_is_404(self):
        attacker_id = self._create_user("create.attacker@example.com")
        victim_id = self._create_user("create.victim@example.com")
        response = self.client.post(
            f"/users/{victim_id}/profiles",
            json={"self_description": "hi"},
            headers=self._auth_headers(attacker_id),
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_may_create_profile_for_another_existing_user(self):
        admin_id = self._create_user("admin.creator@example.com", is_admin=True)
        victim_id = self._create_user("create.admin.target@example.com")
        response = self.client.post(
            f"/users/{victim_id}/profiles",
            json={"self_description": "hi"},
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_id"], victim_id)

    def test_get_own_analyzed_profiles_succeeds(self):
        user_id = self._create_user("analyzed.own@example.com")
        response = self.client.get(
            f"/users/{user_id}/profiles/analyzed", headers=self._auth_headers(user_id)
        )
        self.assertEqual(response.status_code, 200)

    def test_get_another_users_analyzed_profiles_is_404(self):
        attacker_id = self._create_user("analyzed.requester@example.com")
        victim_id = self._create_user("analyzed.victim@example.com")
        response = self.client.get(
            f"/users/{victim_id}/profiles/analyzed",
            headers=self._auth_headers(attacker_id),
        )
        self.assertEqual(response.status_code, 404)


class ProfilePatchIdorTests(_BaseAuthorizationTestCase):
    def test_patch_own_profile_succeeds(self):
        user_id = self._create_user("patch.own@example.com")
        profile_id = self._create_profile(user_id)
        response = self.client.patch(
            f"/users/{user_id}/profiles/{profile_id}",
            json={"self_description": "updated"},
            headers=self._auth_headers(user_id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["self_description"], "updated")

    def test_patch_another_users_profile_via_their_real_user_id_is_404(self):
        # /users/B/profiles/B_PROFILE requested by A
        attacker_id = self._create_user("patch.attacker@example.com")
        victim_id = self._create_user("patch.victim@example.com")
        victim_profile_id = self._create_profile(victim_id)
        response = self.client.patch(
            f"/users/{victim_id}/profiles/{victim_profile_id}",
            json={"self_description": "hacked"},
            headers=self._auth_headers(attacker_id),
        )
        self.assertEqual(response.status_code, 404)

    def test_patch_another_users_profile_while_supplying_own_user_id_is_404(self):
        # /users/A/profiles/B_PROFILE requested by A
        attacker_id = self._create_user("patch.attacker.2@example.com")
        victim_id = self._create_user("patch.victim.2@example.com")
        victim_profile_id = self._create_profile(victim_id)
        response = self.client.patch(
            f"/users/{attacker_id}/profiles/{victim_profile_id}",
            json={"self_description": "hacked"},
            headers=self._auth_headers(attacker_id),
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_with_correct_foreign_pair_succeeds(self):
        admin_id = self._create_user("patch.admin@example.com", is_admin=True)
        victim_id = self._create_user("patch.admin.target@example.com")
        victim_profile_id = self._create_profile(victim_id)
        response = self.client.patch(
            f"/users/{victim_id}/profiles/{victim_profile_id}",
            json={"self_description": "admin edit"},
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["self_description"], "admin edit")

    def test_admin_with_mismatched_user_and_profile_pair_is_404(self):
        admin_id = self._create_user("patch.admin.mismatch@example.com", is_admin=True)
        user_a = self._create_user("patch.pair.a@example.com")
        user_b = self._create_user("patch.pair.b@example.com")
        profile_a = self._create_profile(user_a)
        # Admin requests /users/B/profiles/A_PROFILE -- profile_a belongs to A.
        response = self.client.patch(
            f"/users/{user_b}/profiles/{profile_a}",
            json={"self_description": "should fail"},
            headers=self._auth_headers(admin_id),
        )
        self.assertEqual(response.status_code, 404)


class UploadCvAuthorizationTests(_BaseAuthorizationTestCase):
    def test_cross_user_upload_is_denied_before_any_filesystem_work(self):
        attacker_id = self._create_user("upload.attacker@example.com")
        victim_id = self._create_user("upload.victim@example.com")
        victim_profile_id = self._create_profile(victim_id)

        with patch("backend.routers.profiles.os.makedirs") as mock_makedirs, \
             patch("backend.routers.profiles.open") as mock_open, \
             patch("backend.routers.profiles.extract_text_from_pdf") as mock_extract:

            response = self.client.post(
                f"/users/{victim_id}/profiles/{victim_profile_id}/upload-cv",
                files={
                    "file": (
                        "resume.pdf",
                        b"%PDF-1.4 synthetic non-CV placeholder bytes",
                        "application/pdf",
                    )
                },
                headers=self._auth_headers(attacker_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_makedirs.assert_not_called()
        mock_open.assert_not_called()
        mock_extract.assert_not_called()


class ProfileAnalysisAuthorizationTests(_BaseAuthorizationTestCase):
    def test_cross_user_analyze_is_denied_before_openai_call(self):
        attacker_id = self._create_user("analyze.attacker@example.com")
        victim_id = self._create_user("analyze.victim@example.com")
        victim_profile_id = self._create_profile(victim_id, self_description="victim data")

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_openai:
            response = self.client.post(
                f"/users/{victim_id}/profiles/{victim_profile_id}/analyze",
                headers=self._auth_headers(attacker_id),
            )

        self.assertEqual(response.status_code, 404)
        mock_openai.assert_not_called()

    def test_owner_with_insufficient_profile_data_reaches_existing_400(self):
        # Proves authorization succeeded (past get_owned_profile) while
        # still never invoking OpenAI, by relying on the existing
        # has_profile_text business check to reject first.
        user_id = self._create_user("analyze.owner.insufficient@example.com")
        profile_id = self._create_profile_bare(user_id)

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_openai:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_PROFILE_DATA_MISSING")
        mock_openai.assert_not_called()

    def test_admin_with_correct_foreign_profile_passes_authorization(self):
        admin_id = self._create_user("analyze.admin@example.com", is_admin=True)
        victim_id = self._create_user("analyze.admin.target@example.com")
        victim_profile_id = self._create_profile_bare(victim_id)

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_openai:
            response = self.client.post(
                f"/users/{victim_id}/profiles/{victim_profile_id}/analyze",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_PROFILE_DATA_MISSING")
        mock_openai.assert_not_called()


class OpenAIWrapperTests(unittest.TestCase):
    """_create_profile_analysis_response is the single seam E3.2 relies on
    for applying configured timeout/max_retries -- patching
    client.responses.create directly does NOT work, since
    client.with_options(...) returns a distinct client/resource instance.
    """

    def test_wrapper_applies_configured_timeout_and_max_retries_without_network(self):
        fake_scoped_client = MagicMock()
        fake_scoped_client.responses.create.return_value = _mock_openai_response()

        with patch("backend.routers.profiles.client") as fake_client:
            fake_client.with_options.return_value = fake_scoped_client
            result = profiles._create_profile_analysis_response(
                "a synthetic prompt", timeout_seconds=42, max_retries=3,
            )

        fake_client.with_options.assert_called_once_with(max_retries=3)
        _, kwargs = fake_scoped_client.responses.create.call_args
        self.assertEqual(kwargs["timeout"], 42)
        self.assertEqual(result, fake_scoped_client.responses.create.return_value)


class ProfileAnalysisGuardTests(_BaseAuthorizationTestCase):
    """E3.2 route-level guard/cooldown/concurrency behavior for
    POST /users/{user_id}/profiles/{profile_id}/analyze.
    """

    def test_cache_hit_bypasses_config_guard_and_openai_entirely(self):
        user_id = self._create_user("guard.cache.hit@example.com")
        profile_id = self._create_profile(user_id, self_description="cached data")
        self._seed_completed_analysis(profile_id)

        with patch("backend.routers.profiles.load_analysis_guard_config") as mock_config, \
             patch("backend.routers.profiles.try_acquire_profile_analysis_guard") as mock_acquire, \
             patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cached")
        mock_config.assert_not_called()
        mock_acquire.assert_not_called()
        mock_call.assert_not_called()

    def test_active_profile_guard_returns_409_with_no_retry_after(self):
        user_id = self._create_user("guard.active.profile@example.com")
        profile_id = self._create_profile(user_id, self_description="data")
        result = self._acquire_guard_directly(profile_id, user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("Retry-After", response.headers)
        mock_call.assert_not_called()

    def test_active_user_guard_blocks_a_different_profile_of_the_same_owner(self):
        user_id = self._create_user("guard.active.user@example.com")
        held_profile_id = self._create_profile(user_id, self_description="held")
        other_profile_id = self._create_profile(user_id, self_description="other")
        result = self._acquire_guard_directly(held_profile_id, user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self.client.post(
                f"/users/{user_id}/profiles/{other_profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 409)
        mock_call.assert_not_called()

    def test_admin_cannot_bypass_same_profile_guard(self):
        admin_id = self._create_user("guard.admin.bypass@example.com", is_admin=True)
        victim_id = self._create_user("guard.admin.bypass.victim@example.com")
        profile_id = self._create_profile(victim_id, self_description="data")
        result = self._acquire_guard_directly(profile_id, victim_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self.client.post(
                f"/users/{victim_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 409)
        mock_call.assert_not_called()

    def test_backend_unavailable_returns_503_before_openai(self):
        user_id = self._create_user("guard.backend.unavailable@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles.try_acquire_profile_analysis_guard",
            return_value=AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE),
        ), patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 503)
        mock_call.assert_not_called()

    def test_invalid_config_returns_500_before_guard_or_openai_with_no_failed_row(self):
        user_id = self._create_user("guard.invalid.config@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch.dict(
            os.environ, {"PROFILE_ANALYSIS_OPENAI_TIMEOUT_SECONDS": "0"}, clear=False
        ), patch(
            "backend.routers.profiles.try_acquire_profile_analysis_guard"
        ) as mock_acquire, patch(
            "backend.routers.profiles._create_profile_analysis_response"
        ) as mock_call:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 500)
        mock_acquire.assert_not_called()
        mock_call.assert_not_called()

        session = self.session_factory()
        try:
            rows = session.query(models.ProfileAnalysis).filter(
                models.ProfileAnalysis.profile_id == profile_id
            ).all()
        finally:
            session.close()
        self.assertEqual(rows, [])

    def test_successful_analysis_releases_guard_into_success_cooldown(self):
        user_id = self._create_user("guard.success.release@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")

        # The just-released guard should now be in its success cooldown
        # (default 300s), not simply free.
        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            second_response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
                params={"force_reanalyze": "true"},
            )
        self.assertEqual(second_response.status_code, 429)
        self.assertEqual(second_response.headers.get("Retry-After"), "300")
        mock_call.assert_not_called()

    def test_failed_analysis_releases_guard_into_failure_cooldown(self):
        user_id = self._create_user("guard.failure.release@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            side_effect=RuntimeError("simulated OpenAI failure"),
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_FAILED"
        )

        session = self.session_factory()
        try:
            failed_rows = session.query(models.ProfileAnalysis).filter(
                models.ProfileAnalysis.profile_id == profile_id,
                models.ProfileAnalysis.analysis_status == "failed",
            ).all()
        finally:
            session.close()
        self.assertEqual(len(failed_rows), 1)

        # Both dimensions release into their failure cooldown (default 30s
        # each here), not just the profile dimension.
        profile_row = self._guard_row(PROFILE_OPERATION_TYPE, profile_id)
        user_row = self._guard_row(USER_OPERATION_TYPE, user_id)
        self.assertIsNone(profile_row[0])
        self.assertIsNotNone(profile_row[2])
        self.assertIsNone(user_row[0])
        self.assertIsNotNone(user_row[2])

        # Failure cooldown (default 30s), distinct from the success cooldown.
        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            second_response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )
        self.assertEqual(second_response.status_code, 429)
        self.assertEqual(second_response.headers.get("Retry-After"), "30")
        mock_call.assert_not_called()

    def test_guard_is_acquired_before_the_openai_call_begins(self):
        user_id = self._create_user("guard.timing@example.com")
        profile_id = self._create_profile(user_id, self_description="timing data")

        guard_state_at_call_time = {}

        def check_guard_and_respond(prompt, *, timeout_seconds, max_retries):
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT owner_token, lock_expires_at FROM analysis_guards "
                        "WHERE operation_type = :op AND resource_id = :rid"
                    ),
                    {"op": PROFILE_OPERATION_TYPE, "rid": profile_id},
                ).fetchone()
            guard_state_at_call_time["row"] = row
            return _mock_openai_response()

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            side_effect=check_guard_and_respond,
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200)
        row = guard_state_at_call_time.get("row")
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])  # owner_token was already committed

    def test_guard_acquisition_does_not_commit_the_routes_own_db_session(self):
        user_id = self._create_user("guard.session.isolation@example.com")
        profile_id = self._create_profile(user_id, self_description="data")
        result = self._acquire_guard_directly(profile_id, user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

        captured = {}

        def override_get_db_capture():
            db = self.session_factory()
            original_commit = db.commit
            commit_calls = []

            def tracking_commit(*args, **kwargs):
                commit_calls.append(True)
                return original_commit(*args, **kwargs)

            db.commit = tracking_commit
            captured["commit_calls"] = commit_calls
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db_capture
        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 409)
        mock_call.assert_not_called()
        self.assertEqual(captured["commit_calls"], [])

    def test_different_users_can_analyze_independently(self):
        user_a = self._create_user("guard.independent.a@example.com")
        user_b = self._create_user("guard.independent.b@example.com")
        profile_a = self._create_profile(user_a, self_description="a data")
        profile_b = self._create_profile(user_b, self_description="b data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ):
            response_a = self.client.post(
                f"/users/{user_a}/profiles/{profile_a}/analyze",
                headers=self._auth_headers(user_a),
            )
            response_b = self.client.post(
                f"/users/{user_b}/profiles/{profile_b}/analyze",
                headers=self._auth_headers(user_b),
            )

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)

    def test_two_concurrent_requests_same_profile_result_in_exactly_one_success(self):
        # Real threads hitting the real ASGI app/DB, coordinated with
        # threading.Event (never sleeps): the first request is held
        # in-flight inside its (mocked) OpenAI call, guaranteeing the
        # second request's guard-acquire attempt genuinely races against
        # an active lease rather than a completed-and-released one.
        user_id = self._create_user("guard.concurrency@example.com")
        profile_id = self._create_profile(user_id, self_description="concurrent data")

        openai_call_entered = threading.Event()
        release_openai_call = threading.Event()
        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_openai_call(prompt, *, timeout_seconds, max_retries):
            with call_lock:
                call_count["n"] += 1
            openai_call_entered.set()
            release_openai_call.wait(timeout=10)
            return _mock_openai_response()

        results = {}

        def first_request():
            with patch(
                "backend.routers.profiles._create_profile_analysis_response",
                side_effect=slow_openai_call,
            ):
                results["first"] = self.client.post(
                    f"/users/{user_id}/profiles/{profile_id}/analyze",
                    headers=self._auth_headers(user_id),
                )

        first_thread = threading.Thread(target=first_request)
        first_thread.start()
        self.assertTrue(openai_call_entered.wait(timeout=10))

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_second:
            second_response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(second_response.status_code, 409)
        mock_second.assert_not_called()

        release_openai_call.set()
        first_thread.join(timeout=10)

        self.assertEqual(results["first"].status_code, 200)
        self.assertEqual(call_count["n"], 1)

    def test_language_query_failure_occurs_before_guard_acquisition(self):
        user_id = self._create_user("guard.lang.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles.build_languages_text",
            side_effect=RuntimeError("simulated language lookup failure"),
        ), patch(
            "backend.routers.profiles.try_acquire_profile_analysis_guard"
        ) as mock_acquire, patch(
            "backend.routers.profiles._create_profile_analysis_response"
        ) as mock_call:
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/users/{user_id}/profiles/{profile_id}/analyze",
                    headers=self._auth_headers(user_id),
                )

        mock_acquire.assert_not_called()
        mock_call.assert_not_called()

        # Nothing was ever held -- a normal acquisition attempt for the
        # exact same profile/user succeeds as if this request never happened.
        result = self._acquire_guard_directly(profile_id, user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

    def test_prompt_construction_failure_occurs_before_guard_acquisition(self):
        user_id = self._create_user("guard.prompt.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles.json.dumps",
            side_effect=RuntimeError("simulated prompt construction failure"),
        ), patch(
            "backend.routers.profiles.try_acquire_profile_analysis_guard"
        ) as mock_acquire, patch(
            "backend.routers.profiles._create_profile_analysis_response"
        ) as mock_call:
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/users/{user_id}/profiles/{profile_id}/analyze",
                    headers=self._auth_headers(user_id),
                )

        mock_acquire.assert_not_called()
        mock_call.assert_not_called()

        result = self._acquire_guard_directly(profile_id, user_id)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)

    def test_parsing_validation_failure_releases_both_dimensions_with_failure_cooldown(self):
        user_id = self._create_user("guard.parse.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        bad_response = _mock_openai_response()
        bad_response.output_text = "not valid json{{{"

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=bad_response,
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_FAILED"
        )

        now = time.time()
        profile_row = self._guard_row(PROFILE_OPERATION_TYPE, profile_id)
        user_row = self._guard_row(USER_OPERATION_TYPE, user_id)
        self.assertIsNone(profile_row[0])
        self.assertLess(profile_row[2] - now, 60)  # failure cooldown (30s), not success (300s)
        self.assertIsNone(user_row[0])
        self.assertLess(user_row[2] - now, 60)

    def test_main_analysis_commit_failure_releases_both_dimensions_with_failure_cooldown(self):
        user_id = self._create_user("guard.maincommit.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        commit_call_count = {"n": 0}

        def override_get_db_first_commit_fails():
            db = self.session_factory()
            original_commit = db.commit

            def counting_commit(*args, **kwargs):
                commit_call_count["n"] += 1
                if commit_call_count["n"] == 1:
                    raise RuntimeError("simulated main analysis commit failure")
                return original_commit(*args, **kwargs)

            db.commit = counting_commit
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db_first_commit_fails
        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_FAILED"
        )

        now = time.time()
        profile_row = self._guard_row(PROFILE_OPERATION_TYPE, profile_id)
        user_row = self._guard_row(USER_OPERATION_TYPE, user_id)
        self.assertIsNone(profile_row[0])
        self.assertLess(profile_row[2] - now, 60)
        self.assertIsNone(user_row[0])
        self.assertLess(user_row[2] - now, 60)

    def test_main_commit_failure_followed_by_failed_analysis_commit_failure_still_releases_both(self):
        user_id = self._create_user("guard.doublefault@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        def override_get_db_all_commits_fail():
            db = self.session_factory()

            def failing_commit(*args, **kwargs):
                raise RuntimeError("simulated commit failure")

            db.commit = failing_commit
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db_all_commits_fail
        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/users/{user_id}/profiles/{profile_id}/analyze",
                    headers=self._auth_headers(user_id),
                )

        # Even though BOTH the main analysis commit and the failed-analysis
        # record's own commit failed (the route's db session never
        # commits successfully at all), the guard -- which lives on its
        # own independent session -- must still have released with a
        # failure cooldown via the outer `finally`.
        now = time.time()
        profile_row = self._guard_row(PROFILE_OPERATION_TYPE, profile_id)
        user_row = self._guard_row(USER_OPERATION_TYPE, user_id)
        self.assertIsNone(profile_row[0])
        self.assertLess(profile_row[2] - now, 60)
        self.assertIsNone(user_row[0])
        self.assertLess(user_row[2] - now, 60)

    def test_successful_commit_followed_by_refresh_failure_uses_success_cooldown(self):
        user_id = self._create_user("guard.refresh.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        def override_get_db_refresh_fails():
            db = self.session_factory()

            def failing_refresh(*args, **kwargs):
                raise RuntimeError("simulated refresh failure")

            db.refresh = failing_refresh
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db_refresh_fails
        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        # The paid analysis really did commit successfully before refresh
        # failed, so the route still falls into the pre-existing (unchanged
        # by this correction) failure-handling response...
        self.assertEqual(response.status_code, 500)

        # ...but the guard release must reflect the truth: the analysis was
        # already committed, so this is a SUCCESS cooldown (~300s), not a
        # failure cooldown (~30s), regardless of the HTTP response.
        now = time.time()
        profile_row = self._guard_row(PROFILE_OPERATION_TYPE, profile_id)
        self.assertIsNone(profile_row[0])
        self.assertGreater(profile_row[2] - now, 100)

    def test_exactly_one_release_call_on_success(self):
        user_id = self._create_user("guard.release.count.success@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ), patch(
            "backend.routers.profiles.release_profile_analysis_guard",
            wraps=profiles.release_profile_analysis_guard,
        ) as mock_release:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_release.call_count, 1)

    def test_exactly_one_release_call_on_handled_failure(self):
        user_id = self._create_user("guard.release.count.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            side_effect=RuntimeError("simulated failure"),
        ), patch(
            "backend.routers.profiles.release_profile_analysis_guard",
            wraps=profiles.release_profile_analysis_guard,
        ) as mock_release:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(mock_release.call_count, 1)

    def test_release_helper_failure_does_not_replace_successful_route_response(self):
        user_id = self._create_user("guard.release.raises.success@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_mock_openai_response(),
        ), patch(
            "backend.routers.profiles.release_profile_analysis_guard",
            side_effect=RuntimeError("simulated unexpected release failure"),
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")

    def test_release_helper_failure_does_not_replace_original_route_error(self):
        user_id = self._create_user("guard.release.raises.failure@example.com")
        profile_id = self._create_profile(user_id, self_description="data")

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            side_effect=RuntimeError("simulated OpenAI failure"),
        ), patch(
            "backend.routers.profiles.release_profile_analysis_guard",
            side_effect=RuntimeError("simulated unexpected release failure"),
        ):
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/analyze",
                headers=self._auth_headers(user_id),
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_FAILED"
        )


class AuthenticationBasicsTests(_BaseAuthorizationTestCase):
    def test_missing_token_on_protected_route_is_401(self):
        user_id = self._create_user("auth.missing@example.com")
        response = self.client.get(f"/users/{user_id}/profiles")
        self.assertEqual(response.status_code, 401)

    def test_malformed_token_on_protected_route_is_401(self):
        user_id = self._create_user("auth.malformed@example.com")
        response = self.client.get(
            f"/users/{user_id}/profiles",
            headers={"Authorization": "Bearer this-is-not-a-jwt"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
