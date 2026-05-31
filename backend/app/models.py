from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, Text, DateTime
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

    profile_name = Column(String, nullable=True)

    target_locations_json = Column(Text, nullable=True)
    preferred_roles_json = Column(Text, nullable=True)
    excluded_roles_json = Column(Text, nullable=True)

    visa_sponsorship_needed = Column(Boolean, nullable=True)
    work_authorization_status = Column(String, nullable=True)
    relocation_preference = Column(String, nullable=True)

    years_of_experience = Column(Float, nullable=True)
    seniority_target = Column(String, nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)

class ProfileAnalysis(Base):
    __tablename__ = "profile_analysis"

    analysis_id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(Integer, ForeignKey("candidate_profiles.profile_id"), nullable=False)

    analysis_status = Column(String, default="pending", nullable=True)
    analysis_json = Column(Text, nullable=True)

    candidate_summary = Column(Text, nullable=True)

    current_role_family = Column(String, nullable=True)
    seniority_level = Column(String, nullable=True)
    education_level = Column(String, nullable=True)
    field_of_study = Column(String, nullable=True)

    strong_skills_json = Column(Text, nullable=True)
    moderate_skills_json = Column(Text, nullable=True)
    weak_or_basic_skills_json = Column(Text, nullable=True)
    tools_json = Column(Text, nullable=True)
    languages_json = Column(Text, nullable=True)
    target_roles_json = Column(Text, nullable=True)
    target_role_families_json = Column(Text, nullable=True)
    excluded_roles_json = Column(Text, nullable=True)

    visa_sponsorship_needed = Column(Boolean, nullable=True)
    work_authorization_status = Column(String, nullable=True)
    relocation_preference = Column(String, nullable=True)

    analysis_model = Column(String, nullable=True)
    analysis_prompt_version = Column(String, nullable=True)
    analyzed_at = Column(DateTime, nullable=True)
    analysis_error = Column(Text, nullable=True)

    is_current = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=True)

class Job(Base):
    __tablename__ = "jobs"

    job_id = Column(Integer, primary_key=True, index=True)

    title = Column(String, nullable=False)
    company_name = Column(String, nullable=True)
    location = Column(String, nullable=True)

    url = Column(String, unique=True, index=True, nullable=False)

    description_text = Column(String, nullable=True)

    source_created_at = Column(Integer, nullable=True)
    fetched_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    job_status = Column(String, default="unknown", nullable=True)
    last_status_checked_at = Column(DateTime, nullable=True)
    last_status_code = Column(Integer, nullable=True)
    status_check_error = Column(String, nullable=True)

    source = Column(String, nullable=True)
    source_job_id = Column(String, nullable=True)
    source_updated_at = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)

class JobAnalysis(Base):
    __tablename__ = "job_analysis"

    analysis_id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.job_id"), nullable=False)

    analysis_status = Column(String, default="pending", nullable=True)
    analysis_json = Column(Text, nullable=True)

    role_family = Column(String, nullable=True)
    role_subfamily = Column(String, nullable=True)
    normalized_role_title = Column(String, nullable=True)

    seniority_level = Column(String, nullable=True)
    work_type = Column(String, nullable=True)
    employment_type = Column(String, nullable=True)
    visa_sponsorship = Column(String, nullable=True)

    required_skills_json = Column(Text, nullable=True)
    preferred_skills_json = Column(Text, nullable=True)
    language_requirements_json = Column(Text, nullable=True)
    dealbreakers_json = Column(Text, nullable=True)

    analysis_model = Column(String, nullable=True)
    analysis_prompt_version = Column(String, nullable=True)
    analyzed_at = Column(DateTime, nullable=True)
    analysis_error = Column(Text, nullable=True)

    is_current = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=True)

class JobFetchRun(Base):
    __tablename__ = "job_fetch_runs"

    run_id = Column(Integer, primary_key=True, index=True)

    source = Column(String, nullable=False)
    fetch_type = Column(String, nullable=True)
    params_json = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    pages_checked = Column(Integer, default=0)
    last_checked_page = Column(Integer, nullable=True)

    jobs_seen_count = Column(Integer, default=0)
    new_jobs_count = Column(Integer, default=0)
    duplicate_jobs_count = Column(Integer, default=0)
    skipped_jobs_count = Column(Integer, default=0)

    stopped_reason = Column(String, nullable=True)
    status = Column(String, default="running")
    error_message = Column(Text, nullable=True)

class UserLanguage(Base):
    __tablename__ = "user_languages"

    user_language_id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)

    language_code = Column(String, nullable=False)
    language_name = Column(String, nullable=False)
    proficiency_level = Column(String, nullable=True)
    proficiency_scale = Column(String, default="CEFR", nullable=True)

    is_primary = Column(Boolean, default=False)
    created_at = Column(DateTime, nullable=True)

class JobMatch(Base):
    __tablename__ = "job_matches"

    match_id = Column(Integer, primary_key=True, index=True)

    profile_id = Column(Integer, ForeignKey("candidate_profiles.profile_id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.job_id"), nullable=False)

    profile_analysis_id = Column(Integer, ForeignKey("profile_analysis.analysis_id"), nullable=True)
    job_analysis_id = Column(Integer, ForeignKey("job_analysis.analysis_id"), nullable=True)

    match_status = Column(String, default="completed", nullable=True)
    match_json = Column(Text, nullable=True)

    role_fit_score = Column(Integer, nullable=True)
    skills_fit_score = Column(Integer, nullable=True)
    experience_fit_score = Column(Integer, nullable=True)
    seniority_fit_score = Column(Integer, nullable=True)
    location_fit_score = Column(Integer, nullable=True)
    work_type_fit_score = Column(Integer, nullable=True)
    language_fit_score = Column(Integer, nullable=True)
    visa_fit_score = Column(Integer, nullable=True)

    base_match_score = Column(Integer, nullable=True)
    practical_match_score = Column(Integer, nullable=True)
    overall_score = Column(Integer, nullable=True)

    recommendation = Column(String, nullable=True)
    summary = Column(Text, nullable=True)

    strengths_json = Column(Text, nullable=True)
    weaknesses_json = Column(Text, nullable=True)
    dealbreaker_warnings_json = Column(Text, nullable=True)

    match_model = Column(String, nullable=True)
    match_prompt_version = Column(String, nullable=True)
    matched_at = Column(DateTime, nullable=True)
    match_error = Column(Text, nullable=True)

    is_current = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=True)
