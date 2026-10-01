import contextlib
import io
import os
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text

from backend.app.database import Base
from backend.migrations import phase6_user_token_key


class Phase6UserTokenKeyMigrationTests(unittest.TestCase):
    """Exercises backend/migrations/phase6_user_token_key.py against a
    throwaway, fully synthetic SQLite database. This never reads or copies
    apply101.db.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase6_migration_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- helpers -------------------------------------------------------

    def _create_pre_migration_schema(self):
        # The users table as it existed after phase 3 (password_hash /
        # is_admin) but before token_key, plus one unrelated table to prove
        # the migration only ever touches users.token_key. No real data.
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
                    abitur BOOLEAN,
                    password_hash VARCHAR,
                    is_admin BOOLEAN NOT NULL DEFAULT 0
                )
            """))
            connection.execute(text("""
                CREATE TABLE candidate_profiles (
                    profile_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    self_description VARCHAR
                )
            """))
            connection.execute(text("""
                INSERT INTO users
                    (user_id, name, mail, skills, experience_years, major,
                     master, phd, abitur, password_hash, is_admin)
                VALUES
                    (1, 'Synthetic User One', 'synthetic.one@example.invalid',
                     'python,sql', 2.5, 'CS', 0, 0, 1, 'synthetic-hash-1', 1),
                    (2, 'Synthetic User Two', 'synthetic.two@example.invalid',
                     'java', 5.0, 'EE', 1, 0, 0, NULL, 0),
                    (5, 'Synthetic User Five', 'synthetic.five@example.invalid',
                     NULL, NULL, NULL, 0, 0, 0, 'synthetic-hash-5', 0)
            """))
            connection.execute(text("""
                INSERT INTO candidate_profiles
                    (profile_id, user_id, self_description)
                VALUES
                    (100, 1, 'unrelated row A'),
                    (101, 2, 'unrelated row B')
            """))

    def _run_migration(self):
        # Swallow the migration's summary output; it must never contain a
        # key (checked explicitly in a dedicated test).
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            phase6_user_token_key.run(engine=self.engine)
        return captured.getvalue()

    def _column_shape(self, table):
        # SQLAlchemy type objects don't compare by value; compare their
        # rendered form instead.
        return [
            (column["name"], str(column["type"]), column["nullable"],
             column["default"], column["primary_key"])
            for column in inspect(self.engine).get_columns(table)
        ]

    def _users_columns(self):
        return [column["name"] for column in inspect(self.engine).get_columns("users")]

    def _dump_users_original_columns(self):
        with self.engine.connect() as connection:
            return connection.execute(text("""
                SELECT user_id, name, mail, skills, experience_years, major,
                       master, phd, abitur, password_hash, is_admin
                FROM users ORDER BY user_id
            """)).fetchall()

    def _token_keys(self):
        with self.engine.connect() as connection:
            return dict(connection.execute(text(
                "SELECT user_id, token_key FROM users ORDER BY user_id"
            )).fetchall())

    def _dump_candidate_profiles(self):
        with self.engine.connect() as connection:
            return connection.execute(text("""
                SELECT profile_id, user_id, self_description
                FROM candidate_profiles ORDER BY profile_id
            """)).fetchall()

    def _assert_all_keys_filled_and_distinct(self, keys):
        for user_id, key in keys.items():
            self.assertIsInstance(key, str, f"user_id={user_id}")
            self.assertTrue(key, f"user_id={user_id}")
        self.assertEqual(len(set(keys.values())), len(keys))

    # -- tests -----------------------------------------------------------

    def test_adds_column_and_fills_every_row_with_a_distinct_key(self):
        self._create_pre_migration_schema()
        columns_before = self._users_columns()
        before_users = self._dump_users_original_columns()
        before_profiles = self._dump_candidate_profiles()
        users_shape_before = self._column_shape("users")
        profile_columns_before = self._column_shape("candidate_profiles")
        self.assertNotIn("token_key", columns_before)

        self._run_migration()

        self.assertEqual(self._users_columns(), columns_before + ["token_key"])
        users_shape_after = self._column_shape("users")
        self.assertEqual(users_shape_after[:-1], users_shape_before)
        self.assertEqual(
            users_shape_after[-1], ("token_key", "VARCHAR", True, None, 0)
        )
        keys = self._token_keys()
        self.assertEqual(set(keys), {1, 2, 5})
        self._assert_all_keys_filled_and_distinct(keys)

        # Every other users column/value, and every other table, unchanged.
        self.assertEqual(before_users, self._dump_users_original_columns())
        self.assertEqual(before_profiles, self._dump_candidate_profiles())
        self.assertEqual(
            profile_columns_before, self._column_shape("candidate_profiles")
        )
        self.assertEqual(
            set(inspect(self.engine).get_table_names()),
            {"users", "candidate_profiles"},
        )

    def test_second_run_keeps_existing_keys_unchanged(self):
        self._create_pre_migration_schema()
        self._run_migration()
        first_keys = self._token_keys()
        first_users = self._dump_users_original_columns()
        first_profiles = self._dump_candidate_profiles()

        self._run_migration()

        self.assertEqual(first_keys, self._token_keys())
        self.assertEqual(first_users, self._dump_users_original_columns())
        self.assertEqual(first_profiles, self._dump_candidate_profiles())
        self.assertEqual(self._users_columns().count("token_key"), 1)

    def test_second_run_fills_rows_left_null_or_empty_in_between(self):
        self._create_pre_migration_schema()
        self._run_migration()
        first_keys = self._token_keys()

        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO users (user_id, name, mail, is_admin, token_key)
                VALUES
                    (7, 'Synthetic Null Key', 'synthetic.null@example.invalid', 0, NULL),
                    (8, 'Synthetic Empty Key', 'synthetic.empty@example.invalid', 0, '')
            """))

        self._run_migration()

        second_keys = self._token_keys()
        for user_id, key in first_keys.items():
            self.assertEqual(second_keys[user_id], key)
        self.assertEqual(set(second_keys), {1, 2, 5, 7, 8})
        self._assert_all_keys_filled_and_distinct(second_keys)

    def test_create_all_schema_is_a_no_op_for_the_column(self):
        Base.metadata.create_all(bind=self.engine)
        columns_before = self._users_columns()
        self.assertIn("token_key", columns_before)

        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO users (user_id, name, mail, is_admin, token_key)
                VALUES (1, 'Synthetic Keyed', 'synthetic.keyed@example.invalid',
                        0, 'synthetic-existing-key-0123456789')
            """))

        output = self._run_migration()

        self.assertEqual(self._users_columns(), columns_before)
        self.assertEqual(
            self._token_keys(), {1: "synthetic-existing-key-0123456789"}
        )
        self.assertIn("No changes made", output)

    def test_output_never_contains_a_token_key(self):
        self._create_pre_migration_schema()
        output = self._run_migration()

        self.assertTrue(output)
        for key in self._token_keys().values():
            self.assertNotIn(key, output)


if __name__ == "__main__":
    unittest.main()
