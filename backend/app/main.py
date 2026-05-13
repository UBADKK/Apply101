import requests
from fastapi import FastAPI, Depends
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from . import models, schemas


app = FastAPI()

Base.metadata.create_all(bind=engine)

keywords = ["junior", "developer"]

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n", strip=True)


@app.get("/")
def read_root():
    return {"message": "Apply101 backend is running"}

@app.post("/users", response_model=schemas.UserResponse)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    new_user = models.User(
        name=user.name,
        mail=user.mail,
        skills=user.skills,
        experience_years=user.experience_years,
        major=user.major,
        master=user.master,
        phd=user.phd,
        abitur=user.abitur,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


@app.get("/users", response_model=list[schemas.UserResponse])
def get_users(db: Session = Depends(get_db)):
    users = db.query(models.User).all()
    return users


@app.get("/jobs", response_model=list[schemas.JobResponse])
def get_jobs(db: Session = Depends(get_db)):
    jobs = db.query(models.Job).all()
    return jobs


@app.post("/jobs/fetch")
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