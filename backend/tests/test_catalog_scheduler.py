"""The daily catalog sync of the test deployment (catalog_scheduler) on
throwaway SQLite files, with fixtures instead of the network."""

import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import catalog_scheduler, catalog_sync, models
from backend.app.catalog_scheduler import SchedulerSettings
from backend.app.config import ConfigError
from backend.app.database import Base
from backend.app.job_sources import HttpClient
from backend.tests.catalog_fixtures import FakeResponse, FakeSession, greenhouse_board, greenhouse_jobs


GH_JOBS = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/acme"
SETTINGS = SchedulerSettings(enabled=True, hour_utc=3, max_boards=5, max_pages=2)
DUE = datetime(2026, 11, 2, 4, 0)


class SchedulerSettingsTests(unittest.TestCase):
    def test_off_by_default(self):
        settings = catalog_scheduler.settings_from_env({})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.hour_utc, 3)
        self.assertEqual(settings.max_boards, catalog_sync.DEFAULT_MAX_BOARDS)
        self.assertEqual(settings.max_pages, catalog_sync.DEFAULT_MAX_PAGES)

    def test_reads_bounds(self):
        settings = catalog_scheduler.settings_from_env({
            "APPLY101_CATALOG_SYNC_ENABLED": "1",
            "APPLY101_CATALOG_SYNC_HOUR_UTC": "5",
            "APPLY101_CATALOG_SYNC_MAX_BOARDS": "3",
            "APPLY101_CATALOG_SYNC_MAX_PAGES": "4",
        })
        self.assertEqual(settings, SchedulerSettings(True, 5, 3, 4))

    def test_refuses_bad_values(self):
        for env in (
            {"APPLY101_CATALOG_SYNC_ENABLED": "maybe"},
            {"APPLY101_CATALOG_SYNC_HOUR_UTC": "24"},
            {"APPLY101_CATALOG_SYNC_MAX_BOARDS": str(catalog_sync.MAX_BOARDS_LIMIT + 1)},
            {"APPLY101_CATALOG_SYNC_MAX_PAGES": "0"},
            {"APPLY101_CATALOG_SYNC_MAX_PAGES": "ten"},
        ):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                catalog_scheduler.settings_from_env(env)


class DailyRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_scheduler_test_")
        self.engine = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}",
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.http_calls = []

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def http_factory(self):
        session = FakeSession({GH_JOBS: FakeResponse(200, greenhouse_jobs()), GH_BOARD: FakeResponse(200, greenhouse_board())})
        self.http_calls.append(session)
        return HttpClient(session, sleep=lambda s: None)

    def add_board(self, enabled=True):
        db = self.Session()
        board, _ = catalog_sync.create_board(db, company_name="Acme", source="greenhouse", board_token="acme",
                                             region="global", now=catalog_sync.utcnow())
        board.is_enabled = enabled
        db.commit()
        db.close()

    def run_once(self, now=DUE, **kwargs):
        kwargs.setdefault("http_factory", self.http_factory)
        return catalog_scheduler.run_daily_once(self.Session, SETTINGS, now=now, **kwargs)

    def count(self, model):
        db = self.Session()
        try:
            return db.query(model).count()
        finally:
            db.close()

    def test_syncs_enabled_boards_once_a_day(self):
        self.add_board()
        result = self.run_once()
        self.assertEqual(result["boards_synced"], 1)
        self.assertGreater(self.count(models.Job), 0)
        # A restart later the same day: claimed, nothing is fetched again.
        self.assertIsNone(self.run_once(now=DUE.replace(hour=20)))
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(self.count(models.JobFetchRun), 1)
        # The next day runs again.
        self.assertEqual(self.run_once(now=datetime(2026, 11, 3, 3, 0))["boards_synced"], 1)

    def test_waits_for_the_hour(self):
        self.add_board()
        self.assertIsNone(self.run_once(now=DUE.replace(hour=2, minute=59)))
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.count(models.AnalysisGuard), 0)

    def test_disabled_boards_and_arbeitnow_are_never_synced(self):
        self.add_board(enabled=False)
        seen = {}

        def spy_run_sync(db, http, **kwargs):
            seen.update(kwargs)
            return catalog_sync.run_sync(db, http, **kwargs)

        result = self.run_once(run_sync=spy_run_sync)
        self.assertEqual(result["boards_synced"], 0)
        self.assertEqual(self.count(models.Job), 0)
        self.assertFalse(seen["include_arbeitnow"])
        self.assertNotIn("acknowledge_arbeitnow_terms", seen)
        self.assertEqual((seen["max_boards"], seen["max_pages"]), (5, 2))

    def test_two_processes_claim_the_day_once(self):
        """Two schedulers (separate sessions, as in two processes) racing on
        the same database: exactly one runs."""
        self.add_board()
        barrier = threading.Barrier(2)
        results = []

        def run():
            barrier.wait()
            results.append(self.run_once())

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(r is None for r in results), [False, True])
        self.assertEqual(self.count(models.JobFetchRun), 1)

    def test_a_running_manual_sync_is_not_doubled(self):
        self.add_board()
        db = self.Session()
        catalog_sync.acquire_sync_lock(db)
        db.close()
        self.assertEqual(self.run_once(), {"skipped": "sync_running"})
        self.assertEqual(self.count(models.JobFetchRun), 0)

    def test_scheduler_thread_starts_only_when_enabled(self):
        off = catalog_scheduler.CatalogScheduler(self.Session, SchedulerSettings(False, 3, 1, 1))
        self.assertFalse(off.start())
        self.add_board()
        ran = threading.Event()

        def fake_run_sync(db, http, **kwargs):
            ran.set()
            return {"boards_synced": 0, "stopped_reason": None}

        on = catalog_scheduler.CatalogScheduler(self.Session, SETTINGS, poll_seconds=0.05,
                                                http_factory=self.http_factory, run_sync=fake_run_sync,
                                                now=DUE)
        self.assertTrue(on.start())
        self.assertFalse(on.start())
        self.assertTrue(ran.wait(5))
        on.stop()

    def test_scheduler_thread_survives_a_failed_run(self):
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("network down")

        scheduler = catalog_scheduler.CatalogScheduler(self.Session, SETTINGS, poll_seconds=0.01,
                                                       run_sync=boom, now=DUE)
        with self.assertLogs("apply101.catalog_scheduler", level="ERROR"):
            scheduler.start()
            deadline = datetime.now().timestamp() + 5
            while not calls and datetime.now().timestamp() < deadline:
                threading.Event().wait(0.01)
            scheduler.stop()
        # The day was claimed before the failed run, so it is not retried.
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
