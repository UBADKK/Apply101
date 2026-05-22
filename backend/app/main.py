import requests

from fastapi import FastAPI, Depends, File

from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from . import models, schemas


from ..routers import users
from ..routers import profiles
from ..routers import jobs

app = FastAPI()
app.include_router(users.router)
app.include_router(profiles.router)
app.include_router(jobs.router)

Base.metadata.create_all(bind=engine)



#Home
@app.get("/")
def read_root():
    return {"message": "Apply101 backend is running"}


