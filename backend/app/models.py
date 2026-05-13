from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False)

    mail = Column(String, unique=True, index=True, nullable=False)

    skills = Column(String, nullable=True)

    experience_years = Column(Float, nullable=True)

    # Example: "Computer Science"
    major = Column(String, nullable=True)
    
    # Boolean for now. CHANGE LATER!!!!!!
    master = Column(Boolean, default=False)
    phd = Column(Boolean, default=False)
    abitur = Column(Boolean, default=False)


    # Link to CandidateProfile
    candidate_profiles = relationship("CandidateProfile", back_populates="user")

class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    profile_id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)

    cv_filename = Column(String, nullable=True)
    cv_file_path = Column(String, nullable=True)
    cv_text = Column(Text, nullable=True)

    self_description = Column(Text, nullable=True)

    target_role = Column(String, nullable=True)
    secondary_target_role = Column(String, nullable=True)

    target_location = Column(String, nullable=True)
    preferred_work_type = Column(String, nullable=True)
    # örnek: "remote", "hybrid", "onsite", "any"

    preferred_technologies = Column(Text, nullable=True)
    # örnek: "Python, FastAPI, Django, PostgreSQL"

    extra_preferences = Column(Text, nullable=True)
    # örnek: "English-speaking jobs, visa sponsorship, junior roles"

    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="candidate_profiles")

class Job(Base):
    __tablename__ = "jobs"

    job_id = Column(Integer, primary_key=True, index=True)

    title = Column(String, nullable=False)
    company_name = Column(String, nullable=True)
    location = Column(String, nullable=True)

    url = Column(String, unique=True, index=True, nullable=False)

    description_text = Column(String, nullable=True)