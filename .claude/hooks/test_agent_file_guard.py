"""Tests for .claude/hooks/agent_file_guard.py.

Only synthetic hook inputs are used. A throwaway temp directory holds fake
"apply101.db", ".env" and "uploads/" files so that paths look realistic;
no input ever points at the real repository's database, .env or uploads/,
and the guard itself never opens any file (asserted below).

Run from the repository root:
    .venv/Scripts/python.exe -m unittest discover -s .claude/hooks -p "test_*.py"

WrapperTests run agent_file_guard.sh with Git Bash (bash elsewhere) and are
skipped when it cannot be found.
"""

import builtins
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HOOKS_DIR, "agent_file_guard.py")
WRAPPER = os.path.join(HOOKS_DIR, "agent_file_guard.sh")
SETTINGS = os.path.join(os.path.dirname(HOOKS_DIR), "settings.json")

_spec = importlib.util.spec_from_file_location("agent_file_guard", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

GUARDED = (
    "project-auditor",
    "project-developer",
    "project-reviewer",
    "project-supervisor",
)
UNGUARDED = (None, "general-purpose", "Explore", "Plan", "claude-code-guide", "statusline-setup")


class _FakeProject(unittest.TestCase):
    """A synthetic project root with fake protected files."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="agent_guard_test_")
        with open(os.path.join(cls.root, "apply101.db"), "wb") as f:
            f.write(b"synthetic placeholder, not a database")
        with open(os.path.join(cls.root, ".env"), "w", encoding="utf-8") as f:
            f.write("SYNTHETIC_ONLY=1\n")
        os.makedirs(os.path.join(cls.root, "uploads"))
        with open(os.path.join(cls.root, "uploads", "synthetic.pdf"), "wb") as f:
            f.write(b"%PDF-synthetic")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    @staticmethod
    def payload(tool_name, tool_input, agent_type="project-developer"):
        data = {
            "session_id": "synthetic-session",
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        if agent_type is not None:
            data["agent_type"] = agent_type
            data["agent_id"] = "synthetic-agent-id"
        return data

    def assertBlocked(self, tool_name, tool_input, agent_type="project-developer"):
        reason = guard.decide(self.payload(tool_name, tool_input, agent_type))
        self.assertIsNotNone(reason, f"expected block: {tool_name} {tool_input!r}")
        return reason

    def assertAllowed(self, tool_name, tool_input, agent_type="project-developer"):
        reason = guard.decide(self.payload(tool_name, tool_input, agent_type))
        self.assertIsNone(reason, f"expected allow: {tool_name} {tool_input!r} -> {reason}")


class FileToolTests(_FakeProject):
    def test_protected_paths_blocked_for_every_guarded_agent(self):
        cases = [
            ("Read", {"file_path": self.p("apply101.db")}),
            ("Read", {"file_path": self.p("apply101.db") + "-wal"}),
            ("Read", {"file_path": "apply101.db-shm"}),
            ("Read", {"file_path": "apply101.db-journal"}),
            ("Read", {"file_path": self.p(".env")}),
            ("Read", {"file_path": ".env.local"}),
            ("Read", {"file_path": self.p("uploads", "synthetic.pdf")}),
            ("Read", {"file_path": self.p("uploads", "synthetic.pdf").replace("/", "\\")}),
            ("Edit", {"file_path": self.p(".env"), "old_string": "a", "new_string": "b"}),
            ("MultiEdit", {"file_path": self.p(".env"), "edits": []}),
            ("Write", {"file_path": self.p("uploads", "new.txt"), "content": "x"}),
            ("Write", {"file_path": self.p("apply101.db"), "content": ""}),
            ("NotebookEdit", {"notebook_path": "uploads/n.ipynb", "new_source": ""}),
            ("Grep", {"pattern": "x", "path": self.p("uploads")}),
            ("Grep", {"pattern": "x", "path": self.p(".env")}),
            ("Grep", {"pattern": "x", "glob": ".env*"}),
            ("Grep", {"pattern": "x", "glob": "**/apply101.db*"}),
            ("Glob", {"pattern": "uploads/**"}),
            ("Glob", {"pattern": "**/.env"}),
            ("Glob", {"pattern": "*", "path": self.p("uploads")}),
        ]
        for agent in GUARDED:
            for tool, tool_input in cases:
                with self.subTest(agent=agent, tool=tool, tool_input=tool_input):
                    self.assertBlocked(tool, tool_input, agent)

    def test_ordinary_paths_and_search_patterns_allowed(self):
        cases = [
            ("Read", {"file_path": self.p("backend", "app", "models.py")}),
            ("Edit", {"file_path": "backend/routers/jobs.py", "old_string": "a", "new_string": "b"}),
            ("Write", {"file_path": "backend/tests/test_synthetic.py", "content": "x"}),
            ("Read", {"file_path": "backend/app/environment_notes.md"}),
            ("Read", {"file_path": "docs/upload_guide.md"}),
            # The search regex itself may name protected files.
            ("Grep", {"pattern": "apply101.db", "path": "backend"}),
            ("Grep", {"pattern": "uploads/", "path": "backend", "glob": "*.py"}),
            ("Grep", {"pattern": "os.environ", "glob": "*.py"}),
            # Exclusion globs are fine.
            ("Grep", {"pattern": "SECRET", "path": self.root, "glob": "!**/.env"}),
            ("Glob", {"pattern": "backend/**/*.py"}),
        ]
        for agent in GUARDED:
            for tool, tool_input in cases:
                with self.subTest(agent=agent, tool=tool, tool_input=tool_input):
                    self.assertAllowed(tool, tool_input, agent)


class ShellAccessTests(_FakeProject):
    BLOCKED_BASH = [
        "sha256sum apply101.db",
        'sqlite3 apply101.db ".tables"',
        "cat .env",
        "cat ./.env",
        "cp apply101.db /tmp/copy.db",
        "python -c \"open('apply101.db','rb').read()\"",
        "python -c \"from dotenv import load_dotenv; load_dotenv('.env')\"",
        "ls -l apply101.db && cat apply101.db",
        "git diff --stat -- apply101.db uploads .env",
        "grep SECRET .env",
        "grep -r SECRET --include=.env .",
        "grep -f .env backend/app/models.py",
        "rg TOKEN -g '.env'",
        "rg TOKEN uploads/",
        "git grep -n x -- uploads",
        "echo SECRET=1 > .env",
        "head -c 100 apply101.db",
        "ls uploads/",
        "cd uploads && ls",
        "mv apply101.db old.db",
        'cat ".env',  # unbalanced quote
        "type C:\\proj\\.env",  # Windows path the tokenizer cannot attribute
        # Allowlisted name listings must not feed readers or hashers.
        "ls apply101.db* | xargs sha256sum",
        "echo .env | xargs cat",
        "sha256sum $(ls apply101.db*)",
        "git ls-files -o -- uploads | xargs cat",
        # -f makes every positional a path.
        "grep -f pat.txt .env",
        # git global options before the subcommand.
        "git -C /c/synthetic/project grep -n x -- uploads",
        "git -C /c/synthetic/project diff -- .env",
        # Recursive search of the repository root reaches .env and uploads/.
        "grep -rn OPENAI_API_KEY .",
        "grep -rn OPENAI_API_KEY",
        "grep -Rl TOKEN ./",
        "grep -r --exclude=.env TOKEN .",
        "grep -d recurse TOKEN .",
        "rg --hidden --no-ignore TOKEN",
        "rg -uu TOKEN .",
        "grep -rn TOKEN ..",
        "grep -rn TOKEN --exclude=.env --exclude-dir=uploads .",
        "grep -rn TOKEN --exclude=.env.* --exclude-dir=uploads .",
        "rg -u TOKEN . -g '!.env' -g '!uploads'",
        "grep -rn TOKEN --include='*.local' .",
        "grep -rn TOKEN --include='*' --exclude-dir=uploads .",
        "ls apply101.db* | sort | xargs sha256sum",
        "grep -rn xargs .",
        # A nested shell is inspected like the outer one.
        "bash -c 'cat .env'",
    ]
    ALLOWED_BASH = [
        ".venv/Scripts/python.exe -m unittest discover backend/tests",
        "git status --short --untracked-files=all && git diff --stat",
        "ls -l --time-style=full-iso apply101.db*",
        "git branch --show-current && ls -l --time-style=full-iso apply101.db* && git status --short",
        'for s in -wal -shm -journal; do [ -e "apply101.db$s" ] && echo found || echo absent; done',
        "test -f .env && echo present",
        "git check-ignore -v apply101.db .env uploads/x",
        "git ls-files -- apply101.db .env uploads",
        'grep -n "apply101.db" backend/tests/test_synthetic.py',
        'grep -rn "uploads" backend/',
        "grep -rln SECRET --exclude-dir=.venv --exclude=.env* --exclude-dir=uploads .",
        "rg '\\.env' backend -g '!.env'",
        'git grep -n "apply101.db" -- backend',
        'echo "=== apply101.db unchanged"',
        "python -c \"import os; print(os.environ.get('X'))\"",
        "python -m unittest backend.tests.test_job_owner_migration",
        "sed -n '1,40p' backend/migrations/phase5_job_owner.py",
        "grep -n 'shutil.rmtree\\|os.remove' backend/tests/test_synthetic.py",
        "echo performance --format",
        "git log --oneline -3",
        "git -C /c/synthetic/project status --short -- apply101.db",
        "git -C /c/synthetic/project check-ignore -v .env",
        'git -C /c/synthetic/project grep -n "apply101.db" -- backend',
        "git -c core.quotepath=off status --short",
        "ls -l apply101.db* | awk '{print $5}'",
        "git check-ignore -v apply101.db .env | sort",
        "grep -rl 'apply101.db' backend | xargs grep -n engine",
        "grep -rn TOKEN backend/",
        "grep -rn TOKEN --exclude=.env* --exclude-dir=uploads .",
        "rg TOKEN .",
        "rg -u TOKEN . -g '!.env*' -g '!uploads'",
        "grep -rn TOKEN --include='*.py' .",
        "grep -rn TOKEN --include=*.py --include=*.md",
        "find backend -name '*.py' | xargs grep -rn TOKEN",
        "git log -S shutil.rmtree --oneline",
        "pip show uvicorn",
        "timeout 120 .venv/Scripts/python.exe -m unittest backend.tests.test_matching",
    ]
    BLOCKED_POWERSHELL = [
        "Get-Content .env",
        "type .env",
        "Get-Content apply101.db -Encoding Byte -TotalCount 16",
        "Copy-Item apply101.db C:\\backup\\x.db",
        "Get-ChildItem uploads",
        "Select-String -Pattern SECRET -Path .env",
        "Select-String SECRET .env",
        "Get-Item apply101.db | Get-FileHash",
        "Get-Item .env | Get-Content",
        "Get-Item apply101.db | Select-String x",
        "Get-FileHash (Get-Item apply101.db)",
        "Get-Item .env | Select-Object -ExpandProperty FullName | Get-Content",
    ]
    ALLOWED_POWERSHELL = [
        "Test-Path apply101.db",
        "Get-Item apply101.db | Select-Object Length, LastWriteTime",
        "Select-String -Pattern 'apply101.db' -Path backend/app/database.py",
        "Write-Output $env:PATH",
        "git status --short",
        "if (Test-Path .env) { 'present' }",
        "if (-not (Test-Path apply101.db)) { 'absent' }",
        "Write-Output (Get-Item apply101.db).Length",
        ".venv\\Scripts\\python.exe -m unittest discover -s backend/tests",
    ]

    def test_bash_blocked(self):
        for agent in GUARDED:
            for command in self.BLOCKED_BASH:
                with self.subTest(agent=agent, command=command):
                    self.assertBlocked("Bash", {"command": command}, agent)

    def test_bash_allowed(self):
        for agent in GUARDED:
            for command in self.ALLOWED_BASH:
                with self.subTest(agent=agent, command=command):
                    self.assertAllowed("Bash", {"command": command}, agent)

    def test_powershell_blocked(self):
        for command in self.BLOCKED_POWERSHELL:
            with self.subTest(command=command):
                self.assertBlocked("PowerShell", {"command": command})

    def test_powershell_allowed(self):
        for command in self.ALLOWED_POWERSHELL:
            with self.subTest(command=command):
                self.assertAllowed("PowerShell", {"command": command})


class DeletionTests(_FakeProject):
    BLOCKED = [
        ("Bash", 'rm -rf -- "$TEMP/apply101_inflight_x"'),
        ("Bash", "rm notes.txt"),
        ("Bash", "rmdir build"),
        ("Bash", "unlink stale.lock"),
        ("Bash", "git clean -fdx"),
        ("Bash", "git rm backend/old.py"),
        ("Bash", "find . -name '*.pyc' -delete"),
        ("Bash", "find . -name x -exec rm {} \\;"),
        ("Bash", "ls *.tmp | xargs rm"),
        ("Bash", "xargs -n 1 rm < list.txt"),
        ("Bash", "sudo rm -r /tmp/x"),
        ("Bash", "python -c \"import shutil; shutil.rmtree('x')\""),
        ("Bash", "python - <<'EOF'\nimport os\nos.remove('x')\nEOF"),
        ("Bash", "python -c \"from pathlib import Path; Path('x').unlink()\""),
        ("Bash", "node -e \"require('fs').rmSync('x', {recursive: true})\""),
        ("PowerShell", "Remove-Item -Recurse -Force build"),
        ("PowerShell", "ri build -Recurse"),
        ("PowerShell", "del build"),
        ("PowerShell", "[System.IO.File]::Delete('x.txt')"),
        # git global options, nested shells and wrappers.
        ("Bash", "git -C /c/synthetic/project clean -fdx"),
        ("Bash", "git -c core.x=y rm backend/old.py"),
        ("Bash", 'bash -c "rm -rf build"'),
        ("Bash", "sh -c 'rm x'"),
        ("Bash", "bash -o pipefail -lc 'unlink x'"),
        ("Bash", "cmd //c del x"),
        ("Bash", "cmd /c rd /s /q build"),
        ("Bash", "timeout 10 rm x"),
        ("Bash", "nice -n 10 rm x"),
        ("Bash", "sudo -u someone rm x"),
        ("Bash", "winpty rm x"),
        ("Bash", "find . -name x | xargs -I{} sh -c 'rm {}'"),
        ("Bash", "echo \"import os; os.remove('x')\" | python"),
        ("PowerShell", 'powershell -NoProfile -Command "Remove-Item build"'),
        ("PowerShell", "cmd /c rd /s /q C:\\synthetic\\build"),
    ]

    def test_deletions_blocked_for_every_guarded_agent(self):
        for agent in GUARDED:
            for tool, command in self.BLOCKED:
                with self.subTest(agent=agent, tool=tool, command=command):
                    reason = self.assertBlocked(tool, {"command": command}, agent)
                    self.assertIn("delete", reason)


class RealDatabaseEntryPointTests(_FakeProject):
    BLOCKED = [
        ".venv/Scripts/python.exe -m backend.migrations.phase8_analysis_quota_reservations",
        "python -m backend.migrations.phase7_job_url_ownership --dry-run",
        "python backend/migrations/phase5_job_owner.py",
        "python -W ignore -m backend.migrations.phase4_analysis_guards",
        "uvicorn backend.app.main:app --reload",
        "python -m uvicorn backend.app.main:app",
        "python -m backend.scripts.set_admin",
        "python backend/scripts/set_admin.py someone",
        "python -c \"from backend.migrations import phase8_analysis_quota_reservations as p; p.run()\"",
        "python -c \"import backend.app.main\"",
        "python -c \"from backend.app import main\"",
        # Heredoc and piped code are inspected too.
        "python - <<'EOF'\nfrom backend.app.main import app\nprint(app.routes)\nEOF",
        "python - <<'EOF'\n# don't\nimport backend.migrations.phase8_analysis_quota_reservations as m\nEOF",
        "echo 'from backend.app.main import app' | python -",
        "python - <<'EOF'\nimport uvicorn\nuvicorn.run('backend.app.main:app')\nEOF",
        # Wrappers and nested shells.
        "timeout 60 python -m backend.migrations.phase7_job_url_ownership",
        "winpty python -m backend.migrations.phase8_analysis_quota_reservations",
        "bash -c 'python -m backend.scripts.set_admin'",
        "sudo -u someone python -m backend.scripts.set_admin",
    ]
    BLOCKED_POWERSHELL = [
        ".venv\\Scripts\\python.exe -m backend.migrations.phase8_analysis_quota_reservations",
        "python backend\\migrations\\phase7_job_url_ownership.py",
    ]

    def test_entry_points_blocked(self):
        for agent in GUARDED:
            for command in self.BLOCKED:
                with self.subTest(agent=agent, command=command):
                    self.assertBlocked("Bash", {"command": command}, agent)
            for command in self.BLOCKED_POWERSHELL:
                with self.subTest(agent=agent, command=command):
                    self.assertBlocked("PowerShell", {"command": command}, agent)


class ProjectRootSearchTests(_FakeProject):
    """Recursive grep over CLAUDE_PROJECT_DIR (a synthetic, nonexistent path
    here) or one of its ancestors must exclude .env and uploads/."""

    BLOCKED = [
        "grep -rn TOKEN /c/synthetic/project",
        'grep -rn TOKEN "$CLAUDE_PROJECT_DIR"',
        "grep -rn TOKEN ${CLAUDE_PROJECT_DIR}/",
        "grep -rn TOKEN ~",
        "grep -rna TOKEN /c/synthetic/project --exclude=.env* --exclude-dir=uploads --exclude=apply101.db",
        'grep -rn TOKEN "C:\\synthetic\\project"',
        "grep -rn TOKEN C:/synthetic/project/",
        "grep -rn TOKEN /c/synthetic",
        "grep -rna TOKEN /c/synthetic/project --exclude=.env* --exclude-dir=uploads",
    ]
    ALLOWED = [
        "grep -rn TOKEN /c/synthetic/project/backend",
        "grep -rn TOKEN /c/synthetic/project --exclude=.env* --exclude-dir=uploads",
        "grep -n TOKEN /c/synthetic/project/backend/app/models.py",
        "grep -rn TOKEN /c/synthetic/project-other",
        'grep -rn TOKEN "$CLAUDE_PROJECT_DIR/backend"',
        "grep -rn TOKEN ~/project/backend",
        "grep -rna TOKEN /c/synthetic/project --exclude=.env* --exclude-dir=uploads --exclude=apply101.db*",
    ]

    def test_recursive_search_of_project_root(self):
        fake_env = {
            "CLAUDE_PROJECT_DIR": "C:/synthetic/project",
            "HOME": "C:/synthetic",
            "USERPROFILE": "C:/synthetic",
        }
        with mock.patch.dict(os.environ, fake_env):
            for command in self.BLOCKED:
                with self.subTest(command=command):
                    self.assertBlocked("Bash", {"command": command})
            for command in self.ALLOWED:
                with self.subTest(command=command):
                    self.assertAllowed("Bash", {"command": command})


class AgentToolTests(_FakeProject):
    def test_only_project_agent_types_may_be_started(self):
        for sub in GUARDED:
            with self.subTest(subagent_type=sub):
                self.assertAllowed(
                    "Agent", {"subagent_type": sub, "prompt": "x"}, "project-supervisor"
                )
        for sub in ("general-purpose", "Explore", "Plan", None):
            with self.subTest(subagent_type=sub):
                tool_input = {"prompt": "x"}
                if sub is not None:
                    tool_input["subagent_type"] = sub
                self.assertBlocked("Agent", tool_input, "project-supervisor")
                self.assertBlocked("Task", tool_input, "project-supervisor")


class ScopeTests(_FakeProject):
    FORBIDDEN = [
        ("Read", {"file_path": ".env"}),
        ("Bash", {"command": "sha256sum apply101.db"}),
        ("Bash", {"command": "rm -rf build"}),
        ("Bash", {"command": ".venv/Scripts/python.exe -m backend.migrations.phase8_analysis_quota_reservations"}),
        ("Agent", {"subagent_type": "general-purpose", "prompt": "x"}),
    ]

    def test_main_session_and_other_agent_types_are_never_blocked(self):
        for agent in UNGUARDED:
            for tool, tool_input in self.FORBIDDEN:
                with self.subTest(agent=agent, tool=tool):
                    self.assertAllowed(tool, tool_input, agent)

    def test_agent_type_must_match_exactly(self):
        for agent in ("Project-Developer", "project-developer ", "project-developer-x"):
            with self.subTest(agent=agent):
                self.assertAllowed("Read", {"file_path": ".env"}, agent)

    def test_guard_never_opens_files(self):
        def no_open(*args, **kwargs):
            raise AssertionError("guard opened a file")

        with mock.patch.object(builtins, "open", side_effect=no_open), \
                mock.patch.object(io, "open", side_effect=no_open):
            for tool, tool_input in self.FORBIDDEN:
                with self.subTest(tool=tool):
                    self.assertIsNotNone(guard.decide(self.payload(tool, tool_input)))
            self.assertIsNone(guard.decide(self.payload("Read", {"file_path": "README.md"})))


class FailClosedTests(_FakeProject):
    def test_guarded_agent_with_malformed_tool_input_raises(self):
        for payload in (
            {"agent_type": "project-developer", "tool_name": "Bash"},
            {"agent_type": "project-developer", "tool_name": "Bash", "tool_input": "rm x"},
            {"agent_type": "project-developer", "tool_name": "Bash", "tool_input": {"command": 5}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(guard.GuardError):
                    guard.decide(payload)

    def test_main_returns_2_on_any_error(self):
        for raw in (b"not json", b"[]", b"\xff\xfe\x00", json.dumps(
            {"agent_type": "project-reviewer", "tool_name": "Read"}
        ).encode()):
            with self.subTest(raw=raw):
                err = io.StringIO()
                self.assertEqual(guard.main(stdin=io.BytesIO(raw), stderr=err), 2)
                self.assertIn("could not decide", err.getvalue())


class SubprocessTests(_FakeProject):
    """End to end: the script run the way the hook runs it, stdin JSON in,
    exit code and stdout/stderr out."""

    def run_hook(self, raw):
        return subprocess.run(
            [sys.executable, SCRIPT],
            input=raw,
            capture_output=True,
            cwd=self.root,
            env={**os.environ, "CLAUDE_PROJECT_DIR": self.root},
            timeout=30,
        )

    def test_block_exits_2_with_reason_on_stderr(self):
        raw = json.dumps(self.payload("Bash", {"command": "cat .env"})).encode()
        result = self.run_hook(raw)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"Blocked by Apply101 agent_file_guard", result.stderr)

    def test_allow_exits_0_silently(self):
        # No stdout at all: the hook never emits an "allow" permission
        # decision, so normal permission prompts still apply.
        for payload in (
            self.payload("Bash", {"command": "git status --short"}),
            self.payload("Read", {"file_path": ".env"}, agent_type=None),
        ):
            with self.subTest(payload=payload):
                result = self.run_hook(json.dumps(payload).encode())
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")

    def test_malformed_input_exits_2(self):
        for raw in (b"", b"{not json", b"\xff\xfe"):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_hook(raw).returncode, 2)

    def test_utf8_input_is_read(self):
        payload = self.payload("Read", {"file_path": "backend/notlar_\u015f\u00fc.md"})
        result = self.run_hook(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(result.returncode, 0)


def _git_bash():
    """Git Bash on Windows (what Claude Code runs "shell": "bash" hooks
    with), plain bash elsewhere; None when unavailable."""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if not git:
        return None
    directory = os.path.dirname(os.path.realpath(git))
    for _ in range(3):
        for rel in (("usr", "bin", "bash.exe"), ("bin", "bash.exe")):
            candidate = os.path.join(directory, *rel)
            if os.path.isfile(candidate):
                return candidate
        directory = os.path.dirname(directory)
    return None


class WrapperTests(_FakeProject):
    """agent_file_guard.sh run by bash the way the hook runs it, against a
    synthetic project whose venv interpreter is broken (exits 9)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bash = _git_bash()
        hooks = os.path.join(cls.root, ".claude", "hooks")
        os.makedirs(hooks)
        shutil.copy(SCRIPT, hooks)
        broken = os.path.join(cls.root, ".venv", "bin", "python")
        os.makedirs(os.path.dirname(broken))
        with open(broken, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\necho ran >> "$0.ran"\nexit 9\n')
        os.chmod(broken, 0o755)
        cls.broken_marker = broken + ".ran"
        cls.empty_bin = os.path.join(cls.root, "empty_bin")
        os.makedirs(cls.empty_bin)
        # Always on PATH: cat (the wrapper reads stdin with it, as in a real
        # hook shell) and no Python. Git's usr/bin on Windows; elsewhere a
        # directory holding only a cat symlink, since /usr/bin has python3.
        if os.name == "nt":
            cls.tools_bin = os.path.dirname(cls.bash) if cls.bash else ""
        else:
            cls.tools_bin = os.path.join(cls.root, "tools_bin")
            os.makedirs(cls.tools_bin)
            cat = shutil.which("cat")
            if cat:
                os.symlink(cat, os.path.join(cls.tools_bin, "cat"))
        # A project directory without the guard file.
        cls.no_guard_root = os.path.join(cls.root, "no_guard")
        os.makedirs(cls.no_guard_root)

    def setUp(self):
        if not self.bash:
            self.skipTest("bash not available")

    def broken_runs(self):
        if not os.path.exists(self.broken_marker):
            return 0
        with open(self.broken_marker, encoding="utf-8") as f:
            return len(f.read().split())

    def run_wrapper(self, payload, path, project=None):
        if project is None:
            project = self.root.replace("\\", "/")
        return subprocess.run(
            [self.bash, WRAPPER],
            input=json.dumps(payload).encode(),
            capture_output=True,
            cwd=self.root,
            env={
                **os.environ,
                "CLAUDE_PROJECT_DIR": project,
                "PATH": os.pathsep.join((path, self.tools_bin)),
            },
            timeout=60,
        )

    def test_guard_decisions_pass_through(self):
        python_dir = os.path.dirname(sys.executable)
        # Claude Code passes C:/... on Windows; the native C:\... form must work too.
        for project in sorted({self.root.replace("\\", "/"), self.root}):
            with self.subTest(project=project):
                before = self.broken_runs()
                blocked = self.run_wrapper(
                    self.payload("Bash", {"command": "cat .env"}), python_dir, project
                )
                self.assertEqual(blocked.returncode, 2, blocked.stderr)
                self.assertIn(b"Blocked by Apply101 agent_file_guard (project subagent rule)", blocked.stderr)
                self.assertIn(b"cat references a protected path", blocked.stderr)
                self.assertGreater(self.broken_runs(), before, "broken venv python was not tried")
                for payload in (
                    self.payload("Bash", {"command": "git status --short"}),
                    self.payload("Read", {"file_path": ".env"}, agent_type=None),
                ):
                    result = self.run_wrapper(payload, python_dir, project)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(result.stderr, b"")

    def test_missing_guard_file_only_blocks_project_agents(self):
        python_dir = os.path.dirname(sys.executable)
        project = self.no_guard_root.replace("\\", "/")
        blocked = self.run_wrapper(self.payload("Read", {"file_path": "README.md"}), python_dir, project)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn(b"no Python could run agent_file_guard.py", blocked.stderr)
        allowed = self.run_wrapper(
            self.payload("Read", {"file_path": "README.md"}, agent_type=None), python_dir, project
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_large_input_is_fast(self):
        # bash's read builtin took about 4 s per MB on a pipe; the hook timeout is 15 s.
        payload = self.payload(
            "Write", {"file_path": "notes.md", "content": "a" * (2 * 1024 * 1024)}, agent_type=None
        )
        started = time.monotonic()
        result = self.run_wrapper(payload, os.path.dirname(sys.executable))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 5)

    def test_without_python_only_project_agents_are_blocked(self):
        for agent in GUARDED:
            with self.subTest(agent=agent):
                result = self.run_wrapper(
                    self.payload("Read", {"file_path": "README.md"}, agent), self.empty_bin
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"no Python could run agent_file_guard.py", result.stderr)
        quoted = {"file_path": "notes.json", "content": '{"agent_type": "project-developer"}'}
        for agent in UNGUARDED:
            for tool, tool_input in (("Read", {"file_path": ".env"}), ("Write", quoted)):
                with self.subTest(agent=agent, tool=tool):
                    result = self.run_wrapper(self.payload(tool, tool_input, agent), self.empty_bin)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")

    def test_wrapper_has_lf_line_endings(self):
        with open(WRAPPER, "rb") as f:
            self.assertNotIn(b"\r", f.read())


class SettingsTests(unittest.TestCase):
    def test_settings_register_the_guard_fail_closed(self):
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
        entries = [
            (entry, hook)
            for entry in settings["hooks"]["PreToolUse"]
            for hook in entry["hooks"]
            if "agent_file_guard" in hook.get("command", "")
        ]
        self.assertEqual(len(entries), 1)
        entry, hook = entries[0]
        self.assertEqual(
            set(entry["matcher"].split("|")),
            {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "Grep", "Glob",
             "Bash", "PowerShell", "Agent", "Task"},
        )
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["onFailure"], "block")
        self.assertEqual(hook["shell"], "bash")
        self.assertLessEqual(hook["timeout"], 30)
        self.assertEqual(hook["command"], 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/agent_file_guard.sh"')
        with open(WRAPPER, encoding="utf-8") as f:
            wrapper = f.read()
        self.assertIn('"$CLAUDE_PROJECT_DIR/.venv/Scripts/python.exe"', wrapper)
        self.assertIn('"$CLAUDE_PROJECT_DIR/.venv/bin/python"', wrapper)
        self.assertIn('guard="$CLAUDE_PROJECT_DIR/.claude/hooks/agent_file_guard.py"', wrapper)
        self.assertTrue(os.path.isfile(SCRIPT))


if __name__ == "__main__":
    unittest.main()
