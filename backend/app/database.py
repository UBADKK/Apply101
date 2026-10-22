from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import database_url

# APPLY101_DATABASE_URL, default sqlite:///./apply101.db (see config.py).
DATABASE_URL = database_url()
CONNECT_ARGS = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=CONNECT_ARGS
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()