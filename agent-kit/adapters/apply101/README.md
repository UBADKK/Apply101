# Apply101 adapter

`PROJECT_CONTEXT.md` here is the Apply101 adapter for the Developer Agent
Kit. `test_apply101_adapter.py` checks it with synthetic inputs only (no
Apply101 code is imported, no real data is opened).

## Mapping from the existing Apply101 agents

| Existing (`.claude/agents/`) | Kit | Notes |
|---|---|---|
| `project-auditor` | `kit-auditor` | Same read-only tools. The kit auditor defines scope and risks for one task and returns a verdict. |
| (none) | `kit-researcher` | New read-only fact finder. |
| `project-developer` | `kit-developer` | Same tools. Scope is enforced afterwards by `checkpoint event developed`. |
| `project-reviewer` | `kit-reviewer` | Same read-only boundaries and `permissionMode: plan`. |
| `project-supervisor` | main session + `ORCHESTRATION.md` | No fifth agent. The supervisor's single fix round becomes `max_fix_rounds` (2 here, core maximum 3). |
| `agent_file_guard.py` | `kit_guard.py` + this adapter | `apply101.db*`, `.env`, `uploads/` and the real-database entry points come from the adapter. |

Project facts that used to be written into the agents (Turkish reports,
venv paths, test commands, contract files, real data) are in
`PROJECT_CONTEXT.md` now.

## Differences from the existing guard

The kit guard with this adapter blocks every command of the existing guard's
corpus (see the adapter test) and is stricter in places:

- it also protects backups (`*backup*.db`, `apply101_*.db`, `backups/`),
  secret environment variables and key material, and writes into `.claude/`;
- it blocks `mv`/`cp`, state-changing git (including `git -c`), network,
  cloud and deployment tools, package installs and `sudo`;
- `backend.app.deploy_app` and `frontend/e2e/seed_catalog.py` count as
  real-data entry points;
- only exact kit agent types are guarded; the existing guard keeps covering
  the `project-*` agents, and the main session is covered by the deny rules
  in `.claude/settings.json` for both.

## Installing it in Apply101 (not done in this change)

The kit is not installed into Apply101's live `.claude/` yet. When the user
decides to:

```bash
python3 agent-kit/kit.py install --target . --context agent-kit/adapters/apply101/PROJECT_CONTEXT.md --dry-run
python3 agent-kit/kit.py install --target . --context agent-kit/adapters/apply101/PROJECT_CONTEXT.md
python3 .claude/agent-kit/kit.py validate
.venv/bin/python -m unittest discover -s .claude/hooks -p "test_*.py"
```

The install adds a second PreToolUse entry next to `agent_file_guard` and
appends deny rules; the existing entry, its tests and the `project-*` agents
stay as they are. Retiring the `project-*` agents is a separate decision.
