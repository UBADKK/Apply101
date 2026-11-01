"""Installer: copies the kit into a project's .claude/ directory.

It only writes kit-owned files, creates PROJECT_CONTEXT.md once (never
overwrites it), and merges one hook entry and the deny rules into
.claude/settings.json without removing anything. A kit file that differs
from the kit (a local edit or an older version) is a conflict unless
--force is given. Re-running it is safe and also adds deny rules for
protected paths added to PROJECT_CONTEXT.md later.
"""

import hashlib
import json
from pathlib import Path

from .common import (
    CONTEXT_FILE, GUARD_COMMAND, HOOK_MATCHER_TOOLS, INSTALL_DIR, INSTALL_MAP,
    MANIFEST, SETTINGS_FILE, KitError, KitRefused, Layout, load_guard, read_text,
)
from .validate import deny_rules, kit_hook_entries


def _write(path, data, dry_run):
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _config_for(text):
    guard = load_guard()
    try:
        return guard.parse_config(text)
    except guard.ConfigError as exc:
        raise KitError(f"{CONTEXT_FILE}: {exc}")


def merged_settings(existing, config):
    """settings.json content with the kit hook and deny rules added."""
    settings = json.loads(json.dumps(existing))  # deep copy
    hook = {
        "type": "command",
        "shell": "bash",
        "command": GUARD_COMMAND,
        "timeout": 15,
        "onFailure": "block",
        "statusMessage": "Agent kit guard",
    }
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise KitError(f"{SETTINGS_FILE}: 'hooks' is not an object; fix it by hand")
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise KitError(f"{SETTINGS_FILE}: 'hooks.PreToolUse' is not a list; fix it by hand")
    entries = kit_hook_entries(settings)
    if len(entries) > 1:
        raise KitError(f"{SETTINGS_FILE}: more than one kit guard entry; remove the extras by hand")
    if entries:
        entry, existing_hook = entries[0]
        entry["matcher"] = "|".join(HOOK_MATCHER_TOOLS)
        existing_hook.clear()
        existing_hook.update(hook)
    else:
        pre.append({"matcher": "|".join(HOOK_MATCHER_TOOLS), "hooks": [hook]})
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise KitError(f"{SETTINGS_FILE}: 'permissions' is not an object; fix it by hand")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise KitError(f"{SETTINGS_FILE}: 'permissions.deny' is not a list; fix it by hand")
    for rule in deny_rules(config):
        if rule not in deny:
            deny.append(rule)
    return settings


def install(target, context=None, force=False, dry_run=False, layout=None):
    """Installs the kit into ``target``; returns a list of action lines."""
    layout = layout or Layout()
    if not layout.is_source:
        raise KitError("run install from the kit source tree (the directory with core/)")
    target = Path(target).resolve()
    if not target.is_dir():
        raise KitError(f"target {target} is not a directory")
    if not (target / ".git").exists():
        raise KitError("target is not the root of a git repository; the kit needs git for scope checks")
    kit_root = layout.kit_root
    actions, conflicts, writes = [], [], []

    manifest_path = target / MANIFEST
    for src, dst in INSTALL_MAP:
        data = (kit_root / src).read_bytes()
        path = target / dst
        if path.exists():
            if path.read_bytes() == data:
                actions.append(f"unchanged  {dst}")
                continue
            if not force:
                conflicts.append(dst)
                continue
            actions.append(f"replace    {dst}")
        else:
            actions.append(f"create     {dst}")
        writes.append((path, data))
    if conflicts:
        raise KitRefused(
            "these files exist and differ from the kit (local edits or another version): "
            + ", ".join(conflicts)
            + ". Nothing was written. Review them, then re-run with --force to replace them."
        )

    context_path = target / INSTALL_DIR / CONTEXT_FILE
    if context_path.exists():
        context_text = read_text(context_path)
        actions.append(f"keep       {INSTALL_DIR}/{CONTEXT_FILE} (project-owned, never overwritten)")
    else:
        source = Path(context) if context else kit_root / "templates" / CONTEXT_FILE
        context_text = read_text(source)
        writes.append((context_path, context_text.encode("utf-8")))
        actions.append(f"create     {INSTALL_DIR}/{CONTEXT_FILE} (from {source.name if not context else source})")
    config = _config_for(context_text)

    settings_path = target / SETTINGS_FILE
    existing = {}
    if settings_path.exists():
        try:
            existing = json.loads(read_text(settings_path))
        except json.JSONDecodeError as exc:
            raise KitError(f"{SETTINGS_FILE} is not valid JSON ({exc.msg}); fix it first. Nothing was written.")
        if not isinstance(existing, dict):
            raise KitError(f"{SETTINGS_FILE} is not a JSON object. Nothing was written.")
    merged = merged_settings(existing, config)
    if merged != existing:
        writes.append((settings_path, (json.dumps(merged, indent=2) + "\n").encode("utf-8")))
        actions.append(f"merge      {SETTINGS_FILE} (kit hook and deny rules added; nothing removed)")
    else:
        actions.append(f"unchanged  {SETTINGS_FILE}")

    manifest = {
        "kit_version": layout.version(),
        "files": {dst: hashlib.sha256((kit_root / src).read_bytes()).hexdigest() for src, dst in INSTALL_MAP},
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not manifest_path.exists() or manifest_path.read_bytes() != manifest_data:
        writes.append((manifest_path, manifest_data))
        actions.append(f"write      {MANIFEST}")

    for path, data in writes:
        _write(path, data, dry_run)
    if dry_run:
        actions.append("dry run: nothing was written")
    return actions
