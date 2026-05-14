from pydantic import BaseModel, EmailStr


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


class CandidateProfileCreate(BaseModel):
    self_description: str | None = None

    target_role: str | None = None
    secondary_target_role: str | None = None

    target_location: str | None = None
    preferred_work_type: str | None = None

    preferred_technologies: str | None = None
    extra_preferences: str | None = None


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