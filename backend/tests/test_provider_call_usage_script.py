"""backend.scripts.provider_call_usage: read-only report of the global
provider-call quota and admin calls. Throwaway SQLite only; .env and
apply101.db are never touched and backend.app.main is never imported."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

from backend.app import config
from backend.app.analysis_guard import (
    PROVIDER_CALL_ADMIN_OPERATION_TYPE,
    PROVIDER_CALL_GLOBAL_OPERATION_TYPE,
    USER_OPERATION_TYPE,
)
from backend.app.database import Base
from backend.scripts import provider_call_usage

NOW = 5_000_000.0


class ProviderCallUsageScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_usage_script_test_")
        self.db_path = os.path.join(self.tmp_dir, "usage.db")
        engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(bind=engine)
        rows = (
            [(PROVIDER_CALL_GLOBAL_OPERATION_TYPE, 0, NOW - 86400 + 30)]  # oldest counted
            + [(PROVIDER_CALL_GLOBAL_OPERATION_TYPE, 0, NOW - 10)] * 2
            + [(PROVIDER_CALL_GLOBAL_OPERATION_TYPE, 0, NOW - 90000)]  # outside the window
            + [(PROVIDER_CALL_ADMIN_OPERATION_TYPE, 7, NOW - 5)] * 2
            + [(PROVIDER_CALL_ADMIN_OPERATION_TYPE, 12, NOW - 5)]
            + [(USER_OPERATION_TYPE, 3, NOW - 5)]
        )
        with engine.begin() as connection:
            for op, uid, at in rows:
                connection.execute(text(
                    "INSERT INTO analysis_quota_reservations (operation_type, user_id, job_id, reserved_at) "
                    "VALUES (:op, :uid, NULL, :at)"), {"op": op, "uid": uid, "at": at})
            connection.execute(text("INSERT INTO users (user_id, name, mail) VALUES (7, 'A', 'admin.secret@example.com')"))
        engine.dispose()
        self.env = {config.DATABASE_URL_ENV: f"sqlite:///{self.db_path}"}

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _digest(self):
        return hashlib.sha256(Path(self.db_path).read_bytes()).hexdigest()

    def test_report_for_the_rolling_window(self):
        before = self._digest()
        report = provider_call_usage.run(Path(self.db_path), environ={}, now=NOW)
        self.assertEqual(report["global"], {"used": 3, "limit": 100, "remaining": 97, "next_slot_frees_in_seconds": 0})
        self.assertEqual(report["admin_calls_by_user_id"], {"7": 2, "12": 1})
        self.assertEqual(report["counts_by_operation_type"], {
            PROVIDER_CALL_ADMIN_OPERATION_TYPE: 3, PROVIDER_CALL_GLOBAL_OPERATION_TYPE: 3, USER_OPERATION_TYPE: 1,
        })
        self.assertEqual(self._digest(), before)

    def test_full_quota_reports_when_the_next_slot_frees(self):
        report = provider_call_usage.run(
            Path(self.db_path), environ={"PROVIDER_CALL_GLOBAL_QUOTA_MAX_ATTEMPTS": "3"}, now=NOW
        )
        self.assertEqual(report["global"]["remaining"], 0)
        self.assertEqual(report["global"]["next_slot_frees_in_seconds"], 30)

    def test_main_prints_ids_only_and_writes_nothing(self):
        before = self._digest()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(provider_call_usage.main([], environ=self.env), provider_call_usage.EXIT_SUCCESS)
        report = json.loads(out.getvalue())
        self.assertIn("global", report)
        self.assertNotIn("@", out.getvalue())
        self.assertEqual(self._digest(), before)
        self.assertEqual(sorted(os.listdir(self.tmp_dir)), ["usage.db"])  # no journal left behind

    def test_refusals(self):
        missing = os.path.join(self.tmp_dir, "missing.db")
        for env in ({}, {config.DATABASE_URL_ENV: f"sqlite:///{missing}"},
                    {config.DATABASE_URL_ENV: "sqlite:///./apply101.db"},
                    {config.DATABASE_URL_ENV: "sqlite://"},
                    {**self.env, "PROVIDER_CALL_GLOBAL_QUOTA_WINDOW_SECONDS": "0"}):
            with self.subTest(env=env), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(provider_call_usage.main([], environ=env), provider_call_usage.EXIT_FAILURE)
                self.assertIn("Refusing to continue", out.getvalue())
        self.assertFalse(os.path.exists(missing))


if __name__ == "__main__":
    unittest.main()
