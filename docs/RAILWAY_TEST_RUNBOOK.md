# Railway test environment runbook

A private test deployment of Apply101 on Railway: one service, one volume,
the fake analysis provider and an access key in front of everything. It is
not a production setup.

Railway's own documentation and pricing pages could not be read from the
environment this was written in. Steps marked **(check)** follow Railway's
documented model as known, and must be checked against the Railway
dashboard and docs before relying on them.

## What runs

- **Image:** `Dockerfile`, a single image built by Railway (`railway.json`).
  - It builds the web client with `VITE_API_BASE_URL=/api` and installs
    `requirements.txt`.
  - `.dockerignore` keeps `.env`, `*.db`, `uploads/`, backups, tests and
    `node_modules` out of the image.
- **Process:** `uvicorn --factory backend.app.deploy_app:create_app`, with
  one worker.
  - `/` serves the web client; client routes such as `/catalog` load the app
    shell.
  - `/api/...` serves the API (`backend.app.main`).
  - `/healthz` is the health check and the only path open without the access
    key.
- **Start checks:** the service refuses to start unless all of these hold
  (`check_deploy_environment`):
  - `.env` loading is off;
  - the database and the uploads are inside `APPLY101_DATA_DIR`, and that
    directory is inside the Railway volume when one is mounted;
  - the analysis provider is `fake` and no OpenAI key is set;
  - `JWT_SECRET_KEY` and `APPLY101_ACCESS_KEY` are set.
- **Daily catalog sync:** a thread in the same process, so it uses the same
  disk. It is off by default; see "Catalog sync" below.

## 1. Create the service (check)

1. Create a Railway project, and in it one service from the GitHub
   repository and this branch.
   - Railway reads `railway.json`: Dockerfile build, one replica, health
     check `/healthz`, restart on failure.
2. Attach a volume to the service, mounted at `/data`.
   - Railway sets `RAILWAY_VOLUME_MOUNT_PATH` for it. The start check
     requires `APPLY101_DATA_DIR` to be inside it.
   - Keep the service at one replica: SQLite lives on that single volume.
3. Set the variables below on the service. Secrets are entered only in
   Railway's variables screen; nothing goes into the repository.

| Variable | Value |
|---|---|
| `APPLY101_DATA_DIR` | `/data` |
| `APPLY101_DATABASE_URL` | `sqlite:////data/apply101-test.db` (four slashes: absolute path) |
| `APPLY101_UPLOAD_DIR` | `/data/uploads` |
| `JWT_SECRET_KEY` | random, at least 32 bytes: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `APPLY101_ACCESS_KEY` | random, at least 24 characters: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

The image already sets `APPLY101_LOAD_DOTENV=0`,
`APPLY101_ANALYSIS_PROVIDER=fake` and `APPLY101_WEB_DIR`. Do not set
`OPENAI_API_KEY`, and do not set `APPLY101_CORS_ORIGINS`: the browser talks
to one origin.

## 2. Access restriction

- **No public domain while setting up.** The service needs a domain only
  when someone opens it in a browser. Generate one when needed, and remove
  it when testing pauses (check).
- **Access key:** every page and API call needs it.
  - The form at `/__access` sets an HttpOnly, Secure, SameSite=Strict
    cookie valid for 30 days. The cookie holds an HMAC of the key, not the
    key itself.
  - Ten wrong keys from one address within 15 minutes are refused for a
    while. The counter is per process and restarts with it.
  - Pages carry `noindex`.
- **Share the key only with testers.** To revoke access, change
  `APPLY101_ACCESS_KEY`: the redeploy invalidates every cookie.
- **App accounts:** registration stays open behind the key; testers
  register themselves. Changing `JWT_SECRET_KEY` logs everyone out.
- **Admin:** make one tester admin from a shell in the service (step 4).

## 3. First start

1. Deploy (check: a push to the branch, or "Deploy" in the dashboard).
2. Look at the deploy log.
   - It shows `Uvicorn running`, or the full list of start-check problems.
   - On first start the database file is created on the volume with every
     table (`Base.metadata.create_all`).
3. Open the domain, enter the access key, register, and check that the
   overview says "Demo mode".

## 4. Shell commands in the service (check: `railway ssh`)

The scripts read the same variables, so they work on the volume's database:

```bash
python -m backend.scripts.set_admin --email you@example.com
python -m backend.scripts.sync_catalog summary
python -m backend.scripts.backup_data list
```

## 5. Catalog sync

The pool is filled only from boards an admin enabled.
- Seeded boards start disabled.
- Enabling one needs `acknowledge_terms` (see
  `docs/ONLINE_MVP_PROGRESS.md`, "terms of Greenhouse, Lever and Ashby").
- Nothing in this setup sets an acknowledgement.

### First sync, by hand

In the service shell:

```bash
python -m backend.scripts.sync_catalog seed --yes
python -m backend.scripts.sync_catalog check --unverified --max-boards 10 --yes
```

Enable chosen boards as the admin, from your own machine, after reading
the source's terms. The web client has no admin screen; the API sits
behind the access key:

```bash
BASE=https://<your domain>
curl -s -c jar -d "key=$APPLY101_ACCESS_KEY" "$BASE/__access" -o /dev/null
TOKEN=$(curl -s -b jar -d "username=you@example.com" -d "password=..." "$BASE/api/auth/login" \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
curl -s -b jar -H "Authorization: Bearer $TOKEN" "$BASE/api/admin/catalog/boards"
curl -s -b jar -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X PATCH -d '{"is_enabled": true, "acknowledge_terms": true}' "$BASE/api/admin/catalog/boards/<id>"
```

Then, in the service shell again:

```bash
python -m backend.scripts.sync_catalog sync --max-boards 2 --max-pages 2 --yes
python -m backend.scripts.sync_catalog summary
```

Arbeitnow is synced only by hand, with `--include-arbeitnow` and
`--acknowledge-arbeitnow-terms`. The daily sync never includes it.

### Daily sync

| Variable | Default | Meaning |
|---|---|---|
| `APPLY101_CATALOG_SYNC_ENABLED` | off | `1` turns the daily sync on |
| `APPLY101_CATALOG_SYNC_HOUR_UTC` | `3` | earliest UTC hour of the day's run |
| `APPLY101_CATALOG_SYNC_MAX_BOARDS` | `10` | boards per run (at most 50) |
| `APPLY101_CATALOG_SYNC_MAX_PAGES` | `10` | pages per board (at most 50) |

- **One run a day:** the thread checks every 5 minutes and runs once per
  UTC day after the hour.
  - The day is claimed first in the database, so a restart, a redeploy or a
    second process never runs it twice.
  - A run that fails or is cut off by a restart is not retried that day.
  - A manual sync and the daily one never overlap: they share the catalog
    sync lease.
- **Each run:**
  - takes the least recently synced enabled boards first;
  - stops at the 15-minute sync budget;
  - closes jobs only after a complete fetch.
- **Log:** the deploy log shows `Daily catalog sync on: ...` at start and
  `Daily catalog sync finished: ...` after each run.

## 6. Backup and restore

```bash
python -m backend.scripts.backup_data backup --yes        # keeps the newest 7
python -m backend.scripts.backup_data list
python -m backend.scripts.backup_data restore --from apply101-20261102T030000Z --yes
```

- **Where backups go:** `/data/backups/apply101-<UTC time>/`.
  - `database.db` is made with SQLite's online backup API, so the service
    may keep running.
  - `uploads.tar.gz` holds the CVs.
- **They stay on the same volume:** they protect against mistakes, not
  against losing the volume.
  - Copy important ones off the volume (check: `railway ssh` with `cat` or
    a download), or use Railway's volume backups if your plan has them
    (check).
- **Restore:**
  - It first backs up the current state.
  - It moves the current uploads aside to
    `uploads.before-restore-<time>`.
  - Then it copies the backup in.
  - Run it while nobody uses the service, then redeploy.

## 7. Cost tracking (check)

- The service, its volume and its traffic are billed by Railway. The fake
  provider makes no OpenAI calls.
- Before the first deploy:
  - set a usage limit with an email alert, and a hard limit, in the
    workspace's usage or billing settings;
  - note the plan's included usage.
- Check the usage page weekly. CPU and memory stay low while idle; the
  daily sync adds a short burst.
- To pause costs, remove the deployment or the service.
  - The volume keeps data, and may be billed while it exists.
  - Deleting the volume deletes the data. Back it up and copy it off first.

## 8. Updates

- **A push to the branch redeploys** (check). The volume keeps the
  database.
- **New tables:** `create_all` adds them on start.
- **Changed columns:** they need the matching script from
  `backend/migrations/`, run in the service shell before or right after the
  deploy. Back up first.

## 9. Checking locally (no Railway account needed)

```bash
.venv/bin/python -m unittest backend.tests.test_deploy_app backend.tests.test_catalog_scheduler backend.tests.test_backup_data
(cd frontend && npm run e2e:deploy)    # build with /api, start deploy_app, go through the gate
docker build -t apply101-railway-test .
docker run --rm -p 8080:8000 -v "$(mktemp -d)":/data -e PORT=8000 \
  -e APPLY101_DATA_DIR=/data -e APPLY101_DATABASE_URL=sqlite:////data/apply101-test.db \
  -e APPLY101_UPLOAD_DIR=/data/uploads -e JWT_SECRET_KEY=<random> -e APPLY101_ACCESS_KEY=<random> \
  apply101-railway-test
```

`npm run e2e:deploy` takes the same browser settings as `npm run e2e` (see
`frontend/README.md`).

Behind an HTTPS-intercepting proxy, the build has to trust the proxy's CA.
Pass it as a build secret in a local copy of the Dockerfile; never add it to
the repository's Dockerfile.
