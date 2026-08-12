import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

from backend.app import security


# Synthetic, test-only values -- never read from or written to real .env.
# At least 48 bytes so PyJWT doesn't emit InsecureKeyLengthWarning for the
# HS384 cross-algorithm test below.
SYNTHETIC_SECRET = "synthetic-test-secret-do-not-use-in-production-0123456789"
VALID_PASSWORD = "a-valid-test-password-123"  # 26 chars, >= MIN_PASSWORD_LENGTH


class PasswordHashingTests(unittest.TestCase):
    def test_valid_password_hashes_and_verifies(self):
        password_hash = security.hash_password(VALID_PASSWORD)
        self.assertTrue(security.verify_password(VALID_PASSWORD, password_hash))

    def test_same_password_produces_distinct_secure_hashes(self):
        hash_one = security.hash_password(VALID_PASSWORD)
        hash_two = security.hash_password(VALID_PASSWORD)

        self.assertNotEqual(hash_one, hash_two)  # random salt per hash
        self.assertTrue(security.verify_password(VALID_PASSWORD, hash_one))
        self.assertTrue(security.verify_password(VALID_PASSWORD, hash_two))

    def test_wrong_password_returns_false(self):
        password_hash = security.hash_password(VALID_PASSWORD)
        self.assertFalse(
            security.verify_password("a-completely-different-password", password_hash)
        )

    def test_password_shorter_than_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            security.hash_password("short1234567")  # 13 chars

    def test_password_exactly_at_minimum_length_is_accepted(self):
        password = "x" * security.MIN_PASSWORD_LENGTH
        password_hash = security.hash_password(password)
        self.assertTrue(security.verify_password(password, password_hash))

    def test_64_character_password_works(self):
        password = "p" * 64
        password_hash = security.hash_password(password)
        self.assertTrue(security.verify_password(password, password_hash))

    def test_password_longer_than_64_is_not_silently_truncated(self):
        password_a = ("a" * 64) + "SUFFIX-ONE"
        password_b = ("a" * 64) + "SUFFIX-TWO"
        hash_a = security.hash_password(password_a)

        # If the implementation silently truncated to 64 characters,
        # password_b (identical for its first 64 characters) would
        # incorrectly verify against hash_a.
        self.assertFalse(security.verify_password(password_b, hash_a))
        self.assertTrue(security.verify_password(password_a, hash_a))

    def test_malformed_password_hash_fails_safely(self):
        self.assertFalse(
            security.verify_password(VALID_PASSWORD, "not-a-real-argon2-hash")
        )
        self.assertFalse(security.verify_password(VALID_PASSWORD, ""))


class JWTTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_and_decode_round_trip_returns_user_id(self):
        token = security.create_access_token(user_id=42)
        self.assertEqual(security.decode_access_token(token), 42)

    def test_token_contains_expiry_claim(self):
        token = security.create_access_token(user_id=1)
        payload = jwt.decode(token, SYNTHETIC_SECRET, algorithms=["HS256"])
        self.assertIn("exp", payload)

    def test_expired_token_is_rejected(self):
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"sub": "7", "iat": now - timedelta(hours=2), "exp": now - timedelta(minutes=1)},
            SYNTHETIC_SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(security.TokenError):
            security.decode_access_token(token)

    def test_malformed_token_is_rejected(self):
        with self.assertRaises(security.TokenError):
            security.decode_access_token("this.is-not.a-jwt")

    def test_token_signed_with_wrong_secret_is_rejected(self):
        token = jwt.encode(
            {"sub": "3", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            "a-completely-different-secret-0123456789",
            algorithm="HS256",
        )
        with self.assertRaises(security.TokenError):
            security.decode_access_token(token)

    def test_token_missing_sub_is_rejected(self):
        # Correctly signed, but the required `sub` claim is absent entirely.
        token = jwt.encode(
            {"exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            SYNTHETIC_SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(security.TokenError):
            security.decode_access_token(token)

    def test_token_missing_exp_is_rejected(self):
        # Correctly signed, but the required `exp` claim is absent entirely
        # (distinct from an exp that is merely in the past).
        token = jwt.encode({"sub": "9"}, SYNTHETIC_SECRET, algorithm="HS256")
        with self.assertRaises(security.TokenError):
            security.decode_access_token(token)

    def test_token_with_non_integer_sub_is_rejected(self):
        token = jwt.encode(
            {
                "sub": "not-an-integer",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            SYNTHETIC_SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(security.TokenError):
            security.decode_access_token(token)

    def test_token_using_a_different_algorithm_is_rejected(self):
        # Same secret, different algorithm: an HS256-only decoder must not
        # accept this, regardless of what the token's own header claims.
        token = jwt.encode(
            {"sub": "5", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            SYNTHETIC_SECRET,
            algorithm="HS384",
        )
        with self.assertRaises(security.TokenError):
            security.decode_access_token(token)

    def test_missing_jwt_secret_key_produces_controlled_failure(self):
        with patch.dict(os.environ, {"JWT_SECRET_KEY": ""}, clear=False):
            with self.assertRaises(security.AuthConfigError):
                security.create_access_token(user_id=1)

    def test_31_byte_secret_is_rejected(self):
        secret = "a" * 31  # 31 bytes when UTF-8 encoded (ASCII)
        self.assertEqual(len(secret.encode("utf-8")), 31)
        with patch.dict(os.environ, {"JWT_SECRET_KEY": secret}, clear=False):
            with self.assertRaises(security.AuthConfigError):
                security.create_access_token(user_id=1)

    def test_32_byte_secret_is_accepted(self):
        secret = "a" * 32  # exactly the minimum, 32 bytes when UTF-8 encoded
        self.assertEqual(len(secret.encode("utf-8")), 32)
        with patch.dict(os.environ, {"JWT_SECRET_KEY": secret}, clear=False):
            token = security.create_access_token(user_id=1)
            self.assertEqual(security.decode_access_token(token), 1)

    def test_invalid_access_token_expire_minutes_is_rejected(self):
        with patch.dict(
            os.environ, {"ACCESS_TOKEN_EXPIRE_MINUTES": "not-a-number"}, clear=False
        ):
            with self.assertRaises(security.AuthConfigError):
                security.create_access_token(user_id=1)

    def test_non_positive_access_token_expire_minutes_is_rejected(self):
        with patch.dict(os.environ, {"ACCESS_TOKEN_EXPIRE_MINUTES": "0"}, clear=False):
            with self.assertRaises(security.AuthConfigError):
                security.create_access_token(user_id=1)

        with patch.dict(os.environ, {"ACCESS_TOKEN_EXPIRE_MINUTES": "-5"}, clear=False):
            with self.assertRaises(security.AuthConfigError):
                security.create_access_token(user_id=1)


if __name__ == "__main__":
    unittest.main()
