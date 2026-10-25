# Before anything reads the environment; APPLY101_LOAD_DOTENV=0 skips it.
from .config import check_data_locations, load_env_file
load_env_file()
check_data_locations()
from fastapi import FastAPI

from .cors import configure_cors
from .database import engine, Base

from ..routers import users
from ..routers import profiles
from ..routers import jobs
from ..routers import matches
from ..routers import auth
from ..routers import meta



app = FastAPI()
configure_cors(app)
app.include_router(users.router)
app.include_router(profiles.router)
app.include_router(jobs.router)
app.include_router(matches.router)
app.include_router(auth.router)
app.include_router(meta.router)

Base.metadata.create_all(bind=engine)

#Home
@app.get("/")
def read_root():
    return {"message": "Apply101 backend is running"}


