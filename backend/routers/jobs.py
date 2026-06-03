import requests
import time
import json

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from openai import OpenAI
from datetime import datetime, timezone

from ..app.database import get_db
from ..app import models, schemas

client = OpenAI()

router = APIRouter(
    prefix="/jobs",
    tags=["Jobs"]
)


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


# Get jobs from DB
@router.get("/", response_model=list[schemas.JobResponse])
def get_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
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
            models.JobAnalysis.is_current == True
        )
        .order_by(models.JobAnalysis.analysis_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        {
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
            if analysis.analysis_json else None
        }
        for job, analysis in rows
    ]


@router.get("/fetch-runs")
def get_job_fetch_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
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
def fetch_jobs(db: Session = Depends(get_db)):
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
        nonlocal pages_checked, last_checked_page, stopped_reason, jobs_seen_count, duplicate_jobs_count

        response = requests.get(
            base_url,
            params={"page": page},
            headers=headers,
            timeout=20
        )

        pages_checked += 1
        last_checked_page = page

        if response.status_code == 403:
            stopped_reason = f"FORBIDDEN_ON_PAGE_{page}"
            return False

        if response.status_code == 429:
            stopped_reason = f"RATE_LIMITED_ON_PAGE_{page}"
            return False

        if response.status_code >= 400:
            stopped_reason = f"HTTP_{response.status_code}_ON_PAGE_{page}"
            return False

        data = response.json()
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
        status="completed",
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
        "jobs_seen_count": jobs_seen_count,
        "saved_count": len(saved_jobs),
        "skipped_count": len(skipped_jobs),
        "sample_saved_jobs": [
            build_saved_job_sample(job)
            for job in saved_jobs[:10]
        ],
        "sample_skipped_jobs": skipped_jobs[:10],
    }



JOB_ANALYSIS_MODEL = "gpt-4.1-mini"
JOB_ANALYSIS_PROMPT_VERSION = "job_analysis_v2"

MAX_ANALYZE_JOBS = 10
MAX_ANALYZE_MISSING_JOBS = 100

@router.post("/{job_id}/analyze")
def analyze_job(
    job_id: int,
    force_reanalyze: bool = Query(default=False),
    db: Session = Depends(get_db)
):
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
- Only include hard requirements in dealbreakers.
- Do not include preferred, advantageous, nice-to-have, or optional qualifications in dealbreakers.
- For working-student roles, if university enrollment is required, set employment_type to "working-student" and include student enrollment requirement in dealbreakers.
"""

    now = datetime.now(timezone.utc)

    try:
        response = client.responses.create(
            model=JOB_ANALYSIS_MODEL,
            input=prompt,
            temperature=0
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
                analysis.get("dealbreakers", []),
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


#This endpoint analyzes jobs with no analysis as batchs by using the analyze endpoint.

@router.post("/analyze-missing")
def analyze_missing_jobs(
    limit: int = Query(default=3, ge=1, le=MAX_ANALYZE_MISSING_JOBS),
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

    jobs = (
        db.query(models.Job)
        .filter(
            models.Job.description_text.isnot(None),
            ~models.Job.job_id.in_(completed_analysis_job_ids)
        )
        .order_by(models.Job.job_id.desc())
        .limit(limit)
        .all()
    )

    if not jobs:
        return {
            "status": "no_jobs_to_analyze",
            "requested_limit": limit,
            "max_allowed_limit": MAX_ANALYZE_MISSING_JOBS,
            "analyzed_count": 0,
            "failed_count": 0,
            "results": []
        }

    results = []

    for job in jobs:
        try:
            result = analyze_job(
                job_id=job.job_id,
                force_reanalyze=False,
                db=db
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
        "results": results
    }

# This endpoint sends selected job details to LLM for sample analysis.

@router.post("/analyze-sample")
def analyze_sample_jobs(
    limit: int = Query(default=3, ge=1),
    job_id_list: list[int] | None = Query(
        None,
        description="Analyze only the jobs with these job IDs."
    ),
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
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_NO_JOBS",
                "message": "No jobs found in database."
            }
        )

    results = []

    for job in jobs:
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

        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            temperature=0
        )

        raw_text = response.output_text.strip()

        try:
            analysis = json.loads(raw_text)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "ERR_AI_INVALID_JSON",
                    "message": "AI response was not valid JSON.",
                    "job_id": job.job_id,
                    "raw_response": raw_text
                }
            )

        results.append({
            "job_id": job.job_id,
            "title": job.title,
            "company_name": job.company_name,
            "location": job.location,
            "url": job.url,
            "analysis": analysis
        })

    return {
        "analyzed_count": len(results),
        "requested_job_ids": job_id_list,
        "missing_job_ids": missing_job_ids,
        "max_allowed_jobs": MAX_ANALYZE_JOBS,
        "results": results
    }