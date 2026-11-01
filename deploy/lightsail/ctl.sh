#!/usr/bin/env bash
# Operator commands for the Lightsail test deployment (see
# docs/LIGHTSAIL_TEST_RUNBOOK.md). Run from the repository checkout, as root:
#
#   sudo deploy/lightsail/ctl.sh setup <domain>   first install: dirs, secrets, start
#   sudo deploy/lightsail/ctl.sh up               build and (re)start
#   sudo deploy/lightsail/ctl.sh update           git pull --ff-only, then up
#   sudo deploy/lightsail/ctl.sh status | logs [lines] | down
#   sudo deploy/lightsail/ctl.sh access-key       print the access key
#   sudo deploy/lightsail/ctl.sh admin <email>    make a registered user admin
#   sudo deploy/lightsail/ctl.sh catalog <args>   backend.scripts.sync_catalog
#   sudo deploy/lightsail/ctl.sh backup | backups | restore <name>
#
# Data, secrets and certificates live under APPLY101_ROOT (default
# /srv/apply101), never in the checkout. Secrets are generated here and
# never leave the server.
set -euo pipefail

ROOT="${APPLY101_ROOT:-/srv/apply101}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
COMPOSE_ENV="$ROOT/compose.env"
APP_ENV="$ROOT/app.env"

die() { echo "ctl.sh: $*" >&2; exit 1; }

compose() {
  [ -f "$COMPOSE_ENV" ] || die "$COMPOSE_ENV is missing; run: ctl.sh setup <domain>"
  docker compose --project-directory "$HERE" -f "$HERE/compose.yaml" --env-file "$COMPOSE_ENV" "$@"
}

random_secret() {
  # $1 random bytes, URL-safe, no padding.
  head -c "$1" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
}

in_app() {
  compose exec -T app "$@"
}

cmd_setup() {
  local domain="${1:-}"
  [[ "$domain" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "usage: ctl.sh setup <domain> (a host name such as test.example.com)"
  command -v docker >/dev/null || die "docker is not installed (see the runbook, step 2)"
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed (see the runbook, step 2)"
  install -d -m 700 "$ROOT"
  install -d -m 700 "$ROOT/data" "$ROOT/caddy" "$ROOT/caddy/data" "$ROOT/caddy/config"
  if [ ! -f "$APP_ENV" ]; then
    (
      umask 077
      cat > "$APP_ENV" <<EOF
# Apply101 test deployment secrets and settings. Server only; never commit.
JWT_SECRET_KEY=$(random_secret 48)
APPLY101_ACCESS_KEY=$(random_secret 32)
# Daily catalog sync: off. See the runbook before turning it on.
APPLY101_CATALOG_SYNC_ENABLED=0
APPLY101_CATALOG_SYNC_HOUR_UTC=3
APPLY101_CATALOG_SYNC_MAX_BOARDS=10
APPLY101_CATALOG_SYNC_MAX_PAGES=10
EOF
    )
    echo "Created $APP_ENV with new secrets."
  else
    echo "Keeping the existing $APP_ENV."
  fi
  (
    umask 077
    cat > "$COMPOSE_ENV" <<EOF
APPLY101_ROOT=$ROOT
APPLY101_DOMAIN=$domain
APPLY101_HTTP_PORT=${APPLY101_HTTP_PORT:-80}
APPLY101_HTTPS_PORT=${APPLY101_HTTPS_PORT:-443}
EOF
  )
  cmd_up
  echo
  echo "Open https://$domain and enter the access key from: sudo $0 access-key"
}

cmd_up() {
  # APPLY101_NO_BUILD=1 starts an apply101-test:local image built elsewhere.
  if [ "${APPLY101_NO_BUILD:-0}" = "1" ]; then
    compose up -d
  else
    compose up -d --build
  fi
  compose ps
}

cmd_update() {
  git -C "$REPO" pull --ff-only
  cmd_up
}

cmd_access_key() {
  [ -f "$APP_ENV" ] || die "$APP_ENV is missing; run: ctl.sh setup <domain>"
  sed -n 's/^APPLY101_ACCESS_KEY=//p' "$APP_ENV"
}

cmd_backup() {
  # Inside the container the data directory is /data, so backups go to
  # /data/backups (on the host: $ROOT/data/backups).
  in_app python -m backend.scripts.backup_data backup --yes "$@"
}

cmd_restore() {
  [ -n "${1:-}" ] || die "usage: ctl.sh restore <backup name> (see: ctl.sh backups)"
  in_app python -m backend.scripts.backup_data restore --from "$1" --yes
  compose restart app
}

main() {
  local command="${1:-}"
  shift || true
  case "$command" in
    setup) cmd_setup "$@" ;;
    up) cmd_up ;;
    update) cmd_update ;;
    status) compose ps ;;
    logs) compose logs --tail "${1:-100}" ;;
    down) compose down ;;
    access-key) cmd_access_key ;;
    admin) [ -n "${1:-}" ] || die "usage: ctl.sh admin <email>"; in_app python -m backend.scripts.set_admin --email "$1" ;;
    catalog) in_app python -m backend.scripts.sync_catalog "$@" ;;
    backup) cmd_backup "$@" ;;
    backups) in_app python -m backend.scripts.backup_data list ;;
    restore) cmd_restore "$@" ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; [ -z "$command" ] || exit 1 ;;
  esac
}

main "$@"
