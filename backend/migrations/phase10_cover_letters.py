"""Motivation letters: the tables cover_letters and cover_letter_versions.

Additive only: creates each table from its model when it is missing; never
alters or deletes anything. Starting the app also creates them
(Base.metadata.create_all); this script makes the change explicit. A table
that exists without an expected column stops the migration unchanged.
Idempotent; prints table names only.

Run once from the project root:
    python -m backend.migrations.phase10_cover_letters [--dry-run]
"""

import argparse
import sys

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine
from backend.app.models import CoverLetter, CoverLetterVersion


TABLES = (CoverLetter.__table__, CoverLetterVersion.__table__)


class MigrationAborted(RuntimeError):
    """The database is not in a state this migration can safely handle."""


def run(engine: Engine = default_engine, dry_run: bool = False) -> list[str]:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if "users" not in existing:
        raise MigrationAborted("Table users is missing; this is not an Apply101 database.")
    steps = []
    for table in TABLES:
        if table.name in existing:
            columns = {column["name"] for column in inspector.get_columns(table.name)}
            missing = [column.name for column in table.columns if column.name not in columns]
            if missing:
                raise MigrationAborted(f"Table {table.name} exists without columns {missing}.")
        else:
            steps.append(f"create table {table.name}")
    if not dry_run:
        for table in TABLES:
            if table.name not in existing:
                table.create(bind=engine)
    return steps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(f"Database: {default_engine.url.render_as_string(hide_password=True)}")
    try:
        steps = run(default_engine, dry_run=args.dry_run)
    except MigrationAborted as exc:
        print(f"Aborted, nothing changed: {exc}")
        return 1
    print("\n".join(("Would " if args.dry_run else "Done: ") + step for step in steps) or "Nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
