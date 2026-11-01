# Lightsail test environment runbook

A private test deployment of Apply101 on one AWS Lightsail instance. You run
every command yourself, for example in Lightsail's browser SSH on a phone.
The Claude cloud session that prepared this has no SSH access to the server.

It reuses the image and the safety checks of the Railway setup
(`backend/app/deploy_app.py`, `Dockerfile`; see
`docs/RAILWAY_TEST_RUNBOOK.md` for what they enforce):
- fake analysis provider, no OpenAI key, `.env` loading off;
- the access key in front of every page and API call;
- the daily catalog sync off by default.

Lightsail and AWS console steps are marked **(check)**: AWS documentation
could not be read from the environment this was written in.

## What runs

- **`app`:** the image from `Dockerfile`, with one worker. It has no
  published port and is reachable only through Caddy.
- **`caddy` (2.10.2):** HTTPS on ports 80 and 443, with an automatic
  Let's Encrypt certificate for your domain, HSTS and a redirect from HTTP.
  - The app trusts `X-Forwarded-For` only from Caddy's fixed address.
  - Caddy ignores that header from clients, so each tester has their own
    rate limits and nobody can spoof an address.
- **Data on the host, outside the checkout:**

| Host path | What |
|---|---|
| `/srv/apply101/app.env` | secrets and sync settings (mode 600, generated on the server) |
| `/srv/apply101/data/apply101-test.db` | the database |
| `/srv/apply101/data/uploads/` | CVs |
| `/srv/apply101/data/backups/` | backups |
| `/srv/apply101/caddy/` | certificates |

`deploy/lightsail/ctl.sh` wraps every operation; `sudo deploy/lightsail/ctl.sh`
alone lists them.

## 1. Instance, IP and firewall (check)

1. **Instance:** an Ubuntu 24.04 LTS instance with at least 1 GB of memory.
   The image is built on the server, and building the web client needs the
   memory. On a smaller plan, add swap (step 2).
2. **Static IP:** create one and attach it to the instance.
3. **Domain:** a DNS name pointing to the static IP, as an A record, for
   example `apply101-test.example.com`.
   - Without a domain, `<ip with dashes>.sslip.io` (for example
     `203-0-113-10.sslip.io`) resolves to the IP. Let's Encrypt can issue a
     certificate for it, but the shared domain has rate limits.
4. **Networking tab (IPv4, and IPv6 if enabled):**
   - **SSH 22:** only your own IP, plus the option that allows Lightsail's
     browser SSH. Never "Any IPv4 address".
   - **HTTP 80 and HTTPS 443:** open. Let's Encrypt has to reach them to
     issue and renew the certificate. The access key protects the app.
     - To narrow 443 to known IPs later, keep 80 open for renewals.
     - Phone IPs change often.

## 2. Server preparation (browser SSH, user `ubuntu`)

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
docker compose version
```

Only if the instance has less than 2 GB of memory, add 2 GB of swap:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Install

```bash
cd ~
git clone --branch feat/lightsail-test-env https://github.com/UBADKK/Apply101.git
cd Apply101
sudo deploy/lightsail/ctl.sh setup apply101-test.example.com
```

- **What `setup` does:**
  - creates `/srv/apply101`;
  - generates `JWT_SECRET_KEY` and `APPLY101_ACCESS_KEY` into
    `/srv/apply101/app.env`;
  - builds the image, which takes several minutes the first time;
  - starts both containers.
- **Running it again** keeps the existing secrets.

Then:

```bash
sudo deploy/lightsail/ctl.sh status       # app should become "healthy"
sudo deploy/lightsail/ctl.sh access-key   # copy it into your password manager
sudo deploy/lightsail/ctl.sh logs 50      # Caddy reports the certificate here
```

1. Open `https://<your domain>` on the phone, enter the access key and
   register.
2. Make yourself admin:

```bash
sudo deploy/lightsail/ctl.sh admin you@example.com
```

## 4. Access

- Share the access key only with testers.
- **Revoking access:** edit `APPLY101_ACCESS_KEY` in `/srv/apply101/app.env`
  (`sudo nano`), then run `sudo deploy/lightsail/ctl.sh up`. Every cookie
  becomes invalid.
- **Logging everyone out:** changing `JWT_SECRET_KEY` the same way does that.

## 5. Catalog: first sync

```bash
sudo deploy/lightsail/ctl.sh catalog seed --yes
sudo deploy/lightsail/ctl.sh catalog check --unverified --max-boards 10 --yes
```

- **Enabling boards:** do it only after reading the source's terms, with
  the admin API call in `docs/RAILWAY_TEST_RUNBOOK.md`, section 5.
  - The steps are the same, with your domain as `BASE`.
  - That call runs from a computer: `curl` and a cookie file.
- **Then, a small first sync:**

```bash
sudo deploy/lightsail/ctl.sh catalog sync --max-boards 2 --max-pages 2 --yes
sudo deploy/lightsail/ctl.sh catalog summary
```

- **The daily sync:** to turn it on later, set
  `APPLY101_CATALOG_SYNC_ENABLED=1` in `app.env`, then run `ctl.sh up`.
  Its rules (once a day, bounded, enabled boards only) are in the Railway
  runbook, section 5.

## 6. Backup and restore

```bash
sudo deploy/lightsail/ctl.sh backup          # keeps the newest 7
sudo deploy/lightsail/ctl.sh backups
sudo deploy/lightsail/ctl.sh restore apply101-20261102T030000Z   # restarts the app
```

- **A daily backup at 02:17 UTC:** add it with `sudo crontab -e`:

```
17 2 * * * cd /home/ubuntu/Apply101 && deploy/lightsail/ctl.sh backup >> /srv/apply101/backup.log 2>&1
```

- **Off-instance copies:** these backups stay on the same disk. Also enable
  Lightsail's automatic instance snapshots, or take a manual snapshot
  before updates (check).
- **Restore:**
  - It first backs up the current state.
  - It moves the current CVs aside to `uploads.before-restore-<time>`.
  - It puts the old state back if a step fails.

## 7. Updates

```bash
cd ~/Apply101
sudo deploy/lightsail/ctl.sh backup
sudo deploy/lightsail/ctl.sh update      # git pull --ff-only, rebuild, restart
```

- **Data:** it stays in `/srv/apply101`.
- **New tables:** created on start.
- **Changed columns:** they need the matching script from
  `backend/migrations/`. Run it with `docker compose exec`, after a backup.

## 8. Cost (check)

- **Billing:** a Lightsail plan has a fixed monthly price for the
  instance, its disk and a data allowance.
  - Static IPs are free while attached.
  - Snapshots cost extra per GB.
  - Traffic beyond the allowance costs extra.
- **No other spending:** the fake provider makes no OpenAI calls.
- **Stopping costs:**
  - A stopped instance is still billed in Lightsail; delete it to stop
    paying.
  - Back up and copy data off first.
  - Release the static IP after deleting the instance.
- **Alerts:** set an AWS Budgets alert on the account.

## 9. Stopping or removing

```bash
sudo deploy/lightsail/ctl.sh down        # stops both containers; data stays
sudo rm -rf /srv/apply101                # deletes ALL data, secrets and certificates
```

## How this was checked

- **Run in the cloud session**, on a temporary root instead of
  `/srv/apply101`, with domain `localhost` on alternate ports, and an image
  built from the same Dockerfile:
  - `ctl.sh setup` with mode-600 secrets, a refused bad domain, and secrets
    kept on a rerun;
  - HTTPS checked against Caddy's local CA, the HTTP→HTTPS redirect and
    HSTS;
  - the gate: 401 without the key, then a Secure, HttpOnly, SameSite=strict
    cookie;
  - the API and a deep link;
  - a spoofed `X-Forwarded-For` does not reset the limit, and the app sees
    the client's address, not Caddy's;
  - registration, `admin`, `catalog seed` and `summary`, and a backup and
    restore that removed a user added after the backup;
  - data kept across `down` and `up`, and the app healthy.
- **Not checked there:**
  - a real Let's Encrypt certificate, which needs a public domain;
  - the Lightsail console and firewall steps;
  - `ctl.sh update` against GitHub;
  - the apt package names on a real Lightsail image.
