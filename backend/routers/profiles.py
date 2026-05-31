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
PROFILE_ANALYSIS_PROMPT_VERSION = "profile_analysis_v1"


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

Extract the key matching signals from this candidate profile.
Do not invent information. If something is unclear, return "unknown".

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

Return ONLY valid JSON in this exact structure:
{{
  "candidate_summary": "short but information-dense summary",
  "current_role_family": "engineering|data|product|design|marketing|sales|customer_success|operations|business_analysis|project_management|finance|accounting|hr|legal|consulting|strategy|it|cybersecurity|qa_testing|devops|research|education|healthcare|logistics|supply_chain|manufacturing|administration|support|content|media|other|unknown",
  "target_role_families": ["engineering", "data"],
  "target_roles": ["backend software engineer", "python developer"],
  "excluded_roles": ["role 1", "role 2"],
  "strong_skills": ["skill 1", "skill 2"],
  "moderate_skills": ["skill 1", "skill 2"],
  "weak_or_basic_skills": ["skill 1", "skill 2"],
  "tools": ["tool 1", "tool 2"],
  "industries": ["industry 1", "industry 2"],
  "years_of_experience": 0,
  "seniority_level": "intern|junior|mid|senior|lead|executive|unknown",
  "education_level": "high_school|bachelor|master|phd|bootcamp|unknown",
  "field_of_study": "field of study or unknown",
  "languages": [
    {{
      "language": "English",
      "level": "C1",
      "scale": "CEFR"
    }}
  ],
  "visa_sponsorship_needed": true,
  "work_authorization_status": "string or unknown",
  "relocation_preference": "string or unknown",
  "match_notes": ["important note 1", "important note 2"]
}}

Rules:
- Return only valid JSON.
- Do not include markdown.
- Do not invent skills, experience, languages, authorization, or education.
- Use "unknown" when unclear.
- current_role_family must be selected from the allowed list.
- target_role_families must only contain values from the allowed role family list.
- years_of_experience must be a number. Use 0 if there is no clear experience.
- Classify skills based on evidence from CV, self description, projects, work experience, and technologies.
- Put clearly demonstrated skills into strong_skills.
- Put mentioned but less proven skills into moderate_skills.
- Put basic, beginner, or lightly mentioned skills into weak_or_basic_skills.
- Include visa sponsorship and relocation details exactly as provided. Do not guess.
"""

    now = datetime.now(timezone.utc)

    try:
        response = client.responses.create(
            model=PROFILE_ANALYSIS_MODEL,
            input=prompt,
            temperature=0
        )

        raw_text = response.output_text.strip()

        try:
            analysis = json.loads(raw_text)
        except json.JSONDecodeError:
            failed_analysis = models.ProfileAnalysis(
                profile_id=profile.profile_id,
                analysis_status="failed",
                analysis_json=None,
                analysis_model=PROFILE_ANALYSIS_MODEL,
                analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
                analyzed_at=now,
                analysis_error=f"AI response was not valid JSON: {raw_text[:1000]}",
                is_current=False,
                created_at=now
            )

            db.add(failed_analysis)
            db.commit()

            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "ERR_AI_INVALID_JSON",
                    "message": "AI response was not valid JSON.",
                    "profile_id": profile.profile_id,
                    "raw_response": raw_text
                }
            )

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

    except HTTPException:
        raise

    except Exception as e:
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
    
    