"""Single-service entry point for a private test deployment (Railway).

    uvicorn --factory backend.app.deploy_app:create_app --workers 1 ...

One process serves the built web client at / and the API (backend.app.main)
under /api, so the browser talks to one origin and no CORS is needed. It
refuses to start unless the environment is a safe test setup
(check_deploy_environment):

- .env loading is off (APPLY101_LOAD_DOTENV=0); secrets come from the
  environment only;
- the SQLite database and the upload directory are absolute paths inside
  APPLY101_DATA_DIR, which must be inside the Railway volume when one is
  mounted (RAILWAY_VOLUME_MOUNT_PATH);
- analyses use the fake provider and no OpenAI key is set, so no paid call
  can happen;
- JWT_SECRET_KEY and APPLY101_ACCESS_KEY are set.

Every page and API call needs the access key first (a form at /__access
sets an HttpOnly cookie); only /healthz answers without it. The daily
catalog sync (catalog_scheduler) runs in this process when enabled.

Importing this module does not import backend.app.main; create_app does,
after the checks.
"""
import hashlib
import hmac
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import catalog_scheduler
from .config import (
    DATABASE_URL_ENV,
    LOAD_DOTENV_ENV,
    UPLOAD_DIR_ENV,
    ConfigError,
    _reject_sqlite_uri,
    _sqlite_file,
)
from .openai_client import ANALYSIS_PROVIDER_ENV, FAKE_ANALYSIS_PROVIDER, OPENAI_API_KEY_ENV


DATA_DIR_ENV = "APPLY101_DATA_DIR"
ACCESS_KEY_ENV = "APPLY101_ACCESS_KEY"
WEB_DIR_ENV = "APPLY101_WEB_DIR"
RAILWAY_VOLUME_ENV = "RAILWAY_VOLUME_MOUNT_PATH"

MIN_ACCESS_KEY_LENGTH = 24
ACCESS_COOKIE = "apply101_access"
ACCESS_COOKIE_MAX_AGE = 30 * 24 * 3600
ACCESS_PATH = "/__access"
HEALTH_PATH = "/healthz"
API_PREFIX = "/api"
# Wrong keys per client address before /__access answers 429 for a while.
MAX_ACCESS_FAILURES = 10
ACCESS_FAILURE_WINDOW_SECONDS = 15 * 60

DEFAULT_WEB_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@dataclass(frozen=True)
class DeploySettings:
    data_dir: Path
    database_file: Path
    upload_dir: Path
    web_dir: Path
    access_key: str
    scheduler: catalog_scheduler.SchedulerSettings


def _inside(path: Path, root: Path) -> bool:
    path, root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return path == root or root in path.parents


def check_deploy_environment(environ=None) -> DeploySettings:
    """Returns the settings, or raises ConfigError listing every problem."""
    environ = os.environ if environ is None else environ
    errors: list[str] = []

    def value(name: str) -> str:
        return (environ.get(name) or "").strip()

    if value(LOAD_DOTENV_ENV).lower() not in {"0", "false", "no", "off"}:
        errors.append(f"{LOAD_DOTENV_ENV} must be 0: secrets come from the environment, never a .env file.")

    data_dir = Path(value(DATA_DIR_ENV)) if value(DATA_DIR_ENV) else None
    if data_dir is None or not data_dir.is_absolute():
        errors.append(f"{DATA_DIR_ENV} must be an absolute directory (the volume, e.g. /data).")
        data_dir = None
    elif not data_dir.is_dir():
        errors.append(f"{DATA_DIR_ENV} ({data_dir}) does not exist.")
    volume = value(RAILWAY_VOLUME_ENV)
    if data_dir is not None and volume and not _inside(data_dir, Path(volume)):
        errors.append(f"{DATA_DIR_ENV} ({data_dir}) must be inside the Railway volume ({volume}).")

    database_file = None
    if not value(DATABASE_URL_ENV):
        errors.append(f"{DATABASE_URL_ENV} must be set (sqlite:////<data dir>/<name>.db).")
    else:
        try:
            _reject_sqlite_uri(value(DATABASE_URL_ENV))
            database_file = _sqlite_file(value(DATABASE_URL_ENV))
        except ConfigError as exc:
            errors.append(str(exc))
        if database_file is None:
            errors.append(f"{DATABASE_URL_ENV} must be a file-backed SQLite URL.")
        else:
            database_file = Path(database_file)
            if not database_file.is_absolute():
                errors.append(f"{DATABASE_URL_ENV} must use an absolute path (sqlite:////data/...).")
            elif data_dir is not None and not _inside(database_file, data_dir):
                errors.append(f"The database ({database_file}) must be inside {DATA_DIR_ENV} ({data_dir}).")

    upload_dir = Path(value(UPLOAD_DIR_ENV)) if value(UPLOAD_DIR_ENV) else None
    if upload_dir is None or not upload_dir.is_absolute():
        errors.append(f"{UPLOAD_DIR_ENV} must be an absolute directory inside {DATA_DIR_ENV}.")
    elif data_dir is not None and not _inside(upload_dir, data_dir):
        errors.append(f"The upload directory ({upload_dir}) must be inside {DATA_DIR_ENV} ({data_dir}).")

    if value(ANALYSIS_PROVIDER_ENV).lower() != FAKE_ANALYSIS_PROVIDER:
        errors.append(f"{ANALYSIS_PROVIDER_ENV} must be {FAKE_ANALYSIS_PROVIDER!r} in the test deployment.")
    if value(OPENAI_API_KEY_ENV):
        errors.append(f"{OPENAI_API_KEY_ENV} must not be set in the test deployment (no paid calls).")

    if not value("JWT_SECRET_KEY"):
        errors.append("JWT_SECRET_KEY must be set.")
    access_key = value(ACCESS_KEY_ENV)
    if len(access_key) < MIN_ACCESS_KEY_LENGTH:
        errors.append(f"{ACCESS_KEY_ENV} must be set, at least {MIN_ACCESS_KEY_LENGTH} characters.")

    web_dir = Path(value(WEB_DIR_ENV)) if value(WEB_DIR_ENV) else DEFAULT_WEB_DIR
    if not (web_dir / "index.html").is_file():
        errors.append(f"The built web client is missing: no index.html in {web_dir} ({WEB_DIR_ENV}).")

    scheduler = None
    try:
        scheduler = catalog_scheduler.settings_from_env(environ)
    except ConfigError as exc:
        errors.append(str(exc))

    if errors:
        raise ConfigError("Refusing to start the test deployment:\n- " + "\n- ".join(errors))
    return DeploySettings(
        data_dir=data_dir,
        database_file=database_file,
        upload_dir=upload_dir,
        web_dir=web_dir,
        access_key=access_key,
        scheduler=scheduler,
    )


def access_token(access_key: str) -> str:
    """The cookie value; a new key invalidates every issued cookie."""
    return hmac.new(access_key.encode("utf-8"), b"apply101-access-v1", hashlib.sha256).hexdigest()


class _FailureLimiter:
    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self._limit, self._window, self._clock = limit, window, clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str) -> bool:
        with self._lock:
            now = self._clock()
            recent = [t for t in self._failures.get(client, []) if now - t < self._window]
            self._failures[client] = recent
            return len(recent) >= self._limit

    def record(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(self._clock())


def _access_page(message: str = "", status_code: int = 401) -> HTMLResponse:
    note = f'<p role="alert">{escape(message)}</p>' if message else ""
    return HTMLResponse(
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
        content=(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\"><title>Apply101 test</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem\">"
            "<h1>Apply101 test environment</h1><p>This is a private test environment.</p>"
            f"{note}<form method=\"post\" action=\"{ACCESS_PATH}\">"
            "<label for=\"key\">Access key</label><br>"
            "<input id=\"key\" name=\"key\" type=\"password\" autocomplete=\"current-password\" required "
            "style=\"width:100%;margin:.5rem 0\"><br><button type=\"submit\">Continue</button></form></body></html>"
        ),
    )


def _configure_logging() -> None:
    """uvicorn configures only its own loggers; the daily sync logs under
    "apply101", which goes to stderr (the Railway log) as well."""
    logger = logging.getLogger("apply101")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def create_app(api_app: FastAPI | None = None, *, environ=None, session_factory=None,
               scheduler_kwargs: dict | None = None) -> FastAPI:
    settings = check_deploy_environment(environ)
    _configure_logging()
    if session_factory is None:
        from .database import SessionLocal as session_factory
    if api_app is None:
        from .main import app as api_app  # imported only after the checks passed
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    scheduler = catalog_scheduler.CatalogScheduler(session_factory, settings.scheduler, **(scheduler_kwargs or {}))

    @asynccontextmanager
    async def lifespan(_app):
        if scheduler.start():
            logging.getLogger("apply101.deploy").info(
                "Daily catalog sync on: after %02d:00 UTC, at most %d boards and %d pages each.",
                settings.scheduler.hour_utc, settings.scheduler.max_boards, settings.scheduler.max_pages)
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.scheduler = scheduler
    expected_token = access_token(settings.access_key)
    limiter = _FailureLimiter(MAX_ACCESS_FAILURES, ACCESS_FAILURE_WINDOW_SECONDS)
    web_root = Path(os.path.realpath(settings.web_dir))

    @app.middleware("http")
    async def require_access_key(request: Request, call_next):
        path = request.url.path
        if path in (HEALTH_PATH, ACCESS_PATH):
            return await call_next(request)
        cookie = request.cookies.get(ACCESS_COOKIE, "")
        if hmac.compare_digest(cookie, expected_token):
            response = await call_next(request)
            response.headers.setdefault("X-Robots-Tag", "noindex")
            return response
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            return JSONResponse(status_code=401, content={"detail": "Access key required."})
        return _access_page()

    @app.get(HEALTH_PATH, include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.get(ACCESS_PATH, include_in_schema=False)
    def access_form():
        return _access_page(status_code=200)

    @app.post(ACCESS_PATH, include_in_schema=False)
    async def access_submit(request: Request):
        client = request.client.host if request.client else "unknown"
        if limiter.blocked(client):
            return _access_page("Too many wrong keys. Try again later.", status_code=429)
        form = await request.form()
        key = str(form.get("key") or "")
        if not hmac.compare_digest(key.encode("utf-8"), settings.access_key.encode("utf-8")):
            limiter.record(client)
            return _access_page("Wrong access key.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            ACCESS_COOKIE,
            expected_token,
            max_age=ACCESS_COOKIE_MAX_AGE,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return response

    app.mount(API_PREFIX, api_app)

    @app.get("/{path:path}", include_in_schema=False)
    def web_client(path: str):
        candidate = Path(os.path.realpath(web_root / path)) if path else web_root
        if path and candidate.is_file() and web_root in candidate.parents:
            return FileResponse(candidate)
        if path.startswith("assets/"):
            return Response(status_code=404)
        # Client-side routes (/catalog, /jobs/5, ...) all load the app shell.
        return FileResponse(web_root / "index.html", headers={"Cache-Control": "no-cache"})

    return app
