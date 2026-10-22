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
- APPLY101_CORS_ORIGINS: comma-separated browser origins allowed by CORS
  (e.g. "https://app.example.com,http://localhost:5173"); default none,
  i.e. no CORS middleware at all. Each entry is http:// or https:// plus a
  host and an optional port -- no path, query, fragment or trailing slash;
  "*" or any other entry is an error rather than a guess.

The names carry an APPLY101_ prefix so that an unrelated variable in an
existing .env (e.g. a generic DATABASE_URL) can never silently move the
default database.

Only backend/app/main.py loads .env; migrations and scripts read these
variables from the process environment alone.
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


DATABASE_URL_ENV = "APPLY101_DATABASE_URL"
UPLOAD_DIR_ENV = "APPLY101_UPLOAD_DIR"
LOAD_DOTENV_ENV = "APPLY101_LOAD_DOTENV"
CORS_ORIGINS_ENV = "APPLY101_CORS_ORIGINS"

DEFAULT_DATABASE_FILE = "apply101.db"
DEFAULT_DATABASE_URL = f"sqlite:///./{DEFAULT_DATABASE_FILE}"
DEFAULT_UPLOAD_DIR = "uploads"

# The repository root, where the real local apply101.db and uploads/ live.
REPO_ROOT = Path(__file__).resolve().parents[2]

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# scheme://host[:port] exactly as a browser sends it in the Origin header:
# a DNS name / IPv4 address or a bracketed IPv6 address, nothing after the
# optional port.
_CORS_ORIGIN_PATTERN = re.compile(
    r"https?://(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])(?::(\d{1,5}))?"
)


class ConfigError(ValueError):
    """An APPLY101_* variable has a value that cannot be used."""


def _env_value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def database_url() -> str:
    """APPLY101_DATABASE_URL or the default. SQLite URI-style URLs are
    refused here, before any engine or connection exists (see
    _reject_sqlite_uri)."""
    url = _env_value(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL
    _reject_sqlite_uri(url)
    return url


def _reject_sqlite_uri(url: str) -> None:
    """SQLite URI URLs (a "uri" query parameter, or a "file:" database such
    as sqlite:///file:apply101.db?uri=true) can reach the same file as a
    plain path while the default-database checks below compare paths only,
    so they are not supported; use a plain sqlite:///<path> URL."""
    try:
        parsed = make_url(url)
    except ArgumentError:
        return  # Not a URL at all: left to SQLAlchemy, as before.
    if parsed.get_backend_name() != "sqlite":
        return
    if "uri" in parsed.query or (parsed.database or "").startswith("file:"):
        raise ConfigError(
            f"{DATABASE_URL_ENV} must not use a SQLite URI (a 'uri' query parameter "
            "or a 'file:' database); use a plain sqlite:///<path> URL."
        )


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
    are fine. Also validates the database URL (see _reject_sqlite_uri) so
    startup fails before anything imports database.py.
    """
    database_url()
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


def cors_origins() -> list[str]:
    """APPLY101_CORS_ORIGINS as a list; unset or blank is []. Entries are
    stripped and empty ones ignored. "*" and anything that is not
    http(s)://host[:port] raise ConfigError."""
    origins = []
    for entry in _env_value(CORS_ORIGINS_ENV).split(","):
        origin = entry.strip()
        if not origin:
            continue
        if origin == "*":
            raise ConfigError(
                f"{CORS_ORIGINS_ENV} must list explicit origins; '*' is not allowed."
            )
        match = _CORS_ORIGIN_PATTERN.fullmatch(origin)
        if match is None or (match.group(1) is not None and not 1 <= int(match.group(1)) <= 65535):
            raise ConfigError(
                f"{CORS_ORIGINS_ENV} entries must be http:// or https:// plus a host "
                "and an optional port, with no path, query, fragment or trailing "
                f"slash (e.g. https://app.example.com); got {origin!r}."
            )
        origins.append(origin)
    return origins


def load_env_file() -> bool:
    """Loads a .env file exactly as backend/app/main.py always did -- with
    python-dotenv's search starting from this package's directory and
    walking up, never overriding variables that are already set -- unless
    APPLY101_LOAD_DOTENV turns it off. Returns True when a file was loaded.
    """
    if not dotenv_loading_enabled():
        return False
    return load_dotenv()
