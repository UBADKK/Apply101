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

        with patch("backend.routers.profiles.client.responses.create") as mock_openai:
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

        with patch("backend.routers.profiles.client.responses.create") as mock_openai:
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

        with patch("backend.routers.profiles.client.responses.create") as mock_openai:
            response = self.client.post(
                f"/users/{victim_id}/profiles/{victim_profile_id}/analyze",
                headers=self._auth_headers(admin_id),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_PROFILE_DATA_MISSING")
        mock_openai.assert_not_called()


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
