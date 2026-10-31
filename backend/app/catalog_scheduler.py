"""Daily bounded catalog sync inside the deployed web service.

Off unless APPLY101_CATALOG_SYNC_ENABLED is on. When on, a background thread
in the web process runs catalog_sync.run_sync once a UTC day, after
APPLY101_CATALOG_SYNC_HOUR_UTC, on the same database as the app:

- Only boards an admin enabled are synced (run_sync's own rule); Arbeitnow
  is never included, and no terms acknowledgement is ever passed, so a
  source whose terms need one is never synced from here.
- At most APPLY101_CATALOG_SYNC_MAX_BOARDS boards and
  APPLY101_CATALOG_SYNC_MAX_PAGES pages per board (catalog_sync limits).
- One run per day across restarts and processes: the day is claimed first
  with an insert into analysis_guards, whose primary key makes the claim
  atomic. A day whose run failed, was cut off by a restart or found a
  manual sync running is not retried; the next day runs again. run_sync's
  lease keeps a manual sync and this one apart.
"""
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import catalog_sync, models
from .config import ConfigError
from .job_sources import HttpClient


logger = logging.getLogger("apply101.catalog_scheduler")

ENABLED_ENV = "APPLY101_CATALOG_SYNC_ENABLED"
HOUR_ENV = "APPLY101_CATALOG_SYNC_HOUR_UTC"
MAX_BOARDS_ENV = "APPLY101_CATALOG_SYNC_MAX_BOARDS"
MAX_PAGES_ENV = "APPLY101_CATALOG_SYNC_MAX_PAGES"

DEFAULT_HOUR_UTC = 3
DAILY_CLAIM_OPERATION_TYPE = "catalog_sync_daily"
POLL_SECONDS = 300

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class SchedulerSettings:
    enabled: bool
    hour_utc: int
    max_boards: int
    max_pages: int


def _int_setting(environ, name: str, default: int, low: int, high: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number; got {raw!r}.")
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}; got {value}.")
    return value


def settings_from_env(environ=None) -> SchedulerSettings:
    environ = os.environ if environ is None else environ
    raw = (environ.get(ENABLED_ENV) or "").strip().lower()
    if raw in _TRUE_VALUES:
        enabled = True
    elif not raw or raw in _FALSE_VALUES:
        enabled = False
    else:
        raise ConfigError(f"{ENABLED_ENV} must be one of 1/0, true/false, yes/no, on/off; got {raw!r}.")
    return SchedulerSettings(
        enabled=enabled,
        hour_utc=_int_setting(environ, HOUR_ENV, DEFAULT_HOUR_UTC, 0, 23),
        max_boards=_int_setting(environ, MAX_BOARDS_ENV, catalog_sync.DEFAULT_MAX_BOARDS, 1, catalog_sync.MAX_BOARDS_LIMIT),
        max_pages=_int_setting(environ, MAX_PAGES_ENV, catalog_sync.DEFAULT_MAX_PAGES, 1, catalog_sync.MAX_PAGES_LIMIT),
    )


def _day_key(day: date) -> int:
    return int(day.strftime("%Y%m%d"))


def claim_day(db: Session, day: date) -> bool:
    """True for the first caller of the day, False for every later one."""
    db.add(models.AnalysisGuard(operation_type=DAILY_CLAIM_OPERATION_TYPE, resource_id=_day_key(day)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def run_daily_once(
    session_factory: Callable[[], Session],
    settings: SchedulerSettings,
    *,
    http_factory: Callable[[], HttpClient] = HttpClient,
    now: datetime | None = None,
    run_sync: Callable[..., dict] = catalog_sync.run_sync,
) -> dict | None:
    """Runs today's sync if it is due and unclaimed; None when nothing ran."""
    now = now or catalog_sync.utcnow()
    if now.hour < settings.hour_utc:
        return None
    db = session_factory()
    try:
        if not claim_day(db, now.date()):
            return None
        try:
            result = run_sync(
                db,
                http_factory(),
                include_arbeitnow=False,
                max_boards=settings.max_boards,
                max_pages=settings.max_pages,
            )
        except catalog_sync.SyncAlreadyRunning:
            # Either another sync held the lease at the start, or this run
            # lost it midway. The day stays claimed either way, so the two
            # can never overlap; today's daily run is over.
            logger.warning("Daily catalog sync stopped: the catalog sync lease is held by another sync "
                           "(a manual sync, or this run lost its lease). Today's daily run is skipped.")
            return {"skipped": "sync_running"}
        logger.info(
            "Daily catalog sync finished: %s boards, stopped_reason=%s.",
            result.get("boards_synced"),
            result.get("stopped_reason"),
        )
        return result
    finally:
        db.close()


class CatalogScheduler:
    """The background thread; start() is a no-op when the sync is off."""

    def __init__(self, session_factory: Callable[[], Session], settings: SchedulerSettings,
                 poll_seconds: float = POLL_SECONDS, **run_kwargs):
        self._session_factory = session_factory
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._run_kwargs = run_kwargs
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self._settings.enabled or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._loop, name="catalog-scheduler", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float | None = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_daily_once(self._session_factory, self._settings, **self._run_kwargs)
            except Exception:  # noqa: BLE001 -- the thread must outlive one bad run
                logger.exception("Daily catalog sync failed.")
            self._stop.wait(self._poll_seconds)
