import logging
import requests
import time
import json

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from openai import OpenAI
from datetime import datetime, timezone

from ..app.database import get_db
from ..app import models, schemas
from ..app.analysis_guard import (
    AcquireOutcome,
    AnalysisGuardConfigError,
    RenewOutcome,
    load_job_analysis_batch_config,
    load_job_analysis_config,
    release_job_analysis_guard,
    release_job_batch_guard,
    renew_job_batch_guard,
    try_acquire_job_analysis_guard,
    try_acquire_job_batch_guard,
)
from ..app.auth_dependencies import get_current_admin, get_current_user
from ..app.taxonomy import ROLE_FAMILIES, ROLE_SUBFAMILIES, ROLE_TAGS, SKILL_TAGS
from ..app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
)
from ..app.job_analysis_sanitizer import sanitize_job_analysis_requirements


client = OpenAI()
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/jobs",
    tags=["Jobs"]
)


MAX_ANALYZE_JOBS = 10
MAX_ANALYZE_MISSING_JOBS = 100
MAX_AUTOMATIC_ANALYSIS_FAILURES_PER_JOB = 3


def is_recoverable_analysis_failure(error_message: str | None) -> bool:
    """Failures fixed by deterministic parser fallbacks may be retried automatically."""
    normalized_error = (error_message or "").lower()
    return (
        "invalid role subfamily" in normalized_error
        or "invalid role family" in normalized_error
    )


def get_automatic_retry_blocked_job_ids(db: Session) -> set[int]:
    repeated_failure_rows = (
        db.query(
            models.JobAnalysis.job_id,
            func.count(models.JobAnalysis.analysis_id).label("failure_count"),
        )
        .filter(
            models.JobAnalysis.analysis_status == "failed",
            models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
            models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
        )
        .group_by(models.JobAnalysis.job_id)
        .having(
            func.count(models.JobAnalysis.analysis_id)
            >= MAX_AUTOMATIC_ANALYSIS_FAILURES_PER_JOB
        )
        .all()
    )

    blocked_job_ids: set[int] = set()

    for job_id, _failure_count in repeated_failure_rows:
        latest_failure = (
            db.query(models.JobAnalysis)
            .filter(
                models.JobAnalysis.job_id == job_id,
                models.JobAnalysis.analysis_status == "failed",
                models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
                models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
            )
            .order_by(models.JobAnalysis.analysis_id.desc())
            .first()
        )

        if not latest_failure or not is_recoverable_analysis_failure(
            latest_failure.analysis_error
        ):
            blocked_job_ids.add(job_id)

    return blocked_job_ids


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    return soup.get_text(separator="\n", strip=True)


def build_skipped_job(reason: str, job: dict, page: int | None = None):
    skipped_job = {
        "reason": reason,
        "url": job.get("url"),
        "title": job.get("title")
    }

    if page is not None:
        skipped_job["page"] = page

    return skipped_job


def build_saved_job_sample(job: models.Job):
    return {
        "job_id": job.job_id,
        "title": job.title,
        "company_name": job.company_name,
        "location": job.location,
        "url": job.url,
        "source_created_at": job.source_created_at,
        "fetched_at": job.fetched_at,
        "last_seen_at": job.last_seen_at,
    }


def build_analyzed_job_response(
    job: models.Job,
    analysis: models.JobAnalysis,
) -> dict:
    return {
        "job_id": job.job_id,
        "title": job.title,
        "company_name": job.company_name,
        "location": job.location,
        "url": job.url,
        "source_created_at": job.source_created_at,
        "fetched_at": job.fetched_at,
        "last_seen_at": job.last_seen_at,
        "analysis_id": analysis.analysis_id,
        "analysis_status": analysis.analysis_status,
        "role_family": analysis.role_family,
        "role_subfamily": analysis.role_subfamily,
        "normalized_role_title": analysis.normalized_role_title,
        "role_tags": json.loads(analysis.role_tags_json)
        if analysis.role_tags_json else [],
        "seniority_level": analysis.seniority_level,
        "work_type": analysis.work_type,
        "employment_type": analysis.employment_type,
        "visa_sponsorship": analysis.visa_sponsorship,
        "analysis_model": analysis.analysis_model,
        "analysis_prompt_version": analysis.analysis_prompt_version,
        "analyzed_at": analysis.analyzed_at,
        "required_skills": json.loads(analysis.required_skills_json)
        if analysis.required_skills_json else [],
        "preferred_skills": json.loads(analysis.preferred_skills_json)
        if analysis.preferred_skills_json else [],
        "language_requirements": json.loads(analysis.language_requirements_json)
        if analysis.language_requirements_json else [],
        "dealbreakers": json.loads(analysis.dealbreakers_json)
        if analysis.dealbreakers_json else [],
        "analysis": json.loads(analysis.analysis_json)
        if analysis.analysis_json else None,
    }


# Get jobs from DB
@router.get("/", response_model=list[schemas.JobResponse])
def get_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    jobs = (
        db.query(models.Job)
        .order_by(models.Job.job_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return jobs


@router.get("/analyzed")
def get_analyzed_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    rows = (
        db.query(models.Job, models.JobAnalysis)
        .join(
            models.JobAnalysis,
            models.Job.job_id == models.JobAnalysis.job_id
        )
        .filter(
            models.JobAnalysis.analysis_status == "completed",
            models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
            models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
            models.JobAnalysis.is_current == True
        )
        .order_by(models.JobAnalysis.analysis_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        build_analyzed_job_response(job, analysis)
        for job, analysis in rows
    ]


@router.get("/analyzed/{job_id}")
def get_analyzed_job(
    job_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(models.Job, models.JobAnalysis)
        .join(
            models.JobAnalysis,
            models.Job.job_id == models.JobAnalysis.job_id,
        )
        .filter(
            models.Job.job_id == job_id,
            models.JobAnalysis.analysis_status == "completed",
            models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
            models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
            models.JobAnalysis.is_current == True,
        )
        .order_by(models.JobAnalysis.analysis_id.desc())
        .first()
    )

    if row is None:
        job_exists = (
            db.query(models.Job.job_id)
            .filter(models.Job.job_id == job_id)
            .first()
        )

        if job_exists is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        raise HTTPException(
            status_code=404,
            detail=(
                "No current completed analysis exists for this job "
                f"with {JOB_ANALYSIS_PROMPT_VERSION}."
            ),
        )

    job, analysis = row
    return build_analyzed_job_response(job, analysis)


@router.get("/fetch-runs")
def get_job_fetch_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    fetch_runs = (
        db.query(models.JobFetchRun)
        .order_by(models.JobFetchRun.run_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return fetch_runs

# Fetch first page of jobs from Arbeitnow
@router.post("/fetch")
def fetch_jobs(
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    url = "https://www.arbeitnow.com/api/job-board-api"

    headers = {
        "User-Agent": "Apply101/1.0"
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=20
    )
    response.raise_for_status()

    data = response.json()
    jobs = data.get("data", [])

    saved_jobs = []
    skipped_jobs = []

    now = datetime.now(timezone.utc)

    # Burada jobs[:10] vardı; sayfa başı 100 ilan döndüğü için tamamını işliyoruz.
    for job in jobs:
        job_url = job.get("url")

        if not job_url:
            skipped_jobs.append(build_skipped_job("MISSING_URL", job))
            continue

        description_html = job.get("description")

        if not description_html:
            skipped_jobs.append(build_skipped_job("MISSING_DESCRIPTION", job))
            continue

        clean_description = html_to_text(description_html)

        if not clean_description:
            skipped_jobs.append(build_skipped_job("EMPTY_DESCRIPTION_AFTER_CLEANING", job))
            continue

        existing_job = db.query(models.Job).filter(
            models.Job.url == job_url
        ).first()

        if existing_job:
            # Aynı ilan tekrar API'de görülürse hâlâ feed içinde görüldüğünü kaydediyoruz.
            existing_job.last_seen_at = now

            if existing_job.source_created_at is None:
                existing_job.source_created_at = job.get("created_at")

            skipped_jobs.append(build_skipped_job("DUPLICATE_IN_DB", job))
            continue

        new_job = models.Job(
            title=job.get("title"),
            company_name=job.get("company_name"),
            location=job.get("location"),
            url=job_url,
            description_text=clean_description,
            source="arbeitnow",
            source_job_id=None,
            source_created_at=job.get("created_at"),
            source_updated_at=None,
            fetched_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )

        db.add(new_job)
        saved_jobs.append(new_job)

    db.commit()

    for job in saved_jobs:
        db.refresh(job)

    return {
        "saved_count": len(saved_jobs),
        "skipped_count": len(skipped_jobs),
        "jobs_seen_count": len(jobs),
        "sample_saved_jobs": [
            build_saved_job_sample(job)
            for job in saved_jobs[:10]
        ],
        "sample_skipped_jobs": skipped_jobs[:10],
    }


@router.post("/fetch-pages")
def fetch_jobs_by_pages(
    alljobs: bool = Query(..., description="True ise tüm sayfaları çeker. False ise maxpage veya thispage kullanılır."),
    maxpage: int | None = Query(None, ge=1, description="İlk kaç sayfanın çekileceğini belirler."),
    thispage: int | None = Query(None, ge=1, description="Sadece belirli bir sayfayı çeker."),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    base_url = "https://www.arbeitnow.com/api/job-board-api"

    headers = {
        "User-Agent": "Apply101/1.0"
    }

    if alljobs:
        pages_to_fetch = None
    else:
        if thispage is not None:
            pages_to_fetch = [thispage]
        elif maxpage is not None:
            pages_to_fetch = range(1, maxpage + 1)
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "ERR_JOBS_FETCH_PARAMS",
                    "message": "When alljobs is false, either thispage or maxpage must be provided."
                }
            )

    saved_jobs = []
    skipped_jobs = []
    seen_urls = set()

    pages_checked = 0
    last_checked_page = None
    stopped_reason = None
    jobs_seen_count = 0
    duplicate_jobs_count = 0
    error_message = None

    started_at = datetime.now(timezone.utc)

    fetch_type = "all_pages"
    if not alljobs:
        if thispage is not None:
            fetch_type = "thispage"
        elif maxpage is not None:
            fetch_type = "maxpage"


    # MOVE TO SERVICES LATER !!!
    def process_page(page: int):
        nonlocal pages_checked, last_checked_page, stopped_reason, jobs_seen_count, duplicate_jobs_count, error_message

        try:
            response = requests.get(
                base_url,
                params={"page": page},
                headers=headers,
                timeout=20
            )
        except requests.exceptions.RequestException as e:
            pages_checked += 1
            last_checked_page = page
            stopped_reason = f"REQUEST_ERROR_ON_PAGE_{page}"
            error_message = str(e)
            return False

        pages_checked += 1
        last_checked_page = page

        if response.status_code == 403:
            stopped_reason = f"FORBIDDEN_ON_PAGE_{page}"
            error_message = "Request was forbidden by the remote server."
            return False

        if response.status_code == 429:
            stopped_reason = f"RATE_LIMITED_ON_PAGE_{page}"
            error_message = "Remote server rate limited the request."
            return False

        if response.status_code >= 400:
            stopped_reason = f"HTTP_{response.status_code}_ON_PAGE_{page}"
            error_message = f"Remote server returned HTTP {response.status_code}."
            return False

        try:
            data = response.json()
        except ValueError as e:
            stopped_reason = f"INVALID_JSON_ON_PAGE_{page}"
            error_message = str(e)
            return False
        jobs = data.get("data", [])

        if not jobs:
            stopped_reason = "EMPTY_PAGE"
            return False

        jobs_seen_count += len(jobs)

        page_saved_jobs = []
        now = datetime.now(timezone.utc)
        for job in jobs:
            job_url = job.get("url")

            if not job_url:
                skipped_jobs.append(build_skipped_job("MISSING_URL", job, page))
                continue

            if job_url in seen_urls:
                skipped_jobs.append(build_skipped_job("DUPLICATE_IN_THIS_FETCH", job, page))
                continue

            seen_urls.add(job_url)

            description_html = job.get("description")

            if not description_html:
                skipped_jobs.append(build_skipped_job("MISSING_DESCRIPTION", job, page))
                continue

            clean_description = html_to_text(description_html)

            if not clean_description:
                skipped_jobs.append(build_skipped_job("EMPTY_DESCRIPTION_AFTER_CLEANING", job, page))
                continue

            existing_job = db.query(models.Job).filter(
                models.Job.url == job_url
            ).first()

            if existing_job:
                # Aynı ilan tekrar API'de görülürse last_seen_at güncellenir.
                existing_job.last_seen_at = now

                if existing_job.source_created_at is None:
                    existing_job.source_created_at = job.get("created_at")

                duplicate_jobs_count += 1

                skipped_jobs.append(build_skipped_job("DUPLICATE_IN_DB", job, page))
                continue

            new_job = models.Job(
            title=job.get("title"),
            company_name=job.get("company_name"),
            location=job.get("location"),
            url=job_url,
            description_text=clean_description,
            source="arbeitnow",
            source_job_id=None,
            source_created_at=job.get("created_at"),
            source_updated_at=None,
            fetched_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
)

            db.add(new_job)
            saved_jobs.append(new_job)
            page_saved_jobs.append(new_job)

        # Her başarılı sayfadan sonra commit.
        # Böylece örneğin 7. sayfada 403 olursa ilk 6 sayfa DB'ye yazılmış olur.
        db.commit()

        for job in page_saved_jobs:
            db.refresh(job)

        return True

    if alljobs:
        page = 1

        while True:
            has_jobs = process_page(page)

            if not has_jobs:
                break

            page += 1

            if page > 200:
                stopped_reason = "SAFETY_LIMIT_REACHED"
                break

            time.sleep(2)
    else:
        for page in pages_to_fetch:
            has_jobs = process_page(page)

            if not has_jobs:
                break

            time.sleep(1)

    if stopped_reason is None:
        stopped_reason = "REQUESTED_PAGES_COMPLETED"

    finished_at = datetime.now(timezone.utc)

    fetch_run = models.JobFetchRun(
        source="arbeitnow",
        fetch_type=fetch_type,
        params_json=json.dumps({
            "alljobs": alljobs,
            "maxpage": maxpage,
            "thispage": thispage,
            "sleep_seconds": 2
        }),
        started_at=started_at,
        finished_at=finished_at,
        pages_checked=pages_checked,
        last_checked_page=last_checked_page,
        jobs_seen_count=jobs_seen_count,
        new_jobs_count=len(saved_jobs),
        duplicate_jobs_count=duplicate_jobs_count,
        skipped_jobs_count=len(skipped_jobs),
        stopped_reason=stopped_reason,
        status="completed" if error_message is None else "partial_failed",
        error_message=error_message
    )

    db.add(fetch_run)
    db.commit()
    db.refresh(fetch_run)

    return {
        "fetch_run_id": fetch_run.run_id,
        "alljobs": alljobs,
        "maxpage": maxpage,
        "thispage": thispage,
        "pages_checked": pages_checked,
        "last_checked_page": last_checked_page,
        "stopped_reason": stopped_reason,
        "error_message": error_message,
        "jobs_seen_count": jobs_seen_count,
        "saved_count": len(saved_jobs),
        "skipped_count": len(skipped_jobs),
        "sample_saved_jobs": [
            build_saved_job_sample(job)
            for job in saved_jobs[:10]
        ],
        "sample_skipped_jobs": skipped_jobs[:10],
    }


def _create_job_analysis_response(prompt: str, *, timeout_seconds: int, max_retries: int):
    """Single seam for the structured job-analysis OpenAI call used by
    _analyze_job_impl (and therefore both the single-analyze route and the
    analyze-missing batch route). Tests must patch this function directly
    -- patching client.responses.create does NOT work, since
    client.with_options(...) returns a distinct client/resource instance
    (same mechanism verified for profiles.py's _create_profile_analysis_response).
    """
    scoped_client = client.with_options(max_retries=max_retries)
    return scoped_client.responses.create(
        model=JOB_ANALYSIS_MODEL,
        input=prompt,
        temperature=0,
        text={
            "format": {
                "type": "json_schema",
                "name": "job_analysis",
                "schema": schemas.JobAnalysisStructured.model_json_schema(),
                "strict": True
            }
        },
        timeout=timeout_seconds,
    )


def _create_job_sample_analysis_response(prompt: str, *, timeout_seconds: int, max_retries: int):
    """Single seam for analyze-sample's separate, unstructured OpenAI
    call. analyze-sample does not call _analyze_job_impl, so it needs its
    own wrapper distinct from _create_job_analysis_response -- the two
    calls have different arguments (no structured-output schema here) and
    protect a different call site.
    """
    scoped_client = client.with_options(max_retries=max_retries)
    return scoped_client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        temperature=0,
        timeout=timeout_seconds,
    )


def _analyze_job_impl(
    *,
    job_id: int,
    force_reanalyze: bool,
    db: Session,
    job_config=None,
):
    """Business logic for analyzing a single job. No Depends(), no
    authentication -- callers (the single-analyze route and the
    analyze-missing batch route) must already have authorized the request
    before calling this. Never call this from anywhere that hasn't.

    job_config: an optional, already-loaded JobAnalysisConfig. Batch
    callers load it once and pass it down (avoiding N redundant env reads
    across a large batch); the single-job route leaves this as None so
    _analyze_job_impl loads its own, exactly once per request, as before.
    """
    job = db.query(models.Job).filter(
        models.Job.job_id == job_id
    ).first()

    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_JOB_NOT_FOUND",
                "message": f"Job with id {job_id} was not found."
            }
        )

    if not job.description_text:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_JOB_DESCRIPTION_MISSING",
                "message": "Job does not have description_text, so it cannot be analyzed.",
                "job_id": job_id
            }
        )

    existing_analysis = db.query(models.JobAnalysis).filter(
        models.JobAnalysis.job_id == job_id,
        models.JobAnalysis.analysis_status == "completed",
        models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
        models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
        models.JobAnalysis.is_current == True
    ).first()

    if existing_analysis and not force_reanalyze:
        return {
            "status": "cached",
            "message": "Job already has a current completed analysis for this model and prompt version.",
            "job_id": job.job_id,
            "analysis_id": existing_analysis.analysis_id,
            "analysis": json.loads(existing_analysis.analysis_json)
            if existing_analysis.analysis_json else None
        }

    # Configuration is validated first -- before the guard or OpenAI --
    # and its HTTPException lives outside the analysis exception handler
    # below, so invalid configuration always maps to a plain 500 and never
    # creates a failed-analysis row. Batch callers pass an already-loaded
    # job_config (validated once at the batch's own start); the single-job
    # route leaves job_config=None so one fresh copy is loaded here.
    try:
        config = job_config if job_config is not None else load_job_analysis_config()
    except AnalysisGuardConfigError:
        raise HTTPException(
            status_code=500,
            detail="Job analysis protection is not correctly configured.",
        )

    prompt = f"""
    You are a job posting parser for a job matching system.

    Extract structured matching signals from this job posting.

    Do not invent information. If something is unclear, use "unknown" for free-text or non-taxonomy fields.

    Use only the allowed canonical taxonomy values where applicable.

    For role_family:
    - Choose exactly one value from the allowed role families.
    - Use "other" if unclear or if none of the allowed values fit.

    For role_subfamily:
    - Choose exactly one value from the allowed role subfamilies.
    - Do not invent new role subfamily names.
    - Use "other" if unclear or if none of the allowed values fit.

    For role_tags:
    - Select 3 to 8 tags from the allowed role tags only.
    - Role tags should describe the actual job role, not every mentioned skill.
    - Put the closest and most important role_tags first.
    - Do not create tags outside the allowed list.
    - Use "other" only if none of the allowed tags fit.

    For required_skills and preferred_skills:
    - Use only values from the allowed skill tags.
    - required_skills should include hard required skills only.
    - preferred_skills should include nice-to-have or advantageous skills.
    - Do not create skill names outside the allowed list.
    - If no exact skill tag exists, use "other".

    For seniority_level:
    - Use only: intern, junior, mid, senior, lead, executive, unknown.

    Evidence rule for every hard requirement:
    - A hard requirement is valid only when the job description explicitly states it.
    - Store a short exact excerpt from the job title or description in the matching evidence field.
    - The evidence must be copied from the title or description, not paraphrased or invented.
    - If no exact excerpt supports the requirement, do not create that hard requirement.
    - Do not infer requirements from company country, job location, listing language, onsite work, hybrid work, or general legal assumptions.
    - A title may be used only when it explicitly states the requirement, such as "Werkstudent" or "Working Student".

    For language_requirements:
    - Return one object per explicitly hard-required language.
    - Each object must contain: language, minimum_level, required, evidence.
    - evidence must be an exact excerpt that explicitly mentions that language requirement.
    - Never infer German merely because the listing is written in German or the job is in Germany.
    - Never infer English merely because the company is international.
    - minimum_level must be one of: a1, a2, b1, b2, c1, c2, native, unknown.
    - Use an explicitly stated CEFR level exactly.
    - Map "native" or "mother tongue" to native.
    - Map "fluent", "business fluent", "professional proficiency", "fließend", or "verhandlungssicher" to c1.
    - Map "very good", "advanced", or "sehr gute" to c1.
    - Map "basic knowledge" or "Grundkenntnisse" to a2.
    - For vague wording such as "good", "secure", "language skills", "gute Kenntnisse", or "sichere Kenntnisse", use unknown rather than inventing a CEFR level.
    - Do not include optional or preferred languages with required=true.
    - When the posting requires at least one language from a list, return each mentioned language; the sanitizer will preserve the OR relationship.

    For visa_sponsorship:
    - Use only: yes, no, unknown.
    - Also return visa_sponsorship_evidence.
    - Use yes or no only when sponsorship availability is explicitly stated and supported by an exact excerpt.
    - If sponsorship is not mentioned, use unknown and null evidence.

    For hard_requirements:
    - student_enrollment_required: true when active university enrollment is explicitly mandatory.
    - Treat an explicit "Werkstudent" or "Working Student" role label as requiring active enrollment.
    - student_enrollment_evidence: exact supporting title or description excerpt, otherwise null.
    - work_authorization: use germany when an existing valid right to work in Germany is explicitly mandatory; eu_eea when EU/EEA status is explicitly mandatory; any_valid when existing local authorization is explicitly required but the exact type is unclear; none when no hard authorization requirement is stated; unknown only when authorization is discussed ambiguously.
    - work_authorization_evidence: exact supporting excerpt, otherwise null.
    - Do not infer work authorization from the fact that the job is located in Germany or requires onsite/hybrid work.
    - residency: use germany, eu_eea, specific_location, none, or unknown.
    - residency is a hard requirement only when the candidate must already reside in the stated place at application time.
    - Do not infer residency from office location, job location, commuting expectations, onsite work, hybrid work, or possible relocation.
    - residency_locations: list required residence locations only when explicit current residence is mandatory; otherwise return an empty list.
    - residency_evidence: exact supporting excerpt, otherwise null.
    - minimum_years_experience: return the minimum explicitly mandatory years as a number, or null when no numeric hard minimum is stated.
    - minimum_years_experience_evidence: exact supporting excerpt containing the number of years, otherwise null.
    - Do not convert preferred, ideal, advantageous, or "first experience" wording into a numeric hard requirement.

    For work_type:
    - Use only: remote, hybrid, onsite, unknown.

    For employment_type:
    - Use only: full_time, part_time, internship, working_student, contract, freelance, temporary, unknown.
    - For working student roles, set employment_type to "working_student".
    - Do not use "full-time", "part-time", or "working-student".

    Dealbreaker rules:
    - Only include hard requirements supported by exact evidence excerpts.
    - Do not include preferred, advantageous, nice-to-have, optional, assumed, or legally typical qualifications.
    - Keep dealbreakers consistent with the structured language_requirements and hard_requirements fields.
    - For working student roles, set student_enrollment_required=true when the title or description explicitly says "Werkstudent" or "Working Student".

    Allowed role families:
    {json.dumps(ROLE_FAMILIES, ensure_ascii=False)}

    Allowed role subfamilies:
    {json.dumps(ROLE_SUBFAMILIES, ensure_ascii=False)}

    Allowed role tags:
    {json.dumps(ROLE_TAGS, ensure_ascii=False)}

    Allowed skill tags:
    {json.dumps(SKILL_TAGS, ensure_ascii=False)}

    Job title:
    {job.title or ""}

    Company:
    {job.company_name or ""}

    Location:
    {job.location or ""}

    Job description:
    {(job.description_text or "")[:12000]}
    """

    guard_acquire_result = try_acquire_job_analysis_guard(db, job_id=job.job_id, config=config)

    if guard_acquire_result.outcome is AcquireOutcome.ALREADY_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="An analysis for this job is already in progress.",
        )

    if guard_acquire_result.outcome is AcquireOutcome.COOLDOWN_ACTIVE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(guard_acquire_result.retry_after_seconds)},
        )

    if guard_acquire_result.outcome is AcquireOutcome.BACKEND_UNAVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please try again shortly.",
        )

    guard_owner_token = guard_acquire_result.owner_token
    # Set True only immediately after the paid analysis result is actually
    # committed. The single release point in `finally` below uses this
    # flag -- not whether the function ultimately returns or raises -- so
    # a later refresh/response failure after a real commit still applies
    # the success cooldown for the already-persisted, already-paid-for
    # result.
    job_analysis_committed = False

    now = datetime.now(timezone.utc)

    try:
        response = _create_job_analysis_response(
            prompt,
            timeout_seconds=config.openai_timeout_seconds,
            max_retries=config.openai_max_retries,
        )

        raw_text = response.output_text.strip()

        try:
            analysis = json.loads(raw_text)
        except json.JSONDecodeError:
            failed_analysis = models.JobAnalysis(
                job_id=job.job_id,
                analysis_status="failed",
                analysis_json=None,
                analysis_model=JOB_ANALYSIS_MODEL,
                analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
                analyzed_at=now,
                analysis_error=f"AI response was not valid JSON: {raw_text[:1000]}",
                is_current=False,
                created_at=now
            )

            db.add(failed_analysis)
            db.commit()

            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "ERR_AI_INVALID_JSON",
                    "message": "AI response was not valid JSON.",
                    "job_id": job.job_id,
                    "raw_response": raw_text
                }
            )

        validated_analysis = schemas.JobAnalysisStructured.model_validate(analysis)
        analysis = validated_analysis.model_dump()
        analysis_source_text = "\n".join([
            job.title or "",
            job.description_text or "",
        ])
        analysis = sanitize_job_analysis_requirements(
            analysis,
            analysis_source_text,
        )

        db.query(models.JobAnalysis).filter(
            models.JobAnalysis.job_id == job.job_id,
            models.JobAnalysis.is_current == True
        ).update({
            "is_current": False
        })

        new_analysis = models.JobAnalysis(
            job_id=job.job_id,
            analysis_status="completed",
            analysis_json=json.dumps(analysis, ensure_ascii=False),

            role_family=analysis.get("role_family"),
            role_subfamily=analysis.get("role_subfamily"),
            normalized_role_title=analysis.get("normalized_role_title"),
            role_tags_json=json.dumps(
                analysis.get("role_tags", []),
                ensure_ascii=False
            ),

            seniority_level=analysis.get("seniority_level"),
            work_type=analysis.get("work_type"),
            employment_type=analysis.get("employment_type"),
            visa_sponsorship=analysis.get("visa_sponsorship"),

            required_skills_json=json.dumps(
                analysis.get("required_skills", []),
                ensure_ascii=False
            ),
            preferred_skills_json=json.dumps(
                analysis.get("preferred_skills", []),
                ensure_ascii=False
            ),
            language_requirements_json=json.dumps(
                analysis.get("language_requirements", []),
                ensure_ascii=False
            ),
            dealbreakers_json=json.dumps(
                {
                    "hard_requirements": analysis.get("hard_requirements", {}),
                    "notes": analysis.get("dealbreakers", []),
                },
                ensure_ascii=False
            ),

            analysis_model=JOB_ANALYSIS_MODEL,
            analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
            analyzed_at=now,
            analysis_error=None,
            is_current=True,
            created_at=now
        )

        db.add(new_analysis)
        db.commit()
        job_analysis_committed = True
        db.refresh(new_analysis)

        return {
            "status": "created",
            "job_id": job.job_id,
            "analysis_id": new_analysis.analysis_id,
            "analysis_model": new_analysis.analysis_model,
            "analysis_prompt_version": new_analysis.analysis_prompt_version,
            "analysis": analysis
        }

    except HTTPException:
        raise

    except Exception as e:
        failed_analysis = models.JobAnalysis(
            job_id=job.job_id,
            analysis_status="failed",
            analysis_json=None,
            analysis_model=JOB_ANALYSIS_MODEL,
            analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
            analyzed_at=now,
            analysis_error=str(e),
            is_current=False,
            created_at=now
        )

        db.add(failed_analysis)
        db.commit()

        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ERR_JOB_ANALYSIS_FAILED",
                "message": "Job analysis failed.",
                "job_id": job.job_id,
                "error": str(e)
            }
        )

    finally:
        # Single guaranteed release point, covering every post-acquisition
        # path: the success return above, the handled-failure raises
        # above (including the inline ERR_AI_INVALID_JSON HTTPException),
        # and any exception raised by the failed-analysis bookkeeping
        # itself -- finally always runs even when the except block above
        # raises. succeeded reflects job_analysis_committed, which is only
        # ever set True immediately after the paid analysis result is
        # actually committed.
        try:
            release_job_analysis_guard(
                db,
                job_id=job.job_id,
                owner_token=guard_owner_token,
                succeeded=job_analysis_committed,
                config=config,
            )
        except Exception as release_exc:
            # Defensive only -- release_job_analysis_guard is designed to
            # never raise. Never let a release-time failure replace an
            # already-established return value or exception, and never
            # log the owner token, prompt, job description, or the raw
            # exception (its message could echo bound SQL parameters).
            logger.warning(
                "_analyze_job_impl: guard release attempt raised unexpectedly (%s)",
                type(release_exc).__name__,
            )


@router.post("/{job_id}/analyze")
def analyze_job(
    job_id: int,
    force_reanalyze: bool = Query(default=False),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    return _analyze_job_impl(
        job_id=job_id,
        force_reanalyze=force_reanalyze,
        db=db,
    )


#This endpoint analyzes jobs with no analysis as batchs by using the analyze endpoint.
@router.post("/analyze-missing")
def analyze_missing_jobs(
    limit: int = Query(default=3, ge=1, le=MAX_ANALYZE_MISSING_JOBS),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    completed_analysis_job_ids = (
        db.query(models.JobAnalysis.job_id)
        .filter(
            models.JobAnalysis.analysis_status == "completed",
            models.JobAnalysis.analysis_model == JOB_ANALYSIS_MODEL,
            models.JobAnalysis.analysis_prompt_version == JOB_ANALYSIS_PROMPT_VERSION,
            models.JobAnalysis.is_current == True
        )
    )

    automatic_retry_blocked_job_ids = get_automatic_retry_blocked_job_ids(db)

    job_filters = [
        models.Job.description_text.isnot(None),
        ~models.Job.job_id.in_(completed_analysis_job_ids),
    ]

    if automatic_retry_blocked_job_ids:
        job_filters.append(
            ~models.Job.job_id.in_(automatic_retry_blocked_job_ids)
        )

    jobs = (
        db.query(models.Job)
        .filter(*job_filters)
        .order_by(models.Job.job_id.desc())
        .limit(limit)
        .all()
    )

    if not jobs:
        # No-op: zero OpenAI cost is at risk, so config/batch-guard/per-job-guard
        # are never touched here.
        return {
            "status": "no_jobs_to_analyze",
            "requested_limit": limit,
            "max_allowed_limit": MAX_ANALYZE_MISSING_JOBS,
            "analyzed_count": 0,
            "failed_count": 0,
            "automatic_retry_blocked_count": len(automatic_retry_blocked_job_ids),
            "results": []
        }

    # Configuration is validated before any guard/OpenAI work, entirely
    # outside the batch try/finally below, so invalid configuration always
    # maps to a plain 500 and never touches guard state.
    try:
        per_job_config = load_job_analysis_config()
        batch_config = load_job_analysis_batch_config(
            job_guard_lease_seconds=per_job_config.lease_ttl_seconds
        )
    except AnalysisGuardConfigError:
        raise HTTPException(
            status_code=500,
            detail="Job analysis protection is not correctly configured.",
        )

    batch_guard_result = try_acquire_job_batch_guard(db, config=batch_config)

    if batch_guard_result.outcome is AcquireOutcome.ALREADY_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="A job-analysis batch is already in progress.",
        )

    if batch_guard_result.outcome is AcquireOutcome.COOLDOWN_ACTIVE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(batch_guard_result.retry_after_seconds)},
        )

    if batch_guard_result.outcome is AcquireOutcome.BACKEND_UNAVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please try again shortly.",
        )

    batch_owner_token = batch_guard_result.owner_token
    # True only if the loop below reaches its natural end (no coordination
    # loss) -- individual per-job failures do NOT make this False, since
    # they are independently guarded/cooled down at the per-job level and
    # the batch itself still completed its run.
    batch_completed = False
    results = []
    lock_lost_response = None

    try:
        for job in jobs:
            # Renewed before EVERY job (not every few), so the batch lease
            # only ever needs to cover one job's worst case at a time --
            # see load_job_analysis_batch_config's docstring.
            renew_result = renew_job_batch_guard(db, owner_token=batch_owner_token, config=batch_config)

            if renew_result.outcome is RenewOutcome.OWNERSHIP_LOST:
                # Stop issuing further OpenAI calls immediately. Jobs
                # already processed above remain fully committed --
                # documented, accepted partial-side-effect behavior.
                lock_lost_response = HTTPException(
                    status_code=409,
                    detail="Job-analysis batch coordination was lost. Please retry.",
                )
                break

            if renew_result.outcome is RenewOutcome.BACKEND_UNAVAILABLE:
                lock_lost_response = HTTPException(
                    status_code=503,
                    detail="Service temporarily unavailable. Please try again shortly.",
                )
                break

            try:
                result = _analyze_job_impl(
                    job_id=job.job_id,
                    force_reanalyze=False,
                    db=db,
                    job_config=per_job_config,
                )

                results.append({
                    "job_id": job.job_id,
                    "title": job.title,
                    "company_name": job.company_name,
                    "status": result.get("status"),
                    "analysis_id": result.get("analysis_id"),
                    "analysis_prompt_version": result.get("analysis_prompt_version")
                })

            except HTTPException as e:
                # A per-job 409/429/503/500 (including a per-job guard
                # conflict) is represented as that job's own failed
                # result -- the batch continues to the next job.
                db.rollback()

                results.append({
                    "job_id": job.job_id,
                    "title": job.title,
                    "company_name": job.company_name,
                    "status": "failed",
                    "error": e.detail
                })

            except Exception as e:
                db.rollback()

                results.append({
                    "job_id": job.job_id,
                    "title": job.title,
                    "company_name": job.company_name,
                    "status": "failed",
                    "error": str(e)
                })
        else:
            # Reached only if the loop completed without `break` --
            # i.e. batch coordination was never lost.
            batch_completed = True

        if not batch_completed:
            raise lock_lost_response

        analyzed_count = len([
            result for result in results
            if result.get("status") in ["created", "cached"]
        ])

        failed_count = len([
            result for result in results
            if result.get("status") == "failed"
        ])

        return {
            "status": "completed",
            "requested_limit": limit,
            "max_allowed_limit": MAX_ANALYZE_MISSING_JOBS,
            "selected_job_count": len(jobs),
            "analyzed_count": analyzed_count,
            "failed_count": failed_count,
            "automatic_retry_blocked_count": len(automatic_retry_blocked_job_ids),
            "results": results
        }
    finally:
        # Single guaranteed release point for the batch lock, covering the
        # normal-completion return above and the lock-lost raise above.
        # succeeded reflects batch_completed, i.e. whether the batch
        # actually reached the end of its own selected-job loop -- release
        # is owner-token-gated, so if ownership was already lost this is a
        # safe no-op and applies no cooldown on this caller's behalf.
        try:
            release_job_batch_guard(
                db,
                owner_token=batch_owner_token,
                succeeded=batch_completed,
                config=batch_config,
            )
        except Exception as release_exc:
            logger.warning(
                "analyze_missing_jobs: batch guard release attempt raised unexpectedly (%s)",
                type(release_exc).__name__,
            )


# This endpoint sends selected job details to LLM for sample analysis.
@router.post("/analyze-sample")
def analyze_sample_jobs(
    limit: int = Query(default=3, ge=1),
    job_id_list: list[int] | None = Query(
        None,
        description="Analyze only the jobs with these job IDs."
    ),
    current_admin: models.User = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if limit > MAX_ANALYZE_JOBS:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_LIMIT_TOO_HIGH",
                "message": f"limit cannot be greater than {MAX_ANALYZE_JOBS}."
            }
        )

    if job_id_list and len(job_id_list) > MAX_ANALYZE_JOBS:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_TOO_MANY_JOB_IDS",
                "message": f"You can analyze at most {MAX_ANALYZE_JOBS} jobs at a time."
            }
        )

    if job_id_list:
        jobs = db.query(models.Job).filter(
            models.Job.job_id.in_(job_id_list)
        ).all()

        found_job_ids = {job.job_id for job in jobs}
        missing_job_ids = [
            job_id for job_id in job_id_list
            if job_id not in found_job_ids
        ]

        if not jobs:
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "ERR_NO_JOBS_FOUND",
                    "message": "None of the provided job IDs were found.",
                    "missing_job_ids": missing_job_ids
                }
            )
    else:
        jobs = db.query(models.Job).limit(limit).all()
        missing_job_ids = []

    if not jobs:
        # No-op: zero OpenAI cost is at risk, so config/batch-guard/per-job-guard
        # are never touched here.
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_NO_JOBS",
                "message": "No jobs found in database."
            }
        )

    # Configuration is validated before any guard/OpenAI work, entirely
    # outside the batch try/finally below.
    try:
        per_job_config = load_job_analysis_config()
        batch_config = load_job_analysis_batch_config(
            job_guard_lease_seconds=per_job_config.lease_ttl_seconds
        )
    except AnalysisGuardConfigError:
        raise HTTPException(
            status_code=500,
            detail="Job analysis protection is not correctly configured.",
        )

    # Shares the SAME global batch singleton as analyze-missing.
    batch_guard_result = try_acquire_job_batch_guard(db, config=batch_config)

    if batch_guard_result.outcome is AcquireOutcome.ALREADY_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="A job-analysis batch is already in progress.",
        )

    if batch_guard_result.outcome is AcquireOutcome.COOLDOWN_ACTIVE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(batch_guard_result.retry_after_seconds)},
        )

    if batch_guard_result.outcome is AcquireOutcome.BACKEND_UNAVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please try again shortly.",
        )

    batch_owner_token = batch_guard_result.owner_token
    # True only if the loop below reaches its natural end for every
    # selected job -- any failure (coordination loss, a per-job guard
    # conflict, or an OpenAI/parsing failure) aborts the whole request via
    # a raised HTTPException, which propagates through this flag still
    # False, matching analyze-sample's existing all-or-nothing semantics.
    batch_completed = False
    results = []

    try:
        for job in jobs:
            # Renewed before EVERY job's protected operation.
            renew_result = renew_job_batch_guard(db, owner_token=batch_owner_token, config=batch_config)

            if renew_result.outcome is RenewOutcome.OWNERSHIP_LOST:
                raise HTTPException(
                    status_code=409,
                    detail="Job-analysis batch coordination was lost. Please retry.",
                )

            if renew_result.outcome is RenewOutcome.BACKEND_UNAVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Service temporarily unavailable. Please try again shortly.",
                )

            prompt = f"""
You are a job posting parser for a job matching system.

Extract the key matching signals from this job posting.
Do not invent information. If something is unclear, return "unknown".

Job title:
{job.title or ""}

Company:
{job.company_name or ""}

Location:
{job.location or ""}

Job description:
{(job.description_text or "")[:12000]}

Return ONLY valid JSON in this exact structure:
{{
  "summary": "short but information-dense summary",
  "role_family": "engineering|data|product|design|marketing|sales|customer_success|operations|business_analysis|project_management|finance|accounting|hr|legal|consulting|strategy|it|cybersecurity|qa_testing|devops|research|education|healthcare|logistics|supply_chain|manufacturing|administration|support|content|media|other|unknown",
  "role_subfamily": "short snake_case normalized subcategory, e.g. backend_engineering, data_analysis, product_management, account_executive",
  "normalized_role_title": "specific normalized role title, e.g. backend software engineer, business intelligence analyst, customer success manager",
  "required_skills": ["skill 1", "skill 2"],
  "preferred_skills": ["skill 1", "skill 2"],
  "responsibilities": ["responsibility 1", "responsibility 2"],
  "seniority_level": "intern|junior|mid|senior|lead|executive|unknown",
  "language_requirements": ["English", "German", "unknown"],
  "visa_sponsorship": "yes|no|unknown",
  "work_type": "remote|hybrid|onsite|unknown",
  "employment_type": "full-time|part-time|internship|working-student|contract|freelance|temporary|unknown",
  "dealbreakers": ["dealbreaker 1", "dealbreaker 2"]
}}

Rules:
- Return only valid JSON.
- Do not include markdown.
- Do not infer requirements that are not stated.
- role_family must be selected from the allowed list.
- role_subfamily should be a short snake_case normalized subcategory.
- normalized_role_title should be a concise human-readable normalized title.
- Do not force a job into an inaccurate category. Use "other" or "unknown" when unclear.
- If German is required, include it in language_requirements and dealbreakers if relevant.
- If EU work authorization is required, include it in dealbreakers.
"""

            job_guard_result = try_acquire_job_analysis_guard(db, job_id=job.job_id, config=per_job_config)

            if job_guard_result.outcome is AcquireOutcome.ALREADY_IN_PROGRESS:
                # Aborts the complete sample request -- matches analyze-sample's
                # existing all-or-nothing per-job semantics.
                raise HTTPException(
                    status_code=409,
                    detail="An analysis for this job is already in progress.",
                )

            if job_guard_result.outcome is AcquireOutcome.COOLDOWN_ACTIVE:
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please try again later.",
                    headers={"Retry-After": str(job_guard_result.retry_after_seconds)},
                )

            if job_guard_result.outcome is AcquireOutcome.BACKEND_UNAVAILABLE:
                raise HTTPException(
                    status_code=503,
                    detail="Service temporarily unavailable. Please try again shortly.",
                )

            job_owner_token = job_guard_result.owner_token
            # Named for operation success, not "committed" -- analyze-sample
            # never writes a JobAnalysis row, so nothing is ever committed here.
            sample_call_succeeded = False

            try:
                response = _create_job_sample_analysis_response(
                    prompt,
                    timeout_seconds=per_job_config.openai_timeout_seconds,
                    max_retries=per_job_config.openai_max_retries,
                )

                raw_text = response.output_text.strip()

                try:
                    analysis = json.loads(raw_text)
                except json.JSONDecodeError:
                    # Existing dedicated shape, unchanged, including raw_response.
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error_code": "ERR_AI_INVALID_JSON",
                            "message": "AI response was not valid JSON.",
                            "job_id": job.job_id,
                            "raw_response": raw_text
                        }
                    )

                sample_call_succeeded = True
                results.append({
                    "job_id": job.job_id,
                    "title": job.title,
                    "company_name": job.company_name,
                    "location": job.location,
                    "url": job.url,
                    "analysis": analysis
                })

            except HTTPException:
                raise

            except Exception:
                # Was previously unhandled/raw-propagated. Now a generic,
                # structured failure reusing _analyze_job_impl's error_code/
                # message, but WITHOUT raw exception text and without
                # persisting anything (analyze-sample stays non-persistent).
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error_code": "ERR_JOB_ANALYSIS_FAILED",
                        "message": "Job analysis failed.",
                        "job_id": job.job_id,
                    }
                )

            finally:
                try:
                    release_job_analysis_guard(
                        db,
                        job_id=job.job_id,
                        owner_token=job_owner_token,
                        succeeded=sample_call_succeeded,
                        config=per_job_config,
                    )
                except Exception as release_exc:
                    logger.warning(
                        "analyze_sample_jobs: per-job guard release attempt raised unexpectedly (%s)",
                        type(release_exc).__name__,
                    )

        batch_completed = True

        return {
            "analyzed_count": len(results),
            "requested_job_ids": job_id_list,
            "missing_job_ids": missing_job_ids,
            "max_allowed_jobs": MAX_ANALYZE_JOBS,
            "results": results
        }
    finally:
        try:
            release_job_batch_guard(
                db,
                owner_token=batch_owner_token,
                succeeded=batch_completed,
                config=batch_config,
            )
        except Exception as release_exc:
            logger.warning(
                "analyze_sample_jobs: batch guard release attempt raised unexpectedly (%s)",
                type(release_exc).__name__,
            )