import requests
import time
import json

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from openai import OpenAI

from ..app.database import get_db
from ..app import models, schemas

client = OpenAI()

router = APIRouter(
    prefix="/jobs",
    tags=["Jobs"]
)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n", strip=True)


# Get jobs from DB
@router.get("/", response_model=list[schemas.JobResponse])
def get_jobs(db: Session = Depends(get_db)):
    jobs = db.query(models.Job).all()
    return jobs


# Fetch jobs from Arbeitnow
@router.post("/fetch")
def fetch_jobs(db: Session = Depends(get_db)):
    url = "https://www.arbeitnow.com/api/job-board-api"
    response = requests.get(url)
    data = response.json()
    jobs = data["data"]

    saved_jobs = []
    skipped_jobs = []

#burada jobs[:10] vardi, sayfa basi 100 ilan donuyodu kayip olmasin diye kaldirdim.
    for job in jobs:
        clean_description = html_to_text(job["description"])

        existing_job = db.query(models.Job).filter(
            models.Job.url == job["url"]
        ).first()

        if existing_job:
            skipped_jobs.append(job["url"])
            continue

        new_job = models.Job(
            title=job["title"],
            company_name=job["company_name"],
            location=job["location"],
            url=job["url"],
            description_text=clean_description,
        )

        db.add(new_job)
        saved_jobs.append(new_job)

    db.commit()

    for job in saved_jobs:
        db.refresh(job)

    return {
        "saved_count": len(saved_jobs),
        "skipped_count": len(skipped_jobs),
        "saved_jobs": saved_jobs,
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

    def process_page(page: int):
        nonlocal pages_checked, last_checked_page, stopped_reason

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

        page_saved_jobs = []

        for job in jobs:
            job_url = job.get("url")

            if not job_url:
                skipped_jobs.append({
                    "reason": "MISSING_URL",
                    "title": job.get("title")
                })
                continue

            if job_url in seen_urls:
                skipped_jobs.append({
                    "reason": "DUPLICATE_IN_THIS_FETCH",
                    "url": job_url,
                    "title": job.get("title")
                })
                continue

            seen_urls.add(job_url)

            existing_job = db.query(models.Job).filter(
                models.Job.url == job_url
            ).first()

            if existing_job:
                skipped_jobs.append({
                    "reason": "DUPLICATE_IN_DB",
                    "url": job_url,
                    "title": job.get("title")
                })
                continue

            clean_description = html_to_text(job.get("description", ""))

            new_job = models.Job(
                title=job.get("title"),
                company_name=job.get("company_name"),
                location=job.get("location"),
                url=job_url,
                description_text=clean_description,
            )

            db.add(new_job)
            saved_jobs.append(new_job)
            page_saved_jobs.append(new_job)

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

            time.sleep(1)
    else:
        for page in pages_to_fetch:
            has_jobs = process_page(page)

            if not has_jobs:
                break

            time.sleep(1)

    return {
        "alljobs": alljobs,
        "maxpage": maxpage,
        "thispage": thispage,
        "pages_checked": pages_checked,
        "last_checked_page": last_checked_page,
        "stopped_reason": stopped_reason,
        "saved_count": len(saved_jobs),
        "skipped_count": len(skipped_jobs),
        "sample_saved_jobs": [
            {
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "location": job.location,
                "url": job.url
            }
            for job in saved_jobs[:10]
        ],
        "sample_skipped_jobs": skipped_jobs[:10],
    }

#THIS ENDPOINTS SENDS JOB DETAIL OF FIRST 3 JOBS TO LLM
MAX_ANALYZE_JOBS = 10


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
  "required_skills": ["skill 1", "skill 2"],
  "preferred_skills": ["skill 1", "skill 2"],
  "responsibilities": ["responsibility 1", "responsibility 2"],
  "seniority_level": "intern|junior|mid|senior|lead|unknown",
  "language_requirements": ["English", "German", "unknown"],
  "visa_sponsorship": "yes|no|unknown",
  "work_type": "remote|hybrid|onsite|unknown",
  "employment_type": "full-time|part-time|internship|working-student|contract|unknown",
  "dealbreakers": ["dealbreaker 1", "dealbreaker 2"]
}}

Rules:
- Return only valid JSON.
- Do not include markdown.
- Do not infer requirements that are not stated.
- If German is required, include it in language_requirements and dealbreakers if relevant.
- If EU work authorization is required, include it in dealbreakers.
"""

        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt
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