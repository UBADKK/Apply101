# Project context (agent kit adapter)

This file is the only project-specific part of the agent kit. The four agents
read it at the start of every task; the guard, the validator and the
checkpoint CLI read the `kit-config` block at the end. Replace every
`<FILL ...>` marker, then run `python3 .claude/agent-kit/kit.py validate`.
Re-run the installer after changing `protected_paths` so the deny rules in
`.claude/settings.json` follow.

Keep it factual and short. Do not put secrets, real data or personal
information here.

## Project summary

<FILL: two or three sentences: what the project does, its main languages and frameworks, and its entry points.>

## Repository map

<FILL: the important directories and files, one line each, e.g. "src/api/ - HTTP handlers".>

## Sources of truth

<FILL: which source wins when they disagree, e.g. "1. tests 2. code 3. docs/contract.md". Name known discrepancies.>

## Sensitive areas

<FILL: behaviour that needs extra care and why: contracts and their version constants, authentication, billing, migrations, public API shapes. Point to the files.>

## Real data and secrets

<FILL: where real data, uploads, backups and secrets live; how development and test runs are isolated from them (environment variables, temp databases, fake providers). These paths go into protected_paths and watch_files below.>

## Verification

<FILL: how to run focused and full tests safely, and what tests must never do (e.g. import the app module that opens the real database, call paid APIs).>

## Conventions

<FILL: code and test conventions the agents must follow, commit message style, anything else. Write "none" if there are none.>

## Machine-readable configuration

Fields (the adapter can only add restrictions to the core rules):

| Field | Meaning |
|---|---|
| `kit_config_version` | Always `1`. |
| `project_name` | Short project name. The validator keeps it out of the kit core. |
| `report_language` | Language of agent reports, e.g. `English`. |
| `default_branch` | Branch agents never work on directly, e.g. `main`. |
| `checkpoint_dir` | Project-relative directory for checkpoints. Kit agents cannot write there. |
| `protected_paths` | Extra paths no agent may read or change, added to the core list (secrets and keys). A file-name glob (`*.db`, `app.db*`) matches at any depth; `name/**` protects a directory (`uploads/**`). |
| `real_data_commands` | Python regexes; a shell command of a kit agent that matches one is blocked (entry points that open real data or live systems). The core already blocks common migration runners and database clients (`manage.py migrate`, `alembic upgrade`, `psql`, ...). |
| `secret_env_vars` | Environment variable names agents may not expand, in addition to names containing SECRET, TOKEN, PASSWORD, API_KEY, ... |
| `watch_files` | Project-relative real-data files whose size and modification time the checkpoint records; a change stops the task. |
| `approval_required_paths` | Globs (`migrations/**`, `src/contract.py`) whose change needs an explicit user decision. |
| `risk_keywords` | Extra words per risk flag (`production`, `remote_git`, `data`, `secrets`, `external`) for the request scan. |
| `test_commands` | Named commands the developer may run; `full` is required. They are never run by the validator. |
| `loop_limits` | `max_fix_rounds`, 0 to 3 (default 2). |
| `core_must_not_mention` | Optional words that must never appear in the kit core (checked by `kit.py check` in the kit source). |

```json kit-config
{
  "kit_config_version": 1,
  "project_name": "<FILL: short project name>",
  "report_language": "English",
  "default_branch": "main",
  "checkpoint_dir": ".claude/agent-kit/checkpoints",
  "protected_paths": ["*.db", "*.db-*", "*.sqlite", "*.sqlite-*", "*.sqlite3"],
  "real_data_commands": [],
  "secret_env_vars": [],
  "watch_files": [],
  "approval_required_paths": [
    "**/migrations/**",
    "*.sql",
    ".github/workflows/**",
    "Dockerfile",
    "docker-compose*.yml",
    "requirements*.txt",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    ".claude/**"
  ],
  "risk_keywords": {},
  "test_commands": {
    "focused": "<FILL: command for one test module, e.g. python -m pytest tests/test_x.py>",
    "full": "<FILL: command for the whole suite>"
  },
  "loop_limits": {"max_fix_rounds": 2}
}
```
