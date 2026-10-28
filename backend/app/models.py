from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, Text, DateTime, Index, UniqueConstraint, false, text
from sqlalchemy.orm import relationship
from .database import Base
from .security import new_token_key


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

    # Nullable: existing rows have no password until explicitly migrated
    # (see backend/migrations/phase3_auth_fields.py). Never expose this
    # column in an API response schema.
    password_hash = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False, server_default=false())

    # Per-user random key embedded in every access token (claim "tkey") and
    # checked against this value on each request, so a token can never
    # authenticate a different row that later reuses the same user_id.
    # Nullable in the DB only because SQLite's ADD COLUMN can't give each
    # existing row its own value (see
    # backend/migrations/phase6_user_token_key.py); a NULL key rejects every
    # token until login assigns one. Never expose this column in an API
    # response schema.
    token_key = Column(String, nullable=True, default=new_token_key)


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

    current_residence_country = Column(String, nullable=True)
    student_status = Column(String, nullable=True)

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
    target_role_tags_json = Column(Text, nullable=True)

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

    # url uniqueness depends on ownership (exact string equality, no
    # normalization): ownerless catalog jobs have unique urls, and one user
    # cannot own the same url twice; a catalog job and a personal job, or
    # personal jobs of different users, may share a url. Existing databases
    # get these indexes only via backend/migrations/phase7_job_url_ownership.py
    # (create_all never alters indexes of an existing table), which reuses
    # these Index objects.
    __table_args__ = (
        Index(
            "uq_jobs_catalog_url",
            "url",
            unique=True,
            sqlite_where=text("created_by_user_id IS NULL"),
        ),
        Index(
            "uq_jobs_owner_url",
            "created_by_user_id",
            "url",
            unique=True,
            sqlite_where=text("created_by_user_id IS NOT NULL"),
        ),
    )

    job_id = Column(Integer, primary_key=True, index=True)

    title = Column(String, nullable=False)
    company_name = Column(String, nullable=True)
    location = Column(String, nullable=True)

    url = Column(String, nullable=False)

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

    # NULL = ownerless (e.g. fetched from a public job board), visible to
    # every authenticated user. Non-NULL = owned by that user, visible only
    # to them and to admins (see backend/app/job_visibility.py). Added by
    # backend/migrations/phase5_job_owner.py; existing rows stay NULL.
    created_by_user_id = Column(
        Integer, ForeignKey("users.user_id"), nullable=True, index=True
    )

    # Catalog-only fields, set by backend/app/catalog_sync.py; personal
    # (manual / imported) jobs leave them NULL. Added by
    # backend/migrations/phase9_catalog_sources.py.
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=True, index=True)
    # "remote" / "hybrid" / "onsite" only when the source states it.
    work_mode = Column(String, nullable=True)
    # ISO 3166-1 alpha-2, only from a structured source field.
    country_code = Column(String, nullable=True)
    # sha256 of the analysis inputs (title, company, location, description);
    # see catalog_sync.job_content_hash.
    content_hash = Column(String, nullable=True)
    closed_at = Column(DateTime, nullable=True)

class Company(Base):
    """A company of the shared catalog. Filled only from catalog sources
    (backend/app/catalog_sync.py and the company board list), never from a
    user's CV, notes or personal jobs."""

    __tablename__ = "companies"

    company_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # catalog_sync.normalize_company_name(name); one row per company.
    normalized_name = Column(String, nullable=False, unique=True)
    website = Column(String, nullable=True)
    # Where the company is mainly listed for (e.g. "DE", "TR"), from the
    # company board list; informational only.
    country_code = Column(String, nullable=True)
    # Public text about the company and where it came from (e.g. a
    # Greenhouse board intro). Never generated.
    about_text = Column(Text, nullable=True)
    about_source_url = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class JobSourceBoard(Base):
    """A company's public job board on one source (Greenhouse board token,
    Lever site, Ashby job board name). Configured by admins."""

    __tablename__ = "job_source_boards"
    __table_args__ = (
        UniqueConstraint("source", "board_token", "region", name="uq_job_source_boards_source_token_region"),
    )

    board_id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False, index=True)
    source = Column(String, nullable=False)
    board_token = Column(String, nullable=False)
    # Lever: "global" or "eu"; other sources: "global".
    region = Column(String, nullable=False, default="global")
    is_enabled = Column(Boolean, nullable=False, default=False)
    # "unverified" until a live fetch succeeded, then "verified"; "failed"
    # after a failed check.
    verification_status = Column(String, nullable=False, default="unverified")
    verified_at = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String, nullable=True)
    last_active_jobs_count = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class JobSourceRecord(Base):
    """One posting as a source lists it, linked to the catalog job that
    holds it. Several records may point to one job: exact copies of the
    same posting on different sources (see catalog_sync)."""

    __tablename__ = "job_source_records"
    __table_args__ = (
        UniqueConstraint("source", "source_key", name="uq_job_source_records_source_key"),
    )

    record_id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.job_id"), nullable=False, index=True)
    board_id = Column(Integer, ForeignKey("job_source_boards.board_id"), nullable=True, index=True)
    source = Column(String, nullable=False)
    # Source-unique id: "<board>:<posting id>" for ATS sources, the slug
    # for Arbeitnow.
    source_key = Column(String, nullable=False)
    url = Column(String, nullable=False)
    # The record whose content the job shows; copies only link.
    is_primary = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    content_hash = Column(String, nullable=True)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)


class JobAnalysis(Base):
    __tablename__ = "job_analysis"

    analysis_id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.job_id"), nullable=False)

    analysis_status = Column(String, default="pending", nullable=True)
    analysis_json = Column(Text, nullable=True)

    role_family = Column(String, nullable=True)
    role_subfamily = Column(String, nullable=True)
    normalized_role_title = Column(String, nullable=True)
    role_tags_json = Column(Text, nullable=True)

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

    # Added by backend/migrations/phase9_catalog_sources.py.
    board_id = Column(Integer, ForeignKey("job_source_boards.board_id"), nullable=True)
    updated_jobs_count = Column(Integer, nullable=True)
    closed_jobs_count = Column(Integer, nullable=True)
    linked_duplicates_count = Column(Integer, nullable=True)
    # True only when every page of the board was read.
    complete = Column(Boolean, nullable=True)

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


class AnalysisGuard(Base):
    """Generic cooperative lock + cooldown row, one per (operation_type,
    resource_id). Used by backend/app/analysis_guard.py to serialize/rate-
    limit expensive per-resource operations (currently profile analysis).

    lock_expires_at/cooldown_until are Unix epoch seconds (Float), never
    SQLAlchemy DateTime -- SQLite does not round-trip timezone-aware
    datetimes (verified: they come back naive, which breaks comparisons
    against a fresh timezone-aware `now`), and persisted lease/cooldown
    state must survive a process restart, which time.monotonic() cannot.
    """

    __tablename__ = "analysis_guards"

    operation_type = Column(String, primary_key=True)
    resource_id = Column(Integer, primary_key=True)
    owner_token = Column(String, nullable=True)
    lock_expires_at = Column(Float, nullable=True)
    cooldown_until = Column(Float, nullable=True)


class AnalysisQuotaReservation(Base):
    """One counted attempt of a rolling per-user quota (currently owner
    manual job analysis), inserted by backend/app/analysis_guard.py inside
    the guard transaction right before the paid call; never refunded.
    reserved_at is Unix epoch seconds (Float), like AnalysisGuard. No
    foreign keys (like analysis_guards); job_id is for auditing only.
    Existing databases get this table via
    backend/migrations/phase8_analysis_quota_reservations.py or via the
    Base.metadata.create_all call at app startup (backend/app/main.py).
    """

    __tablename__ = "analysis_quota_reservations"
    __table_args__ = (
        Index(
            "ix_analysis_quota_reservations_op_user_reserved_at",
            "operation_type",
            "user_id",
            "reserved_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    operation_type = Column(String, nullable=False)
    user_id = Column(Integer, nullable=False)
    job_id = Column(Integer, nullable=True)
    reserved_at = Column(Float, nullable=False)


class CoverLetter(Base):
    """A user's motivation letter for one job in one language. Visible to
    its user and, like profiles and CVs, to admins; never shared and never
    part of the catalog. The job title and
    company are kept as written at creation, so the letter stays readable
    if the job changes or is deleted. Created by
    backend/migrations/phase10_cover_letters.py or create_all."""

    __tablename__ = "cover_letters"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", "language", name="uq_cover_letters_user_job_language"),
    )

    letter_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("candidate_profiles.profile_id"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.job_id"), nullable=True, index=True)
    language = Column(String, nullable=False)
    job_title = Column(String, nullable=True)
    company_name = Column(String, nullable=True)
    current_version_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class CoverLetterVersion(Base):
    """One saved text of a letter. Versions are never changed or replaced:
    a regeneration or an edit adds a new one, so an edited draft is never
    overwritten. kind is "generated" or "edited"."""

    __tablename__ = "cover_letter_versions"
    __table_args__ = (
        UniqueConstraint("letter_id", "version_no", name="uq_cover_letter_versions_letter_version"),
    )

    version_id = Column(Integer, primary_key=True, index=True)
    letter_id = Column(Integer, ForeignKey("cover_letters.letter_id"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)
    body_text = Column(Text, nullable=False)
    # Generated versions: the facts given to the model (with their source),
    # the facts it says it used, and what was missing.
    grounding_json = Column(Text, nullable=True)
    model = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)
    # Owner of the letter, for the rolling per-user generation quota.
    user_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, nullable=True)
