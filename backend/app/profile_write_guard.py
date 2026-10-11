"""Owner-identity guard for profile operations (profile analysis, matching,
match listing) whose user/profile may be deleted -- and the same ids reused
by a different person -- while the request is in flight.

users/candidate_profiles ids have no AUTOINCREMENT, so SQLite may hand a
deleted user's/profile's id to the next row, and SQLite foreign keys are not
enforced here. A numeric id is therefore not proof of who owns a row. What
is: the owner's per-row random token_key (models.User.token_key). A key is
never reused and never returns to a previous value, so if the same
(profile_id, user_id, token_key) triple is read at two points in time, the
same user owned the profile the whole time -- profiles are only ever deleted
together with their user -- and every row read in between belonged to that
owner. A legacy NULL key is compared as-is (NULL only matches NULL); every
user created through the ORM gets a fresh key, so a reused user_id never
matches a captured NULL.

Protocol, per request:

1. capture_profile_owner_identity -- once, at the start, before any
   commit/rollback. Reads the profile, its owner and the owner's token_key in
   ONE statement and refreshes the session's profile/user objects from it
   (populate_existing), so all data later taken from them is the captured
   owner's. If the caller is the owner, the key must equal the key that
   authenticated this request (current_user.token_key, verified against the
   JWT by get_current_user), which closes the window between authentication
   and capture. An admin acting on another user's profile has no such
   anchor; it simply operates on whoever owns the profile at capture time,
   with consistent data.
2. verify_profile_owner_current_or_404 -- AFTER the reads whose results are
   returned (cached results, error details naming analysis ids/versions,
   listings) and before returning them.
   Profile analysis also passes profile_owner_unchanged as the precondition
   of its guard acquisition, so the same check is repeated on the guard
   session under the guard's write lock; a mismatch there acquires nothing
   (no lease, no cooldown on a new owner's reused ids) and becomes the 404.
3. verify_profile_owner_unchanged_or_404 -- after every write of the
   operation was issued to the session (at least one INSERT/UPDATE pending
   or executed) and before commit. It flushes first: with pysqlite's default
   (legacy) transaction control SELECTs run outside any transaction, but the
   first INSERT/UPDATE emits BEGIN and takes SQLite's RESERVED write lock,
   which this connection keeps until commit/rollback. The identity SELECT
   that follows therefore sees the latest committed state, and no other
   connection can commit a delete (or delete-and-reuse) between this check
   and our commit.

Every mismatch raises ProfileOwnerChanged: the same 404 "Profile not found."
get_owned_profile uses, so a changed owner is indistinguishable from a
missing profile and none of the new owner's data is ever revealed.
"""

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from . import models


# Same status/body as get_owned_profile (backend/app/auth_dependencies.py).
PROFILE_NOT_FOUND_DETAIL = "Profile not found."


class ProfileOwnerChanged(HTTPException):
    """The generic profile 404, raised when the captured owner identity no
    longer holds. A distinct type only so callers that otherwise collect
    per-item HTTP errors (the bulk match route) can stop the whole request
    instead of continuing for an owner that is gone."""

    def __init__(self):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PROFILE_NOT_FOUND_DETAIL,
        )


@dataclass(frozen=True)
class ProfileOwnerIdentity:
    profile_id: int
    user_id: int
    token_key: str | None


def _current_owner_token_key_row(db: Session, profile_id: int, user_id: int):
    # Column-only query: never returns identity-mapped ORM objects, so a
    # stale in-session User/CandidateProfile can't mask a deleted or reused
    # row.
    return (
        db.query(models.User.token_key)
        .join(
            models.CandidateProfile,
            models.CandidateProfile.user_id == models.User.user_id,
        )
        .filter(
            models.CandidateProfile.profile_id == profile_id,
            models.User.user_id == user_id,
        )
        .first()
    )


def capture_profile_owner_identity(
    db: Session,
    profile: models.CandidateProfile,
    current_user: models.User,
) -> ProfileOwnerIdentity:
    """Call once, at the start of the route body, before any commit/rollback
    (`profile` and `current_user` must still be the objects authorization
    resolved). Raises ProfileOwnerChanged if the profile or its owner is
    gone, or if the caller is the owner and the owner row no longer carries
    the key this request authenticated with."""
    # Plain values first: the refreshing query below overwrites
    # current_user in place when the caller is the profile owner (same
    # identity-map entry).
    caller_user_id = current_user.user_id
    caller_token_key = current_user.token_key
    profile_id = profile.profile_id
    user_id = profile.user_id

    row = (
        db.query(models.CandidateProfile, models.User)
        .join(models.User, models.User.user_id == models.CandidateProfile.user_id)
        .filter(
            models.CandidateProfile.profile_id == profile_id,
            models.CandidateProfile.user_id == user_id,
        )
        .populate_existing()
        .first()
    )
    if row is None:
        raise ProfileOwnerChanged()

    token_key = row[1].token_key

    if caller_user_id == user_id and (
        not caller_token_key or token_key != caller_token_key
    ):
        raise ProfileOwnerChanged()

    return ProfileOwnerIdentity(
        profile_id=profile_id,
        user_id=user_id,
        token_key=token_key,
    )


def _owner_unchanged(db: Session, identity: ProfileOwnerIdentity) -> bool:
    row = _current_owner_token_key_row(db, identity.profile_id, identity.user_id)
    return row is not None and row[0] == identity.token_key


def profile_owner_unchanged(db: Session, identity: ProfileOwnerIdentity) -> bool:
    """True if the profile still exists, still belongs to identity.user_id
    and that user still has the captured token_key. Never raises for a
    mismatch and never writes, so it can serve as a guard-acquisition
    precondition run on the guard's own session."""
    return _owner_unchanged(db, identity)


def verify_profile_owner_current_or_404(
    db: Session, identity: ProfileOwnerIdentity
) -> None:
    """Read-side re-check: call after the reads whose results are about to
    be returned. Writes nothing and needs no lock -- an unchanged key proves
    the rows read since capture were this owner's."""
    if not _owner_unchanged(db, identity):
        raise ProfileOwnerChanged()


def verify_profile_owner_unchanged_or_404(
    db: Session, identity: ProfileOwnerIdentity
) -> None:
    """Write-side check: flushes pending writes, then checks the profile
    still exists, still belongs to identity.user_id, and that user still has
    the captured token_key. On mismatch rolls back the whole transaction
    (pending rows and any is_current flips) and raises ProfileOwnerChanged."""
    db.flush()

    if not _owner_unchanged(db, identity):
        db.rollback()
        raise ProfileOwnerChanged()
