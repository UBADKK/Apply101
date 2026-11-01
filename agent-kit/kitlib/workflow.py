"""The orchestration state machine behind ``kit.py checkpoint``.

Deterministic gates around the LLM agents: risk flags need explicit user
approval before development, changed files must stay inside the approved
scope, fix rounds are capped, and a finding that survives a fix round stops
the loop. Agents never call this; the orchestrator (main session) does.
"""

import hashlib
import re
from pathlib import Path
from urllib.parse import quote, unquote

from . import checkpoint as cpmod
from .common import (
    CORE_MAX_FIX_ROUNDS, INSTALL_DIR, KitError, KitRefused, changed_paths, current_branch,
    head_commit, load_guard, max_fix_rounds, norm_rel, path_matches,
    stat_signature, tracked_paths,
)

# Core risk vocabulary. Adapters add words per flag (risk_keywords).
CORE_RISK_KEYWORDS = {
    "production": [
        "deploy", "deployment", "production", "prod", "release", "publish",
        "rollout", "rollback", "live server", "go live",
    ],
    "remote_git": [
        "git push", "push to", "force push", "force-push", "merge the pr",
        "merge pr", "merge into main", "merge to main", "open a pr", "create a pr",
    ],
    "data": [
        "migration", "migrate", "schema change", "alter table", "drop table",
        "truncate", "delete data", "real data", "production data", "backfill",
        "database", "restore backup",
    ],
    "secrets": [
        "secret", "credential", "credentials", "api key", "password", ".env",
        "private key", "rotate key", "token rotation",
    ],
    "external": [
        "aws", "gcp", "azure", "kubernetes", "k8s", "terraform", "dns",
        "payment", "billing", "send email", "webhook",
    ],
}
RISK_FLAGS = tuple(CORE_RISK_KEYWORDS) + ("approval_path", "auditor")

VERDICTS = {
    "kit-researcher": ("answered", "partial", "blocked"),
    "kit-auditor": ("proceed", "needs_user_approval", "stop"),
    "kit-developer": ("implemented", "partial", "blocked"),
    "kit-reviewer": ("approve", "changes_requested"),
}
# Wait reasons the user can lift with `approve`; limits and no-progress are final.
APPROVABLE_PREFIXES = ("approval required", "out of scope", "developer blocked")
FINDING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
MAX_AUDIT_RUNS = 2  # the first audit plus one after the user changes the scope


# -- agent result blocks -----------------------------------------------------------

def parse_result(report_text):
    """The ```kit-result block every agent report ends with, as a dict with
    role, verdict, findings (list) and risk (list)."""
    lines = report_text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "```kit-result"]
    if len(starts) != 1:
        raise KitError(f"report must contain exactly one ```kit-result block, found {len(starts)}")
    fields = {}
    for line in lines[starts[0] + 1:]:
        if line.strip() == "```":
            break
        m = re.match(r"^\s*([a-z_]+)\s*:\s*(.*?)\s*$", line)
        if m:
            fields[m.group(1)] = m.group(2)
    else:
        raise KitError("the kit-result block is not closed")
    role = fields.get("role", "")
    role = role if role.startswith("kit-") else f"kit-{role}"
    if role not in VERDICTS:
        raise KitError(f"kit-result role must be one of {', '.join(VERDICTS)}")
    verdict = fields.get("verdict", "")
    if verdict not in VERDICTS[role]:
        raise KitError(f"{role} verdict must be one of {', '.join(VERDICTS[role])}, got {verdict!r}")

    def as_list(value):
        items = [x.strip() for x in re.split(r"[,\s]+", value or "") if x.strip()]
        return [] if items in ([], ["none"], ["-"]) else items

    return {
        "role": role, "verdict": verdict,
        "findings": as_list(fields.get("findings")),
        "risk": as_list(fields.get("risk")),
    }


# -- risk scan ----------------------------------------------------------------------

def risk_keywords(config):
    merged = {flag: list(words) for flag, words in CORE_RISK_KEYWORDS.items()}
    for flag, words in (config.get("risk_keywords") or {}).items():
        merged.setdefault(flag, []).extend(words)
    return merged


def scan_risk(text, config):
    """Risk flags whose keywords appear in the task text (whole words)."""
    lowered = " " + re.sub(r"\s+", " ", text.lower()) + " "
    flags = []
    for flag, words in risk_keywords(config).items():
        for word in words:
            w = word.lower().strip()
            if w and re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", lowered):
                flags.append(flag)
                break
    return flags


def scope_needs_approval(project_root, scope, config):
    """Tracked or named files in the approved scope that the adapter marks
    as approval-required."""
    approval = config.get("approval_required_paths") or []
    if not approval:
        return []
    candidates = set(tracked_paths(project_root))
    candidates |= {norm_rel(s) for s in scope if not any(ch in s for ch in "*?[")}
    return sorted(p for p in candidates if path_matches(p, scope) and path_matches(p, approval))


# -- helpers ------------------------------------------------------------------------

def _flags(cp):
    value = cp["state"]["Risk flags"]
    return [] if value in ("", cpmod.NONE) else [f.strip() for f in value.split(",") if f.strip()]


def _set_flags(cp, flags):
    unique = []
    for flag in flags:
        if flag not in unique:
            unique.append(flag)
    cp["state"]["Risk flags"] = ", ".join(unique) if unique else cpmod.NONE


def _approval_coverage(cp):
    """(risk flags, scope entries) the user's recorded approvals cover. An
    approval covers the flags and the approved scope of the moment it was
    given; anything added later needs a new decision."""
    flags, scopes = set(), []
    for entry in cp["Log"]:
        m = re.match(r"^approve: covers=(\S*) scope=(.*)$", entry)
        if m:
            flags |= {f for f in m.group(1).split(",") if f and f != "none"}
            scopes.extend(x for x in m.group(2).split(";") if x and x != "none")
    return flags, scopes


def _fingerprint(project_root, config, cp):
    """Short hash of the task's changed files and their stat signatures."""
    dirty = {path for kind, path, _ in _parse_baseline(cp) if kind == "dirty"}
    rows = [f"{p} {stat_signature(project_root, p)}" for p in task_changes(project_root, config)
            if p not in dirty]
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()[:12]


def _last_developed_fingerprint(cp):
    for entry in reversed(cp["Log"]):
        m = re.match(r"^developed: \S+ files=\d+ fp=([0-9a-f]+)$", entry)
        if m:
            return m.group(1)
    return None


def repository_drift(project_root, cp):
    """Reasons the repository no longer matches the checkpoint (another
    branch, or HEAD moved since the last recorded commit)."""
    st = cp["state"]
    problems = []
    branch = current_branch(project_root)
    if branch != st["Branch"]:
        problems.append(f"current branch is {branch or '(detached)'}, checkpoint is on {st['Branch']}")
    head = head_commit(project_root)
    if head != st["Head commit"]:
        problems.append(f"HEAD moved from {st['Head commit']} to {head} since the checkpoint")
    return problems


def accept_head(project_root, cp):
    """Records a commit the user made or asked for on the task's branch."""
    st = cp["state"]
    if current_branch(project_root) != st["Branch"]:
        raise KitRefused("accept-head only works on the checkpoint's branch")
    old, new = st["Head commit"], head_commit(project_root)
    st["Head commit"] = new
    _log(cp, f"accept-head: {old}..{new}")


# Entries the gates count on (approval coverage, the audit cap) are kept when
# the log is trimmed; the oldest other entries go first.
_KEPT_LOG_PREFIXES = ("approve:", "audit:", "accept-head:")


def _log(cp, entry):
    log = cp["Log"]
    log.append(entry)
    while len(log) > cpmod.MAX_LOG_ENTRIES:
        idx = next((i for i, e in enumerate(log[:-1]) if not e.startswith(_KEPT_LOG_PREFIXES)), None)
        if idx is None:
            raise KitError("the checkpoint log is full of approvals and audits; stop and start a new task")
        del log[idx]


def _wait(cp, reason, next_step):
    cp["state"]["Status"] = "waiting_user"
    cp["state"]["Reason"] = reason
    cp["Next step"] = next_step
    _log(cp, f"waiting_user: {reason}")


def _last_review_findings(cp):
    for entry in reversed(cp["Log"]):
        m = re.match(r"^review: changes_requested (.+)$", entry)
        if m:
            return {x.strip() for x in m.group(1).split(",") if x.strip()}
    return set()


def _require(cp, statuses=("active",), phases=None):
    st = cp["state"]
    if st["Status"] not in statuses:
        raise KitError(f"task status is {st['Status']} ({st['Reason']}); this event is not allowed now")
    if phases and st["Phase"] not in phases:
        raise KitError(f"event not allowed in phase {st['Phase']} (allowed: {', '.join(phases)})")


def _checkpoint_dir(config):
    guard = load_guard()
    return guard.normalize_relative_dir(config.get("checkpoint_dir", guard.DEFAULT_CHECKPOINT_DIR))


def task_changes(project_root, config):
    """Uncommitted paths, without the checkpoint directory itself."""
    prefix = _checkpoint_dir(config) + "/"
    return [p for p in changed_paths(project_root) if not p.startswith(prefix)]


def baseline_entries(project_root, config):
    """Stat signatures (never contents) of pre-existing changes and of the
    adapter's watch_files."""
    entries = []
    for rel in config.get("watch_files") or []:
        entries.append(f"watch {_quote(norm_rel(rel))} {stat_signature(project_root, rel)}")
    for rel in task_changes(project_root, config):
        entries.append(f"dirty {_quote(rel)} {stat_signature(project_root, rel)}")
    return entries


def _quote(path):
    return quote(path, safe="/._-~+@,")


def _parse_baseline(cp):
    parsed = []
    for entry in cp["Baseline"]:
        m = re.match(r"^(watch|dirty) (\S+) (absent|size=\d+ mtime_ns=\d+)$", entry)
        if not m:
            raise KitError(f"unreadable Baseline entry: {entry[:80]!r}")
        parsed.append((m.group(1), unquote(m.group(2)), m.group(3)))
    return parsed


def baseline_problems(project_root, cp):
    """Watched real-data files or pre-existing user changes whose size or
    modification time changed (also when a change was reverted or a file
    removed)."""
    problems = []
    for kind, path, sig in _parse_baseline(cp):
        if stat_signature(project_root, path) != sig:
            what = "watched real-data file changed" if kind == "watch" else \
                "pre-existing user change was modified"
            problems.append(f"{what}: {path}")
    return problems


def scope_check(project_root, cp, config):
    """(changed files, problems). Problems: files outside the approved
    scope, protected paths, approval-required files the user's approvals do
    not cover, touched pre-existing changes and changed watch files."""
    guard = load_guard()
    settings = guard.GuardSettings(config, str(project_root))
    dirty = {path for kind, path, _ in _parse_baseline(cp) if kind == "dirty"}
    scope = cp["Approved scope"]
    approval = config.get("approval_required_paths") or []
    _flags_ok, approved_scope = _approval_coverage(cp)
    changed, problems = [], baseline_problems(project_root, cp)
    for path in task_changes(project_root, config):
        if path in dirty:
            continue  # the user's pre-existing change; checked above
        changed.append(path)
        if guard.path_hits(path, settings):
            problems.append(f"protected path changed: {path}")
        elif not path_matches(path, scope):
            problems.append(f"out of scope: {path}")
        elif path_matches(path, approval) and not path_matches(path, approved_scope):
            problems.append(f"approval required for: {path}")
    return changed, problems


_UNSAFE_PREFIXES = ("watched real-data file changed", "pre-existing user change", "protected path changed")


def _wait_for_changes(cp, problems, next_step):
    """Waits for the user. Unsafe changes (real data, the user's own changes,
    protected files) end the task; scope and approval problems can be
    resolved by the user's decision."""
    detail = "; ".join(problems[:5])
    if any(p.startswith(_UNSAFE_PREFIXES) for p in problems):
        _wait(cp, f"unsafe changes: {detail}",
              "Stop. Report these files to the user; do not revert anything. The task cannot "
              "continue; after the user has checked the data, start a new task.")
    else:
        _wait(cp, f"out of scope or unapproved changes: {detail}", next_step)


def _dirty_in_scope(cp):
    return [path for kind, path, _ in _parse_baseline(cp)
            if kind == "dirty" and path_matches(path, cp["Approved scope"])]


# -- events -------------------------------------------------------------------------

def new_checkpoint(project_root, config, task, goal, scope, criteria, allow_dirty=False):
    if not cpmod.TASK_RE.match(task) or task.endswith(".md"):
        raise KitError("task must be a short slug: a-z, 0-9, '.', '_' or '-', at most 64 characters, "
                       "not ending in .md")
    if not goal.strip():
        raise KitError("goal is empty")
    dirty = task_changes(project_root, config)
    if dirty and not allow_dirty:
        raise KitError(
            "the working tree has uncommitted changes; commit or set them aside yourself, "
            "or pass --allow-dirty to record them as a protected baseline"
        )
    state = cpmod.new_state(task, current_branch(project_root), head_commit(project_root),
                            max_fix_rounds(config))
    cp = cpmod.empty_checkpoint(task, goal, state)
    cp["Approved scope"] = [norm_rel(s) for s in scope]
    cp["Acceptance criteria"] = list(criteria)
    cp["Baseline"] = baseline_entries(project_root, config)
    in_scope = _dirty_in_scope(cp)
    if in_scope:
        raise KitError("files with pre-existing uncommitted changes cannot be in the approved scope "
                       f"({', '.join(in_scope[:5])}); commit or set them aside first")
    flags = scan_risk(" ".join([goal] + list(criteria) + list(scope)), config)
    _set_flags(cp, flags)
    _log(cp, f"new: risk={','.join(flags) or 'none'}")
    if "production" in flags:
        _wait(cp, "approval required: the request mentions production/deployment, which agents never do",
              "Ask the user: agents will not deploy or touch live systems. Narrow the task to a "
              "code change (then `approve`) or let the user do the production step.")
    else:
        cp["Next step"] = ("Run kit-auditor (risk flags present)." if flags
                           else "Run kit-researcher if facts are missing, else kit-auditor or kit-developer.")
    return cp


def apply_event(project_root, config, cp, event, verdict=None, findings=(), risk=(), note=""):
    """Applies one orchestration event; returns a short message. Raises
    KitError when the event is not allowed."""
    st = cp["state"]
    used, limit = cpmod.fix_rounds(cp)
    limit = min(limit, CORE_MAX_FIX_ROUNDS)

    if event == "research":
        _require(cp, phases=("intake",))
        st["Phase"] = "research"
        _log(cp, "research: started")
        cp["Next step"] = "Run kit-researcher with the open questions; then kit-auditor or develop."
        return "phase research"

    if event == "audit":
        _require(cp, phases=("intake", "research", "audit"))
        if sum(entry.startswith("audit:") for entry in cp["Log"]) >= MAX_AUDIT_RUNS:
            raise KitError(f"at most {MAX_AUDIT_RUNS} audit runs per task; start a new task for a new scope")
        if verdict not in VERDICTS["kit-auditor"]:
            raise KitError(f"audit needs --verdict {'|'.join(VERDICTS['kit-auditor'])}")
        st["Phase"] = "audit"
        _set_flags(cp, _flags(cp) + list(risk) + (["auditor"] if verdict == "needs_user_approval" else []))
        _log(cp, f"audit: {verdict}" + (f" risk={','.join(risk)}" if risk else ""))
        if verdict == "stop":
            st["Status"], st["Reason"] = "stopped", f"auditor stopped the task: {note or 'see audit report'}"
            cp["Next step"] = "Report the auditor's reason to the user; do not continue."
            return "stopped by auditor"
        if verdict == "needs_user_approval":
            _wait(cp, "approval required: the auditor needs an explicit user decision",
                  "Show the user the auditor's risks and ask for an explicit decision.")
            return "waiting for user approval"
        cp["Next step"] = "Run kit-developer with the approved scope and acceptance criteria."
        return "audit passed"

    if event == "approve":
        if st["Status"] != "waiting_user" or not st["Reason"].startswith(APPROVABLE_PREFIXES):
            raise KitError(f"nothing the user can approve now (status {st['Status']}: {st['Reason']})")
        if not note.strip():
            raise KitError("approve needs --note with the user's explicit decision")
        covers, scope = cpmod.NONE, cpmod.NONE
        if st["Reason"].startswith("approval required"):
            covers = ",".join(_flags(cp)) or "none"
            scope = ";".join(cp["Approved scope"]) or "none"
        elif st["Reason"].startswith("out of scope"):
            # The user decided about the listed approval-required files.
            scope = ";".join(m.strip() for m in re.findall(r"approval required for: ([^;]+)", st["Reason"]))
            scope = scope or cpmod.NONE
        if len(scope) > 300:
            raise KitError("the approved scope is too long to record in one approval; use globs")
        st["Status"], st["Reason"] = "active", cpmod.NONE
        st["Approval"] = note.strip()[:200]
        _log(cp, f"approve: covers={covers} scope={scope}")
        cp["Next step"] = (
            "Apply the user's decision (extend the scope with record --scope --user-approved-change "
            "if they accepted extra files), re-run kit-developer if work remains, then record "
            "`developed` again." if st["Phase"] in ("develop", "fix")
            else "Run kit-developer with the approved scope and acceptance criteria.")
        return "approved"

    if event == "develop":
        _require(cp, phases=("intake", "research", "audit"))
        if not cp["Approved scope"] or not cp["Acceptance criteria"]:
            raise KitError("record the approved scope and acceptance criteria first (checkpoint record)")
        in_scope = _dirty_in_scope(cp)
        if in_scope:
            raise KitError("files with pre-existing uncommitted changes cannot be in the approved scope "
                           f"({', '.join(in_scope[:5])}); commit or set them aside first")
        # Scope and criteria may have been recorded after `new`: scan them again.
        rescanned = scan_risk(" ".join([cp["Goal"]] + cp["Acceptance criteria"] + cp["Approved scope"]), config)
        _set_flags(cp, _flags(cp) + rescanned)
        approval_files = scope_needs_approval(project_root, cp["Approved scope"], config)
        if approval_files:
            _set_flags(cp, _flags(cp) + ["approval_path"])
        covered_flags, covered_scope = _approval_coverage(cp)
        missing_flags = [f for f in _flags(cp) if f not in covered_flags]
        missing_files = [f for f in approval_files if not path_matches(f, covered_scope)]
        if missing_flags or missing_files:
            detail = ", ".join(missing_flags) + (f" ({', '.join(missing_files[:5])})" if missing_files else "")
            _wait(cp, f"approval required before development: {detail}",
                  "Ask the user for an explicit decision on these risks; record it with `approve`.")
            return "waiting for user approval"
        st["Phase"] = "develop"
        _log(cp, "develop: started")
        cp["Next step"] = "Run kit-developer; when it reports, record `developed`."
        return "phase develop"

    if event == "developed":
        _require(cp, phases=("develop", "fix"))
        if verdict not in VERDICTS["kit-developer"]:
            raise KitError(f"developed needs --verdict {'|'.join(VERDICTS['kit-developer'])}")
        changed, problems = scope_check(project_root, cp, config)
        cp["Changed files"] = changed[:60]
        _log(cp, f"developed: {verdict} files={len(changed)} fp={_fingerprint(project_root, config, cp)}")
        if verdict == "blocked":
            _wait(cp, f"developer blocked: {note or 'see developer report'}",
                  "Report the blocker to the user and ask how to proceed.")
            return "waiting for user"
        if problems:
            _wait_for_changes(cp, problems,
                              "Show the user these files. Do not revert anything yourself. If they accept "
                              "them, extend the scope (record --scope --user-approved-change) or approve.")
            return "waiting for user"
        if not changed:
            _wait(cp, "developer blocked: no changes found in the working tree",
                  "Check the developer report; ask the user before retrying.")
            return "waiting for user"
        st["Phase"] = "review"
        cp["Next step"] = "Run kit-reviewer on the uncommitted diff against the acceptance criteria."
        return "phase review"

    if event == "review":
        _require(cp, phases=("review",))
        if verdict not in VERDICTS["kit-reviewer"]:
            raise KitError(f"review needs --verdict {'|'.join(VERDICTS['kit-reviewer'])}")
        bad = [f for f in findings if not FINDING_RE.match(f)]
        if bad:
            raise KitError(f"finding ids must be short tokens such as R1: {bad}")
        if verdict == "approve" and findings:
            raise KitError("an approval cannot carry blocking findings; use changes_requested")
        # The reviewed tree must be the one the developer run left behind.
        _changed, problems = scope_check(project_root, cp, config)
        if _fingerprint(project_root, config, cp) != _last_developed_fingerprint(cp):
            problems.insert(0, "the working tree changed after the last `developed` record")
        if problems:
            st["Phase"] = "fix" if used else "develop"  # the tree needs a new `developed` record
            _wait_for_changes(cp, problems,
                              "Show the user what changed since the developer run. Do not revert anything "
                              "yourself; after their decision record `developed` again.")
            return "waiting for user"
        if verdict == "approve":
            if findings:
                raise KitError("an approval cannot carry blocking findings; use changes_requested")
            st["Phase"], st["Status"], st["Reason"] = "done", "done", cpmod.NONE
            _log(cp, "review: approve")
            cp["Next step"] = ""
            return "done"
        if not findings:
            raise KitError("changes_requested needs the blocking finding ids (--finding R1 ...)")
        repeated = sorted(set(findings) & _last_review_findings(cp))
        _log(cp, f"review: changes_requested {', '.join(findings)}")
        cp["Open issues"] = [f"{f}: blocking (see review report)" for f in findings]
        if repeated:
            st["Phase"] = "review"
            _wait(cp, f"no progress: finding(s) {', '.join(repeated)} survived a fix round",
                  "Stop the loop. Report the unresolved findings to the user.")
            return "stopped: no progress"
        if used >= limit:
            _wait(cp, f"fix-round limit reached ({used}/{limit})",
                  "Stop the loop. Report the open findings to the user.")
            return "stopped: limit reached"
        st["Fix rounds"] = f"{used + 1}/{limit}"
        st["Phase"] = "fix"
        cp["Next step"] = (f"Fix round {used + 1}/{limit}: run kit-developer with exactly the "
                           f"blocking findings {', '.join(findings)}; then record `developed`.")
        return f"fix round {used + 1}/{limit}"

    if event == "stop":
        st["Status"], st["Reason"] = "stopped", (note.strip() or "stopped by the orchestrator")[:200]
        _log(cp, "stop")
        cp["Next step"] = "Nothing; the task is stopped."
        return "stopped"

    raise KitError(f"unknown event {event!r}")


def touch(project_root, cp):
    cp["state"]["Updated"] = cpmod.now_utc()


ALLOWED_EVENTS = {
    "intake": ("research", "audit", "develop", "stop"),
    "research": ("audit", "develop", "stop"),
    "audit": ("audit", "develop", "stop"),
    "develop": ("developed", "stop"),
    "fix": ("developed", "stop"),
    "review": ("review", "stop"),
    "done": (),
}


def resume_report(project_root, config, cp):
    """(ok, lines): whether the task can continue from this checkpoint
    without asking anyone, and what to tell the orchestrator."""
    st = cp["state"]
    lines = [
        f"Task: {st['Task']}  status={st['Status']}  phase={st['Phase']}  fix rounds={st['Fix rounds']}",
        f"Goal: {cp['Goal'].splitlines()[0][:200]}",
    ]
    if st["Status"] == "done":
        lines.append("Task is complete; nothing to resume.")
        lines.append("RESUME OK")
        return True, lines
    ok = True
    for problem in repository_drift(project_root, cp):
        ok = False
        lines.append(f"STOP: {problem}. Do not switch branches or reset; inspect `git log` and ask the user.")
    now_changed = task_changes(project_root, config)
    for problem in baseline_problems(project_root, cp):
        ok = False
        lines.append(f"STOP: {problem}")
    if st["Status"] in ("waiting_user", "stopped"):
        ok = False
        lines.append(f"STOP: task is {st['Status']}: {st['Reason']}")
    recorded = set(cp["Changed files"])
    dirty = {p for kind, p, _ in _parse_baseline(cp) if kind == "dirty"}
    unrecorded = [p for p in now_changed if p not in recorded and p not in dirty]
    if unrecorded and st["Phase"] in ("develop", "fix"):
        lines.append("NOTE: an interrupted developer run left changes not yet recorded: "
                     + ", ".join(unrecorded[:10])
                     + ". Keep them; give them to kit-developer as partial work, then record `developed`.")
    elif unrecorded:
        ok = False
        lines.append("STOP: changes not recorded in the checkpoint: " + ", ".join(unrecorded[:10]))
    if cp["Open issues"]:
        lines.append("Open issues: " + "; ".join(cp["Open issues"][:5]))
    lines.append(f"Next step: {cp['Next step'] or '-'}")
    lines.append(f"Allowed events: {', '.join(ALLOWED_EVENTS.get(st['Phase'], ())) or 'none'}")
    lines.append("RESUME OK" if ok else "RESUME BLOCKED")
    return ok, lines


def checkpoint_path(project_root, config, ref, slug_only=False):
    """A checkpoint path from a task slug or a path."""
    if not slug_only and (ref.endswith(".md") or "/" in ref or "\\" in ref):
        path = Path(ref)
        return path if path.is_absolute() else Path(project_root) / path
    return Path(project_root) / _checkpoint_dir(config) / f"{ref}.md"


__all__ = [
    "INSTALL_DIR", "apply_event", "checkpoint_path", "new_checkpoint", "parse_result",
    "resume_report", "scan_risk", "scope_check", "touch",
]
