# Apply101 web client

A React + TypeScript + Vite single-page app for the Apply101 FastAPI backend
(decision D1 in `docs/ONLINE_MVP_PLAN.md`). The UI is in English.

Flow:
1. register and log in;
2. set up a profile with the matching fields and languages, and upload a
   PDF CV;
3. add a job manually;
4. analyze the profile and the job;
5. get a reasoned match (`eligible` / `ineligible` / `review_required`,
   scores, strengths, weaknesses, reasons);
6. reopen saved matches and re-run them;
7. browse the shared job pool (search, country, work mode and source
   filters, the source link and when each job was last seen);
8. see "Jobs for me": analyzed open pool jobs ranked against the analyzed
   profile with the existing matching rules, together with the share of the
   pool that was ranked;
9. import a job from a link (Greenhouse, Lever, Ashby, or a page with
   JobPosting JSON-LD), check the fields and save it as a personal job.
   Unsupported pages fall back to pasting the text;
10. write a motivation letter draft (English, German or Turkish) on a job
    page.
    - The draft comes from the user's profile, CV, the posting and sourced
      company information.
    - The page shows the facts used and what is missing.
    - The user can edit, save new versions, copy, download a .txt file and
      regenerate; an edited version is never overwritten.
    - With the fake provider the draft is a fixed template.

## Requirements

- Node.js 20.19 or newer and npm. Tested with 24.22.1 LTS on Windows and
  22.22.0 on Linux.
- The backend from the repository root, with its Python venv in `.venv/`.

### Linux setup

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
(cd frontend && npm ci)
```

`npm ci` installs exactly what `package-lock.json` lists. The venv and
`node_modules/` are gitignored.

## Settings

The web client has one setting. It is read at build time, and its value ends
up in the public bundle, so never put secrets into any `VITE_*` variable.

| Variable | Example | Default |
|---|---|---|
| `VITE_API_BASE_URL` | `http://localhost:8001` | `http://localhost:8001` |

The backend must allow the web client's origin explicitly. `*` is refused:

| Backend variable | Example |
|---|---|
| `APPLY101_CORS_ORIGINS` | `http://localhost:5173` (dev server), `http://localhost:4173` (preview), or a comma-separated list |

Pass these settings on the command line, as below. There are no `.env`
files for the web client in the repository.

## Run the demo locally

Start the backend in the isolated demo setup from the repository root:
- the database and CVs go to a temp directory;
- `.env` is not loaded and the JWT secret is throwaway;
- analyses come from the deterministic fake provider, so no OpenAI calls
  are made.

The command below is for Git Bash. On Linux or macOS, drop `cygpath -m` and
use `.venv/bin/python`.

```bash
SANDBOX="$(cygpath -m "$(mktemp -d)")"
APPLY101_LOAD_DOTENV=0 \
APPLY101_DATABASE_URL="sqlite:///$SANDBOX/demo.db" \
APPLY101_UPLOAD_DIR="$SANDBOX/uploads" \
APPLY101_ANALYSIS_PROVIDER=fake \
APPLY101_CORS_ORIGINS=http://localhost:5173 \
JWT_SECRET_KEY="$(.venv/Scripts/python.exe -c 'import secrets; print(secrets.token_urlsafe(48))')" \
OPENAI_API_KEY= \
.venv/Scripts/python.exe -m uvicorn backend.app.main:app --port 8001
```

Then, in `frontend/`:

```bash
npm ci
VITE_API_BASE_URL=http://localhost:8001 npm run dev
```

Open http://localhost:5173 and use fake data only. A banner says when the
backend runs the fake analysis provider (`GET /meta`).

The backend's protections are active in the demo, for example the per-user
job analysis cooldown (60 s by default). A 429 shows a countdown, and the
web client never re-sends an analysis by itself.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server on port 5173 |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run build` | Typecheck, then a production build into `dist/` |
| `npm run preview` | Serves `dist/` on port 4173 |
| `npm test` | Unit and flow tests (Vitest, Testing Library, jsdom) |
| `npm run e2e` | End-to-end tests in the installed Microsoft Edge by default (Playwright, no browser download) |

`npm run e2e` starts its own servers on ports 8011 and 4173:
- an isolated backend: a new temp directory, `.env` off, a random JWT
  secret, the fake provider, an explicit CORS origin and a 30 s analysis
  cooldown;
- the production build.

It then runs the whole flow with two users, desktop and mobile viewports,
and the error states.

- `e2e/discovery.spec.ts` covers the pool, "Jobs for me" and URL import.
  It first runs `e2e/seed_catalog.py`, which adds synthetic catalog jobs
  and an admin user to the sandbox database through
  `backend/app/catalog_sync.py`. The script refuses any database but the
  sandbox, and any provider but the fake one.
- Then the admin analyzes the pool with the bounded
  `POST /jobs/analyze-missing`.
- `e2e/sandbox.ts` holds the sandbox's data settings, shared by the config
  and the seeding. Screenshots are written to `e2e/screenshots/`
(gitignored). Set `APPLY101_PYTHON` to use another Python interpreter for
the backend.

The browser is chosen with two variables:

| Variable | Values | Default |
|---|---|---|
| `APPLY101_E2E_BROWSER` | `msedge` (installed Microsoft Edge) or `chromium` | `msedge` |
| `APPLY101_E2E_CHROMIUM_PATH` | path to an installed Chromium binary; only with `chromium` | Playwright's own Chromium |

Without `APPLY101_E2E_CHROMIUM_PATH`, `chromium` needs the browser build
that matches `@playwright/test` (`npx playwright install chromium`). On a
machine with a preinstalled Chromium, point at it instead, for example in a
Claude Code cloud session:

```bash
APPLY101_E2E_BROWSER=chromium \
APPLY101_E2E_CHROMIUM_PATH=/opt/pw-browsers/chromium \
npm run e2e
```

That Chromium (153) is older than the build `@playwright/test` 1.64.1
expects (156), and all e2e tests passed with it on 2026-10-25.

## Notes

- **Session:** the session is a bearer JWT kept in `localStorage`, so it
  survives a reload. The app logs out when the token's `exp` passes or the
  backend answers 401. The backend stays the only authority on validity.
- **Concurrency:** each user action runs once at a time, and repeated
  clicks are ignored.
- **Error handling:**
  - 401, 404, 409, 422, 429 and 503 are shown as readable messages;
  - 422 field errors appear next to the fields;
  - 429 honours `Retry-After`.
- **Profile:** the web client manages one profile per user (the first one).
