from fastapi import FastAPI

from .database import engine, Base

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


