"""Catalog sync: no duplicates on re-run, content updates make old results
non-current, only complete fetches close jobs, exact copies are linked and
similar ones are not, personal jobs are never touched, one sync at a time,
and the admin routes. Throwaway SQLite, fixtures, no network."""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import catalog_sync, models
from backend.app.database import Base, get_db
from backend.app.job_sources import BoardFetch, HttpClient, NormalizedPosting
from backend.app.security import create_access_token, hash_password
from backend.routers import catalog_admin
from backend.tests.catalog_fixtures import (
    FakeResponse,
    FakeSession,
    arbeitnow_item,
    arbeitnow_page,
    greenhouse_board,
    greenhouse_jobs,
)

SYNTHETIC_SECRET = "synthetic-test-secret-for-catalog-sync-0123456789abcdef"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
GH_JOBS = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/acme"
ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api"


def posting(key, **overrides):
    values = dict(
        source="greenhouse",
        source_key=f"acme:{key}",
        title=f"Engineer {key}",
        url=f"https://example.test/jobs/{key}",
        description_text=f"Description {key}",
        company_name="Acme",
        location="Berlin",
    )
    values.update(overrides)
    return NormalizedPosting(**values)


class CatalogSyncBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_catalog_sync_test_")
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def board(self, token="acme", source="greenhouse", enabled=True):
        board, _ = catalog_sync.create_board(
            self.db, company_name="Acme", source=source, board_token=token, region="global", now=NOW
        )
        board.is_enabled = enabled
        self.db.commit()
        return board

    def apply(self, postings, complete=True, board=None, source="greenhouse"):
        fetch = BoardFetch(postings=list(postings), complete=complete)
        company = None
        if board is not None:
            company = self.db.get(models.Company, board.company_id)
        counts = catalog_sync.apply_postings(
            self.db, source=source, board=board, fetch=fetch, now=NOW, board_company=company
        )
        self.db.commit()
        return counts

    def catalog_jobs(self):
        return self.db.query(models.Job).filter(models.Job.created_by_user_id.is_(None)).order_by(models.Job.job_id).all()


class ApplyPostingsTests(CatalogSyncBase):
    def test_rerun_creates_no_duplicates(self):
        board = self.board()
        first = self.apply([posting(1), posting(2)], board=board)
        second = self.apply([posting(1), posting(2)], board=board)
        self.assertEqual((first.new, second.new, second.unchanged), (2, 0, 2))
        jobs = self.catalog_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(job.job_status == "active" for job in jobs))
        self.assertTrue(all(job.company_id == board.company_id for job in jobs))

    def test_content_change_updates_and_makes_results_not_current(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        job = self.catalog_jobs()[0]
        profile_analysis = models.ProfileAnalysis(profile_id=1, analysis_status="completed", is_current=True)
        self.db.add(profile_analysis)
        self.db.add(models.JobAnalysis(job_id=job.job_id, analysis_status="completed", is_current=True))
        self.db.add(models.JobMatch(profile_id=1, job_id=job.job_id, is_current=True))
        self.db.commit()

        counts = self.apply([posting(1, description_text="Now requires German C1.")], board=board)
        self.assertEqual(counts.updated, 1)
        self.db.expire_all()
        job = self.catalog_jobs()[0]
        self.assertEqual(job.description_text, "Now requires German C1.")
        self.assertFalse(self.db.query(models.JobAnalysis).filter_by(job_id=job.job_id).one().is_current)
        self.assertFalse(self.db.query(models.JobMatch).filter_by(job_id=job.job_id).one().is_current)

    def test_unchanged_content_keeps_results_current(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        job = self.catalog_jobs()[0]
        self.db.add(models.JobAnalysis(job_id=job.job_id, analysis_status="completed", is_current=True))
        self.db.commit()
        self.apply([posting(1)], board=board)
        self.assertTrue(self.db.query(models.JobAnalysis).filter_by(job_id=job.job_id).one().is_current)

    def test_complete_fetch_closes_unseen_and_reopens_seen(self):
        board = self.board()
        self.apply([posting(1), posting(2)], board=board)
        counts = self.apply([posting(1)], board=board)
        self.assertEqual(counts.closed, 1)
        statuses = {job.title: job.job_status for job in self.catalog_jobs()}
        self.assertEqual(statuses, {"Engineer 1": "active", "Engineer 2": "closed"})
        counts = self.apply([posting(1), posting(2)], board=board)
        self.assertEqual(counts.reopened, 1)
        self.assertTrue(all(job.job_status == "active" for job in self.catalog_jobs()))

    def test_partial_fetch_closes_nothing(self):
        board = self.board()
        self.apply([posting(1), posting(2)], board=board)
        counts = self.apply([posting(1)], complete=False, board=board)
        self.assertEqual(counts.closed, 0)
        self.assertTrue(all(job.job_status == "active" for job in self.catalog_jobs()))

    def test_failed_empty_fetch_closes_nothing(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        self.apply([], complete=False, board=board)
        self.assertEqual(self.catalog_jobs()[0].job_status, "active")

    def test_other_boards_jobs_are_not_closed(self):
        acme = self.board("acme")
        other = self.board("other")
        self.apply([posting(1)], board=acme)
        self.apply([posting(9, source_key="other:9")], board=other)
        self.apply([posting(2)], board=acme)  # complete: acme's job 1 closes
        statuses = {job.title: job.job_status for job in self.catalog_jobs()}
        self.assertEqual(statuses, {"Engineer 1": "closed", "Engineer 9": "active", "Engineer 2": "active"})

    def test_same_url_from_another_source_is_linked_not_duplicated(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        counts = self.apply(
            [posting(1, source="lever", source_key="acme:x1")],
            board=None,
            source="lever",
            complete=False,
        )
        self.assertEqual(counts.linked_duplicates, 1)
        self.assertEqual(len(self.catalog_jobs()), 1)
        records = self.db.query(models.JobSourceRecord).order_by(models.JobSourceRecord.record_id).all()
        self.assertEqual([(r.source, r.is_primary) for r in records], [("greenhouse", True), ("lever", False)])

    def test_exact_copy_with_other_url_is_linked(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        copy = posting(1, source="ashby", source_key="acme:z", url="https://jobs.example.test/other-url")
        counts = catalog_sync.apply_postings(
            self.db,
            source="ashby",
            board=None,
            fetch=BoardFetch(postings=[copy], complete=False),
            now=NOW,
            board_company=self.db.get(models.Company, board.company_id),
        )
        self.db.commit()
        self.assertEqual(counts.linked_duplicates, 1)
        self.assertEqual(len(self.catalog_jobs()), 1)

    def test_similar_title_is_not_merged(self):
        board = self.board()
        self.apply([posting(1, title="Backend Engineer")], board=board)
        similar = posting(2, title="Backend Engineer", description_text="Different text", url="https://example.test/jobs/2")
        counts = self.apply([posting(1, title="Backend Engineer"), similar], board=board)
        self.assertEqual(counts.new, 1)
        self.assertEqual(len(self.catalog_jobs()), 2)

    def test_copy_does_not_update_content_or_close_primary(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        self.apply([posting(1, source="lever", source_key="acme:x1")], board=None, source="lever", complete=False)
        # The copy changes its text: the job keeps the primary's content.
        self.apply(
            [posting(1, source="lever", source_key="acme:x1", description_text="changed elsewhere")],
            board=None, source="lever", complete=False,
        )
        self.assertEqual(self.catalog_jobs()[0].description_text, "Description 1")
        # The primary disappears but the copy is still active: not closed.
        self.apply([posting(5)], board=board)
        self.assertEqual(self.catalog_jobs()[0].job_status, "active")

    def test_personal_job_with_same_url_is_untouched(self):
        user = models.User(name="U", mail="u@example.com")
        self.db.add(user)
        self.db.commit()
        personal = models.Job(
            title="My copy", url="https://example.test/jobs/1", description_text="mine",
            created_by_user_id=user.user_id, source="manual",
        )
        self.db.add(personal)
        self.db.commit()
        board = self.board()
        self.apply([posting(1)], board=board)
        self.db.expire_all()
        personal = self.db.query(models.Job).filter_by(created_by_user_id=user.user_id).one()
        self.assertEqual((personal.description_text, personal.company_id, personal.job_status), ("mine", None, "unknown"))
        self.assertEqual(self.db.query(models.JobSourceRecord).filter_by(job_id=personal.job_id).count(), 0)
        self.assertEqual(len(self.catalog_jobs()), 1)

    def test_legacy_arbeitnow_job_is_adopted_by_url(self):
        item = arbeitnow_item("slug-1")
        legacy = models.Job(
            title=item["title"], company_name=item["company_name"], location=item["location"],
            url=item["url"], description_text="Python in Berlin.", source="arbeitnow",
        )
        self.db.add(legacy)
        self.db.commit()
        self.db.add(models.JobAnalysis(job_id=legacy.job_id, analysis_status="completed", is_current=True))
        self.db.commit()
        from backend.app.job_sources import arbeitnow

        fetch = arbeitnow.fetch_board(
            HttpClient(FakeSession({
                f"{ARBEITNOW}?page=1": FakeResponse(200, arbeitnow_page([item])),
                f"{ARBEITNOW}?page=2": FakeResponse(200, arbeitnow_page([])),
            }), sleep=lambda s: None)
        )
        counts = catalog_sync.apply_postings(self.db, source="arbeitnow", board=None, fetch=fetch, now=NOW)
        self.db.commit()
        self.assertEqual((counts.new, counts.updated, counts.unchanged), (0, 0, 1))
        self.assertEqual(len(self.catalog_jobs()), 1)
        self.db.expire_all()
        self.assertTrue(self.db.query(models.JobAnalysis).filter_by(job_id=legacy.job_id).one().is_current)
        self.assertEqual(self.catalog_jobs()[0].job_status, "active")


class ReviewFixTests(CatalogSyncBase):
    def test_complete_arbeitnow_fetch_closes_unseen_legacy_jobs(self):
        seen = arbeitnow_item("seen")
        legacy_seen = models.Job(title=seen["title"], company_name=seen["company_name"], location="Berlin", url=seen["url"],
                                 description_text="Python in Berlin.", source="arbeitnow")
        legacy_gone = models.Job(title="Gone", url="https://www.arbeitnow.com/jobs/gone", description_text="x", source="arbeitnow")
        manual = models.Job(title="Other source", url="https://example.test/legacy-other", description_text="x", source="greenhouse")
        self.db.add_all([legacy_seen, legacy_gone, manual])
        self.db.commit()
        from backend.app.job_sources import arbeitnow

        def fetch(complete_pages):
            routes = {f"{ARBEITNOW}?page=1": FakeResponse(200, arbeitnow_page([seen]))}
            if complete_pages:
                routes[f"{ARBEITNOW}?page=2"] = FakeResponse(200, arbeitnow_page([]))
            return arbeitnow.fetch_board(HttpClient(FakeSession(routes), sleep=lambda s: None), max_pages=2)

        partial = catalog_sync.apply_postings(self.db, source="arbeitnow", board=None, fetch=fetch(False), now=NOW)
        self.db.commit()
        self.assertEqual(partial.closed, 0)
        complete = catalog_sync.apply_postings(self.db, source="arbeitnow", board=None, fetch=fetch(True), now=NOW)
        self.db.commit()
        self.assertEqual(complete.closed, 1)
        self.db.expire_all()
        statuses = {job.title: job.job_status for job in self.catalog_jobs()}
        self.assertEqual(statuses[seen["title"]], "active")
        self.assertEqual(statuses["Gone"], "closed")
        self.assertEqual(statuses["Other source"], "unknown")

    def test_copy_becomes_primary_when_primary_disappears(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        self.apply([posting(1, source="lever", source_key="acme:x1")], board=None, source="lever", complete=False)
        self.apply([posting(5)], board=board)  # primary gone, copy still active
        copy = self.db.query(models.JobSourceRecord).filter_by(source="lever").one()
        self.assertTrue(copy.is_primary)
        self.apply([posting(1, source="lever", source_key="acme:x1", description_text="Updated by the copy")], board=None, source="lever", complete=False)
        self.assertEqual(self.catalog_jobs()[0].description_text, "Updated by the copy")

    def test_empty_complete_feed_closes_nothing(self):
        board = self.board()
        self.apply([posting(1), posting(2)], board=board)
        counts = self.apply([], board=board)
        self.assertEqual(counts.closed, 0)
        self.assertTrue(all(job.job_status == "active" for job in self.catalog_jobs()))

    def test_link_and_source_follow_the_primary_record(self):
        board = self.board()
        self.apply([posting(1)], board=board)
        copy = posting(1, source="lever", source_key="acme:x1", url="https://jobs.lever.co/acme/x1")
        catalog_sync.apply_postings(self.db, source="lever", board=None, fetch=BoardFetch(postings=[copy], complete=False), now=NOW,
                                    board_company=self.db.get(models.Company, board.company_id))
        self.db.commit()
        self.apply([posting(5)], board=board)  # primary gone: the Lever copy is primary now
        job = next(job for job in self.catalog_jobs() if job.title == "Engineer 1")
        self.assertEqual((job.url, job.source), ("https://jobs.lever.co/acme/x1", "lever"))
        # A primary record whose URL changes moves the job's link too.
        self.apply([posting(5, url="https://example.test/jobs/5-moved")], board=board)
        moved = next(job for job in self.catalog_jobs() if job.title == "Engineer 5")
        self.assertEqual(moved.url, "https://example.test/jobs/5-moved")

    def test_board_disabled_during_fetch_writes_nothing(self):
        board = self.board()
        Session = self.Session

        class DisablingSession(FakeSession):
            def get(inner, url, **kwargs):
                db = Session()
                try:
                    db.get(models.JobSourceBoard, board.board_id).is_enabled = False
                    db.commit()
                finally:
                    db.close()
                return super().get(url, **kwargs)

        http = HttpClient(DisablingSession({GH_JOBS: FakeResponse(200, greenhouse_jobs()), GH_BOARD: FakeResponse(200, greenhouse_board())}), sleep=lambda s: None)
        result = catalog_sync.run_sync(self.db, http, include_arbeitnow=False, sleep=lambda s: None, now_fn=lambda: NOW)
        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertEqual(self.catalog_jobs(), [])
        self.assertEqual(self.db.query(models.JobSourceRecord).count(), 0)

    def test_lease_is_renewed_during_a_long_fetch(self):
        self.board()
        ticks = iter(range(0, 10_000, 70))
        renewals = []
        original = catalog_sync.renew_sync_lock

        def counting(db, token, now_epoch=None):
            renewals.append(token)
            return original(db, token, now_epoch)

        with patch.object(catalog_sync, "renew_sync_lock", counting):
            catalog_sync.run_sync(self.db, HttpClient(FakeSession({GH_JOBS: FakeResponse(200, greenhouse_jobs()), GH_BOARD: FakeResponse(200, greenhouse_board())}), sleep=lambda s: None),
                                  include_arbeitnow=False, sleep=lambda s: None, now_fn=lambda: NOW, clock=lambda: next(ticks))
        # Once before the board, and before each of its two requests.
        self.assertGreaterEqual(len(renewals), 3)

    def test_disabling_a_board_closes_its_jobs(self):
        board = self.board()
        self.apply([posting(1), posting(2)], board=board)
        closed = catalog_sync.close_board_jobs(self.db, board.board_id, NOW)
        self.db.commit()
        self.assertEqual(closed, 2)
        self.assertTrue(all(job.job_status == "closed" for job in self.catalog_jobs()))

    def test_board_token_with_newline_is_invalid(self):
        self.assertFalse(catalog_sync.valid_board_token("acme\n"))
        self.assertTrue(catalog_sync.valid_board_token("acme-1.eu_x"))

    def test_lost_lease_stops_the_run(self):
        self.board("b1")
        self.board("b2")
        db = self.db

        def steal(seconds):
            db.query(models.AnalysisGuard).update({models.AnalysisGuard.owner_token: "someone-else"})
            db.commit()

        with self.assertRaises(catalog_sync.SyncAlreadyRunning):
            catalog_sync.run_sync(db, HttpClient(FakeSession({}), sleep=lambda s: None), include_arbeitnow=True, acknowledge_arbeitnow_terms=True,
                                  sleep=steal, now_fn=lambda: NOW)

    def test_time_budget_stops_starting_boards(self):
        self.board("b1")
        self.board("b2")
        db = self.db
        # Time runs out once the first board's run is recorded.
        clock = lambda: 0 if db.query(models.JobFetchRun).count() == 0 else catalog_sync.SYNC_TIME_BUDGET_SECONDS + 1  # noqa: E731
        result = catalog_sync.run_sync(self.db, HttpClient(FakeSession({}), sleep=lambda s: None), include_arbeitnow=False,
                                       sleep=lambda s: None, now_fn=lambda: NOW, clock=clock)
        self.assertEqual((result["boards_synced"], result["stopped_reason"]), (1, "TIME_BUDGET_REACHED"))
        self.assertIsNone(self.db.query(models.AnalysisGuard).one().owner_token)

    def test_malformed_fields_are_skipped_not_fatal(self):
        from backend.app.job_sources import greenhouse, lever

        jobs = {"jobs": [{"id": 1, "title": "T", "absolute_url": "https://x.test/1", "content": 123},
                         {"id": 2, "title": "Ok", "absolute_url": "https://x.test/2", "content": "&lt;p&gt;fine&lt;/p&gt;"}]}
        fetch = greenhouse.fetch_board(HttpClient(FakeSession({GH_JOBS: FakeResponse(200, jobs)}), sleep=lambda s: None), "acme")
        self.assertTrue(fetch.complete)
        self.assertEqual([p.title for p in fetch.postings], ["Ok"])
        item = {"id": "a", "text": "T", "hostedUrl": "https://x.test/a", "descriptionPlain": "text", "lists": 5, "categories": "bad", "workplaceType": 3}
        self.assertEqual(lever._posting("x", item).description_text, "text")


class SyncRunTests(CatalogSyncBase):
    def http(self, routes):
        return HttpClient(FakeSession(routes), sleep=lambda s: None)

    def test_run_records_and_board_verification(self):
        board = self.board()
        result = catalog_sync.run_sync(
            self.db,
            self.http({GH_JOBS: FakeResponse(200, greenhouse_jobs()), GH_BOARD: FakeResponse(200, greenhouse_board())}),
            include_arbeitnow=False, sleep=lambda s: None, now_fn=lambda: NOW,
        )
        self.assertEqual(result["boards_synced"], 1)
        entry = result["results"][0]
        self.assertEqual((entry["status"], entry["new"], entry["complete"]), ("completed", 2, True))
        self.db.expire_all()
        board = self.db.get(models.JobSourceBoard, board.board_id)
        self.assertEqual((board.verification_status, board.last_active_jobs_count), ("verified", 2))
        run = self.db.query(models.JobFetchRun).one()
        self.assertEqual((run.status, run.complete, run.new_jobs_count, run.board_id), ("completed", True, 2, board.board_id))
        company = self.db.get(models.Company, board.company_id)
        self.assertEqual(company.about_text, "Acme builds tools for logistics teams.")

    def test_failed_board_is_recorded_and_closes_nothing(self):
        board = self.board()
        catalog_sync.run_sync(
            self.db, self.http({GH_JOBS: FakeResponse(200, greenhouse_jobs())}),
            include_arbeitnow=False, sleep=lambda s: None, now_fn=lambda: NOW,
        )
        result = catalog_sync.run_sync(
            self.db, self.http({GH_JOBS: FakeResponse(500)}),
            include_arbeitnow=False, sleep=lambda s: None, now_fn=lambda: NOW,
        )
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(result["results"][0]["closed"], 0)
        self.db.expire_all()
        self.assertTrue(all(job.job_status == "active" for job in self.catalog_jobs()))
        self.assertEqual(self.db.get(models.JobSourceBoard, board.board_id).verification_status, "verified")

    def test_disabled_boards_are_not_synced_and_max_boards_bounds(self):
        self.board("acme", enabled=False)
        self.board("b1")
        self.board("b2")
        session = FakeSession({})
        result = catalog_sync.run_sync(
            self.db, HttpClient(session, sleep=lambda s: None), include_arbeitnow=False,
            max_boards=1, sleep=lambda s: None, now_fn=lambda: NOW,
        )
        self.assertEqual(result["boards_synced"], 1)
        self.assertFalse(any("/acme/" in call for call in session.calls))

    def test_second_sync_is_refused_while_lock_is_held(self):
        token = catalog_sync.acquire_sync_lock(self.db)
        with self.assertRaises(catalog_sync.SyncAlreadyRunning):
            catalog_sync.run_sync(self.db, self.http({}), include_arbeitnow=False, sleep=lambda s: None)
        catalog_sync.release_sync_lock(self.db, token)
        catalog_sync.run_sync(self.db, self.http({}), include_arbeitnow=False, sleep=lambda s: None)

    def test_expired_lock_is_taken_over(self):
        catalog_sync.acquire_sync_lock(self.db, now_epoch=0)
        token = catalog_sync.acquire_sync_lock(self.db)
        self.assertTrue(token)

    def test_seed_is_idempotent_and_disabled(self):
        first = catalog_sync.seed_boards(self.db, NOW)
        self.db.commit()
        second = catalog_sync.seed_boards(self.db, NOW)
        self.db.commit()
        self.assertEqual(first["created"], first["boards_in_file"])
        self.assertEqual(second["created"], 0)
        boards = self.db.query(models.JobSourceBoard).all()
        self.assertGreaterEqual(len(boards), 1)
        self.assertTrue(all(not b.is_enabled and b.verification_status == "unverified" for b in boards))


class CatalogAdminRouteTests(CatalogSyncBase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(catalog_admin.router)

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.admin = self._user("admin@example.com", True)
        self.user = self._user("user@example.com", False)

    def _user(self, mail, is_admin):
        user = models.User(name="U", mail=mail, password_hash=hash_password("a-valid-synthetic-password"), is_admin=is_admin)
        self.db.add(user)
        self.db.commit()
        return {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}

    def test_normal_user_gets_403(self):
        for method, path in [("get", "/admin/catalog/boards"), ("post", "/admin/catalog/sync"), ("get", "/admin/catalog/summary")]:
            response = getattr(self.client, method)(path, headers=self.user, **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status_code, 403, path)

    def test_enabling_unchecked_source_needs_acknowledgement(self):
        response = self.client.post("/admin/catalog/boards", headers=self.admin, json={
            "company_name": "Acme", "source": "greenhouse", "board_token": "acme",
        })
        self.assertEqual(response.status_code, 201)
        board_id = response.json()["board_id"]
        self.assertFalse(response.json()["is_enabled"])
        response = self.client.patch(f"/admin/catalog/boards/{board_id}", headers=self.admin, json={"is_enabled": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_SOURCE_TERMS_UNCHECKED")
        response = self.client.patch(
            f"/admin/catalog/boards/{board_id}", headers=self.admin, json={"is_enabled": True, "acknowledge_terms": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_enabled"])

    def test_invalid_board_token_and_duplicate(self):
        bad = self.client.post("/admin/catalog/boards", headers=self.admin, json={
            "company_name": "Acme", "source": "greenhouse", "board_token": "../etc",
        })
        self.assertEqual(bad.status_code, 422)
        body = {"company_name": "Acme", "source": "lever", "board_token": "acme", "region": "eu"}
        self.assertEqual(self.client.post("/admin/catalog/boards", headers=self.admin, json=body).status_code, 201)
        self.assertEqual(self.client.post("/admin/catalog/boards", headers=self.admin, json=body).status_code, 409)
        eu_greenhouse = {"company_name": "Acme", "source": "greenhouse", "board_token": "acme2", "region": "eu"}
        self.assertEqual(self.client.post("/admin/catalog/boards", headers=self.admin, json=eu_greenhouse).status_code, 422)

    def test_sync_route_and_summary(self):
        self.board()
        with patch.object(catalog_admin, "make_http_client", lambda: HttpClient(FakeSession({
            GH_JOBS: FakeResponse(200, greenhouse_jobs()), GH_BOARD: FakeResponse(200, greenhouse_board()),
        }), sleep=lambda s: None)):
            response = self.client.post("/admin/catalog/sync", headers=self.admin, json={"max_boards": 5, "max_pages": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["results"][0]["new"], 2)
        summary = self.client.get("/admin/catalog/summary", headers=self.admin).json()
        self.assertEqual(summary["active_jobs_by_source"], {"greenhouse": 2})
        runs = self.client.get("/admin/catalog/runs", headers=self.admin).json()
        self.assertEqual(runs[0]["status"], "completed")

    def test_arbeitnow_sync_needs_acknowledgement(self):
        session = FakeSession({})
        with patch.object(catalog_admin, "make_http_client", lambda: HttpClient(session, sleep=lambda s: None)):
            refused = self.client.post("/admin/catalog/sync", headers=self.admin, json={"include_arbeitnow": True})
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()["detail"]["error_code"], "ERR_SOURCE_TERMS_UNCHECKED")
            self.assertEqual(session.calls, [])  # nothing fetched
            accepted = self.client.post("/admin/catalog/sync", headers=self.admin, json={"include_arbeitnow": True, "acknowledge_arbeitnow_terms": True})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["results"][0]["source"], "arbeitnow")

    def test_sources_report_terms_and_arbeitnow_link_back(self):
        sources = {item["source"]: item for item in self.client.get("/admin/catalog/sources", headers=self.admin).json()}
        self.assertTrue(sources["arbeitnow"]["terms_checked"])
        self.assertFalse(sources["arbeitnow"]["auto_publish"])
        self.assertEqual(sources["arbeitnow"]["attribution_url"], "https://www.arbeitnow.com")
        self.assertIn("link back", sources["arbeitnow"]["terms_note"])
        # Checked, but no source grants third-party reuse: none is
        # published automatically.
        for source in ("greenhouse", "lever", "ashby"):
            self.assertTrue(sources[source]["terms_checked"])
            self.assertFalse(sources[source]["auto_publish"])
        self.assertIn("Dedicated Partner Job Feeds", sources["ashby"]["terms_note"])

    def test_company_board_list_holds_live_check_results(self):
        import json as _json
        doc = _json.loads(catalog_sync.COMPANY_BOARDS_FILE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(doc["boards"]), 30)
        for entry in doc["boards"]:
            self.assertIn(entry["verification"], ("verified", "failed"))
            self.assertTrue(catalog_sync.valid_board_token(entry["board_token"]))
            self.assertIn("active_postings", entry["live_check"])

    def test_sync_limits_are_validated(self):
        response = self.client.post("/admin/catalog/sync", headers=self.admin, json={"max_boards": 500})
        self.assertEqual(response.status_code, 422)

    def test_sync_conflict_while_running(self):
        catalog_sync.acquire_sync_lock(self.db)
        response = self.client.post("/admin/catalog/sync", headers=self.admin, json={})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
