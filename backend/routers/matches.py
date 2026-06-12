import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..app.database import get_db
from ..app import models


router = APIRouter(
    prefix="/users",
    tags=["Job Matching"]
)


MATCH_MODEL = "backend_rule_based"
MATCH_PROMPT_VERSION = "backend_match_v1"
MAX_BATCH_MATCH_JOBS = 200

REQUIRED_JOB_ANALYSIS_MODEL = "gpt-4.1-mini"
REQUIRED_JOB_ANALYSIS_PROMPT_VERSION = "job_analysis_v5"


def safe_json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def normalize_text(value: str) -> str:
    return (value or "").lower().strip()


def normalize_list(values):
    if not values:
        return []
    return [normalize_text(v) for v in values if isinstance(v, str) and v.strip()]


def overlap_score(candidate_items, job_items):
    candidate_set = set(normalize_list(candidate_items))
    job_set = set(normalize_list(job_items))

    if not job_set:
        return 70

    if not candidate_set:
        return 0

    matched = candidate_set.intersection(job_set)
    return round((len(matched) / len(job_set)) * 100)


def calculate_role_score(profile_json, profile_analysis, job_analysis):
    tag_score = calculate_role_tag_score(profile_analysis, job_analysis)

    target_roles = normalize_list(profile_json.get("target_roles", []))
    target_families = normalize_list(profile_json.get("target_role_families", []))

    job_title = normalize_text(job_analysis.normalized_role_title)
    job_family = normalize_text(job_analysis.role_family)
    job_subfamily = normalize_text(job_analysis.role_subfamily)

    title_score = 0

    for role in target_roles:
        if role and role == job_title:
            title_score = max(title_score, 100)
        elif role and (role in job_title or job_title in role):
            title_score = max(title_score, 85)

    family_score = 0

    for family in target_families:
        if family and family == job_family:
            family_score = max(family_score, 70)

    profile_role_text = " ".join(target_roles)

    if "backend" in profile_role_text and "fullstack" in job_title:
        title_score = max(title_score, 75)

    if "backend" in profile_role_text and "backend" in job_title:
        title_score = max(title_score, 95)

    if "software" in profile_role_text and job_family in ["engineering", "it", "devops", "qa_testing"]:
        family_score = max(family_score, 60)

    if title_score == 0 and job_subfamily:
        for role in target_roles:
            if any(part in job_subfamily for part in role.split()):
                title_score = max(title_score, 50)

    return round(
        tag_score * 0.60
        + title_score * 0.30
        + family_score * 0.10
    )


def calculate_role_tag_score(profile_analysis, job_analysis):
    profile_tags = set(
        normalize_list(
            safe_json_loads(profile_analysis.target_role_tags_json, [])
        )
    )

    job_tags = set(
        normalize_list(
            safe_json_loads(job_analysis.role_tags_json, [])
        )
    )

    if not profile_tags or not job_tags:
        return 0

    matched_tags = profile_tags.intersection(job_tags)

    if not matched_tags:
        return 0

    return 100

SKILL_ALIASES = {
    "rest api": "rest apis",
    "rest": "rest apis",
    "api": "rest apis",
    "apis": "rest apis",

    "postgres": "postgresql",
    "sql": "database",
    "sqlite": "database",
    "postgresql": "database",

    "backend development": "backend",
    "backend developer": "backend",

    "openai": "openai api",
    "ai integration": "ai integrations",
    "ai integrations": "ai integrations",

    "js": "javascript",
}


def normalize_skill(value: str) -> str:
    value = normalize_text(value)

    value = value.replace("-", " ")
    value = value.replace("_", " ")
    value = value.replace("/", " ")
    value = " ".join(value.split())

    return SKILL_ALIASES.get(value, value)


def normalize_skill_list(values):
    if not values:
        return []

    return [
        normalize_skill(v)
        for v in values
        if isinstance(v, str) and v.strip()
    ]


def fuzzy_skill_match(candidate_skill, job_skill):
    candidate_skill = normalize_skill(candidate_skill)
    job_skill = normalize_skill(job_skill)

    if candidate_skill == job_skill:
        return True

    if candidate_skill in job_skill or job_skill in candidate_skill:
        return True

    candidate_parts = set(candidate_skill.split())
    job_parts = set(job_skill.split())

    if not candidate_parts or not job_parts:
        return False

    overlap = candidate_parts.intersection(job_parts)

    return len(overlap) >= 2


def skill_match_score(candidate_skills, job_skills):
    candidate_skills = normalize_skill_list(candidate_skills)
    job_skills = normalize_skill_list(job_skills)

    if not job_skills:
        return 70

    if not candidate_skills:
        return 0

    matched_count = 0

    for job_skill in job_skills:
        if any(
            fuzzy_skill_match(candidate_skill, job_skill)
            for candidate_skill in candidate_skills
        ):
            matched_count += 1

    return round((matched_count / len(job_skills)) * 100)


def calculate_skills_score(profile_analysis, job_analysis):
    strong_skills = safe_json_loads(profile_analysis.strong_skills_json, [])
    moderate_skills = safe_json_loads(profile_analysis.moderate_skills_json, [])
    weak_skills = safe_json_loads(profile_analysis.weak_or_basic_skills_json, [])
    tools = safe_json_loads(profile_analysis.tools_json, [])

    required_skills = safe_json_loads(job_analysis.required_skills_json, [])
    preferred_skills = safe_json_loads(job_analysis.preferred_skills_json, [])

    candidate_strong = strong_skills + tools
    candidate_all = strong_skills + moderate_skills + weak_skills + tools

    required_score = skill_match_score(candidate_all, required_skills)
    preferred_score = skill_match_score(candidate_all, preferred_skills)
    strong_required_score = skill_match_score(candidate_strong, required_skills)

    return round(
        required_score * 0.65
        + preferred_score * 0.20
        + strong_required_score * 0.15
    )


def calculate_seniority_score(profile_analysis, job_analysis):
    profile_level = normalize_text(profile_analysis.seniority_level)
    job_level = normalize_text(job_analysis.seniority_level)

    levels = {
        "intern": 0,
        "junior": 1,
        "mid": 2,
        "senior": 3,
        "lead": 4,
        "executive": 5
    }

    if not job_level or job_level == "unknown":
        return 70

    if not profile_level or profile_level == "unknown":
        return 50

    p = levels.get(profile_level)
    j = levels.get(job_level)

    if p is None or j is None:
        return 50

    if p == j:
        return 100

    if p > j:
        return 85

    diff = j - p

    if diff == 1:
        return 55

    if diff == 2:
        return 25

    return 5


def calculate_language_score(profile_analysis, job_analysis):
    profile_languages = safe_json_loads(profile_analysis.languages_json, [])
    job_languages = safe_json_loads(job_analysis.language_requirements_json, [])

    normalized_job_langs = normalize_list(job_languages)

    if not normalized_job_langs or "unknown" in normalized_job_langs:
        return 80

    candidate_lang_names = []

    for lang in profile_languages:
        if isinstance(lang, dict):
            candidate_lang_names.append(normalize_text(lang.get("language")))
        elif isinstance(lang, str):
            candidate_lang_names.append(normalize_text(lang))

    candidate_langs = set(candidate_lang_names)

    required_langs = set([
        lang for lang in normalized_job_langs
        if lang != "unknown"
    ])

    if not required_langs:
        return 80

    matched = candidate_langs.intersection(required_langs)

    if len(matched) == len(required_langs):
        return 100

    if matched:
        return 60

    return 20


def calculate_visa_score(profile_analysis, job_analysis):
    needs_sponsorship = profile_analysis.visa_sponsorship_needed
    visa = normalize_text(job_analysis.visa_sponsorship)

    if needs_sponsorship is not True:
        return 100

    if visa == "yes":
        return 100

    if visa == "unknown":
        return 55

    if visa == "no":
        return 10

    return 50


def calculate_work_type_score(profile_json, job_analysis):
    preferred = normalize_text(profile_json.get("preferred_work_type", ""))
    job_work_type = normalize_text(job_analysis.work_type)

    if not preferred or preferred == "unknown" or preferred == "any":
        return 80

    if not job_work_type or job_work_type == "unknown":
        return 60

    if preferred == job_work_type:
        return 100

    if preferred == "remote" and job_work_type == "hybrid":
        return 70

    if preferred == "hybrid" and job_work_type in ["remote", "onsite"]:
        return 70

    return 40


def calculate_location_score(profile_json, job):
    target_location = normalize_text(profile_json.get("target_location", ""))
    job_location = normalize_text(job.location)

    if not target_location:
        return 70

    if not job_location:
        return 60

    if target_location in job_location or job_location in target_location:
        return 100

    if "germany" in target_location and any(x in job_location for x in ["germany", "berlin", "munich", "hamburg", "köln", "cologne"]):
        return 90

    if "remote" in job_location:
        return 85

    return 45


def calculate_dealbreaker_warnings(profile_analysis, job_analysis):
    warnings = []

    dealbreakers = safe_json_loads(job_analysis.dealbreakers_json, [])
    dealbreakers_text = " ".join(normalize_list(dealbreakers))

    if "german" in dealbreakers_text:
        warnings.append("German language appears to be a hard requirement.")

    if "work authorization" in dealbreakers_text or "eu work authorization" in dealbreakers_text:
        warnings.append("Work authorization appears to be a hard requirement.")

    if "student" in dealbreakers_text or "university enrollment" in dealbreakers_text:
        warnings.append("Student enrollment appears to be required.")

    return warnings


def recommendation_from_score(score):
    if score >= 85:
        return "strong_apply"
    if score >= 70:
        return "apply"
    if score >= 50:
        return "maybe"
    return "weak_match"


def calculate_backend_match(profile, profile_analysis, job, job_analysis):
    profile_json = safe_json_loads(profile_analysis.analysis_json, {})

    role_score = calculate_role_score(profile_json, profile_analysis, job_analysis)
    skills_score = calculate_skills_score(profile_analysis, job_analysis)
    seniority_score = calculate_seniority_score(profile_analysis, job_analysis)
    experience_score = seniority_score
    language_score = calculate_language_score(profile_analysis, job_analysis)
    visa_score = calculate_visa_score(profile_analysis, job_analysis)
    work_type_score = calculate_work_type_score(profile_json, job_analysis)
    location_score = calculate_location_score(profile_json, job)

    base_score = round(
        role_score * 0.30
        + skills_score * 0.45
        + experience_score * 0.10
        + seniority_score * 0.15
    )

    practical_score = round(
        location_score * 0.25
        + work_type_score * 0.20
        + language_score * 0.25
        + visa_score * 0.30
    )

    overall_score = round(
        base_score * 0.70
        + practical_score * 0.30
    )

    warnings = calculate_dealbreaker_warnings(profile_analysis, job_analysis)

    if visa_score <= 20:
        overall_score = min(overall_score, 45)

    if language_score <= 25:
        overall_score = min(overall_score, 55)

    strengths = []
    weaknesses = []

    if role_score >= 75:
        strengths.append("Role is close to the candidate target role.")
    else:
        weaknesses.append("Role is not very close to the candidate target role.")

    if skills_score >= 70:
        strengths.append("Candidate skills match many job requirements.")
    else:
        weaknesses.append("Required skills do not strongly overlap with the candidate profile.")

    if visa_score <= 30:
        weaknesses.append("Visa or work authorization may be a problem.")

    if language_score <= 40:
        weaknesses.append("Language requirements may be a problem.")

    return {
        "role_fit_score": role_score,
        "skills_fit_score": skills_score,
        "experience_fit_score": experience_score,
        "seniority_fit_score": seniority_score,
        "location_fit_score": location_score,
        "work_type_fit_score": work_type_score,
        "language_fit_score": language_score,
        "visa_fit_score": visa_score,
        "base_match_score": base_score,
        "practical_match_score": practical_score,
        "overall_score": overall_score,
        "recommendation": recommendation_from_score(overall_score),
        "summary": f"Backend rule-based score is {overall_score}.",
        "strengths": strengths,
        "weaknesses": weaknesses,
        "dealbreaker_warnings": warnings
    }


@router.post("/{user_id}/profiles/{profile_id}/jobs/{job_id}/match")
def match_profile_with_job(
    user_id: int,
    profile_id: int,
    job_id: int,
    force_rematch: bool = Query(default=False),
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

    job = db.query(models.Job).filter(
        models.Job.job_id == job_id
    ).first()

    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_JOB_NOT_FOUND",
                "message": f"Job with id {job_id} was not found."
            }
        )

    profile_analysis = db.query(models.ProfileAnalysis).filter(
        models.ProfileAnalysis.profile_id == profile_id,
        models.ProfileAnalysis.analysis_status == "completed",
        models.ProfileAnalysis.is_current == True
    ).order_by(
        models.ProfileAnalysis.analysis_id.desc()
    ).first()

    if not profile_analysis:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_PROFILE_ANALYSIS_MISSING",
                "message": "Profile must be analyzed before matching.",
                "profile_id": profile_id
            }
        )

    job_analysis = db.query(models.JobAnalysis).filter(
        models.JobAnalysis.job_id == job_id,
        models.JobAnalysis.analysis_status == "completed",
        models.JobAnalysis.analysis_model == REQUIRED_JOB_ANALYSIS_MODEL,
        models.JobAnalysis.analysis_prompt_version == REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
        models.JobAnalysis.is_current == True,
        models.JobAnalysis.role_tags_json.isnot(None),
        models.JobAnalysis.role_tags_json != "[]"
    ).order_by(
        models.JobAnalysis.analysis_id.desc()
    ).first()

    if not job_analysis:
        latest_analysis = db.query(models.JobAnalysis).filter(
            models.JobAnalysis.job_id == job_id,
            models.JobAnalysis.analysis_status == "completed"
        ).order_by(
            models.JobAnalysis.analysis_id.desc()
        ).first()

        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_JOB_ANALYSIS_OUTDATED_OR_MISSING",
                "message": (
                    "Job must be analyzed with the latest job analysis version "
                    "before matching."
                ),
                "job_id": job_id,
                "required_analysis_model": REQUIRED_JOB_ANALYSIS_MODEL,
                "required_analysis_prompt_version": REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
                "latest_existing_analysis_id": (
                    latest_analysis.analysis_id if latest_analysis else None
                ),
                "latest_existing_analysis_model": (
                    latest_analysis.analysis_model if latest_analysis else None
                ),
                "latest_existing_analysis_prompt_version": (
                    latest_analysis.analysis_prompt_version if latest_analysis else None
                ),
                "hint": (
                    f"Run POST /jobs/{job_id}/analyze?force_reanalyze=true "
                    "and then try matching again."
                )
            }
        )

    existing_match = db.query(models.JobMatch).filter(
        models.JobMatch.profile_id == profile_id,
        models.JobMatch.job_id == job_id,
        models.JobMatch.profile_analysis_id == profile_analysis.analysis_id,
        models.JobMatch.job_analysis_id == job_analysis.analysis_id,
        models.JobMatch.match_model == MATCH_MODEL,
        models.JobMatch.match_prompt_version == MATCH_PROMPT_VERSION,
        models.JobMatch.match_status == "completed",
        models.JobMatch.is_current == True
    ).first()

    if existing_match and not force_rematch:
        return {
            "status": "cached",
            "message": "This profile and job already have a current match for this model and prompt version.",
            "match_id": existing_match.match_id,
            "profile_id": profile_id,
            "job_id": job_id,
            "overall_score": existing_match.overall_score,
            "recommendation": existing_match.recommendation,
            "match": json.loads(existing_match.match_json)
            if existing_match.match_json else None
        }


    now = datetime.now(timezone.utc)

    try:
        match_result = calculate_backend_match(
            profile=profile,
            profile_analysis=profile_analysis,
            job=job,
            job_analysis=job_analysis
        )

        db.query(models.JobMatch).filter(
            models.JobMatch.profile_id == profile_id,
            models.JobMatch.job_id == job_id,
            models.JobMatch.is_current == True
        ).update({
            "is_current": False
        })

        new_match = models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis.analysis_id,
            job_analysis_id=job_analysis.analysis_id,

            match_status="completed",
            match_json=json.dumps(match_result, ensure_ascii=False),

            role_fit_score=match_result.get("role_fit_score"),
            skills_fit_score=match_result.get("skills_fit_score"),
            experience_fit_score=match_result.get("experience_fit_score"),
            seniority_fit_score=match_result.get("seniority_fit_score"),
            location_fit_score=match_result.get("location_fit_score"),
            work_type_fit_score=match_result.get("work_type_fit_score"),
            language_fit_score=match_result.get("language_fit_score"),
            visa_fit_score=match_result.get("visa_fit_score"),

            base_match_score=match_result.get("base_match_score"),
            practical_match_score=match_result.get("practical_match_score"),
            overall_score=match_result.get("overall_score"),

            recommendation=match_result.get("recommendation"),
            summary=match_result.get("summary"),

            strengths_json=json.dumps(
                match_result.get("strengths", []),
                ensure_ascii=False
            ),
            weaknesses_json=json.dumps(
                match_result.get("weaknesses", []),
                ensure_ascii=False
            ),
            dealbreaker_warnings_json=json.dumps(
                match_result.get("dealbreaker_warnings", []),
                ensure_ascii=False
            ),

            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_PROMPT_VERSION,
            matched_at=now,
            match_error=None,
            is_current=True,
            created_at=now
        )

        db.add(new_match)
        db.commit()
        db.refresh(new_match)

        return {
            "status": "created",
            "match_id": new_match.match_id,
            "profile_id": profile_id,
            "job_id": job_id,
            "profile_analysis_id": profile_analysis.analysis_id,
            "job_analysis_id": job_analysis.analysis_id,
            "overall_score": new_match.overall_score,
            "recommendation": new_match.recommendation,
            "match": match_result
        }

    except Exception as e:
        db.rollback()

        failed_match = models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis.analysis_id,
            job_analysis_id=job_analysis.analysis_id,
            match_status="failed",
            match_json=None,
            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_PROMPT_VERSION,
            matched_at=now,
            match_error=str(e),
            is_current=False,
            created_at=now
        )

        db.add(failed_match)
        db.commit()

        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ERR_BACKEND_MATCH_FAILED",
                "message": "Backend job match failed.",
                "profile_id": profile_id,
                "job_id": job_id,
                "error": str(e)
            }
        )


@router.post("/{user_id}/profiles/{profile_id}/jobs/match-analyzed")
def match_profile_with_analyzed_jobs(
    user_id: int,
    profile_id: int,
    limit: int = Query(default=20, ge=1, le=MAX_BATCH_MATCH_JOBS),
    offset: int = Query(default=0, ge=0),
    force_rematch: bool = Query(default=False),
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

    profile_analysis = db.query(models.ProfileAnalysis).filter(
        models.ProfileAnalysis.profile_id == profile_id,
        models.ProfileAnalysis.analysis_status == "completed",
        models.ProfileAnalysis.is_current == True
    ).order_by(
        models.ProfileAnalysis.analysis_id.desc()
    ).first()

    if not profile_analysis:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_PROFILE_ANALYSIS_MISSING",
                "message": "Profile must be analyzed before matching.",
                "profile_id": profile_id
            }
        )

    analyzed_jobs = (
        db.query(models.Job)
        .join(
            models.JobAnalysis,
            models.Job.job_id == models.JobAnalysis.job_id
        )
        .filter(
            models.JobAnalysis.analysis_status == "completed",
            models.JobAnalysis.analysis_model == REQUIRED_JOB_ANALYSIS_MODEL,
            models.JobAnalysis.analysis_prompt_version == REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
            models.JobAnalysis.is_current == True,
            models.JobAnalysis.role_tags_json.isnot(None),
            models.JobAnalysis.role_tags_json != "[]"
        )
        .order_by(models.Job.job_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not analyzed_jobs:
        return {
            "status": "no_analyzed_jobs_found",
            "user_id": user_id,
            "profile_id": profile_id,
            "requested_limit": limit,
            "offset": offset,
            "matched_count": 0,
            "failed_count": 0,
            "results": []
        }

    results = []

    for job in analyzed_jobs:
        try:
            result = match_profile_with_job(
                user_id=user_id,
                profile_id=profile_id,
                job_id=job.job_id,
                force_rematch=force_rematch,
                db=db
            )

            results.append({
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "location": job.location,
                "url": job.url,
                "status": result.get("status"),
                "match_id": result.get("match_id"),
                "overall_score": result.get("overall_score"),
                "recommendation": result.get("recommendation"),
                "summary": (
                    result.get("match", {}).get("summary")
                    if result.get("match") else None
                )
            })

        except HTTPException as e:
            db.rollback()

            results.append({
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "status": "failed",
                "error": e.detail
            })

        except Exception as e:
            db.rollback()

            results.append({
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "status": "failed",
                "error": str(e)
            })

    matched_count = len([
        result for result in results
        if result.get("status") in ["created", "cached"]
    ])

    failed_count = len([
        result for result in results
        if result.get("status") == "failed"
    ])

    sorted_results = sorted(
        results,
        key=lambda item: item.get("overall_score") or 0,
        reverse=True
    )

    return {
        "status": "completed",
        "user_id": user_id,
        "profile_id": profile_id,
        "requested_limit": limit,
        "offset": offset,
        "selected_job_count": len(analyzed_jobs),
        "matched_count": matched_count,
        "failed_count": failed_count,
        "max_allowed_limit": MAX_BATCH_MATCH_JOBS,
        "results": sorted_results
    }