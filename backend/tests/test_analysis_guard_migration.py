import os
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text

from backend.migrations import phase4_analysis_guards


class Phase4AnalysisGuardsMigrationTests(unittest.TestCase):
    """Exercises backend/migrations/phase4_analysis_guards.py against a
    throwaway, fully synthetic SQLite database. Never reads or writes
    apply101.db.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase4_migration_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _table_names(self):
        return set(inspect(self.engine).get_table_names())

    def _column_names(self):
        return {c["name"] for c in inspect(self.engine).get_columns("analysis_guards")}

    def test_migration_creates_the_exact_schema_on_an_empty_database(self):
        self.assertNotIn("analysis_guards", self._table_names())

        phase4_analysis_guards.run(engine=self.engine)

        self.assertIn("analysis_guards", self._table_names())
        self.assertEqual(
            self._column_names(),
            {"operation_type", "resource_id", "owner_token", "lock_expires_at", "cooldown_until"},
        )

    def test_migration_is_idempotent_on_second_run(self):
        phase4_analysis_guards.run(engine=self.engine)
        phase4_analysis_guards.run(engine=self.engine)  # must not raise

        self.assertIn("analysis_guards", self._table_names())
        self.assertEqual(
            self._column_names(),
            {"operation_type", "resource_id", "owner_token", "lock_expires_at", "cooldown_until"},
        )

    def test_migration_does_not_touch_unrelated_tables(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO unrelated (id) VALUES (1)"))

        phase4_analysis_guards.run(engine=self.engine)

        self.assertIn("unrelated", self._table_names())
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT id FROM unrelated")).fetchall()
        self.assertEqual([r[0] for r in rows], [1])

    def test_migration_does_not_destroy_existing_analysis_guards_data(self):
        phase4_analysis_guards.run(engine=self.engine)
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO analysis_guards "
                "(operation_type, resource_id, owner_token, lock_expires_at, cooldown_until) "
                "VALUES ('profile_analysis_profile', 1, 'tok', 1000.0, NULL)"
            ))

        phase4_analysis_guards.run(engine=self.engine)  # second run must not wipe data

        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT owner_token FROM analysis_guards")).fetchall()
        self.assertEqual([r[0] for r in rows], ["tok"])


if __name__ == "__main__":
    unittest.main()
