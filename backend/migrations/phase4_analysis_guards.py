"""Create the analysis_guards table used by profile-analysis cost/
concurrency protection (E3.2).

Run once from the project root:
    python -m backend.migrations.phase4_analysis_guards
"""

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from backend.app.database import engine as default_engine
from backend.app.models import AnalysisGuard


def run(engine: Engine = default_engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if AnalysisGuard.__tablename__ in existing_tables:
        print("analysis_guards table already exists. No changes made.")
        return

    AnalysisGuard.__table__.create(bind=engine)
    print(f"Created table: {AnalysisGuard.__tablename__}")


if __name__ == "__main__":
    run()
