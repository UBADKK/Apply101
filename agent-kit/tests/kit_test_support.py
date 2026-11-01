"""Helpers for the kit tests: throwaway fake projects in temp directories.

Nothing here touches the repository the kit lives in: every project is a
fresh ``git init`` under a temporary directory, its "real data" is a
synthetic placeholder file, git runs without the user's global or system
configuration, and CLAUDE_PROJECT_DIR points at the fake project.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
if str(KIT_ROOT) not in sys.path:
    sys.path.insert(0, str(KIT_ROOT))

from kitlib import common  # noqa: E402
from kitlib.install import install  # noqa: E402

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "kit-test",
    "GIT_AUTHOR_EMAIL": "kit-test@example.invalid",
    "GIT_COMMITTER_NAME": "kit-test",
    "GIT_COMMITTER_EMAIL": "kit-test@example.invalid",
}

FAKE_CONFIG = {
    "kit_config_version": 1,
    "project_name": "Calcfake",
    "report_language": "English",
    "default_branch": "main",
    "checkpoint_dir": ".claude/agent-kit/checkpoints",
    "protected_paths": ["*.db", "*.db-*", "uploads/**", "backups/**"],
    "real_data_commands": [
        "-m\\s+calc\\.(?:migrate|admin)\\b",
        "calc[/\\\\]migrate\\.py",
        "^serve-prod\\b",
    ],
    "secret_env_vars": ["CALC_SIGNING_KEY"],
    "watch_files": ["data/app.db"],
    "approval_required_paths": ["migrations/**", "calc/contract.py", ".claude/**"],
    "risk_keywords": {"data": ["ledger"]},
    "test_commands": {
        "focused": "python3 -m unittest tests.test_calc",
        "full": "python3 -m unittest discover -s tests -t .",
    },
    "loop_limits": {"max_fix_rounds": 2},
}

CALC_PY = """\
def add(a, b):
    return a - b


def mul(a, b):
    return a + b
"""

TEST_CALC_PY = """\
import unittest

from calc.core import add


class AddTests(unittest.TestCase):
    def test_add_small(self):
        self.assertEqual(add(1, 0), 1)
"""


def context_text(config=None, **overrides):
    config = dict(config or FAKE_CONFIG)
    config.update(overrides)
    sections = "\n\n".join(
        f"## {name}\n\nSynthetic {name.lower()} for the kit tests."
        for name in ("Project summary", "Repository map", "Sources of truth", "Sensitive areas",
                     "Real data and secrets", "Verification", "Conventions")
    )
    return (
        "# Project context (agent kit adapter): fake calculator\n\n"
        + sections
        + "\n\n## Machine-readable configuration\n\n```json kit-config\n"
        + json.dumps(config, indent=2)
        + "\n```\n"
    )


def git(root, *args, check=True):
    env = dict(os.environ, **GIT_ENV)
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env=env, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


class FakeProjectCase(unittest.TestCase):
    """A fake calculator project with the kit installed and committed."""

    config = FAKE_CONFIG
    branch = "task/work"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kit_fake_project_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "calc-project"
        self.root.mkdir()
        self.assertNotIn(str(KIT_ROOT.parent), str(self.root))
        self.env = dict(os.environ, **GIT_ENV, CLAUDE_PROJECT_DIR=str(self.root),
                        PYTHONDONTWRITEBYTECODE="1")
        self._patch_env({"CLAUDE_PROJECT_DIR": str(self.root)})
        self.write("calc/__init__.py", "")
        self.write("calc/core.py", CALC_PY)
        self.write("calc/contract.py", "VERSION = 'calc_v1'\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_calc.py", TEST_CALC_PY)
        self.write("migrations/0001_init.sql", "CREATE TABLE users (id INTEGER);\n")
        self.write("README.md", "# Fake calculator\n")
        self.write(".gitignore", "*.db\n.env\nuploads/\nbackups/\n__pycache__/\n")
        self.write("data/app.db", "synthetic placeholder, not a database\n")
        self.write(".env", "SYNTHETIC_ONLY=1\n")
        git(self.root, "init", "-q", "-b", "main")
        context = self.tmp / "context.md"
        context.write_text(context_text(self.config), encoding="utf-8")
        install(self.root, context)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "fake project with the kit installed")
        git(self.root, "checkout", "-q", "-b", self.branch)

    def _patch_env(self, values):
        old = {k: os.environ.get(k) for k in values}
        os.environ.update(values)

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def kit(self, *args, expect=None):
        """Runs the installed CLI of the fake project."""
        proc = subprocess.run(
            [sys.executable, str(self.root / ".claude/agent-kit/kit.py"), *args],
            capture_output=True, text=True, cwd=self.root, env=self.env, check=False,
        )
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             f"kit {' '.join(args)}\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
        return proc

    def report(self, role, verdict, findings="none", risk="none", body="Synthetic report."):
        path = self.tmp / f"{role}-{verdict}-{len(list(self.tmp.glob('*.txt')))}.txt"
        path.write_text(textwrap.dedent(f"""\
            {body}

            ```kit-result
            role: {role}
            verdict: {verdict}
            findings: {findings}
            risk: {risk}
            ```
            """), encoding="utf-8")
        return str(path)

    def hook(self, agent_type, tool_name, tool_input):
        """Runs the installed guard wrapper exactly like Claude Code would."""
        payload = {"session_id": "synthetic", "hook_event_name": "PreToolUse",
                   "tool_name": tool_name, "tool_input": tool_input}
        if agent_type:
            payload["agent_type"] = agent_type
        proc = subprocess.run(
            ["bash", str(self.root / ".claude/agent-kit/hooks/kit_guard.sh")],
            input=json.dumps(payload), capture_output=True, text=True, env=self.env,
            cwd=self.root, check=False,
        )
        return proc.returncode, proc.stderr

    def assertHookBlocks(self, agent_type, tool_name, tool_input):
        rc, err = self.hook(agent_type, tool_name, tool_input)
        self.assertEqual(rc, 2, f"expected block: {tool_name} {tool_input}")
        self.assertIn("Blocked by agent-kit guard", err)
        return err

    def assertHookAllows(self, agent_type, tool_name, tool_input):
        rc, err = self.hook(agent_type, tool_name, tool_input)
        self.assertEqual(rc, 0, f"expected allow: {tool_name} {tool_input}: {err}")

    def run_project_tests(self):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            capture_output=True, text=True, cwd=self.root, env=self.env, check=False,
        )
        return proc.returncode

    def checkpoint_text(self, task):
        return (self.root / ".claude/agent-kit/checkpoints" / f"{task}.md").read_text(encoding="utf-8")

    def state(self, task):
        from kitlib import checkpoint
        return checkpoint.parse(self.checkpoint_text(task))


def guard_module():
    return common.load_guard(KIT_ROOT / "core" / "hooks" / "kit_guard.py")
