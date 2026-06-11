from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Literal


class UserCreate(BaseModel):
    name: str
    mail: EmailStr
    skills: str | None = None
    experience_years: float | None = None
    major: str | None = None
    master: bool = False
    phd: bool = False
    abitur: bool = False


class UserResponse(BaseModel):
    user_id: int
    name: str
    mail: EmailStr
    skills: str | None = None
    experience_years: float | None = None
    major: str | None = None
    master: bool
    phd: bool
    abitur: bool

    class Config:
        from_attributes = True


class ProfileLanguageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str
    level: str
    scale: str


class UserLanguageInput(BaseModel):
    language_code: str
    language_name: str
    proficiency_level: str | None = None
    proficiency_scale: str | None = "CEFR"
    is_primary: bool = False
    

class CandidateProfileCreate(BaseModel):
    self_description: str | None = None

    target_role: str | None = None
    secondary_target_role: str | None = None

    target_location: str | None = None
    preferred_work_type: str | None = None

    preferred_technologies: str | None = None
    extra_preferences: str | None = None

    languages: list[UserLanguageInput] | None = None


class CandidateProfileResponse(BaseModel):
    profile_id: int
    user_id: int

    cv_filename: str | None = None
    cv_file_path: str | None = None
    cv_text: str | None = None

    self_description: str | None = None

    target_role: str | None = None
    secondary_target_role: str | None = None

    target_location: str | None = None
    preferred_work_type: str | None = None

    preferred_technologies: str | None = None
    extra_preferences: str | None = None

    is_active: bool

    class Config:
        from_attributes = True


#Currently not being used!
class JobCreate(BaseModel):
    title: str
    company_name: str | None = None
    location: str | None = None
    url: str
    description_text: str | None = None


class JobResponse(BaseModel):
    job_id: int
    title: str
    company_name: str | None = None
    location: str | None = None
    url: str
    description_text: str | None = None

    class Config:
        from_attributes = True


class MatchResult(BaseModel):
    score: int
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    recommendation: Literal[
        "strong_apply",
        "apply",
        "maybe",
        "weak_match"
    ]


class ProfileAnalysisStructured(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_summary: str
    current_role_family: Literal[
        "engineering", "data", "product", "design", "marketing", "sales",
        "customer_success", "operations", "business_analysis",
        "project_management", "finance", "accounting", "hr", "legal",
        "consulting", "strategy", "it", "cybersecurity", "qa_testing",
        "devops", "research", "education", "healthcare", "logistics",
        "supply_chain", "manufacturing", "administration", "support",
        "content", "media", "other", "unknown"
    ]
    target_role_families: list[str]
    target_role_tags: list[str]
    target_roles: list[str]
    excluded_roles: list[str]

    strong_skills: list[str]
    moderate_skills: list[str]
    weak_or_basic_skills: list[str]
    tools: list[str]
    industries: list[str]

    years_of_experience: float
    seniority_level: Literal[
        "intern", "junior", "mid", "senior", "lead", "executive", "unknown"
    ]
    education_level: Literal[
        "high_school", "bachelor", "master", "phd", "bootcamp", "unknown"
    ]
    field_of_study: str

    languages: list[ProfileLanguageItem]

    visa_sponsorship_needed: bool
    work_authorization_status: str
    relocation_preference: str
    match_notes: list[str]


class JobAnalysisStructured(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str

    role_family: Literal[
        "engineering", "data", "product", "design", "marketing", "sales",
        "customer_success", "operations", "business_analysis",
        "project_management", "finance", "accounting", "hr", "legal",
        "consulting", "strategy", "it", "cybersecurity", "qa_testing",
        "devops", "research", "education", "healthcare", "logistics",
        "supply_chain", "manufacturing", "administration", "support",
        "content", "media", "other", "unknown"
    ]

    role_subfamily: str
    normalized_role_title: str
    role_tags: list[Literal[
        "software_engineering",
        "software_development",
        "backend_development",
        "frontend_development",
        "fullstack_development",
        "api_development",
        "python_development",
        "java_development",
        "javascript_development",
        "mobile_development",
        "game_development",
        "data_analysis",
        "data_science",
        "data_engineering",
        "machine_learning",
        "ai_engineering",
        "devops",
        "cloud_engineering",
        "qa_testing",
        "cybersecurity",
        "it_support",
        "technical_support",
        "product_management",
        "project_management",
        "business_analysis",
        "consulting",
        "sales",
        "marketing",
        "customer_support",
        "education",
        "finance",
        "accounting",
        "hr",
        "legal",
        "operations",
        "logistics",
        "supply_chain",
        "manufacturing",
        "design",
        "content",
        "other"
    ]]

    required_skills: list[str]
    preferred_skills: list[str]
    responsibilities: list[str]

    seniority_level: Literal[
        "intern", "junior", "mid", "senior", "lead", "executive", "unknown"
    ]

    language_requirements: list[str]

    visa_sponsorship: Literal["yes", "no", "unknown"]

    work_type: Literal["remote", "hybrid", "onsite", "unknown"]

    employment_type: Literal[
        "full-time",
        "part-time",
        "internship",
        "working-student",
        "contract",
        "freelance",
        "temporary",
        "unknown"
    ]

    dealbreakers: list[str]

