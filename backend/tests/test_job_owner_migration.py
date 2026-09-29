import os
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base
from backend.migrations import phase5_job_owner


ORIGINAL_JOB_COLUMNS = (
    "job_id, title, company_name, location, url, description_text, "
    "source_created_at, fetched_at, last_seen_at, job_status, "
    "last_status_checked_at, last_status_code, status_check_error, source, "
    "source_job_id, source_updated_at, created_at, updated_at"
)


class Phase5JobOwnerMigrationTests(unittest.TestCase):
    """Exercises backend/migrations/phase5_job_owner.py against a throwaway,
    fully synthetic SQLite database. Never reads or writes apply101.db.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase5_migration_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_pre_migration_schema_with_rows(self):
        # jobs table as it existed before created_by_user_id, plus users and
        # an unrelated table. Structure and synthetic rows only.
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    mail VARCHAR UNIQUE NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE jobs (
                    job_id INTEGER PRIMARY KEY,
                    title VARCHAR NOT NULL,
                    company_name VARCHAR,
                    location VARCHAR,
                    url VARCHAR NOT NULL UNIQUE,
                    description_text VARCHAR,
                    source_created_at INTEGER,
                    fetched_at DATETIME,
                    last_seen_at DATETIME,
                    job_status VARCHAR,
                    last_status_checked_at DATETIME,
                    last_status_code INTEGER,
                    status_check_error VARCHAR,
                    source VARCHAR,
                    source_job_id VARCHAR,
                    source_updated_at INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            connection.execute(text(
                "CREATE TABLE unrelated (id INTEGER PRIMARY KEY, value VARCHAR)"
            ))
            connection.execute(text(
                "INSERT INTO users (user_id, name, mail) VALUES "
                "(1, 'Synthetic One', 'synthetic.one@example.invalid')"
            ))
            connection.execute(text("""
                INSERT INTO jobs (job_id, title, company_name, location, url,
                                  description_text, source_created_at, job_status,
                                  source, source_job_id)
                VALUES
                    (10, 'Synthetic Job A', 'Co A', 'Berlin',
                     'https://example.invalid/a', 'desc a', 1700000000,
                     'active', 'arbeitnow', 'a-1'),
                    (11, 'Synthetic Job B', NULL, NULL,
                     'https://example.invalid/b', NULL, NULL,
                     'unknown', NULL, NULL)
            """))
            connection.execute(text(
                "INSERT INTO unrelated (id, value) VALUES (1, 'keep')"
            ))

    def _dump(self, sql):
        with self.engine.connect() as connection:
            return connection.execute(text(sql)).fetchall()

    def _assert_column_and_index_exist(self):
        inspector = inspect(self.engine)
        self.assertIn(
            "created_by_user_id",
            {column["name"] for column in inspector.get_columns("jobs")},
        )
        indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("jobs")
        }
        self.assertEqual(
            indexes.get("ix_jobs_created_by_user_id"), ["created_by_user_id"]
        )

    def test_migration_adds_column_and_index_preserving_data_and_is_idempotent(self):
        self._create_pre_migration_schema_with_rows()
        before_jobs = self._dump(
            f"SELECT {ORIGINAL_JOB_COLUMNS} FROM jobs ORDER BY job_id"
        )
        before_users = self._dump("SELECT * FROM users ORDER BY user_id")
        before_unrelated = self._dump("SELECT * FROM unrelated ORDER BY id")

        phase5_job_owner.run(engine=self.engine)
        phase5_job_owner.run(engine=self.engine)  # must not raise

        self._assert_column_and_index_exist()
        foreign_keys = inspect(self.engine).get_foreign_keys("jobs")
        self.assertIn(
            ("users", ["created_by_user_id"], ["user_id"]),
            [
                (fk["referred_table"], fk["constrained_columns"], fk["referred_columns"])
                for fk in foreign_keys
            ],
        )

        self.assertEqual(
            before_jobs,
            self._dump(f"SELECT {ORIGINAL_JOB_COLUMNS} FROM jobs ORDER BY job_id"),
        )
        self.assertEqual(
            [row[0] for row in self._dump(
                "SELECT created_by_user_id FROM jobs ORDER BY job_id"
            )],
            [None, None],
        )
        self.assertEqual(before_users, self._dump("SELECT * FROM users ORDER BY user_id"))
        self.assertEqual(
            before_unrelated, self._dump("SELECT * FROM unrelated ORDER BY id")
        )

    def test_orm_reads_migrated_table(self):
        self._create_pre_migration_schema_with_rows()
        phase5_job_owner.run(engine=self.engine)

        session = sessionmaker(bind=self.engine)()
        try:
            jobs = session.query(models.Job).order_by(models.Job.job_id).all()
        finally:
            session.close()

        self.assertEqual([job.job_id for job in jobs], [10, 11])
        self.assertTrue(all(job.created_by_user_id is None for job in jobs))

    def test_migration_is_no_op_on_schema_created_from_current_models(self):
        Base.metadata.create_all(bind=self.engine)

        phase5_job_owner.run(engine=self.engine)
        phase5_job_owner.run(engine=self.engine)

        self._assert_column_and_index_exist()


if __name__ == "__main__":
    unittest.main()
