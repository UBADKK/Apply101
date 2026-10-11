"""PreToolUse guard for Apply101's four project subagents.

Scope
-----
Only tool calls whose hook input carries an ``agent_type`` in
GUARDED_AGENT_TYPES are checked. The main session (no ``agent_type``) and
every other agent type are always allowed by this hook, so operations the
user explicitly asks the main session to run (e.g. a migration on the real
database) are never blocked here. A session started with
``claude --agent project-supervisor`` may report that ``agent_type`` too and
is then guarded like the subagent.

What it blocks for a guarded agent (exit code 2, reason on stderr)
------------------------------------------------------------------
* File tools (Read, Edit, MultiEdit, Write, NotebookEdit, Grep, Glob) whose
  path, glob or pattern targets a protected path.
* Shell commands (Bash, PowerShell) that name a protected path outside a
  small allowlist (``ls``/``stat``/``test``/``Test-Path``/``Get-Item``
  metadata for apply101.db* and .env, ``git status``/``check-ignore``/
  ``ls-files``, ``echo``/``printf`` without redirection, and grep/rg/
  Select-String when the name only appears in the search pattern or in an
  exclusion). Allowlisted listings of protected names may only be piped
  (along the whole pipeline) into text filters (head, sort, awk,
  Select-Object, ...), not into xargs, readers or hashers, nor passed to
  another command via ``$(...)``/``(...)``.
* Shell commands that redirect to or from a protected path, that delete
  files (rm, rmdir, unlink, del, erase, rd, Remove-Item, git clean, git rm,
  find -delete/-exec rm, xargs rm, shutil.rmtree, os.remove, ...), or that
  run or import the known real-database entry points (backend.migrations.*,
  backend.scripts.*, backend.app.main, uvicorn) -- also behind wrappers
  (sudo, env, timeout, nice, winpty, ...), ``git -C``/``-c``, nested
  ``bash -c``/``sh -c``/``cmd /c``/``powershell -Command`` and heredocs.
* Recursive grep (``-r``/``-R``) and unrestricted rg (``-u``, ``--hidden``,
  ``--no-ignore``) over the repository root or above (also given as
  ``$CLAUDE_PROJECT_DIR`` or ``~``), unless every .env*/uploads name (and
  apply101.db* in text mode) is excluded -- ``--exclude=.env*
  --exclude-dir=uploads`` or ``-g '!.env*' -g '!uploads'`` -- or only
  source/doc files are included (``--include='*.py'``).
* Agent/Task calls that would start an agent type outside
  GUARDED_AGENT_TYPES (such an agent would not be covered by this guard).

Protected paths: any basename starting with ``apply101.db`` (the SQLite
file and its -wal/-shm/-journal companions), ``.env`` and ``.env.*``, and
anything inside a directory named ``uploads``.

Limits -- this is NOT a security boundary
-----------------------------------------
Shell commands are inspected as text. Not detected: paths built at run time
(variables, globs such as ``apply101.d?``, command substitution into
variables, encodings), access from inside scripts or test code that a
command runs (``bash script.sh``, ``python -m unittest`` importing the app),
symlinks/copies under other names, overwrites and discards that are not
deletions (``mv``/``cp`` onto a file, ``git checkout``/``reset``), deletion
through other library calls (``subprocess.run(['rm', ...])``), PowerShell
``-EncodedCommand`` and ``env -S``. Grep/Glob tool calls and plain rg over a
directory rely on the tool's own ignore rules for files inside it. Some
harmless commands are blocked because they name a protected path, a
deletion call or an entry point as text (``sed -n '/uploads/p' f``,
``git log -- .env``, a heredoc writing code that mentions ``os.remove``);
the file tools avoid that. The main session and non-project agents are
intentionally not covered.

Failure handling
----------------
When the input cannot be read, or an error happens while deciding for a
guarded agent, the call is blocked. The hook runs through
agent_file_guard.sh, which uses the first Python that can run this file and,
if none can, blocks only guarded project agents. The settings entry uses
``onFailure: "block"`` (Claude Code 2.1.295+): if bash cannot start the
wrapper or it times out, every matched call -- the main session's too -- is
blocked until the hook is fixed or disabled. Empty or unreadable hook input
blocks every call as well, since the agent type is then unknown. Recovery:
fix the files from a terminal, or remove the PreToolUse entry from
.claude/settings.json (or set ``"disableAllHooks": true``).
"""

import fnmatch
import json
import os
import re
import shlex
import sys


GUARDED_AGENT_TYPES = frozenset({
    "project-auditor",
    "project-developer",
    "project-reviewer",
    "project-supervisor",
})

# Tool input fields holding a literal path.
PATH_FIELDS = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path",),
    "Glob": ("path",),
}
# Tool input fields holding a glob that selects files (not a search regex).
GLOB_FIELDS = {
    "Grep": ("glob",),
    "Glob": ("pattern",),
}
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
AGENT_TOOLS = frozenset({"Agent", "Task"})

_DB_RE = re.compile(r"apply101\.db", re.IGNORECASE)
_ENV_RE = re.compile(
    r"(?:^|[\s'\"=/\\:(,{\[])\.env(?:\.[\w.-]*)?(?=$|[\s'\"/\\;|&)<>,}\]*?])",
    re.IGNORECASE,
)
_UPLOADS_RE = re.compile(
    r"(?:^|[\s'\"=/\\:(,{\[])uploads(?=$|[\s'\"/\\;|&)<>,}\]*?])",
    re.IGNORECASE,
)

_DELETE_COMMANDS = frozenset({
    "rm", "rmdir", "unlink", "shred", "srm", "del", "erase", "rd", "ri",
    "remove-item", "rimraf", "trash", "trash-put",
})
_INLINE_DELETE_RE = re.compile(
    r"shutil\.rmtree|\bos\.(?:remove|unlink|rmdir|removedirs)\b"
    r"|\.unlink\b|\.rmdir\b"
    r"|\bfs\.(?:rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\b"
    r"|\.(?:rm|rmSync|unlinkSync|rmdirSync)\s*\("
    r"|\[(?:System\.)?IO\.(?:File|Directory)\]::Delete"
    r"|\bRemove-Item\b",
    re.IGNORECASE,
)
# Python options that take a separate value.
_PYTHON_OPTIONS_WITH_VALUE = frozenset({"-W", "-X", "-Q"})
# Modules/scripts that open the real database through the app's default
# engine when run directly.
_REAL_DB_MODULE_RE = re.compile(
    r"^backend\.(?:migrations|scripts)\.|^backend\.app\.main$|^uvicorn$"
)
_REAL_DB_SCRIPT_RE = re.compile(
    r"backend[\\/](?:migrations|scripts)[\\/]|backend[\\/]app[\\/]main\.py$"
)
_REAL_DB_IMPORT = (
    r"backend\.(?:migrations|scripts)\b|backend\.app\.main\b"
    r"|\bfrom\s+backend\.app\s+import\b[^;\n]*\bmain\b"
    r"|\bfrom\s+backend\s+import\b[^;\n]*\b(?:migrations|scripts)\b"
)
# Code naming a real-database entry point: in ``python -c`` code a bare
# "uvicorn" counts, anywhere else (heredocs, piped code) only uvicorn.run.
_INLINE_REAL_DB_RE = re.compile(_REAL_DB_IMPORT + r"|\buvicorn\b")
_REAL_DB_REFERENCE_RE = re.compile(_REAL_DB_IMPORT + r"|\buvicorn\.run\b")

_WRAPPERS = frozenset({
    "sudo", "doas", "command", "builtin", "exec", "time", "nice", "nohup",
    "env", "xargs", "timeout", "stdbuf", "ionice", "winpty",
    "then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "&",
})
# Wrapper options whose value is a separate token.
_WRAPPER_OPTIONS_WITH_VALUE = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"}),
    "doas": frozenset({"-u", "-C"}),
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
# Wrappers followed by one positional argument (timeout's duration).
_WRAPPERS_WITH_POSITIONAL = frozenset({"timeout"})

_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_INTERPRETERS = _POSIX_SHELLS | _POWERSHELLS | frozenset({"cmd", "py", "node", "perl", "ruby"})
_MAX_SHELL_DEPTH = 3

_METADATA_COMMANDS = frozenset({
    "ls", "stat", "test", "[", "[[", "test-path", "get-item",
})
_ECHO_COMMANDS = frozenset({"echo", "printf", "write-host", "write-output"})
_GIT_METADATA_SUBCOMMANDS = frozenset({"status", "check-ignore", "ls-files"})
# git options that take a separate value before the subcommand.
_GIT_OPTIONS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--super-prefix",
})
_GREP_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg"})
_SELECT_STRING_COMMANDS = frozenset({"select-string", "sls"})
# Text filters that may receive an allowlisted listing of protected names
# on stdin: they only see the names, never the files.
_NAME_SAFE_CONSUMERS = frozenset({
    "head", "tail", "wc", "sort", "uniq", "cut", "tr", "cat", "less", "more",
    "column", "awk", "sed", "grep", "egrep", "fgrep", "rg", "findstr",
    "select-object", "format-table", "format-list", "out-string",
    "measure-object", "sort-object",
})
_SEPARATOR_CHARS = frozenset("&|;()")

# Short options that take a value, per tool.
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
# A root-wide recursive search must exclude every one of these names (so
# ".env" alone is not enough, ".env*" is) ...
_EXCLUSION_SAMPLES = {
    ".env": (".env", ".env.local", ".env.production"),
    "apply101.db": ("apply101.db", "apply101.db-wal", "apply101.db-shm", "apply101.db-journal"),
    "uploads/": ("uploads",),
}
# ... unless it only includes source/doc files, which never match them.
_CODE_ONLY_INCLUDE_RE = re.compile(
    r"\*\.(?:py|pyi|md|rst|txt|toml|cfg|ini|json|ya?ml|jsx?|tsx?|html|css|sql|sh|ps1)",
    re.IGNORECASE,
)
_SLS_PARAMS_WITH_VALUE = frozenset({
    "-pattern", "-path", "-literalpath", "-include", "-exclude", "-context",
    "-encoding", "-culture",
})


class GuardError(Exception):
    """The guard cannot decide; the call is blocked."""


def mentioned_kinds(text):
    """Protected path kinds named anywhere in free text."""
    if not isinstance(text, str):
        return set()
    kinds = set()
    if _DB_RE.search(text):
        kinds.add("apply101.db")
    if _ENV_RE.search(text):
        kinds.add(".env")
    if _UPLOADS_RE.search(text):
        kinds.add("uploads/")
    return kinds


def path_kinds(value):
    """Protected path kinds of a literal file system path."""
    if not isinstance(value, str):
        return set()
    parts = [p for p in re.split(r"[\\/]+", value.strip().strip("'\"")) if p]
    kinds = set()
    if any(part.lower() == "uploads" for part in parts):
        kinds.add("uploads/")
    if parts:
        base = parts[-1]
        if re.match(r"apply101\.db", base, re.IGNORECASE):
            kinds.add("apply101.db")
        if re.fullmatch(r"\.env(?:\..*)?", base, re.IGNORECASE):
            kinds.add(".env")
    return kinds


def _token_kinds(token):
    return mentioned_kinds(token) | path_kinds(token)


def _describe(kinds):
    return ", ".join(sorted(kinds))


# -- shell commands ---------------------------------------------------------

def split_segments(command, backslash_paths=False):
    """Splits a shell command into simple commands at && || ; | & ( ) and
    newlines, honoring quotes. Returns (tokens, separator before, separator
    after) triples, a separator being the run of operator tokens between
    two commands (e.g. "|", "&&(" or "("). Backticks and newlines become
    ";". With backslash_paths (PowerShell, cmd) backslashes are path
    separators, not escapes. Raises ValueError for unbalanced quotes."""
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
    return [
        (segment, separators[k], separators[k + 1])
        for k, segment in enumerate(segments)
    ]


def _is_pipe(separator):
    return bool(re.search(r"(?<!\|)\|(?!\|)", separator))


def _command_name(token):
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def command_of(segment):
    """(command name, args) of a simple command, skipping leading variable
    assignments, shell keywords and wrappers such as sudo/env/timeout."""
    i = 0
    while i < len(segment):
        token = segment[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            i += 1
            continue
        name = _command_name(token)
        if name in _WRAPPERS:
            with_value = _WRAPPER_OPTIONS_WITH_VALUE.get(name, frozenset())
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                i += 2 if segment[i] in with_value else 1
            if name in _WRAPPERS_WITH_POSITIONAL:
                i += 1
            continue
        return name, segment[i + 1:]
    return "", []


def _git_subcommand(args):
    """(subcommand, its args) of a git call, skipping global options such
    as -C <path> and -c <name=value>."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _GIT_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg.lower(), args[i + 1:]
    return "", []


def _is_redirect(token):
    return bool(token) and set(token) <= set("<>&0123456789") and ("<" in token or ">" in token)


def _option_value(args, i, eq, val):
    """Value of a long option given as --opt=value or --opt value."""
    if eq:
        return val, i
    if i + 1 < len(args):
        return args[i + 1], i + 1
    return "", i


def _norm_dir(path):
    """Lower-case, forward-slash form of a directory argument: Git Bash
    drive paths (/c/...) become c:/..., a trailing / or /* is dropped."""
    p = path.strip().strip("'\"").replace("\\", "/")
    if p.endswith("/*"):
        p = p[:-2]
    p = p.rstrip("/")
    m = re.match(r"^/([A-Za-z])(?=/|$)", p)
    if m:
        p = m.group(1) + ":" + p[2:]
    return p.lower()


def _reaches_project_root(path):
    """True when a recursive search of ``path`` covers the repository root:
    ".", "*", "..", a filesystem root, or the project directory (from
    CLAUDE_PROJECT_DIR) or one of its ancestors. Environment variables and
    ``~`` are expanded from the hook's own environment."""
    p = _norm_dir(os.path.expanduser(os.path.expandvars(path.strip().strip("'\""))))
    if p in ("", ".", "*") or p == ".." or p.startswith(("../", "./..")):
        return True
    project = _norm_dir(os.environ.get("CLAUDE_PROJECT_DIR", ""))
    return bool(project) and (project == p or project.startswith(p + "/"))


def _unexcluded_kinds(file_globs, dir_globs, text):
    """Protected kinds a root-wide recursive search would still reach."""
    def covered(kind, globs):
        globs = [g.strip("'\"").rstrip("/").lower() for g in globs]
        return all(
            any(fnmatch.fnmatchcase(sample, g) for g in globs)
            for sample in _EXCLUSION_SAMPLES[kind]
        )

    missing = set()
    if not covered(".env", file_globs):
        missing.add(".env")
    if not covered("uploads/", dir_globs):
        missing.add("uploads/")
    if text and not covered("apply101.db", file_globs):
        missing.add("apply101.db")
    return missing


def check_grep(name, args, root_check=True):
    """Reason to block a grep/rg call, or None. Protected names may appear
    in the search pattern and in exclusions but not as searched paths, and
    a recursive grep or unrestricted rg over the repository root must
    exclude them (or include only source/doc files)."""
    is_rg = name == "rg"
    short_with_value = _RG_SHORT_WITH_VALUE if is_rg else _GREP_SHORT_WITH_VALUE
    long_with_value = _RG_LONG_WITH_VALUE if is_rg else _GREP_LONG_WITH_VALUE
    patterns_given = recursive = text = False
    risky, positional = [], []
    excluded_files, excluded_dirs, includes = [], [], []
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
                elif key == "--exclude":
                    excluded_files.append(value)
                elif key == "--exclude-dir":
                    excluded_dirs.append(value)
                elif key in ("--glob", "--iglob") and value.startswith("!"):
                    excluded_files.append(value[1:])
                    excluded_dirs.append(value[1:])
                elif key in ("--glob", "--iglob", "--include"):
                    includes.append(value)
                    risky.append(value)
                elif key == "--directories":
                    recursive = recursive or value.lower() == "recurse"
                elif key == "--binary-files":
                    text = text or value.lower() == "text"
                else:
                    patterns_given = patterns_given or key == "--file"
                    risky.append(value)
            elif key in ("--recursive", "--dereference-recursive") and not is_rg:
                recursive = True
            elif is_rg and (key in ("--hidden", "--unrestricted") or key.startswith("--no-ignore")):
                recursive = True
            elif key == "--text":
                text = True
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
                        if value.startswith("!"):
                            excluded_files.append(value[1:])
                            excluded_dirs.append(value[1:])
                        else:
                            includes.append(value)
                            risky.append(value)
                    elif flag == "d" and not is_rg:
                        recursive = recursive or value.lower() == "recurse"
                    break
                if flag in "rR" and not is_rg:
                    recursive = True
                elif flag in "u." and is_rg:
                    recursive = True
                elif flag == "a":
                    text = True
        else:
            positional.append(arg)
        i += 1
    paths = positional if patterns_given else positional[1:]
    for token in paths + risky:
        kinds = _token_kinds(token)
        if kinds:
            return f"{name} reads a protected path ({_describe(kinds)})"
    code_only = bool(includes) and all(
        _CODE_ONLY_INCLUDE_RE.fullmatch(v.strip("'\"")) for v in includes
    )
    if (
        root_check and recursive and not code_only
        and (not paths or any(_reaches_project_root(p) for p in paths))
    ):
        missing = _unexcluded_kinds(excluded_files, excluded_dirs, text)
        if missing:
            how = "-g '!.env*' -g '!uploads'" if is_rg else "--exclude=.env* --exclude-dir=uploads"
            if "apply101.db" in missing:
                how += " -g '!apply101.db*'" if is_rg else " --exclude=apply101.db*"
            return (
                f"{name} searches the repository root recursively without excluding "
                f"{_describe(missing)}; add {how}, search a subdirectory, or include "
                "only source files"
            )
    return None


def check_select_string(args):
    """Reason to block Select-String, or None when protected names only
    appear in -Pattern or -Exclude."""
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
        elif arg.startswith("-"):
            pass
        else:
            positional.append(arg)
        i += 1
    for token in positional[1:] + risky:
        kinds = _token_kinds(token)
        if kinds:
            return f"Select-String reads a protected path ({_describe(kinds)})"
    return None


def _is_interpreter(name):
    return name in _INTERPRETERS or name.startswith("python")


def _is_text_only(name, args, piped_to):
    """True when the command's arguments are search patterns or messages
    rather than code (grep, echo, git grep/log, ...) and its output does
    not feed an interpreter."""
    if piped_to is not None and _is_interpreter(piped_to):
        return False
    if name in _GREP_COMMANDS | _SELECT_STRING_COMMANDS | _ECHO_COMMANDS:
        return True
    return name == "git" and _git_subcommand(args)[0] in ("grep", "log")


def _nested_command(name, args):
    """(command text, backslash_paths) run by a nested shell such as
    ``bash -c``, ``cmd /c`` or ``powershell -Command``, or None."""
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
            if re.fullmatch(r"-c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?", arg, re.IGNORECASE):
                return " ".join(args[i + 1:]), True
    return None


def _check_deletion(name, args, segment, text_only):
    if name in _DELETE_COMMANDS:
        return f"deletes files ({name})"
    names = [_command_name(token) for token in segment]
    if "xargs" in names:
        after = names[names.index("xargs") + 1:]
        hit = next((n for n in after if n in _DELETE_COMMANDS), None)
        if hit:
            return f"deletes files (xargs {hit})"
    if not text_only and _INLINE_DELETE_RE.search(" ".join(segment)):
        return "deletes files (inline code)"
    if name == "git":
        sub, _ = _git_subcommand(args)
        if sub in ("clean", "rm"):
            return f"deletes files (git {sub})"
    if name == "find":
        lowered = [a.lower() for a in args]
        if "-delete" in lowered:
            return "deletes files (find -delete)"
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in lowered:
                idx = lowered.index(flag)
                if idx + 1 < len(args) and _command_name(args[idx + 1]) in _DELETE_COMMANDS:
                    return f"deletes files (find {flag} {_command_name(args[idx + 1])})"
    return None


def _check_real_db_entry_point(name, args):
    if name == "uvicorn":
        return "starts the app, which opens the real database (uvicorn)"
    if name == "py" or name.startswith("python"):
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "-m" and i + 1 < len(args):
                if _REAL_DB_MODULE_RE.search(args[i + 1]):
                    return f"runs a real-database entry point (-m {args[i + 1]})"
                return None
            if arg == "-c" and i + 1 < len(args):
                if _INLINE_REAL_DB_RE.search(args[i + 1]):
                    return "runs inline code that imports a real-database entry point"
                return None
            if arg in _PYTHON_OPTIONS_WITH_VALUE:
                i += 2
                continue
            if not arg.startswith("-"):
                if _REAL_DB_SCRIPT_RE.search(arg.replace("\\", "/")):
                    return f"runs a real-database entry point ({arg})"
                return None
            i += 1
    return None


def _check_real_db(name, args, segment, text_only):
    reason = _check_real_db_entry_point(name, args)
    if reason:
        return reason
    if not text_only and _REAL_DB_REFERENCE_RE.search(" ".join(segment)):
        return "runs code that imports a real-database entry point"
    return None


def _lists_names(name, args):
    """True for allowlisted commands whose output is protected names, not
    contents: metadata commands, echo, git status/check-ignore/ls-files."""
    if name in _METADATA_COMMANDS or name in _ECHO_COMMANDS:
        return True
    return name == "git" and _git_subcommand(args)[0] in _GIT_METADATA_SUBCOMMANDS


def _pipe_consumer(parts, k):
    """Command reading the pipe after parts[k], or None when no pipe
    follows. xargs counts as the reader: it turns its input into
    arguments."""
    if k + 1 >= len(parts) or not _is_pipe(parts[k][2]):
        return None
    following = parts[k + 1][0]
    if any(_command_name(token) == "xargs" for token in following):
        return "xargs"
    return command_of(following)[0]


def _check_name_flow(parts, k):
    """An allowlisted listing of protected names may feed text filters only,
    along the whole pipeline: not xargs/readers/hashers, nor another command
    through ``$(...)`` or a PowerShell ``(...)`` argument."""
    j = k
    consumer = _pipe_consumer(parts, j)
    while consumer is not None:
        if consumer not in _NAME_SAFE_CONSUMERS:
            return f"pipes protected path names into {consumer or 'another command'}"
        j += 1
        consumer = _pipe_consumer(parts, j)
    if k > 0 and parts[k][1] == "(":
        outer = command_of(parts[k - 1][0])[0]
        if outer and not outer.startswith("-") and outer not in _METADATA_COMMANDS | _ECHO_COMMANDS:
            return f"passes protected path names to {outer}"
    return None


def _check_protected_segment(name, args, kinds, root_check=True):
    for idx, token in enumerate(args):
        if _is_redirect(token) and idx + 1 < len(args) and _token_kinds(args[idx + 1]):
            return f"redirects to or from a protected path ({_describe(_token_kinds(args[idx + 1]))})"
    if name in _METADATA_COMMANDS and "uploads/" not in kinds:
        return None
    if name in _ECHO_COMMANDS:
        return None
    if name == "git":
        sub, rest = _git_subcommand(args)
        if sub in _GIT_METADATA_SUBCOMMANDS:
            return None
        if sub == "grep":
            return check_grep("grep", rest, root_check=False)
    if name in _GREP_COMMANDS:
        return check_grep(name, args, root_check)
    if name in _SELECT_STRING_COMMANDS:
        return check_select_string(args)
    return f"{name or 'command'} references a protected path ({_describe(kinds)})"


def check_shell(command, backslash_paths=False, depth=0):
    if not isinstance(command, str):
        raise GuardError("shell command is not a string")
    if depth > _MAX_SHELL_DEPTH:
        return "nests shells too deeply for the guard to inspect"
    raw_kinds = mentioned_kinds(command)
    try:
        parts = split_segments(command, backslash_paths)
    except ValueError:
        if raw_kinds:
            return f"unparseable command references a protected path ({_describe(raw_kinds)})"
        if _INLINE_DELETE_RE.search(command) or re.search(
            r"(?:^|[\s;&|(])(?:rm|rmdir|del|erase|rd|unlink)\s", command, re.IGNORECASE
        ):
            return "unparseable command may delete files"
        if _INLINE_REAL_DB_RE.search(command):
            return "unparseable command references a real-database entry point"
        return None
    attributed = set()
    for k, (segment, _before, after) in enumerate(parts):
        name, args = command_of(segment)
        piped_to = _pipe_consumer(parts, k)
        nested = _nested_command(name, args)
        if nested is not None:
            reason = check_shell(nested[0], nested[1], depth + 1)
            if reason:
                return f"{reason} (inside {name})"
        text_only = _is_text_only(name, args, piped_to)
        # Under xargs the searched paths arrive on stdin, not as ".".
        prefix = segment[:len(segment) - len(args) - 1]
        from_xargs = any(_command_name(token) == "xargs" for token in prefix)
        reason = (
            _check_deletion(name, args, segment, text_only)
            or _check_real_db(name, args, segment, text_only)
            or (check_grep(name, args, root_check=not from_xargs) if name in _GREP_COMMANDS else None)
        )
        if reason:
            return reason
        seg_kinds = set()
        for token in segment:
            seg_kinds |= _token_kinds(token)
        if not seg_kinds:
            continue
        attributed |= seg_kinds
        reason = _check_protected_segment(name, args, seg_kinds, root_check=not from_xargs)
        if reason is None and _lists_names(name, args):
            reason = _check_name_flow(parts, k)
        if reason:
            return reason
    unattributed = raw_kinds - attributed
    if unattributed:
        return f"command references a protected path in a form the guard cannot attribute ({_describe(unattributed)})"
    return None


# -- entry point --------------------------------------------------------------

def decide(payload):
    """Returns a block reason (str) or None to allow."""
    if not isinstance(payload, dict):
        raise GuardError("hook input is not a JSON object")
    agent_type = payload.get("agent_type")
    if agent_type not in GUARDED_AGENT_TYPES:
        return None
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise GuardError("hook input has no tool_name/tool_input")

    for field in PATH_FIELDS.get(tool_name, ()):
        kinds = path_kinds(tool_input.get(field))
        if kinds:
            return f"{tool_name} targets a protected path ({_describe(kinds)})"
    for field in GLOB_FIELDS.get(tool_name, ()):
        value = tool_input.get(field)
        if isinstance(value, str) and not value.startswith("!"):
            kinds = _token_kinds(value)
            if kinds:
                return f"{tool_name} selects a protected path ({_describe(kinds)})"
    if tool_name in SHELL_TOOLS:
        return check_shell(tool_input.get("command"), backslash_paths=tool_name == "PowerShell")
    if tool_name in AGENT_TOOLS:
        sub = tool_input.get("subagent_type")
        if sub not in GUARDED_AGENT_TYPES:
            return f"starts an agent type this guard does not cover ({sub!r})"
    return None


def main(stdin=None, stderr=None):
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stderr = stderr if stderr is not None else sys.stderr
    try:
        payload = json.loads(stdin.read().decode("utf-8"))
        reason = decide(payload)
    except Exception as exc:  # fail closed
        reason = f"guard could not decide ({type(exc).__name__})"
    if reason is None:
        return 0
    stderr.write(
        "Blocked by Apply101 agent_file_guard (project subagent rule): "
        f"{reason}. Project agents must not access apply101.db*, .env or "
        "uploads/, run real-database entry points, or delete files; report "
        "to the parent session instead.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
