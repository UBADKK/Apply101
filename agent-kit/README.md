# Developer Agent Kit v1

A small, portable Claude Code setup: four agents (researcher, auditor,
developer, reviewer), the main session as orchestrator, a guard hook, a
checkpoint format with resumable workflow gates, a validator and tests.
Standard library Python and bash only (tested with Python 3.11 and 3.13);
no services, no dependencies.

```text
agent-kit/
├── kit.py                  CLI: install, validate, check, checkpoint
├── kitlib/                 installer, validator, checkpoint format, workflow gates
├── core/                   portable core (never project-specific)
│   ├── agents/             kit-researcher, kit-auditor, kit-developer, kit-reviewer
│   ├── commands/           /kit-task and /kit-resume for the main session
│   ├── docs/               ORCHESTRATION.md, SAFETY_POLICY.md, CHECKPOINT_TEMPLATE.md
│   └── hooks/              kit_guard.py (+ kit_guard.sh wrapper)
├── templates/              PROJECT_CONTEXT.md: the adapter template to fill
├── adapters/               example adapters (with their own tests)
└── tests/                  tests in throwaway fake projects
```

The core holds no project names, paths, frameworks, test commands or
credentials. Everything project-specific lives in one file per project, the
**adapter** `PROJECT_CONTEXT.md`: prose the agents read plus a `kit-config`
JSON block that the guard, validator and checkpoint CLI read. An adapter can
only add restrictions to the core rules. `kit.py check` fails if any
adapter's project name or `core_must_not_mention` words appear in the core.

## Install in a new project (about 10 minutes)

Requirements: the project is a git repository; `python3` and `bash` are on
PATH (Git Bash on Windows).

1. **Add the kit.** From a copy of this directory (anywhere on disk):

   ```bash
   python3 agent-kit/kit.py install --target /path/to/project
   ```

   It copies the agents to `.claude/agents/`, the commands to
   `.claude/commands/`, the rest to `.claude/agent-kit/`, creates
   `.claude/agent-kit/PROJECT_CONTEXT.md` from the template (or from
   `--context <adapter.md>`), and merges the guard hook and deny rules into
   `.claude/settings.json` without removing anything. Existing kit files
   that differ are reported and left alone unless you pass `--force`;
   `PROJECT_CONTEXT.md` is never overwritten. Use `--dry-run` to preview.
2. **Fill the project context.** Edit `.claude/agent-kit/PROJECT_CONTEXT.md`:
   replace every `<FILL ...>` marker, list real data under
   `protected_paths` and `watch_files`, entry points that open real data
   under `real_data_commands`, files that need a user decision under
   `approval_required_paths`, and the safe `test_commands`. If you changed
   `protected_paths`, run the install command again (it only adds the new
   deny rules).
3. **Validate.**

   ```bash
   python3 .claude/agent-kit/kit.py validate
   ```

   It checks agent frontmatter and contracts, file references, the adapter,
   the hook entry and deny rules, the manifest (kit files must not be edited
   locally), LF line endings and existing checkpoints. Warnings point at
   protected paths that are not gitignored. Commit the installed files.
4. **Use the team.** In Claude Code (restart it once so it loads the new
   agents, hooks and commands):

   ```text
   /kit-task Fix the rounding error in the invoice total
   /kit-resume fix-invoice-rounding
   ```

   The main session follows `.claude/agent-kit/ORCHESTRATION.md`.

## How a task runs

```text
intake ─► (research) ─► (audit) ─► develop ─► review ─┬─► done
            ≤1 run        gates      scope check      │
                                         ▲            ▼
                                         └── fix ◄── changes_requested
                                             (≤ max_fix_rounds, no repeated finding)
```

- `checkpoint new` scans the request for risk words (production, remote git,
  data, secrets, external systems, plus the adapter's words). Production
  requests wait for the user immediately.
- `event develop` is refused while risk flags or approval-required files in
  the scope have no recorded user decision (`event approve --note ...`).
- `event developed` compares the changed files with the approved scope,
  protected paths, approval-required paths, the pre-existing changes
  (baseline) and watched real-data files. Violations wait for the user;
  nothing is reverted automatically.
- `event review` ends the task on approval, or opens a fix round. The loop
  stops when the round limit is reached or a finding survives a fix round.
- `checkpoint resume` tells a new session whether it can continue
  (`RESUME OK`) or must ask (`RESUME BLOCKED`: other branch, HEAD moved,
  user changes touched, unrecorded changes outside a developer phase).

Exit codes: `0` ok, `1` error, `2` validation failed, `3` refused or
waiting for the user.

## Safety model

See `core/docs/SAFETY_POLICY.md`. In short: deny rules for every session,
a fail-closed guard for the kit agents, explicit tool lists and stop
conditions per agent, and deterministic workflow gates. Kit agents never
commit, push, deploy, install, delete, use the network, read secrets or
real data, edit `.claude/`, or start other agents. The main session commits
or pushes only when the user asks. None of these layers is a security
boundary on its own: the guard reads shell commands as text.

## Developing the kit

One command runs the validator on the core, the template and every adapter,
then all kit and adapter tests:

```bash
python3 agent-kit/kit.py check
```

Tests build throwaway git projects in temporary directories, run git
without the user's configuration and point `CLAUDE_PROJECT_DIR` at the fake
project. They never touch the repository the kit lives in, its data,
`.env` files or the network. To add an adapter, create
`adapters/<name>/PROJECT_CONTEXT.md` (and optionally `test_*.py` next to
it); `check` validates and tests it.

Upgrading a project: copy the new kit version and run
`kit.py install --target <project> --force`; it replaces only kit-owned
files listed in `kitlib/common.py` (`INSTALL_MAP`).

## Known limitations (v1)

- The guard inspects shell text: paths built at run time, code executed by
  scripts or tests, and copies under other names are not detected.
- Plain `rg` and the Grep tool rely on ignore rules; keep protected paths in
  `.gitignore` (the validator warns otherwise).
- Workflow gates run when the orchestrator calls the CLI; the contract
  requires it, but the main session is not hooked.
- Scope checks use `git status`; changes outside the repository or in
  ignored files are only covered by the guard and `watch_files`.
- Risk word scanning is keyword-based; it can over-flag (the user decides)
  and under-flag (the auditor and approval-required paths are the backup).
- No web research for the researcher in v1.

## V2 ideas (not in v1)

- Optional researcher web access with an allowlist, and guard coverage for
  MCP tools.
- Guard-time scope enforcement: block Edit/Write outside the active
  checkpoint's approved scope instead of detecting it afterwards.
- A hook on the main session for the hardest rules (force push, deploy) with
  an explicit per-command user override.
- Structured agent output via JSON schema instead of the `kit-result` block.
- Shared checkpoint history across tasks (a small index), and metrics on
  rounds and stop reasons.
- Packaging as a Claude Code plugin with versioned upgrades.
