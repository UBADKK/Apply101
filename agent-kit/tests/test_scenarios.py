"""The five required end-to-end scenarios, each in a throwaway fake project.

The LLM agents are simulated: the tests play the orchestrator (the installed
kit.py CLI), the developer (file edits and test runs in the fake project),
the agents' reports (kit-result blocks) and their tool calls (through the
installed guard hook). What is verified is everything deterministic around
the agents: gates, limits, scope checks, checkpoints and the guard.
"""

import unittest

from kit_test_support import FakeProjectCase, git

FIXED_ADD = "def add(a, b):\n    return a + b\n"
FIXED_MUL = "def mul(a, b):\n    return a * b\n"
CALC_WIP = "def add(a, b):\n    return a - b  # the user's work in progress\n"
EXIT_REFUSED = 3


class Scenario1SmallBugFix(FakeProjectCase):
    """Small bug fix: the developer implements, the reviewer approves."""

    def test_bug_fix_is_implemented_and_approved(self):
        self.kit("checkpoint", "new", "--task", "fix-add", "--goal", "Fix add() which subtracts",
                 "--scope", "calc/core.py", "--scope", "tests/test_calc.py",
                 "--criterion", "add(2, 3) == 5", "--criterion", "a regression test covers add",
                 expect=0)
        self.assertEqual(self.state("fix-add")["state"]["Risk flags"], "-")
        self.kit("checkpoint", "event", "fix-add", "develop", expect=0)

        dev = "kit-developer"
        self.assertHookAllows(dev, "Read", {"file_path": str(self.root / "calc/core.py")})
        self.assertHookAllows(dev, "Edit", {"file_path": "calc/core.py", "old_string": "a", "new_string": "b"})
        self.assertHookAllows(dev, "Bash", {"command": "python3 -m unittest discover -s tests -t ."})
        self.assertHookAllows(dev, "Bash", {"command": "git status --short && git diff --stat"})
        self.assertHookBlocks(dev, "Bash", {"command": "git commit -am 'fix add'"})

        # Developer: a failing regression test first, then the fix.
        self.write("tests/test_calc.py", self.root.joinpath("tests/test_calc.py").read_text()
                   + "\n    def test_add_regression(self):\n        self.assertEqual(add(2, 3), 5)\n")
        self.assertNotEqual(self.run_project_tests(), 0, "the regression test must fail before the fix")
        core = self.root / "calc/core.py"
        core.write_text(core.read_text().replace("def add(a, b):\n    return a - b\n", FIXED_ADD))
        self.assertEqual(self.run_project_tests(), 0)

        self.kit("checkpoint", "record", "fix-add", "--test",
                 "`python3 -m unittest discover -s tests -t .` -> OK (2 tests)", expect=0)
        self.kit("checkpoint", "event", "fix-add", "developed",
                 "--report", self.report("developer", "implemented"), expect=0)
        cp = self.state("fix-add")
        self.assertEqual(cp["state"]["Phase"], "review")
        self.assertEqual(sorted(cp["Changed files"]), ["calc/core.py", "tests/test_calc.py"])

        self.assertHookBlocks("kit-reviewer", "Bash", {"command": "git add -A"})
        self.assertHookAllows("kit-reviewer", "Bash", {"command": "git diff -- calc/core.py"})
        self.kit("checkpoint", "event", "fix-add", "review",
                 "--report", self.report("reviewer", "approve"), expect=0)
        cp = self.state("fix-add")
        self.assertEqual((cp["state"]["Status"], cp["state"]["Phase"]), ("done", "done"))
        self.kit("checkpoint", "verify", "fix-add", expect=0)
        self.kit("validate", expect=0)
        self.assertEqual(git(self.root, "branch", "--show-current").strip(), self.branch)


class Scenario2MigrationRisk(FakeProjectCase):
    """Migration/data risk: the auditor stops progress until the user decides."""

    def test_auditor_blocks_development_without_user_approval(self):
        self.kit("checkpoint", "new", "--task", "add-email", "--goal",
                 "Add a migration that adds an email column to the users table",
                 "--scope", "migrations/0002_add_email.sql",
                 "--criterion", "a new migration adds users.email", expect=0)
        self.assertIn("data", self.state("add-email")["state"]["Risk flags"])

        self.kit("checkpoint", "event", "add-email", "audit", "--report",
                 self.report("auditor", "needs_user_approval", risk="data, approval_path"),
                 expect=EXIT_REFUSED)
        cp = self.state("add-email")
        self.assertEqual(cp["state"]["Status"], "waiting_user")
        self.assertIn("approval required", cp["state"]["Reason"])

        # Nothing can proceed without the user, and approval needs a note.
        self.kit("checkpoint", "event", "add-email", "develop", expect=EXIT_REFUSED)
        self.kit("checkpoint", "event", "add-email", "approve", expect=EXIT_REFUSED)
        self.assertEqual(self.state("add-email")["state"]["Status"], "waiting_user")

        # The guard keeps every agent away from the real data and its entry points.
        before = (self.root / "data/app.db").stat()
        for agent in ("kit-developer", "kit-auditor", "kit-reviewer", "kit-researcher"):
            self.assertHookBlocks(agent, "Read", {"file_path": str(self.root / "data/app.db")})
        dev = "kit-developer"
        self.assertHookBlocks(dev, "Bash", {"command": "python3 -m calc.migrate --apply"})
        self.assertHookBlocks(dev, "Bash", {"command": "timeout 60 python3 calc/migrate.py"})
        self.assertHookBlocks(dev, "Bash", {"command": "sqlite3 data/app.db 'ALTER TABLE users ADD email'"})
        self.assertHookBlocks(dev, "Bash", {"command": "bash -c 'cp data/app.db /tmp/x.db'"})
        after = (self.root / "data/app.db").stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

        # Only an explicit user decision, recorded by the orchestrator, unlocks it.
        self.kit("checkpoint", "event", "add-email", "approve", "--note",
                 "User approved writing the migration file only; nobody runs it", expect=0)
        self.kit("checkpoint", "event", "add-email", "develop", expect=0)
        self.write("migrations/0002_add_email.sql", "ALTER TABLE users ADD COLUMN email TEXT;\n")
        self.kit("checkpoint", "event", "add-email", "developed",
                 "--report", self.report("developer", "implemented"), expect=0)
        self.assertEqual(self.state("add-email")["state"]["Phase"], "review")

    def test_approval_required_scope_is_gated_even_without_keywords(self):
        self.kit("checkpoint", "new", "--task", "bump-contract", "--goal", "Rename the version constant",
                 "--scope", "calc/contract.py", "--criterion", "constant renamed", expect=0)
        self.kit("checkpoint", "event", "bump-contract", "develop", expect=EXIT_REFUSED)
        cp = self.state("bump-contract")
        self.assertIn("approval_path", cp["state"]["Risk flags"])
        self.assertIn("calc/contract.py", cp["state"]["Reason"])

    def test_approval_does_not_cover_risks_added_later(self):
        self.kit("checkpoint", "new", "--task", "ship-code", "--goal", "Prepare the release notes for deploy",
                 "--criterion", "notes written", expect=EXIT_REFUSED)
        self.kit("checkpoint", "event", "ship-code", "approve", "--note",
                 "User: only a code change, nobody deploys", expect=0)
        # Scope and criteria recorded after the approval bring new risks.
        self.kit("checkpoint", "record", "ship-code", "--scope", "migrations/0002_notes.sql",
                 "--criterion", "add a ledger table", expect=0)
        self.kit("checkpoint", "event", "ship-code", "develop", expect=EXIT_REFUSED)
        reason = self.state("ship-code")["state"]["Reason"]
        self.assertIn("approval_path", reason)
        self.assertIn("data", reason)  # "ledger" is an adapter risk word, found by the rescan
        self.assertIn("migrations/0002_notes.sql", reason)
        self.kit("checkpoint", "event", "ship-code", "approve", "--note",
                 "User approved the notes migration file", expect=0)
        self.kit("checkpoint", "event", "ship-code", "develop", expect=0)

    def test_developer_block_approval_does_not_approve_risks(self):
        self.kit("checkpoint", "new", "--task", "tidy", "--goal", "Tidy the calculator",
                 "--scope", "calc/core.py", "--criterion", "tidy", expect=0)
        self.kit("checkpoint", "event", "tidy", "develop", expect=0)
        self.kit("checkpoint", "event", "tidy", "developed", "--verdict", "blocked", "--note", "unclear",
                 expect=EXIT_REFUSED)
        self.kit("checkpoint", "event", "tidy", "approve", "--note", "User clarified the task", expect=0)
        self.assertIn("approve: covers=- scope=-", self.state("tidy")["Log"])

    def test_audit_runs_are_capped(self):
        self.kit("checkpoint", "new", "--task", "audit-cap", "--goal", "Tidy the calculator",
                 "--scope", "calc/core.py", "--criterion", "tidy", expect=0)
        for _ in range(2):
            self.kit("checkpoint", "event", "audit-cap", "audit", "--verdict", "proceed", expect=0)
        proc = self.kit("checkpoint", "event", "audit-cap", "audit", "--verdict", "proceed",
                        expect=EXIT_REFUSED)
        self.assertIn("at most 2 audit runs", proc.stderr)

    def test_watched_real_data_change_stops_the_task(self):
        self.kit("checkpoint", "new", "--task", "touch", "--goal", "Tidy the calculator",
                 "--scope", "calc/core.py", "--criterion", "tidy", expect=0)
        self.kit("checkpoint", "event", "touch", "develop", expect=0)
        self.write("calc/core.py", FIXED_ADD + "\n\n" + FIXED_MUL)
        self.write("data/app.db", "changed by something\n")
        self.kit("checkpoint", "event", "touch", "developed",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        self.assertIn("watched real-data file changed: data/app.db", self.state("touch")["state"]["Reason"])
        # Real data changed during the task: final, the user cannot approve it away.
        self.kit("checkpoint", "event", "touch", "approve", "--note", "fine", expect=EXIT_REFUSED)


class Scenario3InterruptedTask(FakeProjectCase):
    """Interrupted task: a new session continues from the checkpoint."""

    def test_resume_continues_from_checkpoint(self):
        self.kit("checkpoint", "new", "--task", "fix-mul", "--goal", "Fix mul() which adds",
                 "--scope", "calc/core.py", "--criterion", "mul(2, 3) == 6", expect=0)
        self.kit("checkpoint", "event", "fix-mul", "develop", expect=0)
        # The developer had started editing when the session ended.
        core = self.root / "calc/core.py"
        core.write_text(core.read_text().replace("return a + b", "return a * b  # wip"))

        # New session: only the files on disk and the checkpoint survive.
        out = self.kit("checkpoint", "resume", "fix-mul", expect=0).stdout
        self.assertIn("RESUME OK", out)
        self.assertIn("phase=develop", out)
        self.assertIn("interrupted developer run left changes not yet recorded: calc/core.py", out)
        self.assertIn("Next step: Run kit-developer", out)
        self.assertIn("Allowed events: developed, stop", out)

        core.write_text(core.read_text().replace("  # wip", ""))
        self.kit("checkpoint", "event", "fix-mul", "developed",
                 "--report", self.report("developer", "implemented"), expect=0)
        out = self.kit("checkpoint", "resume", "fix-mul", expect=0).stdout
        self.assertIn("phase=review", out)
        self.assertIn("Next step: Run kit-reviewer", out)
        self.kit("checkpoint", "event", "fix-mul", "review",
                 "--report", self.report("reviewer", "approve"), expect=0)
        self.assertIn("Task is complete", self.kit("checkpoint", "resume", "fix-mul", expect=0).stdout)

    def test_resume_blocks_when_the_repository_moved(self):
        self.kit("checkpoint", "new", "--task", "doc", "--goal", "Document add()",
                 "--scope", "README.md", "--criterion", "README explains add", expect=0)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "checkpoint committed by the user")
        # The checkpoint's Head commit is now behind HEAD.
        out = self.kit("checkpoint", "resume", "doc", expect=EXIT_REFUSED).stdout
        self.assertIn("STOP: HEAD moved", out)
        self.assertIn("RESUME BLOCKED", out)

    def test_record_and_event_refuse_after_the_repository_moved(self):
        self.kit("checkpoint", "new", "--task", "doc3", "--goal", "Document add()",
                 "--scope", "README.md", "--criterion", "README explains add", expect=0)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "user commit during the task")
        proc = self.kit("checkpoint", "record", "doc3", "--done", "x", expect=EXIT_REFUSED)
        self.assertIn("HEAD moved", proc.stderr)
        self.kit("checkpoint", "event", "doc3", "develop", expect=EXIT_REFUSED)
        # The orchestrator records the user's commit explicitly; then work continues.
        self.kit("checkpoint", "record", "doc3", "--accept-head", "--done",
                 "user committed the checkpoint", expect=0)
        self.assertTrue(any(e.startswith("accept-head: ") for e in self.state("doc3")["Log"]))
        self.kit("checkpoint", "event", "doc3", "develop", expect=0)
        git(self.root, "checkout", "-q", "-b", "elsewhere")
        self.kit("checkpoint", "record", "doc3", "--accept-head", expect=EXIT_REFUSED)

    def test_baseline_paths_with_spaces(self):
        self.write("My Notes.md", "user notes\n")
        self.kit("checkpoint", "new", "--task", "spaces", "--goal", "Fix add()",
                 "--scope", "calc/core.py", "--criterion", "add works", "--allow-dirty", expect=0)
        self.kit("checkpoint", "event", "spaces", "develop", expect=0)
        self.write("calc/core.py", FIXED_ADD)
        self.kit("checkpoint", "event", "spaces", "developed",
                 "--report", self.report("developer", "implemented"), expect=0)
        self.assertIn("RESUME OK", self.kit("checkpoint", "resume", "spaces", expect=0).stdout)

    def test_task_names_and_duplicates(self):
        self.kit("checkpoint", "new", "--task", "foo.md", "--goal", "x", "--scope", "README.md",
                 "--criterion", "x", expect=EXIT_REFUSED)
        self.assertFalse((self.root / "foo.md").exists())
        self.kit("checkpoint", "new", "--task", "dup", "--goal", "x", "--scope", "README.md",
                 "--criterion", "x", expect=0)
        self.kit("checkpoint", "new", "--task", "dup", "--goal", "x", "--scope", "README.md",
                 "--criterion", "x", expect=EXIT_REFUSED)

    def test_resume_blocks_on_another_branch(self):
        self.kit("checkpoint", "new", "--task", "doc2", "--goal", "Document mul()",
                 "--scope", "README.md", "--criterion", "README explains mul", expect=0)
        git(self.root, "checkout", "-q", "-b", "other")
        out = self.kit("checkpoint", "resume", "doc2", expect=EXIT_REFUSED).stdout
        self.assertIn("STOP: current branch is other", out)

    def test_checkpoint_refuses_secrets_and_logs(self):
        self.kit("checkpoint", "new", "--task", "notes", "--goal", "Add notes",
                 "--scope", "README.md", "--criterion", "notes added", expect=0)
        before = self.checkpoint_text("notes")
        for value in ("token=" + "ghp_" + "a" * 30, "contact someone@example.org",
                      "x" * 500):
            proc = self.kit("checkpoint", "record", "notes", "--done", value)
            self.assertNotEqual(proc.returncode, 0, value[:30])
        self.assertEqual(self.checkpoint_text("notes"), before)


class Scenario4IncompleteImplementation(FakeProjectCase):
    """Incomplete work: the reviewer rejects and the bounded fix loop runs."""

    def _start(self, task):
        self.kit("checkpoint", "new", "--task", task, "--goal", "Fix add() and mul()",
                 "--scope", "calc/core.py", "--criterion", "add(2, 3) == 5",
                 "--criterion", "mul(2, 3) == 6", expect=0)
        self.kit("checkpoint", "event", task, "develop", expect=0)

    def _developed(self, task, edit):
        self.write("calc/core.py", edit)
        self.kit("checkpoint", "event", task, "developed",
                 "--report", self.report("developer", "implemented"), expect=0)

    def test_rejected_then_fixed_within_the_limit(self):
        self._start("fix-both")
        self._developed("fix-both", FIXED_ADD + "\n\ndef mul(a, b):\n    return a + b\n")  # mul missed
        self.kit("checkpoint", "event", "fix-both", "review", "--report",
                 self.report("reviewer", "changes_requested", findings="R1"), expect=0)
        cp = self.state("fix-both")
        self.assertEqual((cp["state"]["Phase"], cp["state"]["Fix rounds"]), ("fix", "1/2"))
        self.assertIn("R1", cp["Next step"])
        self._developed("fix-both", FIXED_ADD + "\n\n" + FIXED_MUL)
        self.kit("checkpoint", "event", "fix-both", "review",
                 "--report", self.report("reviewer", "approve"), expect=0)
        self.assertEqual(self.state("fix-both")["state"]["Status"], "done")

    def test_fix_round_limit_stops_the_loop(self):
        self._start("endless")
        self._developed("endless", "def add(a, b):\n    return 0\n")
        for round_no, finding in enumerate(("R1", "R2"), start=1):
            self.kit("checkpoint", "event", "endless", "review", "--verdict", "changes_requested",
                     "--finding", finding, expect=0)
            self.assertEqual(self.state("endless")["state"]["Fix rounds"], f"{round_no}/2")
            self._developed("endless", f"def add(a, b):\n    return {round_no}\n")
        self.kit("checkpoint", "event", "endless", "review", "--verdict", "changes_requested",
                 "--finding", "R3", expect=EXIT_REFUSED)
        cp = self.state("endless")
        self.assertEqual(cp["state"]["Status"], "waiting_user")
        self.assertIn("fix-round limit reached (2/2)", cp["state"]["Reason"])
        # The limit is final: no further developer round and no approval override.
        self.kit("checkpoint", "event", "endless", "developed", "--verdict", "implemented", expect=EXIT_REFUSED)
        self.kit("checkpoint", "event", "endless", "approve", "--note", "try again", expect=EXIT_REFUSED)

    def test_repeated_finding_stops_as_no_progress(self):
        self._start("stuck")
        self._developed("stuck", "def add(a, b):\n    return 0\n")
        self.kit("checkpoint", "event", "stuck", "review", "--verdict", "changes_requested",
                 "--finding", "R1", expect=0)
        self._developed("stuck", "def add(a, b):\n    return 1\n")
        self.kit("checkpoint", "event", "stuck", "review", "--verdict", "changes_requested",
                 "--finding", "R1", expect=EXIT_REFUSED)
        self.assertIn("no progress", self.state("stuck")["state"]["Reason"])

    def test_review_refuses_a_tree_changed_after_development(self):
        self._start("late-change")
        self._developed("late-change", FIXED_ADD)
        self.write("calc/core.py", FIXED_ADD + "\n# edited after the developer run\n")
        self.kit("checkpoint", "event", "late-change", "review",
                 "--report", self.report("reviewer", "approve"), expect=EXIT_REFUSED)
        cp = self.state("late-change")
        self.assertIn("working tree changed after the last `developed` record", cp["state"]["Reason"])
        self.assertEqual(cp["state"]["Phase"], "develop")
        self.kit("checkpoint", "event", "late-change", "approve", "--note", "User kept the edit", expect=0)
        self._developed("late-change", FIXED_ADD + "\n# edited after the developer run\n")
        self.kit("checkpoint", "event", "late-change", "review",
                 "--report", self.report("reviewer", "approve"), expect=0)

    def test_approval_required_file_added_during_development_converges(self):
        self._start("contract")
        self.write("calc/core.py", FIXED_ADD + "\n\n" + FIXED_MUL)
        self.write("calc/contract.py", "VERSION = 'calc_v2'\n")  # out of scope and approval-required
        self.kit("checkpoint", "event", "contract", "developed",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        self.assertIn("out of scope: calc/contract.py", self.state("contract")["state"]["Reason"])
        self.kit("checkpoint", "event", "contract", "approve", "--note",
                 "User accepts changing the contract version", expect=0)
        self.kit("checkpoint", "record", "contract", "--scope", "calc/contract.py",
                 "--user-approved-change", expect=0)
        self.kit("checkpoint", "event", "contract", "developed",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        self.assertIn("approval required for: calc/contract.py", self.state("contract")["state"]["Reason"])
        self.kit("checkpoint", "event", "contract", "approve", "--note",
                 "User approved calc/contract.py", expect=0)
        self.kit("checkpoint", "event", "contract", "developed",
                 "--report", self.report("developer", "implemented"), expect=0)
        self.assertEqual(self.state("contract")["state"]["Phase"], "review")

    def test_report_of_another_role_is_refused(self):
        self._start("wrong-role")
        self._developed("wrong-role", FIXED_ADD)
        self.kit("checkpoint", "event", "wrong-role", "review",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        self.assertEqual(self.state("wrong-role")["state"]["Phase"], "review")

    def test_approval_with_findings_is_rejected(self):
        self._start("odd")
        self._developed("odd", FIXED_ADD)
        self.kit("checkpoint", "event", "odd", "review", "--verdict", "approve", "--finding", "R1",
                 expect=EXIT_REFUSED)
        self.assertEqual(self.state("odd")["state"]["Phase"], "review")


class Scenario5ProductionAndOutOfScope(FakeProjectCase):
    """Production or out-of-scope requests stop safely."""

    def test_production_request_waits_for_the_user(self):
        proc = self.kit("checkpoint", "new", "--task", "ship", "--goal",
                        "Deploy the latest version to production", "--scope", "calc/core.py",
                        "--criterion", "live", expect=EXIT_REFUSED)
        self.assertIn("waiting_user", proc.stdout)
        cp = self.state("ship")
        self.assertIn("production", cp["state"]["Risk flags"])
        self.kit("checkpoint", "event", "ship", "develop", expect=EXIT_REFUSED)
        self.kit("checkpoint", "event", "ship", "research", expect=EXIT_REFUSED)

    def test_guard_blocks_production_and_external_actions(self):
        dev = "kit-developer"
        for command in (
            "git push origin main",
            "git push --force",
            "git merge main",
            "git checkout -- calc/core.py",
            "git reset --hard",
            "gh pr merge 1",
            "kubectl apply -f deploy.yml",
            "railway up",
            "serve-prod --port 80",
            "curl -X POST https://example.invalid/deploy",
            "ssh prod.example.invalid",
            "pip install requests",
            "npm install left-pad",
            "sudo systemctl restart app",
            "cat .env",
            "printenv",
            "echo $CALC_SIGNING_KEY",
            "rm -rf build",
        ):
            with self.subTest(command=command):
                self.assertHookBlocks(dev, "Bash", {"command": command})
        self.assertHookBlocks(dev, "Agent", {"subagent_type": "kit-developer", "prompt": "x"})
        self.assertHookBlocks(dev, "Write", {"file_path": str(self.root / ".claude/settings.json"),
                                             "content": "{}"})
        self.assertHookBlocks(dev, "Edit", {"file_path": ".claude/agent-kit/PROJECT_CONTEXT.md",
                                            "old_string": "a", "new_string": "b"})
        # The main session (no agent_type) is not guarded by the hook.
        self.assertHookAllows(None, "Bash", {"command": "git push origin main"})

    def test_out_of_scope_change_stops_without_reverting(self):
        self.kit("checkpoint", "new", "--task", "fix-add2", "--goal", "Fix add()",
                 "--scope", "calc/core.py", "--criterion", "add(2, 3) == 5", expect=0)
        self.kit("checkpoint", "event", "fix-add2", "develop", expect=0)
        self.write("calc/core.py", FIXED_ADD)
        self.write("README.md", "# Fake calculator\n\nRewritten beyond the scope.\n")
        self.kit("checkpoint", "event", "fix-add2", "developed",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        cp = self.state("fix-add2")
        self.assertEqual(cp["state"]["Status"], "waiting_user")
        self.assertIn("out of scope: README.md", cp["state"]["Reason"])
        self.assertIn("Rewritten", (self.root / "README.md").read_text())  # nothing reverted

    def test_pre_existing_changes_cannot_be_in_scope(self):
        self.write("calc/core.py", CALC_WIP)
        proc = self.kit("checkpoint", "new", "--task", "wip", "--goal", "Fix add()",
                        "--scope", "calc/**", "--criterion", "add works", "--allow-dirty", expect=EXIT_REFUSED)
        self.assertIn("pre-existing uncommitted changes", proc.stderr)

    def test_reverted_user_change_is_detected(self):
        self.write("NOTES.txt", "the user's unfinished notes\n")
        self.kit("checkpoint", "new", "--task", "revert", "--goal", "Fix add()",
                 "--scope", "calc/core.py", "--criterion", "add works", "--allow-dirty", expect=0)
        self.kit("checkpoint", "event", "revert", "develop", expect=0)
        self.write("calc/core.py", FIXED_ADD)
        (self.root / "NOTES.txt").unlink()  # simulates an agent discarding the user's file
        self.kit("checkpoint", "event", "revert", "developed",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        self.assertIn("pre-existing user change was modified: NOTES.txt", self.state("revert")["state"]["Reason"])

    def test_pre_existing_user_changes_are_protected(self):
        self.write("NOTES.txt", "the user's unfinished notes\n")
        self.kit("checkpoint", "new", "--task", "fix-add3", "--goal", "Fix add()",
                 "--scope", "calc/core.py", "--criterion", "add works", expect=EXIT_REFUSED)
        self.kit("checkpoint", "new", "--task", "fix-add3", "--goal", "Fix add()",
                 "--scope", "calc/core.py", "--criterion", "add works", "--allow-dirty", expect=0)
        self.assertTrue(any(e.startswith("dirty NOTES.txt ") for e in self.state("fix-add3")["Baseline"]))
        self.kit("checkpoint", "event", "fix-add3", "develop", expect=0)
        self.write("calc/core.py", FIXED_ADD)
        self.write("NOTES.txt", "overwritten by an agent\n")
        self.kit("checkpoint", "event", "fix-add3", "developed",
                 "--report", self.report("developer", "implemented"), expect=EXIT_REFUSED)
        self.assertIn("pre-existing user change was modified: NOTES.txt",
                      self.state("fix-add3")["state"]["Reason"])


if __name__ == "__main__":
    unittest.main()
