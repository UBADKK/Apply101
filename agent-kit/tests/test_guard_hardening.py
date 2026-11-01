"""Regression tests for guard bypasses closed in the v1 hardening round:
sed's program-running and file commands, sort --compress-program,
JavaScript environment access through brackets or methods, and PowerShell
$env: reads and assignments. Synthetic hook inputs only."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from kit_test_support import FAKE_CONFIG, guard_module

guard = guard_module()
KIT_AGENTS = sorted(guard.KIT_AGENT_TYPES)


class HardeningCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="kit_guard_hardening_")
        cls.settings = guard.GuardSettings(FAKE_CONFIG, cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, command, agent="kit-developer", tool="Bash"):
        return guard.decide({"agent_type": agent, "tool_name": tool,
                             "tool_input": {"command": command}}, self.settings)

    def check(self, blocked, allowed, tool="Bash", agents=("kit-developer",)):
        for agent in agents:
            for command in blocked:
                with self.subTest(agent=agent, blocked=command):
                    self.assertIsNotNone(self.decide(command, agent, tool), command)
            for command in allowed:
                with self.subTest(agent=agent, allowed=command):
                    self.assertIsNone(self.decide(command, agent, tool), command)


class SedTests(HardeningCase):
    BLOCKED = [
        # e: runs a command (and so would skip the adapter's real-data patterns)
        "sed -n '1e python3 -m calc.migrate' /dev/null",
        "sed -n '1e serve-prod' /dev/null",
        "sed 'e date' calc/core.py",
        "sed -n '$!e date' calc/core.py",
        "sed -n '/add/e date' calc/core.py",
        "sed 's/.*/date/e' calc/core.py",
        "sed 's|.*|date|ge' calc/core.py",
        "sed --expression='1e date' calc/core.py",
        "sed --expression '1e date' calc/core.py",
        "sed -e p -e 'e date' calc/core.py",
        "sed -n -e1edate calc/core.py",
        "sed -E -n '1e date' calc/core.py",
        "sed -n '1p;2e date' calc/core.py",
        "sed -n '1p\n2e date' calc/core.py",
        # file reads and writes the guard cannot attribute
        "sed -n '1w out.txt' calc/core.py",
        "sed 's/a/b/w out.txt' calc/core.py",
        "sed '1r notes.txt' calc/core.py",
        "sed '1R notes.txt' calc/core.py",
        "sed -n '1W out.txt' calc/core.py",
        # scripts the guard cannot read or parse
        "sed -f script.sed calc/core.py",
        "sed --file=script.sed calc/core.py",
        "sed -n '1{e date' -e '}' calc/core.py",
        # wrappers and nesting
        "find calc -name '*.py' | xargs sed -n '1e date'",
        "bash -c \"sed -n '1e date' calc/core.py\"",
        "timeout 5 sed 's/x/date/e' calc/core.py",
    ]
    ALLOWED = [
        "sed -n '1,40p' calc/core.py",
        "sed -n '/def add/,/return/p' calc/core.py",
        "sed -n '$p' calc/core.py",
        "sed -n 10p calc/core.py",
        "sed '1d' calc/core.py",
        "sed '/^#/d' calc/core.py",
        "sed 's/a/b/g' calc/core.py",
        "sed 's|a|b|' calc/core.py",
        "sed 's/x/y/2' calc/core.py",
        "sed 's/x/y/gI' calc/core.py",
        "sed -E 's/(a+)/\\1/' calc/core.py",
        "sed -e 's/a/b/' -e 's/c/d/' calc/core.py",
        "sed -i 's/return a - b/return a + b/' calc/core.py",
        "sed 'y/abc/xyz/' calc/core.py",
        "sed -n '1p;3p' calc/core.py",
        "sed 's/date/time/' calc/core.py",
        "sed 's/e/E/g' calc/core.py",
        "sed '1i # header' calc/core.py",
        "git diff | sed -n '1,20p'",
        "sed --sandbox -n '1p' calc/core.py",
    ]

    def test_sed(self):
        self.check(self.BLOCKED, self.ALLOWED)


class SortTests(HardeningCase):
    def test_sort_compress_program(self):
        self.check(
            ["sort --compress-program=sh big.txt", "sort --compress-program sh big.txt",
             "sort --compress-prog=./x big.txt", "sort -S 1 --compress-program=python3 big.txt",
             "ls | sort --compress-program=./run.sh", "sort --co=./x big.txt"],
            ["sort big.txt", "sort -u -k2 -n big.txt", "ls | sort", "sort -o sorted.txt big.txt",
             "sort --check big.txt", "git log --oneline | sort -r"],
        )
        self.assertIsNotNone(self.decide("sort --compress-program=sh big.txt", "kit-reviewer"))


class JavaScriptEnvTests(HardeningCase):
    def test_bracket_and_method_access(self):
        self.check(
            [
                "node -e \"console.log(process.env['valueOf']())\"",
                "node -e 'console.log(process.env[\"toString\"]())'",
                "node -e \"console.log(process.env[`valueOf`]())\"",
                "node -e \"const k='valueOf'; console.log(process.env[k]())\"",
                "node -e \"for (const k in process.env) console.log(k)\"",
                "node -e \"console.log(process['env'])\"",
                "node -e \"console.log(process['env']['valueOf']())\"",
                "node -e \"console.log(process.env.valueOf())\"",
                "node -e \"console.log(process.env.hasOwnProperty.call)\"",
                "node -e \"console.log(Object.entries(process.env))\"",
                "node -e \"console.log(process.env['API_TOKEN'])\"",
                "node -e \"console.log(process['env']['API_TOKEN'])\"",
                "node -e \"console.log(process.env.CALC_SIGNING_KEY)\"",
                "deno eval \"console.log(Deno.env.toObject())\"",
                "bun -e \"console.log(Bun.env)\"",
            ],
            [
                "node -e \"console.log(process.env.HOME)\"",
                "node -e \"console.log(process.env['NODE_ENV'])\"",
                "node -e \"console.log(process.env.PATH.split(':').length)\"",
                "grep -rn \"process.env\" calc/",
                "npm test",
            ],
        )


class SameClassTests(HardeningCase):
    """Neighbours of the four bypasses: Python environment access by name
    and git grep -O, which runs a program from a read-only command."""

    def test_python_env_by_name_and_git_grep_pager(self):
        self.check(
            ["python3 -c \"import os; print(getattr(os, 'environ'))\"",
             "python3 -c \"import os; print(vars(os)['environ'])\"",
             "python3 -c \"import os; print(os.environb)\"",
             "git grep -O'python3 -m calc.migrate' add", "git grep --open-files-in-pager=vim add",
             "git grep -nOcat add"],
            ["python3 -c \"import os; print(os.environ.get('HOME'))\"",
             "python3 -c \"import os; print(os.environ['PATH'])\"",
             "git grep -n add -- calc", "git grep -c add"],
        )
        self.assertIsNotNone(self.decide("git grep -O./x add", "kit-reviewer"))


class PowerShellEnvTests(HardeningCase):
    def test_reads_and_listings(self):
        self.check(
            [
                "Write-Output ${env:API_TOKEN}",
                "Write-Output $env:CALC_SIGNING_KEY",
                "Get-Content env:API_TOKEN",
                "Get-Content Env:\\CALC_SIGNING_KEY",
                "(Get-Item env:DB_PASSWORD).Value",
                "Get-Item -Path Env:*",
                "Get-Content env:*",
                "Set-Location env:; Get-ChildItem",
                "cd env:; dir",
                "[Environment]::GetEnvironmentVariable('API_TOKEN')",
                "[System.Environment]::GetEnvironmentVariable($name)",
                "[System.Environment]::GetEnvironmentVariables()",
                "[Environment]::ExpandEnvironmentVariables('%API_TOKEN%')",
            ],
            [
                "Write-Output $env:PATH",
                "Write-Output ${env:USERPROFILE}",
                "Get-Content env:PATH",
                "Test-Path env:PATH",
                "[Environment]::GetEnvironmentVariable('PATH')",
                "git status --short",
            ],
            tool="PowerShell",
        )

    def test_assignments(self):
        self.check(
            [
                "$env:GIT_PAGER = './x'",
                "$env:LESSOPEN='|./x %s'; Get-Content calc/core.py",
                "${env:GIT_EXTERNAL_DIFF} = './x'; git diff",
                "$env:LD_PRELOAD += 'x.so'",
                "Set-Item env:GIT_PAGER ./x",
                "Set-Item -Path Env:EDITOR -Value ./x",
                "New-Item -Path env:NODE_OPTIONS -Value '--require ./x'",
                "[Environment]::SetEnvironmentVariable('GIT_PAGER', './x')",
                "[Environment]::SetEnvironmentVariable($n, './x')",
            ],
            [
                "$env:PYTHONPATH = '.'; python -m unittest discover -s tests",
                "$env:NODE_ENV='test'; npm test",
                "if ($env:CI -eq 'true') { 'ci' }",
            ],
            tool="PowerShell",
        )

    def test_nested_powershell_from_bash(self):
        self.check(
            ["pwsh -Command '$env:GIT_PAGER=\"./x\"; git log'",
             "powershell -Command \"Get-Content env:API_TOKEN\""],
            ["pwsh -Command 'Write-Output $env:PATH'", "grep -n \"env:\" .github/workflows/ci.yml"],
        )


if __name__ == "__main__":
    unittest.main()
