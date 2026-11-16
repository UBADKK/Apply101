"""Read-only usage report of paid provider calls (W17).

    python -m backend.scripts.provider_call_usage

On the Lightsail deployment:

    sudo deploy/lightsail/ctl.sh exec python -m backend.scripts.provider_call_usage

For the rolling window of the system-wide provider-call quota
(PROVIDER_CALL_GLOBAL_QUOTA_WINDOW_SECONDS, default 86400) it prints:

- the global quota: calls used by non-admin users / the limit
  (PROVIDER_CALL_GLOBAL_QUOTA_MAX_ATTEMPTS, default 100), and in how many
  seconds the next slot frees (0 when one is free now);
- admin-triggered calls per admin user_id (recorded, never limited);
- reservation counts per operation_type in the same window (the per-user
  quotas have their own windows; this report always uses the global one).

The global count is app-level calls, not money: see
analysis_guard.ProviderCallQuotaConfig. Rows older than the window may
still be in the table (they are pruned at the next reservation); they are
not counted here.

Ids and counts only, never emails or other personal data. It works on
APPLY101_DATABASE_URL from the process environment only (never .env),
refuses the repository's real apply101.db and ./apply101.db in the working
directory, never creates a database and opens it read-only, so it writes
nothing. Exit code 0 after a report, 1 on a refusal or a database error.
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from backend.app import config


EXIT_SUCCESS = 0
EXIT_FAILURE = 1


class UsageError(RuntimeError):
    pass


def _target(environ) -> Path:
    url = (environ.get(config.DATABASE_URL_ENV) or "").strip()
    if not url:
        raise UsageError(f"Set {config.DATABASE_URL_ENV}; the default is the real local data.")
    config._reject_sqlite_uri(url)
    database = config._sqlite_file(url)
    if database is None:
        raise UsageError(f"{config.DATABASE_URL_ENV} must be a file-backed SQLite URL.")
    database = Path(os.path.abspath(database))
    if config._same_path(database, config.REPO_ROOT / config.DEFAULT_DATABASE_FILE) or config._same_path(
        database, config.DEFAULT_DATABASE_FILE
    ):
        raise UsageError("Refusing the repository's real apply101.db or ./apply101.db in the working directory.")
    if not database.is_file():
        raise UsageError(f"The database {database} does not exist.")
    return database


def run(database: Path, *, environ=None, now: float | None = None) -> dict:
    # Imported here, after the target was checked: backend.app.database
    # (through analysis_guard -> models) reads the process environment on
    # import. Creating that engine does not connect.
    from backend.app import analysis_guard, models

    quota = analysis_guard.load_provider_call_quota_config(environ)
    now = time.time() if now is None else now
    cutoff = now - quota.window_seconds
    table = models.AnalysisQuotaReservation.__table__
    uri = f"{database.as_uri()}?mode=ro"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(table.c.operation_type, table.c.user_id, table.c.reserved_at)
                .where(table.c.reserved_at > cutoff)
                .order_by(table.c.reserved_at.asc())
            ).fetchall()
    finally:
        engine.dispose()

    global_ats = [
        row.reserved_at for row in rows
        if row.operation_type == analysis_guard.PROVIDER_CALL_GLOBAL_OPERATION_TYPE
        and row.user_id == analysis_guard.PROVIDER_CALL_GLOBAL_USER_ID
    ]
    used = len(global_ats)
    if used < quota.max_attempts:
        next_slot = 0
    else:
        # Same rule as analysis_guard._reserve_quota_slot.
        frees_at = global_ats[used - quota.max_attempts] + quota.window_seconds
        next_slot = max(1, math.ceil(frees_at - now))

    admin_calls: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row.operation_type] = by_type.get(row.operation_type, 0) + 1
        if row.operation_type == analysis_guard.PROVIDER_CALL_ADMIN_OPERATION_TYPE:
            key = str(row.user_id)
            admin_calls[key] = admin_calls.get(key, 0) + 1

    return {
        "target": str(database),
        "window_seconds": quota.window_seconds,
        "global": {
            "used": used,
            "limit": quota.max_attempts,
            "remaining": max(0, quota.max_attempts - used),
            "next_slot_frees_in_seconds": next_slot,
        },
        "admin_calls_by_user_id": dict(sorted(admin_calls.items(), key=lambda item: int(item[0]))),
        "counts_by_operation_type": dict(sorted(by_type.items())),
    }


def main(argv=None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Read-only report of paid provider calls in the rolling window")
    parser.parse_args(argv)
    try:
        database = _target(environ)
        report = run(database, environ=environ)
    except (UsageError, config.ConfigError, sqlite3.Error, SQLAlchemyError, OSError) as exc:
        print(f"Refusing to continue: {exc}")
        return EXIT_FAILURE
    except Exception as exc:  # e.g. an invalid PROVIDER_CALL_GLOBAL_QUOTA_* value
        print(f"Refusing to continue: {type(exc).__name__}: {exc}")
        return EXIT_FAILURE
    print(json.dumps(report, indent=2))
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
