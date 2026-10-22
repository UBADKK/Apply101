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
| W1 | Configurable database URL and upload directory, explicit `.env` loading | todo | Keep current local defaults |
| W2 | Fake analysis provider for development and end-to-end tests | todo | Must never be active in production |
| W3 | Per-user limit for profile analysis | todo | Depends on D3 |
| W4 | Web client and CORS | todo | Depends on D1 |
| W5 | Hosting and database setup | todo | Depends on D2 |
| W6 | Portable stored CV paths | todo | Existing rows store OS-specific relative paths |
| W7 | Account deletion removes CV files | todo | |
| W8 | HTTPS, secrets, backups, log hygiene | todo | Depends on D2 |
| W9 | Fix stale version list in `MATCHING_CONTRACT.md` | todo | Documentation only |

## Acceptance criteria

| ID | Area | Status | Evidence |
|---|---|---|---|
| AC1 | Registration and login | todo | Backend endpoints exist and are tested |
| AC2 | Profile and PDF CV | todo | Backend endpoints exist; persistence and paths open (W1, W6) |
| AC3 | Manual job (text and URL) | todo | Backend endpoint exists and is tested |
| AC4 | Analysis | todo | Backend endpoints exist; profile analysis limit open (W3) |
| AC5 | Reasoned match result | todo | Backend endpoints exist |
| AC6 | End to end and operations | todo | Needs W1-W8 |

## Development environment

- Tests use throwaway SQLite databases and temporary directories. None of
  them imports `backend.app.main`.
  - Backend: `.venv/Scripts/python.exe -m unittest discover backend/tests`
  - Hook: `.venv/Scripts/python.exe -m unittest discover -s .claude/hooks -p "test_*.py"`
- To run the app without touching the real `apply101.db`, `.env` or
  `uploads/`, start it from an empty directory outside the repository, with
  `.env` loading disabled and a throwaway JWT secret. The relative database
  and upload paths then resolve inside that directory. Without
  `OPENAI_API_KEY`, the analysis endpoints answer 503. The same setup was
  verified in-process with FastAPI's `TestClient` (see the log); the server
  command below has not been run yet.

  ```bash
  cd /path/to/empty-sandbox
  PYTHON_DOTENV_DISABLED=1 JWT_SECRET_KEY="<random, at least 32 bytes>" \
    /path/to/Apply101/.venv/Scripts/python.exe -m uvicorn backend.app.main:app \
    --app-dir /path/to/Apply101 --port 8001
  ```

## Log

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
