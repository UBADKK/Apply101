import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app import models
from backend.app.database import Base
from backend.app.security import hash_password
from backend.scripts import set_admin


VALID_PASSWORD = "a-valid-synthetic-password"


class _BaseAdminBootstrapTestCase(unittest.TestCase):
    """Shared setup: a throwaway, fully synthetic, FILE-BASED SQLite
    database (never apply101.db). The file is created up front (via
    Base.metadata.create_all) so run()'s "database must already exist"
    guard passes naturally for every test in this base class -- the
    guard itself is exercised separately in MissingDatabaseFileTests.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_admin_bootstrap_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_user(self, mail, password=VALID_PASSWORD, is_admin=False, name="Synthetic User"):
        session = self.session_factory()
        try:
            user = models.User(
                name=name,
                mail=mail,
                password_hash=hash_password(password) if password else None,
                is_admin=is_admin,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _get_user_row(self, user_id):
        session = self.session_factory()
        try:
            return session.query(models.User).filter(models.User.user_id == user_id).first()
        finally:
            session.close()

    def _snapshot_all_users(self):
        session = self.session_factory()
        try:
            rows = session.query(models.User).order_by(models.User.user_id).all()
            return [
                (
                    row.user_id, row.name, row.mail, row.skills,
                    row.experience_years, row.major, row.master, row.phd,
                    row.abitur, row.password_hash, row.is_admin,
                )
                for row in rows
            ]
        finally:
            session.close()


class PromotionTests(_BaseAdminBootstrapTestCase):
    def test_normal_login_capable_user_is_promoted(self):
        user_id = self._create_user("promote.me@example.com")

        exit_code = set_admin.run("promote.me@example.com", engine=self.engine)

        self.assertEqual(exit_code, 0)
        row = self._get_user_row(user_id)
        self.assertTrue(row.is_admin)

    def test_only_is_admin_field_changes(self):
        user_id = self._create_user(
            "fields.unchanged@example.com", name="Original Name"
        )
        before = self._get_user_row(user_id)
        before_snapshot = (
            before.name, before.mail, before.skills, before.experience_years,
            before.major, before.master, before.phd, before.abitur,
            before.password_hash,
        )
        self.assertFalse(before.is_admin)

        exit_code = set_admin.run("fields.unchanged@example.com", engine=self.engine)

        self.assertEqual(exit_code, 0)
        after = self._get_user_row(user_id)
        after_snapshot = (
            after.name, after.mail, after.skills, after.experience_years,
            after.major, after.master, after.phd, after.abitur,
            after.password_hash,
        )
        self.assertEqual(before_snapshot, after_snapshot)
        self.assertTrue(after.is_admin)


class IdempotentTests(_BaseAdminBootstrapTestCase):
    def test_already_admin_is_a_no_op_and_never_commits(self):
        user_id = self._create_user("already.admin@example.com", is_admin=True)

        with patch.object(Session, "commit") as mock_commit:
            exit_code = set_admin.run("already.admin@example.com", engine=self.engine)

        self.assertEqual(exit_code, 0)
        mock_commit.assert_not_called()
        row = self._get_user_row(user_id)
        self.assertTrue(row.is_admin)


class FailureTests(_BaseAdminBootstrapTestCase):
    def test_nonexistent_email_fails_without_mutation(self):
        self._create_user("someone.else@example.com")
        before = self._snapshot_all_users()

        exit_code = set_admin.run("nobody.here@example.com", engine=self.engine)

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(before, self._snapshot_all_users())

    def test_passwordless_legacy_user_is_refused(self):
        user_id = self._create_user("legacy.nopassword@example.com", password=None)

        exit_code = set_admin.run("legacy.nopassword@example.com", engine=self.engine)

        self.assertNotEqual(exit_code, 0)
        row = self._get_user_row(user_id)
        self.assertFalse(row.is_admin)

    def test_invalid_email_fails_without_touching_db(self):
        self._create_user("real.user@example.com")
        before = self._snapshot_all_users()

        exit_code = set_admin.run("not-an-email", engine=self.engine)

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(before, self._snapshot_all_users())


class CommitFailureTests(_BaseAdminBootstrapTestCase):
    def test_commit_failure_rolls_back_and_does_not_leak_exception_text(self):
        user_id = self._create_user("commit.failure@example.com")

        captured = io.StringIO()
        with patch.object(
            Session, "commit", side_effect=RuntimeError("forced commit failure")
        ):
            with contextlib.redirect_stdout(captured):
                exit_code = set_admin.run("commit.failure@example.com", engine=self.engine)

        self.assertNotEqual(exit_code, 0)
        self.assertNotIn("forced commit failure", captured.getvalue())

        # Fresh session/query -- confirms the failed attempt was never
        # actually persisted, not just that the in-memory object rolled back.
        row = self._get_user_row(user_id)
        self.assertFalse(row.is_admin)


class MissingDatabaseFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_admin_bootstrap_missing_db_test_")
        self.missing_db_path = os.path.join(self.tmp_dir, "does_not_exist.db")
        # Deliberately never created/connected -- create_engine() itself is
        # lazy and never touches the filesystem on its own.
        self.engine = create_engine(
            f"sqlite:///{self.missing_db_path}",
            connect_args={"check_same_thread": False},
        )

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_missing_database_file_is_refused_before_any_mutation(self):
        self.assertFalse(os.path.exists(self.missing_db_path))

        exit_code = set_admin.run("anyone@example.com", engine=self.engine)

        self.assertNotEqual(exit_code, 0)
        self.assertFalse(
            os.path.exists(self.missing_db_path),
            "run() must never create a new database file as a side effect",
        )


class TargetOutputTests(_BaseAdminBootstrapTestCase):
    def test_resolved_target_path_is_printed_with_no_secrets(self):
        user_id = self._create_user("target.output@example.com")

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            set_admin.run("target.output@example.com", engine=self.engine)

        output = captured.getvalue()
        self.assertIn(os.path.abspath(self.db_path), output)
        self.assertNotIn(VALID_PASSWORD, output)

        row = self._get_user_row(user_id)
        self.assertNotIn(row.password_hash, output)


class MainArgumentParsingTests(unittest.TestCase):
    """Exercises main()/argparse only -- run() is mocked out so this never
    touches any real or synthetic engine, per the requirement to keep CLI
    parsing testable without pointing anything at apply101.db.
    """

    def test_main_parses_email_and_delegates_to_run(self):
        with patch("backend.scripts.set_admin.run", return_value=0) as mock_run:
            exit_code = set_admin.main(["--email", "registered@example.com"])

        self.assertEqual(exit_code, 0)
        mock_run.assert_called_once_with("registered@example.com")

    def test_main_requires_email_argument(self):
        with self.assertRaises(SystemExit):
            set_admin.main([])


if __name__ == "__main__":
    unittest.main()
