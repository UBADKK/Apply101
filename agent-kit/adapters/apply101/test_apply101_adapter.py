"""Tests for the Apply101 example adapter.

Only the adapter file and synthetic hook inputs are used: no Apply101 code is
imported and no real database, .env or uploads/ is opened. The command
corpus mirrors the cases of Apply101's existing agent file guard, so the
kit guard with this adapter is at least as strict for the kit agents.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADAPTER = Path(__file__).resolve().parent / "PROJECT_CONTEXT.md"
KIT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT_ROOT))

from kitlib.validate import Report, validate_context  # noqa: E402
from kitlib.workflow import scan_risk  # noqa: E402

_spec = importlib.util.spec_from_file_location("kit_guard_apply101", KIT_ROOT / "core/hooks/kit_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)
CONFIG = guard.parse_config(ADAPTER.read_text(encoding="utf-8"))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kit_adapter_test_")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = guard.GuardSettings(CONFIG, self.root)

    def decide(self, command, agent="kit-developer", tool="Bash"):
        return guard.decide({"agent_type": agent, "tool_name": tool, "tool_input": {"command": command}},
                            self.settings)

    def test_adapter_is_valid(self):
        report = Report()
        validate_context(ADAPTER.read_text(encoding="utf-8"), report, "adapter")
        self.assertTrue(report.ok, report.lines())

    def test_real_data_and_secret_paths_are_protected(self):
        for path in ("apply101.db", "apply101.db-wal", "apply101.db-journal", "apply101_backup_1.db",
                     "my_backup.db", "uploads/cv.pdf", "backups/2026.db", ".env", ".env.local"):
            with self.subTest(path=path):
                reason = guard.decide({"agent_type": "kit-reviewer", "tool_name": "Read",
                                       "tool_input": {"file_path": os.path.join(self.root, path)}},
                                      self.settings)
                self.assertIsNotNone(reason)
        # A sandbox database of the documented demo command is not real data.
        self.assertIsNone(guard.decide({"agent_type": "kit-developer", "tool_name": "Read",
                                        "tool_input": {"file_path": "/tmp/sandbox/demo.db"}}, self.settings))

    BLOCKED = [
        # From the existing guard's corpus, with the same names.
        "sha256sum apply101.db", 'sqlite3 apply101.db ".tables"', "cat .env",
        "cp apply101.db /tmp/copy.db", "python -c \"open('apply101.db','rb').read()\"",
        "ls -l apply101.db && cat apply101.db", "git diff --stat -- apply101.db uploads .env",
        "grep -r SECRET --include=.env .", "rg TOKEN uploads/", "git grep -n x -- uploads",
        "echo SECRET=1 > .env", "ls uploads/", "mv apply101.db old.db", 'cat ".env',
        "ls apply101.db* | xargs sha256sum", "sha256sum $(ls apply101.db*)",
        "grep -rn OPENAI_API_KEY .", "rg -uu TOKEN .", "bash -c 'cat .env'",
        "rm -rf build", "git clean -fdx", "find . -name x -exec rm {} \\;",
        "python -c \"import shutil; shutil.rmtree('x')\"",
        # Real-database entry points.
        "python -m backend.migrations.phase5_job_owner",
        ".venv/bin/python -m backend.scripts.set_admin someone",
        ".venv/Scripts/python.exe backend/scripts/sync_catalog.py",
        "uvicorn backend.app.main:app --reload",
        "timeout 30 uvicorn --factory backend.app.deploy_app:create_app",
        "python -m uvicorn backend.app.main:app",
        "python -c \"import backend.app.main\"",
        "python -c \"from backend.app import main\"",
        "python -c \"from backend.app.deploy_app import create_app\"",
        "python - <<'EOF'\nfrom backend.app.main import app\nEOF",
        "python frontend/e2e/seed_catalog.py",
        "echo $OPENAI_API_KEY", "echo $JWT_SECRET_KEY", "echo $APPLY101_DATABASE_URL",
    ]
    ALLOWED = [
        "ls -l --time-style=full-iso apply101.db*",
        "test -f .env && echo present",
        "git check-ignore -v apply101.db .env uploads/x",
        'grep -n "apply101.db" backend/tests/test_synthetic.py',
        'grep -rn "uploads" backend/', "grep -rn uvicorn backend/",
        "sed -n '1,40p' backend/migrations/phase5_job_owner.py",
        "cat backend/app/main.py",
        "git diff -- backend/migrations/",
        "python -m unittest backend.tests.test_job_owner_migration",
        "pip show uvicorn",
        "rg TOKEN .",
    ]

    def test_existing_guard_corpus_is_still_blocked(self):
        for agent in sorted(guard.KIT_AGENT_TYPES):
            for command in self.BLOCKED:
                with self.subTest(agent=agent, command=command):
                    self.assertIsNotNone(self.decide(command, agent))

    def test_windows_paths_in_powershell(self):
        for command in ("python backend\\migrations\\phase2_profile_fields.py",
                        ".venv\\Scripts\\python.exe -m backend.scripts.set_admin x",
                        "Get-Content apply101.db -Encoding Byte -TotalCount 16"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.decide(command, tool="PowerShell"))
        self.assertIsNone(self.decide(".venv\\Scripts\\python.exe -m unittest discover -s backend/tests",
                                      tool="PowerShell"))

    def test_safe_commands_are_allowed(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_documented_test_commands_are_allowed(self):
        for name, command in CONFIG["test_commands"].items():
            command = command.replace("<module>", "test_matching")
            with self.subTest(name=name):
                self.assertIsNone(self.decide(command))

    def test_turkish_and_domain_risk_words(self):
        self.assertIn("production", scan_risk("Canlı sunucuya yayına al", CONFIG))
        self.assertIn("data", scan_risk("apply101.db yedeğini geri yükle", CONFIG))
        self.assertIn("data", scan_risk("Tighten eligibility for student jobs", CONFIG))
        self.assertEqual(scan_risk("Fix a typo in the profile page label", CONFIG), [])


if __name__ == "__main__":
    unittest.main()
