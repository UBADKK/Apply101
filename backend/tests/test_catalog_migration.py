"""phase9_catalog_sources on synthetic SQLite databases (never apply101.db)."""

import os
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.migrations import phase9_catalog_sources as migration


LEGACY_SCHEMA = [
    """CREATE TABLE users (user_id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, mail VARCHAR NOT NULL)""",
    """CREATE TABLE jobs (
        job_id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, company_name VARCHAR, location VARCHAR,
        url VARCHAR NOT NULL, description_text VARCHAR, source_created_at INTEGER, fetched_at DATETIME,
        last_seen_at DATETIME, job_status VARCHAR, last_status_checked_at DATETIME, last_status_code INTEGER,
        status_check_error VARCHAR, source VARCHAR, source_job_id VARCHAR, source_updated_at INTEGER,
        created_at DATETIME, updated_at DATETIME, created_by_user_id INTEGER REFERENCES users(user_id))""",
    """CREATE TABLE job_fetch_runs (
        run_id INTEGER PRIMARY KEY, source VARCHAR NOT NULL, fetch_type VARCHAR, params_json TEXT,
        started_at DATETIME, finished_at DATETIME, pages_checked INTEGER, last_checked_page INTEGER,
        jobs_seen_count INTEGER, new_jobs_count INTEGER, duplicate_jobs_count INTEGER,
        skipped_jobs_count INTEGER, stopped_reason VARCHAR, status VARCHAR, error_message TEXT)""",
    """INSERT INTO jobs (job_id, title, url, description_text, source, job_status)
       VALUES (7, 'Legacy job', 'https://example.test/legacy', 'text', 'arbeitnow', 'unknown')""",
    """INSERT INTO job_fetch_runs (run_id, source, status) VALUES (1, 'arbeitnow', 'completed')""",
]


class CatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase9_test_")
        self.engine = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'legacy.db')}")
        with self.engine.begin() as connection:
            for statement in LEGACY_SCHEMA:
                connection.execute(text(statement))

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def columns(self, table):
        return {column["name"] for column in inspect(self.engine).get_columns(table)}

    def test_dry_run_changes_nothing(self):
        steps = migration.run(self.engine, dry_run=True)
        self.assertIn("create table companies", steps)
        self.assertIn("add column jobs.content_hash", steps)
        self.assertNotIn("companies", inspect(self.engine).get_table_names())
        self.assertNotIn("content_hash", self.columns("jobs"))

    def test_migrates_and_keeps_rows_then_is_idempotent(self):
        migration.run(self.engine)
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue({"companies", "job_source_boards", "job_source_records"} <= tables)
        self.assertTrue({"company_id", "work_mode", "country_code", "content_hash", "closed_at"} <= self.columns("jobs"))
        self.assertTrue({"board_id", "updated_jobs_count", "closed_jobs_count", "linked_duplicates_count", "complete"} <= self.columns("job_fetch_runs"))
        self.assertIn("ix_jobs_company_id", {index["name"] for index in inspect(self.engine).get_indexes("jobs")})
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT title, job_status, company_id, content_hash FROM jobs WHERE job_id = 7")).one()
        self.assertEqual(tuple(row), ("Legacy job", "unknown", None, None))
        self.assertEqual(migration.run(self.engine), [])

        # The ORM works on the migrated database.
        session = sessionmaker(bind=self.engine)()
        try:
            self.assertEqual(session.query(models.Job).one().title, "Legacy job")
            self.assertEqual(session.query(models.JobFetchRun).one().complete, None)
        finally:
            session.close()

    def test_aborts_on_foreign_database(self):
        other = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'other.db')}")
        with other.begin() as connection:
            connection.execute(text("CREATE TABLE something (id INTEGER)"))
        with self.assertRaises(migration.MigrationAborted):
            migration.run(other)
        self.assertEqual(inspect(other).get_table_names(), ["something"])
        other.dispose()

    def test_aborts_on_conflicting_index(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE INDEX ix_jobs_company_id ON jobs (title)"))
        with self.assertRaises(migration.MigrationAborted):
            migration.run(self.engine)
        self.assertNotIn("companies", inspect(self.engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
