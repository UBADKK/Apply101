"""Make jobs.url uniqueness ownership-aware.

Replaces the global unique index ix_jobs_url (UNIQUE(url) over every job)
with two SQLite partial unique indexes defined on models.Job:

- uq_jobs_catalog_url: UNIQUE(url) WHERE created_by_user_id IS NULL
- uq_jobs_owner_url:   UNIQUE(created_by_user_id, url)
                       WHERE created_by_user_id IS NOT NULL

URL rules afterwards (url equality stays exact string equality, no
normalization):
1. Ownerless catalog jobs (created_by_user_id IS NULL) have unique urls.
2. The same user cannot own the same url twice.
3. A catalog job and a personal job may share a url.
4. Personal jobs of different users may share a url.

Base.metadata.create_all only creates missing tables; it never alters the
indexes of an existing jobs table. Every existing database therefore needs
this migration -- only a brand-new database gets the new indexes from
create_all.

Behavior:
- Requires phase 5 (jobs.created_by_user_id). Runs every check and all DDL
  inside ONE explicit SQLite transaction (BEGIN IMMEDIATE ... COMMIT): the
  new indexes are created first, then ix_jobs_url is dropped; any error
  rolls everything back.
- Stops with MigrationAborted and changes nothing if rows would violate the
  new indexes (duplicate catalog urls, or one user owning the same url
  twice), or if the jobs index layout is anything other than the exact
  old state or the exact migrated state.
- Idempotent: on an already-migrated database it changes nothing.
- Prints counts and index names only, never row contents.

DEPLOY ORDER: back up the database first, stop the app, run with --dry-run
first, then run the migration, then start the new code.

Run from the project root:
    python -m backend.migrations.phase7_job_url_ownership --dry-run
    python -m backend.migrations.phase7_job_url_ownership
"""

import argparse
import os
import pathlib
import re
import sqlite3
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex

from backend.app.database import engine as default_engine
from backend.app.models import Job


OLD_INDEX_NAME = "ix_jobs_url"
# What create_all emitted for the previous Column(url, unique=True, index=True).
OLD_INDEX_SQL = "CREATE UNIQUE INDEX ix_jobs_url ON jobs (url)"
CATALOG_INDEX_NAME = "uq_jobs_catalog_url"
OWNER_INDEX_NAME = "uq_jobs_owner_url"
TARGET_INDEX_NAMES = (CATALOG_INDEX_NAME, OWNER_INDEX_NAME)
TARGET_INDEX_COLUMNS = {
    CATALOG_INDEX_NAME: ("url",),
    OWNER_INDEX_NAME: ("created_by_user_id", "url"),
}
REQUIRED_JOBS_COLUMNS = ("url", "created_by_user_id")

PLAN_APPLY = "apply"
PLAN_NO_CHANGES = "no_changes"


class MigrationAborted(RuntimeError):
    """The database is not in a state this migration can safely handle.
    Nothing was changed."""


def _target_indexes() -> list:
    by_name = {index.name: index for index in Job.__table__.indexes}
    missing = [name for name in TARGET_INDEX_NAMES if name not in by_name]
    if missing:
        raise MigrationAborted(
            "models.Job does not define the expected index(es) "
            f"{', '.join(missing)}; this migration must run with the matching "
            "application code. No changes made."
        )
    return [by_name[name] for name in TARGET_INDEX_NAMES]


def _compiled_ddl(connection: Connection, index) -> str:
    return str(CreateIndex(index).compile(dialect=connection.dialect))


def _normalize_sql(sql):
    return " ".join(sql.split()) if sql is not None else None


def _jobs_index_details(connection: Connection) -> dict:
    """name -> {unique, origin, partial, columns, sql} for every index on
    jobs, including sqlite_autoindex_* (which inspector.get_indexes hides)."""
    details = {}
    for _seq, name, unique, origin, partial in connection.exec_driver_sql(
        'PRAGMA index_list("jobs")'
    ).fetchall():
        columns = tuple(
            row[2]
            for row in connection.exec_driver_sql(
                f'PRAGMA index_info("{name}")'
            ).fetchall()
        )
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).scalar()
        details[name] = {
            "unique": unique,
            "origin": origin,
            "partial": partial,
            "columns": columns,
            "sql": sql,
        }
    return details


def _check_jobs_table(connection: Connection) -> str:
    table_sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).scalar()
    if table_sql is None:
        raise MigrationAborted("Table jobs does not exist. No changes made.")

    columns = {
        row[1]
        for row in connection.exec_driver_sql('PRAGMA table_info("jobs")').fetchall()
    }
    missing = [name for name in REQUIRED_JOBS_COLUMNS if name not in columns]
    if missing:
        raise MigrationAborted(
            f"jobs is missing column(s) {', '.join(missing)}; run "
            "backend/migrations/phase5_job_owner.py first. No changes made."
        )
    return table_sql


def _check_conflicts(connection: Connection) -> None:
    catalog_groups, catalog_rows = connection.exec_driver_sql(
        "SELECT COUNT(*), COALESCE(SUM(n), 0) FROM ("
        "SELECT COUNT(*) AS n FROM jobs WHERE created_by_user_id IS NULL "
        "GROUP BY url HAVING COUNT(*) > 1)"
    ).one()
    owned_groups, owned_rows = connection.exec_driver_sql(
        "SELECT COUNT(*), COALESCE(SUM(n), 0) FROM ("
        "SELECT COUNT(*) AS n FROM jobs WHERE created_by_user_id IS NOT NULL "
        "GROUP BY created_by_user_id, url HAVING COUNT(*) > 1)"
    ).one()

    problems = []
    if catalog_groups:
        problems.append(
            f"{catalog_groups} duplicated catalog url value(s) across "
            f"{catalog_rows} ownerless job row(s)"
        )
    if owned_groups:
        problems.append(
            f"{owned_groups} duplicated (created_by_user_id, url) pair(s) "
            f"across {owned_rows} owned job row(s)"
        )
    if problems:
        raise MigrationAborted(
            "Existing jobs would violate the new unique indexes: "
            + "; ".join(problems)
            + ". Resolve these rows first. No changes made."
        )


def _classify_indexes(connection: Connection, table_sql: str) -> str:
    details = _jobs_index_details(connection)

    for name, info in details.items():
        if info["origin"] == "u" or name.startswith("sqlite_autoindex"):
            raise MigrationAborted(
                f"jobs has a table-level UNIQUE constraint ({name}); this "
                "layout is not supported by this migration. No changes made."
            )
    if re.search(r"\bUNIQUE\b", table_sql, re.IGNORECASE):
        raise MigrationAborted(
            "CREATE TABLE jobs contains a UNIQUE constraint; this layout is "
            "not supported by this migration. No changes made."
        )

    allowed_unique = {OLD_INDEX_NAME, *TARGET_INDEX_NAMES}
    unexpected = sorted(
        name for name, info in details.items()
        if info["unique"] and name not in allowed_unique
    )
    if unexpected:
        raise MigrationAborted(
            "jobs has unexpected unique index(es): "
            f"{', '.join(unexpected)}. No changes made."
        )

    # Index names are global in SQLite: a target name on another table
    # would make CREATE INDEX fail, and is not a state we recognise.
    elsewhere = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name IN (?, ?, ?) AND tbl_name != 'jobs'",
        (OLD_INDEX_NAME, *TARGET_INDEX_NAMES),
    ).scalars().all()
    if elsewhere:
        raise MigrationAborted(
            f"Index name(s) {', '.join(sorted(elsewhere))} exist on another "
            "table. No changes made."
        )

    old = details.get(OLD_INDEX_NAME)
    present_targets = [name for name in TARGET_INDEX_NAMES if name in details]

    old_is_expected = old is not None and (
        old["unique"] == 1
        and old["origin"] == "c"
        and old["partial"] == 0
        and old["columns"] == ("url",)
        and _normalize_sql(old["sql"]) == OLD_INDEX_SQL
    )
    if old_is_expected and not present_targets:
        return PLAN_APPLY

    if old is None and len(present_targets) == len(TARGET_INDEX_NAMES):
        expected_sql = {
            index.name: _normalize_sql(_compiled_ddl(connection, index))
            for index in _target_indexes()
        }
        if all(
            details[name]["unique"] == 1
            and details[name]["origin"] == "c"
            and details[name]["partial"] == 1
            and details[name]["columns"] == TARGET_INDEX_COLUMNS[name]
            and _normalize_sql(details[name]["sql"]) == expected_sql[name]
            for name in TARGET_INDEX_NAMES
        ):
            return PLAN_NO_CHANGES

    present = sorted(
        name for name in (OLD_INDEX_NAME, *TARGET_INDEX_NAMES) if name in details
    )
    raise MigrationAborted(
        "jobs URL indexes are in neither the expected old state (only "
        f"{OLD_INDEX_NAME} as UNIQUE(url)) nor the expected migrated state "
        f"(only {CATALOG_INDEX_NAME} and {OWNER_INDEX_NAME} as defined on "
        f"models.Job). Present: {', '.join(present) or 'none'}. "
        "No changes made."
    )


def _plan(connection: Connection) -> str:
    """All pre-checks, read-only, in the required order."""
    table_sql = _check_jobs_table(connection)
    _check_conflicts(connection)
    return _classify_indexes(connection, table_sql)


def _create_new_indexes(connection: Connection) -> None:
    for index in _target_indexes():
        connection.exec_driver_sql(_compiled_ddl(connection, index))


def _drop_old_index(connection: Connection) -> None:
    connection.exec_driver_sql(f"DROP INDEX {OLD_INDEX_NAME}")


def _print_counts(connection: Connection) -> None:
    catalog, owned = connection.exec_driver_sql(
        "SELECT COALESCE(SUM(created_by_user_id IS NULL), 0), "
        "COALESCE(SUM(created_by_user_id IS NOT NULL), 0) FROM jobs"
    ).one()
    print(
        f"Checked {catalog + owned} job row(s) ({catalog} catalog, "
        f"{owned} owned): no url conflicts."
    )


def run(engine: Engine = default_engine, dry_run: bool = False) -> str:
    """Returns PLAN_APPLY (applied, or would apply in a dry run) or
    PLAN_NO_CHANGES. Raises MigrationAborted (nothing changed) when the
    database is not safe to migrate."""
    target_names = ", ".join(TARGET_INDEX_NAMES)

    with engine.connect() as connection:
        # Driver-level autocommit (pysqlite isolation_level=None): the
        # driver then never issues its own BEGIN/COMMIT and, unlike its
        # legacy mode, does not leave DDL outside the transaction. Applied
        # to this connection only; the pool restores it on return.
        connection.execution_options(isolation_level="AUTOCOMMIT")

        if dry_run:
            plan = _plan(connection)
            _print_counts(connection)
            if plan == PLAN_APPLY:
                print(
                    f"Dry run: would create index(es) {target_names} and drop "
                    f"index {OLD_INDEX_NAME}. No changes made."
                )
            else:
                print(
                    "Dry run: No changes needed; ownership-aware url indexes "
                    "already present."
                )
            return plan

        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            plan = _plan(connection)
            _print_counts(connection)
            if plan == PLAN_APPLY:
                _create_new_indexes(connection)
                _drop_old_index(connection)
            connection.exec_driver_sql("COMMIT")
        except BaseException:
            if connection.connection.dbapi_connection.in_transaction:
                connection.exec_driver_sql("ROLLBACK")
            raise

    if plan == PLAN_APPLY:
        print(f"Created index(es): {target_names}. Dropped index: {OLD_INDEX_NAME}.")
    else:
        print(
            "Phase 7 ownership-aware job url indexes already present. "
            "No changes made."
        )
    return plan


def _sqlite_file_path(engine: Engine):
    if engine.url.get_backend_name() != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return os.path.abspath(database)


def _read_only_engine(db_path: str) -> Engine:
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    return create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
        poolclass=NullPool,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.migrations.phase7_job_url_ownership",
        description="Replace the global jobs.url unique index with "
        "ownership-aware partial unique indexes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check and report the plan without changing anything "
        "(opens the database read-only).",
    )
    args = parser.parse_args(argv)

    # SQLite silently creates an empty file on first connection; refuse
    # instead of "migrating" a fresh database from the wrong directory.
    db_path = _sqlite_file_path(default_engine)
    if db_path is None:
        print(
            "Refusing to continue: no file-based SQLite database is configured.",
            file=sys.stderr,
        )
        return 2
    print(f"Target database: {db_path}")
    if not os.path.isfile(db_path):
        print(
            f"Refusing to continue: no database file exists at {db_path}. "
            "Run from the project root. Nothing was done.",
            file=sys.stderr,
        )
        return 2

    try:
        if args.dry_run:
            read_only_engine = _read_only_engine(db_path)
            try:
                run(read_only_engine, dry_run=True)
            finally:
                read_only_engine.dispose()
        else:
            run(default_engine)
    except MigrationAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
