"""backend.scripts.backup_data on throwaway data (never apply101.db or
uploads/)."""

import contextlib
import io
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.app import config
from backend.scripts import backup_data


def _now(second):
    return datetime(2026, 11, 2, 4, 0, second, tzinfo=timezone.utc)


class BackupDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apply101_backup_test_"))
        self.db = self.tmp / "apply101-test.db"
        self.uploads = self.tmp / "uploads"
        self.uploads.mkdir()
        (self.uploads / "user_1_profile_1_cv.pdf").write_bytes(b"%PDF-original")
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE notes (value TEXT)")
            conn.execute("INSERT INTO notes VALUES ('original')")
            conn.commit()
        self.env = {
            "APPLY101_DATABASE_URL": f"sqlite:///{self.db}",
            "APPLY101_UPLOAD_DIR": str(self.uploads),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def notes(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return [row[0] for row in conn.execute("SELECT value FROM notes")]

    def run_main(self, argv, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup_data.main(argv, environ=self.env if env is None else env)
        return code, out.getvalue()

    def test_backup_and_restore_round_trip(self):
        target = backup_data.backup(self.env, keep=7, now=_now(0))
        self.assertEqual(target, self.tmp / "backups" / "apply101-20261102T040000Z")
        # Data changes after the backup.
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE notes SET value = 'changed'")
            conn.commit()
        (self.uploads / "user_1_profile_1_cv.pdf").write_bytes(b"%PDF-changed")
        (self.uploads / "user_2_profile_2_cv.pdf").write_bytes(b"%PDF-new")

        result = backup_data.restore(self.env, target.name, now=_now(5))
        self.assertEqual(self.notes(), ["original"])
        self.assertEqual((self.uploads / "user_1_profile_1_cv.pdf").read_bytes(), b"%PDF-original")
        self.assertFalse((self.uploads / "user_2_profile_2_cv.pdf").exists())
        # The state before the restore is kept twice: a backup and the old dir.
        moved = Path(result["previous_uploads_moved_to"])
        self.assertEqual((moved / "user_2_profile_2_cv.pdf").read_bytes(), b"%PDF-new")
        safety = Path(result["backup_of_previous_state"])
        with contextlib.closing(sqlite3.connect(safety / "database.db")) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT value FROM notes")], ["changed"])

    def test_keeps_only_the_newest_backups(self):
        for second in range(4):
            backup_data.backup(self.env, keep=2, now=_now(second))
        self.assertEqual(backup_data.list_backups(self.tmp / "backups"),
                         ["apply101-20261102T040002Z", "apply101-20261102T040003Z"])

    def test_cli_needs_yes_and_lists(self):
        code, out = self.run_main(["backup"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertFalse((self.tmp / "backups").exists())
        self.assertEqual(self.run_main(["backup", "--yes"])[0], 0)
        code, out = self.run_main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("apply101-", out)

    def test_refuses_defaults_and_real_data(self):
        cases = [
            {},
            {"APPLY101_DATABASE_URL": f"sqlite:///{self.db}"},
            {"APPLY101_DATABASE_URL": f"sqlite:///{config.REPO_ROOT / 'apply101.db'}", "APPLY101_UPLOAD_DIR": str(self.uploads)},
            {"APPLY101_DATABASE_URL": f"sqlite:///{self.db}", "APPLY101_UPLOAD_DIR": str(config.REPO_ROOT / "uploads")},
            {"APPLY101_DATABASE_URL": "postgresql://x/y", "APPLY101_UPLOAD_DIR": str(self.uploads)},
            # The upload directory must not hold the database or the backups.
            {"APPLY101_DATABASE_URL": f"sqlite:///{self.db}", "APPLY101_UPLOAD_DIR": str(self.tmp)},
            {**self.env, "APPLY101_BACKUP_DIR": str(self.uploads / "backups")},
        ]
        for env in cases:
            with self.subTest(env=env):
                code, out = self.run_main(["backup", "--yes"], env=env)
                self.assertEqual(code, 1)
                self.assertIn("Refusing", out)

    def test_failed_database_copy_puts_the_uploads_back(self):
        target = backup_data.backup(self.env, keep=0, now=_now(0))
        (self.uploads / "user_2_profile_2_cv.pdf").write_bytes(b"%PDF-new")
        real_copy = backup_data._copy_sqlite

        def failing_copy(source, destination):
            if Path(destination) == self.db:
                raise sqlite3.OperationalError("database is locked")
            return real_copy(source, destination)

        backup_data._copy_sqlite = failing_copy
        try:
            code, out = self.run_main(["restore", "--from", target.name, "--yes"])
        finally:
            backup_data._copy_sqlite = real_copy
        self.assertEqual(code, 1)
        self.assertIn("database is locked", out)
        self.assertEqual((self.uploads / "user_2_profile_2_cv.pdf").read_bytes(), b"%PDF-new")
        self.assertEqual(self.notes(), ["original"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith((".restore", "uploads.before"))), [])

    def test_restore_refuses_unknown_names_and_bad_archives(self):
        code, out = self.run_main(["restore", "--from", "../etc", "--yes"])
        self.assertEqual(code, 1)
        target = backup_data.backup(self.env, keep=0, now=_now(0))
        archive = target / "uploads.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            data = b"x"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        code, out = self.run_main(["restore", "--from", target.name, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("Unexpected path", out)
        self.assertFalse((self.tmp / "escape.txt").exists())
        # Refused before anything was written, not even a safety backup.
        self.assertEqual(backup_data.list_backups(self.tmp / "backups"), [target.name])
        # Nothing was replaced.
        self.assertEqual(self.notes(), ["original"])
        self.assertEqual((self.uploads / "user_1_profile_1_cv.pdf").read_bytes(), b"%PDF-original")


if __name__ == "__main__":
    unittest.main()
