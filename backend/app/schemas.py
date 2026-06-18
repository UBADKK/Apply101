from pydantic import BaseModel, EmailStr, ConfigDict, field_validator
from typing import Literal

from .taxonomy import ROLE_FAMILIES, ROLE_SUBFAMILIES, ROLE_TAGS, SKILL_TAGS

ROLE_TAG_ALIASES = {
    "rest_api": "api_development",
    "rest_apis": "api_development",
    "api": "api_development",
    "api_design": "api_development",

    "backend": "backend_development",
    "frontend": "frontend_development",
    "fullstack": "fullstack_development",

    "python": "python_development",
    "django": "django_development",
    "fastapi": "fastapi_development",
    "java": "java_development",
    "javascript": "javascript_development",
    "typescript": "typescript_development",
    "react": "react_development",

    "machine_learning_engineering": "machine_learning",
    "ml_engineering": "machine_learning",
    "openai_api": "ai_engineering",

    "software_testing": "qa_testing",
    "automation_testing": "qa_testing",
    "automated_testing": "qa_testing",
    "testing": "qa_testing",

    "affiliate_marketing": "marketing",
    "content_creation": "marketing",
    "social_media_marketing": "marketing",
    "community_management": "marketing",

    "leadership": "operations",
    "non_profit": "operations",
}


def normalize_role_tags(values: list[str]) -> list[str]:
    normalized_values = []

    for raw_value in values:
        value = str(raw_value).strip().lower().replace("-", "_").replace(" ", "_")
        value = ROLE_TAG_ALIASES.get(value, value)

        if value not in ROLE_TAGS:
            value = "other"

        if value not in normalized_values:
            normalized_values.append(value)

    return normalized_values


SKILL_TAG_ALIASES = {
    "rest_apis": "rest_api",
    "api": "rest_api",
    "api_design": "rest_api",

    "github": "git",

    "microsoft_office": "excel",
    "ms_office": "excel",

    "automated_testing": "other",
    "automation_testing": "other",
    "software_testing": "other",
    "test_methods": "other",
    "test_tools": "other",

    "sap": "other",
    "sap_abap": "other",
    "sap_gui": "other",
    "sap_fiori": "other",

    "fundraising": "other",
    "budgeting": "other",
    "project_management": "jira",

    "leadership": "other",
    "communication": "other",
    "time_management": "other",
    "problem_solving": "other",
}

LANGUAGE_SKILL_VALUES = {
    "german",
    "english",
    "turkish",
    "deutsch",
    "englisch",
    "türkisch",
}


def normalize_skill_tags(values: list[str]) -> list[str]:
    normalized_values = []

    for raw_value in values:
        value = str(raw_value).strip().lower().replace("-", "_").replace(" ", "_")

        if value in LANGUAGE_SKILL_VALUES:
            continue

        value = SKILL_TAG_ALIASES.get(value, value)

        if value not in SKILL_TAGS:
            value = "other"

        if value not in normalized_values:
            normalized_values.append(value)

    return normalized_values



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
    current_role_family: str
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

    @field_validator("current_role_family")
    @classmethod
    def validate_current_role_family(cls, value: str) -> str:
        if value not in ROLE_FAMILIES:
            raise ValueError(f"Invalid role family: {value}")
        return value

    @field_validator("target_role_families")
    @classmethod
    def validate_target_role_families(cls, values: list[str]) -> list[str]:
        invalid_values = [value for value in values if value not in ROLE_FAMILIES]
        if invalid_values:
            raise ValueError(f"Invalid role families: {invalid_values}")
        return values


    @field_validator("target_role_tags")
    @classmethod
    def validate_target_role_tags(cls, values: list[str]) -> list[str]:
        return normalize_role_tags(values)

class JobAnalysisStructured(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str

    role_family: str
    role_subfamily: str
    normalized_role_title: str
    role_tags: list[str]

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
        "full_time",
        "part_time",
        "internship",
        "working_student",
        "contract",
        "freelance",
        "temporary",
        "unknown"
    ]

    dealbreakers: list[str]

    @field_validator("role_family")
    @classmethod
    def validate_role_family(cls, value: str) -> str:
        if value not in ROLE_FAMILIES:
            raise ValueError(f"Invalid role family: {value}")
        return value

    @field_validator("role_subfamily")
    @classmethod
    def validate_role_subfamily(cls, value: str) -> str:
        if value not in ROLE_SUBFAMILIES:
            raise ValueError(f"Invalid role subfamily: {value}")
        return value

    @field_validator("role_tags")
    @classmethod
    def validate_role_tags(cls, values: list[str]) -> list[str]:
        return normalize_role_tags(values)

    @field_validator("required_skills")
    @classmethod
    def validate_required_skills(cls, values: list[str]) -> list[str]:
        return normalize_skill_tags(values)

    @field_validator("preferred_skills")
    @classmethod
    def validate_preferred_skills(cls, values: list[str]) -> list[str]:
        return normalize_skill_tags(values)