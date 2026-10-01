"""Add users.token_key (binds access tokens to one specific user row).

Every access token carries a "tkey" claim that must match the user's
current users.token_key. Because users.user_id has no AUTOINCREMENT, SQLite
can hand a deleted user's id to a new row; the per-row random key makes the
deleted user's still-unexpired tokens useless against that new row.

Behavior:
- Adds the nullable column if it is missing. It has no DB default: SQLite's
  ADD COLUMN can only apply one constant default, which would give every
  existing row the same key.
- Fills every row whose token_key is NULL or empty with its own distinct
  random key (backend.app.security.new_token_key). Rows that already have a
  key keep it.
- Idempotent: a second run changes nothing except filling rows that are
  still NULL/empty. Touches no other column or table. Never prints keys.

Every access token issued before this release has no "tkey" claim and is
rejected afterwards; every user must log in once again.

DEPLOY ORDER: back up the database first, then run this migration BEFORE
deploying code whose models.User includes token_key -- until then every
query on users fails with "no such column: users.token_key", including
backend/scripts/set_admin.py.

Run once from the project root:
    python -m backend.migrations.phase6_user_token_key
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine
from backend.app.security import new_token_key


COLUMN_NAME = "token_key"


def run(engine: Engine = default_engine) -> None:
    inspector = inspect(engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("users")
    }

    column_added = False
    filled_rows = 0
    with engine.begin() as connection:
        if COLUMN_NAME not in existing_columns:
            connection.execute(text(
                f"ALTER TABLE users ADD COLUMN {COLUMN_NAME} VARCHAR"
            ))
            column_added = True

        user_ids = connection.execute(text(
            f"SELECT user_id FROM users "
            f"WHERE {COLUMN_NAME} IS NULL OR {COLUMN_NAME} = ''"
        )).scalars().all()

        for user_id in user_ids:
            connection.execute(
                text(
                    f"UPDATE users SET {COLUMN_NAME} = :token_key "
                    "WHERE user_id = :user_id"
                ),
                {"token_key": new_token_key(), "user_id": user_id},
            )
        filled_rows = len(user_ids)

    if column_added:
        print(f"Added column users.{COLUMN_NAME}.")
    if filled_rows:
        print(f"Assigned a new token key to {filled_rows} user row(s).")
    if not column_added and not filled_rows:
        print("Phase 6 users.token_key already present for every user. No changes made.")


if __name__ == "__main__":
    run()
