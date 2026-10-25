"""Admin-only management of the shared job catalog: sources, company boards,
bounded syncs and their run records. Normal users never reach these routes
(get_current_admin)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..app import catalog_sync, models, schemas
from ..app.auth_dependencies import get_current_admin
from ..app.database import get_db
from ..app.job_sources import SOURCES, HttpClient, source_info


router = APIRouter(prefix="/admin/catalog", tags=["Catalog admin"])


def make_http_client() -> HttpClient:
    """Replaced in tests; the real client talks to the public source APIs."""
    return HttpClient()


def _error(status_code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error_code": error_code, "message": message})


def _board_out(board: models.JobSourceBoard, company: models.Company | None) -> dict:
    info = source_info(board.source)
    return {
        "board_id": board.board_id,
        "company_id": board.company_id,
        "company_name": company.name if company else None,
        "company_country_code": company.country_code if company else None,
        "source": board.source,
        "board_token": board.board_token,
        "region": board.region,
        "is_enabled": board.is_enabled,
        "verification_status": board.verification_status,
        "verified_at": board.verified_at,
        "last_synced_at": board.last_synced_at,
        "last_sync_status": board.last_sync_status,
        "last_active_jobs_count": board.last_active_jobs_count,
        "source_auto_publish": info.auto_publish if info else False,
        "notes": board.notes,
    }


@router.get("/sources")
def list_sources(current_admin: models.User = Depends(get_current_admin)):
    return [
        {
            "source": info.source,
            "label": info.label,
            "per_company": info.per_company,
            "regions": list(info.regions),
            "docs_url": info.docs_url,
            "terms_url": info.terms_url,
            "attribution": info.attribution,
            "terms_checked": info.terms_checked,
            "auto_publish": info.auto_publish,
            "terms_note": info.terms_note,
        }
        for info in SOURCES.values()
    ]


@router.get("/boards")
def list_boards(
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(models.JobSourceBoard, models.Company)
        .outerjoin(models.Company, models.Company.company_id == models.JobSourceBoard.company_id)
        .order_by(models.JobSourceBoard.board_id.asc())
        .all()
    )
    return [_board_out(board, company) for board, company in rows]


def _create_board(db: Session, payload: schemas.CatalogBoardCreate, now) -> tuple[models.JobSourceBoard, bool]:
    try:
        return catalog_sync.create_board(
            db,
            company_name=payload.company_name,
            source=payload.source,
            board_token=payload.board_token,
            region=payload.region,
            website=payload.website,
            country_code=payload.country_code,
            notes=payload.notes,
            now=now,
        )
    except catalog_sync.BoardNotSyncable as exc:
        raise _error(422, "ERR_BOARD_INVALID", str(exc))


@router.post("/boards", status_code=201)
def create_board(
    payload: schemas.CatalogBoardCreate,
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    now = catalog_sync.utcnow()
    board, created = _create_board(db, payload, now)
    if not created:
        raise _error(409, "ERR_BOARD_EXISTS", "This board is already configured.")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _error(409, "ERR_BOARD_EXISTS", "This board is already configured.")
    db.refresh(board)
    company = db.query(models.Company).filter(models.Company.company_id == board.company_id).first()
    return _board_out(board, company)


@router.post("/boards/seed")
def seed_boards(
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Adds the boards of app/data/company_boards.json that are missing.
    Idempotent; every added board starts disabled and unverified."""
    result = catalog_sync.seed_boards(db, catalog_sync.utcnow())
    db.commit()
    return result


@router.patch("/boards/{board_id}")
def update_board(
    board_id: int,
    payload: schemas.CatalogBoardUpdate,
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    board = db.query(models.JobSourceBoard).filter(models.JobSourceBoard.board_id == board_id).first()
    if board is None:
        raise _error(404, "ERR_BOARD_NOT_FOUND", "Board not found.")
    info = source_info(board.source)
    if payload.is_enabled and info is not None and not info.auto_publish and not payload.acknowledge_terms:
        raise _error(
            409,
            "ERR_SOURCE_TERMS_UNCHECKED",
            f"The terms of {info.label} are not checked ({info.terms_url}). "
            "Check them, then enable the board with acknowledge_terms=true.",
        )
    board.is_enabled = payload.is_enabled
    board.updated_at = catalog_sync.utcnow()
    db.commit()
    company = db.query(models.Company).filter(models.Company.company_id == board.company_id).first()
    return _board_out(board, company)


@router.post("/boards/{board_id}/check")
def check_board(
    board_id: int,
    max_pages: int = Query(default=5, ge=1, le=catalog_sync.MAX_PAGES_LIMIT),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Reads the board live without publishing anything, and records whether
    it could be read."""
    board = db.query(models.JobSourceBoard).filter(models.JobSourceBoard.board_id == board_id).first()
    if board is None:
        raise _error(404, "ERR_BOARD_NOT_FOUND", "Board not found.")
    result = catalog_sync.check_board(make_http_client(), board.source, board.board_token, board.region, max_pages)
    now = catalog_sync.utcnow()
    if result["complete"]:
        board.verification_status = "verified"
        board.verified_at = now
        board.last_active_jobs_count = result["active_postings"]
    elif board.verification_status != "verified":
        board.verification_status = "failed"
    board.updated_at = now
    db.commit()
    return result


@router.post("/sync")
def sync_catalog(
    payload: schemas.CatalogSyncRequest,
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    try:
        return catalog_sync.run_sync(
            db,
            make_http_client(),
            include_arbeitnow=payload.include_arbeitnow,
            board_ids=payload.board_ids,
            sources=payload.sources,
            max_boards=payload.max_boards,
            max_pages=payload.max_pages,
        )
    except catalog_sync.SyncAlreadyRunning:
        raise _error(409, "ERR_CATALOG_SYNC_RUNNING", "Another catalog sync is running.")


@router.get("/runs")
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    runs = (
        db.query(models.JobFetchRun)
        .filter(models.JobFetchRun.fetch_type == "catalog_sync")
        .order_by(models.JobFetchRun.run_id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "run_id": run.run_id,
            "source": run.source,
            "board_id": run.board_id,
            "status": run.status,
            "complete": run.complete,
            "stopped_reason": run.stopped_reason,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "pages_checked": run.pages_checked,
            "seen": run.jobs_seen_count,
            "new": run.new_jobs_count,
            "updated": run.updated_jobs_count,
            "unchanged": run.duplicate_jobs_count,
            "linked_duplicates": run.linked_duplicates_count,
            "closed": run.closed_jobs_count,
            "skipped": run.skipped_jobs_count,
            "error_message": run.error_message,
        }
        for run in runs
    ]


@router.get("/summary")
def catalog_summary(
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Active catalog jobs per source and country, and boards per state."""
    catalog = models.Job.created_by_user_id.is_(None)
    active = models.Job.job_status != catalog_sync.STATUS_CLOSED
    by_source = dict(
        db.query(models.Job.source, func.count(models.Job.job_id))
        .filter(catalog, active)
        .group_by(models.Job.source)
        .all()
    )
    legacy_unknown = (
        db.query(func.count(models.Job.job_id))
        .filter(catalog, models.Job.job_status.is_(None))
        .scalar()
    )
    by_country = dict(
        db.query(func.coalesce(models.Job.country_code, "unknown"), func.count(models.Job.job_id))
        .filter(catalog, active)
        .group_by(func.coalesce(models.Job.country_code, "unknown"))
        .all()
    )
    by_work_mode = dict(
        db.query(func.coalesce(models.Job.work_mode, "unknown"), func.count(models.Job.job_id))
        .filter(catalog, active)
        .group_by(func.coalesce(models.Job.work_mode, "unknown"))
        .all()
    )
    boards = (
        db.query(models.JobSourceBoard.source, models.JobSourceBoard.verification_status, models.JobSourceBoard.is_enabled, func.count())
        .group_by(models.JobSourceBoard.source, models.JobSourceBoard.verification_status, models.JobSourceBoard.is_enabled)
        .all()
    )
    return {
        "active_jobs_by_source": by_source,
        "jobs_without_status": legacy_unknown,
        "active_jobs_by_country": by_country,
        "active_jobs_by_work_mode": by_work_mode,
        "boards": [
            {"source": source, "verification_status": status, "is_enabled": enabled, "count": count}
            for source, status, enabled, count in boards
        ],
        "companies": db.query(func.count(models.Company.company_id)).scalar(),
    }
