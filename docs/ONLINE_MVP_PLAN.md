# Apply101 Online MVP Plan

Scope and acceptance criteria for the first online release. Progress is
tracked in [ONLINE_MVP_PROGRESS.md](ONLINE_MVP_PROGRESS.md).

Sources of truth still apply in the order given in `CLAUDE.md`: tests, then
code, then `backend/app/analysis_contract.py`, then
`backend/MATCHING_CONTRACT.md`. This plan does not change matching semantics
or contract versions.

## Goal

A normal (non-admin) user can use the deployed application to go from sign-up
to a reasoned match result for a job they add themselves:

1. register / log in
2. create a profile and upload a PDF CV
3. add a job manually with its text and URL
4. analyze the profile and the job
5. get a match result with its reasons

## Current backend baseline

Verified on `main` at `e8208d8` (2026-10-22) by reading the code and by a
smoke run of `backend.app.main` in a throwaway sandbox with fake data,
`.env` loading disabled and no OpenAI key.

| Step | Existing endpoint(s) | Smoke run |
|---|---|---|
| 1 | `POST /auth/register`, `POST /auth/login` (OAuth2 form, JWT signed with `JWT_SECRET_KEY`), `GET /auth/me`; Argon2 hashing; in-process rate limiting | 201 / 200 / 200 |
| 2 | `POST /users/{user_id}/profiles`, `PATCH .../profiles/{profile_id}`, `POST .../profiles/{profile_id}/upload-cv` (PDF only, 10 MiB, text extracted with pypdf, file stored as `uploads/user_{id}_profile_{id}_cv.pdf`) | 200 / 200 |
| 3 | `POST /jobs/manual` (`title`, `url`, `description_text` required; per-owner URL uniqueness; the URL is stored, not fetched) | 201 |
| 4 | `POST /users/{user_id}/profiles/{profile_id}/analyze` (in-flight guard and cooldown); `POST /jobs/{job_id}/analyze` (own manual jobs only, per-user guard, 10 analyses per rolling 24 h, provider spend/credit limit mapped to 503) | 503 "Analysis service is not configured." without a key |
| 5 | `POST /users/{user_id}/profiles/{profile_id}/jobs/{job_id}/match`, `GET /users/{user_id}/profiles/{profile_id}/matches`; result carries `eligibility_status` (`eligible` / `ineligible` / `review_required`), scores and reason codes | 400 `ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING` until analyzed |

The backend already covers steps 1-5. At this baseline the repository had
no web client and no deployment configuration. W4 adds the web client in
`frontend/`; see the progress file.

## In scope

- The five steps above for a normal user, through a minimal web client
  (technology still to decide, see D1) talking to the existing API.
- One hosted environment, staging first, served over HTTPS, with persistent
  storage for the database and the CV files.
- Configuration only through environment variables in the hosted
  environment. No `.env` file is loaded there, and no secrets live in the
  repository.
- Cost control for every analysis a user can trigger.
- Account deletion that also removes the user's CV files.

## Out of scope for the first release

The scope extension below (2026-10-25) replaces three earlier lines of this
list. They read: "Fetching or scraping job text from the URL", "Catalog /
job-board fetching for normal users" and "Batch matching UI, recommendations
across the catalog".

- General scraping. URL import is limited to supported ATS APIs and to
  JobPosting JSON-LD on pages that robots.txt allows. No login or CAPTCHA
  bypass, and no scraper for LinkedIn or similar sites.
- Catalog sync for normal users. Syncing stays admin-only. Normal users only
  browse the synced catalog.
- Changes to analysis prompts, eligibility rules, scoring or contract
  versions.
- Payments, mobile apps, multi-language UI.
- Password reset and email verification, unless D5 decides otherwise.

## Acceptance criteria

Every criterion must be verified with fake data only. A criterion counts as
done when an automated test covers it, or, for a deployment criterion, when
a recorded check on staging does.

### AC1 Registration and login
- Registering with name, email and a password of the minimum length returns
  201. A duplicate email returns 409. A short password is rejected.
- Login returns a bearer token, and `GET /auth/me` works with it. A wrong
  password and an unknown email return the same 401.
- Repeated attempts are rate limited (429). The limit also holds with the
  number of server processes used in production.

### AC2 Profile and PDF CV
- A user can create and edit their own profile.
- A PDF of 10 MiB or less uploads and its text is stored.
- A non-PDF, an oversized or an unreadable file is rejected, and no file is
  left behind.
- Another user gets 404 for a profile and its CV that are not theirs.
- CV files survive a restart and a redeploy, and their stored paths work on
  the hosting OS.

### AC3 Manual job (text and URL)
- A user can add a job with a title, a valid URL and description text.
- The same URL twice for the same owner returns 409.
- Only the owner, and admins, can see the job. Other users get the same 404
  as for a job that doesn't exist.

### AC4 Analysis
- A user can analyze their own profile and their own manual job.
- Stored results carry the versions currently defined in
  `analysis_contract.py`.
- Repeated and concurrent requests respect the existing guards (409 / 429).
- The per-user job quota is enforced, and profile analysis has a per-user
  limit as well (see W3 and D3).
- If the provider is unavailable, out of credit or not configured, the
  response is 503 and no partial analysis is written.

### AC5 Reasoned match result
- For an analyzed profile and job, the result shows `eligibility_status`,
  the overall score, the machine-readable reason codes and readable
  reasons.
- Unknown information leads to `review_required`, not to a hard rejection,
  as `MATCHING_CONTRACT.md` specifies.
- `match_version` equals `MATCH_VERSION`. A repeated request returns the
  stored result, and `force_rematch=true` recomputes it.

### AC6 End to end and operations
- On staging, a new fake user completes steps 1-5 through the web client.
- Secrets exist only in the hosting environment, and the logs contain no
  passwords, tokens, CV text or API keys.
- Migrations phase2-phase8 are applied to the online database, and a backup
  restore has been tested once.
- Deleting an account removes the user's profiles, CV files, owned jobs and
  matches.
- All automated tests pass, both backend tests and hook tests.

## Known gaps (work items)

- **W1 Configuration.**
  - `DATABASE_URL` is hard-coded to `sqlite:///./apply101.db` and the CV
    directory to `uploads`. Both are relative to the working directory.
  - `main.py` calls `load_dotenv()`, which loads the repository `.env`.
  - Needed: make both paths configurable, keep the current local defaults,
    and make `.env` loading explicit.
- **W2 Analysis without cost in development.** Local development and
  end-to-end tests need a fake analysis provider that is never active in
  production. Without one, steps 4-5 need a real OpenAI key.
- **W3 Profile analysis cost limit.** Only job analysis has a per-user
  rolling quota. Profile analysis only has an in-flight guard and a
  cooldown.
- **W4 Web client and CORS.** The repository has no frontend, and
  `main.py` adds no CORS middleware.
- **W5 Hosting and database.** SQLite on one persistent volume with one
  process, or a server database (see D2). The auth rate limiter is
  in-process, so its limits apply per process.
- **W6 Stored CV paths.** `cv_file_path` is saved as a relative OS path, and
  on Windows that means `uploads\user_1_profile_1_cv.pdf`. The path has to
  resolve on the hosting OS and with a configurable upload directory.
- **W7 Account deletion.** `DELETE /users/{user_id}` deletes only database
  rows; it does not remove CV files from disk. The only file removals in
  the backend are inside the CV upload flow.
- **W8 Operations.** HTTPS, secret management, backups, log hygiene,
  health check (`GET /` exists).
- **W9 Documentation.** In `backend/MATCHING_CONTRACT.md`, the "Current
  versions" list still says `job_analysis_v8` / `backend_match_v3`, while
  `analysis_contract.py` and the same document further down say
  `job_analysis_v13` / `backend_match_v6`.

## Scope extension (2026-10-25)

Given by the project owner. It widens the manual-only MVP above, and the
remaining release work (W3, W5-W9, AC6, D2-D6) stays. Hosting and deployment
are not part of it.

### Phase 1: job and company pool
- **Connectors:** keep Arbeitnow, and add Greenhouse Job Board, Lever
  Postings (global and EU) and Ashby public job postings behind one
  interface.
  - Check the official documentation and terms of use.
  - Company board ids are configurable. No API that lists companies
    globally is assumed.
- **Company list:** verified companies with a focus on Turkey, Germany and
  remote work.
  - The target is 30 boards; missing ones are not made up.
  - Report counts per source, country and active jobs.
  - Record source, attribution and reuse terms. Unclear sources are not
    published automatically.
- **Sync:** an admin-only, bounded sync command with run records.
  - It handles pagination, timeouts and 429 with backoff, creates no
    duplicates when run again, and applies content updates.
  - A failed or partial run never closes jobs.
  - A content change that affects analysis means old results are no longer
    shown as current.
  - Exact copies across sources are linked; similar titles are not merged
    blindly.
- **Companies:** a separate record, with jobs and sources linked to it, so
  it can later feed company research. CVs, user notes and personal jobs
  never reach the shared company pool.

### Phase 2: discovery and easy adding
- **Screens:** a shared pool and a "Jobs for me" screen.
  - Search, plus filters for country or location, remote or hybrid, and
    source.
  - Pagination, the source link and how recent each job is.
- **Ranking:** analyzed, active jobs are ranked with reasons under the
  existing matching rules.
  - The coverage that was computed is shown, and the first page is never
    presented as the best of the whole pool.
  - Unknown information is no reason to reject. Remote does not mean every
    country can work there.
  - Opening the pool starts no paid analysis. Admins can start catalog
    analysis in bounded batches with the existing cache and guards.
- **URL import:**
  - Sources: supported ATS links, and JobPosting JSON-LD on pages that allow
    access.
  - Flow: preview, correction and confirmation, then the job is saved as the
    user's personal job.
  - Unsupported, blocked or incomplete pages fall back to pasting the text;
    nothing is made up.
  - Protections against SSRF, DNS and redirect tricks, size and time
    limits, and harmful HTML.

### Phase 3: motivation letter
- **Flow:** a TR, EN or DE draft from the user's own profile and CV, the job
  and sourced company information. The user can edit, save, copy and
  download it as TXT.
- **Grounding:** missing experience, achievements or company facts are never
  made up. The facts used and the gaps are shown to the user.
- **Versions:** regenerating never overwrites an edited draft.
- **Rules:** user isolation, version records, a quota and error handling.
- **Providers:** an implementation for the real analysis provider and its
  deterministic fake. No paid call is made. A fake-provider check is never
  reported as a check of the real model's writing quality.

### Delivery
- **Data and tests:**
  - Temporary databases and upload directories, and the fake AI provider.
  - Automated tests use fixtures and run without internet.
  - Migrations are tried on synthetic databases.
  - Limited live GETs against public sources only where the environment
    allows them.
- **Process:** an auditor, developer, reviewer loop, with one commit per
  phase.
- **Checks:** backend, hook, frontend typecheck, build and Vitest, and
  Chromium E2E.
- **Delivery:** a normal push and a draft PR. No merge, deploy, force push
  or branch deletion.

## Open decisions (owner: project owner)

- **D1** Web client approach. Decided 2026-10-22: a React + TypeScript +
  Vite single-page app in `frontend/`, using the existing FastAPI backend.
- **D2** Hosting provider and database engine for the first release.
- **D3** OpenAI budget: the model, and the per-user limits for profile and
  job analysis.
- **D4** Registration policy for the first release: open sign-up, or an
  invite / allow-list. This also caps the analysis cost.
- **D5** Whether password reset and email verification are part of the
  first release.
- **D6** Privacy: retention of CVs and analyses, and the privacy notice.
