import io
import logging
import math
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base, get_db
from backend.app.rate_limiting import (
    RateLimitConfigError,
    RateLimiter,
    ReservationOutcome,
    ReservationSpec,
    UNKNOWN_CLIENT_KEY,
    default_max_tracked_keys,
    get_rate_limiter,
    login_ip_max_failures,
    login_pair_max_failures,
    login_window_seconds,
    normalize_client_ip,
    register_ip_max_attempts,
    register_window_seconds,
    reserve_login_attempt,
    reserve_registration_attempt,
)
from backend.app.security import hash_password
from backend.routers import auth


VALID_PASSWORD = "a-valid-synthetic-password"


class _FakeClock:
    """Injectable monotonic-style clock: starts at a fixed offset (never 0,
    so // math behaves the same way it would with a real monotonic clock)
    and only moves forward when explicitly advanced -- no real sleeps.
    """

    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ===========================================================================
# Part 1: RateLimiter core -- no FastAPI, no HTTP, pure unit tests.
# ===========================================================================


class BasicReservationTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.limiter = RateLimiter(clock=self.clock, max_tracked_keys=100)

    def _spec(self, space="s", key="k", max_count=5, window=60):
        return ReservationSpec(counter_space=space, key=key, max_count=max_count, window_seconds=window)

    def test_requests_below_limit_are_granted(self):
        for _ in range(4):
            result = self.limiter.try_reserve([self._spec(max_count=5)])
            self.assertEqual(result.outcome, ReservationOutcome.GRANTED)

    def test_exactly_n_allowed_then_boundary_rejection(self):
        spec = self._spec(max_count=3)
        for _ in range(3):
            result = self.limiter.try_reserve([spec])
            self.assertEqual(result.outcome, ReservationOutcome.GRANTED)

        blocked = self.limiter.try_reserve([spec])
        self.assertEqual(blocked.outcome, ReservationOutcome.LIMIT_EXCEEDED)
        self.assertIsNotNone(blocked.retry_after_seconds)
        self.assertGreaterEqual(blocked.retry_after_seconds, 1)

    def test_pair_blocks_before_ip_only(self):
        pair_spec = self._spec(space="login_pair", key="ip|acct", max_count=1, window=60)
        ip_spec = self._spec(space="login_ip", key="ip", max_count=100, window=60)

        first = self.limiter.try_reserve([pair_spec, ip_spec])
        self.assertEqual(first.outcome, ReservationOutcome.GRANTED)

        # Pair is now at its limit (1) while IP-only (100) has plenty of
        # room -- the reservation must still be rejected as a whole, and
        # neither counter may move.
        second = self.limiter.try_reserve([pair_spec, ip_spec])
        self.assertEqual(second.outcome, ReservationOutcome.LIMIT_EXCEEDED)

        # Proven by re-attempting with only the IP spec: still has room.
        ip_only_result = self.limiter.try_reserve([ip_spec])
        self.assertEqual(ip_only_result.outcome, ReservationOutcome.GRANTED)

    def test_multi_key_partial_block_mutates_nothing(self):
        blocked_spec = self._spec(space="a", key="k1", max_count=1, window=60)
        fine_spec = self._spec(space="b", key="k2", max_count=100, window=60)

        # Exhaust the first spec's budget on its own.
        self.limiter.try_reserve([blocked_spec])

        result = self.limiter.try_reserve([blocked_spec, fine_spec])
        self.assertEqual(result.outcome, ReservationOutcome.LIMIT_EXCEEDED)

        # fine_spec must not have been incremented despite having room --
        # a fresh reservation for it alone, up to its real max, must still
        # succeed the full number of times.
        for _ in range(100):
            r = self.limiter.try_reserve([fine_spec])
            self.assertEqual(r.outcome, ReservationOutcome.GRANTED)
        exhausted = self.limiter.try_reserve([fine_spec])
        self.assertEqual(exhausted.outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_different_ips_and_pairs_are_isolated(self):
        spec_a = self._spec(space="login_pair", key="ip1|acctA", max_count=1)
        spec_b = self._spec(space="login_pair", key="ip2|acctB", max_count=1)

        self.assertEqual(self.limiter.try_reserve([spec_a]).outcome, ReservationOutcome.GRANTED)
        # A different key in the same counter space is unaffected by A being
        # exhausted.
        self.assertEqual(self.limiter.try_reserve([spec_b]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(self.limiter.try_reserve([spec_a]).outcome, ReservationOutcome.LIMIT_EXCEEDED)
        self.assertEqual(self.limiter.try_reserve([spec_b]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_release_decrements_only_that_reservation(self):
        spec = self._spec(max_count=2)
        first = self.limiter.try_reserve([spec])
        second = self.limiter.try_reserve([spec])
        self.assertEqual(first.outcome, ReservationOutcome.GRANTED)
        self.assertEqual(second.outcome, ReservationOutcome.GRANTED)

        blocked = self.limiter.try_reserve([spec])
        self.assertEqual(blocked.outcome, ReservationOutcome.LIMIT_EXCEEDED)

        self.limiter.release(first.tokens)

        third = self.limiter.try_reserve([spec])
        self.assertEqual(third.outcome, ReservationOutcome.GRANTED)

    def test_release_does_not_erase_other_failures(self):
        spec = self._spec(max_count=3)
        r1 = self.limiter.try_reserve([spec])
        r2 = self.limiter.try_reserve([spec])
        r3 = self.limiter.try_reserve([spec])
        self.assertEqual(r3.outcome, ReservationOutcome.GRANTED)

        # Release only r2's slot (simulating a successful login among two
        # retained failures).
        self.limiter.release(r2.tokens)

        # Only one slot freed -- one more reservation succeeds, the next is
        # blocked again (r1 and r3's failures are still counted).
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_window_expiry_and_exact_retry_after(self):
        spec = self._spec(max_count=1, window=60)
        self.limiter.try_reserve([spec])

        blocked = self.limiter.try_reserve([spec])
        self.assertEqual(blocked.outcome, ReservationOutcome.LIMIT_EXCEEDED)
        # Generation boundary is at a multiple of 60 relative to the fake
        # clock's arbitrary start -- retry_after must match exactly.
        generation = int(self.clock() // 60)
        window_end = (generation + 1) * 60
        expected_retry_after = max(1, math.ceil(window_end - self.clock()))
        self.assertEqual(blocked.retry_after_seconds, expected_retry_after)

        self.clock.advance(60)
        after_expiry = self.limiter.try_reserve([spec])
        self.assertEqual(after_expiry.outcome, ReservationOutcome.GRANTED)

    def test_release_cannot_decrement_a_newer_generation(self):
        spec = self._spec(max_count=1, window=60)
        stale = self.limiter.try_reserve([spec])
        self.assertEqual(stale.outcome, ReservationOutcome.GRANTED)

        # Roll into a new window and reserve fresh -- a different generation.
        self.clock.advance(60)
        fresh = self.limiter.try_reserve([spec])
        self.assertEqual(fresh.outcome, ReservationOutcome.GRANTED)

        # Releasing the OLD (stale-generation) token must not touch the new
        # generation's counter.
        self.limiter.release(stale.tokens)

        still_blocked = self.limiter.try_reserve([spec])
        self.assertEqual(still_blocked.outcome, ReservationOutcome.LIMIT_EXCEEDED)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_reservations_never_exceed_the_limit(self):
        clock = _FakeClock()
        limiter = RateLimiter(clock=clock, max_tracked_keys=1000)
        spec = ReservationSpec(counter_space="s", key="shared", max_count=10, window_seconds=60)

        granted_count = 0
        blocked_count = 0
        count_lock = threading.Lock()

        def attempt():
            nonlocal granted_count, blocked_count
            result = limiter.try_reserve([spec])
            with count_lock:
                if result.outcome is ReservationOutcome.GRANTED:
                    granted_count += 1
                else:
                    blocked_count += 1

        threads = [threading.Thread(target=attempt) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(granted_count, 10)
        self.assertEqual(blocked_count, 40)


class BoundedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()

    def test_expired_entries_are_reclaimed(self):
        limiter = RateLimiter(clock=self.clock, max_tracked_keys=2)
        spec_a = ReservationSpec("s", "a", max_count=1, window_seconds=60)
        spec_b = ReservationSpec("s", "b", max_count=1, window_seconds=60)

        self.assertEqual(limiter.try_reserve([spec_a]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(limiter.try_reserve([spec_b]).outcome, ReservationOutcome.GRANTED)

        # At capacity (2/2). A brand new key must fail...
        spec_c = ReservationSpec("s", "c", max_count=1, window_seconds=60)
        self.assertEqual(limiter.try_reserve([spec_c]).outcome, ReservationOutcome.CAPACITY_UNAVAILABLE)

        # ...until the window rolls over, at which point a/b become
        # reclaimable and c must succeed without any background thread.
        self.clock.advance(60)
        self.assertEqual(limiter.try_reserve([spec_c]).outcome, ReservationOutcome.GRANTED)

    def test_active_entries_are_never_evicted_to_admit_a_new_key(self):
        limiter = RateLimiter(clock=self.clock, max_tracked_keys=2)
        spec_a = ReservationSpec("s", "a", max_count=5, window_seconds=60)
        spec_b = ReservationSpec("s", "b", max_count=5, window_seconds=60)
        spec_c = ReservationSpec("s", "c", max_count=5, window_seconds=60)

        limiter.try_reserve([spec_a])
        limiter.try_reserve([spec_b])

        # a and b are both still active (well within their window) -- c
        # must be rejected, not silently evict one of them.
        result = limiter.try_reserve([spec_c])
        self.assertEqual(result.outcome, ReservationOutcome.CAPACITY_UNAVAILABLE)

        # Prove a/b are untouched: each still enforces its own real limit.
        for _ in range(4):
            self.assertEqual(limiter.try_reserve([spec_a]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(limiter.try_reserve([spec_a]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_capacity_exhaustion_makes_no_partial_mutation_and_is_logged_safely(self):
        # max_tracked_keys=2, one slot pre-filled -- exactly 1 slot of room
        # remains, so a 2-new-key reservation must fail as a whole.
        limiter = RateLimiter(clock=self.clock, max_tracked_keys=2)
        spec_a = ReservationSpec("s", "a", max_count=5, window_seconds=60)
        limiter.try_reserve([spec_a])

        spec_new_1 = ReservationSpec("s", "new1", max_count=5, window_seconds=60)
        spec_new_2 = ReservationSpec("s", "new2", max_count=5, window_seconds=60)

        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("backend.app.rate_limiting")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            result = limiter.try_reserve([spec_new_1, spec_new_2])
        finally:
            logger.removeHandler(handler)

        self.assertEqual(result.outcome, ReservationOutcome.CAPACITY_UNAVAILABLE)
        self.assertIsNone(result.tokens)

        # Prove NEITHER new1 nor new2 was created (not even one of the
        # two): total tracked count must still be exactly 1 ("a"). One more
        # brand-new key still fits in the single remaining slot...
        check_a = ReservationSpec("s", "checkA", max_count=5, window_seconds=60)
        self.assertEqual(limiter.try_reserve([check_a]).outcome, ReservationOutcome.GRANTED)
        # ...but a second brand-new key does not -- proving capacity was
        # truly still 1/2, not silently bumped by a partial write above.
        check_b = ReservationSpec("s", "checkB", max_count=5, window_seconds=60)
        self.assertEqual(limiter.try_reserve([check_b]).outcome, ReservationOutcome.CAPACITY_UNAVAILABLE)

        log_output = log_stream.getvalue()
        self.assertIn("capacity exhausted", log_output)
        # Safe: only counter-space names, never keys/emails/credentials.
        self.assertNotIn("new1", log_output)
        self.assertNotIn("new2", log_output)


class ConfigTests(unittest.TestCase):
    def test_invalid_numeric_config_raises_typed_error(self):
        with patch.dict(os.environ, {"LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES": "not-a-number"}, clear=False):
            with self.assertRaises(RateLimitConfigError):
                login_pair_max_failures()

    def test_non_positive_config_raises_typed_error(self):
        with patch.dict(os.environ, {"LOGIN_RATE_LIMIT_IP_MAX_FAILURES": "0"}, clear=False):
            with self.assertRaises(RateLimitConfigError):
                login_ip_max_failures()

    def test_unset_config_uses_safe_defaults(self):
        # Defaults must exist and be usable with no environment configured.
        self.assertGreater(login_pair_max_failures(), 0)
        self.assertGreater(login_ip_max_failures(), 0)
        self.assertGreater(login_window_seconds(), 0)
        self.assertGreater(register_ip_max_attempts(), 0)
        self.assertGreater(register_window_seconds(), 0)
        self.assertGreater(default_max_tracked_keys(), 0)


class IpNormalizationTests(unittest.TestCase):
    def test_ipv4_normalizes_to_itself(self):
        self.assertEqual(normalize_client_ip("203.0.113.5"), "203.0.113.5")

    def test_ipv6_canonical_compressed_form(self):
        self.assertEqual(
            normalize_client_ip("2001:0db8:0000:0000:0000:0000:0000:0001"),
            "2001:db8::1",
        )

    def test_ipv4_mapped_ipv6_collapses_to_ipv4(self):
        self.assertEqual(normalize_client_ip("::ffff:127.0.0.1"), "127.0.0.1")
        self.assertEqual(
            normalize_client_ip("::ffff:127.0.0.1"),
            normalize_client_ip("127.0.0.1"),
        )

    def test_malformed_or_missing_uses_shared_conservative_bucket(self):
        self.assertEqual(normalize_client_ip(None), UNKNOWN_CLIENT_KEY)
        self.assertEqual(normalize_client_ip(""), UNKNOWN_CLIENT_KEY)
        self.assertEqual(normalize_client_ip("not-an-ip-at-all"), UNKNOWN_CLIENT_KEY)
        self.assertEqual(normalize_client_ip("testclient"), UNKNOWN_CLIENT_KEY)


class AccountKeyDerivationTests(unittest.TestCase):
    def test_derived_key_never_contains_the_raw_email(self):
        limiter = RateLimiter(hmac_secret=b"synthetic-test-hmac-secret-key!")
        email = "someone.private@example.com"
        derived = limiter.derive_account_key(email)

        self.assertNotIn(email, derived)
        self.assertNotIn("someone.private", derived)
        self.assertNotIn("example.com", derived)
        # A hex SHA-256 digest.
        self.assertEqual(len(derived), 64)
        int(derived, 16)  # raises if not valid hex

    def test_derivation_is_deterministic_and_domain_separated_per_instance(self):
        limiter_a = RateLimiter(hmac_secret=b"secret-one-aaaaaaaaaaaaaaaaaaaaaa")
        limiter_b = RateLimiter(hmac_secret=b"secret-two-bbbbbbbbbbbbbbbbbbbbbb")
        email = "same.email@example.com"

        self.assertEqual(
            limiter_a.derive_account_key(email), limiter_a.derive_account_key(email)
        )
        # Different secrets (different limiter instances) must not collide.
        self.assertNotEqual(
            limiter_a.derive_account_key(email), limiter_b.derive_account_key(email)
        )


# ===========================================================================
# Part 2: Route-level tests through the real /auth/register and /auth/login
# HTTP paths.
# ===========================================================================


def _build_app(engine, rate_limiter):
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
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    return app, session_factory


class _BaseRateLimitRouteTestCase(unittest.TestCase):
    SYNTHETIC_SECRET = "synthetic-test-secret-for-rate-limiting-auth-0123456789"

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_rate_limit_auth_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        self.clock = _FakeClock()
        self.rate_limiter = RateLimiter(clock=self.clock, max_tracked_keys=1000)
        self.app, _ = _build_app(self.engine, self.rate_limiter)

        env_patcher = patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": self.SYNTHETIC_SECRET,
                "LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES": "3",
                "LOGIN_RATE_LIMIT_IP_MAX_FAILURES": "5",
                "LOGIN_RATE_LIMIT_WINDOW_SECONDS": "900",
                "REGISTER_RATE_LIMIT_IP_MAX_ATTEMPTS": "3",
                "REGISTER_RATE_LIMIT_WINDOW_SECONDS": "3600",
            },
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _client(self, ip="203.0.113.10", port=12345):
        return TestClient(self.app, client=(ip, port))

    def _create_user(self, mail, password=VALID_PASSWORD):
        session = self.session_factory()
        try:
            user = models.User(
                name="Synthetic User",
                mail=mail,
                password_hash=hash_password(password) if password else None,
                is_admin=False,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _count_users(self):
        session = self.session_factory()
        try:
            return session.query(models.User).count()
        finally:
            session.close()


class LoginRouteTests(_BaseRateLimitRouteTestCase):
    def test_below_limit_preserves_existing_behavior(self):
        self._create_user("below.limit@example.com")
        client = self._client()

        for _ in range(2):
            response = client.post(
                "/auth/login",
                data={"username": "below.limit@example.com", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)

        good = client.post(
            "/auth/login",
            data={"username": "below.limit@example.com", "password": VALID_PASSWORD},
        )
        self.assertEqual(good.status_code, 200)
        self.assertIn("access_token", good.json())

    def test_exactly_n_failed_attempts_then_429(self):
        self._create_user("boundary@example.com")
        client = self._client()

        for _ in range(3):  # LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES=3
            response = client.post(
                "/auth/login",
                data={"username": "boundary@example.com", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 401)

        blocked = client.post(
            "/auth/login",
            data={"username": "boundary@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        self.assertGreaterEqual(int(blocked.headers["Retry-After"]), 1)
        self.assertEqual(blocked.json(), {"detail": "Too many requests. Please try again later."})

    def test_ip_only_limit_catches_one_ip_spraying_many_accounts(self):
        client = self._client()
        # LOGIN_RATE_LIMIT_IP_MAX_FAILURES=5, distinct account each time so
        # the pair bucket (max 3) never becomes the blocking factor.
        for i in range(5):
            response = client.post(
                "/auth/login",
                data={"username": f"spray{i}@example.com", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 401)

        blocked = client.post(
            "/auth/login",
            data={"username": "spray-final@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_different_ips_and_accounts_stay_isolated(self):
        self._create_user("isolated.one@example.com")
        self._create_user("isolated.two@example.com")
        client_a = self._client(ip="198.51.100.1")
        client_b = self._client(ip="198.51.100.2")

        for _ in range(3):
            client_a.post(
                "/auth/login",
                data={"username": "isolated.one@example.com", "password": "wrong"},
            )
        blocked_a = client_a.post(
            "/auth/login", data={"username": "isolated.one@example.com", "password": "wrong"}
        )
        self.assertEqual(blocked_a.status_code, 429)

        # A different IP AND a different account is entirely unaffected.
        still_fine = client_b.post(
            "/auth/login",
            data={"username": "isolated.two@example.com", "password": VALID_PASSWORD},
        )
        self.assertEqual(still_fine.status_code, 200)

    def test_successful_login_releases_only_its_own_reservation(self):
        self._create_user("release.mine@example.com")
        client = self._client()

        client.post(
            "/auth/login",
            data={"username": "release.mine@example.com", "password": "wrong"},
        )
        client.post(
            "/auth/login",
            data={"username": "release.mine@example.com", "password": "wrong"},
        )
        # 2 of 3 pair-budget consumed. A successful login now releases its
        # own (3rd) reservation...
        success = client.post(
            "/auth/login",
            data={"username": "release.mine@example.com", "password": VALID_PASSWORD},
        )
        self.assertEqual(success.status_code, 200)

        # ...leaving exactly 2 consumed (the earlier failures), so one more
        # failed attempt is still allowed before hitting the boundary.
        third_failure = client.post(
            "/auth/login",
            data={"username": "release.mine@example.com", "password": "wrong"},
        )
        self.assertEqual(third_failure.status_code, 401)
        blocked = client.post(
            "/auth/login",
            data={"username": "release.mine@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_success_does_not_erase_other_earlier_failures(self):
        self._create_user("shared.pair@example.com")
        client = self._client()

        # Fail 3 times (exhausts the pair bucket).
        for _ in range(3):
            client.post(
                "/auth/login",
                data={"username": "shared.pair@example.com", "password": "wrong"},
            )
        blocked = client.post(
            "/auth/login",
            data={"username": "shared.pair@example.com", "password": VALID_PASSWORD},
        )
        # Even with the CORRECT password, the pair bucket is already
        # exhausted -- the reservation itself is rejected before any
        # credential check happens.
        self.assertEqual(blocked.status_code, 429)

    def test_unexpected_backend_exception_releases_only_that_reservation(self):
        self._create_user("backend.error@example.com")
        client = self._client()

        with patch("backend.routers.auth.verify_password", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                client.post(
                    "/auth/login",
                    data={"username": "backend.error@example.com", "password": "irrelevant"},
                )

        # The reservation from the failed attempt above must have been
        # released -- the full budget (3) is still available afterward.
        for _ in range(3):
            response = client.post(
                "/auth/login",
                data={"username": "backend.error@example.com", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 401)
        blocked = client.post(
            "/auth/login",
            data={"username": "backend.error@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_once_limited_neither_real_nor_dummy_argon2_runs(self):
        self._create_user("no.argon2.after.limit@example.com")
        client = self._client()

        for _ in range(3):
            client.post(
                "/auth/login",
                data={"username": "no.argon2.after.limit@example.com", "password": "wrong"},
            )

        with patch("backend.routers.auth.verify_password") as mock_verify:
            blocked = client.post(
                "/auth/login",
                data={"username": "no.argon2.after.limit@example.com", "password": "wrong"},
            )

        self.assertEqual(blocked.status_code, 429)
        mock_verify.assert_not_called()

    def test_capacity_unavailable_returns_503_not_429(self):
        tiny_limiter = RateLimiter(clock=self.clock, max_tracked_keys=1)
        app, session_factory = _build_app(self.engine, tiny_limiter)
        client = TestClient(app, client=("192.0.2.1", 1))

        self._create_user("capacity.a@example.com")
        self._create_user("capacity.b@example.com")

        first = client.post(
            "/auth/login",
            data={"username": "capacity.a@example.com", "password": "wrong"},
        )
        # First attempt: 2 new keys (pair + ip) exactly fill capacity (1)...
        # actually needs room for 2 with only 1 slot -- capacity is
        # exhausted immediately.
        self.assertEqual(first.status_code, 503)
        self.assertNotIn("Retry-After", first.headers)
        self.assertEqual(
            first.json(), {"detail": "Service temporarily unavailable. Please try again shortly."}
        )

    def test_spoofed_x_forwarded_for_does_not_change_the_bucket(self):
        self._create_user("spoof.target@example.com")
        client = self._client(ip="198.51.100.50")

        for _ in range(3):
            client.post(
                "/auth/login",
                data={"username": "spoof.target@example.com", "password": "wrong"},
                headers={"X-Forwarded-For": "1.1.1.1"},
            )

        # Same real peer IP, different spoofed header each time -- still
        # blocked, proving the header was never consulted.
        blocked = client.post(
            "/auth/login",
            data={"username": "spoof.target@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_missing_peer_address_uses_shared_conservative_bucket(self):
        # Default TestClient (no client= override) reports "testclient" as
        # the peer host, which normalize_client_ip cannot parse -> UNKNOWN.
        default_client = TestClient(self.app)
        self._create_user("unknown.bucket@example.com")

        for _ in range(3):
            default_client.post(
                "/auth/login",
                data={"username": "unknown.bucket@example.com", "password": "wrong"},
            )
        blocked = default_client.post(
            "/auth/login",
            data={"username": "unknown.bucket@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_invalid_config_returns_generic_500(self):
        self._create_user("bad.config@example.com")
        client = self._client(ip="192.0.2.200")

        with patch.dict(os.environ, {"LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES": "not-a-number"}, clear=False):
            response = client.post(
                "/auth/login",
                data={"username": "bad.config@example.com", "password": "wrong"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Rate limiting is not correctly configured."})
        self.assertNotIn("not-a-number", response.text)


class RegisterRouteTests(_BaseRateLimitRouteTestCase):
    def _register(self, client, mail, password=VALID_PASSWORD):
        return client.post(
            "/auth/register",
            json={"name": "Synthetic User", "mail": mail, "password": password},
        )

    def test_registration_boundary_behavior(self):
        client = self._client(ip="203.0.113.20")

        for i in range(3):  # REGISTER_RATE_LIMIT_IP_MAX_ATTEMPTS=3
            response = self._register(client, f"reg.boundary{i}@example.com")
            self.assertEqual(response.status_code, 201)

        blocked = self._register(client, "reg.boundary.blocked@example.com")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

    def test_duplicate_attempts_also_consume_the_budget(self):
        client = self._client(ip="203.0.113.21")
        self._register(client, "reg.dup@example.com")
        self._register(client, "reg.dup@example.com")  # 409, still counted
        self._register(client, "reg.dup@example.com")  # 409, still counted

        blocked = self._register(client, "reg.dup.new@example.com")
        self.assertEqual(blocked.status_code, 429)

    def test_blocked_registration_hashes_no_password_and_writes_no_row(self):
        client = self._client(ip="203.0.113.22")
        for i in range(3):
            self._register(client, f"reg.blocked.fill{i}@example.com")

        before_count = self._count_users()

        with patch("backend.routers.auth.hash_password") as mock_hash:
            blocked = self._register(client, "reg.blocked.final@example.com")

        self.assertEqual(blocked.status_code, 429)
        mock_hash.assert_not_called()
        self.assertEqual(self._count_users(), before_count)

    def test_capacity_unavailable_returns_503(self):
        tiny_limiter = RateLimiter(clock=self.clock, max_tracked_keys=0)
        app, _ = _build_app(self.engine, tiny_limiter)
        client = TestClient(app, client=("192.0.2.5", 1))

        response = client.post(
            "/auth/register",
            json={"name": "X", "mail": "cap.register@example.com", "password": VALID_PASSWORD},
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Retry-After", response.headers)


class DependencyIsolationTests(unittest.TestCase):
    """Explicitly proves that two independently-built test apps (as every
    test class above constructs per-test) never share limiter state, even
    though get_rate_limiter() is a single production function -- because
    each app's dependency_overrides substitutes its own fresh instance.
    """

    def test_two_overridden_apps_do_not_share_state(self):
        tmp_dir = tempfile.mkdtemp(prefix="apply101_rate_limit_isolation_test_")
        try:
            db_path = os.path.join(tmp_dir, "synthetic.db")
            engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
            Base.metadata.create_all(bind=engine)
            session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

            session = session_factory()
            user = models.User(
                name="U", mail="isolation@example.com",
                password_hash=hash_password(VALID_PASSWORD), is_admin=False,
            )
            session.add(user)
            session.commit()
            session.close()

            with patch.dict(
                os.environ,
                {
                    "JWT_SECRET_KEY": "synthetic-isolation-secret-0123456789",
                    "LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES": "1",
                    "LOGIN_RATE_LIMIT_IP_MAX_FAILURES": "1",
                },
                clear=False,
            ):
                limiter_1 = RateLimiter(clock=_FakeClock())
                app_1, _ = _build_app(engine, limiter_1)
                client_1 = TestClient(app_1, client=("203.0.113.30", 1))
                r1 = client_1.post(
                    "/auth/login",
                    data={"username": "isolation@example.com", "password": "wrong"},
                )
                self.assertEqual(r1.status_code, 401)
                blocked_1 = client_1.post(
                    "/auth/login",
                    data={"username": "isolation@example.com", "password": "wrong"},
                )
                self.assertEqual(blocked_1.status_code, 429)

                # A second, independently-built app/limiter for the SAME
                # account/IP must start with a completely fresh budget.
                limiter_2 = RateLimiter(clock=_FakeClock())
                app_2, _ = _build_app(engine, limiter_2)
                client_2 = TestClient(app_2, client=("203.0.113.30", 1))
                r2 = client_2.post(
                    "/auth/login",
                    data={"username": "isolation@example.com", "password": "wrong"},
                )
                self.assertEqual(r2.status_code, 401)

            engine.dispose()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Part 3: correction regression tests -- reservation-identity release
# idempotency and immediate capacity reclamation (E3.1 review fix set).
# These deliberately inspect RateLimiter._counters directly to prove
# identity/memory invariants that a purely outcome-based test cannot
# distinguish from a coincidentally-correct implementation.
# ===========================================================================


class ReleaseIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.limiter = RateLimiter(clock=self.clock, max_tracked_keys=100)

    def _spec(self, space="s", key="k", max_count=5, window=60):
        return ReservationSpec(counter_space=space, key=key, max_count=max_count, window_seconds=window)

    def test_sequential_double_release_is_idempotent(self):
        spec = self._spec(max_count=3)
        r1 = self.limiter.try_reserve([spec])
        r2 = self.limiter.try_reserve([spec])
        self.assertEqual(r1.outcome, ReservationOutcome.GRANTED)
        self.assertEqual(r2.outcome, ReservationOutcome.GRANTED)

        self.limiter.release(r1.tokens)
        self.limiter.release(r1.tokens)  # same tokens, released a second time

        # Only r1's single slot was ever freed -- r2's is still retained,
        # so exactly 2 more reservations succeed before hitting max_count=3.
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_double_release_does_not_erase_another_retained_failure(self):
        spec = self._spec(max_count=2)
        success_reservation = self.limiter.try_reserve([spec])  # simulates a login that succeeds
        failed_reservation = self.limiter.try_reserve([spec])  # simulates a retained failed login
        self.assertEqual(success_reservation.outcome, ReservationOutcome.GRANTED)
        self.assertEqual(failed_reservation.outcome, ReservationOutcome.GRANTED)

        self.limiter.release(success_reservation.tokens)
        self.limiter.release(success_reservation.tokens)  # buggy double release

        # The failed reservation's exact identity must still be present.
        bucket = self.limiter._counters[spec.counter_space][spec.key]
        self.assertIn(failed_reservation.tokens[0].reservation_id, bucket.reservation_ids)
        self.assertEqual(len(bucket.reservation_ids), 1)

        # One more attempt is allowed (bringing total to max_count=2), then blocked.
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_concurrent_double_release_of_the_same_reservation_is_safe(self):
        spec = self._spec(max_count=5)
        r1 = self.limiter.try_reserve([spec])
        r2 = self.limiter.try_reserve([spec])
        self.assertEqual(r1.outcome, ReservationOutcome.GRANTED)
        self.assertEqual(r2.outcome, ReservationOutcome.GRANTED)

        threads = [
            threading.Thread(target=self.limiter.release, args=(r1.tokens,))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        bucket = self.limiter._counters[spec.counter_space][spec.key]
        self.assertNotIn(r1.tokens[0].reservation_id, bucket.reservation_ids)
        self.assertIn(r2.tokens[0].reservation_id, bucket.reservation_ids)
        self.assertEqual(len(bucket.reservation_ids), 1)

    def test_concurrent_release_of_distinct_reservations_cannot_corrupt_the_bucket(self):
        spec = self._spec(max_count=50)
        reservations = [self.limiter.try_reserve([spec]) for _ in range(50)]
        for r in reservations:
            self.assertEqual(r.outcome, ReservationOutcome.GRANTED)

        threads = [
            threading.Thread(target=self.limiter.release, args=(r.tokens,))
            for r in reservations
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 50 distinct reservations released concurrently -- the bucket
        # must be fully and correctly empty (and therefore removed), not
        # partially decremented or left in a corrupted state.
        self.assertNotIn(spec.key, self.limiter._counters.get(spec.counter_space, {}))

        # Room for a fresh full batch again proves nothing was lost.
        for _ in range(50):
            self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.GRANTED)
        self.assertEqual(self.limiter.try_reserve([spec]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

    def test_release_after_rollover_does_not_touch_the_newer_generation_bucket(self):
        spec = self._spec(max_count=1, window=60)
        stale = self.limiter.try_reserve([spec])
        self.assertEqual(stale.outcome, ReservationOutcome.GRANTED)

        self.clock.advance(60)
        fresh = self.limiter.try_reserve([spec])
        self.assertEqual(fresh.outcome, ReservationOutcome.GRANTED)

        bucket_before = self.limiter._counters[spec.counter_space][spec.key]
        ids_before = set(bucket_before.reservation_ids)

        self.limiter.release(stale.tokens)  # stale generation -- must be ignored

        bucket_after = self.limiter._counters[spec.counter_space][spec.key]
        self.assertEqual(bucket_after.generation, fresh.tokens[0].generation)
        self.assertEqual(set(bucket_after.reservation_ids), ids_before)
        self.assertIn(fresh.tokens[0].reservation_id, bucket_after.reservation_ids)

    def test_multi_key_release_touches_only_that_requests_ids_in_both_buckets(self):
        pair_spec = self._spec(space="login_pair", key="ip|acct", max_count=5, window=60)
        ip_spec = self._spec(space="login_ip", key="ip", max_count=5, window=60)

        request_a = self.limiter.try_reserve([pair_spec, ip_spec])
        request_b = self.limiter.try_reserve([pair_spec, ip_spec])
        self.assertEqual(request_a.outcome, ReservationOutcome.GRANTED)
        self.assertEqual(request_b.outcome, ReservationOutcome.GRANTED)

        self.limiter.release(request_a.tokens)

        pair_bucket = self.limiter._counters["login_pair"]["ip|acct"]
        ip_bucket = self.limiter._counters["login_ip"]["ip"]

        a_pair_token, a_ip_token = request_a.tokens
        b_pair_token, b_ip_token = request_b.tokens

        self.assertNotIn(a_pair_token.reservation_id, pair_bucket.reservation_ids)
        self.assertNotIn(a_ip_token.reservation_id, ip_bucket.reservation_ids)
        self.assertIn(b_pair_token.reservation_id, pair_bucket.reservation_ids)
        self.assertIn(b_ip_token.reservation_id, ip_bucket.reservation_ids)


class BucketReclamationTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()

    def test_bucket_with_no_remaining_ids_is_removed_immediately(self):
        limiter = RateLimiter(clock=self.clock, max_tracked_keys=100)
        spec = ReservationSpec("s", "solo", max_count=5, window_seconds=60)
        r = limiter.try_reserve([spec])
        self.assertEqual(r.outcome, ReservationOutcome.GRANTED)
        self.assertIn("solo", limiter._counters["s"])

        limiter.release(r.tokens)

        self.assertNotIn("solo", limiter._counters.get("s", {}))

    def test_bucket_with_another_retained_reservation_is_not_removed(self):
        limiter = RateLimiter(clock=self.clock, max_tracked_keys=100)
        spec = ReservationSpec("s", "shared", max_count=5, window_seconds=60)
        r1 = limiter.try_reserve([spec])
        r2 = limiter.try_reserve([spec])
        self.assertEqual(r1.outcome, ReservationOutcome.GRANTED)
        self.assertEqual(r2.outcome, ReservationOutcome.GRANTED)

        limiter.release(r1.tokens)

        self.assertIn("shared", limiter._counters["s"])
        bucket = limiter._counters["s"]["shared"]
        self.assertEqual(bucket.reservation_ids, {r2.tokens[0].reservation_id})

    def test_immediate_removal_frees_tracked_key_capacity_before_expiry(self):
        limiter = RateLimiter(clock=self.clock, max_tracked_keys=1)
        spec_a = ReservationSpec("s", "a", max_count=5, window_seconds=60)
        r_a = limiter.try_reserve([spec_a])
        self.assertEqual(r_a.outcome, ReservationOutcome.GRANTED)

        spec_b = ReservationSpec("s", "b", max_count=5, window_seconds=60)
        # At capacity (1/1) -- a brand-new key must fail, with NO time
        # having passed (proving expiry isn't what would free this).
        self.assertEqual(limiter.try_reserve([spec_b]).outcome, ReservationOutcome.CAPACITY_UNAVAILABLE)

        limiter.release(r_a.tokens)  # empties and removes "a" immediately

        # Room is available again, at the exact same clock time.
        self.assertEqual(limiter.try_reserve([spec_b]).outcome, ReservationOutcome.GRANTED)


class ReservationIdCountingTests(unittest.TestCase):
    def test_limit_checks_use_the_number_of_reservation_ids(self):
        clock = _FakeClock()
        limiter = RateLimiter(clock=clock, max_tracked_keys=100)
        spec = ReservationSpec("s", "k", max_count=4, window_seconds=60)

        results = [limiter.try_reserve([spec]) for _ in range(4)]
        for r in results:
            self.assertEqual(r.outcome, ReservationOutcome.GRANTED)

        bucket = limiter._counters["s"]["k"]
        self.assertEqual(len(bucket.reservation_ids), 4)
        ids = {r.tokens[0].reservation_id for r in results}
        self.assertEqual(len(ids), 4)  # all distinct

        self.assertEqual(limiter.try_reserve([spec]).outcome, ReservationOutcome.LIMIT_EXCEEDED)

        limiter.release(results[0].tokens)
        self.assertEqual(len(limiter._counters["s"]["k"].reservation_ids), 3)
        self.assertEqual(limiter.try_reserve([spec]).outcome, ReservationOutcome.GRANTED)


class MultiKeyAtomicityWithNewRepresentationTests(unittest.TestCase):
    def test_partial_block_creates_no_reservation_ids_anywhere(self):
        clock = _FakeClock()
        limiter = RateLimiter(clock=clock, max_tracked_keys=100)
        blocked_spec = ReservationSpec("a", "k1", max_count=1, window_seconds=60)
        fine_spec = ReservationSpec("b", "k2", max_count=100, window_seconds=60)

        limiter.try_reserve([blocked_spec])  # exhausts "a"/"k1" alone

        result = limiter.try_reserve([blocked_spec, fine_spec])
        self.assertEqual(result.outcome, ReservationOutcome.LIMIT_EXCEEDED)

        # fine_spec's key must never have been created at all.
        self.assertNotIn("k2", limiter._counters.get("b", {}))


class MemoryGrowthPreventionTests(unittest.TestCase):
    def test_full_ip_bucket_blocks_new_pair_keys_without_creating_them(self):
        clock = _FakeClock()
        limiter = RateLimiter(clock=clock, max_tracked_keys=1000)
        ip_key = "203.0.113.99"

        def specs_for(account_key):
            return [
                ReservationSpec("login_pair", f"{ip_key}|{account_key}", max_count=100, window_seconds=60),
                ReservationSpec("login_ip", ip_key, max_count=2, window_seconds=60),
            ]

        # Exhaust the IP-only bucket (max 2) using 2 distinct accounts --
        # this legitimately creates 2 pair keys along the way.
        for i in range(2):
            result = limiter.try_reserve(specs_for(f"account-{i}"))
            self.assertEqual(result.outcome, ReservationOutcome.GRANTED)

        pair_count_before = len(limiter._counters.get("login_pair", {}))
        self.assertEqual(pair_count_before, 2)

        # Spray 20 MORE distinct, never-before-seen accounts from the SAME
        # (now IP-exhausted) IP.
        for i in range(20):
            result = limiter.try_reserve(specs_for(f"new-account-{i}"))
            self.assertEqual(result.outcome, ReservationOutcome.LIMIT_EXCEEDED)

        # No new pair keys were created by any of the 20 blocked attempts.
        self.assertEqual(len(limiter._counters.get("login_pair", {})), pair_count_before)


class LoweringCapacityTests(unittest.TestCase):
    def test_lowering_cap_below_occupancy_is_safe(self):
        clock = _FakeClock()
        limiter = RateLimiter(clock=clock)  # max_tracked_keys=None -> reads env fresh

        with patch.dict(os.environ, {"RATE_LIMIT_MAX_TRACKED_KEYS": "5"}, clear=False):
            specs = [
                ReservationSpec("s", f"k{i}", max_count=5, window_seconds=60) for i in range(5)
            ]
            reservations = []
            for spec in specs:
                result = limiter.try_reserve([spec])
                self.assertEqual(result.outcome, ReservationOutcome.GRANTED)
                reservations.append(result)

            self.assertEqual(len(limiter._counters["s"]), 5)

            # Lower the cap below current occupancy.
            os.environ["RATE_LIMIT_MAX_TRACKED_KEYS"] = "3"

            # (a) Active entries are not removed merely by lowering the cap.
            self.assertEqual(len(limiter._counters["s"]), 5)
            for i in range(5):
                self.assertIn(f"k{i}", limiter._counters["s"])

            # (b) A brand-new key is safely blocked -- no crash, no corruption.
            new_spec = ReservationSpec("s", "brand-new", max_count=5, window_seconds=60)
            blocked = limiter.try_reserve([new_spec])
            self.assertEqual(blocked.outcome, ReservationOutcome.CAPACITY_UNAVAILABLE)
            self.assertEqual(len(limiter._counters["s"]), 5)
            self.assertNotIn("brand-new", limiter._counters["s"])

            # (c) Releasing enough existing keys (occupancy drops below the
            # new cap of 3) allows a new key again.
            for result in reservations[:3]:
                limiter.release(result.tokens)
            self.assertEqual(len(limiter._counters["s"]), 2)

            allowed_again = limiter.try_reserve([new_spec])
            self.assertEqual(allowed_again.outcome, ReservationOutcome.GRANTED)
            self.assertEqual(len(limiter._counters["s"]), 3)


class SuccessThenConfigFailureTests(_BaseRateLimitRouteTestCase):
    def test_success_then_config_failure_releases_only_that_reservation_once(self):
        self._create_user("config.failure.after.success@example.com")
        client = self._client()

        first_failure = client.post(
            "/auth/login",
            data={"username": "config.failure.after.success@example.com", "password": "wrong"},
        )
        self.assertEqual(first_failure.status_code, 401)

        # Correct credentials, but force create_access_token to fail via a
        # missing JWT_SECRET_KEY -- this happens AFTER the rate limiter's
        # own success-path release has already run for THIS request.
        with patch.dict(os.environ, {"JWT_SECRET_KEY": ""}, clear=False):
            response = client.post(
                "/auth/login",
                data={
                    "username": "config.failure.after.success@example.com",
                    "password": VALID_PASSWORD,
                },
            )
        self.assertEqual(response.status_code, 500)

        # The first failure must still be exactly-once counted (this
        # request's own reserve+release nets back to the same 1 retained
        # slot) -- exactly 2 more failed attempts are allowed (pair max=3)
        # before blocking.
        for _ in range(2):
            still_failing = client.post(
                "/auth/login",
                data={
                    "username": "config.failure.after.success@example.com",
                    "password": "wrong",
                },
            )
            self.assertEqual(still_failing.status_code, 401)

        blocked = client.post(
            "/auth/login",
            data={"username": "config.failure.after.success@example.com", "password": "wrong"},
        )
        self.assertEqual(blocked.status_code, 429)


class AllConfigVariablesValidationTests(unittest.TestCase):
    CONFIG_GETTERS = [
        ("LOGIN_RATE_LIMIT_PAIR_MAX_FAILURES", login_pair_max_failures),
        ("LOGIN_RATE_LIMIT_IP_MAX_FAILURES", login_ip_max_failures),
        ("LOGIN_RATE_LIMIT_WINDOW_SECONDS", login_window_seconds),
        ("REGISTER_RATE_LIMIT_IP_MAX_ATTEMPTS", register_ip_max_attempts),
        ("REGISTER_RATE_LIMIT_WINDOW_SECONDS", register_window_seconds),
        ("RATE_LIMIT_MAX_TRACKED_KEYS", default_max_tracked_keys),
    ]

    def test_non_integer_value_is_rejected_for_every_setting(self):
        for env_name, getter in self.CONFIG_GETTERS:
            with self.subTest(env_var=env_name, case="non-integer"):
                with patch.dict(os.environ, {env_name: "not-a-number"}, clear=False):
                    with self.assertRaises(RateLimitConfigError):
                        getter()

    def test_zero_value_is_rejected_for_every_setting(self):
        for env_name, getter in self.CONFIG_GETTERS:
            with self.subTest(env_var=env_name, case="zero"):
                with patch.dict(os.environ, {env_name: "0"}, clear=False):
                    with self.assertRaises(RateLimitConfigError):
                        getter()

    def test_negative_value_is_rejected_for_every_setting(self):
        for env_name, getter in self.CONFIG_GETTERS:
            with self.subTest(env_var=env_name, case="negative"):
                with patch.dict(os.environ, {env_name: "-5"}, clear=False):
                    with self.assertRaises(RateLimitConfigError):
                        getter()


if __name__ == "__main__":
    unittest.main()
