"""Create the analysis_quota_reservations table (rolling per-user quota of
owner manual job analysis) and its lookup index.

Additive only: creates the table (with its index) from
models.AnalysisQuotaReservation when it is missing, or only the index when
the table already exists without it. Never drops, alters or deletes
anything. If the table exists but lacks an expected column, or an index of
the same name covers other columns, it stops with MigrationAborted and
changes nothing. Idempotent: a second run makes no changes. Prints table/
index names only, never row contents.

Starting the app also creates this table and index: backend/app/main.py
calls Base.metadata.create_all at import, with the same DDL. This script
makes the additive change explicit and auditable; it can be run (after
backing up the database) before starting the new code. Only if neither has
run (e.g. an entry point that does not import main.py) do owner (non-admin)
job analyses fail closed with 503; admin, batch and cached paths are
unaffected. Running it after create_all is a no-op.

Run once from the project root:
    python -m backend.migrations.phase8_analysis_quota_reservations
"""

import argparse
import os
import sys

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine
from backend.app.models import AnalysisQuotaReservation


TABLE_NAME = AnalysisQuotaReservation.__tablename__
INDEX_NAME = "ix_analysis_quota_reservations_op_user_reserved_at"
REQUIRED_COLUMNS = ("id", "operation_type", "user_id", "job_id", "reserved_at")


class MigrationAborted(RuntimeError):
    """The database is not in a state this migration can safely handle.
    Nothing was changed."""


def _model_index():
    by_name = {index.name: index for index in AnalysisQuotaReservation.__table__.indexes}
    return by_name[INDEX_NAME]


def run(engine: Engine = default_engine) -> None:
    inspector = inspect(engine)

    if TABLE_NAME not in set(inspector.get_table_names()):
        # Creates the table and its index, exactly as create_all would.
        AnalysisQuotaReservation.__table__.create(bind=engine)
        print(f"Created table: {TABLE_NAME} (with index {INDEX_NAME})")
        return

    existing_columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in existing_columns]
    if missing_columns:
        raise MigrationAborted(
            f"Table {TABLE_NAME} exists but is missing {len(missing_columns)} expected "
            f"column(s): {', '.join(missing_columns)}. Nothing was changed."
        )

    index = _model_index()
    expected_index_columns = [column.name for column in index.columns]
    existing_indexes = {
        existing["name"]: existing["column_names"]
        for existing in inspector.get_indexes(TABLE_NAME)
    }
    if INDEX_NAME in existing_indexes:
        if existing_indexes[INDEX_NAME] != expected_index_columns:
            raise MigrationAborted(
                f"Index {INDEX_NAME} exists on {TABLE_NAME} with unexpected columns. "
                "Nothing was changed."
            )
        print(f"{TABLE_NAME} table and index {INDEX_NAME} already exist. No changes made.")
        return

    index.create(bind=engine, checkfirst=True)
    print(f"Created index: {INDEX_NAME} on existing table {TABLE_NAME}")


def _sqlite_file_path(engine: Engine):
    if engine.url.get_backend_name() != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return os.path.abspath(database)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.migrations.phase8_analysis_quota_reservations",
        description="Create the analysis_quota_reservations table and its index.",
    )
    parser.parse_args(argv)

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
        run(default_engine)
    except MigrationAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
