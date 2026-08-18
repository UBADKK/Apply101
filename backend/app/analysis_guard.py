"""Cost/concurrency protection for expensive per-resource operations.

Used by normal-user profile analysis (E3.2) and admin job analysis /
batch job analysis (E3.3). Independent of backend/app/rate_limiting.py
(E3.1's in-process auth-abuse limiter) -- this module is entirely
DB-backed, since its state (an active analysis lease, a cooldown) must be
correct across concurrent requests on possibly different threads/
connections and must survive a process restart.

Numeric configuration is read from the environment at call time, never
cached at import time (same discipline as security.py/rate_limiting.py).

Persisted lease/cooldown state uses Unix epoch seconds (float), never
datetime -- SQLite does not round-trip timezone-aware datetimes (verified
directly: a stored timezone-aware value comes back naive, which then
raises TypeError against a fresh timezone-aware `now`), and this state
must survive a process restart, which time.monotonic() cannot.

Internally, all acquire/release logic is implemented once as a generic,
resource-list-based core (_try_acquire_guard_resources /
_release_guard_resources) operating on (operation_type, resource_id)
pairs. The public profile-analysis functions (try_acquire_profile_
analysis_guard / release_profile_analysis_guard) and the job-analysis /
batch functions added for E3.3 are thin, behavior-preserving wrappers
around that same core -- there is exactly one acquire implementation and
one release implementation in this module, not one per feature.
"""

from __future__ import annotations

import logging
import math
import os
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import AnalysisGuard


logger = logging.getLogger(__name__)


class AnalysisGuardConfigError(RuntimeError):
    """Raised when E3.2 configuration is missing/invalid/inconsistent, or
    when the route's injected session is bound in a way this module
    cannot safely operate on (see _guard_session). Both cases are mapped
    to a generic 500 by the route.
    """


PROFILE_OPERATION_TYPE = "profile_analysis_profile"
USER_OPERATION_TYPE = "profile_analysis_user"

JOB_OPERATION_TYPE = "job_analysis"
JOB_BATCH_OPERATION_TYPE = "job_analysis_batch"
# The batch guard is a single global singleton shared by both batch
# endpoints (analyze-missing, analyze-sample) -- not keyed by admin, since
# the job corpus and OpenAI budget it protects are global, not per-admin.
# operation_type already disambiguates this row from any real job_id, so
# resource_id=0 can never collide with a per-job "job_analysis" row even
# if a real job_id were ever 0.
JOB_BATCH_RESOURCE_ID = 0

# Not an environment variable -- a fixed operational constant covering the
# JSON parsing / pydantic validation / DB write time between "the OpenAI
# call returns" and "the guard is actually released".
GUARD_SAFETY_MARGIN_SECONDS = 30

# The OpenAI SDK honors a server-provided Retry-After header up to this
# many seconds per retry (verified from installed openai._base_client
# source) -- used as the conservative per-retry delay bound in the lease
# validation below, since the SDK's own exponential-backoff cap (8s) is
# not the only delay it will ever wait.
_SDK_MAX_RETRY_AFTER_SECONDS = 60


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise AnalysisGuardConfigError(f"{name} must be a whole number; got {raw_value!r}.")
    if value <= 0:
        raise AnalysisGuardConfigError(f"{name} must be a positive integer; got {value}.")
    return value


def _get_non_negative_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise AnalysisGuardConfigError(f"{name} must be a whole number; got {raw_value!r}.")
    if value < 0:
        raise AnalysisGuardConfigError(f"{name} must be a non-negative integer; got {value}.")
    return value


@dataclass(frozen=True)
class AnalysisGuardConfig:
    openai_timeout_seconds: int
    openai_max_retries: int
    lease_ttl_seconds: int
    profile_success_cooldown_seconds: int
    profile_failure_cooldown_seconds: int
    user_success_cooldown_seconds: int
    user_failure_cooldown_seconds: int


def load_config() -> AnalysisGuardConfig:
    """Reads and validates the complete E3.2 configuration in one call.
    Must be called -- and must succeed -- before any guard row is touched
    or OpenAI is reached, so invalid configuration can never create guard
    state, start a cooldown, write a failed-analysis row, or call OpenAI.
    """
    timeout_seconds = _get_positive_int_env("PROFILE_ANALYSIS_OPENAI_TIMEOUT_SECONDS", 60)
    max_retries = _get_non_negative_int_env("PROFILE_ANALYSIS_OPENAI_MAX_RETRIES", 0)
    lease_ttl_seconds = _get_positive_int_env("PROFILE_ANALYSIS_GUARD_LEASE_TTL_SECONDS", 300)
    profile_success_cooldown = _get_positive_int_env(
        "PROFILE_ANALYSIS_PROFILE_SUCCESS_COOLDOWN_SECONDS", 300
    )
    profile_failure_cooldown = _get_positive_int_env(
        "PROFILE_ANALYSIS_PROFILE_FAILURE_COOLDOWN_SECONDS", 30
    )
    user_success_cooldown = _get_positive_int_env(
        "PROFILE_ANALYSIS_USER_SUCCESS_COOLDOWN_SECONDS", 60
    )
    user_failure_cooldown = _get_positive_int_env(
        "PROFILE_ANALYSIS_USER_FAILURE_COOLDOWN_SECONDS", 30
    )

    # Conservative OPERATIONAL bound, not a formal wall-clock deadline
    # guaranteed by httpx: httpx.Timeout's connect/read/write/pool
    # components are per-phase inactivity timeouts, not one absolute
    # deadline for the whole request. For a non-streaming call (this one),
    # the read timeout approximates a total-duration bound in practice,
    # but this is a property of typical HTTP behavior, not something
    # httpx documents or guarantees. This check exists so an operator
    # cannot configure a lease shorter than the worst realistic duration
    # of the OpenAI call it exists to protect.
    required_minimum_lease = (
        (max_retries + 1) * timeout_seconds
        + max_retries * _SDK_MAX_RETRY_AFTER_SECONDS
        + GUARD_SAFETY_MARGIN_SECONDS
    )
    if lease_ttl_seconds < required_minimum_lease:
        raise AnalysisGuardConfigError(
            "PROFILE_ANALYSIS_GUARD_LEASE_TTL_SECONDS "
            f"({lease_ttl_seconds}) is too short for the configured OpenAI "
            f"timeout/retry settings; it must be at least {required_minimum_lease} "
            "seconds: (max_retries + 1) * timeout + max_retries * 60 + "
            f"{GUARD_SAFETY_MARGIN_SECONDS}."
        )

    return AnalysisGuardConfig(
        openai_timeout_seconds=timeout_seconds,
        openai_max_retries=max_retries,
        lease_ttl_seconds=lease_ttl_seconds,
        profile_success_cooldown_seconds=profile_success_cooldown,
        profile_failure_cooldown_seconds=profile_failure_cooldown,
        user_success_cooldown_seconds=user_success_cooldown,
        user_failure_cooldown_seconds=user_failure_cooldown,
    )


@dataclass(frozen=True)
class JobAnalysisConfig:
    openai_timeout_seconds: int
    openai_max_retries: int
    lease_ttl_seconds: int
    success_cooldown_seconds: int
    failure_cooldown_seconds: int


def load_job_analysis_config() -> JobAnalysisConfig:
    """Reads and validates the complete per-job E3.3 configuration in one
    call. Must be called -- and must succeed -- before any per-job guard
    row is touched or OpenAI is reached, mirroring load_config's
    discipline exactly. Reuses the same lease-inequality reasoning and the
    same GUARD_SAFETY_MARGIN_SECONDS / _SDK_MAX_RETRY_AFTER_SECONDS
    constants as the profile-analysis formula.
    """
    timeout_seconds = _get_positive_int_env("JOB_ANALYSIS_OPENAI_TIMEOUT_SECONDS", 60)
    max_retries = _get_non_negative_int_env("JOB_ANALYSIS_OPENAI_MAX_RETRIES", 0)
    lease_ttl_seconds = _get_positive_int_env("JOB_ANALYSIS_GUARD_LEASE_TTL_SECONDS", 300)
    success_cooldown = _get_positive_int_env("JOB_ANALYSIS_SUCCESS_COOLDOWN_SECONDS", 300)
    failure_cooldown = _get_positive_int_env("JOB_ANALYSIS_FAILURE_COOLDOWN_SECONDS", 30)

    required_minimum_lease = (
        (max_retries + 1) * timeout_seconds
        + max_retries * _SDK_MAX_RETRY_AFTER_SECONDS
        + GUARD_SAFETY_MARGIN_SECONDS
    )
    if lease_ttl_seconds < required_minimum_lease:
        raise AnalysisGuardConfigError(
            "JOB_ANALYSIS_GUARD_LEASE_TTL_SECONDS "
            f"({lease_ttl_seconds}) is too short for the configured OpenAI "
            f"timeout/retry settings; it must be at least {required_minimum_lease} "
            "seconds: (max_retries + 1) * timeout + max_retries * 60 + "
            f"{GUARD_SAFETY_MARGIN_SECONDS}."
        )

    return JobAnalysisConfig(
        openai_timeout_seconds=timeout_seconds,
        openai_max_retries=max_retries,
        lease_ttl_seconds=lease_ttl_seconds,
        success_cooldown_seconds=success_cooldown,
        failure_cooldown_seconds=failure_cooldown,
    )


@dataclass(frozen=True)
class JobAnalysisBatchConfig:
    lease_ttl_seconds: int
    success_cooldown_seconds: int
    failure_cooldown_seconds: int


def load_job_analysis_batch_config(*, job_guard_lease_seconds: int) -> JobAnalysisBatchConfig:
    """Reads and validates the batch-level E3.3 configuration. Requires
    the ALREADY-loaded per-job lease value (job_guard_lease_seconds) as an
    explicit input -- never a hardcoded constant -- so the batch lease is
    always validated against whatever is actually configured for the
    per-job dimension it must outlast.

    Under the approved renewal design, the batch lease only ever needs to
    survive the gap between one renewal (issued immediately before each
    job) and the next: exactly one job's own worst-case duration (already
    bounded by job_guard_lease_seconds) plus the batch loop's own fast,
    DB-only overhead around it. No separate "how many jobs" multiplier is
    needed or used.
    """
    lease_ttl_seconds = _get_positive_int_env("JOB_ANALYSIS_BATCH_LEASE_TTL_SECONDS", 330)
    success_cooldown = _get_positive_int_env("JOB_ANALYSIS_BATCH_SUCCESS_COOLDOWN_SECONDS", 60)
    failure_cooldown = _get_positive_int_env("JOB_ANALYSIS_BATCH_FAILURE_COOLDOWN_SECONDS", 30)

    required_minimum_lease = job_guard_lease_seconds + GUARD_SAFETY_MARGIN_SECONDS
    if lease_ttl_seconds < required_minimum_lease:
        raise AnalysisGuardConfigError(
            "JOB_ANALYSIS_BATCH_LEASE_TTL_SECONDS "
            f"({lease_ttl_seconds}) is too short; it must be at least "
            f"{required_minimum_lease} seconds: job_guard_lease_seconds "
            f"({job_guard_lease_seconds}) + {GUARD_SAFETY_MARGIN_SECONDS}."
        )

    return JobAnalysisBatchConfig(
        lease_ttl_seconds=lease_ttl_seconds,
        success_cooldown_seconds=success_cooldown,
        failure_cooldown_seconds=failure_cooldown,
    )


class AcquireOutcome(Enum):
    GRANTED = "granted"
    ALREADY_IN_PROGRESS = "already_in_progress"
    COOLDOWN_ACTIVE = "cooldown_active"
    BACKEND_UNAVAILABLE = "backend_unavailable"


@dataclass(frozen=True)
class AcquireResult:
    outcome: AcquireOutcome
    owner_token: str | None = None
    retry_after_seconds: int | None = None


def _safe_rollback(session: Session) -> None:
    """Best-effort rollback that never raises and never logs anything that
    could expose secrets (owner tokens, bound SQL parameters) -- only the
    failing exception's type name, if any.
    """
    try:
        session.rollback()
    except Exception as exc:
        logger.warning("analysis guard: rollback failed (%s)", type(exc).__name__)


def _safe_close(session: Session) -> None:
    """Best-effort close that never raises, so a close-time failure can
    never override an already-computed return value in the caller's
    `finally` block.
    """
    try:
        session.close()
    except Exception as exc:
        logger.warning("analysis guard: session close failed (%s)", type(exc).__name__)


def _guard_session(db: Session) -> Session:
    """Derives a genuinely independent, short-lived Session bound to the
    SAME engine as the route's injected `db` session (via db.get_bind()),
    never the production engine/SessionLocal imported directly -- so this
    automatically follows whatever engine (real or synthetic-per-test)
    `db` is already bound to. Never commits or rolls back `db` itself;
    the caller is responsible for closing the returned session.
    """
    bind = db.get_bind()
    if not isinstance(bind, Engine):
        raise AnalysisGuardConfigError(
            "Analysis guard requires the route session to be bound to an "
            f"Engine, not a {type(bind).__name__}."
        )
    return Session(bind=bind)


def _try_acquire_guard_resources(
    db: Session,
    *,
    resources: list[tuple[str, int]],
    lease_ttl_seconds: int,
    clock: Callable[[], float] = time.time,
) -> AcquireResult:
    """Generic core: atomically acquires ALL listed (operation_type,
    resource_id) resources under one owner token, or none. Never calls
    OpenAI -- purely a DB operation, committed and closed before the
    caller may proceed to the actual expensive work.

    Resources are acquired in the given list order purely for
    consistency/auditability; all are attempted within the same
    uncommitted transaction and either committed together or rolled back
    together, so ordering does not affect correctness here. Works
    identically for a single resource (job analysis) or two (profile
    analysis).
    """
    if not resources or len(set(resources)) != len(resources):
        # Internal programmer error, not a runtime/backend condition --
        # fail loudly rather than silently returning a false GRANTED (an
        # empty list would otherwise vacuously satisfy `won == set(resources)`)
        # or silently under-locking (a duplicate pair would collapse through
        # set() equality). Never embed the actual resource values here.
        raise ValueError(
            "resources must be a non-empty list of unique (operation_type, resource_id) pairs."
        )

    now = clock()
    owner_token = secrets.token_urlsafe(32)
    lease_expires_at = now + lease_ttl_seconds

    try:
        guard_session = _guard_session(db)
    except AnalysisGuardConfigError:
        # Intentional, distinct contract: an actual misconfiguration must
        # keep propagating so the route can return its own 500, not be
        # folded into "backend unavailable".
        raise
    except Exception as exc:
        logger.warning(
            "analysis guard: failed to open guard session (%s); backend unavailable",
            type(exc).__name__,
        )
        return AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE)

    try:
        try:
            for operation_type, resource_id in resources:
                stmt = sqlite_insert(AnalysisGuard).values(
                    operation_type=operation_type,
                    resource_id=resource_id,
                    owner_token=owner_token,
                    lock_expires_at=lease_expires_at,
                    cooldown_until=None,
                ).on_conflict_do_update(
                    index_elements=["operation_type", "resource_id"],
                    set_={"owner_token": owner_token, "lock_expires_at": lease_expires_at},
                    where=and_(
                        or_(
                            AnalysisGuard.owner_token.is_(None),
                            # A malformed row (owner_token set but no expiry)
                            # must never become permanently unacquirable --
                            # `NULL <= now` is neither true nor false in SQL,
                            # so it has to be listed explicitly here.
                            AnalysisGuard.lock_expires_at.is_(None),
                            AnalysisGuard.lock_expires_at <= now,
                        ),
                        or_(AnalysisGuard.cooldown_until.is_(None), AnalysisGuard.cooldown_until <= now),
                    ),
                )
                guard_session.execute(stmt)

            condition = or_(*[
                and_(AnalysisGuard.operation_type == op, AnalysisGuard.resource_id == rid)
                for op, rid in resources
            ])
            rows = guard_session.execute(select(AnalysisGuard).where(condition)).scalars().all()
        except Exception as exc:
            _safe_rollback(guard_session)
            logger.warning(
                "analysis guard: acquisition query failed (%s); backend unavailable",
                type(exc).__name__,
            )
            return AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE)

        won = {(row.operation_type, row.resource_id) for row in rows if row.owner_token == owner_token}

        if won == set(resources):
            try:
                guard_session.commit()
            except Exception as exc:
                _safe_rollback(guard_session)
                logger.warning(
                    "analysis guard: acquisition commit failed (%s); backend unavailable",
                    type(exc).__name__,
                )
                return AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE)
            return AcquireResult(outcome=AcquireOutcome.GRANTED, owner_token=owner_token)

        # Capture blocker state from THIS still-uncommitted read -- the
        # exact snapshot the upserts above were conditioned against --
        # before rolling back. A later, post-rollback read would race
        # against concurrent release/takeover activity and could report
        # no blockers even though acquisition genuinely failed.
        blockers = [row for row in rows if (row.operation_type, row.resource_id) not in won]
        _safe_rollback(guard_session)

        if not blockers:
            # Should not happen (every requested resource always has a row
            # by this point), but never guess -- fail closed.
            return AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE)

        if any(
            b.owner_token is not None and b.lock_expires_at is not None and b.lock_expires_at > now
            for b in blockers
        ):
            return AcquireResult(outcome=AcquireOutcome.ALREADY_IN_PROGRESS)

        cooldown_blockers = [
            b for b in blockers if b.cooldown_until is not None and b.cooldown_until > now
        ]
        if cooldown_blockers:
            retry_after = max(b.cooldown_until for b in cooldown_blockers) - now
            return AcquireResult(
                outcome=AcquireOutcome.COOLDOWN_ACTIVE,
                retry_after_seconds=max(1, math.ceil(retry_after)),
            )

        # A blocker existed but is neither actively held nor cooling down
        # -- a concurrent acquirer won it between our upsert and our read.
        # Fail closed rather than guess; never call max() on an empty
        # sequence.
        return AcquireResult(outcome=AcquireOutcome.BACKEND_UNAVAILABLE)
    finally:
        _safe_close(guard_session)


def try_acquire_profile_analysis_guard(
    db: Session,
    *,
    profile_id: int,
    owner_user_id: int,
    config: AnalysisGuardConfig,
    clock: Callable[[], float] = time.time,
) -> AcquireResult:
    """Atomically acquires BOTH the per-profile and per-owner-user guard
    rows under one owner token, or neither. Thin wrapper over the generic
    two-resource acquisition core; signature and behavior unchanged from
    E3.2 (deterministic profile-then-user order, all-or-nothing atomicity).
    """
    return _try_acquire_guard_resources(
        db,
        resources=[
            (PROFILE_OPERATION_TYPE, profile_id),
            (USER_OPERATION_TYPE, owner_user_id),
        ],
        lease_ttl_seconds=config.lease_ttl_seconds,
        clock=clock,
    )


def _release_guard_resources(
    db: Session,
    *,
    resource_cooldowns: list[tuple[str, int, int]],
    owner_token: str,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Generic core: releases ALL listed (operation_type, resource_id,
    cooldown_seconds) resources, each independently gated by owner_token
    so a stale/taken-over owner can never clear a newer owner's row (and a
    repeated release of the same token is a safe no-op). Returns True on
    success, False if the release itself failed -- the caller must NOT let
    a False return override an already-determined route response; a
    failed release only means the row(s) self-heal via natural lease
    expiry rather than being freed immediately.
    """
    resource_keys = [
        (operation_type, resource_id)
        for operation_type, resource_id, _cooldown_seconds in resource_cooldowns
    ]
    if not resource_cooldowns or len(set(resource_keys)) != len(resource_keys):
        # Internal programmer error, not a runtime/backend condition --
        # reject duplicate (operation_type, resource_id) keys even when
        # their cooldown values differ, since that would otherwise update
        # the same row twice with ambiguous last-update-wins behavior.
        # Never embed the actual resource values here.
        raise ValueError(
            "resource_cooldowns must be a non-empty list with unique "
            "(operation_type, resource_id) keys."
        )

    now = clock()

    try:
        guard_session = _guard_session(db)
    except Exception as exc:
        logger.warning(
            "analysis guard: release failed to open guard session (%s)",
            type(exc).__name__,
        )
        return False

    try:
        try:
            for operation_type, resource_id, cooldown_seconds in resource_cooldowns:
                guard_session.query(AnalysisGuard).filter(
                    AnalysisGuard.operation_type == operation_type,
                    AnalysisGuard.resource_id == resource_id,
                    AnalysisGuard.owner_token == owner_token,
                ).update(
                    {
                        "owner_token": None,
                        "lock_expires_at": None,
                        "cooldown_until": now + cooldown_seconds,
                    },
                    synchronize_session=False,
                )
            guard_session.commit()
            return True
        except Exception as exc:
            _safe_rollback(guard_session)
            logger.warning(
                "analysis guard: release transaction failed (%s)",
                type(exc).__name__,
            )
            return False
    finally:
        _safe_close(guard_session)


def release_profile_analysis_guard(
    db: Session,
    *,
    profile_id: int,
    owner_user_id: int,
    owner_token: str,
    succeeded: bool,
    config: AnalysisGuardConfig,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Releases both guard rows. Thin wrapper over the generic release
    core; signature and behavior unchanged from E3.2.
    """
    profile_cooldown = (
        config.profile_success_cooldown_seconds if succeeded else config.profile_failure_cooldown_seconds
    )
    user_cooldown = (
        config.user_success_cooldown_seconds if succeeded else config.user_failure_cooldown_seconds
    )
    return _release_guard_resources(
        db,
        resource_cooldowns=[
            (PROFILE_OPERATION_TYPE, profile_id, profile_cooldown),
            (USER_OPERATION_TYPE, owner_user_id, user_cooldown),
        ],
        owner_token=owner_token,
        clock=clock,
    )


def try_acquire_job_analysis_guard(
    db: Session,
    *,
    job_id: int,
    config: JobAnalysisConfig,
    clock: Callable[[], float] = time.time,
) -> AcquireResult:
    """Acquires the single per-job guard resource. Jobs have no per-user
    ownership concept (every job-analysis route requires admin access, and
    the job corpus is global) -- unlike profile analysis, there is no
    second "owner" resource dimension here.
    """
    return _try_acquire_guard_resources(
        db,
        resources=[(JOB_OPERATION_TYPE, job_id)],
        lease_ttl_seconds=config.lease_ttl_seconds,
        clock=clock,
    )


def release_job_analysis_guard(
    db: Session,
    *,
    job_id: int,
    owner_token: str,
    succeeded: bool,
    config: JobAnalysisConfig,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Releases the single per-job guard resource."""
    cooldown_seconds = (
        config.success_cooldown_seconds if succeeded else config.failure_cooldown_seconds
    )
    return _release_guard_resources(
        db,
        resource_cooldowns=[(JOB_OPERATION_TYPE, job_id, cooldown_seconds)],
        owner_token=owner_token,
        clock=clock,
    )


def try_acquire_job_batch_guard(
    db: Session,
    *,
    config: JobAnalysisBatchConfig,
    clock: Callable[[], float] = time.time,
) -> AcquireResult:
    """Acquires the single global batch-start guard, shared by both
    analyze-missing and analyze-sample (JOB_BATCH_RESOURCE_ID is a fixed
    singleton, not keyed by admin or job). Manual single-job analysis
    never consults this guard.
    """
    return _try_acquire_guard_resources(
        db,
        resources=[(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID)],
        lease_ttl_seconds=config.lease_ttl_seconds,
        clock=clock,
    )


def release_job_batch_guard(
    db: Session,
    *,
    owner_token: str,
    succeeded: bool,
    config: JobAnalysisBatchConfig,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Releases the single global batch-start guard."""
    cooldown_seconds = (
        config.success_cooldown_seconds if succeeded else config.failure_cooldown_seconds
    )
    return _release_guard_resources(
        db,
        resource_cooldowns=[(JOB_BATCH_OPERATION_TYPE, JOB_BATCH_RESOURCE_ID, cooldown_seconds)],
        owner_token=owner_token,
        clock=clock,
    )


class RenewOutcome(Enum):
    RENEWED = "renewed"
    OWNERSHIP_LOST = "ownership_lost"
    BACKEND_UNAVAILABLE = "backend_unavailable"


@dataclass(frozen=True)
class RenewResult:
    outcome: RenewOutcome


def renew_job_batch_guard(
    db: Session,
    *,
    owner_token: str,
    config: JobAnalysisBatchConfig,
    clock: Callable[[], float] = time.time,
) -> RenewResult:
    """Owner-token- and live-expiry-gated renewal of the shared batch
    lease, called immediately before each per-job unit of work inside a
    batch -- not a fixed lease sized for the whole batch. Uses a
    dedicated short-lived guard session, entirely independent of the
    route's own `db` session; never commits or rolls back `db` itself.

    The live-expiry predicate (lock_expires_at IS NOT NULL AND
    lock_expires_at > now) is required, not optional: without it, an
    owner whose lease already expired (and who may therefore no longer be
    the true holder once a new owner takes over) could otherwise
    resurrect its own stale lease. Empirically verified against a
    synthetic temp-file SQLite database: correct-owner+live-lease matches
    exactly one row and commits; wrong owner, missing row, expired lease,
    null expiry, and a stale owner attempting to renew after a takeover
    all match zero rows and change nothing.
    """
    now = clock()
    new_expiry = now + config.lease_ttl_seconds

    try:
        guard_session = _guard_session(db)
    except Exception as exc:
        logger.warning(
            "analysis guard: batch renewal failed to open guard session (%s)",
            type(exc).__name__,
        )
        return RenewResult(outcome=RenewOutcome.BACKEND_UNAVAILABLE)

    try:
        try:
            stmt = (
                update(AnalysisGuard)
                .where(
                    AnalysisGuard.operation_type == JOB_BATCH_OPERATION_TYPE,
                    AnalysisGuard.resource_id == JOB_BATCH_RESOURCE_ID,
                    AnalysisGuard.owner_token == owner_token,
                    AnalysisGuard.lock_expires_at.is_not(None),
                    AnalysisGuard.lock_expires_at > now,
                )
                .values(lock_expires_at=new_expiry)
            )
            matched = guard_session.execute(stmt).rowcount
        except Exception as exc:
            _safe_rollback(guard_session)
            logger.warning(
                "analysis guard: batch renewal transaction failed (%s)",
                type(exc).__name__,
            )
            return RenewResult(outcome=RenewOutcome.BACKEND_UNAVAILABLE)

        if matched == 1:
            try:
                guard_session.commit()
            except Exception as exc:
                _safe_rollback(guard_session)
                logger.warning(
                    "analysis guard: batch renewal commit failed (%s)",
                    type(exc).__name__,
                )
                return RenewResult(outcome=RenewOutcome.BACKEND_UNAVAILABLE)
            return RenewResult(outcome=RenewOutcome.RENEWED)

        if matched == 0:
            _safe_rollback(guard_session)
            return RenewResult(outcome=RenewOutcome.OWNERSHIP_LOST)

        # Unsupported/unexpected rowcount (None, negative, or >1) -- fail
        # closed rather than guess. This statement's WHERE clause is
        # scoped to the exact composite primary key, so rowcount can never
        # legitimately exceed 1; a value outside {0, 1} indicates the
        # backend is not behaving as expected, not that ownership was
        # ordinarily lost. Never commit here, and never log the actual
        # rowcount value.
        _safe_rollback(guard_session)
        logger.warning(
            "analysis guard: batch renewal saw an unsupported rowcount; backend unavailable"
        )
        return RenewResult(outcome=RenewOutcome.BACKEND_UNAVAILABLE)
    finally:
        _safe_close(guard_session)
