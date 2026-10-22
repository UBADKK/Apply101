# Apply101 Online MVP Progress

Tracks [ONLINE_MVP_PLAN.md](ONLINE_MVP_PLAN.md). Update this file in the same
change that moves an item; the newest log entry goes on top.

Status values: `todo`, `in progress`, `blocked`, `done`.

## Decisions

| ID | Decision | Status | Outcome |
|---|---|---|---|
| D1 | Web client approach | todo | |
| D2 | Hosting provider and database engine | todo | |
| D3 | OpenAI budget and per-user analysis limits | todo | |
| D4 | Registration policy (open / invite) | todo | |
| D5 | Password reset and email verification in v1 | todo | |
| D6 | Data retention and privacy notice | todo | |

## Work items

| ID | Item | Status | Notes |
|---|---|---|---|
| W1 | Configurable database URL and upload directory, explicit `.env` loading | done | `APPLY101_DATABASE_URL`, `APPLY101_UPLOAD_DIR`, `APPLY101_LOAD_DOTENV`; defaults unchanged |
| W2 | Fake analysis provider for development and end-to-end tests | done | `APPLY101_ANALYSIS_PROVIDER=fake`, only with a non-default database and upload directory; no fallback |
| W3 | Per-user limit for profile analysis | todo | Depends on D3 |
| W4 | Web client and CORS | todo | Depends on D1 |
| W5 | Hosting and database setup | todo | Depends on D2 |
| W6 | Portable stored CV paths | todo | Existing rows store OS-specific relative paths; a configured upload dir on Windows stores mixed separators (`C:/.../uploads\user_1_profile_1_cv.pdf`) |
| W7 | Account deletion removes CV files | todo | |
| W8 | HTTPS, secrets, backups, log hygiene | todo | Depends on D2 |
| W9 | Fix stale version list in `MATCHING_CONTRACT.md` | todo | Documentation only |

## Acceptance criteria

| ID | Area | Status | Evidence |
|---|---|---|---|
| AC1 | Registration and login | todo | Backend endpoints exist and are tested |
| AC2 | Profile and PDF CV | todo | Backend endpoints exist; persistence and paths open (W1, W6) |
| AC3 | Manual job (text and URL) | todo | Backend endpoint exists and is tested |
| AC4 | Analysis | in progress | Works end to end with the fake provider in a sandbox; profile analysis limit open (W3); real provider not exercised |
| AC5 | Reasoned match result | in progress | Sandbox demo returned `eligible` and `ineligible` results with reason codes; staging check open |
| AC6 | End to end and operations | todo | Needs W1-W8 |

## Development environment

- Tests use throwaway SQLite databases and temporary directories. None of
  them imports `backend.app.main`.
  - Backend: `.venv/Scripts/python.exe -m unittest discover backend/tests`
  - Hook: `.venv/Scripts/python.exe -m unittest discover -s .claude/hooks -p "test_*.py"`
- Runtime configuration (`backend/app/config.py`, `backend/app/openai_client.py`).
  Every default is the previous behaviour.

  | Variable | Default | Effect |
  |---|---|---|
  | `APPLY101_DATABASE_URL` | `sqlite:///./apply101.db` | SQLAlchemy URL. Migrations and scripts read it too, but only from the process environment (only `main.py` loads `.env`). |
  | `APPLY101_UPLOAD_DIR` | `uploads` | CV upload directory. The app refuses to start when it points to the repository's `uploads/` and the database is not the repository's `apply101.db`, because CV file names come from database ids. |
  | `APPLY101_LOAD_DOTENV` | on | `0`/`false`/`no`/`off` skips `.env`; any other unrecognized value is an error. |
  | `APPLY101_ANALYSIS_PROVIDER` | `openai` | `fake` selects the deterministic demo provider. It is accepted only when both the database and the upload directory resolve away from the defaults and the repository's real files; any other unrecognized value is an error. |

- **Demo server.** This command runs the app from the repository root without
  touching the real `apply101.db`, `.env` or `uploads/`:
  - the database and CVs go to a new temp directory;
  - `.env` is not loaded and the JWT secret is throwaway;
  - analyses come from the fake provider, so no OpenAI call is made.

  It was verified on 2026-10-22 with Git Bash. On Linux or macOS, drop the
  `cygpath -m` call and use `.venv/bin/python`.

  ```bash
  SANDBOX="$(cygpath -m "$(mktemp -d)")"
  APPLY101_LOAD_DOTENV=0 \
  APPLY101_DATABASE_URL="sqlite:///$SANDBOX/demo.db" \
  APPLY101_UPLOAD_DIR="$SANDBOX/uploads" \
  APPLY101_ANALYSIS_PROVIDER=fake \
  JWT_SECRET_KEY="$(.venv/Scripts/python.exe -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  OPENAI_API_KEY= \
  .venv/Scripts/python.exe -m uvicorn backend.app.main:app --port 8001
  ```

  Fake analyses are stored with the real model name and prompt version, so
  matching accepts them, and demo CVs are named after database ids. That is
  why the fake provider refuses to run against the default database or the
  default upload directory.

  The existing per-user job analysis cooldown still applies: analyzing a
  second job within 60 seconds returns 429 with `Retry-After`.

## Log

### 2026-10-22: W1 and W2
- **Branch:** `feat/online-mvp-config-fake-analysis`, opened from the
  preparation commit `b12f709`.
- **W1:**
  - Added `backend/app/config.py` (`APPLY101_DATABASE_URL`,
    `APPLY101_UPLOAD_DIR`, `APPLY101_LOAD_DOTENV`).
  - `database.py`, the CV upload route and `main.py` use it. SQLite keeps
    `check_same_thread=False`; other URLs get no SQLite-only arguments.
- **W2:**
  - Added `backend/app/fake_analysis.py`, a deterministic `FakeAnalysisClient`
    selected in `openai_client.create_openai_client_or_none()`.
  - It stands in for the OpenAI client at the existing seams, so routes,
    guards, quotas, schema validation, the job-analysis sanitizer,
    persistence and matching are unchanged. No matching rule or contract
    version changed.
  - It uses only the candidate/job data section of the prompt: the skill
    tags found there, and the profile fields the user entered. Hard
    requirements come from the existing sanitizer.
  - A job's role tags are `["other"]`, because matching requires at least
    one role tag; a profile's stay empty.
  - It only produces the two structured outputs. The admin-only
    `analyze-sample` call therefore fails with its generic 500
    `ERR_JOB_ANALYSIS_FAILED` in fake mode.
  - **After review:**
    - Fake mode also requires a non-default upload directory.
    - "Default" is decided by resolved path, not by text.
    - The app refuses the repository's `uploads/` with any database other
      than the repository's `apply101.db`.
    - Not handled: a SQLite URI-style URL
      (`sqlite:///file:apply101.db?uri=true`) is not recognized as the
      default database. It has to be written on purpose.
    - Language lines are parsed loosely, so a language is never dropped.
    - The prompt's data section is found by its first marker.
- **Tests:**
  - Backend 793 OK (32 new: 20 in `test_runtime_config.py`, 12 in
    `test_fake_analysis_provider.py`).
  - Hook: 25 tests (781 subtests) OK.
  - The real `apply101.db`, `.env` and `uploads/` kept the same size and
    mtime.
- **End to end on the real server:** uvicorn, fake data, sandbox only.
  - Run once through a Python launcher and once with the demo command
    above; both gave identical results.
  - Flow: register, login, `/me`, profile with languages, PDF CV upload
    (text extracted), two manual jobs, fake profile and job analysis.
  - Match results:
    - Matching job: `eligible`, overall 74, `apply`, with strengths,
      weaknesses and requirement warnings.
    - Job requiring German C1: `ineligible`, overall 20, hard fail
      `required_language_missing`.
  - Then a repeated request returned `cached`, and the match list worked.
  - Re-run with the demo command after the review fixes: same results.
- **Startup check:** `import backend.app.main` from the repository root,
  with `.env` off and a separate temp database but no upload directory,
  raised `ConfigError` before anything was created.
- **Review:** project-reviewer, read-only.
  - First round: changes requested (uploads overwrite risk, path-based
    default detection, language lines, marker search, docs, test gaps).
  - Second round: approved.
  - The remaining minor finding was fixed afterwards: a fresh
    `./apply101.db` in another working directory combined with the
    repository's `uploads/` is now refused too.

### 2026-10-22: preparation
- **Branch:** `chore/online-mvp-prep`, opened from `main` at `e8208d8`
  after PR #24 (project-agent guard) was merged.
- **Plan:** added this file and [ONLINE_MVP_PLAN.md](ONLINE_MVP_PLAN.md).
- **Baseline:** backend 761 tests OK; hook 25 tests (781 subtests) OK.
  - The real `apply101.db`, `.env` and `uploads/` kept the same size and
    mtime across the backend run.
- **Smoke run:** done in a sandbox with fake data. Register, login, `/me`,
  profile, PDF CV upload and manual job all worked. Analysis returned 503
  without a key, and matching returned 400 until analyses exist.
- **Main session protection:** `.claude/settings.json` now denies Read and
  Edit on `.env`, `.env.*`, `apply101.db*` and `uploads/**`, because the
  agent guard does not cover the main session.
  - Verified in a fake project on Claude Code 2.1.308: the Read and Write
    calls were denied, and Grep skipped the fake `.env`.
