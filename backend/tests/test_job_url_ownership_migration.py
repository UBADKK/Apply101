import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError

from backend.app import models  # noqa: F401  (registers every table on Base)
from backend.app.database import Base
from backend.migrations import phase7_job_url_ownership as phase7


CATALOG_INDEX = "uq_jobs_catalog_url"
OWNER_INDEX = "uq_jobs_owner_url"
OLD_INDEX = "ix_jobs_url"

CATALOG_INDEX_DDL = (
    "CREATE UNIQUE INDEX uq_jobs_catalog_url ON jobs (url) "
    "WHERE created_by_user_id IS NULL"
)
OWNER_INDEX_DDL = (
    "CREATE UNIQUE INDEX uq_jobs_owner_url ON jobs (created_by_user_id, url) "
    "WHERE created_by_user_id IS NOT NULL"
)

# users/jobs tables and all jobs indexes exactly as Base.metadata.create_all
# produced them before the phase 7 model change (compiled from the previous
# models.py): url is NOT NULL with no inline UNIQUE, uniqueness comes only
# from CREATE UNIQUE INDEX ix_jobs_url. job_analysis is a reduced copy that
# only serves as a related table whose rows must survive untouched.
OLD_USERS_DDL = """
CREATE TABLE users (
	user_id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	mail VARCHAR NOT NULL,
	skills VARCHAR,
	experience_years FLOAT,
	major VARCHAR,
	master BOOLEAN,
	phd BOOLEAN,
	abitur BOOLEAN,
	password_hash VARCHAR,
	is_admin BOOLEAN DEFAULT 0 NOT NULL,
	token_key VARCHAR,
	PRIMARY KEY (user_id)
)
"""

OLD_JOBS_COLUMNS_DDL = """
	job_id INTEGER NOT NULL,
	title VARCHAR NOT NULL,
	company_name VARCHAR,
	location VARCHAR,
	url VARCHAR NOT NULL{url_suffix},
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
	updated_at DATETIME,
	created_by_user_id INTEGER,
	PRIMARY KEY (job_id),
	FOREIGN KEY(created_by_user_id) REFERENCES users (user_id)
"""

OLD_JOB_ANALYSIS_DDL = """
CREATE TABLE job_analysis (
	analysis_id INTEGER NOT NULL,
	job_id INTEGER NOT NULL,
	analysis_status VARCHAR,
	analysis_json TEXT,
	role_family VARCHAR,
	is_current BOOLEAN,
	created_at DATETIME,
	PRIMARY KEY (analysis_id),
	FOREIGN KEY(job_id) REFERENCES jobs (job_id)
)
"""

OLD_OTHER_INDEXES = (
    "CREATE UNIQUE INDEX ix_users_mail ON users (mail)",
    "CREATE INDEX ix_users_user_id ON users (user_id)",
    "CREATE INDEX ix_job_analysis_analysis_id ON job_analysis (analysis_id)",
)

OLD_JOBS_NON_UNIQUE_INDEXES = (
    "CREATE INDEX ix_jobs_created_by_user_id ON jobs (created_by_user_id)",
    "CREATE INDEX ix_jobs_job_id ON jobs (job_id)",
)

OLD_URL_INDEX_DDL = "CREATE UNIQUE INDEX ix_jobs_url ON jobs (url)"

CONFLICT_PREFIX = "Existing jobs would violate the new unique indexes"
CATALOG_CONFLICT_FRAGMENT = "duplicated catalog url value(s)"
OWNED_CONFLICT_FRAGMENT = "duplicated (created_by_user_id, url) pair(s)"


def _jobs_table_ddl(inline_unique_url=False):
    suffix = " UNIQUE" if inline_unique_url else ""
    return "CREATE TABLE jobs (" + OLD_JOBS_COLUMNS_DDL.format(url_suffix=suffix) + ")"


def _normalize_sql(sql):
    return " ".join(sql.split()) if sql is not None else None


class Phase7JobUrlOwnershipMigrationTests(unittest.TestCase):
    """Exercises backend/migrations/phase7_job_url_ownership.py against
    throwaway, fully synthetic file-based SQLite databases created in a
    temporary directory. Never reads, copies or writes apply101.db.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_phase7_migration_test_")
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
        return create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False},
        )

    def _fresh_create_all_engine(self):
        path = os.path.join(self.tmp_dir, "create_all.db")
        engine = self._make_engine(path)
        self._extra_engines.append(engine)
        Base.metadata.create_all(bind=engine)
        return engine

    def _create_schema(
        self,
        *,
        inline_unique_url=False,
        jobs_index_ddls=(OLD_URL_INDEX_DDL,),
        jobs_rows=None,
    ):
        """users + jobs + job_analysis like the pre-phase-7 create_all
        output; jobs URL indexes are configurable to build broken/unexpected
        variants. Synthetic rows only."""
        if jobs_rows is None:
            jobs_rows = [
                # (job_id, title, url, created_by_user_id)
                (10, "Catalog A", "https://example.invalid/a", None),
                (11, "Catalog B", "https://example.invalid/b", None),
                (12, "Owned C", "https://example.invalid/c", 1),
                (13, "Owned D", "https://example.invalid/d", 2),
                (14, "Owned E", "https://example.invalid/e", 1),
            ]
        with self.engine.begin() as connection:
            connection.execute(text(OLD_USERS_DDL))
            connection.execute(text(_jobs_table_ddl(inline_unique_url)))
            connection.execute(text(OLD_JOB_ANALYSIS_DDL))
            for ddl in OLD_OTHER_INDEXES + OLD_JOBS_NON_UNIQUE_INDEXES:
                connection.execute(text(ddl))
            for ddl in jobs_index_ddls:
                connection.execute(text(ddl))

            connection.execute(text(
                "INSERT INTO users (user_id, name, mail, is_admin) VALUES "
                "(1, 'Synthetic One', 'one@example.invalid', 0), "
                "(2, 'Synthetic Two', 'two@example.invalid', 0)"
            ))
            for job_id, title, url, owner in jobs_rows:
                connection.execute(
                    text(
                        "INSERT INTO jobs (job_id, title, company_name, location, "
                        "url, description_text, source_created_at, job_status, "
                        "source, created_by_user_id) VALUES (:job_id, :title, "
                        "'Synthetic Co', 'Berlin', :url, 'desc', 1700000000, "
                        "'active', 'synthetic', :owner)"
                    ),
                    {"job_id": job_id, "title": title, "url": url, "owner": owner},
                )
            connection.execute(text(
                "INSERT INTO job_analysis (analysis_id, job_id, analysis_status, "
                "analysis_json, role_family, is_current) VALUES "
                "(100, 10, 'completed', '{}', 'software', 1), "
                "(101, 12, 'completed', '{}', 'data', 1)"
            ))

    def _schema_snapshot(self, engine=None):
        engine = engine or self.engine
        with engine.connect() as connection:
            return connection.exec_driver_sql(
                "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()

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

    def _jobs_index_set(self, engine=None):
        engine = engine or self.engine
        with engine.connect() as connection:
            index_list = connection.exec_driver_sql(
                'PRAGMA index_list("jobs")'
            ).fetchall()
            result = set()
            for _seq, name, unique, origin, partial in index_list:
                columns = tuple(
                    row[2] for row in connection.exec_driver_sql(
                        f'PRAGMA index_info("{name}")'
                    ).fetchall()
                )
                sql = connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (name,),
                ).scalar()
                result.add(
                    (name, unique, origin, partial, columns, _normalize_sql(sql))
                )
            return result

    def _index_names(self, engine=None):
        return {entry[0] for entry in self._jobs_index_set(engine)}

    def _run(self, engine=None, dry_run=False):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            phase7.run(engine=engine or self.engine, dry_run=dry_run)
        return captured.getvalue()

    def _sha256(self, path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def _assert_aborts_without_changes(
        self, expected_messages=(), unexpected_messages=()
    ):
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()

        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    with self.assertRaises(phase7.MigrationAborted) as raised:
                        phase7.run(engine=self.engine, dry_run=dry_run)
                message = str(raised.exception)
                for expected in expected_messages:
                    self.assertIn(expected, message)
                for unexpected in unexpected_messages:
                    self.assertNotIn(unexpected, message)
                self.assertNotIn("example.invalid", message)
                self.assertNotIn("example.invalid", captured.getvalue())

                self.assertEqual(schema_before, self._schema_snapshot())
                self.assertEqual(data_before, self._data_snapshot())

    def _assert_url_rules(self, engine):
        insert = text(
            "INSERT INTO jobs (title, url, created_by_user_id) "
            "VALUES ('Rule job', :url, :owner)"
        )

        def add(url, owner):
            with engine.begin() as connection:
                connection.execute(insert, {"url": url, "owner": owner})

        base = "https://example.invalid/rules/"

        # Ownerless catalog jobs: url unique among catalog jobs.
        add(base + "catalog", None)
        with self.assertRaises(IntegrityError):
            add(base + "catalog", None)

        # The same user cannot own the same url twice.
        add(base + "owned", 1)
        with self.assertRaises(IntegrityError):
            add(base + "owned", 1)

        # A catalog job and a personal job may share a url (both orders).
        add(base + "catalog-first", None)
        add(base + "catalog-first", 1)
        add(base + "personal-first", 2)
        add(base + "personal-first", None)

        # Personal jobs of different users may share a url.
        add(base + "two-users", 1)
        add(base + "two-users", 2)

        # Exact string equality: no normalization.
        add(base + "Case", None)
        add(base + "case", None)

        with engine.connect() as connection:
            counts = dict(connection.exec_driver_sql(
                "SELECT url, COUNT(*) FROM jobs WHERE url LIKE ? GROUP BY url",
                (base + "%",),
            ).fetchall())
        self.assertEqual(
            counts,
            {
                base + "catalog": 1,
                base + "owned": 1,
                base + "catalog-first": 2,
                base + "personal-first": 2,
                base + "two-users": 2,
                base + "Case": 1,
                base + "case": 1,
            },
        )

    # -- AC1: fresh create_all ------------------------------------------

    def test_create_all_builds_partial_unique_indexes_and_no_global_url_index(self):
        engine = self._fresh_create_all_engine()

        with engine.connect() as connection:
            index_list = {
                row[1]: (row[2], row[3], row[4])
                for row in connection.exec_driver_sql(
                    'PRAGMA index_list("jobs")'
                ).fetchall()
            }
        self.assertEqual(
            set(index_list),
            {CATALOG_INDEX, OWNER_INDEX, "ix_jobs_job_id", "ix_jobs_created_by_user_id"},
        )
        self.assertNotIn(OLD_INDEX, index_list)
        # (unique, origin, partial)
        self.assertEqual(index_list[CATALOG_INDEX], (1, "c", 1))
        self.assertEqual(index_list[OWNER_INDEX], (1, "c", 1))
        self.assertEqual(index_list["ix_jobs_job_id"], (0, "c", 0))
        self.assertEqual(index_list["ix_jobs_created_by_user_id"], (0, "c", 0))

        index_set = self._jobs_index_set(engine)
        self.assertIn(
            (CATALOG_INDEX, 1, "c", 1, ("url",), _normalize_sql(CATALOG_INDEX_DDL)),
            index_set,
        )
        self.assertIn(
            (OWNER_INDEX, 1, "c", 1, ("created_by_user_id", "url"),
             _normalize_sql(OWNER_INDEX_DDL)),
            index_set,
        )
        self.assertIn(
            ("ix_jobs_job_id", 0, "c", 0, ("job_id",),
             "CREATE INDEX ix_jobs_job_id ON jobs (job_id)"),
            index_set,
        )
        self.assertIn(
            ("ix_jobs_created_by_user_id", 0, "c", 0, ("created_by_user_id",),
             "CREATE INDEX ix_jobs_created_by_user_id ON jobs (created_by_user_id)"),
            index_set,
        )

        with engine.connect() as connection:
            jobs_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
            ).scalar()
        self.assertNotIn("UNIQUE", jobs_sql.upper())

    def test_migration_is_no_op_on_create_all_schema(self):
        engine = self._fresh_create_all_engine()
        schema_before = self._schema_snapshot(engine)

        output = self._run(engine)

        self.assertIn("No changes made.", output)
        self.assertEqual(schema_before, self._schema_snapshot(engine))

    # -- AC2 + AC3: migrate old schema ----------------------------------

    def test_migration_matches_create_all_indexes_and_preserves_data(self):
        self._create_schema()
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()
        jobs_before = data_before["jobs"]

        output = self._run()

        self.assertIn(CATALOG_INDEX, output)
        self.assertIn(OWNER_INDEX, output)
        self.assertIn(OLD_INDEX, output)
        self.assertNotIn("example.invalid", output)

        create_all_engine = self._fresh_create_all_engine()
        self.assertEqual(
            self._jobs_index_set(), self._jobs_index_set(create_all_engine)
        )
        self.assertNotIn(OLD_INDEX, self._index_names())

        # Only the jobs URL indexes changed; everything else in sqlite_master
        # (tables, other indexes) is untouched.
        def without(snapshot, names):
            return [
                (type_, name, tbl_name, sql)
                for type_, name, tbl_name, _rootpage, sql in snapshot
                if name not in names
            ]

        self.assertEqual(
            without(schema_before, {OLD_INDEX}),
            without(self._schema_snapshot(), {CATALOG_INDEX, OWNER_INDEX}),
        )

        data_after = self._data_snapshot()
        self.assertEqual(data_before, data_after)
        self.assertEqual(
            [row[0] for row in jobs_before], [10, 11, 12, 13, 14]
        )
        self.assertEqual(jobs_before, data_after["jobs"])

    # -- AC4: idempotent second run -------------------------------------

    def test_second_run_changes_nothing(self):
        self._create_schema()
        self._run()
        schema_after_first = self._schema_snapshot()
        data_after_first = self._data_snapshot()

        output = self._run()

        self.assertIn("No changes made.", output)
        self.assertEqual(schema_after_first, self._schema_snapshot())
        self.assertEqual(data_after_first, self._data_snapshot())

    # -- AC5: conflicting rows ------------------------------------------

    def test_duplicate_owned_url_aborts_without_changes(self):
        self._create_schema(
            jobs_index_ddls=(),
            jobs_rows=[
                (10, "Catalog A", "https://example.invalid/a", None),
                (12, "Owned X 1", "https://example.invalid/x", 1),
                (13, "Owned X 2", "https://example.invalid/x", 1),
                (14, "Owned X other user", "https://example.invalid/x", 2),
            ],
        )
        self._assert_aborts_without_changes(
            expected_messages=(
                CONFLICT_PREFIX,
                "1 duplicated (created_by_user_id, url) pair(s) "
                "across 2 owned job row(s)",
            ),
            unexpected_messages=(CATALOG_CONFLICT_FRAGMENT,),
        )

    def test_duplicate_catalog_url_aborts_without_changes(self):
        self._create_schema(
            jobs_index_ddls=(),
            jobs_rows=[
                (10, "Catalog X 1", "https://example.invalid/x", None),
                (11, "Catalog X 2", "https://example.invalid/x", None),
                (12, "Owned X", "https://example.invalid/x", 1),
            ],
        )
        self._assert_aborts_without_changes(
            expected_messages=(
                CONFLICT_PREFIX,
                "1 duplicated catalog url value(s) across 2 ownerless job row(s)",
            ),
            unexpected_messages=(OWNED_CONFLICT_FRAGMENT,),
        )

    def test_duplicate_catalog_and_owned_urls_report_both(self):
        self._create_schema(
            jobs_index_ddls=(),
            jobs_rows=[
                (10, "Catalog X 1", "https://example.invalid/x", None),
                (11, "Catalog X 2", "https://example.invalid/x", None),
                (12, "Catalog X 3", "https://example.invalid/x", None),
                (13, "Owned Y 1", "https://example.invalid/y", 1),
                (14, "Owned Y 2", "https://example.invalid/y", 1),
            ],
        )
        self._assert_aborts_without_changes(
            expected_messages=(
                CONFLICT_PREFIX,
                "1 duplicated catalog url value(s) across 3 ownerless job row(s)",
                "1 duplicated (created_by_user_id, url) pair(s) "
                "across 2 owned job row(s)",
            ),
        )

    def test_no_url_index_without_duplicates_aborts_without_changes(self):
        self._create_schema(jobs_index_ddls=())
        self._assert_aborts_without_changes()

    # -- AC6: unexpected schema -----------------------------------------

    def test_inline_unique_url_aborts_without_changes(self):
        for jobs_index_ddls in ((), (OLD_URL_INDEX_DDL,)):
            with self.subTest(jobs_index_ddls=jobs_index_ddls):
                self.engine.dispose()
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
                self._create_schema(
                    inline_unique_url=True, jobs_index_ddls=jobs_index_ddls
                )
                self._assert_aborts_without_changes()

    def test_non_unique_old_url_index_aborts_without_changes(self):
        self._create_schema(
            jobs_index_ddls=("CREATE INDEX ix_jobs_url ON jobs (url)",)
        )
        self._assert_aborts_without_changes()

    def test_old_url_index_on_wrong_columns_aborts_without_changes(self):
        for ddl in (
            "CREATE UNIQUE INDEX ix_jobs_url ON jobs (title)",
            "CREATE UNIQUE INDEX ix_jobs_url ON jobs (url, title)",
            "CREATE UNIQUE INDEX ix_jobs_url ON jobs (url) WHERE url IS NOT NULL",
            "CREATE UNIQUE INDEX ix_jobs_url ON jobs (url COLLATE NOCASE)",
        ):
            with self.subTest(ddl=ddl):
                self.engine.dispose()
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
                self._create_schema(jobs_index_ddls=(ddl,))
                self._assert_aborts_without_changes()

    def test_target_index_name_with_different_definition_aborts_without_changes(self):
        variants = (
            # Old state plus a non-unique index squatting a target name.
            (OLD_URL_INDEX_DDL, "CREATE INDEX uq_jobs_owner_url ON jobs (created_by_user_id, url)"),
            # Looks migrated, but catalog index is not partial.
            ("CREATE UNIQUE INDEX uq_jobs_catalog_url ON jobs (url)", OWNER_INDEX_DDL),
            # Looks migrated, but catalog index has the wrong predicate.
            (
                "CREATE UNIQUE INDEX uq_jobs_catalog_url ON jobs (url) "
                "WHERE created_by_user_id IS NOT NULL",
                OWNER_INDEX_DDL,
            ),
            # Looks migrated, but owner index lacks created_by_user_id.
            (
                CATALOG_INDEX_DDL,
                "CREATE UNIQUE INDEX uq_jobs_owner_url ON jobs (url) "
                "WHERE created_by_user_id IS NOT NULL",
            ),
        )
        for ddls in variants:
            with self.subTest(ddls=ddls):
                self.engine.dispose()
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
                self._create_schema(jobs_index_ddls=ddls)
                self._assert_aborts_without_changes()

    def test_mixed_state_aborts_without_changes(self):
        variants = (
            (OLD_URL_INDEX_DDL, CATALOG_INDEX_DDL, OWNER_INDEX_DDL),
            (OLD_URL_INDEX_DDL, CATALOG_INDEX_DDL),
            (OLD_URL_INDEX_DDL, OWNER_INDEX_DDL),
            (CATALOG_INDEX_DDL,),
            (OWNER_INDEX_DDL,),
        )
        for ddls in variants:
            with self.subTest(ddls=ddls):
                self.engine.dispose()
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
                self._create_schema(jobs_index_ddls=ddls)
                self._assert_aborts_without_changes()

    def test_unexpected_extra_unique_index_aborts_without_changes(self):
        self._create_schema(
            jobs_index_ddls=(
                OLD_URL_INDEX_DDL,
                "CREATE UNIQUE INDEX ix_jobs_title ON jobs (title)",
            )
        )
        self._assert_aborts_without_changes()

    def test_jobs_table_without_owner_column_aborts_without_changes(self):
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE jobs (job_id INTEGER NOT NULL, "
                "url VARCHAR NOT NULL, PRIMARY KEY (job_id))"
            ))
            connection.execute(text(OLD_URL_INDEX_DDL))
        self._assert_aborts_without_changes()

    def test_missing_jobs_table_aborts_without_changes(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
        self._assert_aborts_without_changes()

    # -- AC7: failure mid-transaction -----------------------------------

    def test_failure_after_create_rolls_back_everything(self):
        self._create_schema()
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()
        seen_inside_transaction = []

        def failing_drop(connection):
            seen_inside_transaction.extend(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'jobs'"
                ).scalars().all()
            )
            raise RuntimeError("simulated failure after creating indexes")

        with mock.patch.object(phase7, "_drop_old_index", side_effect=failing_drop):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                    phase7.run(engine=self.engine)

        # The new indexes really were created before the failure ...
        self.assertIn(CATALOG_INDEX, seen_inside_transaction)
        self.assertIn(OWNER_INDEX, seen_inside_transaction)

        # ... and a brand-new connection sees none of it.
        self.engine.dispose()
        fresh_engine = self._make_engine(self.db_path)
        self._extra_engines.append(fresh_engine)
        self.assertEqual(schema_before, self._schema_snapshot(fresh_engine))
        self.assertEqual(data_before, self._data_snapshot(fresh_engine))
        names = self._index_names(fresh_engine)
        self.assertIn(OLD_INDEX, names)
        self.assertNotIn(CATALOG_INDEX, names)
        self.assertNotIn(OWNER_INDEX, names)

    # -- AC8: dry run ----------------------------------------------------

    def test_dry_run_on_old_schema_changes_nothing(self):
        self._create_schema()
        schema_before = self._schema_snapshot()
        data_before = self._data_snapshot()
        self.engine.dispose()
        hash_before = self._sha256(self.db_path)

        statements = []

        @event.listens_for(self.engine, "before_cursor_execute")
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        output = self._run(dry_run=True)

        self.assertIn(CATALOG_INDEX, output)
        self.assertIn(OWNER_INDEX, output)
        self.assertIn(OLD_INDEX, output)
        self.assertNotIn("example.invalid", output)
        for statement in statements:
            first_word = statement.strip().split()[0].upper()
            self.assertNotIn(
                first_word,
                {"BEGIN", "CREATE", "DROP", "INSERT", "UPDATE", "DELETE", "ALTER"},
                statement,
            )

        self.engine.dispose()
        self.assertEqual(hash_before, self._sha256(self.db_path))
        self.assertEqual(schema_before, self._schema_snapshot())
        self.assertEqual(data_before, self._data_snapshot())
        self.assertIn(OLD_INDEX, self._index_names())

    def test_dry_run_on_migrated_schema_reports_no_changes(self):
        self._create_schema()
        self._run()
        self.engine.dispose()
        hash_before = self._sha256(self.db_path)

        output = self._run(dry_run=True)

        self.assertIn("No changes needed", output)
        self.engine.dispose()
        self.assertEqual(hash_before, self._sha256(self.db_path))

    # -- AC9: URL rules --------------------------------------------------

    def test_url_rules_on_migrated_database(self):
        self._create_schema()
        self._run()
        self._assert_url_rules(self.engine)

    def test_url_rules_on_create_all_database(self):
        engine = self._fresh_create_all_engine()
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (user_id, name, mail) VALUES "
                "(1, 'Synthetic One', 'one@example.invalid'), "
                "(2, 'Synthetic Two', 'two@example.invalid')"
            ))
        self._assert_url_rules(engine)

    # -- CLI -------------------------------------------------------------

    def test_cli_refuses_missing_database_file(self):
        missing_path = os.path.join(self.tmp_dir, "does_not_exist.db")
        missing_engine = self._make_engine(missing_path)
        self._extra_engines.append(missing_engine)

        for argv in ([], ["--dry-run"]):
            with self.subTest(argv=argv):
                with mock.patch.object(phase7, "default_engine", missing_engine):
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        exit_code = phase7.main(argv)
                self.assertNotEqual(exit_code, 0)
                self.assertFalse(os.path.exists(missing_path))

    def test_cli_dry_run_is_read_only_and_cli_applies(self):
        self._create_schema()
        self.engine.dispose()
        hash_before = self._sha256(self.db_path)

        with mock.patch.object(phase7, "default_engine", self.engine):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                exit_code = phase7.main(["--dry-run"])
            self.assertEqual(exit_code, 0)
            self.assertIn(OLD_INDEX, captured.getvalue())
            self.engine.dispose()
            self.assertEqual(hash_before, self._sha256(self.db_path))

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = phase7.main([])
            self.assertEqual(exit_code, 0)

        self.assertEqual(
            self._index_names(),
            {CATALOG_INDEX, OWNER_INDEX, "ix_jobs_job_id", "ix_jobs_created_by_user_id"},
        )

    def test_cli_returns_non_zero_on_abort(self):
        self._create_schema(jobs_index_ddls=())
        schema_before = self._schema_snapshot()
        self.engine.dispose()

        for argv in ([], ["--dry-run"]):
            with self.subTest(argv=argv):
                with mock.patch.object(phase7, "default_engine", self.engine):
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        exit_code = phase7.main(argv)
                self.assertNotEqual(exit_code, 0)
                self.assertEqual(schema_before, self._schema_snapshot())


if __name__ == "__main__":
    unittest.main()
