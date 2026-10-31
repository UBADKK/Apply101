"""Shared job catalog: companies, source boards and source records, plus
catalog columns on jobs and job_fetch_runs.

Additive only:
- creates the tables companies, job_source_boards and job_source_records
  from their models when they are missing;
- adds the nullable columns jobs.company_id, work_mode, country_code,
  content_hash, closed_at and the index ix_jobs_company_id;
- adds the nullable columns job_fetch_runs.board_id, updated_jobs_count,
  closed_jobs_count, linked_duplicates_count, complete (when that table
  exists; create_all builds it complete otherwise).
Existing rows keep NULL in every new column; no row is changed or deleted.
Idempotent: a second run makes no changes. Prints table/column names only,
never row contents.

Unlike new tables, new columns are NOT added by Base.metadata.create_all at
app startup: run this script (after backing up the database and stopping
the app) before starting code that reads these columns.

Run once from the project root:
    python -m backend.migrations.phase9_catalog_sources [--dry-run]
"""

import argparse
import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine
from backend.app.models import Company, JobSourceBoard, JobSourceRecord


NEW_TABLES = (Company.__table__, JobSourceBoard.__table__, JobSourceRecord.__table__)

NEW_COLUMNS = {
    "jobs": (
        ("company_id", "INTEGER REFERENCES companies(company_id)"),
        ("work_mode", "VARCHAR"),
        ("country_code", "VARCHAR"),
        ("content_hash", "VARCHAR"),
        ("closed_at", "DATETIME"),
    ),
    "job_fetch_runs": (
        ("board_id", "INTEGER REFERENCES job_source_boards(board_id)"),
        ("updated_jobs_count", "INTEGER"),
        ("closed_jobs_count", "INTEGER"),
        ("linked_duplicates_count", "INTEGER"),
        ("complete", "BOOLEAN"),
    ),
}

NEW_INDEXES = (("ix_jobs_company_id", "jobs", "company_id"),)


class MigrationAborted(RuntimeError):
    """The database is not in a state this migration can safely handle.
    Nothing was changed."""


def plan(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "jobs" not in tables:
        raise MigrationAborted("Table jobs is missing; this is not an Apply101 database.")
    steps = []
    for table in NEW_TABLES:
        if table.name not in tables:
            steps.append(f"create table {table.name}")
    for table_name, columns in NEW_COLUMNS.items():
        if table_name not in tables:
            # Created complete by create_all at app startup.
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for name, _ in columns:
            if name not in existing:
                steps.append(f"add column {table_name}.{name}")
    for index_name, table_name, column in NEW_INDEXES:
        indexes = {index["name"]: index for index in inspector.get_indexes(table_name)}
        if index_name in indexes:
            if indexes[index_name]["column_names"] != [column]:
                raise MigrationAborted(f"Index {index_name} exists on other columns.")
        else:
            steps.append(f"create index {index_name}")
    return steps


def run(engine: Engine = default_engine, dry_run: bool = False) -> list[str]:
    steps = plan(engine)
    if dry_run or not steps:
        return steps
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in NEW_TABLES:
            if table.name not in tables:
                table.create(bind=connection)
        for table_name, columns in NEW_COLUMNS.items():
            if table_name not in tables:
                continue
            existing = {column["name"] for column in inspect(connection).get_columns(table_name)}
            for name, ddl in columns:
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))
        for index_name, table_name, column in NEW_INDEXES:
            indexes = {index["name"] for index in inspect(connection).get_indexes(table_name)}
            if index_name not in indexes:
                connection.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({column})"))
    return steps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Only print what would change.")
    args = parser.parse_args(argv)
    print(f"Database: {default_engine.url.render_as_string(hide_password=True)}")
    try:
        steps = run(default_engine, dry_run=args.dry_run)
    except MigrationAborted as exc:
        print(f"Aborted, nothing changed: {exc}")
        return 1
    if not steps:
        print("Nothing to do.")
    for step in steps:
        print(("Would " if args.dry_run else "Done: ") + step)
    return 0


if __name__ == "__main__":
    sys.exit(main())
