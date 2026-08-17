"""In-process, thread-safe fixed-window rate limiting for the auth endpoints.

Deliberately independent of FastAPI/SQLAlchemy request/DB objects -- this
module only knows about abstract "counter spaces" and string keys. Callers
(backend/routers/auth.py) decide what those keys mean (an IP, an
IP+account pair) and what the numeric limits are.

Numeric configuration is always read from the environment at call time, not
cached at import or construction time (same discipline as
backend/app/security.py), so tests can patch os.environ per-test without
stale values leaking in from an earlier import.

The production limiter instance is a module-level singleton, but route code
must never import and use it directly -- it is exposed only through the
get_rate_limiter() FastAPI dependency, so tests can override it with a
fresh, isolated instance (and a fake clock) per test.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import itertools
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from fastapi import Request


logger = logging.getLogger(__name__)


class RateLimitConfigError(RuntimeError):
    """Raised when a rate-limit environment variable is set but invalid."""


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        raise RateLimitConfigError(
            f"{name} must be a whole number; got {raw_value!r}."
        )

    if value <= 0:
        raise RateLimitConfigError(
            f"{name} must be a positive integer; got {value}."
        )

    return value


def login_pair_max_failures() -> int:
    return _get_positive_int_env("LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES", 5)


def login_ip_max_failures() -> int:
    return _get_positive_int_env("LOGIN_RATE_LIMIT_IP_MAX_FAILURES", 20)


def login_window_seconds() -> int:
    return _get_positive_int_env("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 15 * 60)


def register_ip_max_attempts() -> int:
    return _get_positive_int_env("REGISTER_RATE_LIMIT_IP_MAX_ATTEMPTS", 5)


def register_window_seconds() -> int:
    return _get_positive_int_env("REGISTER_RATE_LIMIT_WINDOW_SECONDS", 60 * 60)


def default_max_tracked_keys() -> int:
    return _get_positive_int_env("RATE_LIMIT_MAX_TRACKED_KEYS", 10_000)


# ---------------------------------------------------------------------------
# IP extraction / normalization
# ---------------------------------------------------------------------------

# Shared, deliberately conservative bucket for any request whose real peer
# address is unavailable or unparseable. Never crashes, never trusts a
# client-supplied header, and is strictly MORE restrictive than per-IP
# keying (many such requests share one budget), never less.
UNKNOWN_CLIENT_KEY = "unknown"


def normalize_client_ip(raw_host: str | None) -> str:
    """Normalizes a raw peer address string to a canonical bucket key.

    IPv4-mapped IPv6 addresses (::ffff:a.b.c.d) collapse to their plain
    IPv4 form so the same real address always lands in the same bucket
    regardless of which textual form the ASGI server reports.
    """
    if not raw_host:
        return UNKNOWN_CLIENT_KEY

    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        return UNKNOWN_CLIENT_KEY

    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return address.compressed

    return str(address)


def get_client_ip_key(request: Request) -> str:
    """Direct ASGI peer address only -- X-Forwarded-For/X-Real-IP and any
    other client-supplied header are never read, since nothing in this
    deployment's topology makes them trustworthy (see the E3 design audit).
    """
    client = request.client
    if client is None or not client.host:
        return UNKNOWN_CLIENT_KEY
    return normalize_client_ip(client.host)


# ---------------------------------------------------------------------------
# Core reservation engine
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    """The set of reservation_ids IS the source of truth for how many slots
    are consumed in this generation -- there is deliberately no parallel
    mutable integer count that could drift out of sync with it. Limit
    checks use len(reservation_ids); release() removes an id via
    set.discard(), which is a safe no-op if the id is already absent (or
    already removed by a prior/concurrent release of the same token).
    """

    generation: int
    window_seconds: int
    reservation_ids: set[str]


@dataclass(frozen=True)
class ReservationSpec:
    counter_space: str
    key: str
    max_count: int
    window_seconds: int


@dataclass(frozen=True)
class ReservationToken:
    counter_space: str
    key: str
    generation: int
    reservation_id: str


class ReservationOutcome(Enum):
    GRANTED = "granted"
    LIMIT_EXCEEDED = "limit_exceeded"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"


@dataclass
class ReservationResult:
    outcome: ReservationOutcome
    tokens: list[ReservationToken] | None = None
    retry_after_seconds: int | None = None


_ACCOUNT_KEY_DOMAIN = b"apply101.rate_limiting.login_account.v1"

_SWEEP_BATCH_SIZE = 32


class RateLimiter:
    """Thread-safe, bounded, fixed-window multi-key reservation engine.

    All state is process-local. A single lock guards every check and
    mutation, so a multi-key reservation (e.g. login's IP+account pair AND
    IP-only buckets together) is atomic: either every involved key has room
    and all of them are incremented together, or none are touched at all.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        hmac_secret: bytes | None = None,
        max_tracked_keys: int | None = None,
    ):
        self._clock = clock
        # Process-local, generated fresh per instance -- this is not a
        # credential and does not need to survive a restart. Must never
        # reuse JWT_SECRET_KEY (a different trust domain entirely).
        self._hmac_secret = (
            hmac_secret if hmac_secret is not None else secrets.token_bytes(32)
        )
        # None (production) means "read RATE_LIMIT_MAX_TRACKED_KEYS from the
        # environment fresh on every call"; a test may inject a small fixed
        # int to exercise capacity behavior deterministically.
        self._max_tracked_keys_override = max_tracked_keys
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, _Bucket]] = {}

    # -- account-key derivation -------------------------------------------

    def derive_account_key(self, normalized_or_raw_email: str) -> str:
        """Domain-separated HMAC-SHA256 of the submitted login identifier.
        Only the digest is ever stored as a bucket key -- the raw string is
        never retained.
        """
        mac = hmac.new(self._hmac_secret, digestmod=hashlib.sha256)
        mac.update(_ACCOUNT_KEY_DOMAIN)
        mac.update(b"\x00")
        mac.update(normalized_or_raw_email.encode("utf-8"))
        return mac.hexdigest()

    # -- capacity -----------------------------------------------------------

    def _resolve_max_tracked_keys(self) -> int:
        if self._max_tracked_keys_override is not None:
            return self._max_tracked_keys_override
        return default_max_tracked_keys()

    def _has_room(self, new_key_count: int) -> bool:
        max_keys = self._resolve_max_tracked_keys()
        total_tracked = sum(len(space) for space in self._counters.values())
        return (max_keys - total_tracked) >= new_key_count

    # -- expiry sweeps --------------------------------------------------------
    # Neither sweep ever removes an entry whose window is still current --
    # only genuinely stale (rolled-over) entries are ever reclaimed.

    def _bounded_sweep(self, now: float) -> None:
        """Cheap, opportunistic cleanup run on every reservation attempt:
        checks only the first _SWEEP_BATCH_SIZE entries per counter space,
        so its cost never scales with total tracked-key count.
        """
        for space in self._counters.values():
            if not space:
                continue
            stale_keys = [
                key
                for key, bucket in itertools.islice(space.items(), _SWEEP_BATCH_SIZE)
                if bucket.generation != int(now // bucket.window_seconds)
            ]
            for key in stale_keys:
                del space[key]

    def _full_sweep(self, now: float) -> None:
        """Unconditional full scan. Only invoked as a capacity backstop when
        the bounded sweep didn't free enough room for new keys -- this is
        what guarantees expired entries are eventually reclaimed without any
        background thread.
        """
        for space in self._counters.values():
            stale_keys = [
                key
                for key, bucket in space.items()
                if bucket.generation != int(now // bucket.window_seconds)
            ]
            for key in stale_keys:
                del space[key]

    # -- reservation ----------------------------------------------------------

    def _retry_after_seconds(self, generation: int, window_seconds: int, now: float) -> int:
        window_end = (generation + 1) * window_seconds
        remaining = window_end - now
        return max(1, math.ceil(remaining))

    def _commit(
        self,
        prepared: list[tuple[ReservationSpec, int, bool]],
        now: float,
    ) -> ReservationResult:
        tokens = []
        for spec, generation, _already_exists in prepared:
            space = self._counters.setdefault(spec.counter_space, {})
            bucket = space.get(spec.key)
            if bucket is None or bucket.generation != generation:
                bucket = _Bucket(
                    generation=generation,
                    window_seconds=spec.window_seconds,
                    reservation_ids=set(),
                )
                space[spec.key] = bucket

            # 128 bits of randomness, unique per granted reservation -- this
            # identity, not a bare count, is what release() removes.
            reservation_id = secrets.token_hex(16)
            bucket.reservation_ids.add(reservation_id)

            tokens.append(
                ReservationToken(
                    counter_space=spec.counter_space,
                    key=spec.key,
                    generation=generation,
                    reservation_id=reservation_id,
                )
            )
        return ReservationResult(outcome=ReservationOutcome.GRANTED, tokens=tokens)

    def try_reserve(
        self,
        specs: list[ReservationSpec],
        now: float | None = None,
    ) -> ReservationResult:
        """Atomically checks and reserves one slot in every listed bucket.

        Either every spec has room and all of them are incremented
        together, or none are touched -- there is no partially-applied
        outcome.
        """
        if now is None:
            now = self._clock()

        with self._lock:
            self._bounded_sweep(now)

            blocking_retry_afters: list[int] = []
            prepared: list[tuple[ReservationSpec, int, bool]] = []

            for spec in specs:
                generation = int(now // spec.window_seconds)
                space = self._counters.get(spec.counter_space)
                bucket = space.get(spec.key) if space is not None else None
                already_exists = bucket is not None
                current_count = (
                    len(bucket.reservation_ids)
                    if bucket is not None and bucket.generation == generation
                    else 0
                )

                if current_count >= spec.max_count:
                    blocking_retry_afters.append(
                        self._retry_after_seconds(generation, spec.window_seconds, now)
                    )
                else:
                    prepared.append((spec, generation, already_exists))

            if blocking_retry_afters:
                return ReservationResult(
                    outcome=ReservationOutcome.LIMIT_EXCEEDED,
                    retry_after_seconds=max(blocking_retry_afters),
                )

            new_key_count = sum(1 for _, _, exists in prepared if not exists)
            if new_key_count > 0 and not self._has_room(new_key_count):
                self._full_sweep(now)
                if not self._has_room(new_key_count):
                    logger.warning(
                        "rate limiter capacity exhausted; rejecting new "
                        "key(s) for counter_space(s)=%s",
                        sorted({spec.counter_space for spec, _, _ in prepared}),
                    )
                    return ReservationResult(outcome=ReservationOutcome.CAPACITY_UNAVAILABLE)

            return self._commit(prepared, now)

    def release(self, tokens: list[ReservationToken] | None) -> None:
        """Releases exactly the reservation identities in `tokens`.

        Truly idempotent: removing an id that is already absent (because
        this exact token was already released once, sequentially or
        concurrently under the lock) is a safe no-op via set.discard() --
        it can never remove a *different* reservation's id, so it can never
        erase another request's retained failure.

        A token whose generation no longer matches the live bucket (the
        window rolled over since it was reserved) is silently ignored -- it
        must never touch a newer window's bucket.

        If removing an id empties a bucket, that bucket is deleted
        immediately, freeing its tracked-key capacity right away rather
        than waiting for a later expiry sweep. A bucket that still holds
        other retained/in-flight reservation ids is left untouched.
        """
        if not tokens:
            return

        with self._lock:
            for token in tokens:
                space = self._counters.get(token.counter_space)
                if space is None:
                    continue
                bucket = space.get(token.key)
                if bucket is None or bucket.generation != token.generation:
                    continue

                bucket.reservation_ids.discard(token.reservation_id)

                if not bucket.reservation_ids:
                    del space[token.key]


# ---------------------------------------------------------------------------
# Login / registration specific reservation helpers
# ---------------------------------------------------------------------------

def reserve_login_attempt(
    limiter: RateLimiter,
    *,
    ip_key: str,
    account_key: str,
    now: float | None = None,
) -> ReservationResult:
    # Pair checked first so that when both would block, the pair (the more
    # precise, primary control) is what determines the outcome.
    specs = [
        ReservationSpec(
            counter_space="login_pair",
            key=f"{ip_key}|{account_key}",
            max_count=login_pair_max_failures(),
            window_seconds=login_window_seconds(),
        ),
        ReservationSpec(
            counter_space="login_ip",
            key=ip_key,
            max_count=login_ip_max_failures(),
            window_seconds=login_window_seconds(),
        ),
    ]
    return limiter.try_reserve(specs, now=now)


def reserve_registration_attempt(
    limiter: RateLimiter,
    *,
    ip_key: str,
    now: float | None = None,
) -> ReservationResult:
    specs = [
        ReservationSpec(
            counter_space="register_ip",
            key=ip_key,
            max_count=register_ip_max_attempts(),
            window_seconds=register_window_seconds(),
        ),
    ]
    return limiter.try_reserve(specs, now=now)


# ---------------------------------------------------------------------------
# Production dependency
# ---------------------------------------------------------------------------

# A single process-local instance. Route code must never import this name
# directly -- only via get_rate_limiter(), so tests can override the
# dependency with a fresh instance and a fake clock.
_production_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _production_limiter
