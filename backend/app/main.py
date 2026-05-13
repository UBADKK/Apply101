import requests
from fastapi import FastAPI, Depends
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from . import models, schemas


app = FastAPI()

Base.metadata.create_all(bind=engine)

keywords = ["junior", "developer"]


@app.get("/")
def read_root():
    return {"message": "Apply101 backend is running"}


@app.get("/jobs")
def get_jobs():
    url = "https://www.arbeitnow.com/api/job-board-api"
    response = requests.get(url)
    data = response.json()
    jobs = data["data"]

    clean_jobs = []

    for job in jobs[:5]:

        clean_job = {
            "title": job["title"],
            "company_name": job["company_name"],
            "location": job["location"],
            "url": job["url"],
            "description_text": job["description"],
        }

        clean_jobs.append(clean_job)

    return clean_jobs


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