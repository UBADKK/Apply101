import os
import shutil
import json

from pypdf import PdfReader

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session

from openai import OpenAI
from datetime import datetime, timezone

from ..app.database import get_db
from ..app import models, schemas

client = OpenAI()

router = APIRouter(
    prefix="/users",
    tags=["Candidate Profiles"]
)

PROFILE_ANALYSIS_MODEL = "gpt-4.1-mini"
PROFILE_ANALYSIS_PROMPT_VERSION = "profile_analysis_v3"


def build_languages_text(languages: list[models.UserLanguage]) -> str:
    if not languages:
        return "No language information provided."

    lines = []

    for language in languages:
        primary_text = "primary" if language.is_primary else "not primary"

        lines.append(
            f"- {language.language_name} "
            f"({language.language_code}): "
            f"{language.proficiency_level or 'unknown'} "
            f"{language.proficiency_scale or ''} "
            f"({primary_text})"
        )

    return "\n".join(lines)


def extract_text_from_pdf(file_path: str) -> str:
    reader = PdfReader(file_path)

    text = ""

    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"

    return text.strip()


# Get user's profiles from DB
@router.get("/{user_id}/profiles", response_model=list[schemas.CandidateProfileResponse])
def get_user_profiles(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.user_id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    profiles = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.user_id == user_id
    ).all()

    return profiles


#Add new profile to a user
@router.post("/{user_id}/profiles", response_model=schemas.CandidateProfileResponse)
def create_candidate_profile(
    user_id: int,
    profile: schemas.CandidateProfileCreate,
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.user_id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_profile = models.CandidateProfile(
        user_id=user_id,
        self_description=profile.self_description,
        target_role=profile.target_role,
        secondary_target_role=profile.secondary_target_role,
        target_location=profile.target_location,
        preferred_work_type=profile.preferred_work_type,
        preferred_technologies=profile.preferred_technologies,
        extra_preferences=profile.extra_preferences,
    )

    db.add(new_profile)

    if profile.languages is not None:
        db.query(models.UserLanguage).filter(
            models.UserLanguage.user_id == user_id
        ).delete()

        now = datetime.now(timezone.utc)

        for language in profile.languages:
            new_language = models.UserLanguage(
                user_id=user_id,
                language_code=language.language_code,
                language_name=language.language_name,
                proficiency_level=language.proficiency_level,
                proficiency_scale=language.proficiency_scale,
                is_primary=language.is_primary,
                created_at=now
            )

            db.add(new_language)

    db.commit()
    db.refresh(new_profile)

    return new_profile


#PDF Upload
@router.post("/{user_id}/profiles/{profile_id}/upload-cv", response_model=schemas.CandidateProfileResponse)
def upload_cv(
    user_id: int,
    profile_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported for now")

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id
    ).first()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)

    safe_filename = f"user_{user_id}_profile_{profile_id}_{file.filename}"
    file_path = os.path.join(upload_dir, safe_filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    cv_text = extract_text_from_pdf(file_path)

    profile.cv_filename = file.filename
    profile.cv_file_path = file_path
    profile.cv_text = cv_text

    db.commit()
    db.refresh(profile)

    return profile


@router.get("/{user_id}/profiles/analyzed")
def get_analyzed_profiles(
    user_id: int,
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.user_id == user_id
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    analyzed_profiles = (
        db.query(models.CandidateProfile, models.ProfileAnalysis)
        .join(
            models.ProfileAnalysis,
            models.CandidateProfile.profile_id == models.ProfileAnalysis.profile_id
        )
        .filter(
            models.CandidateProfile.user_id == user_id,
            models.ProfileAnalysis.is_current == True,
            models.ProfileAnalysis.analysis_status == "completed"
        )
        .all()
    )

    result = []

    for profile, analysis in analyzed_profiles:
        result.append({
            "profile_id": profile.profile_id,
            "user_id": profile.user_id,
            "profile_name": profile.profile_name,
            "cv_filename": profile.cv_filename,

            "self_description": profile.self_description,
            "target_role": profile.target_role,
            "secondary_target_role": profile.secondary_target_role,
            "target_location": profile.target_location,
            "preferred_work_type": profile.preferred_work_type,
            "preferred_technologies": profile.preferred_technologies,
            "extra_preferences": profile.extra_preferences,

            "visa_sponsorship_needed": profile.visa_sponsorship_needed,
            "work_authorization_status": profile.work_authorization_status,
            "relocation_preference": profile.relocation_preference,
            "years_of_experience": profile.years_of_experience,
            "seniority_target": profile.seniority_target,

            "analysis": {
                "analysis_id": analysis.analysis_id,
                "analysis_status": analysis.analysis_status,

                "candidate_summary": analysis.candidate_summary,
                "current_role_family": analysis.current_role_family,
                "seniority_level": analysis.seniority_level,
                "education_level": analysis.education_level,
                "field_of_study": analysis.field_of_study,

                "strong_skills": json.loads(analysis.strong_skills_json or "[]"),
                "moderate_skills": json.loads(analysis.moderate_skills_json or "[]"),
                "weak_or_basic_skills": json.loads(analysis.weak_or_basic_skills_json or "[]"),
                "tools": json.loads(analysis.tools_json or "[]"),
                "languages": json.loads(analysis.languages_json or "[]"),
                "target_roles": json.loads(analysis.target_roles_json or "[]"),
                "target_role_families": json.loads(analysis.target_role_families_json or "[]"),
                "target_role_tags": json.loads(analysis.target_role_tags_json or "[]"),
                "excluded_roles": json.loads(analysis.excluded_roles_json or "[]"),

                "visa_sponsorship_needed": analysis.visa_sponsorship_needed,
                "work_authorization_status": analysis.work_authorization_status,
                "relocation_preference": analysis.relocation_preference,

                "analysis_model": analysis.analysis_model,
                "analysis_prompt_version": analysis.analysis_prompt_version,
                "analyzed_at": analysis.analyzed_at,
                "analysis_error": analysis.analysis_error,
                "is_current": analysis.is_current,
                "created_at": analysis.created_at,
            }
        })

    return result



ALLOWED_ROLE_TAGS = [
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
]


@router.post("/{user_id}/profiles/{profile_id}/analyze")
def analyze_profile(
    user_id: int,
    profile_id: int,
    force_reanalyze: bool = Query(default=False),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.user_id == user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_USER_NOT_FOUND",
                "message": f"User with id {user_id} was not found."
            }
        )

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_PROFILE_NOT_FOUND",
                "message": f"Profile with id {profile_id} was not found for user {user_id}."
            }
        )

    has_profile_text = bool(
        profile.cv_text
        or profile.self_description
        or profile.target_role
        or profile.preferred_roles_json
        or profile.preferred_technologies
    )

    if not has_profile_text:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_PROFILE_DATA_MISSING",
                "message": "Profile does not have enough information to analyze.",
                "profile_id": profile_id
            }
        )

    existing_analysis = db.query(models.ProfileAnalysis).filter(
        models.ProfileAnalysis.profile_id == profile_id,
        models.ProfileAnalysis.analysis_status == "completed",
        models.ProfileAnalysis.analysis_model == PROFILE_ANALYSIS_MODEL,
        models.ProfileAnalysis.analysis_prompt_version == PROFILE_ANALYSIS_PROMPT_VERSION,
        models.ProfileAnalysis.is_current == True
    ).first()

    if existing_analysis and not force_reanalyze:
        return {
            "status": "cached",
            "message": "Profile already has a current completed analysis for this model and prompt version.",
            "profile_id": profile.profile_id,
            "analysis_id": existing_analysis.analysis_id,
            "analysis": json.loads(existing_analysis.analysis_json)
            if existing_analysis.analysis_json else None
        }

    languages = db.query(models.UserLanguage).filter(
        models.UserLanguage.user_id == user_id
    ).all()

    languages_text = build_languages_text(languages)

    prompt = f"""
You are a candidate profile parser for a job matching system.

Extract structured matching signals from the candidate profile.

Do not invent skills, experience, languages, authorization, education, or relocation details.
Use "unknown" for unclear string fields.
For years_of_experience, use 0 if there is no clear experience.
For visa_sponsorship_needed, use true only if the profile clearly says sponsorship is needed.
For target_role_tags, select 5 to 10 tags from the allowed list only.
Put the closest and most important target_role_tags first.
Do not create tags outside the allowed list.
Use "other" only if none of the allowed tags fit.

Allowed role tags:
{json.dumps(ALLOWED_ROLE_TAGS, ensure_ascii=False)}

User basic information:
Name: {user.name or ""}
Email: {user.mail or ""}
Skills from user record: {user.skills or ""}
Experience years from user record: {user.experience_years if user.experience_years is not None else "unknown"}
Major: {user.major or ""}
Has master degree: {user.master}
Has PhD: {user.phd}
Has Abitur: {user.abitur}

Profile information:
Profile name: {profile.profile_name or ""}
Target role: {profile.target_role or ""}
Secondary target role: {profile.secondary_target_role or ""}
Preferred roles JSON: {profile.preferred_roles_json or ""}
Excluded roles JSON: {profile.excluded_roles_json or ""}
Target location: {profile.target_location or ""}
Target locations JSON: {profile.target_locations_json or ""}
Preferred work type: {profile.preferred_work_type or ""}
Preferred technologies: {profile.preferred_technologies or ""}
Extra preferences: {profile.extra_preferences or ""}

Visa and relocation:
Visa sponsorship needed: {profile.visa_sponsorship_needed}
Work authorization status: {profile.work_authorization_status or ""}
Relocation preference: {profile.relocation_preference or ""}

Experience and seniority:
Years of experience: {profile.years_of_experience if profile.years_of_experience is not None else "unknown"}
Seniority target: {profile.seniority_target or ""}

Languages:
{languages_text}

Self description:
{profile.self_description or ""}

CV text:
{(profile.cv_text or "")[:12000]}
"""

    now = datetime.now(timezone.utc)

    try:
        response = client.responses.create(
            model=PROFILE_ANALYSIS_MODEL,
            input=prompt,
            temperature=0,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "profile_analysis",
                    "schema": schemas.ProfileAnalysisStructured.model_json_schema(),
                    "strict": True
                }
            }
        )

        raw_text = response.output_text.strip()
        analysis = json.loads(raw_text)

        db.query(models.ProfileAnalysis).filter(
            models.ProfileAnalysis.profile_id == profile.profile_id,
            models.ProfileAnalysis.is_current == True
        ).update({
            "is_current": False
        })

        new_analysis = models.ProfileAnalysis(
            profile_id=profile.profile_id,
            analysis_status="completed",
            analysis_json=json.dumps(analysis, ensure_ascii=False),

            candidate_summary=analysis.get("candidate_summary"),
            current_role_family=analysis.get("current_role_family"),
            seniority_level=analysis.get("seniority_level"),
            education_level=analysis.get("education_level"),
            field_of_study=analysis.get("field_of_study"),

            strong_skills_json=json.dumps(
                analysis.get("strong_skills", []),
                ensure_ascii=False
            ),
            moderate_skills_json=json.dumps(
                analysis.get("moderate_skills", []),
                ensure_ascii=False
            ),
            weak_or_basic_skills_json=json.dumps(
                analysis.get("weak_or_basic_skills", []),
                ensure_ascii=False
            ),
            tools_json=json.dumps(
                analysis.get("tools", []),
                ensure_ascii=False
            ),
            languages_json=json.dumps(
                analysis.get("languages", []),
                ensure_ascii=False
            ),
            target_roles_json=json.dumps(
                analysis.get("target_roles", []),
                ensure_ascii=False
            ),
            target_role_families_json=json.dumps(
                analysis.get("target_role_families", []),
                ensure_ascii=False
            ),
            target_role_tags_json=json.dumps(
                analysis.get("target_role_tags", []),
                ensure_ascii=False
            ),
            excluded_roles_json=json.dumps(
                analysis.get("excluded_roles", []),
                ensure_ascii=False
            ),

            visa_sponsorship_needed=analysis.get("visa_sponsorship_needed"),
            work_authorization_status=analysis.get("work_authorization_status"),
            relocation_preference=analysis.get("relocation_preference"),

            analysis_model=PROFILE_ANALYSIS_MODEL,
            analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
            analyzed_at=now,
            analysis_error=None,
            is_current=True,
            created_at=now
        )

        db.add(new_analysis)
        db.commit()
        db.refresh(new_analysis)

        return {
            "status": "created",
            "profile_id": profile.profile_id,
            "analysis_id": new_analysis.analysis_id,
            "analysis_model": new_analysis.analysis_model,
            "analysis_prompt_version": new_analysis.analysis_prompt_version,
            "analysis": analysis
        }

    except Exception as e:
        db.rollback()

        failed_analysis = models.ProfileAnalysis(
            profile_id=profile.profile_id,
            analysis_status="failed",
            analysis_json=None,
            analysis_model=PROFILE_ANALYSIS_MODEL,
            analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
            analyzed_at=now,
            analysis_error=str(e),
            is_current=False,
            created_at=now
        )

        db.add(failed_analysis)
        db.commit()

        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ERR_PROFILE_ANALYSIS_FAILED",
                "message": "Profile analysis failed.",
                "profile_id": profile.profile_id,
                "error": str(e)
            }
        )