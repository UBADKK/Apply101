from sqlalchemy import Column, Integer, String, Float, Boolean
from .database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False)

    mail = Column(String, unique=True, index=True, nullable=False)

    skills = Column(String, nullable=True)
    # Örnek: "Python, FastAPI, PostgreSQL, Django"

    experience_years = Column(Float, nullable=True)
    # Örnek: 0.5, 1, 2.5

    major = Column(String, nullable=True)
    # Örnek: "Computer Science"

    master = Column(Boolean, default=False)
    phd = Column(Boolean, default=False)
    abitur = Column(Boolean, default=False)