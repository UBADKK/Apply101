"""Validator for the kit source tree and for installed projects.

Checks agent frontmatter and contracts, file references, the project
context (adapter), the guard hook and its settings entry, the deny rules,
the install manifest, checkpoints, and that the core stays free of
project-specific terms. It never runs project commands and never reads
protected files.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import checkpoint as cpmod
from .common import (
    CONTEXT_FILE, CORE_MAX_FIX_ROUNDS, GUARD_COMMAND, HOOK_MATCHER_TOOLS,
    INSTALL_DIR, INSTALL_MAP, MANIFEST, ROLES, SETTINGS_FILE, KitError,
    Layout, load_guard, read_text, tracked_paths,
)
from .workflow import RISK_FLAGS

ROLE_TOOLS = {
    "kit-researcher": {"Read", "Grep", "Glob"},
    "kit-auditor": {"Read", "Grep", "Glob"},
    "kit-developer": {"Read", "Grep", "Glob", "Bash", "Edit", "Write"},
    "kit-reviewer": {"Read", "Grep", "Glob", "Bash"},
}
ROLE_MAX_TURNS = {"kit-researcher": 30, "kit-auditor": 40, "kit-developer": 80, "kit-reviewer": 50}
ROLE_PERMISSION_MODE = {"kit-reviewer": "plan"}
FORBIDDEN_PERMISSION_MODES = {"bypassPermissions", "acceptEdits", "dontAsk", "auto"}
AGENT_SECTIONS = (
    "## Responsibility", "## Inputs", "## Outputs", "## Authority limits",
    "## Stop conditions",
)
CONTEXT_SECTIONS = (
    "## Project summary", "## Repository map", "## Sources of truth",
    "## Sensitive areas", "## Real data and secrets", "## Verification",
    "## Conventions", "## Machine-readable configuration",
)
CONFIG_TYPES = {
    "kit_config_version": int, "project_name": str, "report_language": str,
    "default_branch": str, "checkpoint_dir": str, "protected_paths": list,
    "real_data_commands": list, "secret_env_vars": list, "watch_files": list,
    "approval_required_paths": list, "risk_keywords": dict,
    "test_commands": dict, "loop_limits": dict, "core_must_not_mention": list,
}
REQUIRED_CONFIG = (
    "kit_config_version", "project_name", "report_language", "default_branch",
    "protected_paths", "approval_required_paths", "test_commands", "loop_limits",
)
PLACEHOLDER_RE = re.compile(r"<FILL\b")
# A concrete path under .claude/; globs and placeholders (kit-*.md, <task>) are skipped.
REFERENCE_RE = re.compile(
    r"\.claude/(?:agent-kit|agents|commands)(?:/[A-Za-z0-9_.-]*[A-Za-z0-9_])*(?![\w*</-])(?!\.\w)"
)
# Files of the kit that must stay free of any adapter's terms.
CORE_GLOBS = ("core/**/*", "kitlib/*.py", "kit.py", "templates/*", "tests/*.py")


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, where, message):
        self.errors.append(f"{where}: {message}")

    def warn(self, where, message):
        self.warnings.append(f"{where}: {message}")

    @property
    def ok(self):
        return not self.errors

    def lines(self):
        out = [f"ERROR   {e}" for e in self.errors] + [f"WARNING {w}" for w in self.warnings]
        out.append("VALIDATION PASSED" if self.ok else f"VALIDATION FAILED ({len(self.errors)} errors)")
        return out


# -- agents -------------------------------------------------------------------------

def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KitError("missing '---' frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise KitError("frontmatter is not closed")
    meta = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not m:
            raise KitError(f"unreadable frontmatter line: {line[:60]!r}")
        meta[m.group(1)] = m.group(2).strip()
    return meta, "\n".join(lines[end + 1:])


def validate_agent(text, role, report, where):
    try:
        meta, body = parse_frontmatter(text)
    except KitError as exc:
        report.error(where, str(exc))
        return
    if meta.get("name") != role:
        report.error(where, f"name must be {role!r}")
    if len(meta.get("description", "")) < 40:
        report.error(where, "description is missing or too short to route tasks")
    if "tools" not in meta:
        report.error(where, "tools must be listed explicitly (an omitted list inherits every tool)")
    else:
        tools = {t.strip() for t in meta["tools"].split(",") if t.strip()}
        if tools != ROLE_TOOLS[role]:
            report.error(where, f"tools must be exactly {', '.join(sorted(ROLE_TOOLS[role]))}")
    mode = meta.get("permissionMode")
    if mode in FORBIDDEN_PERMISSION_MODES:
        report.error(where, f"permissionMode {mode} weakens the permission prompts")
    if role in ROLE_PERMISSION_MODE and mode != ROLE_PERMISSION_MODE[role]:
        report.error(where, f"permissionMode must be {ROLE_PERMISSION_MODE[role]}")
    try:
        turns = int(meta.get("maxTurns", ""))
        if not 1 <= turns <= ROLE_MAX_TURNS[role]:
            raise ValueError
    except ValueError:
        report.error(where, f"maxTurns must be an integer from 1 to {ROLE_MAX_TURNS[role]}")
    if "model" not in meta:
        report.error(where, "model is missing (use 'inherit')")
    for section in AGENT_SECTIONS:
        if section not in body:
            report.error(where, f"contract section missing: {section}")
    if "```kit-result" not in body:
        report.error(where, "the agent must end its report with a ```kit-result block")


def check_references(text, report, where, exists):
    for ref in sorted(set(REFERENCE_RE.findall(text))):
        if not exists(ref):
            report.error(where, f"referenced file does not exist: {ref}")


# -- project context ----------------------------------------------------------------

def validate_context(text, report, where, template=False):
    """The adapter config, or None when it cannot be parsed."""
    for section in CONTEXT_SECTIONS:
        if section not in text:
            report.error(where, f"section missing: {section}")
    if not template and PLACEHOLDER_RE.search(text):
        count = len(PLACEHOLDER_RE.findall(text))
        report.error(where, f"{count} unfilled <FILL ...> placeholder(s) left")
    if template and not PLACEHOLDER_RE.search(text):
        report.error(where, "the template should contain <FILL ...> placeholders")
    guard = load_guard()
    try:
        config = guard.parse_config(text)
    except guard.ConfigError as exc:
        report.error(where, str(exc))
        return None
    for key in config:
        if key not in CONFIG_TYPES:
            report.error(where, f"unknown kit-config key {key!r}")
    for key in REQUIRED_CONFIG:
        if key not in config:
            report.error(where, f"kit-config key {key!r} is required")
    for key, expected in CONFIG_TYPES.items():
        if key in config and (not isinstance(config[key], expected) or isinstance(config[key], bool) and expected is int):
            report.error(where, f"kit-config {key!r} must be a {expected.__name__}")
    if config.get("kit_config_version") != 1:
        report.error(where, "kit_config_version must be 1")
    for key in ("approval_required_paths", "watch_files", "core_must_not_mention"):
        values = config.get(key, [])
        if isinstance(values, list) and not all(isinstance(v, str) and v.strip() for v in values):
            report.error(where, f"{key} must contain non-empty strings")
    for rel in config.get("watch_files", []) if isinstance(config.get("watch_files"), list) else []:
        try:
            guard.normalize_relative_dir(rel, "watch_files entry")
        except guard.ConfigError as exc:
            report.error(where, str(exc))
    try:
        guard.GuardSettings(config)
    except guard.ConfigError as exc:
        report.error(where, str(exc))
    tests = config.get("test_commands")
    if isinstance(tests, dict):
        if not isinstance(tests.get("full"), str) or not tests.get("full", "").strip():
            report.error(where, "test_commands needs a non-empty 'full' command")
        if not all(isinstance(v, str) for v in tests.values()):
            report.error(where, "test_commands values must be strings")
    risk = config.get("risk_keywords", {})
    if isinstance(risk, dict):
        for flag, words in risk.items():
            if flag not in RISK_FLAGS:
                report.error(where, f"risk_keywords flag {flag!r} must be one of {', '.join(RISK_FLAGS)}")
            if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
                report.error(where, f"risk_keywords[{flag!r}] must be a list of words")
    limits = config.get("loop_limits")
    if isinstance(limits, dict):
        extra = set(limits) - {"max_fix_rounds"}
        if extra:
            report.error(where, f"unknown loop_limits keys: {', '.join(sorted(extra))}")
        value = limits.get("max_fix_rounds")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= CORE_MAX_FIX_ROUNDS:
            report.error(where, f"loop_limits.max_fix_rounds must be an integer from 0 to {CORE_MAX_FIX_ROUNDS}")
    return config


# -- installed project ----------------------------------------------------------------

def deny_rules(config):
    """permissions.deny rules for every protected path (core + adapter)."""
    guard = load_guard()
    patterns = list(guard.CORE_PROTECTED_PATHS) + list(config.get("protected_paths") or [])
    rules = []
    for pattern in patterns:
        for tool in ("Read", "Edit"):
            for prefix in ("./", "./**/"):
                rule = f"{tool}({prefix}{pattern})"
                if rule not in rules:
                    rules.append(rule)
    return rules


def kit_hook_entries(settings):
    entries = []
    for entry in (settings.get("hooks") or {}).get("PreToolUse") or []:
        for hook in entry.get("hooks") or []:
            if "agent-kit/hooks/kit_guard.sh" in str(hook.get("command", "")):
                entries.append((entry, hook))
    return entries


def validate_settings(project_root, config, report):
    path = Path(project_root) / SETTINGS_FILE
    where = SETTINGS_FILE
    try:
        settings = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        report.error(where, f"cannot be read as JSON ({type(exc).__name__}); re-run install")
        return
    entries = kit_hook_entries(settings)
    if len(entries) != 1:
        report.error(where, f"expected exactly one PreToolUse entry for the kit guard, found {len(entries)}")
    else:
        entry, hook = entries[0]
        tools = set(str(entry.get("matcher", "")).split("|"))
        missing = set(HOOK_MATCHER_TOOLS) - tools
        if missing:
            report.error(where, f"kit guard matcher misses: {', '.join(sorted(missing))}")
        if hook.get("command") != GUARD_COMMAND:
            report.error(where, f"kit guard command must be: {GUARD_COMMAND}")
        if hook.get("onFailure") != "block":
            report.error(where, "kit guard must fail closed (onFailure: block)")
        if not isinstance(hook.get("timeout"), int) or hook["timeout"] > 30:
            report.error(where, "kit guard timeout must be an integer of at most 30 seconds")
    deny = ((settings.get("permissions") or {}).get("deny")) or []
    missing_rules = [r for r in deny_rules(config) if r not in deny]
    if missing_rules:
        report.error(where, f"{len(missing_rules)} deny rule(s) missing (e.g. {missing_rules[0]}); re-run install")
    for key in ("defaultMode",):
        if (settings.get("permissions") or {}).get(key) in FORBIDDEN_PERMISSION_MODES:
            report.error(where, f"permissions.{key} weakens the permission prompts")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_manifest(project_root, report):
    path = Path(project_root) / MANIFEST
    try:
        manifest = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError):
        report.error(MANIFEST, "missing or unreadable; re-run install")
        return
    files = manifest.get("files") or {}
    for _src, dst in INSTALL_MAP:
        target = Path(project_root) / dst
        if not target.is_file():
            report.error(dst, "kit file is missing; re-run install")
        elif dst not in files:
            report.error(dst, "not in the manifest; re-run install")
        elif sha256(target) != files[dst]:
            report.error(dst, "kit file was edited locally; put project specifics in "
                              f"{CONTEXT_FILE} and re-run install --force to restore it")
    version_file = Path(project_root) / INSTALL_DIR / "VERSION"
    if version_file.is_file() and manifest.get("kit_version") != read_text(version_file).strip():
        report.error(MANIFEST, "kit_version does not match VERSION")


def _git_available(project_root):
    try:
        tracked_paths(project_root)
        return True
    except (KitError, OSError):
        return False


def _not_ignored(project_root, sample):
    proc = subprocess.run(["git", "-C", str(project_root), "check-ignore", "-q", "--no-index", sample],
                          capture_output=True, check=False)
    return proc.returncode == 1


def validate_project(project_root):
    project_root = Path(project_root).resolve()
    report = Report()
    context_path = project_root / INSTALL_DIR / CONTEXT_FILE
    if not context_path.is_file():
        report.error(f"{INSTALL_DIR}/{CONTEXT_FILE}", "missing; run the installer")
        return report
    config = validate_context(read_text(context_path), report, f"{INSTALL_DIR}/{CONTEXT_FILE}") or {}
    validate_manifest(project_root, report)
    for role in ROLES:
        rel = f".claude/agents/{role}.md"
        if (project_root / rel).is_file():
            validate_agent(read_text(project_root / rel), role, report, rel)
    guard = load_guard()
    lazy_dirs = {guard.DEFAULT_CHECKPOINT_DIR}  # created by the first `checkpoint new`

    def exists(ref):
        return (project_root / ref).exists() or ref.rstrip("/") in lazy_dirs

    for _src, dst in INSTALL_MAP:
        if dst.endswith(".md") and (project_root / dst).is_file():
            check_references(read_text(project_root / dst), report, dst, exists)
    sh = project_root / INSTALL_DIR / "hooks" / "kit_guard.sh"
    if sh.is_file() and b"\r" in sh.read_bytes():
        report.error(str(sh.relative_to(project_root)), "has CRLF line endings; bash needs LF")
    if config:
        validate_settings(project_root, config, report)
        validate_checkpoints(project_root, config, report)
        if _git_available(project_root):
            tracked = tracked_paths(project_root)
            from .common import path_matches
            for pattern in config.get("approval_required_paths", []):
                if not any(path_matches(p, [pattern]) for p in tracked):
                    report.warn(CONTEXT_FILE, f"approval_required_paths entry {pattern!r} matches no tracked file")
            guard = load_guard()
            for pattern in list(guard.CORE_PROTECTED_PATHS[:2]) + list(config.get("protected_paths", [])):
                sample = pattern[:-3] + "/x" if pattern.endswith("/**") else pattern.replace("*", "").replace("?", "x")
                if _not_ignored(project_root, sample):
                    report.warn(CONTEXT_FILE, f"protected path {pattern!r} is not gitignored: the Grep tool "
                                              "and plain rg may read such files")
    return report


def validate_checkpoints(project_root, config, report):
    guard = load_guard()
    try:
        directory = Path(project_root) / guard.normalize_relative_dir(
            config.get("checkpoint_dir", guard.DEFAULT_CHECKPOINT_DIR))
    except guard.ConfigError:
        return
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.md")):
        for problem in cpmod.verify(read_text(path)):
            report.error(str(path.relative_to(project_root)), problem)


# -- kit source tree ------------------------------------------------------------------

def validate_kit(kit_root):
    """Validates the kit source: core agents, commands, docs, template,
    checkpoint template, every adapter, and core purity."""
    kit_root = Path(kit_root).resolve()
    report = Report()
    installed_to_source = {dst: src for src, dst in INSTALL_MAP}

    def exists(ref):
        src = installed_to_source.get(ref)
        if ref == f"{INSTALL_DIR}/{CONTEXT_FILE}":
            return (kit_root / "templates" / CONTEXT_FILE).is_file()
        if ref.rstrip("/") in (f"{INSTALL_DIR}/checkpoints", INSTALL_DIR, ".claude/agents", ".claude/commands"):
            return True
        return bool(src) and (kit_root / src).is_file()

    for src, _dst in INSTALL_MAP:
        if not (kit_root / src).is_file():
            report.error(src, "listed in INSTALL_MAP but missing")
    for role in ROLES:
        rel = f"core/agents/{role}.md"
        if (kit_root / rel).is_file():
            validate_agent(read_text(kit_root / rel), role, report, rel)
    extra_agents = {p.stem for p in (kit_root / "core" / "agents").glob("*.md")} - set(ROLES)
    if extra_agents:
        report.error("core/agents", f"only the four kit roles are allowed, found extra: {sorted(extra_agents)}")
    for rel in [src for src, _ in INSTALL_MAP if src.endswith(".md")]:
        if (kit_root / rel).is_file():
            check_references(read_text(kit_root / rel), report, rel, exists)
    template = kit_root / "templates" / CONTEXT_FILE
    if template.is_file():
        validate_context(read_text(template), report, f"templates/{CONTEXT_FILE}", template=True)
    else:
        report.error(f"templates/{CONTEXT_FILE}", "missing")
    cp_template = kit_root / "core" / "docs" / "CHECKPOINT_TEMPLATE.md"
    if cp_template.is_file():
        text = read_text(cp_template)
        for section in cpmod.SECTIONS:
            if f"## {section}" not in text:
                report.error("core/docs/CHECKPOINT_TEMPLATE.md", f"section missing: {section}")
        for field in cpmod.STATE_FIELDS:
            if f"- {field}:" not in text:
                report.error("core/docs/CHECKPOINT_TEMPLATE.md", f"State field missing: {field}")
    sh = kit_root / "core" / "hooks" / "kit_guard.sh"
    if sh.is_file() and b"\r" in sh.read_bytes():
        report.error("core/hooks/kit_guard.sh", "has CRLF line endings; bash needs LF")
    terms = set()
    adapters = sorted(p for p in (kit_root / "adapters").glob("*/") if p.is_dir()) if (kit_root / "adapters").is_dir() else []
    for adapter in adapters:
        context = adapter / CONTEXT_FILE
        where = str(context.relative_to(kit_root))
        if not context.is_file():
            report.error(where, "every adapter needs a PROJECT_CONTEXT.md")
            continue
        config = validate_context(read_text(context), report, where) or {}
        terms |= {t for t in [config.get("project_name", "")] + list(config.get("core_must_not_mention", [])) if t}
    for pattern in CORE_GLOBS:
        for path in kit_root.glob(pattern):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            for term in sorted(terms):
                if term.lower() in text:
                    report.error(str(path.relative_to(kit_root)),
                                 f"core file mentions adapter term {term!r}; move it to the adapter")
    return report


def run(layout, project=None):
    if project is not None:
        return validate_project(project)
    if layout.is_source:
        return validate_kit(layout.kit_root)
    if layout.project_root is None:
        raise KitError("cannot find the project root; pass --project")
    return validate_project(layout.project_root)
