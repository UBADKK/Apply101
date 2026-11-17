"""Checkpoint file format: a small Markdown file that a new session can
resume from. It holds state and short notes only: never secrets, personal
data, file contents or logs."""

import re
from datetime import datetime, timezone

MARKER = "<!-- kit-checkpoint v1: keep it small; no secrets, personal data, file contents or logs. -->"
STATE_FIELDS = (
    "Task", "Status", "Phase", "Reason", "Branch", "Base commit",
    "Head commit", "Fix rounds", "Risk flags", "Approval", "Updated",
)
SECTIONS = (
    "State", "Goal", "Approved scope", "Acceptance criteria", "Done",
    "Changed files", "Tests run", "Open issues", "Next step", "Baseline", "Log",
)
LIST_SECTIONS = (
    "Approved scope", "Acceptance criteria", "Done", "Changed files",
    "Tests run", "Open issues", "Baseline", "Log",
)
TEXT_SECTIONS = ("Goal", "Next step")
STATUSES = ("active", "waiting_user", "done", "stopped")
PHASES = ("intake", "research", "audit", "develop", "review", "fix", "done")
NONE = "-"

MAX_BYTES = 12_000
MAX_LINES = 220
MAX_LINE_CHARS = 400
MAX_LOG_ENTRIES = 40
TASK_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Content that must never be in a checkpoint.
SENSITIVE_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API token", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abpr]-[A-Za-z0-9-]{10,}|\bglpat-[A-Za-z0-9_-]{16,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("credential assignment", re.compile(
        r"(?i)\b[\w-]*(?:password|passwd|secret|token|api[_-]?key)[\w-]*\s*[:=]\s*['\"]?(?!<|\*|-\s|none\b|redacted\b)[^\s'\"]{6,}")),
    ("URL with credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("e-mail address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("log or traceback", re.compile(r"^\s*(?:Traceback \(most recent call last\)|File \"[^\"]+\", line \d+)", re.M)),
)


class CheckpointError(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_state(task, branch, commit, max_fix_rounds):
    return {
        "Task": task, "Status": "active", "Phase": "intake", "Reason": NONE,
        "Branch": branch or "(detached)", "Base commit": commit,
        "Head commit": commit, "Fix rounds": f"0/{max_fix_rounds}",
        "Risk flags": NONE, "Approval": NONE, "Updated": now_utc(),
    }


def empty_checkpoint(task, goal, state):
    cp = {"state": dict(state), "title": task}
    for section in LIST_SECTIONS:
        cp[section] = []
    cp["Goal"] = goal.strip()
    cp["Next step"] = ""
    return cp


def render(cp):
    lines = [f"# Checkpoint: {cp['title']}", "", MARKER, "", "## State"]
    lines += [f"- {key}: {cp['state'].get(key, NONE) or NONE}" for key in STATE_FIELDS]
    for section in SECTIONS[1:]:
        lines += ["", f"## {section}"]
        if section in TEXT_SECTIONS:
            lines.append(cp.get(section, "").strip() or NONE)
        else:
            items = cp.get(section) or []
            lines += [f"- {item}" for item in items] if items else [NONE]
    return "\n".join(lines) + "\n"


def parse(text):
    """Checkpoint dict from Markdown. Raises CheckpointError on structure
    problems; content rules are checked by verify()."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# Checkpoint: "):
        raise CheckpointError("first line must be '# Checkpoint: <task>'")
    cp = {"title": lines[0][len("# Checkpoint: "):].strip(), "state": {}}
    if MARKER.split(":")[0] not in text:
        raise CheckpointError("the kit-checkpoint v1 marker comment is missing")
    current, bodies, order = None, {}, []
    for line in lines[1:]:
        m = re.match(r"^## (.+?)\s*$", line)
        if m:
            current = m.group(1)
            if current in bodies:
                raise CheckpointError(f"section '{current}' appears twice")
            bodies[current] = []
            order.append(current)
        elif current is not None:
            bodies[current].append(line)
    missing = [s for s in SECTIONS if s not in bodies]
    if missing:
        raise CheckpointError(f"missing sections: {', '.join(missing)}")
    unknown = [s for s in order if s not in SECTIONS]
    if unknown:
        raise CheckpointError(f"unknown sections: {', '.join(unknown)}")
    if order != list(SECTIONS):
        raise CheckpointError("sections are not in the template order")
    for line in bodies["State"]:
        m = re.match(r"^- ([^:]+):\s*(.*)$", line)
        if m:
            cp["state"][m.group(1).strip()] = m.group(2).strip()
        elif line.strip():
            raise CheckpointError(f"unexpected line in State: {line.strip()[:60]!r}")
    missing_fields = [f for f in STATE_FIELDS if f not in cp["state"]]
    if missing_fields:
        raise CheckpointError(f"State is missing: {', '.join(missing_fields)}")
    for section in TEXT_SECTIONS:
        body = "\n".join(bodies[section]).strip()
        cp[section] = "" if body == NONE else body
    for section in LIST_SECTIONS:
        items = []
        for line in bodies[section]:
            stripped = line.strip()
            if not stripped or stripped == NONE:
                continue
            if not stripped.startswith("- "):
                raise CheckpointError(f"'{section}' must be a '- ' list (or '{NONE}')")
            items.append(stripped[2:].strip())
        cp[section] = items
    return cp


def fix_rounds(cp):
    m = re.fullmatch(r"(\d+)/(\d+)", cp["state"].get("Fix rounds", ""))
    if not m:
        raise CheckpointError("Fix rounds must look like 'used/max', e.g. '0/2'")
    return int(m.group(1)), int(m.group(2))


def verify(text, core_max_fix_rounds=3):
    """List of problems (empty when the checkpoint is valid)."""
    problems = []
    if len(text.encode("utf-8")) > MAX_BYTES:
        problems.append(f"larger than {MAX_BYTES} bytes; keep checkpoints short")
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        problems.append(f"more than {MAX_LINES} lines")
    long_lines = [i + 1 for i, line in enumerate(lines) if len(line) > MAX_LINE_CHARS]
    if long_lines:
        problems.append(f"lines longer than {MAX_LINE_CHARS} characters (no logs): {long_lines[:5]}")
    for label, regex in SENSITIVE_PATTERNS:
        if regex.search(text):
            problems.append(f"contains what looks like a {label}; remove it")
    try:
        cp = parse(text)
    except CheckpointError as exc:
        return problems + [str(exc)]
    st = cp["state"]
    if not TASK_RE.match(cp["title"]) or st["Task"] != cp["title"]:
        problems.append("Task must be a short slug (a-z, 0-9, . _ -) equal to the title")
    if st["Status"] not in STATUSES:
        problems.append(f"Status must be one of {', '.join(STATUSES)}")
    if st["Phase"] not in PHASES:
        problems.append(f"Phase must be one of {', '.join(PHASES)}")
    try:
        used, limit = fix_rounds(cp)
        if limit > core_max_fix_rounds:
            problems.append(f"Fix rounds limit {limit} exceeds the core maximum {core_max_fix_rounds}")
        if used > limit:
            problems.append("Fix rounds used exceeds the limit")
    except CheckpointError as exc:
        problems.append(str(exc))
    if not cp["Goal"]:
        problems.append("Goal is empty")
    if st["Phase"] in ("develop", "review", "fix", "done"):
        if not cp["Approved scope"]:
            problems.append("Approved scope is empty after development started")
        if not cp["Acceptance criteria"]:
            problems.append("Acceptance criteria are empty after development started")
    if st["Status"] != "done" and not cp["Next step"]:
        problems.append("Next step is empty")
    if st["Status"] in ("waiting_user", "stopped") and st["Reason"] in ("", NONE):
        problems.append("Reason is required when the task waits for the user or is stopped")
    if len(cp["Log"]) > MAX_LOG_ENTRIES:
        problems.append(f"Log has more than {MAX_LOG_ENTRIES} entries")
    return problems
