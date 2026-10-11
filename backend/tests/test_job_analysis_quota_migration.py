"""backend/migrations/phase8_analysis_quota_reservations.py against
throwaway, fully synthetic file-based SQLite databases in a temporary
directory. Never reads, copies or writes apply101.db.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine, text

from backend.app import models  # noqa: F401  (registers every table on Base)
from backend.app.database import Base
from backend.migrations import phase8_analysis_quota_reservations as phase8


QUOTA_TABLE = "analysis_quota_reservations"
QUOTA_INDEX = "ix_analysis_quota_reservations_op_user_reserved_at"
EXPECTED_COLUMNS = ("id", "operation_type", "user_id", "job_id", "reserved_at")
# Distinctive synthetic value that must never appear in migration output.
SENTINEL = "synthetic-sentinel-value-0f3c"


def _normalize_sql(sql):
    return " ".join(sql.split()) if sql is not None else None


class Phase8AnalysisQuotaReservationsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase8_migration_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = self._make_engine(self.db_path)
        self._extra_engines = []

    def tearDown(self):
        self.engine.dispose()
        for engine in self._extra_engines:
            engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _make_engine(path):
        return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    def _fresh_create_all_engine(self):
        engine = self._make_engine(os.path.join(self.tmp_dir, "create_all.db"))
        self._extra_engines.append(engine)
        Base.metadata.create_all(bind=engine)
        return engine

    def _create_pre_phase8_schema(self):
        """The current schema minus the quota table, with synthetic rows in
        other tables."""
        tables = [t for t in Base.metadata.sorted_tables if t.name != QUOTA_TABLE]
        Base.metadata.create_all(bind=self.engine, tables=tables)
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (user_id, name, mail, is_admin, token_key) VALUES "
                f"(1, 'Synthetic One', 'one@example.invalid', 0, '{SENTINEL}'), "
                "(2, 'Synthetic Two', 'two@example.invalid', 1, 'tk-2')"
            ))
            connection.execute(text(
                "INSERT INTO jobs (job_id, title, url, description_text, source, "
                "created_by_user_id) VALUES "
                "(10, 'Catalog', 'https://example.invalid/a', 'desc', 'arbeitnow', NULL), "
                "(11, 'Owned', 'https://example.invalid/b', 'desc', 'manual', 1)"
            ))
            connection.execute(text(
                "INSERT INTO job_analysis (analysis_id, job_id, analysis_status, is_current) "
                "VALUES (100, 10, 'completed', 1)"
            ))
            connection.execute(text(
                "INSERT INTO analysis_guards (operation_type, resource_id, owner_token, "
                "lock_expires_at, cooldown_until) VALUES "
                "('job_analysis_user', 1, NULL, NULL, 1234.5), "
                "('job_analysis', 11, 'tok', 999.0, NULL)"
            ))

    def _schema_snapshot(self, engine=None):
        engine = engine or self.engine
        with engine.connect() as connection:
            return connection.exec_driver_sql(
                "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()

    def _quota_objects(self, engine=None):
        """Normalized DDL of the quota table and its indexes, plus their
        PRAGMA column/index descriptions."""
        engine = engine or self.engine
        with engine.connect() as connection:
            ddl = {
                (row[0], row[1]): _normalize_sql(row[2])
                for row in connection.exec_driver_sql(
                    "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ?",
                    (QUOTA_TABLE,),
                ).fetchall()
            }
            table_info = connection.exec_driver_sql(
                f'PRAGMA table_info("{QUOTA_TABLE}")'
            ).fetchall()
            indexes = set()
            for _seq, name, unique, origin, partial in connection.exec_driver_sql(
                f'PRAGMA index_list("{QUOTA_TABLE}")'
            ).fetchall():
                columns = tuple(
                    row[2] for row in connection.exec_driver_sql(
                        f'PRAGMA index_info("{name}")'
                    ).fetchall()
                )
                indexes.add((name, unique, origin, partial, columns))
        return {"ddl": ddl, "table_info": table_info, "indexes": indexes}

    def _data_snapshot(self, engine=None):
        engine = engine or self.engine
        with engine.connect() as connection:
            tables = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).scalars().all()
            return {
                table: connection.exec_driver_sql(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            }

    def _without_quota(self, snapshot):
        return [row for row in snapshot if row[2] != QUOTA_TABLE]

    def _run(self, engine=None):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            phase8.run(engine=engine or self.engine)
        return captured.getvalue()

    # -- tests -------------------------------------------------------------

    def test_creates_table_and_index_and_preserves_other_data(self):
        self._create_pre_phase8_schema()
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()
        self.assertNotIn(QUOTA_TABLE, data_before)

        output = self._run()

        self.assertIn(QUOTA_TABLE, output)
        self.assertIn(QUOTA_INDEX, output)
        self.assertNotIn(SENTINEL, output)
        objects = self._quota_objects()
        self.assertIn(("table", QUOTA_TABLE), objects["ddl"])
        self.assertIn(("index", QUOTA_INDEX), objects["ddl"])
        self.assertEqual(tuple(row[1] for row in objects["table_info"]), EXPECTED_COLUMNS)
        self.assertIn(
            (QUOTA_INDEX, 0, "c", 0, ("operation_type", "user_id", "reserved_at")),
            objects["indexes"],
        )
        # Other objects (incl. rootpage) and all existing rows unchanged.
        self.assertEqual(self._without_quota(self._schema_snapshot()), schema_before)
        data_after = self._data_snapshot()
        self.assertEqual(data_after.pop(QUOTA_TABLE), [])
        self.assertEqual(data_after, data_before)

    def test_result_matches_create_all(self):
        self._create_pre_phase8_schema()
        self._run()
        self.assertEqual(self._quota_objects(), self._quota_objects(self._fresh_create_all_engine()))

    def test_second_run_changes_nothing(self):
        self._create_pre_phase8_schema()
        self._run()
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO analysis_quota_reservations "
                "(operation_type, user_id, job_id, reserved_at) "
                f"VALUES ('{SENTINEL}', 1, 11, 1000.5)"
            ))
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()

        output = self._run()

        self.assertIn("No changes made.", output)
        self.assertNotIn(SENTINEL, output)
        self.assertEqual(self._schema_snapshot(), schema_before)
        self.assertEqual(self._data_snapshot(), data_before)

    def test_no_op_on_create_all_database(self):
        engine = self._fresh_create_all_engine()
        schema_before = self._schema_snapshot(engine)
        output = self._run(engine)
        self.assertIn("No changes made.", output)
        self.assertEqual(self._schema_snapshot(engine), schema_before)

    def test_existing_table_without_index_gets_the_index(self):
        engine = self._fresh_create_all_engine()
        expected = self._quota_objects(engine)

        self._create_pre_phase8_schema()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(expected["ddl"][("table", QUOTA_TABLE)])
            connection.execute(text(
                "INSERT INTO analysis_quota_reservations "
                "(operation_type, user_id, job_id, reserved_at) "
                f"VALUES ('{SENTINEL}', 1, 11, 1000.5)"
            ))
        self.assertEqual(self._quota_objects()["indexes"], set())
        data_before = self._data_snapshot()

        output = self._run()

        self.assertIn(QUOTA_INDEX, output)
        self.assertNotIn(SENTINEL, output)
        self.assertEqual(self._quota_objects(), expected)
        self.assertEqual(self._data_snapshot(), data_before)

    def test_existing_table_with_missing_columns_aborts_without_changes(self):
        self._create_pre_phase8_schema()
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE analysis_quota_reservations ("
                "id INTEGER NOT NULL PRIMARY KEY, operation_type VARCHAR NOT NULL, "
                "user_id INTEGER NOT NULL)"
            ))
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(phase8.MigrationAborted):
                phase8.run(engine=self.engine)

        self.assertEqual(self._schema_snapshot(), schema_before)
        self.assertEqual(self._data_snapshot(), data_before)

    def test_index_name_with_other_columns_aborts_without_changes(self):
        expected = self._quota_objects(self._fresh_create_all_engine())

        self._create_pre_phase8_schema()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(expected["ddl"][("table", QUOTA_TABLE)])
            connection.exec_driver_sql(
                f'CREATE INDEX "{QUOTA_INDEX}" ON "{QUOTA_TABLE}" (user_id, job_id)'
            )
        self.assertEqual(
            tuple(row[1] for row in self._quota_objects()["table_info"]), EXPECTED_COLUMNS
        )
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(phase8.MigrationAborted):
                phase8.run(engine=self.engine)

        self.assertEqual(self._schema_snapshot(), schema_before)
        self.assertEqual(self._data_snapshot(), data_before)

    # -- CLI -------------------------------------------------------------

    def test_cli_refuses_missing_database_file(self):
        missing_path = os.path.join(self.tmp_dir, "does_not_exist.db")
        missing_engine = self._make_engine(missing_path)
        self._extra_engines.append(missing_engine)

        with mock.patch.object(phase8, "default_engine", missing_engine):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                exit_code = phase8.main([])
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(os.path.exists(missing_path))

    def test_cli_applies_on_existing_database(self):
        self._create_pre_phase8_schema()
        with mock.patch.object(phase8, "default_engine", self.engine):
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = phase8.main([])
        self.assertEqual(exit_code, 0)
        self.assertIn(("index", QUOTA_INDEX), self._quota_objects()["ddl"])

    def test_cli_returns_non_zero_on_abort(self):
        self._create_pre_phase8_schema()
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE analysis_quota_reservations (id INTEGER NOT NULL PRIMARY KEY)"
            ))
        schema_before = self._schema_snapshot()
        with mock.patch.object(phase8, "default_engine", self.engine):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                exit_code = phase8.main([])
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(self._schema_snapshot(), schema_before)


if __name__ == "__main__":
    unittest.main()
