import os
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text

from backend.migrations import phase3_auth_fields


class Phase3AuthMigrationTests(unittest.TestCase):
    """Exercises backend/migrations/phase3_auth_fields.py against a throwaway,
    fully synthetic SQLite database. This never reads or copies apply101.db.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase3_migration_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        self._create_pre_migration_schema()
        self._seed_synthetic_users()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_pre_migration_schema(self):
        # Mirrors the users table shape as it existed before password_hash /
        # is_admin were added, plus one unrelated table to prove the
        # migration only ever touches `users`. Structure only, no real data.
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    mail VARCHAR UNIQUE NOT NULL,
                    skills VARCHAR,
                    experience_years FLOAT,
                    major VARCHAR,
                    master BOOLEAN,
                    phd BOOLEAN,
                    abitur BOOLEAN
                )
            """))
            connection.execute(text("""
                CREATE TABLE candidate_profiles (
                    profile_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    self_description VARCHAR
                )
            """))

    def _seed_synthetic_users(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO users
                    (user_id, name, mail, skills, experience_years, major,
                     master, phd, abitur)
                VALUES
                    (1, 'Synthetic User One', 'synthetic.one@example.invalid',
                     'python,sql', 2.5, 'CS', 0, 0, 1),
                    (2, 'Synthetic User Two', 'synthetic.two@example.invalid',
                     'java', 5.0, 'EE', 1, 0, 0),
                    (3, 'Synthetic User Three', 'synthetic.three@example.invalid',
                     NULL, NULL, NULL, 0, 0, 0)
            """))
            connection.execute(text("""
                INSERT INTO candidate_profiles
                    (profile_id, user_id, self_description)
                VALUES
                    (100, 1, 'unrelated row A'),
                    (101, 2, 'unrelated row B')
            """))

    def _users_columns(self):
        inspector = inspect(self.engine)
        return {column["name"] for column in inspector.get_columns("users")}

    def _dump_users_original_columns(self):
        with self.engine.connect() as connection:
            return connection.execute(text("""
                SELECT user_id, name, mail, skills, experience_years, major,
                       master, phd, abitur
                FROM users ORDER BY user_id
            """)).fetchall()

    def _dump_users_with_auth_columns(self):
        with self.engine.connect() as connection:
            return connection.execute(text("""
                SELECT user_id, name, mail, skills, experience_years, major,
                       master, phd, abitur, password_hash, is_admin
                FROM users ORDER BY user_id
            """)).fetchall()

    def _dump_candidate_profiles(self):
        with self.engine.connect() as connection:
            return connection.execute(text("""
                SELECT profile_id, user_id, self_description
                FROM candidate_profiles ORDER BY profile_id
            """)).fetchall()

    def test_migration_adds_columns_without_losing_existing_data(self):
        before_users = self._dump_users_original_columns()
        before_profiles = self._dump_candidate_profiles()
        self.assertEqual(len(before_users), 3)
        self.assertEqual(len(before_profiles), 2)

        phase3_auth_fields.run(engine=self.engine)

        columns = self._users_columns()
        self.assertIn("password_hash", columns)
        self.assertIn("is_admin", columns)

        after_users = self._dump_users_with_auth_columns()
        self.assertEqual(len(after_users), 3)

        for row in after_users:
            self.assertIsNone(row.password_hash)
            self.assertEqual(row.is_admin, 0)

        # Every pre-existing column/value for existing users is unchanged.
        self.assertEqual(before_users, self._dump_users_original_columns())

        # The migration must not touch any other table.
        self.assertEqual(before_profiles, self._dump_candidate_profiles())

    def test_migration_is_idempotent_on_second_run(self):
        phase3_auth_fields.run(engine=self.engine)
        first_run_users = self._dump_users_with_auth_columns()
        first_run_profiles = self._dump_candidate_profiles()

        # Must not raise and must not alter anything on a repeat run.
        phase3_auth_fields.run(engine=self.engine)

        second_run_users = self._dump_users_with_auth_columns()
        second_run_profiles = self._dump_candidate_profiles()

        self.assertEqual(first_run_users, second_run_users)
        self.assertEqual(first_run_profiles, second_run_profiles)

        for row in second_run_users:
            self.assertIsNone(row.password_hash)
            self.assertEqual(row.is_admin, 0)


if __name__ == "__main__":
    unittest.main()
