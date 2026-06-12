from pydantic import BaseModel, EmailStr, ConfigDict, field_validator
from typing import Literal

from .taxonomy import ROLE_FAMILIES, ROLE_SUBFAMILIES, ROLE_TAGS, SKILL_TAGS


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
        invalid_values = [value for value in values if value not in ROLE_TAGS]
        if invalid_values:
            raise ValueError(f"Invalid role tags: {invalid_values}")
        return values


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
        invalid_values = [value for value in values if value not in ROLE_TAGS]
        if invalid_values:
            raise ValueError(f"Invalid role tags: {invalid_values}")
        return values

    @field_validator("required_skills")
    @classmethod
    def validate_required_skills(cls, values: list[str]) -> list[str]:
        invalid_values = [value for value in values if value not in SKILL_TAGS]
        if invalid_values:
            raise ValueError(f"Invalid required skills: {invalid_values}")
        return values

    @field_validator("preferred_skills")
    @classmethod
    def validate_preferred_skills(cls, values: list[str]) -> list[str]:
        invalid_values = [value for value in values if value not in SKILL_TAGS]
        if invalid_values:
            raise ValueError(f"Invalid preferred skills: {invalid_values}")
        return values