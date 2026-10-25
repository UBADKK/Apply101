"""Bounded sync of public job boards into the shared catalog.

Only catalog rows are touched: jobs with created_by_user_id IS NULL, their
JobSourceRecord rows, companies and boards. Personal (manual or imported)
jobs, CVs and notes are never read or linked here.

Rules (see docs/ONLINE_MVP_PROGRESS.md, "Design decisions"):
- A posting is identified by (source, source_key) through JobSourceRecord,
  so a second run creates no duplicates. A legacy catalog job (from the old
  POST /jobs/fetch, keyed only by URL) is adopted by its URL.
- Exact copies across sources (same public URL, or same company with the
  same normalized title, location and content hash) are linked to the
  existing job by a non-primary record. Similar titles are never merged.
- Only a job's primary record updates its content. A changed content hash
  marks the job's analyses and matches as not current.
- Only a complete board fetch closes jobs: records of the board that were
  not seen become inactive, and a job with no active record left is closed.
  Failed, partial and capped fetches close nothing.
- One sync at a time (a lease row in analysis_guards).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .job_sources import BoardFetch, HttpClient, NormalizedPosting, source_info


SYNC_LOCK_OPERATION_TYPE = "catalog_sync"
SYNC_LOCK_RESOURCE_ID = 0
SYNC_LOCK_LEASE_SECONDS = 10 * 60
# A run starts no new board after this many seconds; the lease is renewed
# before every board, so it never expires while a run is still going.
SYNC_TIME_BUDGET_SECONDS = 15 * 60

DEFAULT_MAX_BOARDS = 10
MAX_BOARDS_LIMIT = 50
DEFAULT_MAX_PAGES = 10
MAX_PAGES_LIMIT = 50

STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class SyncAlreadyRunning(RuntimeError):
    pass


class BoardNotSyncable(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def valid_board_token(token: str) -> bool:
    return isinstance(token, str) and bool(_TOKEN_PATTERN.fullmatch(token))


def normalize_company_name(name: str) -> str:
    text = re.sub(r"[^\w\s]", " ", (name or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_field(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def job_content_hash(title: str | None, company_name: str | None, location: str | None, description: str | None) -> str:
    """Hash of the inputs a job analysis reads (see routers/jobs.py's job
    analysis prompt: title, company, location, description)."""
    payload = "\x1f".join(
        (title or "", company_name or "", location or "", description or "")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_create_company(db: Session, name: str | None, now: datetime) -> models.Company | None:
    normalized = normalize_company_name(name or "")
    if not normalized:
        return None
    company = db.query(models.Company).filter(models.Company.normalized_name == normalized).first()
    if company is None:
        company = models.Company(
            name=name.strip()[:300],
            normalized_name=normalized,
            created_at=now,
            updated_at=now,
        )
        db.add(company)
        db.flush()
    return company


def mark_job_results_not_current(db: Session, job_id: int) -> None:
    """After a content change: the job's analyses and matches no longer
    describe it, so nothing may present them as current."""
    db.query(models.JobAnalysis).filter(
        models.JobAnalysis.job_id == job_id,
        models.JobAnalysis.is_current == True,  # noqa: E712
    ).update({models.JobAnalysis.is_current: False}, synchronize_session=False)
    db.query(models.JobMatch).filter(
        models.JobMatch.job_id == job_id,
        models.JobMatch.is_current == True,  # noqa: E712
    ).update({models.JobMatch.is_current: False}, synchronize_session=False)


@dataclass
class ApplyCounts:
    seen: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    linked_duplicates: int = 0
    reopened: int = 0
    closed: int = 0
    skipped: int = 0
    sample_errors: list[str] = field(default_factory=list)


def _posting_company_name(posting: NormalizedPosting, company: models.Company | None) -> str | None:
    return posting.company_name or (company.name if company is not None else None)


def _apply_content(job: models.Job, posting: NormalizedPosting, company_name: str | None, content_hash: str, now: datetime) -> None:
    job.title = posting.title
    job.company_name = company_name
    job.location = posting.location
    job.description_text = posting.description_text
    job.work_mode = posting.work_mode
    job.country_code = posting.country_code
    job.source_updated_at = posting.source_updated_at
    job.content_hash = content_hash
    job.updated_at = now


def _find_exact_copy(
    db: Session,
    posting: NormalizedPosting,
    company: models.Company | None,
    content_hash: str,
) -> models.Job | None:
    by_url = (
        db.query(models.Job)
        .filter(models.Job.url == posting.url, models.Job.created_by_user_id.is_(None))
        .first()
    )
    if by_url is not None:
        return by_url
    if company is None:
        return None
    candidates = (
        db.query(models.Job)
        .filter(
            models.Job.created_by_user_id.is_(None),
            models.Job.company_id == company.company_id,
            models.Job.content_hash == content_hash,
            models.Job.job_status != STATUS_CLOSED,
        )
        .all()
    )
    for job in candidates:
        if _normalize_field(job.title) == _normalize_field(posting.title) and _normalize_field(job.location) == _normalize_field(posting.location):
            return job
    return None


def apply_postings(
    db: Session,
    *,
    source: str,
    board: models.JobSourceBoard | None,
    fetch: BoardFetch,
    now: datetime,
    board_company: models.Company | None = None,
) -> ApplyCounts:
    """Writes one board's postings (no commit). Closes unseen records only
    when fetch.complete."""
    counts = ApplyCounts(skipped=len(fetch.skipped))
    seen_record_ids: set[int] = set()
    seen_keys: set[str] = set()
    board_id = board.board_id if board is not None else None

    for posting in fetch.postings:
        if posting.source_key in seen_keys:
            counts.skipped += 1
            continue
        seen_keys.add(posting.source_key)
        counts.seen += 1

        company = board_company or get_or_create_company(db, posting.company_name, now)
        company_name = _posting_company_name(posting, company)
        content_hash = job_content_hash(posting.title, company_name, posting.location, posting.description_text)

        record = (
            db.query(models.JobSourceRecord)
            .filter(
                models.JobSourceRecord.source == source,
                models.JobSourceRecord.source_key == posting.source_key,
            )
            .first()
        )
        job = None
        if record is not None:
            job = (
                db.query(models.Job)
                .filter(models.Job.job_id == record.job_id, models.Job.created_by_user_id.is_(None))
                .first()
            )
            if job is None:
                # The job row is gone (deleted); start over for this posting.
                db.delete(record)
                db.flush()
                record = None

        if record is None:
            job = _find_exact_copy(db, posting, company, content_hash)
            if job is not None:
                has_records = (
                    db.query(models.JobSourceRecord.record_id)
                    .filter(models.JobSourceRecord.job_id == job.job_id)
                    .first()
                    is not None
                )
                record = models.JobSourceRecord(
                    job_id=job.job_id,
                    board_id=board_id,
                    source=source,
                    source_key=posting.source_key,
                    url=posting.url,
                    # A legacy job without records is adopted by its
                    # source; otherwise this is a linked copy.
                    is_primary=not has_records,
                    is_active=True,
                    content_hash=content_hash,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(record)
                db.flush()
                if has_records:
                    counts.linked_duplicates += 1
            else:
                job = models.Job(
                    title=posting.title,
                    company_name=company_name,
                    location=posting.location,
                    url=posting.url,
                    description_text=posting.description_text,
                    source=source,
                    source_job_id=posting.source_key[:500],
                    source_created_at=posting.source_created_at,
                    source_updated_at=posting.source_updated_at,
                    fetched_at=now,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                    job_status=STATUS_ACTIVE,
                    company_id=company.company_id if company is not None else None,
                    work_mode=posting.work_mode,
                    country_code=posting.country_code,
                    content_hash=content_hash,
                )
                db.add(job)
                db.flush()
                record = models.JobSourceRecord(
                    job_id=job.job_id,
                    board_id=board_id,
                    source=source,
                    source_key=posting.source_key,
                    url=posting.url,
                    is_primary=True,
                    is_active=True,
                    content_hash=content_hash,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(record)
                db.flush()
                seen_record_ids.add(record.record_id)
                counts.new += 1
                continue

        seen_record_ids.add(record.record_id)
        record.last_seen_at = now
        record.is_active = True
        record.closed_at = None
        record.url = posting.url
        record.content_hash = content_hash
        if board_id is not None and record.board_id is None:
            record.board_id = board_id

        job.last_seen_at = now
        if job.company_id is None and company is not None:
            job.company_id = company.company_id
        if job.job_status == STATUS_CLOSED:
            job.job_status = STATUS_ACTIVE
            job.closed_at = None
            counts.reopened += 1
        elif job.job_status != STATUS_ACTIVE:
            job.job_status = STATUS_ACTIVE

        if record.is_primary:
            if job.content_hash != content_hash:
                legacy_same_text = job.content_hash is None and job_content_hash(
                    job.title, job.company_name, job.location, job.description_text
                ) == content_hash
                if legacy_same_text:
                    job.content_hash = content_hash
                    counts.unchanged += 1
                else:
                    _apply_content(job, posting, company_name, content_hash, now)
                    mark_job_results_not_current(db, job.job_id)
                    counts.updated += 1
            else:
                counts.unchanged += 1
            if job.work_mode is None and posting.work_mode:
                job.work_mode = posting.work_mode
            if job.country_code is None and posting.country_code:
                job.country_code = posting.country_code
        else:
            counts.unchanged += 1

    if fetch.complete:
        counts.closed = _close_unseen(db, source=source, board_id=board_id, seen_record_ids=seen_record_ids, now=now)
        if source == "arbeitnow" and board_id is None:
            counts.closed += _close_unseen_legacy_arbeitnow(db, now)
    return counts


def _close_unseen_legacy_arbeitnow(db: Session, now: datetime) -> int:
    """After a complete Arbeitnow fetch (the whole feed was read), legacy
    Arbeitnow catalog jobs from the old POST /jobs/fetch that were not seen
    -- seen ones were adopted and now have a record -- are no longer listed:
    close them."""
    has_record = db.query(models.JobSourceRecord.record_id).filter(
        models.JobSourceRecord.job_id == models.Job.job_id
    ).exists()
    jobs = (
        db.query(models.Job)
        .filter(
            models.Job.created_by_user_id.is_(None),
            models.Job.source == "arbeitnow",
            or_(models.Job.job_status.is_(None), models.Job.job_status != STATUS_CLOSED),
            ~has_record,
        )
        .all()
    )
    for job in jobs:
        job.job_status = STATUS_CLOSED
        job.closed_at = now
    return len(jobs)


def _close_unseen(db: Session, *, source: str, board_id: int | None, seen_record_ids: set[int], now: datetime) -> int:
    query = db.query(models.JobSourceRecord).filter(
        models.JobSourceRecord.source == source,
        models.JobSourceRecord.is_active == True,  # noqa: E712
    )
    if board_id is None:
        query = query.filter(models.JobSourceRecord.board_id.is_(None))
    else:
        query = query.filter(models.JobSourceRecord.board_id == board_id)
    unseen = [record for record in query.all() if record.record_id not in seen_record_ids]
    affected_job_ids = set()
    for record in unseen:
        record.is_active = False
        record.closed_at = now
        affected_job_ids.add(record.job_id)
    db.flush()

    return _close_jobs_without_active_records(db, affected_job_ids, now)


def _close_jobs_without_active_records(db: Session, job_ids: Iterable[int], now: datetime) -> int:
    """Closes each job with no active record left. A job that keeps an
    active copy but lost its primary record gets that copy as its new
    primary, so its content stays updated."""
    closed = 0
    for job_id in job_ids:
        active_records = (
            db.query(models.JobSourceRecord)
            .filter(
                models.JobSourceRecord.job_id == job_id,
                models.JobSourceRecord.is_active == True,  # noqa: E712
            )
            .order_by(models.JobSourceRecord.record_id.asc())
            .all()
        )
        if active_records:
            if not any(record.is_primary for record in active_records):
                db.query(models.JobSourceRecord).filter(
                    models.JobSourceRecord.job_id == job_id,
                    models.JobSourceRecord.is_primary == True,  # noqa: E712
                ).update({models.JobSourceRecord.is_primary: False}, synchronize_session=False)
                active_records[0].is_primary = True
            continue
        job = (
            db.query(models.Job)
            .filter(models.Job.job_id == job_id, models.Job.created_by_user_id.is_(None))
            .first()
        )
        if job is not None and job.job_status != STATUS_CLOSED:
            job.job_status = STATUS_CLOSED
            job.closed_at = now
            closed += 1
    return closed


# --- sync lock -------------------------------------------------------------

def acquire_sync_lock(db: Session, now_epoch: float | None = None) -> str:
    now_epoch = time.time() if now_epoch is None else now_epoch
    token = secrets.token_hex(16)
    expires = now_epoch + SYNC_LOCK_LEASE_SECONDS
    guard = (
        db.query(models.AnalysisGuard)
        .filter(
            models.AnalysisGuard.operation_type == SYNC_LOCK_OPERATION_TYPE,
            models.AnalysisGuard.resource_id == SYNC_LOCK_RESOURCE_ID,
        )
        .first()
    )
    if guard is None:
        db.add(models.AnalysisGuard(
            operation_type=SYNC_LOCK_OPERATION_TYPE,
            resource_id=SYNC_LOCK_RESOURCE_ID,
            owner_token=token,
            lock_expires_at=expires,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise SyncAlreadyRunning("Another catalog sync is running.")
        return token
    if guard.owner_token and guard.lock_expires_at and guard.lock_expires_at > now_epoch:
        raise SyncAlreadyRunning("Another catalog sync is running.")
    previous = guard.owner_token
    updated = (
        db.query(models.AnalysisGuard)
        .filter(
            models.AnalysisGuard.operation_type == SYNC_LOCK_OPERATION_TYPE,
            models.AnalysisGuard.resource_id == SYNC_LOCK_RESOURCE_ID,
            or_(models.AnalysisGuard.owner_token == previous, models.AnalysisGuard.owner_token.is_(None))
            if previous is not None
            else models.AnalysisGuard.owner_token.is_(None),
        )
        .update(
            {models.AnalysisGuard.owner_token: token, models.AnalysisGuard.lock_expires_at: expires},
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        raise SyncAlreadyRunning("Another catalog sync is running.")
    return token


def renew_sync_lock(db: Session, token: str, now_epoch: float | None = None) -> None:
    """Extends the lease; raises SyncAlreadyRunning when it was lost."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    updated = (
        db.query(models.AnalysisGuard)
        .filter(
            models.AnalysisGuard.operation_type == SYNC_LOCK_OPERATION_TYPE,
            models.AnalysisGuard.resource_id == SYNC_LOCK_RESOURCE_ID,
            models.AnalysisGuard.owner_token == token,
        )
        .update({models.AnalysisGuard.lock_expires_at: now_epoch + SYNC_LOCK_LEASE_SECONDS}, synchronize_session=False)
    )
    db.commit()
    if updated != 1:
        raise SyncAlreadyRunning("The catalog sync lease was lost.")


def release_sync_lock(db: Session, token: str) -> None:
    db.rollback()
    db.query(models.AnalysisGuard).filter(
        models.AnalysisGuard.operation_type == SYNC_LOCK_OPERATION_TYPE,
        models.AnalysisGuard.resource_id == SYNC_LOCK_RESOURCE_ID,
        models.AnalysisGuard.owner_token == token,
    ).update(
        {models.AnalysisGuard.owner_token: None, models.AnalysisGuard.lock_expires_at: None},
        synchronize_session=False,
    )
    db.commit()


# --- running a sync ---------------------------------------------------------

def check_board(http: HttpClient, source: str, board_token: str, region: str, max_pages: int) -> dict:
    """Reads a board without writing anything (for verification)."""
    info = source_info(source)
    if info is None:
        raise BoardNotSyncable(f"Unknown source {source!r}.")
    fetch = info.fetch_board(http, board_token, region, max_pages)
    countries: dict[str, int] = {}
    work_modes: dict[str, int] = {}
    for posting in fetch.postings:
        countries[posting.country_code or "unknown"] = countries.get(posting.country_code or "unknown", 0) + 1
        work_modes[posting.work_mode or "unknown"] = work_modes.get(posting.work_mode or "unknown", 0) + 1
    return {
        "source": source,
        "board_token": board_token,
        "region": region,
        "complete": fetch.complete,
        "pages_checked": fetch.pages_checked,
        "active_postings": len(fetch.postings),
        "skipped": len(fetch.skipped),
        "stopped_reason": fetch.stopped_reason,
        "countries": countries,
        "work_modes": work_modes,
    }


def _record_run(db: Session, *, source: str, board_id: int | None, params: dict, started_at: datetime) -> models.JobFetchRun:
    run = models.JobFetchRun(
        source=source,
        fetch_type="catalog_sync",
        params_json=json.dumps(params),
        started_at=started_at,
        status="running",
        board_id=board_id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def sync_one(
    db: Session,
    http: HttpClient,
    *,
    source: str,
    board: models.JobSourceBoard | None,
    max_pages: int,
    now_fn: Callable[[], datetime] = utcnow,
) -> dict:
    info = source_info(source)
    if info is None:
        raise BoardNotSyncable(f"Unknown source {source!r}.")
    started_at = now_fn()
    board_id = board.board_id if board is not None else None
    run = _record_run(
        db,
        source=source,
        board_id=board_id,
        params={
            "board_token": board.board_token if board is not None else None,
            "region": board.region if board is not None else "global",
            "max_pages": max_pages,
        },
        started_at=started_at,
    )
    run_id = run.run_id
    board_token = board.board_token if board is not None else ""
    region = board.region if board is not None else "global"

    now = started_at
    try:
        fetch = info.fetch_board(http, board_token, region, max_pages)
        now = now_fn()
        board_company = None
        if board is not None:
            board = db.query(models.JobSourceBoard).filter(models.JobSourceBoard.board_id == board_id).first()
            board_company = db.query(models.Company).filter(models.Company.company_id == board.company_id).first()
            if board_company is not None and fetch.company_about and not board_company.about_text:
                board_company.about_text = fetch.company_about
                board_company.about_source_url = fetch.company_about_url
                board_company.updated_at = now
        counts = apply_postings(db, source=source, board=board, fetch=fetch, now=now, board_company=board_company)
        if fetch.complete:
            status = "completed"
        elif fetch.postings:
            status = "partial_failed"
        else:
            status = "failed"
        if board is not None:
            board.last_synced_at = now
            board.last_sync_status = status if fetch.stopped_reason is None else f"{status}:{fetch.stopped_reason}"
            if fetch.complete:
                board.verification_status = "verified"
                board.verified_at = now
                board.last_active_jobs_count = counts.seen
            elif not fetch.postings and board.verification_status != "verified":
                board.verification_status = "failed"
            board.updated_at = now
        run = db.query(models.JobFetchRun).filter(models.JobFetchRun.run_id == run_id).first()
        run.finished_at = now
        run.pages_checked = fetch.pages_checked
        run.jobs_seen_count = counts.seen
        run.new_jobs_count = counts.new
        run.duplicate_jobs_count = counts.unchanged
        run.skipped_jobs_count = counts.skipped
        run.updated_jobs_count = counts.updated
        run.closed_jobs_count = counts.closed
        run.linked_duplicates_count = counts.linked_duplicates
        run.complete = fetch.complete
        run.stopped_reason = fetch.stopped_reason or ("COMPLETE" if fetch.complete else None)
        run.status = status
        run.error_message = fetch.error_message
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.query(models.JobFetchRun).filter(models.JobFetchRun.run_id == run_id).first()
        if run is not None:
            run.finished_at = now_fn()
            run.status = "failed"
            run.complete = False
            run.stopped_reason = "WRITE_ERROR"
            run.error_message = type(exc).__name__
            db.commit()
        raise
    return {
        "run_id": run_id,
        "source": source,
        "board_id": board_id,
        "board_token": board.board_token if board is not None else None,
        "status": status,
        "complete": fetch.complete,
        "stopped_reason": fetch.stopped_reason,
        "pages_checked": fetch.pages_checked,
        "seen": counts.seen,
        "new": counts.new,
        "updated": counts.updated,
        "unchanged": counts.unchanged,
        "linked_duplicates": counts.linked_duplicates,
        "reopened": counts.reopened,
        "closed": counts.closed,
        "skipped": counts.skipped,
    }


def select_boards(
    db: Session,
    *,
    board_ids: Iterable[int] | None,
    sources: Iterable[str] | None,
    max_boards: int,
) -> list[models.JobSourceBoard]:
    query = db.query(models.JobSourceBoard).filter(models.JobSourceBoard.is_enabled == True)  # noqa: E712
    if board_ids:
        query = query.filter(models.JobSourceBoard.board_id.in_(list(board_ids)))
    if sources:
        query = query.filter(models.JobSourceBoard.source.in_(list(sources)))
    # Least recently synced first, so repeated bounded runs cover every board.
    boards = query.order_by(
        models.JobSourceBoard.last_synced_at.is_(None).desc(),
        models.JobSourceBoard.last_synced_at.asc(),
        models.JobSourceBoard.board_id.asc(),
    ).limit(max_boards).all()
    return [board for board in boards if source_info(board.source) is not None]


def run_sync(
    db: Session,
    http: HttpClient,
    *,
    include_arbeitnow: bool,
    board_ids: Iterable[int] | None = None,
    sources: Iterable[str] | None = None,
    max_boards: int = DEFAULT_MAX_BOARDS,
    max_pages: int = DEFAULT_MAX_PAGES,
    pause_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = utcnow,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    max_boards = max(1, min(int(max_boards), MAX_BOARDS_LIMIT))
    max_pages = max(1, min(int(max_pages), MAX_PAGES_LIMIT))
    token = acquire_sync_lock(db)
    started = clock()
    results = []
    stopped_reason = None
    try:
        if include_arbeitnow:
            results.append(sync_one(db, http, source="arbeitnow", board=None, max_pages=max_pages, now_fn=now_fn))
        boards = select_boards(db, board_ids=board_ids, sources=sources, max_boards=max_boards)
        board_ids_to_sync = [board.board_id for board in boards]
        for index, board_id in enumerate(board_ids_to_sync):
            if clock() - started > SYNC_TIME_BUDGET_SECONDS:
                stopped_reason = "TIME_BUDGET_REACHED"
                break
            if index or include_arbeitnow:
                sleep(pause_seconds)
            renew_sync_lock(db, token)
            board = db.query(models.JobSourceBoard).filter(models.JobSourceBoard.board_id == board_id).first()
            if board is None or not board.is_enabled:
                continue
            results.append(sync_one(db, http, source=board.source, board=board, max_pages=max_pages, now_fn=now_fn))
    finally:
        release_sync_lock(db, token)
    return {
        "stopped_reason": stopped_reason,
        "boards_synced": len(results),
        "max_boards": max_boards,
        "max_pages": max_pages,
        "results": results,
    }


# --- boards -----------------------------------------------------------------

COMPANY_BOARDS_FILE = Path(__file__).resolve().parent / "data" / "company_boards.json"


def create_board(
    db: Session,
    *,
    company_name: str,
    source: str,
    board_token: str,
    region: str,
    now: datetime,
    website: str | None = None,
    country_code: str | None = None,
    notes: str | None = None,
) -> tuple[models.JobSourceBoard, bool]:
    """Adds a disabled, unverified board (and its company) when missing.
    Returns (board, created). Raises BoardNotSyncable for invalid input."""
    info = source_info(source)
    if info is None or not info.per_company:
        raise BoardNotSyncable(f"{source!r} has no per-company boards.")
    if region not in info.regions:
        raise BoardNotSyncable(f"{info.label} has no {region!r} region.")
    if not valid_board_token(board_token):
        raise BoardNotSyncable("board_token may only contain letters, digits, '.', '_' and '-'.")
    existing = (
        db.query(models.JobSourceBoard)
        .filter(
            models.JobSourceBoard.source == source,
            models.JobSourceBoard.board_token == board_token,
            models.JobSourceBoard.region == region,
        )
        .first()
    )
    if existing is not None:
        return existing, False
    company = get_or_create_company(db, company_name, now)
    if company is None:
        raise BoardNotSyncable("company_name is empty.")
    if website and not company.website:
        company.website = website
    if country_code and not company.country_code:
        company.country_code = country_code
    board = models.JobSourceBoard(
        company_id=company.company_id,
        source=source,
        board_token=board_token,
        region=region,
        is_enabled=False,
        verification_status="unverified",
        notes=notes,
        created_at=now,
        updated_at=now,
    )
    db.add(board)
    db.flush()
    return board, True


def seed_boards(db: Session, now: datetime, path=None) -> dict:
    """Adds the boards of company_boards.json that are missing (no commit).
    Every added board starts disabled and unverified."""
    path = path or COMPANY_BOARDS_FILE
    entries = json.loads(path.read_text(encoding="utf-8"))["boards"]
    created = 0
    for entry in entries:
        _, was_created = create_board(
            db,
            company_name=entry["company"],
            source=entry["source"],
            board_token=entry["board_token"],
            region=entry.get("region", "global"),
            website=entry.get("website"),
            country_code=entry.get("country_code"),
            notes=entry.get("notes"),
            now=now,
        )
        created += int(was_created)
    return {"boards_in_file": len(entries), "created": created}



def close_board_jobs(db: Session, board_id: int, now: datetime) -> int:
    """When an admin disables a board: its records become inactive and its
    jobs close unless another source still lists them (no commit)."""
    records = (
        db.query(models.JobSourceRecord)
        .filter(
            models.JobSourceRecord.board_id == board_id,
            models.JobSourceRecord.is_active == True,  # noqa: E712
        )
        .all()
    )
    job_ids = set()
    for record in records:
        record.is_active = False
        record.closed_at = now
        job_ids.add(record.job_id)
    db.flush()
    return _close_jobs_without_active_records(db, job_ids, now)
