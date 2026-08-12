"""Reusable current-user resolution for protected routes.

Not wired into any existing route yet (that is a later phase) -- this only
provides get_current_user for the new /auth endpoints to use.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .security import AuthConfigError, TokenError, decode_access_token


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    # Deliberately identical for every failure reason (malformed, expired,
    # invalid signature/algorithm, missing claims, or a token whose user no
    # longer exists) -- callers must not be able to distinguish these cases.
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        user_id = decode_access_token(token)
    except TokenError:
        raise unauthorized
    except AuthConfigError:
        # A missing/invalid JWT_SECRET_KEY is a server misconfiguration, not
        # a bad credential -- never fold it into the 401 path, and never
        # include the underlying config error text (it names the missing
        # env var, never its value, but the client still doesn't need it).
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication is not correctly configured.",
        )

    user = db.query(models.User).filter(models.User.user_id == user_id).first()
    if user is None:
        raise unauthorized

    return user
