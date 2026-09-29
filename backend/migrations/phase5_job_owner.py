"""Add jobs.created_by_user_id (job ownership / visibility foundation).

Nullable, no default: every existing job stays NULL (ownerless), i.e.
visible to every authenticated user exactly as before. Idempotent: a
second run makes no changes.

Must run BEFORE deploying code whose models.Job includes
created_by_user_id -- until then every ORM query on jobs fails with
"no such column".

Run once from the project root:
    python -m backend.migrations.phase5_job_owner
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine


COLUMN_NAME = "created_by_user_id"
# Same name SQLAlchemy generates for Column(..., index=True) on jobs.
INDEX_NAME = "ix_jobs_created_by_user_id"


def run(engine: Engine = default_engine) -> None:
    inspector = inspect(engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("jobs")
    }
    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("jobs")
    }

    changes = []
    with engine.begin() as connection:
        if COLUMN_NAME not in existing_columns:
            # SQLite allows ADD COLUMN ... REFERENCES only when the column's
            # default is NULL, which is the case here (no DEFAULT clause).
            connection.execute(text(
                f"ALTER TABLE jobs ADD COLUMN {COLUMN_NAME} INTEGER "
                "REFERENCES users(user_id)"
            ))
            changes.append(f"column jobs.{COLUMN_NAME}")

        if INDEX_NAME not in existing_indexes:
            connection.execute(text(
                f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
                f"ON jobs ({COLUMN_NAME})"
            ))
            changes.append(f"index {INDEX_NAME}")

    if changes:
        print("Added:", ", ".join(changes))
    else:
        print("Phase 5 job owner column/index already exist. No changes made.")


if __name__ == "__main__":
    run()
