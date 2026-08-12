from pydantic import EmailStr, TypeAdapter, ValidationError
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..app.database import get_db
from ..app import models, schemas
from ..app.auth_dependencies import get_current_user
from ..app.security import (
    AuthConfigError,
    create_access_token,
    hash_password,
    verify_password,
)


router = APIRouter(
    prefix="/auth",
    tags=["Auth"]
)


_email_adapter = TypeAdapter(EmailStr)


def _normalize_login_email(raw_email: str) -> str | None:
    """Validate/normalize a submitted login identifier the same way
    registration's EmailStr field does (domain lowercased), without ever
    surfacing a validation-specific error -- malformed input just means no
    normalized email to look up, handled identically to "user not found".
    """
    try:
        return _email_adapter.validate_python(raw_email)
    except ValidationError:
        return None


# Computed once at import time, never per-request. This is not a real
# credential and must never grant access on its own -- it exists purely so
# a nonexistent/passwordless login still pays the same Argon2 verification
# cost as a real wrong-password attempt.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-used-only-for-timing-0000")


@router.post(
    "/register",
    response_model=schemas.CurrentUserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(payload: schemas.UserRegister, db: Session = Depends(get_db)):
    existing_user = db.query(models.User).filter(
        models.User.mail == payload.mail
    ).first()

    # An email match rejects registration outright, including a legacy row
    # whose password_hash is NULL -- registration never claims or modifies
    # an existing user row.
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    new_user = models.User(
        name=payload.name,
        mail=payload.mail,
        password_hash=hash_password(payload.password),
        is_admin=False,
    )

    db.add(new_user)

    try:
        db.commit()
    except IntegrityError:
        # Two requests can both pass the pre-check above before either
        # commits. The UNIQUE constraint on users.mail is the real source of
        # truth; if it fired because this email now exists, treat it the
        # same as the normal duplicate-email rejection. Any other integrity
        # failure is not ours to reinterpret -- re-raise it untouched.
        db.rollback()

        conflicting_user = db.query(models.User).filter(
            models.User.mail == payload.mail
        ).first()

        if conflicting_user is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )

        raise

    db.refresh(new_user)

    return new_user


@router.post("/login", response_model=schemas.AuthTokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    # OAuth2PasswordRequestForm's `username` field carries the email here.
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    normalized_email = _normalize_login_email(form_data.username)

    user = None
    if normalized_email is not None:
        user = db.query(models.User).filter(
            models.User.mail == normalized_email
        ).first()

    # Always run Argon2 verification, even when there is no real user or no
    # password set, using a fixed dummy hash in that case. This removes the
    # obvious timing discrepancy between "user/password state already rules
    # this out" and "a real user just typed the wrong password" -- without
    # attempting to make timing mathematically uniform.
    hash_to_verify = (
        user.password_hash
        if user is not None and user.password_hash is not None
        else _DUMMY_PASSWORD_HASH
    )
    password_valid = verify_password(form_data.password, hash_to_verify)

    # Malformed email, nonexistent user, legacy passwordless user, and wrong
    # password all produce the exact same response -- none of those states
    # is distinguishable from outside.
    if user is None or user.password_hash is None or not password_valid:
        raise invalid_credentials

    try:
        access_token = create_access_token(user.user_id)
    except AuthConfigError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication is not correctly configured.",
        )

    return schemas.AuthTokenResponse(access_token=access_token)


@router.get("/me", response_model=schemas.CurrentUserResponse)
def read_current_user(
    current_user: models.User = Depends(get_current_user),
):
    return current_user
