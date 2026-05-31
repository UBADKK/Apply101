import json

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from openai import OpenAI

from ..app.database import get_db
from ..app import models


router = APIRouter(
    prefix="/users",
    tags=["Job Matching"]
)

client = OpenAI()


MATCH_MODEL = "gpt-4.1-mini"
MATCH_PROMPT_VERSION = "job_match_v1"
MAX_BATCH_MATCH_JOBS = 50

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
        models.JobAnalysis.is_current == True
    ).order_by(
        models.JobAnalysis.analysis_id.desc()
    ).first()

    if not job_analysis:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_JOB_ANALYSIS_MISSING",
                "message": "Job must be analyzed before matching.",
                "job_id": job_id
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

    try:
        profile_analysis_json = json.loads(profile_analysis.analysis_json)
        job_analysis_json = json.loads(job_analysis.analysis_json)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ERR_ANALYSIS_JSON_INVALID",
                "message": "Profile analysis or job analysis contains invalid JSON."
            }
        )

    prompt = f"""
You are a job matching engine for a job search product.

Compare the candidate profile analysis with the job analysis.
Give explainable scores.

Candidate profile analysis:
{json.dumps(profile_analysis_json, ensure_ascii=False, indent=2)}

Job analysis:
{json.dumps(job_analysis_json, ensure_ascii=False, indent=2)}

Return ONLY valid JSON in this exact structure:
{{
  "role_fit_score": 0,
  "skills_fit_score": 0,
  "experience_fit_score": 0,
  "seniority_fit_score": 0,
  "location_fit_score": 0,
  "work_type_fit_score": 0,
  "language_fit_score": 0,
  "visa_fit_score": 0,
  "base_match_score": 0,
  "practical_match_score": 0,
  "overall_score": 0,
  "recommendation": "strong_apply|apply|maybe|weak_match|do_not_apply",
  "summary": "short explanation of the match",
  "strengths": ["strength 1", "strength 2"],
  "weaknesses": ["weakness 1", "weakness 2"],
  "dealbreaker_warnings": ["warning 1", "warning 2"]
}}

Scoring rules:
- All scores must be integers between 0 and 100.
- role_fit_score: how well the candidate target/current role matches the job role.
- skills_fit_score: how well candidate skills match required and preferred job skills.
- experience_fit_score: how well years and depth of experience match.
- seniority_fit_score: how well candidate seniority matches job seniority.
- location_fit_score: location and relocation fit.
- work_type_fit_score: remote/hybrid/onsite preference fit.
- language_fit_score: language requirement fit.
- visa_fit_score: visa sponsorship / work authorization fit.

Important:
- base_match_score should focus mostly on role, skills, experience, and seniority.
- practical_match_score should focus mostly on location, work type, language, and visa.
- overall_score should combine both.
- If there is a hard dealbreaker, overall_score should be low even if skills fit is good.
- If German is required and the candidate does not have enough German, language_fit_score must be low.
- If EU work authorization is required and the candidate needs sponsorship, visa_fit_score must be low.
- Do not invent candidate skills or job requirements.
- Use only the provided analyses.
- Return only valid JSON.
- Do not include markdown.
"""

    now = datetime.now(timezone.utc)

    try:
        response = client.responses.create(
            model=MATCH_MODEL,
            input=prompt,
            temperature=0
        )

        raw_text = response.output_text.strip()

        try:
            match_result = json.loads(raw_text)
        except json.JSONDecodeError:
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
                match_error=f"AI response was not valid JSON: {raw_text[:1000]}",
                is_current=False,
                created_at=now
            )

            db.add(failed_match)
            db.commit()

            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "ERR_AI_INVALID_JSON",
                    "message": "AI response was not valid JSON.",
                    "raw_response": raw_text
                }
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

    except HTTPException:
        raise

    except Exception as e:
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
                "error_code": "ERR_JOB_MATCH_FAILED",
                "message": "Job match failed.",
                "profile_id": profile_id,
                "job_id": job_id,
                "error": str(e)
            }
        )
    
    MAX_BATCH_MATCH_JOBS = 50


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
            models.JobAnalysis.is_current == True
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