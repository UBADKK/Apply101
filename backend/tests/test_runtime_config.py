"""W1: configurable database URL, upload directory and .env loading.

Every default must equal the behaviour from before these variables existed.
Throwaway SQLite databases and temporary directories only; backend.app.main
is never imported, python-dotenv's load_dotenv is always mocked (the real
.env is never loaded), and apply101.db / uploads/ are never touched.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import config, models
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password
from backend.routers import profiles, users


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTHETIC_SECRET = "synthetic-test-secret-for-runtime-config-0123456789"
CONFIG_ENVS = (config.DATABASE_URL_ENV, config.UPLOAD_DIR_ENV, config.LOAD_DOTENV_ENV)


def _env_without_config(**values):
    env = {name: value for name, value in os.environ.items() if name not in CONFIG_ENVS}
    env.update(values)
    return env


class _FakeLocationsTestCase(unittest.TestCase):
    """A temp working directory and a fake repository root, so that path
    resolution never involves the real repository's files."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_runtime_config_paths_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.cwd = os.path.join(self.tmp_dir, "cwd")
        self.fake_repo = os.path.join(self.tmp_dir, "fake_repo")
        os.makedirs(self.cwd)
        os.makedirs(self.fake_repo)
        original_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, original_cwd)
        root_patcher = patch.object(config, "REPO_ROOT", config.Path(self.fake_repo))
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

    def env(self, **values):
        return patch.dict(os.environ, _env_without_config(**values), clear=True)

    @staticmethod
    def sqlite_url(path):
        return "sqlite:///" + path.replace("\\", "/")


class DatabaseUrlTests(_FakeLocationsTestCase):
    def test_default_is_the_relative_sqlite_file(self):
        with self.env():
            self.assertEqual(config.database_url(), "sqlite:///./apply101.db")
            self.assertTrue(config.database_is_default())

    def test_blank_value_keeps_the_default(self):
        with self.env(APPLY101_DATABASE_URL="   "):
            self.assertEqual(config.database_url(), config.DEFAULT_DATABASE_URL)
            self.assertTrue(config.database_is_default())

    def test_explicit_value_is_used(self):
        url = self.sqlite_url(os.path.join(self.tmp_dir, "demo.db"))
        with self.env(APPLY101_DATABASE_URL=url):
            self.assertEqual(config.database_url(), url)
            self.assertFalse(config.database_is_default())

    def test_other_spellings_of_the_default_file_are_still_default(self):
        for url in (
            "sqlite:///./apply101.db",
            "sqlite:///apply101.db",
            "sqlite:///.//apply101.db",
            self.sqlite_url(os.path.join(self.cwd, "apply101.db")),
            self.sqlite_url(os.path.join(self.fake_repo, "apply101.db")),
        ):
            with self.subTest(url=url), self.env(APPLY101_DATABASE_URL=url):
                self.assertTrue(config.database_is_default())

    def test_memory_and_server_databases_are_not_default(self):
        for url in ("sqlite://", "sqlite:///:memory:", "postgresql://synthetic@localhost/demo"):
            with self.subTest(url=url), self.env(APPLY101_DATABASE_URL=url):
                self.assertFalse(config.database_is_default())

    def test_database_module_builds_its_engine_from_the_setting(self):
        # Fresh interpreter, working directory and database path in a temp
        # dir: creating an engine opens no connection, so no file appears.
        tmp_dir = tempfile.mkdtemp(prefix="apply101_runtime_config_test_")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        script = (
            "from backend.app import database as d; "
            "print(d.DATABASE_URL); print(d.engine.url); print(d.CONNECT_ARGS)"
        )
        configured = self.sqlite_url(os.path.join(tmp_dir, "configured.db"))
        cases = (
            ({}, "sqlite:///./apply101.db"),
            ({config.DATABASE_URL_ENV: configured}, configured),
        )
        for extra, expected in cases:
            with self.subTest(expected=expected):
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=tmp_dir,
                    env=_env_without_config(PYTHONPATH=REPO_ROOT, **extra),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [expected, expected, "{'check_same_thread': False}"],
                )
        self.assertEqual(os.listdir(tmp_dir), [])


class SqliteUriTests(_FakeLocationsTestCase):
    """SQLite URI URLs can reach the default file under another spelling,
    so they are refused before any engine exists."""

    def uri_urls(self):
        fake_repo_db = os.path.join(self.fake_repo, "apply101.db").replace("\\", "/")
        temp_db = os.path.join(self.tmp_dir, "demo.db").replace("\\", "/")
        return (
            "sqlite:///file:apply101.db?uri=true",
            f"sqlite:///file:{fake_repo_db}?mode=ro&uri=true",
            f"sqlite:///file:{temp_db}?uri=true",
            f"sqlite:///{temp_db}?uri=true",
            f"sqlite:///file:{temp_db}",
            "sqlite+pysqlite:///file:apply101.db?uri=true",
        )

    def test_uri_urls_are_refused(self):
        for url in self.uri_urls():
            with self.subTest(url=url), self.env(APPLY101_DATABASE_URL=url):
                with self.assertRaises(config.ConfigError):
                    config.database_url()
                # Every check built on the URL fails closed as well.
                with self.assertRaises(config.ConfigError):
                    config.database_is_default()
                with self.assertRaises(config.ConfigError):
                    config.check_data_locations()

    def test_plain_urls_are_still_accepted(self):
        temp_db = os.path.join(self.tmp_dir, "demo.db").replace("\\", "/")
        for url in (
            None,
            "sqlite:///./apply101.db",
            "sqlite:///apply101.db",
            f"sqlite:///{temp_db}",
            f"sqlite:///{temp_db}?timeout=10",
            "sqlite://",
            "sqlite:///:memory:",
            "postgresql://synthetic@localhost/demo?sslmode=disable",
        ):
            env = {} if url is None else {config.DATABASE_URL_ENV: url}
            with self.subTest(url=url), self.env(**env):
                self.assertEqual(config.database_url(), url or config.DEFAULT_DATABASE_URL)

    def test_database_module_refuses_a_uri_url_at_import(self):
        # Fresh interpreter in a temp working directory: the import fails
        # with ConfigError before create_engine, and no file appears.
        tmp_dir = tempfile.mkdtemp(prefix="apply101_runtime_config_uri_test_")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        url = "sqlite:///file:" + os.path.join(tmp_dir, "uri.db").replace("\\", "/") + "?uri=true"
        result = subprocess.run(
            [sys.executable, "-c", "import backend.app.database"],
            cwd=tmp_dir,
            env=_env_without_config(PYTHONPATH=REPO_ROOT, **{config.DATABASE_URL_ENV: url}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ConfigError", result.stderr)
        self.assertEqual(os.listdir(tmp_dir), [])


class UploadDirTests(_FakeLocationsTestCase):
    def test_default_is_the_relative_uploads_dir(self):
        with self.env():
            self.assertEqual(config.upload_dir(), "uploads")
            self.assertTrue(config.upload_dir_is_default())

    def test_explicit_value_is_used(self):
        cv_dir = os.path.join(self.tmp_dir, "cv")
        with self.env(APPLY101_UPLOAD_DIR=cv_dir):
            self.assertEqual(config.upload_dir(), cv_dir)
            self.assertFalse(config.upload_dir_is_default())

    def test_other_spellings_of_the_default_dir_are_still_default(self):
        for value in ("uploads", "./uploads/", os.path.join(self.cwd, "uploads"),
                      os.path.join(self.fake_repo, "uploads")):
            with self.subTest(value=value), self.env(APPLY101_UPLOAD_DIR=value):
                self.assertTrue(config.upload_dir_is_default())


class DataLocationCheckTests(_FakeLocationsTestCase):
    def test_defaults_together_are_accepted(self):
        with self.env():
            config.check_data_locations()

    def test_separate_database_with_the_repository_uploads_is_refused(self):
        separate = self.sqlite_url(os.path.join(self.tmp_dir, "separate.db"))
        for upload_dir in (None, os.path.join(self.fake_repo, "uploads")):
            env = {config.DATABASE_URL_ENV: separate}
            if upload_dir is not None:
                env[config.UPLOAD_DIR_ENV] = upload_dir
            with self.subTest(upload_dir=upload_dir), self.env(**env):
                # Without APPLY101_UPLOAD_DIR, "uploads" resolves against the
                # working directory; make that the fake repository.
                os.chdir(self.fake_repo)
                try:
                    with self.assertRaises(config.ConfigError):
                        config.check_data_locations()
                finally:
                    os.chdir(self.cwd)

    def test_defaults_from_the_repository_root_are_accepted(self):
        os.chdir(self.fake_repo)
        try:
            with self.env():
                config.check_data_locations()
        finally:
            os.chdir(self.cwd)

    def test_fresh_working_dir_database_with_the_repository_uploads_is_refused(self):
        # No APPLY101_DATABASE_URL: ./apply101.db is a new file in the
        # working directory, not the repository's database.
        with self.env(APPLY101_UPLOAD_DIR=os.path.join(self.fake_repo, "uploads")):
            with self.assertRaises(config.ConfigError):
                config.check_data_locations()

    def test_moved_database_and_uploads_are_accepted(self):
        with self.env(
            APPLY101_DATABASE_URL=self.sqlite_url(os.path.join(self.tmp_dir, "separate.db")),
            APPLY101_UPLOAD_DIR=os.path.join(self.tmp_dir, "separate_uploads"),
        ):
            config.check_data_locations()

    def test_default_database_with_a_separate_upload_dir_is_accepted(self):
        with self.env(APPLY101_UPLOAD_DIR=os.path.join(self.tmp_dir, "separate_uploads")):
            config.check_data_locations()


class DotenvLoadingTests(unittest.TestCase):
    def _load_with(self, value):
        env = _env_without_config() if value is None else _env_without_config(APPLY101_LOAD_DOTENV=value)
        with patch.dict(os.environ, env, clear=True), \
                patch.object(config, "load_dotenv", return_value=True) as load_dotenv:
            return config.load_env_file(), load_dotenv

    def test_loads_by_default(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                loaded, load_dotenv = self._load_with(value)
                self.assertTrue(loaded)
                load_dotenv.assert_called_once_with()

    def test_explicit_on_values_load(self):
        for value in ("1", "true", "YES", " on "):
            with self.subTest(value=value):
                loaded, load_dotenv = self._load_with(value)
                self.assertTrue(loaded)
                load_dotenv.assert_called_once_with()

    def test_off_values_skip_loading(self):
        for value in ("0", "false", "No", "off"):
            with self.subTest(value=value):
                loaded, load_dotenv = self._load_with(value)
                self.assertFalse(loaded)
                load_dotenv.assert_not_called()

    def test_unknown_value_is_an_error_and_loads_nothing(self):
        for value in ("2", "maybe", "disabled"):
            with self.subTest(value=value):
                with patch.dict(os.environ, _env_without_config(APPLY101_LOAD_DOTENV=value), clear=True), \
                        patch.object(config, "load_dotenv") as load_dotenv:
                    with self.assertRaises(config.ConfigError):
                        config.load_env_file()
                    load_dotenv.assert_not_called()


class ConfiguredUploadDirRouteTests(unittest.TestCase):
    """upload-cv writes into APPLY101_UPLOAD_DIR instead of ./uploads."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_runtime_config_upload_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmp_dir, 'synthetic_test.db')}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(users.router)
        app.include_router(profiles.router)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        # The working directory is a separate temp dir, so a relative
        # "uploads" fallback would be observable there.
        self.workdir = os.path.join(self.tmp_dir, "workdir")
        self.cv_dir = os.path.join(self.tmp_dir, "configured_cv_store")
        os.makedirs(self.workdir)
        original_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, original_cwd)
        env_patcher = patch.dict(
            os.environ,
            {"JWT_SECRET_KEY": SYNTHETIC_SECRET, config.UPLOAD_DIR_ENV: self.cv_dir},
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _create_user_and_profile(self):
        session = self.session_factory()
        try:
            user = models.User(
                name="Synthetic User",
                mail="synthetic.config@example.com",
                password_hash=hash_password("a-valid-synthetic-password"),
                is_admin=False,
            )
            session.add(user)
            session.commit()
            profile = models.CandidateProfile(user_id=user.user_id, self_description="synthetic")
            session.add(profile)
            session.commit()
            return user.user_id, profile.profile_id, user.token_key
        finally:
            session.close()

    def test_cv_is_stored_in_the_configured_dir(self):
        user_id, profile_id, token_key = self._create_user_and_profile()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        pdf_path = os.path.join(self.tmp_dir, "synthetic.pdf")
        with open(pdf_path, "wb") as f:
            writer.write(f)
        with open(pdf_path, "rb") as f:
            response = self.client.post(
                f"/users/{user_id}/profiles/{profile_id}/upload-cv",
                headers={"Authorization": f"Bearer {create_access_token(user_id, token_key)}"},
                files={"file": ("synthetic_cv.pdf", f.read(), "application/pdf")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        expected_path = os.path.join(self.cv_dir, f"user_{user_id}_profile_{profile_id}_cv.pdf")
        self.assertEqual(response.json()["cv_file_path"], expected_path)
        self.assertTrue(os.path.isfile(expected_path))
        self.assertEqual(os.listdir(self.workdir), [])


if __name__ == "__main__":
    unittest.main()
