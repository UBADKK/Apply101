from pydantic import EmailStr, TypeAdapter, ValidationError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..app.database import get_db
from ..app import models, schemas
from ..app.auth_dependencies import get_current_user
from ..app.rate_limiting import (
    RateLimitConfigError,
    RateLimiter,
    ReservationOutcome,
    get_client_ip_key,
    get_rate_limiter,
    reserve_login_attempt,
    reserve_registration_attempt,
)
from ..app.security import (
    AuthConfigError,
    create_access_token,
    hash_password,
    verify_password,
)


_RATE_LIMIT_EXCEEDED_DETAIL = "Too many requests. Please try again later."
_RATE_LIMIT_UNAVAILABLE_DETAIL = "Service temporarily unavailable. Please try again shortly."


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
def register(
    request: Request,
    payload: schemas.UserRegister,
    db: Session = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    # Volume/account-creation protection, not failed-credential tracking:
    # every request that reaches this point consumes the reservation,
    # including successful registrations and duplicate-account attempts,
    # and it is never released. Checked before any password hashing or DB
    # mutation.
    ip_key = get_client_ip_key(request)

    try:
        reservation = reserve_registration_attempt(rate_limiter, ip_key=ip_key)
    except RateLimitConfigError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rate limiting is not correctly configured.",
        )

    if reservation.outcome is ReservationOutcome.CAPACITY_UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_RATE_LIMIT_UNAVAILABLE_DETAIL,
        )

    if reservation.outcome is ReservationOutcome.LIMIT_EXCEEDED:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMIT_EXCEEDED_DETAIL,
            headers={"Retry-After": str(reservation.retry_after_seconds)},
        )

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
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    # OAuth2PasswordRequestForm's `username` field carries the email here.
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    normalized_email = _normalize_login_email(form_data.username)
    # Prefer the normalized form (so case/domain variants of the same real
    # address collapse to one bucket, like everywhere else in this app);
    # fall back to the raw submitted string only so malformed-email spam is
    # still throttled per distinct string. Either way this is immediately
    # HMAC-derived below -- the raw value itself is never stored as a key.
    account_key_source = normalized_email if normalized_email is not None else form_data.username

    ip_key = get_client_ip_key(request)
    account_key = rate_limiter.derive_account_key(account_key_source)

    # Atomic reservation BEFORE any password verification (real or dummy):
    # this is what makes concurrent in-flight attempts count against the
    # same budget a purely check-then-increment design would miss, and it
    # means an already-over-budget caller never costs the server an Argon2
    # call at all.
    try:
        reservation = reserve_login_attempt(
            rate_limiter, ip_key=ip_key, account_key=account_key
        )
    except RateLimitConfigError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rate limiting is not correctly configured.",
        )

    if reservation.outcome is ReservationOutcome.CAPACITY_UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_RATE_LIMIT_UNAVAILABLE_DETAIL,
        )

    if reservation.outcome is ReservationOutcome.LIMIT_EXCEEDED:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMIT_EXCEEDED_DETAIL,
            headers={"Retry-After": str(reservation.retry_after_seconds)},
        )

    tokens = reservation.tokens

    try:
        user = None
        if normalized_email is not None:
            user = db.query(models.User).filter(
                models.User.mail == normalized_email
            ).first()

        # Always run Argon2 verification, even when there is no real user or
        # no password set, using a fixed dummy hash in that case. This
        # removes the obvious timing discrepancy between "user/password
        # state already rules this out" and "a real user just typed the
        # wrong password" -- without attempting to make timing
        # mathematically uniform.
        hash_to_verify = (
            user.password_hash
            if user is not None and user.password_hash is not None
            else _DUMMY_PASSWORD_HASH
        )
        password_valid = verify_password(form_data.password, hash_to_verify)
    except Exception:
        # A genuinely unexpected failure during credential verification
        # (not an invalid-credentials outcome) isn't evidence of a bad
        # attempt -- release this request's reservation before propagating.
        rate_limiter.release(tokens)
        raise

    # Malformed email, nonexistent user, legacy passwordless user, and wrong
    # password all produce the exact same response -- none of those states
    # is distinguishable from outside. The reservation is deliberately
    # retained here: it IS the recorded failed attempt.
    if user is None or user.password_hash is None or not password_valid:
        raise invalid_credentials

    # Valid credentials: release only this request's own reservation. Other
    # requests' failures (earlier or concurrent) are never touched.
    rate_limiter.release(tokens)

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
