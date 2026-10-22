# Apply101 — Claude Development Guide

## Project Overview

Apply101 is a backend-focused job analysis and candidate-job matching application.

The backend is built primarily with:

- Python
- FastAPI
- SQLAlchemy
- SQLite
- Pydantic
- OpenAI API
- `unittest`

Main application entry point:

`backend/app/main.py`

The current API is organized around:

- authentication (register / login, JWT)
- users
- candidate profiles
- jobs and job analysis
- candidate/job matching

---

# Repository Structure

Important paths:

```text
backend/
├── app/
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── analysis_contract.py
│   ├── job_analysis_sanitizer.py
│   ├── taxonomy.py
│   ├── security.py, auth_dependencies.py, rate_limiting.py
│   ├── analysis_guard.py, profile_write_guard.py
│   ├── job_visibility.py
│   └── openai_client.py
│
├── routers/
│   ├── auth.py
│   ├── users.py
│   ├── profiles.py
│   ├── jobs.py
│   └── matches.py
│
├── migrations/        phase2 ... phase8
├── scripts/           set_admin.py
├── tests/             test_*.py (unittest)
│
├── MATCHING_CONTRACT.md
└── PHASE2_2_INSTALL.md

.claude/               project agents, agent guard hook, settings.json
docs/                  ONLINE_MVP_PLAN.md, ONLINE_MVP_PROGRESS.md
requirements.txt
apply101.db            real local data (not in git)
uploads/               real CV files (not in git)
```

Before making architectural assumptions, inspect the actual repository because this document may lag behind active development.

---

# Sources of Truth

When investigating behavior, use this priority:

1. Existing automated tests
2. Current executable code
3. `backend/app/analysis_contract.py`
4. `backend/MATCHING_CONTRACT.md`
5. Other documentation
6. Assumptions

If documentation conflicts with the current implementation or version constants, do not silently choose one.

Point out the discrepancy.

Current analysis/matching versions should always be read from:

`backend/app/analysis_contract.py`

Do not hard-code version assumptions elsewhere.

---

# Matching Contract

Matching is one of the most sensitive parts of Apply101.

Before modifying any of the following:

- profile analysis
- job analysis
- eligibility rules
- matching
- scoring
- taxonomy
- normalization
- sponsorship handling
- work authorization handling
- residency handling
- language requirements
- experience requirements
- student requirements
- employment type logic

read:

`backend/MATCHING_CONTRACT.md`

and:

`backend/app/analysis_contract.py`

Matching behavior is versioned.

Any change that can alter persisted analysis or matching results may require incrementing the appropriate contract version.

Never bump a version casually.

Never change matching semantics without checking existing tests and compatibility implications.

---

# Current Contract Versions

At the time this file was created, the active constants were:

```text
PROFILE_ANALYSIS_PROMPT_VERSION = profile_analysis_v5
JOB_ANALYSIS_PROMPT_VERSION     = job_analysis_v13
MATCH_VERSION                   = backend_match_v6
```

However, ALWAYS verify these values directly from:

`backend/app/analysis_contract.py`

before relying on them.

Known discrepancy: the "Current versions" list at the top of
`backend/MATCHING_CONTRACT.md` still shows `job_analysis_v8` /
`backend_match_v3`. The constants and the same document further down show
`job_analysis_v13` / `backend_match_v6`.

---

# Database Safety

The current local database is SQLite:

`apply101.db`

Database configuration is defined in:

`backend/app/database.py`

Where real data is touched:

- The database URL is `APPLY101_DATABASE_URL` (see `backend/app/config.py`).
  It defaults to `sqlite:///./apply101.db`, which is relative to the current
  working directory.
- Migrations and scripts read the same variable, but only from the process
  environment: they never load `.env`.
- At startup the app refuses the repository's `uploads/` with any database
  other than the repository's `apply101.db`, because CV file names come from
  database ids.
- CV uploads go to `APPLY101_UPLOAD_DIR`, default the relative directory
  `uploads/`.
- `backend/app/main.py` loads `.env` through `config.load_env_file()`, unless
  `APPLY101_LOAD_DOTENV=0`. python-dotenv finds the repository `.env` from
  the package location, whatever the working directory.
- Importing `backend.app.main` also runs `Base.metadata.create_all` on the
  configured database.

So any app run, script or import without these variables works on the real
data. For development runs, use the demo command in
`docs/ONLINE_MVP_PROGRESS.md`. It sets every variable, together with
`APPLY101_ANALYSIS_PROVIDER=fake`.

Important rules:

- Do NOT delete `apply101.db`.
- Do NOT overwrite it.
- Do NOT reset it.
- Do NOT recreate it just to fix a development problem.
- Do NOT commit arbitrary database mutations.
- Do NOT modify production/user data unless explicitly requested.
- Prefer explicit migrations for schema changes.
- Preserve backward compatibility where practical.

If a requested feature requires a schema change:

1. inspect the current model,
2. inspect existing migrations,
3. explain the required schema change,
4. implement the migration,
5. verify existing data compatibility.

Never use destructive database operations as the easiest solution.

---

# Secrets and Environment Variables

The application uses environment configuration and external APIs.

Never:

- print API keys,
- expose secrets,
- commit `.env`,
- hard-code secrets,
- replace environment variables with literal credentials.

If an environment variable is missing, report it instead of inventing a value.

---

# Claude Code Guards

There are two separate guards, with different scopes:

- **Project agents.** `.claude/hooks/agent_file_guard.py` (PreToolUse) applies
  only to the four project agents (`project-auditor`, `project-developer`,
  `project-reviewer`, `project-supervisor`).
  - It blocks access to `apply101.db*`, `.env` and `uploads/`, file deletion,
    and real-database entry points.
  - It inspects shell commands as text, so it is not a security boundary.
- **Main session.** The guard does not cover the main session.
  - `.claude/settings.json` denies Read and Edit on `.env`, `.env.*`,
    `apply101.db*` and `uploads/**` for every session.
  - Shell commands are not covered by these rules, so the rules above still
    apply.

Do not weaken either guard to get a task done; report the conflict instead.

---

# Development Workflow

For every non-trivial task:

## 1. Investigate

Before editing code:

- locate the relevant files,
- understand the existing flow,
- inspect callers and dependencies,
- inspect relevant tests,
- check matching/version contracts when applicable.

Do not implement from the task description alone.

## 2. Explain

Briefly state:

- what is causing the issue or where the feature belongs,
- which files likely need modification,
- any important compatibility concerns.

## 3. Implement

Make the smallest coherent change necessary.

Prefer modifying existing abstractions over introducing unnecessary new abstractions.

## 4. Verify

Run the narrowest relevant tests first.

Then run broader tests when appropriate.

## 5. Report

At the end report:

- files changed,
- behavior changed,
- tests executed,
- test results,
- remaining risks or TODOs.

---

# Change Scope Rules

Do not make unrelated improvements while implementing a task.

Avoid:

- drive-by refactors,
- mass formatting,
- unnecessary renaming,
- moving files without a reason,
- rewriting functioning modules,
- introducing frameworks or libraries unnecessarily,
- changing public API response structures unless required.

If you notice unrelated technical debt, mention it separately instead of fixing it automatically.

Keep diffs small and reviewable.

---

# Existing Architecture First

Do not create a second implementation of something that already exists.

Before adding:

- helper functions,
- services,
- schemas,
- database models,
- normalization logic,
- matching logic,
- API routes

search the repository for existing implementations.

Prefer reuse and consolidation.

---

# API Compatibility

Before changing an endpoint:

- inspect its request schema,
- inspect its response schema,
- inspect callers,
- inspect tests,
- consider persisted data compatibility.

Do not silently rename JSON fields or change response shapes.

If a breaking API change is genuinely required, explicitly flag it before implementation.

---

# Pydantic / SQLAlchemy Boundary

Keep responsibilities clear:

- SQLAlchemy models represent persisted database entities.
- Pydantic schemas represent API/input/output contracts.
- Router logic should not duplicate schema or model behavior unnecessarily.

When adding fields, verify all relevant layers:

1. database model
2. migration
3. Pydantic schemas
4. creation/update logic
5. API responses
6. tests
7. matching/analysis code if applicable

---

# Matching and Eligibility Safety

Hard requirements must not be inferred from weak evidence.

When working on job analysis or eligibility:

- preserve evidence-backed requirements,
- distinguish hard requirements from preferences,
- do not infer restrictions solely from location,
- do not convert soft preferences into hard requirements,
- preserve `eligible`, `ineligible`, and `review_required` semantics,
- preserve stable machine-readable reason codes where possible.

Unknown information should not automatically become a hard rejection unless the existing contract explicitly says so.

Job-analysis sanitization exists for a reason.

Do not bypass:

`backend/app/job_analysis_sanitizer.py`

without a strong technical justification.

---

# AI / LLM Changes

Treat prompt changes like code changes.

A prompt modification can change persisted output and downstream matching behavior.

Before changing an analysis prompt:

- inspect its schema,
- inspect sanitizer behavior,
- inspect downstream matching,
- inspect existing examples/tests,
- determine whether its version must be bumped.

`backend/app/fake_analysis.py` is the deterministic stand-in for the
OpenAI client, selected only with `APPLY101_ANALYSIS_PROVIDER=fake`.
- It reads the data section of the analysis prompts, starting at
  `User basic information:` and `Job title:`.
- It returns payloads for the structured-output schemas.
- If you change a prompt's data labels or a structured-output schema,
  update it and run `backend.tests.test_fake_analysis_provider`.
- Never make it a fallback for the real provider.

Never solve deterministic business logic by adding an LLM call when normal code is sufficient.

Prefer:

deterministic validation → deterministic normalization → LLM only where semantic analysis is actually required.

---

# Testing

Matching tests currently use Python's `unittest`.

For matching-related changes, the baseline command is:

```bash
python -m unittest backend.tests.test_matching
```

When appropriate, run broader discovery from the repository root:

```bash
python -m unittest discover backend/tests
```

Plain `python -m unittest discover` from the root runs 0 tests because `backend/` has no `__init__.py`.

On Windows, use the project venv: `.venv/Scripts/python.exe -m unittest ...`.

Tests must only use throwaway SQLite databases and temporary directories.
They must never import `backend.app.main`, load `.env`, or call the real
OpenAI API.

The agent guard hook has its own tests:

```bash
.venv/Scripts/python.exe -m unittest discover -s .claude/hooks -p "test_*.py"
```

For bug fixes:

1. reproduce the bug,
2. add or identify a regression test,
3. verify the test fails for the expected reason when practical,
4. implement the fix,
5. verify the test passes.

Never weaken or delete a valid test simply to make a change pass.

If an existing test appears incorrect because the contract intentionally changed, explain why before updating it.

---

# Running the Backend

From the repository root, the FastAPI application can normally be run with:

```bash
uvicorn backend.app.main:app --reload
```

Without the `APPLY101_*` variables, this uses the real `apply101.db`,
`uploads/` and `.env`. Only do that when the user explicitly asks.

For development and testing, use the demo command in
`docs/ONLINE_MVP_PROGRESS.md`:
- a temp database and upload directory;
- `APPLY101_LOAD_DOTENV=0` and a throwaway JWT secret;
- the fake analysis provider, so no OpenAI calls are made.

If the command fails due to environment/setup differences, investigate rather than changing imports blindly.

---

# Dependencies

Dependencies are defined in:

`requirements.txt`

Do not add a new dependency if Python's standard library or an existing dependency can reasonably solve the problem.

If adding a dependency is necessary:

- explain why,
- add it explicitly,
- consider deployment implications.

---

# Large Files and Generated Data

Treat the following carefully:

- `apply101.db`
- `uploads/`
- generated analysis data
- user documents
- cached/generated results

Do not modify, remove, regenerate, or commit these as side effects of unrelated development.

---

# Git Safety

Do not:

- force push,
- rewrite history,
- delete branches,
- commit secrets,
- commit database changes accidentally,
- commit unrelated generated files.

Before committing, inspect the diff.

Commits should represent one understandable unit of work.

---

# Definition of Done

A task is NOT complete merely because code was written.

A task is complete when:

- the existing implementation was understood,
- the requested behavior is implemented,
- unrelated behavior was preserved,
- relevant tests pass,
- new behavior is covered where reasonable,
- schemas/models/migrations remain consistent,
- matching contract/versioning was considered where relevant,
- no secrets or unwanted generated data were introduced,
- the final diff is focused,
- remaining risks are clearly reported.

---

# Communication Style

Be concise but technically explicit.

When given a task:

1. investigate first,
2. do not guess,
3. state important findings,
4. implement the smallest correct solution,
5. test it,
6. report the result.

Do not claim that something works unless you verified it.

If verification cannot be performed, say exactly what was not verified.

---

# Important Apply101 Principle

Correctness is more important than adding functionality quickly.

In particular, false job eligibility conclusions can cause the user to waste time applying to unsuitable jobs or skip valid opportunities.

Changes to analysis and matching logic should therefore favor:

- explicit evidence,
- deterministic rules,
- explainability,
- backward compatibility,
- test coverage,
- conservative handling of unknown information.