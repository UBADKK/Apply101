"""Password hashing and JWT helpers.

This module has no dependency on FastAPI, SQLAlchemy, or any route/model
code -- it is pure security infrastructure, usable and testable on its own
before any auth endpoint or route protection exists.

JWT_SECRET_KEY and ACCESS_TOKEN_EXPIRE_MINUTES are read from the process
environment at call time (not cached at import time), so this module never
needs to know how/when .env was loaded and can be exercised in tests with
patched environment variables.
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


MIN_PASSWORD_LENGTH = 15

JWT_ALGORITHM = "HS256"
DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 480
MIN_JWT_SECRET_KEY_BYTES = 32


class AuthConfigError(RuntimeError):
    """Raised when required auth environment configuration is missing or invalid."""


class TokenError(Exception):
    """Raised for any malformed, expired, or otherwise untrustworthy access token."""


_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except VerificationError:
        # Covers both a plain verification failure and the documented
        # password-mismatch subclass (VerifyMismatchError).
        return False
    except InvalidHashError:
        # password_hash is not a well-formed argon2 hash string.
        return False


def _get_jwt_secret_key() -> str:
    secret_key = os.environ.get("JWT_SECRET_KEY")
    if not secret_key:
        raise AuthConfigError(
            "JWT_SECRET_KEY environment variable is not set. It must be "
            "configured before any JWT functionality can be used."
        )
    if len(secret_key.encode("utf-8")) < MIN_JWT_SECRET_KEY_BYTES:
        raise AuthConfigError(
            "JWT_SECRET_KEY is too short. It must be at least "
            f"{MIN_JWT_SECRET_KEY_BYTES} bytes when UTF-8 encoded."
        )
    return secret_key


def _get_access_token_expire_minutes() -> int:
    raw_value = os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES")
    if not raw_value:
        return DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES

    try:
        minutes = int(raw_value)
    except ValueError:
        raise AuthConfigError(
            "ACCESS_TOKEN_EXPIRE_MINUTES must be a whole number of minutes; "
            f"got {raw_value!r}."
        )

    if minutes <= 0:
        raise AuthConfigError(
            "ACCESS_TOKEN_EXPIRE_MINUTES must be a positive number of "
            f"minutes; got {minutes}."
        )

    return minutes


def create_access_token(user_id: int) -> str:
    secret_key = _get_jwt_secret_key()
    expire_minutes = _get_access_token_expire_minutes()

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }

    return jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> int:
    secret_key = _get_jwt_secret_key()

    try:
        # algorithms is an explicit allow-list: PyJWT verifies the token's
        # header alg against it and rejects anything else, so the token's
        # own header can never choose how it gets verified. options=require
        # rejects a token outright if exp or sub is absent (iss/aud/nbf are
        # not required for v1).
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Access token is invalid.") from exc

    subject = payload.get("sub")
    if subject is None:
        raise TokenError("Access token is missing its subject claim.")

    try:
        return int(subject)
    except (TypeError, ValueError) as exc:
        raise TokenError("Access token subject is not a valid user id.") from exc
