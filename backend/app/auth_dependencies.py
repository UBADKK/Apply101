"""Reusable current-user resolution and ownership/authorization helpers.

Not wired into any existing users/profiles/jobs/matches route yet (that is a
later phase) -- get_current_user backs the /auth endpoints already, and
get_current_admin / require_user_access / get_owned_profile are the
authorization building blocks defined here for that later wiring.
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


def get_current_admin(
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """403 if authenticated but not an admin. Authentication failures
    (missing/expired/invalid token) are already handled by get_current_user
    and surface as 401 before this ever runs.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )

    return current_user


def require_user_access(
    user_id: int,
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """Validates that the authenticated user may act as the path's user_id
    -- either because it's their own, or because they're an admin. Returns
    404, not 403, for a mismatch: this app never reveals that a user_id
    exists but belongs to someone else.

    This only validates access to user_id itself; it does not replace
    resource-specific existence checks (e.g. a profile_id lookup still
    needs its own query -- see get_owned_profile).
    """
    if current_user.user_id == user_id or current_user.is_admin:
        return current_user

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="User not found.",
    )


def get_owned_profile(
    user_id: int,
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> models.CandidateProfile:
    """Resolves a CandidateProfile only when the caller is allowed to see
    it: either user_id is their own, or they're an admin, AND profile_id
    actually belongs to user_id. Every failure mode -- wrong user_id, wrong
    profile_id, or a profile_id that belongs to a different user than the
    one named in the path -- returns the same generic 404.
    """
    # Enforce path-level access before touching the profile at all, so a
    # non-owner never learns anything about whether profile_id exists.
    if not (current_user.user_id == user_id or current_user.is_admin):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found.",
        )

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id,
    ).first()

    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found.",
        )

    return profile
