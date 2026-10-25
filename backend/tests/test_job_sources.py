"""Job-source connectors and their HTTP client, offline with fixtures."""

import unittest

import requests

from backend.app.job_sources import HttpClient, SourceError
from backend.app.job_sources import arbeitnow, ashby, greenhouse, lever
from backend.tests.catalog_fixtures import (
    FakeResponse,
    FakeSession,
    arbeitnow_item,
    arbeitnow_page,
    ashby_board,
    greenhouse_board,
    greenhouse_jobs,
    lever_postings,
)


def client(routes, retries=2):
    sleeps = []
    http = HttpClient(FakeSession(routes), max_retries=retries, sleep=sleeps.append)
    return http, sleeps


class GreenhouseTests(unittest.TestCase):
    def test_parses_jobs_and_drops_markup(self):
        http, _ = client({
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": FakeResponse(200, greenhouse_jobs()),
            "https://boards-api.greenhouse.io/v1/boards/acme": FakeResponse(200, greenhouse_board()),
        })
        fetch = greenhouse.fetch_board(http, "acme")
        self.assertTrue(fetch.complete)
        self.assertEqual([p.source_key for p in fetch.postings], ["acme:101", "acme:102"])
        first = fetch.postings[0]
        self.assertEqual(first.description_text, "Build APIs with\nPython\nand FastAPI.")
        self.assertNotIn("alert", first.description_text)
        self.assertEqual(first.location, "Berlin, Germany")
        self.assertIsNone(first.work_mode)
        self.assertIsNone(first.country_code)
        self.assertEqual(first.company_name, "Acme GmbH")
        self.assertEqual(len(fetch.skipped), 1)
        self.assertEqual(fetch.company_about, "Acme builds tools for logistics teams.")

    def test_http_error_is_incomplete(self):
        http, _ = client({})
        fetch = greenhouse.fetch_board(http, "missing")
        self.assertFalse(fetch.complete)
        self.assertEqual(fetch.stopped_reason, "HTTP_404")
        self.assertEqual(fetch.postings, [])


class LeverTests(unittest.TestCase):
    def test_paginates_until_short_page(self):
        base = "https://api.lever.co/v0/postings/leverco"
        http, _ = client({
            f"{base}?mode=json&skip=0&limit=100": FakeResponse(200, lever_postings(count=100)),
            f"{base}?mode=json&skip=100&limit=100": FakeResponse(200, lever_postings(count=3, offset=100)),
        })
        fetch = lever.fetch_board(http, "leverco")
        self.assertTrue(fetch.complete)
        self.assertEqual(fetch.pages_checked, 2)
        self.assertEqual(len(fetch.postings), 103)
        posting = fetch.postings[0]
        self.assertEqual(posting.work_mode, "hybrid")
        self.assertEqual(posting.country_code, "TR")
        self.assertIn("Requirements", posting.description_text)
        self.assertIn("TypeScript", posting.description_text)
        self.assertEqual(posting.source_created_at, 1760000000)

    def test_page_cap_is_incomplete(self):
        base = "https://api.lever.co/v0/postings/leverco"
        http, _ = client({f"{base}?mode=json&skip=0&limit=100": FakeResponse(200, lever_postings(count=100))})
        fetch = lever.fetch_board(http, "leverco", max_pages=1)
        self.assertFalse(fetch.complete)
        self.assertEqual(fetch.stopped_reason, "MAX_PAGES_REACHED")

    def test_eu_region_uses_eu_host(self):
        session = FakeSession({"https://api.eu.lever.co/v0/postings/euco": FakeResponse(200, [])})
        fetch = lever.fetch_board(HttpClient(session, sleep=lambda s: None), "euco", region="eu")
        self.assertTrue(fetch.complete)
        self.assertTrue(session.calls[0].startswith("https://api.eu.lever.co/"))


class AshbyTests(unittest.TestCase):
    def test_skips_unlisted_and_reads_structured_fields(self):
        http, _ = client({"https://api.ashbyhq.com/posting-api/job-board/ashbyco": FakeResponse(200, ashby_board())})
        fetch = ashby.fetch_board(http, "ashbyco")
        self.assertTrue(fetch.complete)
        self.assertEqual(len(fetch.postings), 1)
        posting = fetch.postings[0]
        self.assertEqual(posting.work_mode, "remote")
        self.assertEqual(posting.country_code, "DE")
        self.assertEqual(fetch.skipped[0]["reason"], "NOT_LISTED")


class ArbeitnowTests(unittest.TestCase):
    def test_reads_until_empty_page(self):
        url = "https://www.arbeitnow.com/api/job-board-api"
        http, _ = client({
            f"{url}?page=1": FakeResponse(200, arbeitnow_page([arbeitnow_item("a"), arbeitnow_item("b")])),
            f"{url}?page=2": FakeResponse(200, arbeitnow_page([])),
        })
        fetch = arbeitnow.fetch_board(http)
        self.assertTrue(fetch.complete)
        self.assertEqual([p.source_key for p in fetch.postings], ["a", "b"])


class HttpClientTests(unittest.TestCase):
    URL = "https://api.example.test/x"

    def test_retries_429_with_retry_after(self):
        http, sleeps = client({self.URL: [FakeResponse(429, None, {"Retry-After": "3"}), FakeResponse(200, {"ok": True})]})
        self.assertEqual(http.get_json(self.URL), {"ok": True})
        self.assertEqual(sleeps, [3.0])

    def test_gives_up_after_retries(self):
        http, sleeps = client({self.URL: [FakeResponse(429)]}, retries=2)
        with self.assertRaises(SourceError) as ctx:
            http.get_json(self.URL)
        self.assertEqual(ctx.exception.code, "RATE_LIMITED")
        self.assertEqual(len(sleeps), 2)

    def test_long_retry_after_is_not_waited(self):
        http, sleeps = client({self.URL: [FakeResponse(429, None, {"Retry-After": "3600"})]})
        with self.assertRaises(SourceError):
            http.get_json(self.URL)
        self.assertEqual(sleeps, [])

    def test_timeout_retries_then_fails(self):
        http, sleeps = client({self.URL: requests.exceptions.Timeout()}, retries=1)
        with self.assertRaises(SourceError) as ctx:
            http.get_json(self.URL)
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertEqual(sleeps, [2])

    def test_redirect_is_not_followed(self):
        http, _ = client({self.URL: FakeResponse(302, None, {"Location": "http://169.254.169.254/"})})
        with self.assertRaises(SourceError) as ctx:
            http.get_json(self.URL)
        self.assertEqual(ctx.exception.code, "HTTP_302")

    def test_invalid_json(self):
        http, _ = client({self.URL: FakeResponse(200, raise_json=True)})
        with self.assertRaises(SourceError) as ctx:
            http.get_json(self.URL)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")


if __name__ == "__main__":
    unittest.main()
