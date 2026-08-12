"""Add authentication fields required for login (email/password).

Run once from the project root:
    python -m backend.migrations.phase3_auth_fields
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine


# Nullable, no default: existing users have no password until explicitly
# migrated one at a time (see PHASE3_AUTH design notes). Adding this column
# leaves every existing row's value as NULL.
NULLABLE_COLUMNS = {
    "password_hash": "VARCHAR",
}

# NOT NULL with a DB-level default: existing rows must get a concrete,
# non-null value (0/false) rather than NULL.
NOT_NULL_COLUMNS_WITH_DEFAULT = {
    "is_admin": ("BOOLEAN", "0"),
}


def run(engine: Engine = default_engine) -> None:
    inspector = inspect(engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("users")
    }

    added = []
    with engine.begin() as connection:
        for column_name, column_type in NULLABLE_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(
                f"ALTER TABLE users ADD COLUMN {column_name} {column_type}"
            ))
            added.append(column_name)

        for column_name, (column_type, default_value) in NOT_NULL_COLUMNS_WITH_DEFAULT.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(
                f"ALTER TABLE users ADD COLUMN {column_name} {column_type} "
                f"NOT NULL DEFAULT {default_value}"
            ))
            added.append(column_name)

    if added:
        print("Added columns:", ", ".join(added))
    else:
        print("Phase 3 auth columns already exist. No changes made.")


if __name__ == "__main__":
    run()
