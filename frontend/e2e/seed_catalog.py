"""Seeds the e2e sandbox with synthetic catalog jobs and an admin user.

Run only by e2e/discovery.spec.ts, with the environment playwright.config.ts
gives the backend. Refuses the default database and upload directory, and
any provider but the fake one, so it can never touch real data. Writes
through backend.app.catalog_sync, exactly like a sync from fixtures.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app import catalog_sync, config, models  # noqa: E402
from backend.app.openai_client import FAKE_ANALYSIS_PROVIDER, selected_analysis_provider  # noqa: E402

if config.database_is_default() or config.upload_dir_is_default() or selected_analysis_provider() != FAKE_ANALYSIS_PROVIDER:
    sys.exit("Refusing to seed: only the isolated e2e sandbox with the fake provider.")

from backend.app.database import Base, SessionLocal, engine  # noqa: E402
from backend.app.job_sources import BoardFetch, NormalizedPosting  # noqa: E402
from backend.app.security import hash_password  # noqa: E402

admin_mail, admin_password = sys.argv[1], sys.argv[2]
now = datetime.now(timezone.utc)
Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    if db.query(models.User).filter(models.User.mail == admin_mail).first() is None:
        db.add(models.User(name="E2E Admin", mail=admin_mail, password_hash=hash_password(admin_password), is_admin=True))
    board, _ = catalog_sync.create_board(db, company_name="Demo Robotics", source="greenhouse", board_token="demorobotics", region="global", now=now)
    board.is_enabled = True
    postings = [
        NormalizedPosting(source="greenhouse", source_key="demorobotics:1", title="Pool Backend Developer",
                          url="https://job-boards.greenhouse.io/demorobotics/jobs/1",
                          description_text="Demo pool posting. You build APIs with Python, FastAPI and SQL. Fluent English (C1) is required.",
                          location="Berlin, Germany", work_mode="remote", country_code="DE"),
        NormalizedPosting(source="greenhouse", source_key="demorobotics:2", title="Pool Platform Engineer (German)",
                          url="https://job-boards.greenhouse.io/demorobotics/jobs/2",
                          description_text="Demo pool posting. Python and Kubernetes. Fluent German (C1) is required for this role.",
                          location="Munich, Germany", work_mode="onsite", country_code="DE"),
        NormalizedPosting(source="greenhouse", source_key="demorobotics:3", title="Pool Office Coordinator",
                          url="https://job-boards.greenhouse.io/demorobotics/jobs/3",
                          description_text="Demo pool posting. Organize the Istanbul office and events.",
                          location="Istanbul, Turkey", work_mode="hybrid", country_code="TR"),
    ]
    catalog_sync.apply_postings(db, source="greenhouse", board=board, now=now,
                                board_company=db.get(models.Company, board.company_id),
                                fetch=BoardFetch(postings=postings, complete=True))
    db.commit()
finally:
    db.close()
print("seeded")
