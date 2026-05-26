import requests
import time

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..app.database import get_db
from ..app import models, schemas


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