import logging
import os
import json
import tempfile

from pypdf import PdfReader

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session

from openai import OpenAI
from datetime import datetime, timezone

from ..app.database import get_db
from ..app import models, schemas
from ..app.analysis_guard import (
    AcquireOutcome,
    AnalysisGuardConfigError,
    load_config as load_analysis_guard_config,
    release_profile_analysis_guard,
    try_acquire_profile_analysis_guard,
)
from ..app.auth_dependencies import get_owned_profile, require_user_access
from ..app.taxonomy import ROLE_FAMILIES, ROLE_TAGS, SKILL_TAGS
from ..app.analysis_contract import (
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)

client = OpenAI()
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/users",
    tags=["Candidate Profiles"]
)



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
def get_user_profiles(
    user_id: int,
    current_user: models.User = Depends(require_user_access),
    db: Session = Depends(get_db),
):
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
    current_user: models.User = Depends(require_user_access),
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
        visa_sponsorship_needed=profile.visa_sponsorship_needed,
        work_authorization_status=profile.work_authorization_status,
        relocation_preference=profile.relocation_preference,
        years_of_experience=profile.years_of_experience,
        seniority_target=profile.seniority_target,
        current_residence_country=profile.current_residence_country,
        student_status=profile.student_status,
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


@router.patch("/{user_id}/profiles/{profile_id}", response_model=schemas.CandidateProfileResponse)
def update_candidate_profile(
    user_id: int,
    profile_id: int,
    profile_update: schemas.CandidateProfileUpdate,
    profile: models.CandidateProfile = Depends(get_owned_profile),
    db: Session = Depends(get_db),
):
    update_data = profile_update.model_dump(exclude_unset=True)
    languages = update_data.pop("languages", None)

    for field, value in update_data.items():
        setattr(profile, field, value)

    if languages is not None:
        db.query(models.UserLanguage).filter(
            models.UserLanguage.user_id == user_id
        ).delete()

        now = datetime.now(timezone.utc)
        for language in languages:
            db.add(models.UserLanguage(
                user_id=user_id,
                language_code=language["language_code"],
                language_name=language["language_name"],
                proficiency_level=language.get("proficiency_level"),
                proficiency_scale=language.get("proficiency_scale") or "CEFR",
                is_primary=language.get("is_primary", False),
                created_at=now,
            ))

    db.commit()
    db.refresh(profile)
    return profile


# Maximum accepted CV upload size. Enforced while streaming (not via the
# client-controlled Content-Length header), so an attacker cannot bypass it
# by omitting or lying about that header.
MAX_CV_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB
_CV_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _validate_cv_display_filename(filename: str | None) -> str:
    """Validates the client-supplied filename as display metadata only.
    This is NOT the security boundary against path traversal -- the stored
    filesystem filename is always fully server-generated (see upload_cv)
    regardless of what is accepted here.
    """
    if not filename:
        raise HTTPException(status_code=400, detail="A filename is required.")
    if "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=400, detail="Filename must not contain path separators."
        )
    if filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported for now")
    return filename


#PDF Upload
@router.post("/{user_id}/profiles/{profile_id}/upload-cv", response_model=schemas.CandidateProfileResponse)
def upload_cv(
    user_id: int,
    profile_id: int,
    file: UploadFile = File(...),
    profile: models.CandidateProfile = Depends(get_owned_profile),
    db: Session = Depends(get_db)
):
    display_filename = _validate_cv_display_filename(file.filename)

    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)

    # The on-disk filename is entirely server-generated from trusted path
    # parameters -- file.filename never reaches the filesystem path.
    final_filename = f"user_{user_id}_profile_{profile_id}_cv.pdf"
    final_path = os.path.join(upload_dir, final_filename)

    temp_file = tempfile.NamedTemporaryFile(
        dir=upload_dir, prefix=".cv_upload_", suffix=".tmp", delete=False
    )
    temp_path = temp_file.name
    new_file_installed = False
    db_committed = False
    backup_path = None

    try:
        # Nested try/finally: guarantees the temp file handle is closed on
        # every path (size-limit rejection, an unexpected write error, or
        # falling through normally) before any cleanup below ever attempts
        # to remove it -- an open handle can block deletion on Windows.
        try:
            total_bytes = 0
            while True:
                chunk = file.file.read(_CV_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_CV_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="CV file exceeds the maximum allowed size of 10 MiB.",
                    )
                temp_file.write(chunk)
        finally:
            temp_file.close()

        try:
            cv_text = extract_text_from_pdf(temp_path)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid or unreadable PDF file.")

        # Preserve any existing valid CV under a server-generated backup
        # name before installing the new one, so a later DB failure can
        # restore it atomically instead of leaving a stale-metadata /
        # overwritten-file mismatch.
        if os.path.exists(final_path):
            backup_file = tempfile.NamedTemporaryFile(
                dir=upload_dir, prefix=".cv_backup_", suffix=".bak", delete=False
            )
            backup_path = backup_file.name
            backup_file.close()
            os.replace(final_path, backup_path)

        try:
            os.replace(temp_path, final_path)
            new_file_installed = True
        except Exception:
            # The old CV (if any) was already moved to backup_path above --
            # restore it so a failed install never leaves the profile with
            # no CV at all.
            if backup_path is not None:
                os.replace(backup_path, final_path)
                backup_path = None
            raise HTTPException(
                status_code=500,
                detail="Failed to save the uploaded CV. Please try again.",
            )

        try:
            profile.cv_filename = display_filename
            profile.cv_file_path = final_path
            profile.cv_text = cv_text
            db.commit()
            db_committed = True
        except Exception:
            db.rollback()
            if backup_path is not None:
                # Atomically overwrites the failed new file with the
                # preserved original in one step.
                os.replace(backup_path, final_path)
                backup_path = None
            elif os.path.exists(final_path):
                os.remove(final_path)
            raise HTTPException(
                status_code=500,
                detail="Failed to save the uploaded CV. Please try again.",
            )

        db.refresh(profile)

        if backup_path is not None and os.path.exists(backup_path):
            os.remove(backup_path)
            backup_path = None

        return profile
    finally:
        if not new_file_installed and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        # The backup may only be deleted once the new file is both
        # installed and committed -- never on a failed install or a failed
        # commit, and never twice (the explicit removal above already
        # clears backup_path to None on every success/restore path, so this
        # is purely a safety net for anything unexpected in between).
        if backup_path is not None and new_file_installed and db_committed:
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass


@router.get("/{user_id}/profiles/analyzed")
def get_analyzed_profiles(
    user_id: int,
    current_user: models.User = Depends(require_user_access),
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
            models.ProfileAnalysis.analysis_status == "completed",
            models.ProfileAnalysis.analysis_model == PROFILE_ANALYSIS_MODEL,
            models.ProfileAnalysis.analysis_prompt_version == PROFILE_ANALYSIS_PROMPT_VERSION
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
            "current_residence_country": profile.current_residence_country,
            "student_status": profile.student_status,

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


def _create_profile_analysis_response(prompt: str, *, timeout_seconds: int, max_retries: int):
    """Single seam for the profile-analysis OpenAI call. Tests must patch
    this function directly -- patching client.responses.create does NOT
    work here, since client.with_options(...) returns a distinct client/
    resource instance (verified directly: a mock on the original client's
    .responses.create is never reached through a with_options()-derived
    client).
    """
    scoped_client = client.with_options(max_retries=max_retries)
    return scoped_client.responses.create(
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
        },
        timeout=timeout_seconds,
    )


@router.post("/{user_id}/profiles/{profile_id}/analyze")
def analyze_profile(
    user_id: int,
    profile_id: int,
    force_reanalyze: bool = Query(default=False),
    profile: models.CandidateProfile = Depends(get_owned_profile),
    db: Session = Depends(get_db)
):
    # Still queried directly (not just implied by get_owned_profile): the
    # analysis prompt below needs this user's own data, which matters in
    # particular when an admin is analyzing another user's profile.
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
        # Cache hit: returns before any E3.2 configuration is read and
        # before the guard is touched at all -- no OpenAI cost is at risk
        # here, so there is nothing to protect.
        return {
            "status": "cached",
            "message": "Profile already has a current completed analysis for this model and prompt version.",
            "profile_id": profile.profile_id,
            "analysis_id": existing_analysis.analysis_id,
            "analysis": json.loads(existing_analysis.analysis_json)
            if existing_analysis.analysis_json else None
        }

    # Configuration is validated first -- before any guard row or OpenAI
    # work -- and its HTTPException lives outside the analysis exception
    # handler below, so invalid configuration always maps to a plain 500
    # and never creates a failed-analysis row.
    try:
        analysis_guard_config = load_analysis_guard_config()
    except AnalysisGuardConfigError:
        raise HTTPException(
            status_code=500,
            detail="Analysis protection is not correctly configured.",
        )

    # Language lookup and prompt construction perform no billed call and
    # need no guard. If either fails, no guard row has been touched yet,
    # so there is nothing to release -- this propagates exactly as it did
    # before E3.2 (an unhandled exception from this route).
    languages = db.query(models.UserLanguage).filter(
        models.UserLanguage.user_id == user_id
    ).all()

    languages_text = build_languages_text(languages)

    prompt = f"""
You are a candidate profile parser for a job matching system.

Extract structured matching signals from the candidate profile.

Do not invent skills, experience, languages, authorization, education, or relocation details.
Use "unknown" for unclear free-text string fields.
For taxonomy fields such as current_role_family, target_role_families, and target_role_tags, use only the allowed taxonomy values.
For taxonomy fields, use "other" if unclear or if none of the allowed values fit.
For years_of_experience, use null if there is no clear evidence.
For visa_sponsorship_needed, use only: yes, no, unknown.
- Use yes when the candidate clearly needs employer sponsorship for the target country.
- Use no when the candidate clearly has valid authorization for the target country.
- Use unknown when the available data is not enough.
For work_authorization_status, use only: germany, eu_eea, other_country_only, none, unknown.
For student_status, use only: currently_enrolled, not_enrolled, unknown.
For current_residence_country, return a lowercase country name or unknown.
Do not treat a completed degree as current student enrollment.

Use only the allowed canonical taxonomy values where applicable.

For current_role_family and target_role_families:
- Choose only from the allowed role families.
- Do not invent new role family names.
- Use "other" only if none of the allowed role families fit.

For target_role_tags:
- Select 5 to 10 tags from the allowed role tags only.
- Put the closest and most important target_role_tags first.
- Do not create tags outside the allowed list.
- Use "other" only if none of the allowed tags fit.

For strong_skills, moderate_skills, weak_or_basic_skills, and tools:
- Prefer canonical skill tags from the allowed skill tags list when there is a clear match.
- Do not force everything into the allowed list if it would lose important detail.
- If a skill/tool is important but not in the allowed list, keep the original clear name.

Allowed role families:
{json.dumps(ROLE_FAMILIES, ensure_ascii=False)}

Allowed role tags:
{json.dumps(ROLE_TAGS, ensure_ascii=False)}

Allowed skill tags:
{json.dumps(SKILL_TAGS, ensure_ascii=False)}

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
Current residence country: {profile.current_residence_country or ""}
Student status: {profile.student_status or ""}

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

    guard_acquire_result = try_acquire_profile_analysis_guard(
        db,
        profile_id=profile.profile_id,
        owner_user_id=profile.user_id,
        config=analysis_guard_config,
    )

    if guard_acquire_result.outcome is AcquireOutcome.ALREADY_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="An analysis for this profile is already in progress.",
        )

    if guard_acquire_result.outcome is AcquireOutcome.COOLDOWN_ACTIVE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(guard_acquire_result.retry_after_seconds)},
        )

    if guard_acquire_result.outcome is AcquireOutcome.BACKEND_UNAVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please try again shortly.",
        )

    guard_owner_token = guard_acquire_result.owner_token
    # Set True only immediately after the paid analysis result is actually
    # committed. The single release point in `finally` below uses this
    # flag -- not whether the function ultimately returns or raises -- so
    # a later refresh/response failure after a real commit still applies
    # the success cooldown for the already-persisted, already-paid-for
    # result, and any pre-acquisition rejection above never reaches here
    # at all (so it can never trigger a release).
    analysis_committed = False

    now = datetime.now(timezone.utc)

    try:
        response = _create_profile_analysis_response(
            prompt,
            timeout_seconds=analysis_guard_config.openai_timeout_seconds,
            max_retries=analysis_guard_config.openai_max_retries,
        )

        raw_text = response.output_text.strip()
        analysis = json.loads(raw_text)

        validated_analysis = schemas.ProfileAnalysisStructured.model_validate(analysis)
        analysis = validated_analysis.model_dump()

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

            visa_sponsorship_needed=(
                True if analysis.get("visa_sponsorship_needed") == "yes"
                else False if analysis.get("visa_sponsorship_needed") == "no"
                else None
            ),
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
        analysis_committed = True
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

    finally:
        # Single guaranteed release point, covering every post-acquisition
        # path: the success return above, the handled-failure raise above,
        # and any exception raised by the failed-analysis bookkeeping
        # itself (db.rollback/add/commit) -- finally always runs even when
        # the except block above raises. succeeded reflects
        # analysis_committed, which is only ever set True immediately
        # after the paid analysis result is actually committed.
        try:
            release_profile_analysis_guard(
                db,
                profile_id=profile.profile_id,
                owner_user_id=profile.user_id,
                owner_token=guard_owner_token,
                succeeded=analysis_committed,
                config=analysis_guard_config,
            )
        except Exception as release_exc:
            # Defensive only -- release_profile_analysis_guard is designed
            # to never raise. Never let a release-time failure replace an
            # already-established return value or exception, and never log
            # the owner token, prompt, profile/CV content, or the raw
            # exception (its message could echo bound SQL parameters).
            logger.warning(
                "analyze_profile: guard release attempt raised unexpectedly (%s)",
                type(release_exc).__name__,
            )
