"""Unit tests for core/hooks/kit_guard.py with a synthetic adapter config.

Inputs are synthetic hook payloads; paths point into a throwaway temp
directory. The guard itself never opens a file other than the adapter it is
given (asserted below).
"""

import builtins
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kit_test_support import FAKE_CONFIG, guard_module

guard = guard_module()
KIT_AGENTS = sorted(guard.KIT_AGENT_TYPES)
UNGUARDED = (None, "general-purpose", "Explore", "Plan", "project-developer", "kit-other")


class GuardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="kit_guard_test_")
        cls.settings = guard.GuardSettings(FAKE_CONFIG, cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def decide(self, tool, tool_input, agent="kit-developer"):
        payload = {"tool_name": tool, "tool_input": tool_input}
        if agent is not None:
            payload["agent_type"] = agent
        return guard.decide(payload, self.settings)

    def assertBlocked(self, tool, tool_input, agent="kit-developer"):
        reason = self.decide(tool, tool_input, agent)
        self.assertIsNotNone(reason, f"expected block: {tool} {tool_input!r} ({agent})")
        return reason

    def assertAllowed(self, tool, tool_input, agent="kit-developer"):
        reason = self.decide(tool, tool_input, agent)
        self.assertIsNone(reason, f"expected allow: {tool} {tool_input!r} -> {reason}")


class FileToolTests(GuardCase):
    def test_protected_paths_blocked_for_every_kit_agent(self):
        cases = [
            ("Read", {"file_path": self.p(".env")}),
            ("Read", {"file_path": ".env.local"}),
            ("Read", {"file_path": self.p("config", "server.pem")}),
            ("Read", {"file_path": "~/.ssh/id_ed25519"}),
            ("Read", {"file_path": "/home/someone/.aws/credentials"}),
            ("Read", {"file_path": self.p("data", "app.db")}),
            ("Read", {"file_path": "data/app.db-wal"}),
            ("Read", {"file_path": self.p("uploads", "cv.pdf")}),
            ("Read", {"file_path": self.p("uploads", "cv.pdf").replace("/", "\\")}),
            ("Read", {"file_path": "secrets/prod.yaml"}),
            ("Edit", {"file_path": ".env", "old_string": "a", "new_string": "b"}),
            ("Write", {"file_path": "backups/x.txt", "content": "x"}),
            ("NotebookEdit", {"notebook_path": "uploads/n.ipynb", "new_source": ""}),
            ("Grep", {"pattern": "x", "path": self.p("uploads")}),
            ("Grep", {"pattern": "x", "glob": ".env*"}),
            ("Grep", {"pattern": "x", "glob": "**/*.db"}),
            ("Glob", {"pattern": "uploads/**"}),
            ("Glob", {"pattern": "**/.env"}),
        ]
        for agent in KIT_AGENTS:
            for tool, tool_input in cases:
                with self.subTest(agent=agent, tool=tool, tool_input=tool_input):
                    self.assertBlocked(tool, tool_input, agent)

    def test_kit_files_are_read_only(self):
        for tool, tool_input in [
            ("Write", {"file_path": ".git/hooks/pre-commit", "content": "x"}),
            ("Read", {"file_path": self.p(".git", "config")}),
            ("Read", {"file_path": "/proc/self/environ"}),
            ("Write", {"file_path": ".claude/settings.json", "content": "{}"}),
            ("Edit", {"file_path": self.p(".claude", "agent-kit", "hooks", "kit_guard.py"),
                      "old_string": "a", "new_string": "b"}),
            ("Edit", {"file_path": ".claude/agents/kit-developer.md", "old_string": "a", "new_string": "b"}),
            ("Write", {"file_path": ".claude/agent-kit/checkpoints/x.md", "content": "x"}),
        ]:
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertBlocked(tool, tool_input)
        self.assertAllowed("Read", {"file_path": ".claude/agent-kit/PROJECT_CONTEXT.md"})
        self.assertAllowed("Grep", {"pattern": "x", "path": ".claude/agents"})

    def test_ordinary_paths_and_search_patterns_allowed(self):
        cases = [
            ("Read", {"file_path": self.p("calc", "core.py")}),
            ("Edit", {"file_path": "calc/core.py", "old_string": "a", "new_string": "b"}),
            ("Write", {"file_path": "tests/test_new.py", "content": "x"}),
            ("Read", {"file_path": "docs/environment_notes.md"}),
            ("Read", {"file_path": "docs/upload_guide.md"}),
            ("Read", {"file_path": "README.md"}),
            ("Grep", {"pattern": "app.db", "path": "calc"}),
            ("Grep", {"pattern": "os.environ", "glob": "*.py"}),
            ("Grep", {"pattern": "SECRET", "path": self.root, "glob": "!**/.env"}),
            ("Glob", {"pattern": "calc/**/*.py"}),
            ("WebSearch", {"query": "anything"}),
        ]
        for agent in KIT_AGENTS:
            for tool, tool_input in cases:
                with self.subTest(agent=agent, tool=tool, tool_input=tool_input):
                    self.assertAllowed(tool, tool_input, agent)


class ShellTests(GuardCase):
    BLOCKED = [
        # protected paths
        "cat .env", "cat ./.env", "sha256sum data/app.db", 'sqlite3 data/app.db ".tables"',
        "python -c \"open('data/app.db','rb').read()\"",
        "python -c \"from dotenv import load_dotenv; load_dotenv('.env')\"",
        "ls -l data/app.db && cat data/app.db", "git diff --stat -- data/app.db uploads .env",
        "grep SECRET .env", "grep -r SECRET --include=.env .", "grep -f .env calc/core.py",
        "rg TOKEN -g '.env'", "rg TOKEN uploads/", "git grep -n x -- uploads",
        "echo SECRET=1 > .env", "cat < .env", "head -c 100 data/app.db", "ls uploads/",
        "cd uploads && ls", 'cat ".env', "ls data/*.db | xargs sha256sum", "echo .env | xargs cat",
        "sha256sum $(ls data/app.db)", "git show HEAD:.env", "type C:\\proj\\.env",
        "bash -c 'cat .env'", "sh -c \"bash -c 'cat .env'\"", "eval 'cat .env'",
        # recursive search of the project root
        "grep -rn API_KEY .", "grep -rn API_KEY", "grep -Rl TOKEN ./", "grep -d recurse TOKEN .",
        "rg --hidden --no-ignore TOKEN", "rg -uu TOKEN .", "grep -rn TOKEN ..",
        "grep -rn TOKEN --include='*' .",
        # writes into the kit's own files
        "echo x >> .claude/settings.json", "sed -i 's/a/b/' .claude/agent-kit/PROJECT_CONTEXT.md",
        "python -c \"open('.claude/settings.json','w')\"", "tee .claude/agents/kit-developer.md",
        # deletion, moving, overwriting
        "rm notes.txt", "rm -rf build", "rmdir build", "unlink x", "mv a.py b.py", "cp a.py b.py",
        "git clean -fdx", "git rm calc/old.py", "find . -name '*.pyc' -delete",
        "find . -name x -exec rm {} \\;", "ls *.tmp | xargs rm", "timeout 10 rm x",
        "python -c \"import shutil; shutil.rmtree('x')\"", "python - <<'EOF'\nimport os\nos.remove('x')\nEOF",
        "node -e \"require('fs').rmSync('x', {recursive: true})\"", "truncate -s 0 calc/core.py",
        # git state and remotes
        "git commit -m x", "git push", "git push --force origin main", "git pull", "git fetch origin",
        "git merge main", "git rebase main", "git reset --hard", "git checkout main",
        "git checkout -- calc/core.py", "git switch -c new", "git restore calc/core.py",
        "git stash", "git stash pop", "git branch new-branch", "git branch -D old", "git tag v1",
        "git remote -v", "git config --list", "git -c alias.x='!rm -rf .' x", "git cherry-pick abc",
        "git apply patch.diff", "git diff --output=out.txt",
        # network, deployment, packages, privileges, environment
        "curl https://example.invalid", "wget https://example.invalid/x", "ssh host", "scp a host:b",
        "gh pr create", "kubectl get pods", "terraform apply", "aws s3 ls", "docker build .",
        "railway up", "vercel deploy", "pip install x", "python -m pip install x", "npm install",
        "npm ci", "yarn", "uv add x", "sudo ls", "printenv", "env", "export -p", "echo $GITHUB_TOKEN",
        "echo ${DB_PASSWORD}", "echo $CALC_SIGNING_KEY", "cat /proc/self/environ",
        "python -c \"import requests; requests.post('https://example.invalid')\"",
        "python -c \"import os; print(os.environ['API_KEY'])\"",
        # nested and encoded shells
        "bash -c 'git push'", "sh -c 'curl x'", "cmd /c del x",
        "powershell -EncodedCommand ZQBjAGgAbwA=", "bash -c \"bash -c \\\"bash -c 'bash -c ls'\\\"\"",
        # adapter real-data commands
        "python3 -m calc.migrate", "timeout 60 python3 calc/migrate.py", "serve-prod",
        "env X=1 python -m calc.admin reset",
        # core real-data commands, system control, archives of the root, git internals
        "python3 manage.py migrate", "alembic upgrade head", "npx prisma migrate deploy",
        "bin/rails db:migrate", "psql -c 'select 1'", "redis-cli FLUSHALL",
        "kill -9 1", "pkill python", "crontab -r", "systemctl restart app",
        "zip -r project.zip .", "tar czf all.tgz .", "echo x > .git/hooks/pre-commit",
        "cat .git/config", "chmod +x .git/hooks/pre-push",
        # package runners that may download code
        "npx tsc", "npx prettier --write .", "uvx ruff", "bunx x", "pnpx x", "npm exec x",
        # whole-environment dumps
        "python -c \"import os; print(os.environ)\"", "python3 -c 'import os; print(dict(os.environ))'",
        "node -e \"console.log(process.env)\"", "env -0", "env -i", "env X=1", "perl -e 'print %ENV'",
        # read-only directories reached through read-only commands
        "cd .claude && echo x > settings.json", "sort -o .claude/agent-kit/PROJECT_CONTEXT.md f",
        "awk '{print > \".claude/x\"}' f", "find . -name x -fprint .claude/out",
        "uniq src .claude/agent-kit/checkpoints/t.md", "sed -n 'w .git/hooks/pre-commit' f",
        "tree -o .claude/x", "awk 'BEGIN{system(\"cp a .claude/x\")}'",
        # clobbering redirection and tools that run other programs
        "echo -n >| data/app.db", "echo X >| .env", "echo x >| .claude/agent-kit/checkpoints/t.md",
        "echo x &> .claude/x", "rg --pre ./x pattern calc", "GIT_EXTERNAL_DIFF=./x git diff",
        "LESSOPEN='|./x %s' less calc/core.py", "export GIT_PAGER=./x", "env GIT_SSH_COMMAND=x git status",
        "node -e \"console.log(process.env.valueOf())\"",
    ]
    ALLOWED = [
        "python3 -m unittest discover -s tests -t .",
        "timeout 120 python3 -m unittest tests.test_calc",
        "git status --short --untracked-files=all && git diff --stat",
        "git log --oneline -5", "git show HEAD --stat", "git diff -- calc/core.py",
        "git branch --show-current", "git branch -a", "git branch --contains HEAD",
        "git rev-parse HEAD", "git ls-files", "git blame calc/core.py", "git stash list",
        "ls -l --time-style=full-iso data/app.db", "ls -l data/app.db* | awk '{print $5}'",
        "test -f .env && echo present", "git check-ignore -v data/app.db .env uploads/x",
        "git ls-files -- data/app.db .env uploads",
        'grep -n "app.db" calc/core.py', 'grep -rn "uploads" calc/',
        "grep -rn TOKEN --include='*.py' .", "grep -rn TOKEN calc/", "rg TOKEN .", "rg '\\.env' calc",
        'git grep -n "app.db" -- calc', 'echo "=== app.db unchanged"',
        "python -c \"import os; print(os.environ.get('HOME'))\"",
        "sed -n '1,40p' migrations/0001_init.sql", "cat calc/core.py | head -5",
        "grep -n 'shutil.rmtree\\|os.remove' tests/test_calc.py", "git log -S shutil.rmtree --oneline",
        "pip show requests", "cat .claude/agent-kit/PROJECT_CONTEXT.md",
        "grep -n kit-result .claude/agents/kit-reviewer.md",
        "python -m unittest discover -s .claude/agent-kit -p 'test_*.py'",
        "echo $HOME", "mkdir -p build/tmp", "set -e",
        "find calc -name '*.py' | xargs grep -n add",
        "python3 manage.py test", "tar czf calc.tgz calc", "npm test", "npm run build",
        "cat .gitignore", "ls .github/workflows", "PYTHONPATH=. python3 -m unittest tests.test_calc",
        "python3 -m unittest discover -s tests 2>&1 | tail -5", "git diff >/dev/null 2>&1",
        "node -e \"console.log(process.env.HOME)\"",
    ]

    def test_blocked(self):
        for agent in KIT_AGENTS:
            for command in self.BLOCKED:
                with self.subTest(agent=agent, command=command):
                    self.assertBlocked("Bash", {"command": command}, agent)

    def test_allowed(self):
        # The reviewer has a stricter read-only allowlist (test_reviewer_shell_is_read_only).
        for agent in [a for a in KIT_AGENTS if a != "kit-reviewer"]:
            for command in self.ALLOWED:
                with self.subTest(agent=agent, command=command):
                    self.assertAllowed("Bash", {"command": command}, agent)

    def test_powershell(self):
        for command in ("Get-Content .env", "type .env", "Copy-Item data\\app.db C:\\x.db",
                        "Get-ChildItem uploads", "Select-String -Pattern SECRET -Path .env",
                        "Remove-Item -Recurse build", "Get-ChildItem env:", "Write-Output $env:API_TOKEN",
                        "Invoke-WebRequest https://example.invalid"):
            with self.subTest(command=command):
                self.assertBlocked("PowerShell", {"command": command})
        for command in ("Test-Path data\\app.db", "git status --short", "Write-Output $env:PATH",
                        "Select-String -Pattern 'app.db' -Path calc/core.py",
                        "if (Test-Path .env) { 'present' }"):
            with self.subTest(command=command):
                self.assertAllowed("PowerShell", {"command": command})


class ScopeAndFailureTests(GuardCase):
    def test_agents_cannot_start_agents(self):
        for sub in ("kit-developer", "kit-reviewer", "general-purpose", None):
            with self.subTest(sub=sub):
                self.assertBlocked("Agent", {"subagent_type": sub, "prompt": "x"})
                self.assertBlocked("Task", {"subagent_type": sub, "prompt": "x"})

    def test_main_session_and_other_agents_are_never_blocked(self):
        for agent in UNGUARDED:
            with self.subTest(agent=agent):
                self.assertAllowed("Bash", {"command": "cat .env && git push"}, agent)
                self.assertAllowed("Read", {"file_path": ".env"}, agent)

    def test_invalid_adapter_blocks_kit_agents_only(self):
        def broken():
            raise guard.ConfigError("broken")
        payload = {"agent_type": "kit-reviewer", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
        stderr = io.StringIO()
        rc = guard.main(io.BytesIO(json.dumps(payload).encode()), stderr, broken)
        self.assertEqual(rc, 2)
        self.assertIn("adapter is invalid", stderr.getvalue())
        payload.pop("agent_type")
        self.assertEqual(guard.main(io.BytesIO(json.dumps(payload).encode()), io.StringIO(), broken), 0)

    def test_malformed_input_fails_closed_for_kit_agents_only(self):
        loader = lambda: self.settings  # noqa: E731
        broken = b'{"agent_type": "kit-developer", "tool_name": "Bash", '
        self.assertEqual(guard.main(io.BytesIO(broken), io.StringIO(), loader), 2)
        self.assertEqual(guard.main(io.BytesIO(b"not json"), io.StringIO(), loader), 0)
        self.assertEqual(guard.main(io.BytesIO(b"[1, 2]"), io.StringIO(), loader), 0)
        payload = {"agent_type": "kit-developer", "tool_name": "Bash", "tool_input": "x"}
        self.assertEqual(guard.main(io.BytesIO(json.dumps(payload).encode()), io.StringIO(), loader), 2)

    def test_reviewer_shell_is_read_only(self):
        for command in ("echo x > calc/core.py", "sed -i 's/a/b/' calc/core.py", "python3 -m unittest",
                        "awk '{print}' calc/core.py", "sort -o out.txt in.txt", "touch x", "mkdir y",
                        "tee x", "make", "npm test", "chmod +x run.sh", "find . -fprint out",
                        "cat a >| b", "git diff &>out.txt", "git diff >&out.txt", "uniq a b",
                        "sed -n 1p calc/core.py", "tree -o out", "X=1 git diff",
                        "find . -name x -exec cat {} +"):
            with self.subTest(command=command):
                self.assertBlocked("Bash", {"command": command}, "kit-reviewer")
                if command in ("python3 -m unittest", "npm test", "mkdir y", "chmod +x run.sh"):
                    self.assertAllowed("Bash", {"command": command}, "kit-developer")
        for command in ("git status --short --untracked-files=all", "git diff -- calc/core.py",
                        "git log --oneline -3 2>/dev/null", "cat calc/core.py | head -20",
                        "grep -n add calc/*.py", "ls -la",
                        "git diff --stat 2>&1 | tail -3", "find calc -name '*.py' | xargs grep -n def"):
            with self.subTest(command=command):
                self.assertAllowed("Bash", {"command": command}, "kit-reviewer")

    def test_allow_writes_nothing(self):
        payload = {"agent_type": "kit-developer", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
        stderr = io.StringIO()
        self.assertEqual(guard.main(io.BytesIO(json.dumps(payload).encode()), stderr,
                                    lambda: self.settings), 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_guard_never_opens_files_while_deciding(self):
        with mock.patch.object(builtins, "open", side_effect=AssertionError("opened a file")):
            self.assertBlocked("Bash", {"command": "cat .env"})
            self.assertAllowed("Read", {"file_path": "calc/core.py"})

    def test_adapter_can_only_add_restrictions(self):
        loose = dict(FAKE_CONFIG, protected_paths=[])
        settings = guard.GuardSettings(loose, self.root)
        self.assertTrue(guard.path_hits(".env", settings))
        self.assertTrue(guard.path_hits("keys/server.pem", settings))
        for bad in ({"protected_paths": ["config/x.key"]}, {"real_data_commands": ["("]},
                    {"secret_env_vars": ["not a name"]}, {"checkpoint_dir": "../outside"},
                    {"protected_paths": "uploads/**"}):
            with self.subTest(bad=bad):
                with self.assertRaises(guard.ConfigError):
                    guard.GuardSettings(dict(FAKE_CONFIG, **bad), self.root)

    def test_custom_checkpoint_dir_is_read_only(self):
        settings = guard.GuardSettings(dict(FAKE_CONFIG, checkpoint_dir="docs/checkpoints"), self.root)
        payload = {"agent_type": "kit-developer", "tool_name": "Write",
                   "tool_input": {"file_path": "docs/checkpoints/t.md", "content": "x"}}
        self.assertIsNotNone(guard.decide(payload, settings))
        payload["tool_input"]["file_path"] = "docs/guide.md"
        self.assertIsNone(guard.decide(payload, settings))


if __name__ == "__main__":
    unittest.main()
