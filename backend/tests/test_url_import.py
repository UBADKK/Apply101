"""URL import preview: supported ATS links, JobPosting JSON-LD, robots.txt,
unsupported sites, and the SSRF guards of the page fetch. No internet: ATS
APIs are faked, and the guarded fetch runs against a local test server
only with an injected resolver."""

import http.server
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import models, url_import
from backend.app.database import Base, get_db
from backend.app.job_sources import HttpClient
from backend.app.rate_limiting import RateLimiter, get_rate_limiter
from backend.app.security import create_access_token, hash_password
from backend.tests.catalog_fixtures import FakeResponse, FakeSession, ashby_board, greenhouse_jobs, lever_postings

with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-synthetic-test-key-not-a-real-key"}, clear=False):
    from backend.routers import jobs


JSON_LD_PAGE = """<html><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "x"},
 {"@type": "JobPosting", "title": "Data Engineer", "description": "<p>Spark &amp; SQL</p><script>alert(1)<\\/script>",
  "hiringOrganization": {"@type": "Organization", "name": "Gamma GmbH"},
  "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Hamburg", "addressCountry": "DE"}},
  "datePosted": "2026-10-01"}]}</script>
<script type="application/ld+json">not json</script>
</head><body>ignored</body></html>"""


def page(body, status=200, content_type="text/html; charset=utf-8", url="https://careers.example.com/jobs/1"):
    return url_import.FetchedPage(url=url, status=status, content_type=content_type.split(";")[0], body=body.encode())


class FakeFetch:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        result = self.pages.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return page("", status=404, url=url)
        return result


def public_resolver(host, port, type=None):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class CheckUrlTests(unittest.TestCase):
    def test_rejects_bad_schemes_ports_and_credentials(self):
        for url in ["ftp://x.com/a", "file:///etc/passwd", "javascript:alert(1)", "https://user:pw@x.com/", "x.com/job"]:
            with self.assertRaises(url_import.ImportError_, msg=url):
                url_import.check_url(url)
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.check_url("https://x.com:8443/a")
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_URL_BLOCKED")

    def test_private_and_special_addresses_are_blocked(self):
        for address in ["127.0.0.1", "10.1.2.3", "192.168.0.10", "172.16.5.4", "169.254.169.254", "100.64.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1"]:
            resolver = lambda host, port, type=None, a=address: [(0, 0, 0, "", (a, port))]
            with self.assertRaises(url_import.ImportError_, msg=address) as ctx:
                url_import.resolve_public("evil.example", 443, resolver)
            self.assertEqual(ctx.exception.code, "ERR_IMPORT_URL_BLOCKED")

    def test_any_private_address_blocks_the_host(self):
        resolver = lambda host, port, type=None: [(0, 0, 0, "", ("93.184.216.34", port)), (0, 0, 0, "", ("10.0.0.1", port))]
        with self.assertRaises(url_import.ImportError_):
            url_import.resolve_public("mixed.example", 443, resolver)

    def test_public_address_passes(self):
        self.assertEqual(url_import.resolve_public("example.com", 443, public_resolver), "93.184.216.34")


class PreviewTests(unittest.TestCase):
    def http(self, routes):
        return HttpClient(FakeSession(routes), max_retries=0, sleep=lambda s: None)

    def test_greenhouse_link_uses_the_api(self):
        job = greenhouse_jobs()["jobs"][0]
        http = self.http({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/101": FakeResponse(200, job)})
        fetch = FakeFetch({})
        preview = url_import.preview_url("https://job-boards.greenhouse.io/acme/jobs/101", http=http, fetch=fetch)
        self.assertEqual((preview.source, preview.title, preview.company_name), ("greenhouse", "Backend Engineer (Python)", "Acme GmbH"))
        self.assertNotIn("alert", preview.description_text)
        self.assertEqual(fetch.calls, [])  # the page itself is never fetched

    def test_lever_link(self):
        posting = lever_postings(site="leverco", count=1)[0]
        http = self.http({"https://api.lever.co/v0/postings/leverco/aaaa-0000?mode=json": FakeResponse(200, posting)})
        preview = url_import.preview_url("https://jobs.lever.co/leverco/aaaa-0000", http=http, fetch=FakeFetch({}))
        self.assertEqual((preview.source, preview.location), ("lever", "Istanbul, Turkey"))
        self.assertIn("company_name", preview.missing)

    def test_ashby_link_and_closed_posting(self):
        http = self.http({"https://api.ashbyhq.com/posting-api/job-board/ashbyco": FakeResponse(200, ashby_board())})
        preview = url_import.preview_url("https://jobs.ashbyhq.com/ashbyco/1111-2222", http=http, fetch=FakeFetch({}))
        self.assertEqual(preview.work_mode, "remote")
        self.assertTrue(any("countries" in note for note in preview.notes))
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.preview_url("https://jobs.ashbyhq.com/ashbyco/9999", http=http, fetch=FakeFetch({}))
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_NOT_FOUND")

    def test_json_ld_page(self):
        fetch = FakeFetch({
            "https://careers.example.com/robots.txt": page("User-agent: *\nAllow: /\n", content_type="text/plain"),
            "https://careers.example.com/jobs/1": page(JSON_LD_PAGE),
        })
        preview = url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)
        self.assertEqual(preview.source, "json_ld")
        self.assertEqual(preview.title, "Data Engineer")
        self.assertEqual(preview.company_name, "Gamma GmbH")
        self.assertEqual(preview.location, "Hamburg, DE")
        self.assertEqual(preview.description_text, "Spark & SQL")

    def test_robots_disallow_blocks(self):
        fetch = FakeFetch({
            "https://careers.example.com/robots.txt": page("User-agent: *\nDisallow: /jobs/\n", content_type="text/plain"),
            "https://careers.example.com/jobs/1": page(JSON_LD_PAGE),
        })
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_BLOCKED")
        self.assertEqual(fetch.calls, ["https://careers.example.com/robots.txt"])

    def test_missing_robots_allows_and_robots_errors_block(self):
        fetch = FakeFetch({"https://careers.example.com/jobs/1": page(JSON_LD_PAGE)})
        self.assertEqual(url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch).title, "Data Engineer")
        fetch = FakeFetch({
            "https://careers.example.com/robots.txt": page("", status=503),
            "https://careers.example.com/jobs/1": page(JSON_LD_PAGE),
        })
        with self.assertRaises(url_import.ImportError_):
            url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)

    def test_page_without_job_posting_falls_back(self):
        fetch = FakeFetch({"https://careers.example.com/jobs/1": page("<html><body><h1>Job</h1></body></html>")})
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_NO_JOB_DATA")

    def test_posting_without_description_is_incomplete(self):
        html = '<script type="application/ld+json">{"@type": "JobPosting", "title": "Only a title"}</script>'
        fetch = FakeFetch({"https://careers.example.com/jobs/1": page(html)})
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_INCOMPLETE")

    def test_login_page_and_non_html(self):
        fetch = FakeFetch({"https://careers.example.com/jobs/1": page("login", status=403)})
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_BLOCKED")
        fetch = FakeFetch({"https://careers.example.com/jobs/1": page("%PDF", content_type="application/pdf")})
        with self.assertRaises(url_import.ImportError_) as ctx:
            url_import.preview_url("https://careers.example.com/jobs/1", fetch=fetch)
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_NO_JOB_DATA")

    def test_unsupported_sites_are_never_fetched(self):
        fetch = FakeFetch({})
        for url in ["https://www.linkedin.com/jobs/view/123", "https://de.indeed.com/viewjob?jk=1", "https://www.kariyer.net/is-ilani/x"]:
            with self.assertRaises(url_import.ImportError_) as ctx:
                url_import.preview_url(url, fetch=fetch)
            self.assertEqual(ctx.exception.code, "ERR_IMPORT_SITE_UNSUPPORTED")
        self.assertEqual(fetch.calls, [])


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):  # noqa: N802
        status, headers, body = self.routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class GuardedGetTests(unittest.TestCase):
    """The real fetch against a local server. Loopback is only reachable
    because the test resolver maps the public test host names to it; the
    address check itself is bypassed for these names only."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        original = url_import._public_address
        patcher = patch.object(url_import, "_public_address", lambda a: a == "127.0.0.1" or original(a))
        patcher.start()
        self.addCleanup(patcher.stop)
        # http://site.test/... must use port 80 per check_url; connect to the
        # local server's port instead.
        self.opened = []

        def opener(scheme, host, port, address, path, accept, read_timeout):
            self.opened.append((host, address, path))
            return url_import._open(scheme, host, self.port, address, path, accept, read_timeout)

        self.opener = opener

    def resolver(self, host, port, type=None):
        mapping = {"site.test": "127.0.0.1", "internal.test": "10.0.0.5"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]

    def get(self, url, max_bytes=1000):
        return url_import.guarded_get(url, max_bytes=max_bytes, resolver=self.resolver, opener=self.opener)

    def test_fetches_with_host_header_and_pinned_address(self):
        _Handler.routes = {"/job": (200, {"Content-Type": "text/html"}, b"<html>ok</html>")}
        fetched = self.get("http://site.test/job")
        self.assertEqual((fetched.status, fetched.body, fetched.content_type), (200, b"<html>ok</html>", "text/html"))
        self.assertEqual(self.opened, [("site.test", "127.0.0.1", "/job")])

    def test_size_cap(self):
        _Handler.routes = {"/big": (200, {"Content-Type": "text/html"}, b"x" * 5000)}
        with self.assertRaises(url_import.ImportError_) as ctx:
            self.get("http://site.test/big", max_bytes=1000)
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_TOO_LARGE")

    def test_redirect_to_private_address_is_blocked(self):
        _Handler.routes = {"/r": (302, {"Location": "http://internal.test/admin"}, b"")}
        with self.assertRaises(url_import.ImportError_) as ctx:
            self.get("http://site.test/r")
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_URL_BLOCKED")

    def test_redirect_to_unsupported_site_and_loops(self):
        _Handler.routes = {"/r": (301, {"Location": "https://www.linkedin.com/jobs/1"}, b"")}
        with self.assertRaises(url_import.ImportError_) as ctx:
            self.get("http://site.test/r")
        self.assertEqual(ctx.exception.code, "ERR_IMPORT_SITE_UNSUPPORTED")
        _Handler.routes = {"/loop": (302, {"Location": "/loop"}, b"")}
        with self.assertRaises(url_import.ImportError_):
            self.get("http://site.test/loop")
        self.assertEqual(len(self.opened), 1 + url_import.MAX_REDIRECTS + 1)


class ImportPreviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_import_route_test_")
        self.engine = create_engine(f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic.db')}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(jobs.router)
        app.dependency_overrides[get_db] = override_get_db
        self.limiter = RateLimiter()
        app.dependency_overrides[get_rate_limiter] = lambda: self.limiter
        self.client = TestClient(app)
        env = patch.dict(os.environ, {"JWT_SECRET_KEY": "synthetic-secret-for-import-route-0123456789abcdef", "IMPORT_PREVIEW_MAX_PER_WINDOW": "2"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        db = Session()
        user = models.User(name="U", mail="u@example.com", password_hash=hash_password("a-valid-synthetic-password"))
        db.add(user)
        db.commit()
        self.headers = {"Authorization": f"Bearer {create_access_token(user.user_id, user.token_key)}"}
        db.close()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_preview_saves_nothing_and_rate_limits(self):
        fetch = FakeFetch({"https://careers.example.com/jobs/1": page(JSON_LD_PAGE)})
        with patch.object(jobs, "_import_fetch", fetch):
            first = self.client.post("/jobs/import/preview", headers=self.headers, json={"url": "https://careers.example.com/jobs/1"})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["fields"]["title"], "Data Engineer")
            failed = self.client.post("/jobs/import/preview", headers=self.headers, json={"url": "https://www.linkedin.com/jobs/view/1"})
            self.assertEqual(failed.status_code, 422)
            self.assertEqual(failed.json()["detail"]["fallback"], "paste_text")
            limited = self.client.post("/jobs/import/preview", headers=self.headers, json={"url": "https://careers.example.com/jobs/1"})
            self.assertEqual(limited.status_code, 429)
            self.assertIn("Retry-After", limited.headers)
        session = sessionmaker(bind=self.engine)()
        self.assertEqual(session.query(models.Job).count(), 0)
        session.close()

    def test_requires_login(self):
        self.assertEqual(self.client.post("/jobs/import/preview", json={"url": "https://x.com"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
