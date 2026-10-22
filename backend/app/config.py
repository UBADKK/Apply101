"""Runtime configuration read from APPLY101_* environment variables.

Every value defaults to the behaviour from before it became configurable, so
a run without these variables is unchanged:

- APPLY101_DATABASE_URL: SQLAlchemy URL; default sqlite:///./apply101.db
  (relative to the working directory).
- APPLY101_UPLOAD_DIR: CV upload directory; default "uploads" (relative to
  the working directory).
- APPLY101_LOAD_DOTENV: whether backend/app/main.py loads a .env file with
  python-dotenv; default on. 1/true/yes/on or 0/false/no/off; any other
  value is an error rather than a guess. python-dotenv's own
  PYTHON_DOTENV_DISABLED switch keeps working as well.

The names carry an APPLY101_ prefix so that an unrelated variable in an
existing .env (e.g. a generic DATABASE_URL) can never silently move the
default database.

Only backend/app/main.py loads .env; migrations and scripts read these
variables from the process environment alone.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


DATABASE_URL_ENV = "APPLY101_DATABASE_URL"
UPLOAD_DIR_ENV = "APPLY101_UPLOAD_DIR"
LOAD_DOTENV_ENV = "APPLY101_LOAD_DOTENV"

DEFAULT_DATABASE_FILE = "apply101.db"
DEFAULT_DATABASE_URL = f"sqlite:///./{DEFAULT_DATABASE_FILE}"
DEFAULT_UPLOAD_DIR = "uploads"

# The repository root, where the real local apply101.db and uploads/ live.
REPO_ROOT = Path(__file__).resolve().parents[2]

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """An APPLY101_* variable has a value that cannot be used."""


def _env_value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def database_url() -> str:
    return _env_value(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL


def upload_dir() -> str:
    return _env_value(UPLOAD_DIR_ENV) or DEFAULT_UPLOAD_DIR


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _sqlite_file(url: str) -> str | None:
    """The file of a file-backed SQLite URL, else None."""
    try:
        parsed = make_url(url)
    except ArgumentError:
        return None
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        return None
    return parsed.database


def database_is_default() -> bool:
    """True when the configured database is the default SQLite file --
    apply101.db in the working directory or in the repository root --
    however the URL spells its path."""
    path = _sqlite_file(database_url())
    return path is not None and any(
        _same_path(path, candidate)
        for candidate in (DEFAULT_DATABASE_FILE, REPO_ROOT / DEFAULT_DATABASE_FILE)
    )


def upload_dir_is_default() -> bool:
    """True when the configured upload directory is uploads/ in the working
    directory or in the repository root."""
    return any(
        _same_path(upload_dir(), candidate)
        for candidate in (DEFAULT_UPLOAD_DIR, REPO_ROOT / DEFAULT_UPLOAD_DIR)
    )


def _is_repository_database() -> bool:
    path = _sqlite_file(database_url())
    return path is not None and _same_path(path, REPO_ROOT / DEFAULT_DATABASE_FILE)


def check_data_locations() -> None:
    """Refuses the repository's real uploads/ directory with any database
    other than the repository's real apply101.db.

    CV files are named after database ids (user_{id}_profile_{id}_cv.pdf),
    so another database -- also a fresh ./apply101.db in some other working
    directory -- writing into the real uploads/ would replace real users'
    CVs. The defaults from the repository root, or both settings moved,
    are fine.
    """
    if _same_path(upload_dir(), REPO_ROOT / DEFAULT_UPLOAD_DIR) and not _is_repository_database():
        raise ConfigError(
            "CV uploads would go to the repository's uploads/ directory while the "
            f"database ({DATABASE_URL_ENV} or ./apply101.db in the working directory) "
            f"is not the repository's apply101.db; set {UPLOAD_DIR_ENV} to a "
            "directory that belongs to that database."
        )


def dotenv_loading_enabled() -> bool:
    value = _env_value(LOAD_DOTENV_ENV).lower()
    if not value or value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{LOAD_DOTENV_ENV} must be one of 1/0, true/false, yes/no, on/off; "
        f"got {value!r}."
    )


def load_env_file() -> bool:
    """Loads a .env file exactly as backend/app/main.py always did -- with
    python-dotenv's search starting from this package's directory and
    walking up, never overriding variables that are already set -- unless
    APPLY101_LOAD_DOTENV turns it off. Returns True when a file was loaded.
    """
    if not dotenv_loading_enabled():
        return False
    return load_dotenv()
