# Apply101 Online MVP Progress

Tracks [ONLINE_MVP_PLAN.md](ONLINE_MVP_PLAN.md). Update this file in the same
change that moves an item; the newest log entry goes on top.

Status values: `todo`, `in progress`, `blocked`, `done`.

## Decisions

| ID | Decision | Status | Outcome |
|---|---|---|---|
| D1 | Web client approach | done | React + TypeScript + Vite single-page app in `frontend/`, talking to the existing FastAPI backend (decided by the project owner, 2026-10-22) |
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
| W4 | Web client and CORS | in review | `frontend/` and the backend additions on branch `feat/online-mvp-web-client`; verified locally (Vitest, Playwright in Edge); see "W4 task" below |
| W5 | Hosting and database setup | todo | Depends on D2 |
| W6 | Portable stored CV paths | todo | Existing rows store OS-specific relative paths; a configured upload dir on Windows stores mixed separators (`C:/.../uploads\user_1_profile_1_cv.pdf`) |
| W7 | Account deletion removes CV files | todo | |
| W8 | HTTPS, secrets, backups, log hygiene | todo | Depends on D2 |
| W9 | Fix stale version list in `MATCHING_CONTRACT.md` | todo | Documentation only |

## Acceptance criteria

| ID | Area | Status | Evidence |
|---|---|---|---|
| AC1 | Registration and login | in progress | Web client: register, login, logout, reload and expiry verified locally in Edge; multi-process rate limiting (W5) and staging open |
| AC2 | Profile and PDF CV | in progress | Web client profile (all matching fields, languages) and PDF upload verified locally; persistence and paths on the host open (W5, W6) |
| AC3 | Manual job (text and URL) | in progress | Web client add, list and open, duplicate URL (409), and another user's job (404) verified locally; staging open |
| AC4 | Analysis | in progress | Web client analyses with the fake provider, 429 countdown without retry, and mocked 503 verified locally; profile analysis limit open (W3); real provider not exercised |
| AC5 | Reasoned match result | in progress | Web client shows `eligible` (74) and `ineligible` (20, `required_language_missing`) with reasons; saved results reopen and rematch work; `review_required` covered by a unit test only; staging open |
| AC6 | End to end and operations | todo | Needs W1-W8 |

## W4 task (web client)

Scope, as given by the project owner on 2026-10-22. It is recorded here so
that an interrupted session can resume.

- **Stack:** React + TypeScript + Vite SPA in `frontend/` (D1), English UI,
  the existing FastAPI backend, stable dependencies with a committed
  lockfile. D2-D6 stay open.
- **Flow in the browser:**
  - register, log in and log out;
  - protected pages that survive a page reload and handle an expired
    session;
  - create, open and edit a profile, including every matching field
    (languages, experience, preferences, work authorization), and upload a
    PDF CV;
  - list and open the user's own jobs, and add a manual job (title, URL,
    text);
  - start profile and job analysis, show status and summary, and guide the
    user when an analysis is missing or outdated;
  - match results with `eligible` / `ineligible` / `review_required`,
    scores, strengths, weaknesses and readable reasons;
  - reopen saved results and use the existing rematch behaviour.
- **Quality:**
  - a simple, consistent, presentable design that works on mobile;
  - labelled forms, keyboard use, and empty, loading, success and error
    states;
  - no double submits;
  - clear handling of 401, 404, 409, 422, 429 and 503;
  - Retry-After and quotas respected, and analyses never re-sent
    automatically.
- **Backend:**
  - real endpoints with the fake analysis provider, and a visible demo
    marker;
  - a configurable API URL, and CORS with an explicit origin list;
  - only small, backward-compatible additions. Auth, ownership, quota,
    analysis and matching rules stay unchanged.
  - No secrets in the frontend; example settings are documented in
    Markdown.
- **Safety:** never touch the real `.env`, `apply101.db` or `uploads/`.
  - Use the documented isolation: temp DB and CV directory, `.env` off,
    throwaway JWT secret, fake analysis.
  - No real OpenAI calls, no weaker hooks or permissions, no new agent
    infrastructure.
- **Verification:**
  - frontend build and typecheck, meaningful flow tests, and the backend
    test suite;
  - in a real browser against an isolated real server: the main flow,
    saved results, error states, and data separation between two users;
  - desktop and mobile views, and both a matching and a non-matching
    result.
- **Delivery:** reviewer pass on the final diff, commit and push, and a
  draft PR. No merge or deploy.

### W4 plan and status

| Step | Status |
|---|---|
| Node.js v24.22.1 LTS (official zip, SHA-256 checked) in `%LOCALAPPDATA%\Programs\node-v24.22.1-win-x64` | done |
| Backend additions: `APPLY101_CORS_ORIGINS` and CORS middleware (`expose_headers=["Retry-After"]`); `GET /jobs/?owned=true`; `GET /jobs/{job_id}`; `GET /users/{user_id}/languages`; `GET /meta` | done (commit `26bc320`; 36 new tests, backend 833 OK) |
| Frontend scaffold (Vite, React, TypeScript, React Router, Vitest), API client with typed errors, auth context | done |
| Screens: login/register, dashboard, profile (fields, languages, CV, analysis), jobs (list, new, detail with analysis and match), saved matches | done (typecheck and build pass); browser check pending |
| Frontend unit and flow tests (Vitest + Testing Library) | done (21 tests) |
| End-to-end browser checks against isolated servers (desktop and mobile, two users, error states), screenshots | done (Playwright, 5 tests in Edge, 12 screenshots; also checked by hand in the built-in browser) |
| Docs: run commands, example settings | done (`frontend/README.md`) |
| Review, fixes, commit, push, draft PR | todo |

### W4 resume notes (2026-10-22, usage limit)

- **Node:** `export PATH="/c/Users/umutbaris/AppData/Local/Programs/node-v24.22.1-win-x64:$PATH"`
  in Git Bash (no system PATH change).
- **npm:** npm 11 skips package install scripts. esbuild still works,
  because its `@esbuild/win32-x64` binary comes as an optional dependency.
- **Done in `frontend/`:**
  - `package.json` and `package-lock.json`: React 19.3, React Router 7.18,
    Vite 7.3, Vitest 3.2, TypeScript 5.9, Testing Library, Playwright 1.64
    (to run against the installed Edge, `channel: 'msedge'`, with no
    browser download), `@types/node`;
  - `tsconfig.json`, `vite.config.ts`, `index.html`, `.gitignore`;
  - `src/vite-env.d.ts` and `src/config.ts` (`VITE_API_BASE_URL`, default
    `http://localhost:8001`);
  - `src/api/types.ts`, `src/api/client.ts` (`ApiError` with status, code,
    hint, field errors and Retry-After; the 401 handler),
    `src/api/endpoints.ts`, `src/api/errors.ts` (`describeError`);
  - `src/auth/session.ts` (token in localStorage, JWT `exp`),
    `src/auth/AuthContext.tsx`, `src/auth/RequireAuth.tsx`.
- **Next, in order:**
  1. Components: `Alert` and `LoadingState` (already imported), `Layout`
     with nav, the logout button and the `/meta` demo banner, form fields,
     `MatchResultView`.
  2. `src/styles.css`, `src/main.tsx` and `src/App.tsx` (routes: `/login`,
     `/register`, `/`, `/profile`, `/jobs`, `/jobs/new`, `/jobs/:jobId`,
     `/matches`, `/matches/:matchId`, 404).
  3. The pages.
  4. Vitest tests (`src/test/setup.ts`).
  5. A Playwright e2e test with `webServer`: an isolated backend (temp DB
     and uploads, `APPLY101_LOAD_DOTENV=0`, fake provider, random JWT
     secret, `APPLY101_CORS_ORIGINS`, a short
     `JOB_ANALYSIS_USER_SUCCESS_COOLDOWN_SECONDS`) plus the Vite server.
     It saves screenshots to `e2e/screenshots/`, which is gitignored.
  6. Docs and a README, the reviewer pass, and a draft PR.
- **API facts:**
  - Profile `force_reanalyze` is allowed for owners; job
    `force_reanalyze` is admin-only (403).
  - The profile-level success cooldown is 300 s
    (`PROFILE_ANALYSIS_PROFILE_SUCCESS_COOLDOWN_SECONDS`).
  - The saved-match list already contains the full `match` result.

## Development environment

- Tests use throwaway SQLite databases and temporary directories. None of
  them imports `backend.app.main`.
  - Backend: `.venv/Scripts/python.exe -m unittest discover backend/tests`
  - Hook: `.venv/Scripts/python.exe -m unittest discover -s .claude/hooks -p "test_*.py"`
- Runtime configuration (`backend/app/config.py`, `backend/app/openai_client.py`).
  Every default is the previous behaviour.

  | Variable | Default | Effect |
  |---|---|---|
  | `APPLY101_DATABASE_URL` | `sqlite:///./apply101.db` | SQLAlchemy URL. Migrations and scripts read it too, but only from the process environment (only `main.py` loads `.env`). SQLite URI URLs (a `uri` query parameter or a `file:` database) are refused with `ConfigError` before any engine exists; use a plain `sqlite:///<path>` URL. |
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

### 2026-10-22: W4 web client
- **Branch:** `feat/online-mvp-web-client`, opened from `main` at `c56c1df`.
  - Node.js v24.22.1 LTS is installed per user (official zip, SHA-256
    checked).
- **Backend:** additive changes only (`26bc320`):
  - `APPLY101_CORS_ORIGINS`, with an explicit origin list and Retry-After
    exposed;
  - `GET /jobs/?owned=true`, `GET /jobs/{job_id}`,
    `GET /users/{user_id}/languages`, `GET /meta`.
  - Backend 833 OK.
- **Frontend (`frontend/`):** React 19, React Router 7, Vite 7 and
  TypeScript 5.9, with a lockfile.
  - Screens: login/register, overview, profile (matching fields,
    languages, PDF CV, analysis), jobs (list, add, detail with analysis and
    match), saved matches (list, reopen, rematch).
  - The demo banner comes from `GET /meta`.
- **Checks:**
  - `npm run build` and the typecheck pass; Vitest 21 OK.
  - Playwright (installed Edge, isolated servers): 5 OK, covering the main
    flow, two-user isolation, 429 without automatic retry, a mocked 503,
    keyboard login, logout and expiry, and mobile without horizontal
    overflow.
  - A manual pass in the built-in browser found two bugs, both fixed: the
    nested profile-analysis response, and locale-dependent dates.
- **Next:** reviewer pass, then fixes, push and a draft PR.

### 2026-10-22: SQLite URI URLs refused
- **Gap:** `sqlite:///file:apply101.db?uri=true` passed `database_is_default()`,
  so it was not recognized as the default database, and the fake provider
  turned on. Yet SQLAlchemy's pysqlite driver opens that URL in URI mode,
  which reaches the same `apply101.db` file.
- **Fix:** `config.database_url()` refuses a SQLite URL with a `uri` query
  parameter or a `file:` database, raising `ConfigError`. There is no URI
  parser.
  - `database.py` calls it before `create_engine`, so no engine or
    connection exists.
  - `check_data_locations()` calls it first at startup.
  - Plain SQLite paths, `:memory:` and server URLs are unchanged.
- **Tests:** regression tests with a fake repository root, a temp working
  directory and temp database paths. They cover:
  - refusal of each URI spelling by `database_url()`,
    `database_is_default()` and `check_data_locations()`;
  - plain URLs still accepted;
  - `import backend.app.database` failing with `ConfigError` and creating
    nothing;
  - fake mode refused with URI URLs.
- **Test results:**
  - Backend 797 OK (4 new).
  - Hook 25 tests (781 subtests) OK.
  - The real `apply101.db`, `.env` and `uploads/` kept the same size and
    mtime.
- **Startup checks:**
  - `import backend.app.main` with a URI URL pointing to a temp file failed
    with `ConfigError` before any file existed.
  - The documented demo command still starts, and the end-to-end demo gave
    the same results (eligible 74, ineligible 20).

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
    - SQLite URI URLs such as `sqlite:///file:apply101.db?uri=true` are
      refused (see the next log entry).
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
