# Project context (agent kit adapter): Apply101

Example adapter for the Apply101 repository. Installed, it lives at
`.claude/agent-kit/PROJECT_CONTEXT.md`. The repository's `CLAUDE.md` stays
the main project guide; this file adds what the kit agents and gates need.

## Project summary

Apply101 analyses job postings and matches candidates to jobs. Backend:
Python, FastAPI, SQLAlchemy, SQLite, Pydantic, OpenAI API (`backend/`,
entry point `backend/app/main.py`, deployment app
`backend/app/deploy_app.py`). Web client: React, TypeScript, Vite
(`frontend/`). A private Railway test deployment is built from `Dockerfile`
and `railway.json`.

## Repository map

- `backend/app/` - application modules: config, database, models, schemas,
  analysis contract, sanitizer, taxonomy, OpenAI client, fake provider.
- `backend/routers/` - auth, users, profiles, jobs, matches and more.
- `backend/migrations/` - phase migrations (each runnable as `__main__`).
- `backend/scripts/` - admin, catalog sync and backup scripts.
- `backend/tests/` - `unittest` suites (temporary SQLite only).
- `backend/MATCHING_CONTRACT.md` - matching and eligibility contract.
- `frontend/` - web client with Vitest and Playwright tests.
- `.claude/` - project agents, the existing agent file guard, settings.
- `docs/` - online MVP plan, progress log, Railway runbook.

## Sources of truth

1. Existing automated tests. 2. Current executable code.
3. `backend/app/analysis_contract.py` (version constants). 4.
`backend/MATCHING_CONTRACT.md`. 5. Other documentation.
Known discrepancy: the "Current versions" list at the top of
`backend/MATCHING_CONTRACT.md` is older than the constants in
`backend/app/analysis_contract.py`. Report discrepancies; never pick one
silently.

## Sensitive areas

- Matching, eligibility, scoring, taxonomy, normalization and job-analysis
  sanitization: read `backend/MATCHING_CONTRACT.md` and
  `backend/app/analysis_contract.py` first. Changes that can alter persisted
  analysis or match results may need a version bump; never bump casually.
- Analysis prompts and structured-output schemas (in `backend/routers/` and
  `backend/app/cover_letter.py`) and the fake provider
  `backend/app/fake_analysis.py`, which must follow prompt label changes.
- Hard requirements must not be inferred from weak evidence; keep
  `eligible`, `ineligible` and `review_required` semantics and stable reason
  codes.
- Authentication, authorization, quotas and rate limits.
- Public API request and response shapes.
- `backend/app/models.py`, `backend/app/schemas.py`, migrations and
  `backend/app/database.py`: schema changes need an explicit migration.

## Real data and secrets

- Real data: `apply101.db` (and `-wal`, `-shm`, `-journal`), local database
  backups (`*backup*.db`, `apply101_*.db`, `backups/`), CV files in
  `uploads/`. Secrets: `.env`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`,
  `APPLY101_ACCESS_KEY`.
- Any app run, script, migration or import of `backend.app.main` without the
  `APPLY101_*` variables opens the real database and loads `.env`.
  Importing `backend.app.main` also runs `create_all`.
- Isolated runs use the demo command in `docs/ONLINE_MVP_PROGRESS.md`: a temp
  database and upload directory, `APPLY101_LOAD_DOTENV=0`, a throwaway JWT
  secret and `APPLY101_ANALYSIS_PROVIDER=fake`. Agents do not start servers;
  the orchestrator runs the demo only when the user asks.

## Verification

- Backend tests use throwaway SQLite databases and temporary directories,
  never import `backend.app.main`, never load `.env`, never call OpenAI.
- Use the project venv: `.venv/bin/python` on Linux,
  `.venv/Scripts/python.exe` on Windows (the commands below use the Linux
  form; replace the interpreter on Windows).
- Matching changes: run `test_commands.matching` first.
- Prompt label or structured-output schema changes: run
  `test_commands.fake_provider`.
- The frontend commands run from `frontend/`; `npm run e2e` starts its own
  isolated backend with the fake provider.

## Conventions

- Smallest coherent change; no drive-by refactors, mass formatting or
  renames; reuse existing helpers before adding new ones.
- Agent reports in Turkish, concise and technically explicit.
- Commit messages: imperative summary line, body explaining why.

## Machine-readable configuration

```json kit-config
{
  "kit_config_version": 1,
  "project_name": "Apply101",
  "report_language": "Turkish",
  "default_branch": "main",
  "checkpoint_dir": ".claude/agent-kit/checkpoints",
  "protected_paths": [
    "apply101.db*",
    "*backup*.db",
    "apply101_*.db",
    "uploads/**",
    "backups/**"
  ],
  "real_data_commands": [
    "^uvicorn\\b",
    "-m\\s+uvicorn\\b",
    "\\buvicorn\\.run\\b",
    "-m\\s+backend\\.(?:migrations|scripts)\\b",
    "backend[/\\\\](?:migrations|scripts)[/\\\\]\\S+\\.py",
    "backend[./\\\\]app[./\\\\](?:main|deploy_app)\\b",
    "\\bfrom\\s+backend\\.app\\s+import\\b[^;\\n]*\\b(?:main|deploy_app)\\b",
    "\\bfrom\\s+backend\\s+import\\b[^;\\n]*\\b(?:migrations|scripts)\\b",
    "frontend[/\\\\]e2e[/\\\\]seed_catalog\\.py"
  ],
  "secret_env_vars": [
    "OPENAI_API_KEY",
    "JWT_SECRET_KEY",
    "APPLY101_ACCESS_KEY",
    "APPLY101_DATABASE_URL"
  ],
  "watch_files": ["apply101.db"],
  "approval_required_paths": [
    "backend/migrations/**",
    "backend/app/analysis_contract.py",
    "backend/MATCHING_CONTRACT.md",
    "backend/app/job_analysis_sanitizer.py",
    "backend/app/taxonomy.py",
    "backend/app/fake_analysis.py",
    "backend/app/cover_letter.py",
    "backend/app/models.py",
    "backend/app/schemas.py",
    "backend/app/database.py",
    "backend/app/config.py",
    "backend/app/security.py",
    "backend/app/auth_dependencies.py",
    "backend/app/deploy_app.py",
    "backend/routers/matches.py",
    "Dockerfile",
    ".dockerignore",
    "railway.json",
    "requirements.txt",
    "frontend/package.json",
    "frontend/package-lock.json",
    "CLAUDE.md",
    ".gitignore",
    ".claude/**"
  ],
  "risk_keywords": {
    "production": ["railway", "canlı", "yayına", "üretim", "dağıtım"],
    "data": ["apply101.db", "uploads", "veritabanı", "göç", "yedek", "matching contract", "eligibility", "version bump"],
    "secrets": ["openai key", "jwt secret", "sır", "parola"],
    "external": ["openai", "greenhouse"]
  },
  "test_commands": {
    "full": ".venv/bin/python -m unittest discover backend/tests",
    "focused": ".venv/bin/python -m unittest backend.tests.<module>",
    "matching": ".venv/bin/python -m unittest backend.tests.test_matching",
    "fake_provider": ".venv/bin/python -m unittest backend.tests.test_fake_analysis_provider",
    "hooks": ".venv/bin/python -m unittest discover -s .claude/hooks -p \"test_*.py\"",
    "frontend_unit": "cd frontend && npm test",
    "frontend_typecheck": "cd frontend && npm run typecheck",
    "frontend_e2e": "cd frontend && npm run e2e"
  },
  "loop_limits": {"max_fix_rounds": 2},
  "core_must_not_mention": ["apply101", "OPENAI", "FastAPI", "MATCHING_CONTRACT", "matching contract"]
}
```
