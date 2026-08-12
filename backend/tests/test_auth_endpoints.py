import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app import models, schemas
from backend.app.database import Base, get_db
from backend.app.security import hash_password
from backend.routers import auth


# Synthetic, test-only secret -- never read from or written to real .env.
SYNTHETIC_SECRET = "synthetic-test-secret-for-auth-endpoints-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"  # >= MIN_PASSWORD_LENGTH


def build_test_app(engine):
    """Isolated FastAPI app: only the auth router, get_db overridden to a
    throwaway synthetic SQLite database. Never touches the real app/db.
    """
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


class AuthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_auth_endpoint_test_")
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

    # -- helpers -------------------------------------------------------

    def _register(self, mail, password=VALID_PASSWORD, name="Synthetic User"):
        return self.client.post(
            "/auth/register",
            json={"name": name, "mail": mail, "password": password},
        )

    def _login(self, mail, password):
        return self.client.post(
            "/auth/login",
            data={"username": mail, "password": password},
        )

    def _create_legacy_user_directly(self, mail):
        """Insert a passwordless (legacy) user straight through the ORM,
        bypassing /auth/register entirely -- simulates a pre-Phase-A row.
        """
        session = self.session_factory()
        try:
            user = models.User(name="Legacy User", mail=mail, password_hash=None, is_admin=False)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _get_password_hash(self, user_id):
        session = self.session_factory()
        try:
            user = session.query(models.User).filter(models.User.user_id == user_id).first()
            return user.password_hash
        finally:
            session.close()

    # -- OPENAPI ---------------------------------------------------------

    def test_openapi_advertises_relative_token_url(self):
        # A relative tokenUrl (no leading slash) keeps the docs' OAuth2
        # "Authorize" flow correct if the app is later served under a proxy
        # path prefix, rather than resolving against the proxy's root.
        schema = self.app.openapi()
        security_schemes = schema["components"]["securitySchemes"]
        token_url = security_schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"]
        self.assertEqual(token_url, "auth/login")

    # -- REGISTER --------------------------------------------------------

    def test_valid_registration_returns_201(self):
        response = self._register("valid.register@example.com")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["mail"], "valid.register@example.com")
        self.assertIn("user_id", body)

    def test_password_hash_stored_not_plaintext(self):
        response = self._register("hash.stored@example.com")
        user_id = response.json()["user_id"]
        stored_hash = self._get_password_hash(user_id)
        self.assertIsNotNone(stored_hash)
        self.assertNotEqual(stored_hash, VALID_PASSWORD)
        self.assertTrue(stored_hash.startswith("$argon2"))

    def test_registered_account_has_is_admin_false(self):
        response = self._register("admin.default@example.com")
        self.assertFalse(response.json()["is_admin"])

    def test_register_response_does_not_contain_password_fields(self):
        response = self._register("no.leak.register@example.com")
        body = response.json()
        self.assertNotIn("password", body)
        self.assertNotIn("password_hash", body)

    def test_short_password_is_rejected(self):
        response = self._register("short.pw@example.com", password="x" * 14)
        self.assertEqual(response.status_code, 422)

    def test_64_character_password_accepted(self):
        response = self._register("sixty.four@example.com", password="p" * 64)
        self.assertEqual(response.status_code, 201)

    def test_password_longer_than_64_accepted_without_truncation(self):
        password_a = ("a" * 64) + "SUFFIX-ONE"
        password_b = ("a" * 64) + "SUFFIX-TWO"

        register_response = self._register("long.pw@example.com", password=password_a)
        self.assertEqual(register_response.status_code, 201)

        # If the first 64 characters were silently truncated, password_b
        # (identical for its first 64 characters) would also authenticate.
        wrong_login = self._login("long.pw@example.com", password_b)
        self.assertEqual(wrong_login.status_code, 401)

        correct_login = self._login("long.pw@example.com", password_a)
        self.assertEqual(correct_login.status_code, 200)

    def test_duplicate_email_is_rejected(self):
        first = self._register("duplicate@example.com")
        self.assertEqual(first.status_code, 201)

        second = self._register("duplicate@example.com")
        self.assertEqual(second.status_code, 409)
        self.assertNotIn(str(first.json()["user_id"]), second.text)

    def test_legacy_passwordless_user_cannot_be_claimed(self):
        user_id = self._create_legacy_user_directly("legacy.claim@example.com")

        response = self._register("legacy.claim@example.com")
        self.assertEqual(response.status_code, 409)

        # The failed claim must not have touched the existing row at all.
        self.assertIsNone(self._get_password_hash(user_id))

    def test_registration_cannot_set_is_admin(self):
        response = self.client.post(
            "/auth/register",
            json={
                "name": "Attempted Admin",
                "mail": "attempted.admin@example.com",
                "password": VALID_PASSWORD,
                "is_admin": True,
            },
        )
        # UserRegister forbids unknown fields entirely, so this must be
        # rejected outright rather than silently accepted with is_admin=True.
        self.assertEqual(response.status_code, 422)

    # -- LOGIN -------------------------------------------------------------

    def test_correct_credentials_return_bearer_token(self):
        self._register("login.correct@example.com")
        response = self._login("login.correct@example.com", VALID_PASSWORD)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access_token", body)
        self.assertEqual(body["token_type"], "bearer")

    def test_returned_token_decodes_to_correct_user_id(self):
        register_response = self._register("login.decode@example.com")
        expected_user_id = register_response.json()["user_id"]

        login_response = self._login("login.decode@example.com", VALID_PASSWORD)
        token = login_response.json()["access_token"]

        payload = jwt.decode(token, SYNTHETIC_SECRET, algorithms=["HS256"])
        self.assertEqual(int(payload["sub"]), expected_user_id)

    def test_wrong_password_returns_401(self):
        self._register("login.wrongpw@example.com")
        response = self._login("login.wrongpw@example.com", "a-totally-different-password")
        self.assertEqual(response.status_code, 401)

    def test_nonexistent_user_returns_401(self):
        response = self._login("login.nonexistent@example.com", "irrelevant-password-value")
        self.assertEqual(response.status_code, 401)

    def test_passwordless_legacy_user_returns_401(self):
        self._create_legacy_user_directly("login.legacy@example.com")
        response = self._login("login.legacy@example.com", "any-password-value-here")
        self.assertEqual(response.status_code, 401)

    def test_login_failure_paths_share_identical_public_detail(self):
        self._register("login.shared.detail@example.com")
        self._create_legacy_user_directly("login.shared.legacy@example.com")

        wrong_password = self._login(
            "login.shared.detail@example.com", "a-totally-different-password"
        )
        nonexistent = self._login(
            "login.shared.nonexistent@example.com", "irrelevant-password-value"
        )
        legacy = self._login("login.shared.legacy@example.com", "any-password-value-here")

        for response in (wrong_password, nonexistent, legacy):
            self.assertEqual(response.status_code, 401)

        self.assertEqual(wrong_password.json()["detail"], nonexistent.json()["detail"])
        self.assertEqual(wrong_password.json()["detail"], legacy.json()["detail"])

    def test_password_or_hash_never_appears_in_login_response(self):
        self._register("login.no.leak@example.com")
        response = self._login("login.no.leak@example.com", VALID_PASSWORD)

        self.assertNotIn(VALID_PASSWORD, response.text)
        self.assertNotIn("password_hash", response.text)

    def test_login_with_missing_jwt_secret_key_is_a_controlled_server_error(self):
        self._register("login.config.fail@example.com")

        with patch.dict(os.environ, {"JWT_SECRET_KEY": ""}, clear=False):
            response = self._login("login.config.fail@example.com", VALID_PASSWORD)

        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SYNTHETIC_SECRET, response.text)
        self.assertNotIn("JWT_SECRET_KEY", response.text)

    def test_login_with_invalid_access_token_expire_minutes_is_a_controlled_server_error(self):
        self._register("login.config.fail.expiry@example.com")

        with patch.dict(os.environ, {"ACCESS_TOKEN_EXPIRE_MINUTES": "not-a-number"}, clear=False):
            response = self._login("login.config.fail.expiry@example.com", VALID_PASSWORD)

        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SYNTHETIC_SECRET, response.text)

    def test_login_normalizes_uppercase_domain_like_registration_does(self):
        register_response = self._register("Case.Check@EXAMPLE.COM")
        self.assertEqual(register_response.status_code, 201)
        stored_mail = register_response.json()["mail"]
        self.assertEqual(stored_mail, "Case.Check@example.com")

        # Login with an equivalent normalized form of the same address.
        response = self._login("Case.Check@example.com", VALID_PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())

    def test_malformed_login_email_gets_same_generic_401_as_nonexistent(self):
        malformed = self._login("not-an-email-at-all", "irrelevant-password-value")
        nonexistent = self._login("truly.nonexistent@example.com", "irrelevant-password-value")

        self.assertEqual(malformed.status_code, 401)
        self.assertEqual(nonexistent.status_code, 401)
        self.assertEqual(malformed.json()["detail"], nonexistent.json()["detail"])

    # -- ME ------------------------------------------------------------

    def test_valid_token_returns_correct_user(self):
        register_response = self._register("me.valid@example.com")
        expected_user_id = register_response.json()["user_id"]

        login_response = self._login("me.valid@example.com", VALID_PASSWORD)
        token = login_response.json()["access_token"]

        me_response = self.client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )

        self.assertEqual(me_response.status_code, 200)
        body = me_response.json()
        self.assertEqual(body["user_id"], expected_user_id)
        self.assertEqual(body["mail"], "me.valid@example.com")
        self.assertFalse(body["is_admin"])

    def test_me_response_contains_no_password_hash(self):
        self._register("me.no.leak@example.com")
        login_response = self._login("me.no.leak@example.com", VALID_PASSWORD)
        token = login_response.json()["access_token"]

        me_response = self.client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )

        self.assertNotIn("password_hash", me_response.text)
        self.assertNotIn("password", me_response.json())

    def test_missing_bearer_token_returns_401(self):
        response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 401)
        self.assertIn("bearer", response.headers.get("www-authenticate", "").lower())

    def test_malformed_token_returns_401(self):
        response = self.client.get(
            "/auth/me", headers={"Authorization": "Bearer this-is-not-a-jwt"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("bearer", response.headers.get("www-authenticate", "").lower())

    def test_expired_token_returns_401(self):
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"sub": "1", "iat": now - timedelta(hours=2), "exp": now - timedelta(minutes=1)},
            SYNTHETIC_SECRET,
            algorithm="HS256",
        )
        response = self.client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("bearer", response.headers.get("www-authenticate", "").lower())

    def test_token_for_nonexistent_user_returns_401(self):
        token = jwt.encode(
            {"sub": "999999", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            SYNTHETIC_SECRET,
            algorithm="HS256",
        )
        response = self.client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("bearer", response.headers.get("www-authenticate", "").lower())


class _RaceWindowThenDuplicateDbStub:
    """Simulates the exact commit-time UNIQUE-constraint race: at pre-check
    time no conflicting row is visible (another request hasn't committed
    yet), the INSERT still collides at commit time, and a post-rollback
    lookup then finds the concurrently-committed row.

    A lightweight hand-built stub rather than a real second SQLAlchemy
    session/thread, since exercising this deterministically through the
    real engine would need actual concurrency control for one narrow branch.
    """

    def __init__(self, conflicting_user):
        self._conflicting_user = conflicting_user
        self._query_call_count = 0
        self.rolled_back = False

    def query(self, model):
        self._query_call_count += 1
        call_number = self._query_call_count
        stub = self

        class _Query:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                if call_number == 1:
                    return None  # pre-check: race window, nothing visible yet
                return stub._conflicting_user  # post-rollback re-check

        return _Query()

    def add(self, obj):
        pass

    def commit(self):
        raise IntegrityError(
            "UNIQUE constraint failed: users.mail",
            params=None,
            orig=Exception("UNIQUE constraint failed: users.mail"),
        )

    def rollback(self):
        self.rolled_back = True

    def refresh(self, obj):
        pass


class _UnrelatedIntegrityErrorDbStub:
    """An IntegrityError not caused by the email uniqueness constraint --
    the pre-check and the post-rollback re-check both find nothing, so the
    original error must propagate rather than being mislabeled as 409.
    """

    def __init__(self):
        self.rolled_back = False

    def query(self, model):
        class _Query:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return None

        return _Query()

    def add(self, obj):
        pass

    def commit(self):
        raise IntegrityError(
            "NOT NULL constraint failed: users.name",
            params=None,
            orig=Exception("NOT NULL constraint failed: users.name"),
        )

    def rollback(self):
        self.rolled_back = True

    def refresh(self, obj):
        pass


class AuthRegisterRaceConditionTests(unittest.TestCase):
    def test_registration_handles_commit_time_duplicate_email_race(self):
        conflicting_user = models.User(
            user_id=999,
            name="Race Winner",
            mail="race.condition@example.com",
            password_hash="irrelevant-for-this-test",
            is_admin=False,
        )
        fake_db = _RaceWindowThenDuplicateDbStub(conflicting_user)
        payload = schemas.UserRegister(
            name="Race Loser",
            mail="race.condition@example.com",
            password=VALID_PASSWORD,
        )

        with self.assertRaises(HTTPException) as ctx:
            auth.register(payload=payload, db=fake_db)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(fake_db.rolled_back)

    def test_registration_reraises_unrelated_integrity_error(self):
        fake_db = _UnrelatedIntegrityErrorDbStub()
        payload = schemas.UserRegister(
            name="Irrelevant",
            mail="unrelated.integrity@example.com",
            password=VALID_PASSWORD,
        )

        with self.assertRaises(IntegrityError):
            auth.register(payload=payload, db=fake_db)

        self.assertTrue(fake_db.rolled_back)


if __name__ == "__main__":
    unittest.main()
