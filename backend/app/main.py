import os
import shutil
import requests

from fastapi import FastAPI, Depends, UploadFile, File
from pypdf import PdfReader
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from . import models, schemas


from ..routers import users

app = FastAPI()
app.include_router(users.router)

Base.metadata.create_all(bind=engine)

keywords = ["junior", "developer"]

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n", strip=True)


def extract_text_from_pdf(file_path: str) -> str:
    reader = PdfReader(file_path)

    text = ""

    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"

    return text.strip()


#Home
@app.get("/")
def read_root():
    return {"message": "Apply101 backend is running"}


#Add new profile to a user
@app.post("/users/{user_id}/profile", response_model=schemas.CandidateProfileResponse)
def create_candidate_profile(
    user_id: int,
    profile: schemas.CandidateProfileCreate,
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.user_id == user_id).first()

    if not user:
        return {"error": "User not found"}

    new_profile = models.CandidateProfile(
        user_id=user_id,
        self_description=profile.self_description,
        target_role=profile.target_role,
        secondary_target_role=profile.secondary_target_role,
        target_location=profile.target_location,
        preferred_work_type=profile.preferred_work_type,
        preferred_technologies=profile.preferred_technologies,
        extra_preferences=profile.extra_preferences,
    )

    db.add(new_profile)
    db.commit()
    db.refresh(new_profile)

    return new_profile


#Get user's profiles from DB
@app.get("/users/{user_id}/profiles", response_model=list[schemas.CandidateProfileResponse])
def get_user_profiles(user_id: int, db: Session = Depends(get_db)):
    profiles = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.user_id == user_id
    ).all()

    return profiles


#Get jobs from DB
@app.get("/jobs", response_model=list[schemas.JobResponse])
def get_jobs(db: Session = Depends(get_db)):
    jobs = db.query(models.Job).all()
    return jobs


#Fetch job from arbeitnow
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


#PDF Upload
@app.post("/users/{user_id}/profiles/{profile_id}/upload-cv", response_model=schemas.CandidateProfileResponse)
def upload_cv(user_id: int,profile_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith(".pdf"):
        return {"error": "Only PDF files are supported for now"}

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id
    ).first()

    if not profile:
        return {"error": "Profile not found"}

    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)

    file_path = os.path.join(upload_dir, file.filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    cv_text = extract_text_from_pdf(file_path)

    profile.cv_filename = file.filename
    profile.cv_file_path = file_path
    profile.cv_text = cv_text

    db.commit()
    db.refresh(profile)

    return profile