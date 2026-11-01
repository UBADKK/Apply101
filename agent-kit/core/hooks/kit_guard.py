"""PreToolUse guard for the four agent-kit roles.

Installed as ``.claude/agent-kit/hooks/kit_guard.py`` and run through
``kit_guard.sh``. Standard library only.

Scope
-----
Only tool calls whose hook input carries an ``agent_type`` in
KIT_AGENT_TYPES are checked. The main session (the orchestrator) and every
other agent type are always allowed here; the main session is covered by
the ``permissions.deny`` rules the installer writes into
``.claude/settings.json`` and by the orchestration contract.

Configuration
-------------
Core rules are built in. The project adapter
(``.claude/agent-kit/PROJECT_CONTEXT.md``, ``kit-config`` block) can only
add restrictions: extra protected paths, commands that touch real data,
secret environment variable names and the checkpoint directory. It cannot
remove a core rule. If the adapter cannot be read or is invalid, every call
of a kit agent is blocked (fail closed).

What it blocks for a kit agent (exit code 2, reason on stderr)
--------------------------------------------------------------
* File tools whose path or glob targets a protected path (core secrets and
  key material, ``.git/config``, ``/proc/*/environ``, plus the adapter's
  ``protected_paths``), and file writes into ``.claude/``, ``.git/`` or the
  checkpoint directory.
* Shell commands that name a protected path (outside a small metadata
  allowlist: ls/stat/test, git status/ls-files/check-ignore, echo, and grep
  when the name is only the search pattern), redirect to or from one, or
  write into ``.claude/`` or the checkpoint directory.
* Deleting, moving or overwriting files through the shell; git commands
  other than read-only ones (no commit, push, merge, rebase, reset,
  checkout, switch, restore, stash, clean, fetch, pull, remote, config);
  network, deployment and cloud CLIs; package installs and publishing;
  privilege escalation; process and service control; archives of the
  project root; environment dumps and reads of secret-looking environment
  variables (shell, Python, JavaScript, PowerShell and .NET forms) and
  assignments of variables that make tools run programs; programs started
  or files read and written by viewer tools (sed e/r/w, sort
  --compress-program, git grep -O, rg --pre); root-wide recursive grep and
  unrestricted rg.
* Common migration runners and database clients (CORE_REAL_DATA_COMMANDS)
  and the adapter's ``real_data_commands`` (entry points that open real data
  or live systems), also inside nested shells and heredocs.
* Agent/Task calls: kit agents never start other agents.

Limits -- this is NOT a security boundary
-----------------------------------------
Shell commands are inspected as text. Paths built at run time (variables,
globs, encodings), code run from scripts or test files, symlinks and copies
under other names are not detected. Some harmless commands are blocked
because they name a protected path or a risky word as text; the file tools
avoid that. The guard is one layer next to the settings deny rules, the
agent contracts and the orchestrator's checks.
"""

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path


KIT_AGENT_TYPES = frozenset({
    "kit-researcher",
    "kit-auditor",
    "kit-developer",
    "kit-reviewer",
})
BLOCK_PREFIX = "Blocked by agent-kit guard"
CONFIG_FENCE = "```json kit-config"
CONTEXT_FILE_NAME = "PROJECT_CONTEXT.md"
DEFAULT_CHECKPOINT_DIR = ".claude/agent-kit/checkpoints"

# Always protected, in every project: secrets and key material. Real data
# (databases, uploads, backups) is project-specific and comes from the
# adapter's protected_paths; the template starts with common database files.
# A pattern without "/" is a file-name glob matched at any depth; "name/**"
# protects a directory at any depth.
CORE_PROTECTED_PATHS = (
    ".env", ".env.*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", ".pgpass", ".git-credentials",
    ".ssh/**", ".aws/**", ".gnupg/**", ".kube/**", ".docker/**", "secrets/**",
)
# Kit agents may read but never write these: their own instructions, the
# guard, settings and checkpoints, and git's own directory (hooks there would
# run on the orchestrator's next commit).
CORE_READONLY_DIRS = (".claude", ".git")
# Commands that open real databases or live systems in common stacks. The
# adapter adds its own entry points in real_data_commands.
CORE_REAL_DATA_COMMANDS = (
    r"\bmanage\.py\s+(?:migrate|flush|loaddata|dbshell|sqlflush|runserver|shell)\b",
    r"\balembic\s+(?:upgrade|downgrade|stamp)\b",
    r"\bprisma\s+(?:migrate|db)\b",
    r"\b(?:rails|rake|bin/rails)\s+db:",
    r"\b(?:knex\s+migrate|sequelize\s+db:|typeorm\s+migration:run|diesel\s+migration)",
    r"^(?:flyway|liquibase|psql|mysql|mariadb|mongo|mongosh|redis-cli|sqlcmd|cqlsh)\b",
)

PATH_FIELDS = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path",),
    "Glob": ("path",),
}
GLOB_FIELDS = {"Grep": ("glob",), "Glob": ("pattern",)}
WRITE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
AGENT_TOOLS = frozenset({"Agent", "Task"})


class ConfigError(Exception):
    """The adapter configuration is missing or invalid."""


class GuardError(Exception):
    """The guard cannot decide; the call is blocked."""


# -- configuration --------------------------------------------------------------

def extract_config_block(markdown):
    """Text of the single ```json kit-config block of PROJECT_CONTEXT.md."""
    lines = markdown.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == CONFIG_FENCE]
    if len(starts) != 1:
        raise ConfigError(
            f"expected exactly one '{CONFIG_FENCE}' block, found {len(starts)}"
        )
    for j in range(starts[0] + 1, len(lines)):
        if lines[j].strip() == "```":
            return "\n".join(lines[starts[0] + 1:j])
    raise ConfigError("the kit-config block is not closed")


def parse_config(markdown):
    """The kit-config object of PROJECT_CONTEXT.md as a dict."""
    raw = extract_config_block(markdown)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"kit-config is not valid JSON: {exc.msg} (line {exc.lineno})")
    if not isinstance(data, dict):
        raise ConfigError("kit-config must be a JSON object")
    return data


def compile_path_pattern(pattern):
    """(kind, lower-case glob, original) for a protected path pattern."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"protected path {pattern!r} must be a non-empty string")
    p = pattern.strip()
    if p.endswith("/**"):
        name = p[:-3]
        if name and "/" not in name and "\\" not in name:
            return ("dir", name.lower(), p)
    elif "/" not in p and "\\" not in p:
        return ("name", p.lower(), p)
    raise ConfigError(
        f"protected path {p!r}: use a file-name glob ('*.db', '.env.*') or a "
        "directory name followed by '/**' ('uploads/**')"
    )


def normalize_relative_dir(value, field="checkpoint_dir"):
    """A project-relative directory in POSIX form, without ./ or trailing /."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    p = value.strip().replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("~"):
        raise ConfigError(f"{field} must be relative to the project root")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        raise ConfigError(f"{field} must stay inside the project")
    return "/".join(parts)


def _string_list(config, key):
    value = config.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} must be a list of strings")
    return value


class GuardSettings:
    """What the guard enforces for one project."""

    def __init__(self, config, project_dir=""):
        if not isinstance(config, dict):
            raise ConfigError("kit-config must be a JSON object")
        extra = _string_list(config, "protected_paths")
        self.protected = [compile_path_pattern(p) for p in CORE_PROTECTED_PATHS + tuple(extra)]
        self.real_data = [re.compile(p) for p in CORE_REAL_DATA_COMMANDS]
        for pattern in _string_list(config, "real_data_commands"):
            try:
                self.real_data.append(re.compile(pattern))
            except re.error as exc:
                raise ConfigError(f"real_data_commands entry {pattern!r} is not a valid regex: {exc}")
        self.secret_env = set()
        for name in _string_list(config, "secret_env_vars"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ConfigError(f"secret_env_vars entry {name!r} is not a variable name")
            self.secret_env.add(name.upper())
        self.checkpoint_dir = normalize_relative_dir(
            config.get("checkpoint_dir", DEFAULT_CHECKPOINT_DIR)
        )
        readonly = [d.split("/") for d in CORE_READONLY_DIRS]
        checkpoint_parts = self.checkpoint_dir.split("/")
        if checkpoint_parts[0] not in CORE_READONLY_DIRS:
            readonly.append(checkpoint_parts)
        self.readonly = [[x.lower() for x in parts] for parts in readonly]
        self.project_dir = project_dir


def load_installed_settings():
    """Settings for the project this installed guard belongs to."""
    here = Path(__file__).resolve().parent
    context = here.parent / CONTEXT_FILE_NAME
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(here.parents[2])
    try:
        text = context.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {context.name}: {type(exc).__name__}")
    return GuardSettings(parse_config(text), project_dir)


# -- path matching ----------------------------------------------------------------

def _path_parts(value):
    return [p for p in re.split(r"[\\/]+", value.strip().strip("'\"")) if p and p != "."]


def path_hits(value, settings):
    """Protected patterns a literal path matches."""
    if not isinstance(value, str):
        return set()
    parts = [p.lower() for p in _path_parts(value)]
    if not parts:
        return set()
    hits = set()
    for kind, glob, label in settings.protected:
        if kind == "name" and fnmatch.fnmatchcase(parts[-1], glob):
            hits.add(label)
        elif kind == "dir" and any(fnmatch.fnmatchcase(part, glob) for part in parts):
            hits.add(label)
    if parts[-2:] == [".git", "config"]:
        hits.add(".git/config")  # remote URLs may embed tokens
    if parts[0] == "proc" and parts[-1] == "environ":
        hits.add("/proc/*/environ")
    return hits


_FRAGMENT_SPLIT_RE = re.compile(r"[\s'\"`=:;,|&<>(){}\[\]*?!$]+")


def mention_hits(text, settings):
    """Protected patterns named anywhere in free text."""
    if not isinstance(text, str):
        return set()
    hits = set()
    for fragment in _FRAGMENT_SPLIT_RE.split(text):
        if fragment:
            hits |= path_hits(fragment, settings)
    return hits


def readonly_hit(value, settings):
    """True when a path lies in a directory kit agents may not write."""
    if not isinstance(value, str):
        return False
    parts = [p.lower() for p in _path_parts(value)]
    for ro in settings.readonly:
        for i in range(len(parts) - len(ro) + 1):
            if parts[i:i + len(ro)] == ro:
                return True
    return False


def readonly_mentioned(text, settings):
    if not isinstance(text, str):
        return False
    return any(
        readonly_hit(fragment, settings)
        for fragment in _FRAGMENT_SPLIT_RE.split(text) if fragment
    )


def _describe(hits):
    return ", ".join(sorted(hits))


# -- shell parsing (inspects text only) ---------------------------------------------

_SEPARATOR_CHARS = frozenset("&|;()")
_WRAPPERS = frozenset({
    "command", "builtin", "exec", "time", "nice", "nohup", "env", "xargs",
    "timeout", "stdbuf", "ionice", "winpty",
    "then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "&",
})
_WRAPPER_OPTIONS_WITH_VALUE = {
    "exec": frozenset({"-a"}),
    "env": frozenset({"-u", "-C", "--unset", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "xargs": frozenset({
        "-n", "-L", "-I", "-P", "-d", "-E", "-s", "-a", "--max-args",
        "--max-lines", "--max-procs", "--delimiter", "--eof", "--max-chars",
        "--arg-file",
    }),
}
_WRAPPERS_WITH_POSITIONAL = frozenset({"timeout"})
_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_INTERPRETERS = _POSIX_SHELLS | _POWERSHELLS | frozenset({"cmd", "py", "node", "perl", "ruby", "deno", "bun"})
_MAX_SHELL_DEPTH = 3


def split_segments(command, backslash_paths=False):
    """(tokens, separator before, separator after) for each simple command.
    Splits at && || ; | & ( ) and newlines, honouring quotes. Raises
    ValueError for unbalanced quotes."""
    text = command.replace("\r", "")
    if backslash_paths:
        text = text.replace("\\", "/")
    text = text.replace("`", " ; ").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments, separators, current = [], [""], []
    for token in lexer:
        if token and set(token) <= _SEPARATOR_CHARS:
            if current:
                segments.append(current)
                separators.append("")
                current = []
            separators[-1] += token
        else:
            current.append(token)
    if current:
        segments.append(current)
        separators.append("")
    return [(seg, separators[k], separators[k + 1]) for k, seg in enumerate(segments)]


def _is_pipe(separator):
    return bool(re.search(r"(?<!\|)\|(?!\|)", separator))


def command_name(token):
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def command_of(segment):
    """(command name, args), skipping variable assignments, keywords and
    wrappers such as env, timeout and xargs."""
    i, saw_env = 0, False
    while i < len(segment):
        token = segment[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            i += 1
            continue
        name = command_name(token)
        if name in _WRAPPERS:
            saw_env = saw_env or name == "env"
            with_value = _WRAPPER_OPTIONS_WITH_VALUE.get(name, frozenset())
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                i += 2 if segment[i] in with_value else 1
            if name in _WRAPPERS_WITH_POSITIONAL:
                i += 1
            continue
        return name, segment[i + 1:]
    # "env", "env -0" or "env X=1" without a command prints the environment.
    return ("env", []) if saw_env else ("", [])


def _is_redirect(token):
    """True for redirection operators: > >> < >| &> 2>&1 parts and the like."""
    return bool(token) and set(token) <= set("<>&|0123456789") and ("<" in token or ">" in token)


def _is_fd_duplication(token, target):
    """2>&1, >&2, <&0: duplicates a descriptor, writes no file."""
    return token.endswith("&") and bool(re.fullmatch(r"\d+|-", target))


# -- command rules ------------------------------------------------------------------

_DELETE_OR_OVERWRITE = frozenset({
    "rm", "rmdir", "unlink", "shred", "srm", "del", "erase", "rd", "ri",
    "remove-item", "rimraf", "trash", "trash-put", "mv", "move", "move-item",
    "mi", "ren", "rename", "rename-item", "truncate", "dd", "mkfs",
})
_INLINE_DELETE_RE = re.compile(
    r"shutil\.rmtree|shutil\.move|\bos\.(?:remove|unlink|rmdir|removedirs|rename|replace)\b"
    r"|\.unlink\b|\.rmdir\b"
    r"|\bfs\.(?:rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync|rename|renameSync)\b"
    r"|\.(?:rm|rmSync|unlinkSync|rmdirSync)\s*\("
    r"|\[(?:System\.)?IO\.(?:File|Directory)\]::(?:Delete|Move)"
    r"|\bRemove-Item\b",
    re.IGNORECASE,
)
_INLINE_ENV_DUMP_RE = re.compile(
    r"\bos\.environ\b(?!\s*(?:\[|\.get\b))|\bos\.environb\b|['\"]environb?['\"]"
    r"|%ENV\b|\bENV\.to_h\b"
    r"|\bos\.getenv\s*\(\s*\)|\bSystem\.getenv\s*\(\s*\)",
    re.IGNORECASE,
)
# JavaScript runtimes: process.env, process['env'], Bun.env, Deno.env.
_JS_ENV_RE = re.compile(
    r"\b(?:process\s*(?:\.\s*env\b|\[\s*(['\"`])env\1\s*\])|Bun\s*\.\s*env\b|Deno\s*\.\s*env\b)"
)
# Object.prototype members: calling or reading them on the env object
# exposes the whole environment (process.env['valueOf']()).
_JS_PROTO_MEMBERS = frozenset({
    "valueOf", "toString", "toLocaleString", "toJSON", "hasOwnProperty",
    "isPrototypeOf", "propertyIsEnumerable", "constructor", "__proto__",
    "__defineGetter__", "__defineSetter__", "__lookupGetter__", "__lookupSetter__",
})
_INLINE_NETWORK_RE = re.compile(
    r"\brequests\.(?:get|post|put|patch|delete|request)\b|\burllib\.request\b"
    r"|\bhttp\.client\b|\bhttpx\.|\burlopen\s*\(|\bfetch\s*\(|\bsocket\.socket\b"
    r"|\bsmtplib\b|\bparamiko\b|\bboto3\b",
    re.IGNORECASE,
)
_NETWORK_COMMANDS = frozenset({
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat",
    "telnet", "ftp", "socat", "gh", "hub", "glab", "kubectl", "helm",
    "terraform", "tofu", "pulumi", "ansible", "ansible-playbook", "aws",
    "gcloud", "gsutil", "bq", "az", "heroku", "fly", "flyctl", "railway",
    "vercel", "netlify", "firebase", "doctl", "wrangler", "supabase",
    "serverless", "docker", "docker-compose", "podman", "invoke-webrequest",
    "iwr", "invoke-restmethod", "irm", "start-bitstransfer",
})
_PRIVILEGE_COMMANDS = frozenset({"sudo", "doas", "su", "runas", "pkexec"})
_SYSTEM_COMMANDS = frozenset({
    "kill", "pkill", "killall", "taskkill", "stop-process", "crontab", "systemctl",
    "service", "launchctl", "shutdown", "reboot", "halt", "poweroff", "schtasks",
})
_ARCHIVE_COMMANDS = frozenset({"tar", "zip", "7z", "7za", "rar", "compress-archive"})
_PACKAGE_RULES = {
    "pip": {"install", "uninstall", "download", "wheel"},
    "pip3": {"install", "uninstall", "download", "wheel"},
    "pipx": None, "npx": None, "pnpx": None, "bunx": None, "uvx": None,
    "npm": {"install", "i", "ci", "add", "uninstall", "remove", "rm", "un",
            "update", "up", "upgrade", "publish", "unpublish", "link", "dedupe",
            "exec", "x", "init", "create"},
    "yarn": {"", "add", "install", "remove", "publish", "upgrade", "up", "link", "dlx"},
    "pnpm": {"add", "install", "i", "remove", "rm", "update", "up", "publish", "link", "dlx"},
    "bun": {"add", "install", "i", "remove", "rm", "update", "publish", "link", "x"},
    "poetry": {"add", "install", "remove", "update", "publish", "lock", "self"},
    "uv": {"add", "remove", "sync", "lock", "pip", "tool", "publish"},
    "pdm": {"add", "install", "remove", "update", "sync", "publish", "lock"},
    "cargo": {"install", "uninstall", "publish", "add", "remove", "update"},
    "gem": {"install", "uninstall", "push", "update"},
    "go": {"install", "get"},
    "composer": {"install", "require", "remove", "update"},
    "twine": None, "apt": None, "apt-get": None, "yum": None, "dnf": None,
    "apk": None, "brew": None, "choco": None, "winget": None, "scoop": None,
    "snap": None, "conda": {"install", "create", "update", "remove", "env"},
}
_GIT_READ_ONLY = frozenset({
    "status", "diff", "log", "show", "blame", "annotate", "ls-files",
    "ls-tree", "rev-parse", "rev-list", "describe", "shortlog",
    "check-ignore", "check-attr", "grep", "merge-base", "show-ref",
    "name-rev", "whatchanged", "cat-file", "help", "version", "count-objects",
})
_GIT_LISTING_ONLY = {
    "branch": frozenset({"-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv",
                         "--verbose", "--show-current", "--contains", "--no-contains",
                         "--merged", "--no-merged", "--no-color", "--color",
                         "--points-at", "--column", "--no-column", "-i", "--ignore-case"}),
    "tag": frozenset({"-l", "--list", "-n", "--contains", "--no-contains", "--merged",
                      "--no-merged", "--points-at", "--no-color", "--color"}),
    "stash": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "reflog": frozenset({"show"}),
}
_GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})
_METADATA_COMMANDS = frozenset({"ls", "stat", "test", "[", "[[", "test-path", "get-item", "dir"})
_ECHO_COMMANDS = frozenset({"echo", "printf", "write-host", "write-output"})
_GIT_METADATA = frozenset({"status", "check-ignore", "ls-files"})
_GREP_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg", "git-grep"})
# Commands that only read files (they have no option that writes one). They
# skip the adapter's real-data patterns, and they alone may name .claude/,
# .git/ or the checkpoint directory.
_READ_ONLY_COMMANDS = frozenset({
    "cat", "head", "tail", "ls", "dir", "stat", "wc", "file",
    "grep", "egrep", "fgrep", "rg", "diff", "cmp", "sha256sum", "sha1sum",
    "shasum", "md5sum", "test", "[", "[[", "echo", "printf", "find",
    "get-content", "gc", "type", "get-item", "get-childitem", "gci",
    "test-path", "select-string", "sls", "realpath", "readlink", "basename",
    "dirname", "pwd", "cut", "column", "jq",
})
# Viewers that cannot start a program (sed's e command and sort
# --compress-program are blocked separately): the adapter's real-data
# patterns, which name modules and scripts, do not apply to them.
_REAL_DATA_SKIP = _READ_ONLY_COMMANDS | frozenset({"sed", "sort", "uniq", "less", "more", "tree", "cd"})
# Environment variables that make common tools run other programs.
_DANGEROUS_ENV_RE = re.compile(
    r"^(?:GIT_[A-Z0-9_]+|LESSOPEN|LESSCLOSE|LESSKEY|[A-Z_]*PAGER|EDITOR|VISUAL|LD_[A-Z_]+"
    r"|DYLD_[A-Z_]+|BASH_ENV|ENV|PROMPT_COMMAND|PYTHONSTARTUP|NODE_OPTIONS|PERL5OPT|RUBYOPT"
    r"|SHELLOPTS|BASHOPTS|IFS)$",
    re.IGNORECASE,
)
# Text filters that may receive an allowlisted listing of protected names.
_NAME_SAFE_CONSUMERS = frozenset({
    "head", "tail", "wc", "sort", "uniq", "cut", "tr", "cat", "less", "more",
    "column", "awk", "grep", "egrep", "fgrep", "rg", "findstr",
    "select-object", "format-table", "format-list", "out-string",
    "measure-object", "sort-object",
})
_TEST_RUNNER_RE = re.compile(
    r"^(?:\S*python[\d.]*(?:\.exe)?|py)\s+(?:-\S+\s+)*-m\s+(?:unittest|pytest)\b"
    r"|^(?:\S*[\\/])?pytest\b"
)
_SECRET_NAME_RE = re.compile(
    r"[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|CREDENTIAL)[A-Z0-9_]*",
    re.IGNORECASE,
)
_ENV_EXPANSION_RE = re.compile(
    r"\$env:([A-Za-z_][A-Za-z0-9_]*)|\$\{?([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_]*)%"
    r"|(?:environ(?:\.get)?\s*[\[(]\s*|getenv\s*\(\s*|process\.env\.)['\"]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_CODE_ONLY_INCLUDE_RE = re.compile(
    r"\*\.(?:py|pyi|md|rst|txt|toml|cfg|ini|json|ya?ml|jsx?|tsx?|mjs|cjs|vue|svelte"
    r"|html|css|scss|sql|sh|ps1|go|rs|java|kt|rb|php|cs|c|h|cpp|hpp|swift)",
    re.IGNORECASE,
)
_GREP_SHORT_WITH_VALUE = frozenset("efmABCdD")
_RG_SHORT_WITH_VALUE = frozenset("efgtTmABCMjrE")
_GREP_LONG_WITH_VALUE = frozenset({
    "--regexp", "--file", "--max-count", "--context", "--after-context",
    "--before-context", "--directories", "--devices", "--include",
    "--exclude", "--exclude-dir", "--exclude-from", "--label", "--color",
    "--colour", "--binary-files",
})
_RG_LONG_WITH_VALUE = frozenset({
    "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
    "--type-add", "--max-count", "--context", "--after-context",
    "--before-context", "--max-columns", "--threads", "--max-depth",
    "--maxdepth", "--replace", "--encoding", "--max-filesize", "--color",
    "--colors", "--sort", "--sortr", "--pre", "--pre-glob", "--ignore-file",
})
_UNPARSEABLE_RISK_RE = re.compile(
    r"\b(?:rm|rmdir|unlink|mv|del|erase|rd|sudo|curl|wget|ssh|scp|kubectl|terraform"
    r"|docker|gh|aws|gcloud|az|printenv)\b"
    r"|\bgit\s+(?!status\b|diff\b|log\b|show\b)\w+",
    re.IGNORECASE,
)


def _git_subcommand(args):
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _GIT_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if arg in ("-c", "--config-env", "--exec-path") or arg.startswith(("--exec-path=", "--config-env=")):
            return "-c", []
        if arg.startswith("-"):
            i += 1
            continue
        return arg.lower(), args[i + 1:]
    return "", []


def _check_git(args):
    sub, rest = _git_subcommand(args)
    if sub == "-c":
        return "runs git with configuration overrides (-c/--config-env/--exec-path)"
    if sub in ("", "--version"):
        return None
    if any(a == "--output" or a.startswith("--output=") for a in rest):
        return f"writes files through git {sub} --output"
    if sub == "grep" and any(a.startswith("--open-files-in-pager") or (
            a.startswith("-") and not a.startswith("--") and "O" in a) for a in rest):
        return "git grep -O runs another program on the matching files"
    if sub in _GIT_READ_ONLY:
        return None
    if sub in _GIT_LISTING_ONLY:
        allowed = _GIT_LISTING_ONLY[sub]
        if sub in ("stash", "worktree", "reflog"):
            # A bare "git stash" stashes; a bare "git reflog" only shows.
            if rest[:1] and rest[0] in allowed or (not rest and sub == "reflog"):
                return None
        elif all(a.split("=")[0] in allowed for a in rest if a.startswith("-")) and (
            any(a in ("-l", "--list") for a in rest) or _filters_only(rest)
        ):
            return None
    return f"changes git state or contacts a remote (git {sub}); only the orchestrator does that"


def _filters_only(args):
    """True when every positional of git branch/tag is the value of a
    filter option (--contains <commit>), so the call only lists."""
    takes_value = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
    i = 0
    while i < len(args):
        if args[i] in takes_value:
            i += 2
            continue
        if not args[i].startswith("-"):
            return False
        i += 1
    return True


def _check_packages(name, args):
    if name in _PACKAGE_RULES:
        subs = _PACKAGE_RULES[name]
        first = next((a.lower() for a in args if not a.startswith("-")), "")
        if subs is None or first in subs:
            return f"installs, removes or publishes packages ({name} {first}".rstrip() + ")"
    if (name == "py" or name.startswith("python")) and "-m" in args:
        idx = args.index("-m")
        module = args[idx + 1].lower() if idx + 1 < len(args) else ""
        rest = [a.lower() for a in args[idx + 2:] if not a.startswith("-")]
        if module in ("pip", "pip3") and rest[:1] and rest[0] in _PACKAGE_RULES["pip"]:
            return f"installs or removes packages (-m {module} {rest[0]})"
        if module in ("ensurepip", "twine"):
            return f"installs or publishes packages (-m {module})"
    return None


def _check_env_dump(name, args):
    if name in ("printenv",) or (name == "env" and not args):
        return "prints environment variables, which may hold secrets"
    if name in ("set", "export", "declare", "typeset") and (not args or args[0] in ("-p", "-x", "-px")):
        return "prints environment variables, which may hold secrets"
    if name in ("get-childitem", "gci", "dir", "ls", "get-item") and any(
        a.lower().startswith("env:") for a in args
    ):
        return "lists environment variables, which may hold secrets"
    return None


def _assignments(segment, name, args):
    """Variable names assigned before the command or through export/declare."""
    names = []
    for token in segment[:len(segment) - len(args)]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", token)
        if m:
            names.append(m.group(1))
    if name in ("export", "declare", "typeset", "set-item", "local", "readonly"):
        names += [m.group(1) for a in args for m in [re.match(r"^(?:env:)?([A-Za-z_][A-Za-z0-9_]*)=", a)] if m]
    if name == "env":
        names += [m.group(1) for a in args for m in [re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", a)] if m]
    return names


def _check_dangerous_env(segment, name, args):
    hit = next((n for n in _assignments(segment, name, args) if _DANGEROUS_ENV_RE.match(n)), None)
    if hit:
        return f"sets {hit}, which makes tools run other programs"
    return None


def js_env_access(text):
    """(whole environment exposed?, variable names read) for JavaScript.
    Only a plain ``.NAME`` or ``['NAME']`` read of one variable counts as a
    single read; computed keys, prototype members, calls and any other use
    of the env object count as exposing all of it."""
    dump, names = False, []
    for m in _JS_ENV_RE.finditer(text):
        after = text[m.end():]
        if m.group(0).startswith("Deno"):
            one = re.match(r"\s*\.\s*get\s*\(\s*(['\"`])([A-Za-z_][A-Za-z0-9_]*)\1\s*\)", after)
            if one:
                names.append(one.group(2))
            else:
                dump = True
            continue
        dot = re.match(r"\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)", after)
        bracket = re.match(r"\s*\[\s*(['\"`])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]", after)
        hit = dot or bracket
        if not hit:
            dump = True
            continue
        name = hit.group(1) if dot else hit.group(2)
        if name in _JS_PROTO_MEMBERS or "$" in name or re.match(r"\s*\(", after[hit.end():]):
            dump = True
        else:
            names.append(name)
    return dump, names


def _check_inline_env_dump(segment, text_only, settings):
    if text_only:
        return None
    text = " ".join(segment)
    if _INLINE_ENV_DUMP_RE.search(text):
        return "prints the whole environment from inline code, which may hold secrets"
    dump, names = js_env_access(text)
    if dump:
        return "exposes the whole environment object from inline code, which may hold secrets"
    secret = next((n for n in names if _is_secret_name(n, settings)), None)
    if secret:
        return f"reads a secret-looking environment variable ({secret})"
    return None


def _is_secret_name(name, settings):
    return name.upper() in settings.secret_env or bool(_SECRET_NAME_RE.fullmatch(name))


_PS_ENV_VAR_RE = re.compile(r"\$\{?env:([A-Za-z_][A-Za-z0-9_]*)\}?(\s*[-+*/]?=(?!=))?", re.IGNORECASE)
_PS_ENV_PATH_RE = re.compile(r"(?<![\w$:{])env:[\\/]?([A-Za-z0-9_*?\[\]]*)", re.IGNORECASE)
_PS_ENV_LOCATION_RE = re.compile(
    r"(?:^|[\s;|&(])(?:cd|chdir|sl|set-location|pushd|push-location)\s+"
    r"(?:-(?:literal)?path\s+)?['\"]?env:", re.IGNORECASE)
_DOTNET_ENV_RE = re.compile(
    r"\[(?:System\.)?Environment\]\s*::\s*(GetEnvironmentVariables|GetEnvironmentVariable"
    r"|SetEnvironmentVariable|ExpandEnvironmentVariables)\s*\(\s*(?:(['\"])([^'\"]*)\2)?",
    re.IGNORECASE)


def check_powershell_env(text, settings, powershell):
    """PowerShell and .NET environment access: $env:/${env:} reads of secret
    names and assignments of variables that make tools run programs,
    [Environment]:: calls, and (PowerShell only) the env: drive."""
    for m in _PS_ENV_VAR_RE.finditer(text):
        name, assignment = m.group(1), m.group(2)
        if assignment and _DANGEROUS_ENV_RE.match(name):
            return f"sets {name}, which makes tools run other programs"
        if not assignment and _is_secret_name(name, settings):
            return f"reads a secret-looking environment variable ({name})"
    for m in _DOTNET_ENV_RE.finditer(text):
        method, literal = m.group(1).lower(), m.group(3)
        if method == "getenvironmentvariables":
            return "lists environment variables, which may hold secrets"
        if literal is None:
            return f"calls {m.group(1)} with a name the guard cannot read"
        if method == "getenvironmentvariable" and _is_secret_name(literal, settings):
            return f"reads a secret-looking environment variable ({literal})"
        if method == "setenvironmentvariable" and _DANGEROUS_ENV_RE.match(literal):
            return f"sets {literal}, which makes tools run other programs"
        if method == "expandenvironmentvariables":
            hit = next((n for n in re.findall(r"%([A-Za-z_][A-Za-z0-9_]*)%", literal)
                        if _is_secret_name(n, settings)), None)
            if hit:
                return f"reads a secret-looking environment variable ({hit})"
    if powershell:
        if _PS_ENV_LOCATION_RE.search(text):
            return "moves into the env: drive, where any listing prints environment variables"
        for m in _PS_ENV_PATH_RE.finditer(text):
            name = m.group(1)
            if not name or any(ch in name for ch in "*?["):
                return "lists environment variables, which may hold secrets"
            if _is_secret_name(name, settings):
                return f"reads a secret-looking environment variable ({name})"
            if _DANGEROUS_ENV_RE.match(name):
                return f"touches {name}, which makes tools run other programs"
    return None


def _check_secret_env(text, settings):
    for match in _ENV_EXPANSION_RE.finditer(text):
        var = next(g for g in match.groups() if g)
        if var.upper() in settings.secret_env or _SECRET_NAME_RE.fullmatch(var):
            return f"reads a secret-looking environment variable ({var})"
    if re.search(r"/proc/[^\s/]+/environ", text):
        return "reads a process environment"
    return None


def _check_deletion(name, args, segment, text_only):
    if name in _DELETE_OR_OVERWRITE:
        return f"deletes, moves or overwrites files ({name}); use Edit/Write or ask the orchestrator"
    names = [command_name(token) for token in segment]
    if "xargs" in names:
        hit = next((n for n in names[names.index("xargs") + 1:] if n in _DELETE_OR_OVERWRITE), None)
        if hit:
            return f"deletes, moves or overwrites files (xargs {hit})"
    if name == "find":
        lowered = [a.lower() for a in args]
        if "-delete" in lowered:
            return "deletes files (find -delete)"
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in lowered:
                return f"runs commands on found files (find {flag}); use xargs with a read-only command"
    if name == "cp" or name == "copy-item" or name == "copy":
        return "copies files through the shell (may overwrite); use Write or ask the orchestrator"
    if not text_only and _INLINE_DELETE_RE.search(" ".join(segment)):
        return "deletes or moves files (inline code)"
    return None


def _check_network(name, args, segment, text_only):
    if name in _NETWORK_COMMANDS:
        return f"uses a network, deployment or cloud tool ({name}); only the user does that"
    if not text_only and _INLINE_NETWORK_RE.search(" ".join(segment)):
        return "makes network calls from inline code"
    return None


def _check_real_data(name, args, text_only, settings):
    """Adapter regexes, matched against the command after wrappers
    (``timeout 60 uvicorn ...`` is checked as ``uvicorn ...``)."""
    if text_only or name in _REAL_DATA_SKIP or name == "git":
        return None
    text = " ".join([name] + list(args))
    for regex in settings.real_data:
        if regex.search(text):
            return f"runs a command the project marks as touching real data ({regex.pattern})"
    return None


def _option_value(args, i, eq, val):
    if eq:
        return val, i
    if i + 1 < len(args):
        return args[i + 1], i + 1
    return "", i


def _norm_dir(path):
    p = path.strip().strip("'\"").replace("\\", "/")
    if p.endswith("/*"):
        p = p[:-2]
    p = p.rstrip("/")
    m = re.match(r"^/([A-Za-z])(?=/|$)", p)
    if m:
        p = m.group(1) + ":" + p[2:]
    return p.lower()


def _reaches_project_root(path, settings):
    raw = path.strip().strip("'\"")
    p = _norm_dir(os.path.expanduser(os.path.expandvars(raw)))
    if p in ("", ".", "*", "~") or p == ".." or p.startswith(("../", "./..")) or raw.startswith("$"):
        return True
    project = _norm_dir(settings.project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ""))
    return bool(project) and (project == p or project.startswith(p + "/"))


def parse_grep(name, args):
    """(paths, other risky values, recursive, unrestricted, includes) of a
    grep/rg call; the search pattern itself is not a path."""
    is_rg = name == "rg"
    short_with_value = _RG_SHORT_WITH_VALUE if is_rg else _GREP_SHORT_WITH_VALUE
    long_with_value = _RG_LONG_WITH_VALUE if is_rg else _GREP_LONG_WITH_VALUE
    patterns_given = recursive = unrestricted = False
    risky, positional, includes = [], [], []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1:])
            break
        if _is_redirect(arg):
            if i + 1 < len(args):
                risky.append(args[i + 1])
            i += 2
            continue
        if arg.startswith("--"):
            key, eq, val = arg.partition("=")
            key = key.lower()
            if key in long_with_value:
                value, i = _option_value(args, i, eq, val)
                if key == "--regexp":
                    patterns_given = True
                elif key in ("--exclude", "--exclude-dir"):
                    pass
                elif key in ("--glob", "--iglob") and value.startswith("!"):
                    pass
                elif key in ("--glob", "--iglob", "--include"):
                    includes.append(value)
                    risky.append(value)
                elif key == "--directories":
                    recursive = recursive or value.lower() == "recurse"
                else:
                    patterns_given = patterns_given or key == "--file"
                    risky.append(value)
            elif key in ("--recursive", "--dereference-recursive") and not is_rg:
                recursive = True
            elif is_rg and (key in ("--hidden", "--unrestricted") or key.startswith("--no-ignore")):
                unrestricted = True
            elif eq:
                risky.append(val)
        elif arg.startswith("-") and len(arg) > 1:
            for pos, flag in enumerate(arg[1:], start=1):
                if flag in short_with_value:
                    rest = arg[pos + 1:]
                    if rest:
                        value = rest
                    elif i + 1 < len(args):
                        i += 1
                        value = args[i]
                    else:
                        value = ""
                    if flag == "e":
                        patterns_given = True
                    elif flag == "f":
                        patterns_given = True
                        risky.append(value)
                    elif flag == "g" and is_rg:
                        if not value.startswith("!"):
                            includes.append(value)
                            risky.append(value)
                    elif flag == "d" and not is_rg:
                        recursive = recursive or value.lower() == "recurse"
                    break
                if flag in "rR" and not is_rg:
                    recursive = True
                elif flag in "u." and is_rg:
                    unrestricted = True
        else:
            positional.append(arg)
        i += 1
    paths = positional if patterns_given else positional[1:]
    return paths, risky, recursive, unrestricted, includes


def check_grep(name, args, settings, root_check=True):
    paths, risky, recursive, unrestricted, includes = parse_grep(name, args)
    for token in paths + risky:
        hits = mention_hits(token, settings)
        if hits:
            return f"{name} reads a protected path ({_describe(hits)})"
    if not root_check:
        return None
    reaches_root = not paths or any(_reaches_project_root(p, settings) for p in paths)
    if name == "rg" and unrestricted and reaches_root:
        return ("rg searches the project root without ignore rules (-u/--hidden/--no-ignore); "
                "search a subdirectory or drop those flags")
    code_only = bool(includes) and all(
        _CODE_ONLY_INCLUDE_RE.fullmatch(v.strip("'\"")) for v in includes
    )
    if name != "rg" and recursive and reaches_root and not code_only:
        return (f"{name} searches the project root recursively; use the Grep tool, search a "
                "subdirectory, or add --include='*.py' style source filters")
    return None


_SLS_PARAMS_WITH_VALUE = frozenset({
    "-pattern", "-path", "-literalpath", "-include", "-exclude", "-context",
    "-encoding", "-culture",
})


def check_select_string(args, settings):
    """Select-String may name protected paths only in -Pattern/-Exclude."""
    risky, positional = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        key = arg.lower()
        if _is_redirect(arg):
            if i + 1 < len(args):
                risky.append(args[i + 1])
            i += 2
            continue
        if key in _SLS_PARAMS_WITH_VALUE:
            value = args[i + 1] if i + 1 < len(args) else ""
            i += 1
            if key not in ("-pattern", "-exclude"):
                risky.append(value)
        elif not arg.startswith("-"):
            positional.append(arg)
        i += 1
    for token in positional[1:] + risky:
        hits = mention_hits(token, settings)
        if hits:
            return f"Select-String reads a protected path ({_describe(hits)})"
    return None


def _is_interpreter(name):
    return name in _INTERPRETERS or name.startswith("python")


def _is_text_only(name, args, piped_to):
    """True when the arguments are patterns or messages, not code."""
    if piped_to is not None and _is_interpreter(piped_to):
        return False
    if name in _GREP_COMMANDS | _ECHO_COMMANDS | {"select-string", "sls"}:
        return True
    return name == "git" and _git_subcommand(args)[0] in ("grep", "log", "show", "diff")


def _nested_command(name, args):
    """(command text, backslash_paths) run by a nested shell, or None."""
    if name == "eval":
        return " ".join(args), False
    if name in _POSIX_SHELLS:
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("-o", "+o", "-O", "+O"):
                i += 2
                continue
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg):
                return (args[i + 1] if i + 1 < len(args) else ""), False
            if not arg.startswith(("-", "+")):
                return None
            i += 1
        return None
    if name == "cmd":
        for i, arg in enumerate(args):
            if re.fullmatch(r"/+[ck]", arg, re.IGNORECASE):
                return " ".join(args[i + 1:]), True
        return None
    if name in _POWERSHELLS:
        for i, arg in enumerate(args):
            if re.fullmatch(r"-(?:c|co|com|comm|comma|comman|command|e|ec|encodedcommand)", arg, re.IGNORECASE):
                if arg.lower().startswith("-e"):
                    return "\x00encoded", True
                return " ".join(args[i + 1:]), True
    return None


def _pipe_consumer(parts, k):
    if k + 1 >= len(parts) or not _is_pipe(parts[k][2]):
        return None
    following = parts[k + 1][0]
    if any(command_name(token) == "xargs" for token in following):
        return "xargs"
    return command_of(following)[0]


def _lists_names(name, args):
    if name in _METADATA_COMMANDS or name in _ECHO_COMMANDS:
        return True
    return name == "git" and _git_subcommand(args)[0] in _GIT_METADATA


def _check_name_flow(parts, k):
    """A listing of protected names may feed text filters only."""
    j = k
    consumer = _pipe_consumer(parts, j)
    while consumer is not None:
        if consumer not in _NAME_SAFE_CONSUMERS:
            return f"pipes protected path names into {consumer or 'another command'}"
        j += 1
        consumer = _pipe_consumer(parts, j)
    if k > 0 and parts[k][1] == "(":
        outer = command_of(parts[k - 1][0])[0]
        if outer and not outer.startswith("-") and outer not in _METADATA_COMMANDS | _ECHO_COMMANDS | {"if"}:
            return f"passes protected path names to {outer}"
    return None


def _check_protected_segment(name, args, hits, settings, root_check):
    if name in _METADATA_COMMANDS and not any(h.endswith("/**") for h in hits):
        return None
    if name in _ECHO_COMMANDS:
        return None
    if name == "git":
        sub, rest = _git_subcommand(args)
        if sub in _GIT_METADATA:
            return None
        if sub == "grep":
            return check_grep("git-grep", rest, settings, root_check=False)
    if name in _GREP_COMMANDS:
        return check_grep(name, args, settings, root_check)
    if name in ("select-string", "sls"):
        return check_select_string(args, settings)
    return f"{name or 'command'} references a protected path ({_describe(hits)})"


_SED_ADDRESS = r"(?:\d+(?:~\d+)?|\$|/(?:[^/\\]|\\.)*/[IM]*)"
_SED_ADDRESS_RE = re.compile(
    rf"^(?:{_SED_ADDRESS}(?:\s*,\s*(?:{_SED_ADDRESS}|[+~]\d+))?)?\s*!?\s*")
_SED_SIMPLE_RE = re.compile(r"[pPdDnNgGhHxz=]|[qQ]\s*\d*|l\s*\d*")


def _sed_delimited(rest, parts):
    """Index after ``parts`` delimiter-terminated fields of an s or y
    command (rest[0] is the command letter), or None."""
    if len(rest) < 2:
        return None
    delim = rest[1]
    if delim in "\\\n" or delim.isalnum() or delim.isspace():
        return None
    i, found = 2, 0
    while i < len(rest):
        if rest[i] == "\\":
            i += 2
            continue
        if rest[i] == delim:
            found += 1
            if found == parts:
                return i + 1
        i += 1
    return None


def _sed_command_safe(command):
    """True for sed commands that neither run programs (e, s///e) nor read
    or write files (r, R, w, W, s///w): p, d, s, y, a/i/c and the like."""
    if command.startswith("#"):
        return True
    rest = command[_SED_ADDRESS_RE.match(command).end():]
    if _SED_SIMPLE_RE.fullmatch(rest):
        return True
    if rest[:1] in ("a", "i", "c") and (len(rest) == 1 or rest[1] in " \t\\"):
        return True
    if rest.startswith("s"):
        end = _sed_delimited(rest, 2)
        return end is not None and re.fullmatch(r"[gpiImM0-9]*", rest[end:]) is not None
    if rest.startswith("y"):
        end = _sed_delimited(rest, 2)
        return end is not None and not rest[end:].strip()
    return False


def _sed_scripts(args):
    """(scripts, unreadable) of a sed call: -e/--expression values or the
    first operand; -f/--file scripts cannot be inspected."""
    scripts, unreadable, explicit, positional = [], False, False, []
    sandbox = "--sandbox" in args
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1:])
            break
        if arg.startswith("--"):
            key, eq, value = arg.partition("=")
            if key.startswith("--e"):  # --expression
                if not eq and i + 1 < len(args):
                    i += 1
                    value = args[i]
                scripts.append(value)
                explicit = True
            elif key.startswith("--f"):  # --file
                unreadable = explicit = True
                if not eq:
                    i += 1
            elif key.startswith("--l") and not eq:  # --line-length N
                i += 1
        elif arg.startswith("-") and len(arg) > 1:
            for j in range(1, len(arg)):
                flag = arg[j]
                if flag in "efl":
                    value = arg[j + 1:]
                    if not value and i + 1 < len(args):
                        i += 1
                        value = args[i]
                    if flag == "e":
                        scripts.append(value)
                        explicit = True
                    elif flag == "f":
                        unreadable = explicit = True
                    break
                if flag == "i":
                    break  # the rest is a backup suffix
        else:
            positional.append(arg)
        i += 1
    if not explicit and positional:
        scripts.append(positional[0])
    return scripts, unreadable and not sandbox, sandbox


def _check_sed(args):
    scripts, unreadable, sandbox = _sed_scripts(args)
    if unreadable:
        return "sed reads its script from a file the guard cannot inspect"
    if sandbox:
        return None  # GNU sed --sandbox rejects e, r and w
    for script in scripts:
        for command in re.split(r"[;\n]", script):
            command = command.strip()
            if command and not _sed_command_safe(command):
                return ("sed script uses a command the guard cannot verify (e runs programs; "
                        "r, R, w and W read or write files; blocks); use plain p, d, s or y "
                        "commands, or the Read/Edit tools")
    return None


def check_shell(command, settings, backslash_paths=False, depth=0):
    """Block reason for a shell command, or None."""
    if not isinstance(command, str):
        raise GuardError("shell command is not a string")
    if command == "\x00encoded":
        return "runs an encoded PowerShell command the guard cannot inspect"
    if depth > _MAX_SHELL_DEPTH:
        return "nests shells too deeply for the guard to inspect"
    reason = check_powershell_env(command, settings, powershell=backslash_paths)
    if reason:
        return reason
    raw_hits = mention_hits(command, settings)
    try:
        parts = split_segments(command, backslash_paths)
    except ValueError:
        if raw_hits:
            return f"unparseable command references a protected path ({_describe(raw_hits)})"
        if _UNPARSEABLE_RISK_RE.search(command) or _INLINE_DELETE_RE.search(command):
            return "unparseable command (unbalanced quotes) contains a risky command; use the file tools"
        if any(r.search(command) for r in settings.real_data):
            return "unparseable command references a real-data command"
        return None
    attributed = set()
    for k, (segment, _before, _after) in enumerate(parts):
        name, args = command_of(segment)
        piped_to = _pipe_consumer(parts, k)
        nested = _nested_command(name, args)
        if nested is not None:
            reason = check_shell(nested[0], settings, nested[1], depth + 1)
            if reason:
                return f"{reason} (inside {name})"
        text_only = _is_text_only(name, args, piped_to)
        prefix = segment[:len(segment) - len(args) - 1]
        from_xargs = any(command_name(token) == "xargs" for token in prefix)
        if name in _PRIVILEGE_COMMANDS:
            return f"uses elevated privileges ({name})"
        if name in _SYSTEM_COMMANDS:
            return f"controls processes or system services ({name})"
        if name in _ARCHIVE_COMMANDS and any(
            _reaches_project_root(a, settings) for a in args if not a.startswith("-")
        ):
            return f"archives the project root ({name}), which would include protected files"
        reason = (
            _check_env_dump(name, args)
            or _check_deletion(name, args, segment, text_only)
            or (_check_git(args) if name == "git" else None)
            or _check_network(name, args, segment, text_only)
            or _check_packages(name, args)
            or _check_real_data(name, args, text_only, settings)
            or _check_secret_env(" ".join(segment), settings)
            or _check_inline_env_dump(segment, text_only, settings)
            or (_check_sed(args) if name in ("sed", "gsed") else None)
            or ("sort --compress-program runs another program" if name == "sort" and any(
                a.startswith("--co") for a in args) else None)
            or _check_dangerous_env(segment, name, args)
            or (f"{name} --pre runs a preprocessor command" if name == "rg" and any(
                a.startswith(("--pre", "--pre-glob")) for a in args) else None)
            or (check_grep(name, args, settings, root_check=not from_xargs) if name in _GREP_COMMANDS else None)
        )
        if reason:
            return reason
        for idx, token in enumerate(args):
            if _is_redirect(token) and idx + 1 < len(args):
                target = args[idx + 1]
                if _is_fd_duplication(token, target):
                    continue
                hits = path_hits(target, settings) | mention_hits(target, settings)
                if hits:
                    return f"redirects to or from a protected path ({_describe(hits)})"
                if ">" in token and readonly_hit(target, settings):
                    return "writes into .claude/, .git/ or the checkpoint directory"
        if readonly_mentioned(" ".join(segment), settings):
            writes = name == "find" and any(
                a.lower() in ("-fprint", "-fprint0", "-fprintf", "-fls") for a in args)
            runs_tests = bool(_TEST_RUNNER_RE.search(" ".join([name] + args)))
            if writes or (name not in _READ_ONLY_COMMANDS and name != "git" and not runs_tests):
                return (f"{name or 'command'} may write into .claude/ or the checkpoint "
                        "directory; kit agents only read there")
        seg_hits = set()
        for token in segment:
            seg_hits |= mention_hits(token, settings)
        if not seg_hits:
            continue
        attributed |= seg_hits
        reason = _check_protected_segment(name, args, seg_hits, settings, not from_xargs)
        if reason is None and _lists_names(name, args):
            reason = _check_name_flow(parts, k)
        if reason:
            return reason
    unattributed = raw_hits - attributed
    if unattributed:
        return f"command references a protected path in a form the guard cannot attribute ({_describe(unattributed)})"
    return None


# The reviewer is read-only: these commands only, no output redirection
# except to /dev/null or another descriptor.
_REVIEWER_COMMANDS = frozenset({
    "git", "cat", "head", "tail", "ls", "dir", "wc", "grep", "egrep", "fgrep", "rg",
    "diff", "cmp", "stat", "file", "find", "cut", "tr", "column", "pwd", "cd", "echo",
    "printf", "test", "[", "[[", "basename", "dirname", "realpath", "readlink",
    "sha256sum", "sha1sum", "md5sum", "true", "false", "get-content", "gc", "type",
    "select-string", "sls", "get-childitem", "gci", "test-path", "get-item",
})


def check_reviewer_shell(command, backslash_paths=False):
    """Extra rule for kit-reviewer: only read-only commands."""
    try:
        parts = split_segments(command, backslash_paths)
    except ValueError:
        return "the reviewer's shell commands must parse; use the file tools"
    for segment, _before, _after in parts:
        name, args = command_of(segment)
        if any(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) for t in segment[:len(segment) - len(args)]):
            return "the reviewer does not set environment variables for commands"
        if not name:
            continue
        if name not in _REVIEWER_COMMANDS:
            return f"the reviewer only runs read-only commands; {name} is not one (use the Read tool)"
        for idx, token in enumerate(args):
            if _is_redirect(token) and ">" in token:
                target = args[idx + 1] if idx + 1 < len(args) else ""
                if not _is_fd_duplication(token, target) and target.lower() not in ("/dev/null", "nul", "$null"):
                    return "the reviewer does not write files (output redirection)"
        if name == "find" and any(a.lower() in ("-fprint", "-fprint0", "-fprintf", "-fls", "-exec",
                                                "-execdir", "-ok", "-okdir", "-delete") for a in args):
            return "the reviewer does not write files or run commands through find"
    return None


# -- entry point ------------------------------------------------------------------

def decide(payload, settings_loader=load_installed_settings):
    """Block reason (str) or None to allow."""
    if not isinstance(payload, dict):
        raise GuardError("hook input is not a JSON object")
    if payload.get("agent_type") not in KIT_AGENT_TYPES:
        return None
    settings = settings_loader() if callable(settings_loader) else settings_loader
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise GuardError("hook input has no tool_name/tool_input")
    if tool_name in AGENT_TOOLS:
        return "kit agents never start other agents; report to the orchestrator"
    for field in PATH_FIELDS.get(tool_name, ()):
        value = tool_input.get(field)
        hits = path_hits(value, settings)
        if hits:
            return f"{tool_name} targets a protected path ({_describe(hits)})"
        if tool_name in WRITE_TOOLS and readonly_hit(value, settings):
            return f"{tool_name} writes into .claude/ or the checkpoint directory"
    for field in GLOB_FIELDS.get(tool_name, ()):
        value = tool_input.get(field)
        if isinstance(value, str) and not value.startswith("!"):
            hits = mention_hits(value, settings)
            if hits:
                return f"{tool_name} selects a protected path ({_describe(hits)})"
    if tool_name in SHELL_TOOLS:
        backslash = tool_name == "PowerShell"
        reason = check_shell(tool_input.get("command"), settings, backslash_paths=backslash)
        if reason is None and payload.get("agent_type") == "kit-reviewer":
            reason = check_reviewer_shell(tool_input.get("command"), backslash)
        return reason
    return None


_KIT_AGENT_RE = re.compile(r'"agent_type"\s*:\s*"kit-(?:researcher|auditor|developer|reviewer)"')


def main(stdin=None, stderr=None, settings_loader=load_installed_settings):
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stderr = stderr if stderr is not None else sys.stderr
    raw = b""
    try:
        raw = stdin.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise GuardError("hook input is not a JSON object")
    except Exception:
        # Unreadable input: block only when it names a kit agent, like the
        # wrapper does when no Python is available; never lock out the main session.
        if not _KIT_AGENT_RE.search(raw.decode("utf-8", "replace")):
            return 0
        payload = None
    try:
        if payload is None:
            raise GuardError("hook input is not valid JSON")
        reason = decide(payload, settings_loader)
    except ConfigError as exc:
        reason = f"the project adapter is invalid ({str(exc)[:200]}); run the kit validator"
    except Exception as exc:  # fail closed
        reason = f"guard could not decide ({type(exc).__name__})"
    if reason is None:
        return 0
    stderr.write(
        f"{BLOCK_PREFIX}: {reason}. Kit agents must not read secrets or real data, "
        "change git state, delete files, use the network or deploy; stop and report "
        "to the orchestrator instead.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
