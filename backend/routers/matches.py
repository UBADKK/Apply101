import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from openai import OpenAI

from ..app.database import get_db
from ..app import models


router = APIRouter(
    prefix="/users",
    tags=["Job Matching"]
)

client = OpenAI()


def build_profile_text(profile: models.CandidateProfile) -> str:
    return f"""
Self description:
{profile.self_description or ""}

Target role:
{profile.target_role or ""}

Secondary target role:
{profile.secondary_target_role or ""}

Target location:
{profile.target_location or ""}

Preferred work type:
{profile.preferred_work_type or ""}

Preferred technologies:
{profile.preferred_technologies or ""}

Extra preferences:
{profile.extra_preferences or ""}

CV text:
{(profile.cv_text or "")[:8000]}
""".strip()


def build_job_text(job: models.Job) -> str:
    return f"""
Job title:
{job.title or ""}

Company name:
{job.company_name or ""}

Location:
{job.location or ""}

Job description:
{(job.description_text or "")[:8000]}
""".strip()


def get_ai_match(profile_text: str, job_text: str):
    prompt = f"""
Analyze how well this candidate profile matches this job posting.

Candidate Profile:
{profile_text}

Job Posting:
{job_text}

Return ONLY valid JSON in this exact structure:
{{
  "score": 0,
  "summary": "short summary",
  "strengths": ["point 1", "point 2"],
  "weaknesses": ["point 1", "point 2"],
  "recommendation": "apply" 
}}

Rules:
- score must be an integer between 0 and 100
- summary should be short and clear
- strengths and weaknesses should each contain 2 to 5 short bullet points
- recommendation must be one of:
  "strong_apply", "apply", "maybe", "weak_match"
- do not return anything except JSON
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )

    raw_text = response.output_text.strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail=f"AI response was not valid JSON: {raw_text}"
        )

    return result


@router.post("/{user_id}/profiles/{profile_id}/jobs/{job_id}/match")
def match_profile_with_job(
    user_id: int,
    profile_id: int,
    job_id: int,
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.user_id == user_id
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id
    ).first()

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    job = db.query(models.Job).filter(
        models.Job.job_id == job_id
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    profile_text = build_profile_text(profile)
    job_text = build_job_text(job)

    match_result = get_ai_match(profile_text, job_text)

    return {
        "user_id": user_id,
        "profile_id": profile_id,
        "job_id": job_id,
        "job_title": job.title,
        "company_name": job.company_name,
        "match_result": match_result
    }