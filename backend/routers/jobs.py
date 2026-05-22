import requests

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends
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

    for job in jobs[:10]:
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