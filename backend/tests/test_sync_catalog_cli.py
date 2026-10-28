"""backend.scripts.sync_catalog on a throwaway SQLite file (never
apply101.db), with fixtures instead of the network."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models
from backend.app.database import Base
from backend.app.job_sources import HttpClient
from backend.scripts import sync_catalog
from backend.tests.catalog_fixtures import FakeResponse, FakeSession, greenhouse_board, greenhouse_jobs


class SyncCatalogCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_sync_cli_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_cli(self, argv, engine=None, routes=None):
        out = io.StringIO()
        factory = lambda: HttpClient(FakeSession(routes or {}), sleep=lambda s: None)
        with contextlib.redirect_stdout(out):
            code = sync_catalog.main(argv, engine=engine or self.engine, http_factory=factory)
        return code, out.getvalue()

    def test_writes_need_yes(self):
        code, out = self.run_cli(["seed"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        session = sessionmaker(bind=self.engine)()
        self.assertEqual(session.query(models.JobSourceBoard).count(), 0)
        session.close()

    def test_arbeitnow_needs_acknowledgement(self):
        code, out = self.run_cli(["sync", "--include-arbeitnow", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("acknowledge_arbeitnow_terms", out)
        routes = {"https://www.arbeitnow.com/api/job-board-api?page=1": FakeResponse(200, {"data": []})}
        code, out = self.run_cli(["sync", "--include-arbeitnow", "--acknowledge-arbeitnow-terms", "--yes"], routes=routes)
        self.assertEqual(code, 0)
        session = sessionmaker(bind=self.engine)()
        run = session.query(models.JobFetchRun).one()
        self.assertTrue(json.loads(run.params_json)["terms_acknowledged"])
        session.close()

    def test_refuses_missing_database_file(self):
        missing = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'missing.db')}")
        code, out = self.run_cli(["summary"], engine=missing)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "missing.db")))
        missing.dispose()

    def test_seed_check_enable_sync(self):
        self.assertEqual(self.run_cli(["seed", "--yes"])[0], 0)
        session = sessionmaker(bind=self.engine)()
        board = models.JobSourceBoard(company_id=1, source="greenhouse", board_token="acme", region="global", is_enabled=False, verification_status="unverified")
        session.add(models.Company(company_id=999, name="Acme", normalized_name="acme"))
        board.company_id = 999
        session.add(board)
        session.commit()
        board_id = board.board_id
        session.close()
        routes = {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": FakeResponse(200, greenhouse_jobs()),
            "https://boards-api.greenhouse.io/v1/boards/acme": FakeResponse(200, greenhouse_board()),
        }
        code, out = self.run_cli(["check", "--board-id", str(board_id), "--yes"], routes=routes)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out[out.index("["):])[0]["active_postings"], 2)
        session = sessionmaker(bind=self.engine)()
        board = session.get(models.JobSourceBoard, board_id)
        self.assertEqual(board.verification_status, "verified")
        self.assertEqual(session.query(models.Job).count(), 0)  # check publishes nothing
        board.is_enabled = True
        session.commit()
        session.close()
        code, out = self.run_cli(["sync", "--board-id", str(board_id), "--yes"], routes=routes)
        self.assertEqual(code, 0)
        session = sessionmaker(bind=self.engine)()
        self.assertEqual(session.query(models.Job).count(), 2)
        session.close()


if __name__ == "__main__":
    unittest.main()
