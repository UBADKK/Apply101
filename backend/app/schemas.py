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