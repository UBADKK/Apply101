"""Add profile facts required by structured eligibility matching.

Run once from the project root:
    python -m backend.migrations.phase2_profile_fields
"""

from sqlalchemy import inspect, text

from backend.app.database import engine


REQUIRED_COLUMNS = {
    "current_residence_country": "VARCHAR",
    "student_status": "VARCHAR",
}


def run() -> None:
    inspector = inspect(engine)
    existing_columns = {
        column["name"]
        for column in inspector.get_columns("candidate_profiles")
    }

    added = []
    with engine.begin() as connection:
        for column_name, column_type in REQUIRED_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(
                f"ALTER TABLE candidate_profiles ADD COLUMN {column_name} {column_type}"
            ))
            added.append(column_name)

    if added:
        print("Added columns:", ", ".join(added))
    else:
        print("Phase 2 profile columns already exist. No changes made.")


if __name__ == "__main__":
    run()
