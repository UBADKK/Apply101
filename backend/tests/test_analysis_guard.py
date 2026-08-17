import math
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import Select, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.database import Base
from backend.app.models import AnalysisGuard
from backend.app.analysis_guard import (
    AcquireOutcome,
    AnalysisGuardConfig,
    AnalysisGuardConfigError,
    GUARD_SAFETY_MARGIN_SECONDS,
    PROFILE_OPERATION_TYPE,
    USER_OPERATION_TYPE,
    load_config,
    release_profile_analysis_guard,
    try_acquire_profile_analysis_guard,
)


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _config(**overrides) -> AnalysisGuardConfig:
    defaults = dict(
        openai_timeout_seconds=60,
        openai_max_retries=0,
        lease_ttl_seconds=300,
        profile_success_cooldown_seconds=300,
        profile_failure_cooldown_seconds=30,
        user_success_cooldown_seconds=60,
        user_failure_cooldown_seconds=30,
    )
    defaults.update(overrides)
    return AnalysisGuardConfig(**defaults)


class ConfigTests(unittest.TestCase):
    def test_defaults_match_approved_values(self):
        config = load_config()
        self.assertEqual(config.openai_timeout_seconds, 60)
        self.assertEqual(config.openai_max_retries, 0)
        self.assertEqual(config.lease_ttl_seconds, 300)
        self.assertEqual(config.profile_success_cooldown_seconds, 300)
        self.assertEqual(config.profile_failure_cooldown_seconds, 30)
        self.assertEqual(config.user_success_cooldown_seconds, 60)
        self.assertEqual(config.user_failure_cooldown_seconds, 30)

    def test_safety_margin_is_a_code_constant_not_an_env_var(self):
        self.assertEqual(GUARD_SAFETY_MARGIN_SECONDS, 30)
        with patch.dict(os.environ, {"PROFILE_ANALYSIS_GUARD_SAFETY_MARGIN_SECONDS": "999"}, clear=False):
            # Must have no effect -- there is no such environment variable.
            config = load_config()
            self.assertEqual(config.lease_ttl_seconds, 300)

    def test_nonpositive_and_invalid_values_rejected_for_positive_settings(self):
        positive_settings = [
            "PROFILE_ANALYSIS_OPENAI_TIMEOUT_SECONDS",
            "PROFILE_ANALYSIS_GUARD_LEASE_TTL_SECONDS",
            "PROFILE_ANALYSIS_PROFILE_SUCCESS_COOLDOWN_SECONDS",
            "PROFILE_ANALYSIS_PROFILE_FAILURE_COOLDOWN_SECONDS",
            "PROFILE_ANALYSIS_USER_SUCCESS_COOLDOWN_SECONDS",
            "PROFILE_ANALYSIS_USER_FAILURE_COOLDOWN_SECONDS",
        ]
        for env_name in positive_settings:
            for bad_value in ("0", "-1", "not-a-number"):
                with self.subTest(env_var=env_name, value=bad_value):
                    with patch.dict(os.environ, {env_name: bad_value}, clear=False):
                        with self.assertRaises(AnalysisGuardConfigError):
                            load_config()

    def test_max_retries_zero_is_accepted(self):
        with patch.dict(os.environ, {"PROFILE_ANALYSIS_OPENAI_MAX_RETRIES": "0"}, clear=False):
            config = load_config()
            self.assertEqual(config.openai_max_retries, 0)

    def test_max_retries_negative_is_rejected(self):
        with patch.dict(os.environ, {"PROFILE_ANALYSIS_OPENAI_MAX_RETRIES": "-1"}, clear=False):
            with self.assertRaises(AnalysisGuardConfigError):
                load_config()

    def test_max_retries_non_integer_is_rejected(self):
        with patch.dict(os.environ, {"PROFILE_ANALYSIS_OPENAI_MAX_RETRIES": "not-a-number"}, clear=False):
            with self.assertRaises(AnalysisGuardConfigError):
                load_config()

    def test_unsafe_timeout_retry_lease_relationship_is_rejected(self):
        # (max_retries+1)*timeout + max_retries*60 + 30 = 2*60 + 1*60 + 30 = 210
        # A lease of 100 is far too short for max_retries=1, timeout=60.
        with patch.dict(
            os.environ,
            {
                "PROFILE_ANALYSIS_OPENAI_TIMEOUT_SECONDS": "60",
                "PROFILE_ANALYSIS_OPENAI_MAX_RETRIES": "1",
                "PROFILE_ANALYSIS_GUARD_LEASE_TTL_SECONDS": "100",
            },
            clear=False,
        ):
            with self.assertRaises(AnalysisGuardConfigError):
                load_config()

    def test_safe_relationship_at_the_exact_boundary_is_accepted(self):
        # Exactly the minimum required: 2*60 + 1*60 + 30 = 210.
        with patch.dict(
            os.environ,
            {
                "PROFILE_ANALYSIS_OPENAI_TIMEOUT_SECONDS": "60",
                "PROFILE_ANALYSIS_OPENAI_MAX_RETRIES": "1",
                "PROFILE_ANALYSIS_GUARD_LEASE_TTL_SECONDS": "210",
            },
            clear=False,
        ):
            config = load_config()
            self.assertEqual(config.lease_ttl_seconds, 210)


class _BaseGuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_analysis_guard_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.clock = _FakeClock()
        self.config = _config()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _db(self):
        return self.session_factory()

    def _acquire(self, profile_id, user_id, config=None, clock=None):
        db = self._db()
        try:
            return try_acquire_profile_analysis_guard(
                db,
                profile_id=profile_id,
                owner_user_id=user_id,
                config=config or self.config,
                clock=clock or self.clock,
            )
        finally:
            db.close()

    def _release(self, profile_id, user_id, owner_token, succeeded, config=None, clock=None):
        db = self._db()
        try:
            return release_profile_analysis_guard(
                db,
                profile_id=profile_id,
                owner_user_id=user_id,
                owner_token=owner_token,
                succeeded=succeeded,
                config=config or self.config,
                clock=clock or self.clock,
            )
        finally:
            db.close()


class AcquireReleaseTests(_BaseGuardTestCase):
    def test_first_analysis_acquires_both_rows(self):
        result = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertIsNotNone(result.owner_token)

    def test_second_request_same_profile_is_already_in_progress(self):
        first = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)

        second = self._acquire(profile_id=1, user_id=99)
        self.assertEqual(second.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)
        self.assertIsNone(second.retry_after_seconds)

    def test_second_request_same_owner_different_profile_is_already_in_progress(self):
        # Same user, different profile -- user dimension must block this,
        # preventing bypass via profile rotation.
        first = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)

        second = self._acquire(profile_id=2, user_id=10)
        self.assertEqual(second.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

    def test_different_profiles_different_users_are_independent(self):
        first = self._acquire(profile_id=1, user_id=10)
        second = self._acquire(profile_id=2, user_id=20)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)
        self.assertEqual(second.outcome, AcquireOutcome.GRANTED)

    def test_no_partial_acquisition_when_profile_blocks(self):
        # Exhaust ONLY the profile dimension directly, then attempt a
        # fresh acquire sharing that profile but a brand-new user -- the
        # user row must never end up committed.
        self._acquire(profile_id=1, user_id=10)  # holds profile=1, user=10

        blocked = self._acquire(profile_id=1, user_id=77)
        self.assertEqual(blocked.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

        # Prove user=77's row was never committed: a fresh acquire for
        # user=77 alone (different, free profile) must succeed as if
        # nothing had ever touched user=77 before.
        fresh = self._acquire(profile_id=99, user_id=77)
        self.assertEqual(fresh.outcome, AcquireOutcome.GRANTED)

    def test_no_partial_acquisition_when_user_blocks(self):
        # Exhaust the user dimension via profile=1, user=10. Then attempt
        # a fresh profile with the SAME user -- profile dimension is free,
        # user dimension blocks; the profile row must never end up
        # committed.
        self._acquire(profile_id=1, user_id=10)

        blocked = self._acquire(profile_id=55, user_id=10)
        self.assertEqual(blocked.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

        fresh = self._acquire(profile_id=55, user_id=888)
        self.assertEqual(fresh.outcome, AcquireOutcome.GRANTED)

    def test_active_profile_guard_returns_already_in_progress(self):
        self._acquire(profile_id=1, user_id=10)
        result = self._acquire(profile_id=1, user_id=20)
        self.assertEqual(result.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

    def test_active_user_guard_returns_already_in_progress(self):
        self._acquire(profile_id=1, user_id=10)
        result = self._acquire(profile_id=2, user_id=10)
        self.assertEqual(result.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

    def test_profile_cooldown_returns_cooldown_active(self):
        first = self._acquire(profile_id=1, user_id=10)
        self._release(1, 10, first.owner_token, succeeded=True)  # profile cooldown=300

        result = self._acquire(profile_id=1, user_id=999)
        self.assertEqual(result.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result.retry_after_seconds, 300)

    def test_user_cooldown_returns_cooldown_active(self):
        first = self._acquire(profile_id=1, user_id=10)
        self._release(1, 10, first.owner_token, succeeded=True)  # user cooldown=60

        result = self._acquire(profile_id=2, user_id=10)
        self.assertEqual(result.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result.retry_after_seconds, 60)

    def test_multiple_cooldowns_return_the_maximum_required_retry_after(self):
        # Same profile+user pair: profile cooldown (300) > user cooldown (60)
        # after a successful release -- both block, response must reflect
        # the LARGER (more restrictive) remaining wait.
        first = self._acquire(profile_id=1, user_id=10)
        self._release(1, 10, first.owner_token, succeeded=True)

        result = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(result.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result.retry_after_seconds, 300)  # max(300, 60)

    def test_retry_after_ceiling_and_minimum_one(self):
        config = _config(profile_success_cooldown_seconds=5)
        first = self._acquire(profile_id=1, user_id=10, config=config)
        self._release(1, 10, first.owner_token, succeeded=True, config=config)

        self.clock.advance(4.3)  # 0.7s remaining -> ceil -> 1
        result = self._acquire(profile_id=1, user_id=999, config=config)
        self.assertEqual(result.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result.retry_after_seconds, 1)

    def test_cooldown_expiry_permits_a_new_acquisition(self):
        config = _config(profile_success_cooldown_seconds=10, user_success_cooldown_seconds=10)
        first = self._acquire(profile_id=1, user_id=10, config=config)
        self._release(1, 10, first.owner_token, succeeded=True, config=config)

        blocked = self._acquire(profile_id=1, user_id=10, config=config)
        self.assertEqual(blocked.outcome, AcquireOutcome.COOLDOWN_ACTIVE)

        self.clock.advance(10)
        allowed = self._acquire(profile_id=1, user_id=10, config=config)
        self.assertEqual(allowed.outcome, AcquireOutcome.GRANTED)

    def test_stale_lease_takeover(self):
        config = _config(lease_ttl_seconds=60)
        first = self._acquire(profile_id=1, user_id=10, config=config)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)

        still_active = self._acquire(profile_id=1, user_id=20, config=config)
        self.assertEqual(still_active.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

        self.clock.advance(61)  # past the lease TTL -- crashed-worker simulation
        takeover = self._acquire(profile_id=1, user_id=20, config=config)
        self.assertEqual(takeover.outcome, AcquireOutcome.GRANTED)
        self.assertNotEqual(takeover.owner_token, first.owner_token)

    def test_old_owner_cannot_release_a_newer_owners_lease(self):
        config = _config(lease_ttl_seconds=60)
        first = self._acquire(profile_id=1, user_id=10, config=config)
        self.clock.advance(61)
        second = self._acquire(profile_id=1, user_id=20, config=config)
        self.assertEqual(second.outcome, AcquireOutcome.GRANTED)

        # The stale first owner releases -- must be a no-op, not clearing
        # the second owner's active lease.
        self._release(1, 10, first.owner_token, succeeded=True, config=config)

        still_blocked = self._acquire(profile_id=1, user_id=30, config=config)
        self.assertEqual(still_blocked.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

    def test_release_is_idempotent(self):
        first = self._acquire(profile_id=1, user_id=10)
        self._release(1, 10, first.owner_token, succeeded=True)
        self._release(1, 10, first.owner_token, succeeded=True)  # second call, same token

        # No crash, and a fresh acquire attempt still correctly reflects
        # exactly one release's cooldown (not a double-applied effect).
        result = self._acquire(profile_id=1, user_id=999)
        self.assertEqual(result.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result.retry_after_seconds, 300)

    def test_success_and_failure_cooldowns_differ_correctly_for_profile_and_user(self):
        config = _config(
            profile_success_cooldown_seconds=300,
            profile_failure_cooldown_seconds=30,
            user_success_cooldown_seconds=60,
            user_failure_cooldown_seconds=15,
        )
        first = self._acquire(profile_id=1, user_id=10, config=config)
        self._release(1, 10, first.owner_token, succeeded=False, config=config)

        result = self._acquire(profile_id=1, user_id=999, config=config)
        self.assertEqual(result.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result.retry_after_seconds, 30)  # profile_failure, not success

        self.clock.advance(30)
        second = self._acquire(profile_id=1, user_id=10, config=config)
        self.assertEqual(second.outcome, AcquireOutcome.GRANTED)
        self._release(1, 10, second.owner_token, succeeded=False, config=config)

        result2 = self._acquire(profile_id=2, user_id=10, config=config)
        self.assertEqual(result2.outcome, AcquireOutcome.COOLDOWN_ACTIVE)
        self.assertEqual(result2.retry_after_seconds, 15)  # user_failure


class BackendUnavailableTests(_BaseGuardTestCase):
    def test_acquire_query_failure_returns_backend_unavailable(self):
        db = self._db()
        try:
            with patch.object(Session, "execute", side_effect=SQLAlchemyError("simulated DB failure")):
                result = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, config=self.config, clock=self.clock,
                )
            self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        finally:
            db.close()

    def test_acquire_commit_failure_returns_backend_unavailable(self):
        db = self._db()
        try:
            with patch.object(Session, "commit", side_effect=SQLAlchemyError("simulated commit failure")):
                result = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, config=self.config, clock=self.clock,
                )
            self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        finally:
            db.close()

        # No guard state was left behind by the failed commit.
        fresh = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(fresh.outcome, AcquireOutcome.GRANTED)

    def test_non_engine_bind_raises_config_error(self):
        db = self._db()
        try:
            connection = self.engine.connect()
            try:
                bad_session = Session(bind=connection)
                with self.assertRaises(AnalysisGuardConfigError):
                    try_acquire_profile_analysis_guard(
                        bad_session, profile_id=1, owner_user_id=10,
                        config=self.config, clock=self.clock,
                    )
            finally:
                connection.close()
        finally:
            db.close()

    def test_release_failure_rolls_back_both_rows_and_returns_false(self):
        first = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)

        db = self._db()
        try:
            with patch.object(Session, "commit", side_effect=SQLAlchemyError("simulated release failure")):
                released = release_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, owner_token=first.owner_token,
                    succeeded=True, config=self.config, clock=self.clock,
                )
            self.assertFalse(released)
        finally:
            db.close()

        # The guard is still held by the ORIGINAL owner -- release truly
        # rolled back, it did not partially apply.
        still_active = self._acquire(profile_id=1, user_id=999)
        self.assertEqual(still_active.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

        # And the original owner can still release normally afterward.
        released_for_real = self._release(1, 10, first.owner_token, succeeded=True)
        self.assertTrue(released_for_real)

    def test_non_sqlalchemy_exception_during_acquisition_query_fails_closed(self):
        db = self._db()
        try:
            with patch.object(Session, "execute", side_effect=ValueError("boom")):
                result = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, config=self.config, clock=self.clock,
                )
            self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        finally:
            db.close()

    def test_non_sqlalchemy_exception_during_acquisition_commit_fails_closed(self):
        db = self._db()
        try:
            with patch.object(Session, "commit", side_effect=ValueError("boom")):
                result = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, config=self.config, clock=self.clock,
                )
            self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)
        finally:
            db.close()

        # No guard state was left behind by the failed commit.
        fresh = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(fresh.outcome, AcquireOutcome.GRANTED)

    def test_non_sqlalchemy_exception_during_release_is_contained(self):
        first = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)

        db = self._db()
        try:
            with patch.object(Session, "commit", side_effect=ValueError("boom")):
                released = release_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, owner_token=first.owner_token,
                    succeeded=True, config=self.config, clock=self.clock,
                )
            self.assertFalse(released)
        finally:
            db.close()

        # No exception propagated (this line is only reached if the ValueError
        # was truly contained), and the original owner's lease is unaffected.
        still_active = self._acquire(profile_id=1, user_id=999)
        self.assertEqual(still_active.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)


class MalformedStateAndClassificationTests(_BaseGuardTestCase):
    def _insert_row_directly(self, operation_type, resource_id, owner_token, lock_expires_at, cooldown_until):
        # Bypasses try_acquire_profile_analysis_guard entirely -- writes a
        # row exactly as specified, including states the guard's own code
        # would never itself produce (e.g. owner_token set with a null
        # expiry), to prove the acquire predicate self-heals such rows
        # rather than trusting the module to only ever see well-formed data.
        session = self._db()
        try:
            session.add(AnalysisGuard(
                operation_type=operation_type,
                resource_id=resource_id,
                owner_token=owner_token,
                lock_expires_at=lock_expires_at,
                cooldown_until=cooldown_until,
            ))
            session.commit()
        finally:
            session.close()

    def test_null_expiry_malformed_owner_row_can_be_taken_over(self):
        self._insert_row_directly(
            PROFILE_OPERATION_TYPE, 1,
            owner_token="stale-malformed-token", lock_expires_at=None, cooldown_until=None,
        )

        result = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(result.outcome, AcquireOutcome.GRANTED)
        self.assertNotEqual(result.owner_token, "stale-malformed-token")

    def test_null_expiry_malformed_row_does_not_break_two_resource_atomicity(self):
        # Profile dimension is malformed (recoverable); user dimension is
        # genuinely, actively held by someone else. The malformed profile
        # row must NOT be won in isolation -- acquisition must still be
        # all-or-nothing.
        self._insert_row_directly(
            PROFILE_OPERATION_TYPE, 1,
            owner_token="stale-malformed-token", lock_expires_at=None, cooldown_until=None,
        )
        held = self._acquire(profile_id=99, user_id=10)
        self.assertEqual(held.outcome, AcquireOutcome.GRANTED)  # holds user_id=10

        blocked = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(blocked.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

        # The malformed profile row must still be exactly as it was (not
        # partially taken over then rolled back into some other state) --
        # a fresh acquisition attempt against ONLY that profile dimension
        # (paired with a free user) must still succeed via self-heal.
        recovered = self._acquire(profile_id=1, user_id=777)
        self.assertEqual(recovered.outcome, AcquireOutcome.GRANTED)

    def test_live_future_expiry_owner_remains_protected(self):
        config = _config(lease_ttl_seconds=3600)
        first = self._acquire(profile_id=1, user_id=10, config=config)
        self.assertEqual(first.outcome, AcquireOutcome.GRANTED)

        # Far from expiry (not malformed -- a real, live, future expiry).
        still_blocked = self._acquire(profile_id=1, user_id=999, config=config)
        self.assertEqual(still_blocked.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)

    def test_mixed_active_and_cooldown_blockers_classify_as_already_in_progress(self):
        # Profile dimension: actively held by someone else (live, unexpired
        # lease). User dimension: separately in cooldown (different
        # resource, reached via its own prior release). A request that
        # collides with BOTH must return 409-equivalent (ALREADY_IN_PROGRESS),
        # never 429 (COOLDOWN_ACTIVE), even though a cooldown blocker also
        # exists in the same attempt.
        active_holder = self._acquire(profile_id=1, user_id=500)
        self.assertEqual(active_holder.outcome, AcquireOutcome.GRANTED)

        cooldown_owner = self._acquire(profile_id=2, user_id=10)
        self.assertEqual(cooldown_owner.outcome, AcquireOutcome.GRANTED)
        self._release(2, 10, cooldown_owner.owner_token, succeeded=True)  # user_id=10 now cooling down

        mixed = self._acquire(profile_id=1, user_id=10)
        self.assertEqual(mixed.outcome, AcquireOutcome.ALREADY_IN_PROGRESS)
        self.assertIsNone(mixed.retry_after_seconds)

    def test_incoherent_blocker_state_fails_closed_as_backend_unavailable(self):
        # Simulates a resource that appears fully free in the post-upsert
        # read yet was not won by us -- only reachable via a genuine
        # concurrent-acquirer race in production. Forced deterministically
        # here by intercepting only the verification SELECT (the UPSERTs
        # still hit the real database normally).
        class _FakeRow:
            def __init__(self, operation_type, resource_id):
                self.operation_type = operation_type
                self.resource_id = resource_id
                self.owner_token = None
                self.lock_expires_at = None
                self.cooldown_until = None

        class _FakeScalars:
            def __init__(self, items):
                self._items = items

            def all(self):
                return self._items

        class _FakeResult:
            def __init__(self, items):
                self._items = items

            def scalars(self):
                return _FakeScalars(self._items)

        fake_rows = [_FakeRow(PROFILE_OPERATION_TYPE, 1), _FakeRow(USER_OPERATION_TYPE, 10)]
        original_execute = Session.execute

        def fake_execute(self, stmt, *args, **kwargs):
            if isinstance(stmt, Select):
                return _FakeResult(fake_rows)
            return original_execute(self, stmt, *args, **kwargs)

        with patch.object(Session, "execute", fake_execute):
            result = self._acquire(profile_id=1, user_id=10)

        self.assertEqual(result.outcome, AcquireOutcome.BACKEND_UNAVAILABLE)


class ConcurrencyTests(unittest.TestCase):
    """Real threads, real temp-file SQLite, separate SQLAlchemy sessions/
    connections per thread -- not mocked sequential calls. Coordinated via
    threading.Event, never sleeps.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_analysis_guard_concurrency_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.config = _config()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_two_concurrent_first_time_requests_same_profile_only_one_granted(self):
        both_ready = threading.Barrier(2, timeout=10)
        results = {}

        def attempt(name, user_id):
            db = self.session_factory()
            try:
                both_ready.wait()  # maximize real overlap without a sleep
                results[name] = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=user_id, config=self.config,
                ).outcome
            finally:
                db.close()

        t1 = threading.Thread(target=attempt, args=("a", 10))
        t2 = threading.Thread(target=attempt, args=("b", 20))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        outcomes = sorted([results["a"].value, results["b"].value])
        self.assertEqual(outcomes, sorted([AcquireOutcome.GRANTED.value, AcquireOutcome.ALREADY_IN_PROGRESS.value]))

    def test_owner_and_admin_style_concurrent_requests_same_profile_only_one_granted(self):
        # Simulates the owner's own request and an admin's request for the
        # SAME profile -- both would carry the SAME profile.user_id as the
        # owner-user dimension in the real route (see profiles.py: the
        # guard is always keyed by profile.user_id, never the caller's own
        # id), so both threads use the identical (profile_id, owner_user_id)
        # pair here.
        both_ready = threading.Barrier(2, timeout=10)
        results = {}

        def attempt(name):
            db = self.session_factory()
            try:
                both_ready.wait()
                results[name] = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=10, config=self.config,
                ).outcome
            finally:
                db.close()

        t1 = threading.Thread(target=attempt, args=("owner",))
        t2 = threading.Thread(target=attempt, args=("admin",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        outcomes = sorted([results["owner"].value, results["admin"].value])
        self.assertEqual(outcomes, sorted([AcquireOutcome.GRANTED.value, AcquireOutcome.ALREADY_IN_PROGRESS.value]))

    def test_two_different_profiles_same_user_cannot_concurrently_acquire(self):
        both_ready = threading.Barrier(2, timeout=10)
        results = {}

        def attempt(name, profile_id):
            db = self.session_factory()
            try:
                both_ready.wait()
                results[name] = try_acquire_profile_analysis_guard(
                    db, profile_id=profile_id, owner_user_id=10, config=self.config,
                ).outcome
            finally:
                db.close()

        t1 = threading.Thread(target=attempt, args=("p1", 1))
        t2 = threading.Thread(target=attempt, args=("p2", 2))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        outcomes = sorted([results["p1"].value, results["p2"].value])
        self.assertEqual(outcomes, sorted([AcquireOutcome.GRANTED.value, AcquireOutcome.ALREADY_IN_PROGRESS.value]))

    def test_different_users_different_profiles_both_succeed_concurrently(self):
        both_ready = threading.Barrier(2, timeout=10)
        results = {}

        def attempt(name, profile_id, user_id):
            db = self.session_factory()
            try:
                both_ready.wait()
                results[name] = try_acquire_profile_analysis_guard(
                    db, profile_id=profile_id, owner_user_id=user_id, config=self.config,
                ).outcome
            finally:
                db.close()

        t1 = threading.Thread(target=attempt, args=("a", 1, 10))
        t2 = threading.Thread(target=attempt, args=("b", 2, 20))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(results["a"], AcquireOutcome.GRANTED)
        self.assertEqual(results["b"], AcquireOutcome.GRANTED)

    def test_fifty_concurrent_attempts_exactly_one_granted(self):
        thread_count = 50
        start_barrier = threading.Barrier(thread_count, timeout=10)
        outcomes = []
        lock = threading.Lock()

        def attempt(user_id):
            db = self.session_factory()
            try:
                start_barrier.wait()
                outcome = try_acquire_profile_analysis_guard(
                    db, profile_id=1, owner_user_id=user_id, config=self.config,
                ).outcome
                with lock:
                    outcomes.append(outcome)
            finally:
                db.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(outcomes), thread_count)
        granted = [o for o in outcomes if o is AcquireOutcome.GRANTED]
        blocked = [o for o in outcomes if o is AcquireOutcome.ALREADY_IN_PROGRESS]
        self.assertEqual(len(granted), 1)
        self.assertEqual(len(blocked), thread_count - 1)


if __name__ == "__main__":
    unittest.main()
