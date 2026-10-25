"""The shared job catalog for every signed-in user, and "jobs for me".

Browsing never starts an analysis or any other paid call. Recommendations
rank active catalog jobs that already have a current analysis, with the
existing rule-based matching (matches.calculate_backend_match, the same
function and inputs as the match route) and write nothing. The response
says how much of the pool was evaluated, so a page is never presented as
the best of the whole catalog.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..app import models
from ..app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    MATCH_VERSION,
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)
from ..app.auth_dependencies import get_current_user, get_owned_profile
from ..app.catalog_sync import STATUS_ACTIVE
from ..app.database import get_db
from ..app.job_sources import SOURCES, source_info
from ..app.profile_write_guard import capture_profile_owner_identity, verify_profile_owner_current_or_404
from .matches import calculate_backend_match


router = APIRouter(tags=["Catalog"])

MAX_PAGE_SIZE = 50
# Upper bound of jobs evaluated per recommendation request; the response
# reports when it was reached.
MAX_RECOMMENDATION_POOL = 1000
WORK_MODES = ("remote", "hybrid", "onsite")
ELIGIBILITY_RANK = {"eligible": 0, "review_required": 1, "ineligible": 2}
REMOTE_NOTE = (
    "Remote does not say from which countries you may work. Check the posting "
    "for location, time zone and work authorization limits."
)


def _catalog_filters(q: str | None, country: str | None, work_mode: str | None, source: str | None) -> list:
    filters = [
        models.Job.created_by_user_id.is_(None),
        # Open as of the last catalog sync. Legacy jobs from the old
        # POST /jobs/fetch ("unknown") appear once a sync has seen them.
        models.Job.job_status == STATUS_ACTIVE,
    ]
    if q:
        pattern = f"%{q.strip()[:100]}%"
        filters.append(or_(
            models.Job.title.ilike(pattern),
            models.Job.company_name.ilike(pattern),
            models.Job.location.ilike(pattern),
        ))
    if country:
        code = country.strip().upper()
        if code == "UNKNOWN":
            filters.append(models.Job.country_code.is_(None))
        else:
            filters.append(models.Job.country_code == code)
    if work_mode:
        filters.append(models.Job.work_mode == work_mode)
    if source:
        filters.append(models.Job.source == source)
    return filters


def _validate_filters(country: str | None, work_mode: str | None, source: str | None) -> None:
    if work_mode is not None and work_mode not in WORK_MODES:
        raise HTTPException(status_code=422, detail={"error_code": "ERR_FILTER_INVALID", "message": "work_mode must be remote, hybrid or onsite."})
    if source is not None and source not in SOURCES:
        raise HTTPException(status_code=422, detail={"error_code": "ERR_FILTER_INVALID", "message": "Unknown source."})
    if country is not None:
        code = country.strip().upper()
        if code != "UNKNOWN" and (len(code) != 2 or not code.isalpha()):
            raise HTTPException(status_code=422, detail={"error_code": "ERR_FILTER_INVALID", "message": "country must be a two-letter code or 'unknown'."})


def _current_job_analysis_filters() -> list:
    return [
        models.JobAnalysis.analysis_status == "completed",
        models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
        models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
        models.JobAnalysis.is_current == True,  # noqa: E712
        models.JobAnalysis.role_tags_json.isnot(None),
        models.JobAnalysis.role_tags_json != "[]",
    ]


def _job_card(job: models.Job) -> dict:
    info = source_info(job.source or "")
    return {
        "job_id": job.job_id,
        "title": job.title,
        "company_name": job.company_name,
        "company_id": job.company_id,
        "location": job.location,
        "country_code": job.country_code,
        "work_mode": job.work_mode,
        "source": job.source,
        "source_label": info.label if info else job.source,
        "attribution": info.attribution if info else None,
        "url": job.url,
        "first_seen_at": job.created_at,
        "last_seen_at": job.last_seen_at,
        "source_created_at": job.source_created_at,
        "remote_note": REMOTE_NOTE if job.work_mode == "remote" else None,
    }


@router.get("/catalog/jobs")
def list_catalog_jobs(
    q: str | None = Query(default=None, max_length=100),
    country: str | None = Query(default=None, max_length=7),
    work_mode: str | None = Query(default=None),
    source: str | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _validate_filters(country, work_mode, source)
    filters = _catalog_filters(q, country, work_mode, source)
    total = db.query(func.count(models.Job.job_id)).filter(*filters).scalar()
    jobs = (
        db.query(models.Job)
        .filter(*filters)
        .order_by(
            func.coalesce(models.Job.last_seen_at, models.Job.created_at).desc(),
            models.Job.job_id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    analyzed_ids = set()
    if jobs:
        analyzed_ids = {
            row[0]
            for row in db.query(models.JobAnalysis.job_id)
            .filter(models.JobAnalysis.job_id.in_([job.job_id for job in jobs]), *_current_job_analysis_filters())
            .all()
        }
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [{**_job_card(job), "has_current_analysis": job.job_id in analyzed_ids} for job in jobs],
    }


@router.get("/catalog/filters")
def catalog_filter_options(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    base = _catalog_filters(None, None, None, None)
    sources = db.query(models.Job.source, func.count(models.Job.job_id)).filter(*base).group_by(models.Job.source).all()
    countries = (
        db.query(models.Job.country_code, func.count(models.Job.job_id))
        .filter(*base)
        .group_by(models.Job.country_code)
        .all()
    )
    return {
        "sources": sorted(
            [
                {"source": source, "label": source_info(source).label if source_info(source or "") else source, "count": count}
                for source, count in sources
                if source
            ],
            key=lambda item: -item["count"],
        ),
        "countries": sorted(
            [{"country_code": code or "unknown", "count": count} for code, count in countries],
            key=lambda item: -item["count"],
        ),
        "work_modes": list(WORK_MODES),
    }


@router.get("/users/{user_id}/profiles/{profile_id}/recommendations")
def recommend_jobs(
    user_id: int,
    profile_id: int,
    q: str | None = Query(default=None, max_length=100),
    country: str | None = Query(default=None, max_length=7),
    work_mode: str | None = Query(default=None),
    source: str | None = Query(default=None),
    include_ineligible: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    profile: models.CandidateProfile = Depends(get_owned_profile),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _validate_filters(country, work_mode, source)
    owner_identity = capture_profile_owner_identity(db, profile, current_user)

    profile_analysis = (
        db.query(models.ProfileAnalysis)
        .filter(
            models.ProfileAnalysis.profile_id == profile_id,
            models.ProfileAnalysis.analysis_status == "completed",
            models.ProfileAnalysis.analysis_model == PROFILE_ANALYSIS_MODEL,
            models.ProfileAnalysis.analysis_prompt_version == PROFILE_ANALYSIS_PROMPT_VERSION,
            models.ProfileAnalysis.is_current == True,  # noqa: E712
        )
        .order_by(models.ProfileAnalysis.analysis_id.desc())
        .first()
    )
    if profile_analysis is None:
        verify_profile_owner_current_or_404(db, owner_identity)
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
                "message": "Analyze your profile first; recommendations use its current analysis.",
                "profile_id": profile_id,
            },
        )

    filters = _catalog_filters(q, country, work_mode, source)
    active_count = db.query(func.count(models.Job.job_id)).filter(*filters).scalar()
    analyzed_query = (
        db.query(models.Job, models.JobAnalysis)
        .join(models.JobAnalysis, models.JobAnalysis.job_id == models.Job.job_id)
        .filter(*filters, *_current_job_analysis_filters())
    )
    analyzed_count = (
        db.query(func.count(func.distinct(models.Job.job_id)))
        .join(models.JobAnalysis, models.JobAnalysis.job_id == models.Job.job_id)
        .filter(*filters, *_current_job_analysis_filters())
        .scalar()
    )
    rows = (
        analyzed_query
        .order_by(models.Job.job_id.desc(), models.JobAnalysis.analysis_id.desc())
        .limit(MAX_RECOMMENDATION_POOL * 2)
        .all()
    )
    latest: dict[int, tuple] = {}
    for job, job_analysis in rows:
        if job.job_id not in latest and len(latest) < MAX_RECOMMENDATION_POOL:
            latest[job.job_id] = (job, job_analysis)

    evaluated = []
    status_counts = {"eligible": 0, "review_required": 0, "ineligible": 0}
    for job, job_analysis in latest.values():
        result = calculate_backend_match(
            profile=profile,
            profile_analysis=profile_analysis,
            job=job,
            job_analysis=job_analysis,
        )
        status = result["eligibility_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        evaluated.append((job, result))

    verify_profile_owner_current_or_404(db, owner_identity)

    shown = [item for item in evaluated if include_ineligible or item[1]["eligibility_status"] != "ineligible"]
    shown.sort(key=lambda item: (
        ELIGIBILITY_RANK.get(item[1]["eligibility_status"], 3),
        -(item[1]["overall_score"] or 0),
        -item[0].job_id,
    ))
    start = (page - 1) * page_size
    page_items = shown[start:start + page_size]
    return {
        "match_version": MATCH_VERSION,
        "coverage": {
            "active_jobs": active_count,
            "analyzed_jobs": analyzed_count,
            "evaluated_jobs": len(evaluated),
            "not_analyzed_jobs": max(0, active_count - analyzed_count),
            "pool_limit": MAX_RECOMMENDATION_POOL,
            "capped": analyzed_count > len(evaluated),
            "status_counts": status_counts,
        },
        "include_ineligible": include_ineligible,
        "total": len(shown),
        "page": page,
        "page_size": page_size,
        "items": [
            {
                **_job_card(job),
                "eligibility_status": result["eligibility_status"],
                "overall_score": result["overall_score"],
                "recommendation": result["recommendation"],
                "summary": result["summary"],
                "strengths": result["strengths"],
                "weaknesses": result["weaknesses"],
                "hard_fail_reasons": result["hard_fail_reasons"],
                "review_reasons": result["review_reasons"],
                "dealbreaker_warnings": result["dealbreaker_warnings"],
            }
            for job, result in page_items
        ],
    }
