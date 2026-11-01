"""Shared paths, file layout, git helpers and glob matching."""

import importlib.util
import os
import re
import subprocess
from pathlib import Path


KIT_ROOT = Path(__file__).resolve().parent.parent
ROLES = ("kit-researcher", "kit-auditor", "kit-developer", "kit-reviewer")
INSTALL_DIR = ".claude/agent-kit"
CONTEXT_FILE = "PROJECT_CONTEXT.md"
SETTINGS_FILE = ".claude/settings.json"
CORE_MAX_FIX_ROUNDS = 3
DEFAULT_MAX_FIX_ROUNDS = 2

# Source path (relative to the kit root) -> installed path (relative to the
# project root). Every installed file here is kit-owned: the manifest records
# its hash and the validator rejects local edits. Project-specific content
# belongs in PROJECT_CONTEXT.md, never in these files.
INSTALL_MAP = (
    [(f"core/agents/{role}.md", f".claude/agents/{role}.md") for role in ROLES]
    + [
        ("core/commands/kit-task.md", ".claude/commands/kit-task.md"),
        ("core/commands/kit-resume.md", ".claude/commands/kit-resume.md"),
        ("core/docs/ORCHESTRATION.md", f"{INSTALL_DIR}/ORCHESTRATION.md"),
        ("core/docs/SAFETY_POLICY.md", f"{INSTALL_DIR}/SAFETY_POLICY.md"),
        ("core/docs/CHECKPOINT_TEMPLATE.md", f"{INSTALL_DIR}/CHECKPOINT_TEMPLATE.md"),
        ("core/hooks/kit_guard.py", f"{INSTALL_DIR}/hooks/kit_guard.py"),
        ("core/hooks/kit_guard.sh", f"{INSTALL_DIR}/hooks/kit_guard.sh"),
        (".gitattributes", f"{INSTALL_DIR}/.gitattributes"),
        ("kit.py", f"{INSTALL_DIR}/kit.py"),
        ("kitlib/__init__.py", f"{INSTALL_DIR}/kitlib/__init__.py"),
        ("kitlib/common.py", f"{INSTALL_DIR}/kitlib/common.py"),
        ("kitlib/checkpoint.py", f"{INSTALL_DIR}/kitlib/checkpoint.py"),
        ("kitlib/workflow.py", f"{INSTALL_DIR}/kitlib/workflow.py"),
        ("kitlib/validate.py", f"{INSTALL_DIR}/kitlib/validate.py"),
        ("kitlib/install.py", f"{INSTALL_DIR}/kitlib/install.py"),
        ("VERSION", f"{INSTALL_DIR}/VERSION"),
    ]
)
MANIFEST = f"{INSTALL_DIR}/manifest.json"
GUARD_COMMAND = f'bash "$CLAUDE_PROJECT_DIR/{INSTALL_DIR}/hooks/kit_guard.sh"'
HOOK_MATCHER_TOOLS = (
    "Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "Grep", "Glob",
    "Bash", "PowerShell", "Agent", "Task",
)


class KitError(Exception):
    """A user-facing error; the message says what to do."""


class Layout:
    """Where the kit's files are: the source tree or an installed copy."""

    def __init__(self, kit_root=KIT_ROOT):
        self.kit_root = Path(kit_root)
        self.is_source = (self.kit_root / "core").is_dir()
        if self.is_source:
            self.guard_path = self.kit_root / "core" / "hooks" / "kit_guard.py"
            self.project_root = None
        else:
            self.guard_path = self.kit_root / "hooks" / "kit_guard.py"
            installed = self.kit_root.name == "agent-kit" and self.kit_root.parent.name == ".claude"
            self.project_root = self.kit_root.parent.parent if installed else None

    def version(self):
        return (self.kit_root / "VERSION").read_text(encoding="utf-8").strip()


_GUARD_CACHE = {}


def load_guard(path=None):
    """The guard module, the single source of the config parser and the
    protected-path rules."""
    path = Path(path) if path else Layout().guard_path
    key = str(path)
    if key not in _GUARD_CACHE:
        spec = importlib.util.spec_from_file_location("kit_guard_module", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GUARD_CACHE[key] = module
    return _GUARD_CACHE[key]


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def load_context(project_root):
    """(markdown text, config dict) of a project's PROJECT_CONTEXT.md."""
    path = Path(project_root) / INSTALL_DIR / CONTEXT_FILE
    if not path.is_file():
        raise KitError(f"{INSTALL_DIR}/{CONTEXT_FILE} not found; run the installer first")
    text = read_text(path)
    guard = load_guard()
    try:
        return text, guard.parse_config(text)
    except guard.ConfigError as exc:
        raise KitError(f"{CONTEXT_FILE}: {exc}")


def max_fix_rounds(config):
    limits = config.get("loop_limits") or {}
    value = limits.get("max_fix_rounds", DEFAULT_MAX_FIX_ROUNDS)
    if not isinstance(value, int) or isinstance(value, bool):
        return DEFAULT_MAX_FIX_ROUNDS
    return max(0, min(value, CORE_MAX_FIX_ROUNDS))


# -- glob matching for project-relative paths ---------------------------------------

def glob_to_regex(pattern):
    """Gitignore-like glob: no "/" matches a file name at any depth, "**"
    spans directories, "*" and "?" stay within one path segment."""
    p = norm_rel(pattern)
    anywhere = "/" not in p.rstrip("/")
    if p.endswith("/"):
        p += "**"
    out, i = [], 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    body = "".join(out)
    return re.compile(("(?:.*/)?" if anywhere else "") + body + "$", re.IGNORECASE)


def norm_rel(path):
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def path_matches(path, patterns):
    rel = norm_rel(path)
    return any(glob_to_regex(p).match(rel) for p in patterns)


# -- read-only git ---------------------------------------------------------------

def git(project_root, *args):
    """Runs a read-only git command; returns stdout or raises KitError."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    proc = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True, text=True, env=env, check=False,
    )
    if proc.returncode != 0:
        raise KitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def current_branch(project_root):
    return git(project_root, "branch", "--show-current").strip()


def head_commit(project_root):
    try:
        return git(project_root, "rev-parse", "--short=12", "HEAD").strip()
    except KitError:
        return "none"


def changed_paths(project_root):
    """Paths with uncommitted changes (tracked or untracked), POSIX form."""
    out = git(project_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths, entries, i = [], out.split("\0"), 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3:
            status, path = entry[:2], entry[3:]
            paths.append(path)
            if "R" in status or "C" in status:
                i += 1  # the next entry is the rename source
                if i < len(entries) and entries[i]:
                    paths.append(entries[i])
        i += 1
    return sorted(set(paths))


def tracked_paths(project_root):
    return [p for p in git(project_root, "ls-files", "-z").split("\0") if p]


def stat_signature(project_root, rel):
    """size and mtime of a file without reading it, or "absent"."""
    try:
        st = os.stat(Path(project_root) / rel)
    except OSError:
        return "absent"
    return f"size={st.st_size} mtime_ns={st.st_mtime_ns}"
