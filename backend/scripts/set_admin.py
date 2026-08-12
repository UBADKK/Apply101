"""Promote an existing, login-capable registered user to admin.

This is the only way to create the first admin account -- there is no HTTP
endpoint and no startup-time bootstrap behavior. It must be run manually,
once, by an operator with server/shell access:

    python -m backend.scripts.set_admin --email admin@example.com

This tool never creates a user, never sets or changes a password, and never
touches any column other than is_admin.
"""

import argparse
import os

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import engine as default_engine


EXIT_SUCCESS = 0
EXIT_FAILURE = 1

# Same EmailStr validation the auth system already uses (see
# backend/routers/auth.py's _normalize_login_email) -- this is a second call
# site for the identical canonical pattern, not a new normalization
# algorithm. Deliberately not imported from backend.routers.auth: that
# module is an HTTP-layer router, and this is a data-layer maintenance
# script with no business depending on it.
_email_adapter = TypeAdapter(EmailStr)


def _normalize_email(raw_email: str) -> str | None:
    try:
        return _email_adapter.validate_python(raw_email)
    except ValidationError:
        return None


def run(email: str, engine: Engine = default_engine) -> int:
    normalized_email = _normalize_email(email)
    if normalized_email is None:
        print(f"Refusing to continue: {email!r} is not a valid email address.")
        return EXIT_FAILURE

    # For a file-based SQLite target, resolve and print the exact file this
    # run would modify, and refuse outright if it doesn't already exist --
    # SQLite silently creates an empty database file on first connection,
    # which would otherwise let this tool point at nothing and appear to
    # "fail" in a confusing way, or worse, quietly operate against a fresh,
    # unrelated database. This check happens before any session/connection
    # is opened, using only the path string from the engine's URL.
    if engine.url.get_backend_name() == "sqlite":
        db_path = engine.url.database
        if not db_path:
            print(
                "Refusing to continue: no file-based SQLite database is "
                "configured for this engine."
            )
            return EXIT_FAILURE

        resolved_path = os.path.abspath(db_path)
        print(f"Target database: {resolved_path}")

        if not os.path.isfile(resolved_path):
            print(
                f"Refusing to continue: no database file exists at "
                f"{resolved_path}. This tool never creates a new database."
            )
            return EXIT_FAILURE

    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_factory()
    try:
        user = db.query(models.User).filter(models.User.mail == normalized_email).first()

        if user is None:
            print(f"Refusing to continue: no user found with email {normalized_email!r}.")
            return EXIT_FAILURE

        if user.password_hash is None:
            print(
                f"Refusing to continue: user_id={user.user_id} "
                f"({normalized_email}) has no password set and cannot log "
                "in. Promoting a passwordless account would create an "
                "unusable admin identity. Register/set a password for this "
                "account first."
            )
            return EXIT_FAILURE

        if user.is_admin:
            print(
                f"user_id={user.user_id} ({normalized_email}) is already "
                "an admin. No changes made."
            )
            return EXIT_SUCCESS

        user.is_admin = True

        try:
            db.commit()
        except Exception:
            db.rollback()
            print(
                "Refusing to continue: the database commit failed. "
                "No changes were saved."
            )
            return EXIT_FAILURE

        print(f"user_id={user.user_id} ({normalized_email}) promoted to admin.")
        return EXIT_SUCCESS
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.scripts.set_admin",
        description="Promote an existing, login-capable registered user to admin.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email address of the existing registered user to promote.",
    )
    args = parser.parse_args(argv)
    return run(args.email)


if __name__ == "__main__":
    raise SystemExit(main())
