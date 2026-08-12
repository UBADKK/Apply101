import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base
from backend.app.auth_dependencies import (
    get_current_admin,
    get_owned_profile,
    require_user_access,
)


class GetCurrentAdminTests(unittest.TestCase):
    def test_admin_is_returned(self):
        admin = SimpleNamespace(user_id=1, is_admin=True)
        self.assertIs(get_current_admin(current_user=admin), admin)

    def test_normal_user_is_rejected_with_403(self):
        normal_user = SimpleNamespace(user_id=1, is_admin=False)
        with self.assertRaises(HTTPException) as ctx:
            get_current_admin(current_user=normal_user)
        self.assertEqual(ctx.exception.status_code, 403)


class RequireUserAccessTests(unittest.TestCase):
    def test_own_user_id_is_allowed(self):
        user = SimpleNamespace(user_id=5, is_admin=False)
        self.assertIs(require_user_access(user_id=5, current_user=user), user)

    def test_admin_accessing_another_user_id_is_allowed(self):
        admin = SimpleNamespace(user_id=1, is_admin=True)
        self.assertIs(
            require_user_access(user_id=999, current_user=admin), admin
        )

    def test_normal_user_accessing_another_user_id_is_404(self):
        user = SimpleNamespace(user_id=5, is_admin=False)
        with self.assertRaises(HTTPException) as ctx:
            require_user_access(user_id=6, current_user=user)
        self.assertEqual(ctx.exception.status_code, 404)
        # Must not confirm user_id=6 exists but belongs to someone else.
        self.assertNotIn("belongs", ctx.exception.detail.lower())
        self.assertNotIn("6", ctx.exception.detail)


class GetOwnedProfileTests(unittest.TestCase):
    """Uses a real temporary SQLite database (not apply101.db) so the
    ownership query is exercised against actual SQLAlchemy/SQLite semantics
    rather than a mocked query chain. current_user is still a lightweight
    stand-in, since get_owned_profile never queries the users table.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_authz_dep_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)

        session_factory = sessionmaker(bind=self.engine)
        self.db = session_factory()

        user_a = models.User(
            name="Synthetic User A", mail="synthetic.a@example.com",
            password_hash=None, is_admin=False,
        )
        user_b = models.User(
            name="Synthetic User B", mail="synthetic.b@example.com",
            password_hash=None, is_admin=False,
        )
        self.db.add_all([user_a, user_b])
        self.db.commit()
        self.db.refresh(user_a)
        self.db.refresh(user_b)
        self.user_a_id = user_a.user_id
        self.user_b_id = user_b.user_id

        profile_a = models.CandidateProfile(
            user_id=self.user_a_id, self_description="Profile belonging to A"
        )
        profile_b = models.CandidateProfile(
            user_id=self.user_b_id, self_description="Profile belonging to B"
        )
        self.db.add_all([profile_a, profile_b])
        self.db.commit()
        self.db.refresh(profile_a)
        self.db.refresh(profile_b)
        self.profile_a_id = profile_a.profile_id
        self.profile_b_id = profile_b.profile_id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _current_user(self, user_id, is_admin=False):
        return SimpleNamespace(user_id=user_id, is_admin=is_admin)

    def test_owner_and_own_profile_returns_the_profile(self):
        profile = get_owned_profile(
            user_id=self.user_a_id,
            profile_id=self.profile_a_id,
            db=self.db,
            current_user=self._current_user(self.user_a_id),
        )
        self.assertEqual(profile.profile_id, self.profile_a_id)

    def test_attack_1_another_users_user_id_and_profile_id_is_404(self):
        # /users/B/profiles/B_PROFILE requested by A
        with self.assertRaises(HTTPException) as ctx:
            get_owned_profile(
                user_id=self.user_b_id,
                profile_id=self.profile_b_id,
                db=self.db,
                current_user=self._current_user(self.user_a_id),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_attack_2_own_user_id_but_another_users_profile_id_is_404(self):
        # /users/A/profiles/B_PROFILE requested by A
        with self.assertRaises(HTTPException) as ctx:
            get_owned_profile(
                user_id=self.user_a_id,
                profile_id=self.profile_b_id,
                db=self.db,
                current_user=self._current_user(self.user_a_id),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_admin_with_correct_foreign_user_id_and_profile_id_succeeds(self):
        profile = get_owned_profile(
            user_id=self.user_b_id,
            profile_id=self.profile_b_id,
            db=self.db,
            current_user=self._current_user(self.user_a_id, is_admin=True),
        )
        self.assertEqual(profile.profile_id, self.profile_b_id)

    def test_owner_with_nonexistent_profile_id_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            get_owned_profile(
                user_id=self.user_a_id,
                profile_id=999999,
                db=self.db,
                current_user=self._current_user(self.user_a_id),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_admin_with_mismatched_user_id_and_profile_id_pair_is_404(self):
        # Admin requests /users/B/profiles/A_PROFILE -- A_PROFILE belongs to
        # A, not B, so even an admin must not get it through this path.
        with self.assertRaises(HTTPException) as ctx:
            get_owned_profile(
                user_id=self.user_b_id,
                profile_id=self.profile_a_id,
                db=self.db,
                current_user=self._current_user(self.user_a_id, is_admin=True),
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
