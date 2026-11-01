"""Installer, validator, checkpoint format and hook wrapper tests (fake
projects and temp copies of the kit only)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kit_test_support import FAKE_CONFIG, GIT_ENV, KIT_ROOT, FakeProjectCase, context_text, git

from kitlib import checkpoint as cpmod
from kitlib.common import INSTALL_MAP, KitError, Layout, ROLES, glob_to_regex, path_matches
from kitlib.install import install
from kitlib.validate import validate_kit, validate_project


class InstallerTests(FakeProjectCase):
    def test_fresh_install_validates(self):
        report = validate_project(self.root)
        self.assertTrue(report.ok, report.lines())
        for _src, dst in INSTALL_MAP:
            self.assertTrue((self.root / dst).is_file(), dst)
        self.assertEqual(git(self.root, "status", "--porcelain").strip(), "")

    def test_reinstall_is_idempotent_and_keeps_the_context(self):
        context = self.root / ".claude/agent-kit/PROJECT_CONTEXT.md"
        context.write_text(context.read_text() + "\nLocal note.\n", encoding="utf-8")
        actions = install(self.root)
        self.assertTrue(all(a.startswith(("unchanged", "keep")) for a in actions), actions)
        self.assertIn("Local note.", context.read_text())

    def test_conflicting_kit_file_needs_force(self):
        guard = self.root / ".claude/agent-kit/hooks/kit_guard.py"
        guard.write_text(guard.read_text() + "\n# weakened\n", encoding="utf-8")
        report = validate_project(self.root)
        self.assertFalse(report.ok)
        self.assertTrue(any("edited locally" in e for e in report.errors), report.errors)
        with self.assertRaises(KitError):
            install(self.root)
        self.assertIn("# weakened", guard.read_text())  # nothing written on conflict
        install(self.root, force=True)
        self.assertNotIn("# weakened", guard.read_text())
        self.assertTrue(validate_project(self.root).ok)

    def test_settings_merge_keeps_existing_entries(self):
        settings_path = self.root / ".claude/settings.json"
        settings = json.loads(settings_path.read_text())
        settings["permissions"]["deny"].insert(0, "Read(./private/**)")
        settings["permissions"]["allow"] = ["Bash(git status)"]
        settings["hooks"]["PreToolUse"].insert(0, {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "bash other_hook.sh"}]})
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        install(self.root)
        merged = json.loads(settings_path.read_text())
        self.assertIn("Read(./private/**)", merged["permissions"]["deny"])
        self.assertEqual(merged["permissions"]["allow"], ["Bash(git status)"])
        commands = [h["command"] for e in merged["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("bash other_hook.sh", commands)
        self.assertEqual(sum("kit_guard.sh" in c for c in commands), 1)

    def test_new_protected_path_needs_reinstall(self):
        context = self.root / ".claude/agent-kit/PROJECT_CONTEXT.md"
        config = dict(FAKE_CONFIG, protected_paths=FAKE_CONFIG["protected_paths"] + ["exports/**"])
        context.write_text(context_text(config), encoding="utf-8")
        report = validate_project(self.root)
        self.assertTrue(any("deny rule" in e for e in report.errors), report.errors)
        install(self.root)
        self.assertTrue(validate_project(self.root).ok)

    def test_invalid_settings_json_is_not_touched(self):
        settings_path = self.root / ".claude/settings.json"
        settings_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(KitError):
            install(self.root)
        self.assertEqual(settings_path.read_text(), "{ not json")

    def test_target_must_be_a_git_repository(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(KitError):
            install(plain)


class ValidatorTests(FakeProjectCase):
    def assertError(self, fragment):
        report = validate_project(self.root)
        self.assertTrue(any(fragment in e for e in report.errors), report.errors)

    def _edit_installed(self, rel, old, new):
        """Simulates a kit file edit and refreshes the manifest hash, so the
        content check itself is what fails."""
        path = self.root / rel
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")
        manifest_path = self.root / ".claude/agent-kit/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        import hashlib
        manifest["files"][rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_agent_tools_are_checked(self):
        self._edit_installed(".claude/agents/kit-reviewer.md", "tools: Read, Grep, Glob, Bash",
                             "tools: Read, Grep, Glob, Bash, Edit, Agent")
        self.assertError("tools must be exactly")

    def test_agent_contract_sections_are_checked(self):
        self._edit_installed(".claude/agents/kit-auditor.md", "## Stop conditions", "## Notes")
        self.assertError("contract section missing: ## Stop conditions")

    def test_permission_mode_is_checked(self):
        self._edit_installed(".claude/agents/kit-developer.md", "model: inherit",
                             "model: inherit\npermissionMode: bypassPermissions")
        self.assertError("weakens the permission prompts")

    def test_missing_reference_is_reported(self):
        (self.root / ".claude/agent-kit/SAFETY_POLICY.md").unlink()
        self.assertError("referenced file does not exist: .claude/agent-kit/SAFETY_POLICY.md")

    def test_context_placeholders_and_unknown_keys(self):
        context = self.root / ".claude/agent-kit/PROJECT_CONTEXT.md"
        text = context_text(dict(FAKE_CONFIG, project_name="<FILL: name>", typo_key=1))
        context.write_text(text, encoding="utf-8")
        self.assertError("unfilled <FILL")
        self.assertError("unknown kit-config key 'typo_key'")

    def test_loop_limit_above_core_maximum(self):
        context = self.root / ".claude/agent-kit/PROJECT_CONTEXT.md"
        context.write_text(context_text(dict(FAKE_CONFIG, loop_limits={"max_fix_rounds": 9})),
                           encoding="utf-8")
        self.assertError("max_fix_rounds must be an integer from 0 to 3")

    def test_hook_entry_must_fail_closed(self):
        settings_path = self.root / ".claude/settings.json"
        settings = json.loads(settings_path.read_text())
        for entry in settings["hooks"]["PreToolUse"]:
            for hook in entry["hooks"]:
                hook["onFailure"] = "allow"
            entry["matcher"] = "Read|Bash"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        self.assertError("fail closed")
        self.assertError("matcher misses")

    def test_invalid_checkpoint_is_reported(self):
        cp_dir = self.root / ".claude/agent-kit/checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        (cp_dir / "broken.md").write_text("# Checkpoint: broken\n\nno state\n", encoding="utf-8")
        self.assertError("broken.md")


class KitSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kit_source_copy_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kit = self.tmp / "kit"
        shutil.copytree(KIT_ROOT, self.kit, ignore=shutil.ignore_patterns("__pycache__"))

    def test_kit_source_validates(self):
        report = validate_kit(KIT_ROOT)
        self.assertTrue(report.ok, report.lines())

    def test_core_purity_catches_adapter_terms(self):
        # Built at run time so that this test file itself stays free of them.
        name, term = "Zz" + "fakeproj", "qq" + "specialterm"
        adapter = self.kit / "adapters" / "zzfake"
        adapter.mkdir(parents=True)
        (adapter / "PROJECT_CONTEXT.md").write_text(
            context_text(dict(FAKE_CONFIG, project_name=name, core_must_not_mention=[term])),
            encoding="utf-8")
        self.assertTrue(validate_kit(self.kit).ok, validate_kit(self.kit).lines())
        guard = self.kit / "core" / "hooks" / "kit_guard.py"
        guard.write_text(guard.read_text() + f"\n# special case for {name} and {term}\n",
                         encoding="utf-8")
        errors = validate_kit(self.kit).errors
        self.assertTrue(any(f"'{name}'" in e for e in errors), errors)
        self.assertTrue(any(f"'{term}'" in e for e in errors), errors)

    def test_fifth_agent_is_rejected(self):
        extra = self.kit / "core" / "agents" / "kit-supervisor.md"
        shutil.copy(self.kit / "core" / "agents" / "kit-auditor.md", extra)
        errors = validate_kit(self.kit).errors
        self.assertTrue(any("only the four kit roles" in e for e in errors), errors)

    def test_template_must_keep_placeholders(self):
        template = self.kit / "templates" / "PROJECT_CONTEXT.md"
        template.write_text(template.read_text().replace("<FILL", "<DONE"), encoding="utf-8")
        self.assertTrue(any("placeholders" in e for e in validate_kit(self.kit).errors))

    def test_layout_detects_source_and_installed_copies(self):
        self.assertTrue(Layout(KIT_ROOT).is_source)
        installed = self.tmp / "proj" / ".claude" / "agent-kit"
        installed.mkdir(parents=True)
        layout = Layout(installed)
        self.assertFalse(layout.is_source)
        self.assertEqual(layout.project_root, self.tmp / "proj")

    def test_shell_wrapper_has_lf_line_endings(self):
        self.assertNotIn(b"\r", (KIT_ROOT / "core" / "hooks" / "kit_guard.sh").read_bytes())
        self.assertIn("*.sh text eol=lf", (KIT_ROOT / ".gitattributes").read_text())


class CheckpointFormatTests(unittest.TestCase):
    def make(self):
        state = cpmod.new_state("demo-task", "task/demo", "abc123", 2)
        cp = cpmod.empty_checkpoint("demo-task", "Demo goal", state)
        cp["Approved scope"] = ["src/a.py"]
        cp["Acceptance criteria"] = ["works"]
        cp["Next step"] = "Run kit-developer."
        return cp

    def test_render_parse_roundtrip(self):
        cp = self.make()
        text = cpmod.render(cp)
        self.assertEqual(cpmod.verify(text), [])
        parsed = cpmod.parse(text)
        self.assertEqual(parsed["Approved scope"], ["src/a.py"])
        self.assertEqual(parsed["state"]["Fix rounds"], "0/2")
        self.assertEqual(cpmod.render(parsed), text)

    def test_template_matches_the_format(self):
        text = (KIT_ROOT / "core" / "docs" / "CHECKPOINT_TEMPLATE.md").read_text()
        for section in cpmod.SECTIONS:
            self.assertIn(f"## {section}", text)

    def test_sensitive_content_is_refused(self):
        # Synthetic look-alikes, assembled at run time so no scanner sees a literal.
        for bad in ("-----BEGIN RSA " + "PRIVATE KEY-----", "AKIA" + "A" * 16, "password: " + "x1y2z3w4",
                    "sk-" + "a" * 20, "postgres://user:" + "pw@db.example.invalid/x",
                    "mail me at person@example.org",
                    'Traceback (most recent call last):\n  File "x.py", line 1'):
            cp = self.make()
            cp["Done"] = [bad]
            with self.subTest(bad=bad[:20]):
                self.assertTrue(cpmod.verify(cpmod.render(cp)))

    def test_structure_rules(self):
        cp = self.make()
        cp["state"]["Phase"] = "develop"
        cp["Approved scope"] = []
        self.assertTrue(any("Approved scope" in p for p in cpmod.verify(cpmod.render(cp))))
        cp = self.make()
        cp["state"]["Fix rounds"] = "4/9"
        self.assertTrue(any("core maximum" in p for p in cpmod.verify(cpmod.render(cp))))
        cp = self.make()
        cp["state"]["Status"] = "waiting_user"
        self.assertTrue(any("Reason is required" in p for p in cpmod.verify(cpmod.render(cp))))
        text = cpmod.render(self.make()).replace("## Done", "## Extra\n- x\n\n## Done")
        self.assertTrue(cpmod.verify(text))


class GlobTests(unittest.TestCase):
    def test_gitignore_like_globs(self):
        cases = [
            ("migrations/**", "migrations/0001.sql", True),
            ("migrations/**", "app/migrations/0001.sql", False),
            ("**/migrations/**", "app/migrations/0001.sql", True),
            ("*.sql", "db/schema.sql", True),
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "src/sub/a.py", False),
            ("Dockerfile", "Dockerfile", True),
            ("Dockerfile", "docker/Dockerfile", True),
            ("./README.md", "README.md", True),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(glob_to_regex(pattern).match(path)), expected)
        self.assertTrue(path_matches("./calc/core.py", ["calc/**"]))


class WrapperTests(FakeProjectCase):
    def test_missing_guard_blocks_only_kit_agents(self):
        (self.root / ".claude/agent-kit/hooks/kit_guard.py").rename(self.tmp / "moved_guard.py")
        rc, err = self.hook("kit-reviewer", "Read", {"file_path": "README.md"})
        self.assertEqual(rc, 2)
        self.assertIn("no Python could run kit_guard.py", err)
        rc, _ = self.hook(None, "Read", {"file_path": "README.md"})
        self.assertEqual(rc, 0)

    def test_broken_adapter_blocks_kit_agents(self):
        (self.root / ".claude/agent-kit/PROJECT_CONTEXT.md").write_text("no config block", encoding="utf-8")
        rc, err = self.hook("kit-developer", "Read", {"file_path": "README.md"})
        self.assertEqual(rc, 2)
        self.assertIn("adapter is invalid", err)
        self.assertEqual(self.hook(None, "Read", {"file_path": "README.md"})[0], 0)

    def test_large_input_is_handled(self):
        rc, _ = self.hook("kit-developer", "Write", {"file_path": "calc/big.py", "content": "x = 1\n" * 50000})
        self.assertEqual(rc, 0)

    def test_installed_cli_runs_without_the_source_tree(self):
        moved = self.tmp / "elsewhere"
        shutil.copytree(self.root, moved, symlinks=True)
        env = dict(os.environ, **GIT_ENV, CLAUDE_PROJECT_DIR=str(moved))
        proc = subprocess.run(["python3", str(moved / ".claude/agent-kit/kit.py"), "validate"],
                              capture_output=True, text=True, env=env, cwd=moved, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
