"""The test-deployment entry point (deploy_app): its environment checks, the
access-key gate and the static web client. A stub API app stands in for
backend.app.main, which tests never import; all data is in temp dirs."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import deploy_app
from backend.app.config import ConfigError


ACCESS_KEY = "k" * 32


def stub_api() -> FastAPI:
    api = FastAPI()

    @api.get("/meta")
    def meta():
        return {"analysis_provider": "fake"}

    @api.get("/jobs/{job_id}")
    def job(job_id: int):
        return {"job_id": job_id}

    return api


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apply101_deploy_test_"))
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.web = self.tmp / "web"
        (self.web / "assets").mkdir(parents=True)
        (self.web / "index.html").write_text("<!doctype html><title>Apply101</title><div id=root></div>")
        (self.web / "assets" / "app.js").write_text("console.log('app')")
        (self.tmp / "secret.txt").write_text("outside the web dir")
        self.env = {
            "APPLY101_LOAD_DOTENV": "0",
            "APPLY101_DATA_DIR": str(self.data),
            "APPLY101_DATABASE_URL": f"sqlite:///{self.data / 'apply101-test.db'}",
            "APPLY101_UPLOAD_DIR": str(self.data / "uploads"),
            "APPLY101_ANALYSIS_PROVIDER": "fake",
            "JWT_SECRET_KEY": "j" * 48,
            "APPLY101_ACCESS_KEY": ACCESS_KEY,
            "APPLY101_WEB_DIR": str(self.web),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, **env):
        app = deploy_app.create_app(stub_api(), environ={**self.env, **env}, session_factory=lambda: None)
        return TestClient(app, base_url="https://testserver")

    def unlocked_client(self):
        client = self.client()
        response = client.post("/__access", data={"key": ACCESS_KEY}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return client


class EnvironmentCheckTests(DeployTestCase):
    def assert_refused(self, fragment, **changes):
        env = {k: v for k, v in {**self.env, **changes}.items() if v is not None}
        with self.assertRaises(ConfigError) as caught:
            deploy_app.check_deploy_environment(env)
        self.assertIn(fragment, str(caught.exception))

    def test_accepts_the_test_setup(self):
        settings = deploy_app.check_deploy_environment(self.env)
        self.assertEqual(settings.database_file, self.data / "apply101-test.db")
        self.assertFalse(settings.scheduler.enabled)

    def test_refuses_unsafe_settings(self):
        cases = [
            ("APPLY101_LOAD_DOTENV must be 0", {"APPLY101_LOAD_DOTENV": None}),
            ("APPLY101_LOAD_DOTENV must be 0", {"APPLY101_LOAD_DOTENV": "1"}),
            ("APPLY101_DATA_DIR must be an absolute", {"APPLY101_DATA_DIR": None}),
            ("does not exist", {"APPLY101_DATA_DIR": str(self.tmp / "missing")}),
            ("APPLY101_DATABASE_URL must be set", {"APPLY101_DATABASE_URL": None}),
            ("must be inside APPLY101_DATA_DIR", {"APPLY101_DATABASE_URL": f"sqlite:///{self.tmp / 'other.db'}"}),
            ("absolute path", {"APPLY101_DATABASE_URL": "sqlite:///./apply101.db"}),
            ("file-backed SQLite", {"APPLY101_DATABASE_URL": "sqlite://"}),
            ("must not use a SQLite URI", {"APPLY101_DATABASE_URL": f"sqlite:///file:{self.data / 'x.db'}?uri=true"}),
            ("upload directory", {"APPLY101_UPLOAD_DIR": str(self.tmp / "uploads")}),
            ("APPLY101_UPLOAD_DIR must be an absolute", {"APPLY101_UPLOAD_DIR": "uploads"}),
            ("must be 'fake'", {"APPLY101_ANALYSIS_PROVIDER": "openai"}),
            ("must be 'fake'", {"APPLY101_ANALYSIS_PROVIDER": None}),
            ("OPENAI_API_KEY must not be set", {"OPENAI_API_KEY": "sk-test"}),
            ("JWT_SECRET_KEY must be set", {"JWT_SECRET_KEY": None}),
            ("APPLY101_ACCESS_KEY must be set", {"APPLY101_ACCESS_KEY": "short"}),
            ("no index.html", {"APPLY101_WEB_DIR": str(self.tmp)}),
            ("APPLY101_CATALOG_SYNC_HOUR_UTC", {"APPLY101_CATALOG_SYNC_HOUR_UTC": "25"}),
            ("inside the Railway volume", {"RAILWAY_VOLUME_MOUNT_PATH": str(self.tmp / "volume")}),
        ]
        for fragment, changes in cases:
            with self.subTest(fragment=fragment, changes=changes):
                self.assert_refused(fragment, **changes)

    def test_accepts_data_dir_inside_the_volume(self):
        settings = deploy_app.check_deploy_environment({**self.env, "RAILWAY_VOLUME_MOUNT_PATH": str(self.tmp)})
        self.assertEqual(settings.data_dir, self.data)

    def test_lists_every_problem(self):
        with self.assertRaises(ConfigError) as caught:
            deploy_app.check_deploy_environment({})
        message = str(caught.exception)
        for name in ("APPLY101_LOAD_DOTENV", "APPLY101_DATA_DIR", "APPLY101_DATABASE_URL",
                     "APPLY101_UPLOAD_DIR", "APPLY101_ANALYSIS_PROVIDER", "JWT_SECRET_KEY", "APPLY101_ACCESS_KEY"):
            self.assertIn(name, message)

    def test_create_app_refuses_before_importing_the_api(self):
        with self.assertRaises(ConfigError):
            deploy_app.create_app(environ={**self.env, "APPLY101_LOAD_DOTENV": "1"})


class AccessGateTests(DeployTestCase):
    def test_health_needs_no_key(self):
        response = self.client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_everything_else_needs_the_key(self):
        client = self.client()
        page = client.get("/catalog")
        self.assertEqual(page.status_code, 401)
        self.assertIn("Access key", page.text)
        self.assertNotIn("Apply101</title><div id=root>", page.text)
        self.assertEqual(client.get("/assets/app.js").status_code, 401)
        api = client.get("/api/meta")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json(), {"detail": "Access key required."})
        client.cookies.set("apply101_access", "forged")
        self.assertEqual(client.get("/api/meta").status_code, 401)

    def test_wrong_key_is_refused_and_limited(self):
        client = self.client()
        for _ in range(deploy_app.MAX_ACCESS_FAILURES):
            response = client.post("/__access", data={"key": "wrong"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("apply101_access", response.cookies)
        blocked = client.post("/__access", data={"key": ACCESS_KEY}, follow_redirects=False)
        self.assertEqual(blocked.status_code, 429)
        self.assertNotIn("apply101_access", blocked.cookies)

    def test_right_key_sets_a_strict_cookie(self):
        client = self.client()
        response = client.post("/__access", data={"key": ACCESS_KEY}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/")
        cookie = response.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict", "max-age="):
            self.assertIn(flag, cookie)
        self.assertNotIn(ACCESS_KEY, response.headers["set-cookie"])
        self.assertEqual(client.get("/api/meta").json(), {"analysis_provider": "fake"})

    def test_a_new_key_invalidates_old_cookies(self):
        old = deploy_app.access_token(ACCESS_KEY)
        client = self.client(APPLY101_ACCESS_KEY="n" * 32)
        client.cookies.set("apply101_access", old)
        self.assertEqual(client.get("/api/meta").status_code, 401)


class WebClientTests(DeployTestCase):
    def test_serves_the_app_and_its_routes(self):
        client = self.unlocked_client()
        self.assertIn("id=root", client.get("/").text)
        for route in ("/catalog", "/jobs/5", "/for-you"):
            with self.subTest(route=route):
                response = client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertIn("id=root", response.text)
        self.assertEqual(client.get("/assets/app.js").text, "console.log('app')")
        self.assertEqual(client.get("/assets/missing.js").status_code, 404)
        self.assertEqual(client.get("/robots.txt").headers["x-robots-tag"], "noindex")

    def test_api_lives_under_api(self):
        client = self.unlocked_client()
        self.assertEqual(client.get("/api/jobs/5").json(), {"job_id": 5})
        self.assertIn("id=root", client.get("/jobs/5").text)
        self.assertEqual(client.get("/api/docs").status_code, 200)

    def test_no_files_outside_the_web_dir(self):
        client = self.unlocked_client()
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
            with self.subTest(path=path):
                self.assertNotIn("outside the web dir", client.get(path).text)

    def test_creates_the_upload_dir(self):
        self.client()
        self.assertTrue((self.data / "uploads").is_dir())


if __name__ == "__main__":
    unittest.main()
