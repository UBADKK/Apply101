"""APPLY101_CORS_ORIGINS parsing and configure_cors.

No database at all: configure_cors is exercised on throwaway FastAPI apps
with one synthetic route; backend.app.main is never imported.
"""
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from backend.app import config
from backend.app.cors import configure_cors


ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ALLOWED_ORIGIN = "http://localhost:5173"
DISALLOWED_ORIGIN = "https://evil.example.net"


def _env(value=None):
    env = {name: v for name, v in os.environ.items() if name != config.CORS_ORIGINS_ENV}
    if value is not None:
        env[config.CORS_ORIGINS_ENV] = value
    return patch.dict(os.environ, env, clear=True)


class CorsOriginsParsingTests(unittest.TestCase):
    def test_unset_is_empty(self):
        with _env():
            self.assertEqual(config.cors_origins(), [])

    def test_blank_is_empty(self):
        with _env("   "):
            self.assertEqual(config.cors_origins(), [])

    def test_only_separators_is_empty(self):
        with _env(" , ,"):
            self.assertEqual(config.cors_origins(), [])

    def test_entries_are_stripped_and_empty_entries_ignored(self):
        with _env(f" {ALLOWED_ORIGIN} ,, {OTHER_ALLOWED_ORIGIN},"):
            self.assertEqual(config.cors_origins(), [ALLOWED_ORIGIN, OTHER_ALLOWED_ORIGIN])

    def test_valid_forms_are_accepted(self):
        valid = [
            "http://localhost",
            "http://localhost:5173",
            "https://app.example.com",
            "https://app.example.com:8443",
            "http://127.0.0.1:8000",
            "http://[::1]:5173",
        ]
        with _env(",".join(valid)):
            self.assertEqual(config.cors_origins(), valid)

    def test_wildcard_is_refused(self):
        for value in ("*", f"{ALLOWED_ORIGIN},*", " * "):
            with self.subTest(value=value), _env(value):
                with self.assertRaises(config.ConfigError) as ctx:
                    config.cors_origins()
                self.assertIn(config.CORS_ORIGINS_ENV, str(ctx.exception))
                self.assertIn("'*'", str(ctx.exception))

    def test_invalid_entries_are_refused(self):
        invalid = [
            "https://app.example.com/",          # trailing slash
            "https://app.example.com/path",      # path
            "https://app.example.com?x=1",       # query
            "https://app.example.com#frag",      # fragment
            "ftp://app.example.com",             # scheme
            "ws://app.example.com",              # scheme
            "app.example.com",                   # no scheme
            "//app.example.com",                 # no scheme
            "https://",                          # no host
            "https://:8000",                     # no host
            "https://app.example.com:",          # empty port
            "https://app.example.com:0",         # port out of range
            "https://app.example.com:65536",     # port out of range
            "https://app.example.com:abc",       # non-numeric port
            "https://user@app.example.com",      # userinfo
            "https://*.example.com",             # wildcard host
            "https://app example.com",           # whitespace
            "null",
        ]
        for value in invalid:
            with self.subTest(value=value), _env(f"{ALLOWED_ORIGIN},{value}"):
                with self.assertRaises(config.ConfigError) as ctx:
                    config.cors_origins()
                self.assertIn(config.CORS_ORIGINS_ENV, str(ctx.exception))
                self.assertIn(repr(value), str(ctx.exception))


def _app():
    app = FastAPI()

    @app.get("/limited")
    def limited():
        return JSONResponse(
            status_code=429,
            content={"detail": "synthetic rate limit"},
            headers={"Retry-After": "17"},
        )

    return app


class ConfigureCorsTests(unittest.TestCase):
    def _configured_client(self, value):
        app = _app()
        with _env(value):
            configure_cors(app)
        return app, TestClient(app)

    def _preflight(self, client, origin, method="GET"):
        return client.options(
            "/limited",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )

    def test_no_origins_adds_no_middleware(self):
        for value in (None, "", "  , "):
            with self.subTest(value=value):
                app, client = self._configured_client(value)
                self.assertEqual(app.user_middleware, [])
                response = client.get("/limited", headers={"Origin": ALLOWED_ORIGIN})
                self.assertEqual(response.status_code, 429)
                self.assertNotIn("access-control-allow-origin", response.headers)

    def test_middleware_options(self):
        app, _client = self._configured_client(f"{ALLOWED_ORIGIN},{OTHER_ALLOWED_ORIGIN}")
        self.assertEqual(len(app.user_middleware), 1)
        middleware = app.user_middleware[0]
        self.assertIs(middleware.cls, CORSMiddleware)
        self.assertEqual(middleware.kwargs, {
            "allow_origins": [ALLOWED_ORIGIN, OTHER_ALLOWED_ORIGIN],
            "allow_credentials": False,
            "allow_methods": ["GET", "POST", "PATCH", "DELETE"],
            "allow_headers": ["Authorization", "Content-Type"],
            "expose_headers": ["Retry-After"],
        })

    def test_invalid_value_raises_before_adding_middleware(self):
        app = _app()
        with _env("*"), self.assertRaises(config.ConfigError):
            configure_cors(app)
        self.assertEqual(app.user_middleware, [])

    def test_preflight_from_allowed_origin_is_allowed(self):
        _app_, client = self._configured_client(f"{ALLOWED_ORIGIN},{OTHER_ALLOWED_ORIGIN}")
        for origin in (ALLOWED_ORIGIN, OTHER_ALLOWED_ORIGIN):
            for method in ("GET", "POST", "PATCH", "DELETE"):
                with self.subTest(origin=origin, method=method):
                    response = self._preflight(client, origin, method)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["access-control-allow-origin"], origin)
                    self.assertIn(method, response.headers["access-control-allow-methods"])
                    allowed_headers = response.headers["access-control-allow-headers"].lower()
                    self.assertIn("authorization", allowed_headers)
                    self.assertIn("content-type", allowed_headers)
                    self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_preflight_from_disallowed_origin_gets_no_allow_origin(self):
        _app_, client = self._configured_client(ALLOWED_ORIGIN)
        response = self._preflight(client, DISALLOWED_ORIGIN)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_preflight_with_unlisted_method_is_refused(self):
        _app_, client = self._configured_client(ALLOWED_ORIGIN)
        response = self._preflight(client, ALLOWED_ORIGIN, method="PUT")
        self.assertEqual(response.status_code, 400)

    def test_simple_request_from_disallowed_origin_gets_no_allow_origin(self):
        _app_, client = self._configured_client(ALLOWED_ORIGIN)
        response = client.get("/limited", headers={"Origin": DISALLOWED_ORIGIN})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_retry_after_is_exposed_on_429(self):
        _app_, client = self._configured_client(ALLOWED_ORIGIN)
        response = client.get("/limited", headers={"Origin": ALLOWED_ORIGIN})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "17")
        self.assertEqual(response.headers["access-control-allow-origin"], ALLOWED_ORIGIN)
        exposed = [
            name.strip().lower()
            for name in response.headers["access-control-expose-headers"].split(",")
        ]
        self.assertIn("retry-after", exposed)
        self.assertNotIn("access-control-allow-credentials", response.headers)


if __name__ == "__main__":
    unittest.main()
