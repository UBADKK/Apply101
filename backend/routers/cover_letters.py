"""Motivation letters: generate a draft (TR/EN/DE) from the user's own
profile and CV, the job and sourced company information; edit, save and
regenerate as new versions. A letter is visible to its user and, like
profiles and CVs, to admins (require_user_access); another user (or a
missing letter) gets the same 404.

The provider call goes through _create_cover_letter_response (the real
OpenAI client, or the fake one with APPLY101_ANALYSIS_PROVIDER=fake). A
draft from the fake provider is a fixed template, not a writing-quality
check of the real model.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..app import cover_letter, models, schemas
from ..app.analysis_contract import COVER_LETTER_MODEL, COVER_LETTER_PROMPT_VERSION
from ..app.auth_dependencies import get_current_user, get_owned_profile, require_user_access
from ..app.database import get_db
from ..app.job_visibility import visible_jobs_clause_for_owner
from ..app.openai_client import (
    ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL,
    create_openai_client_or_none,
    provider_spend_limit_error_code,
)


client = create_openai_client_or_none()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["Motivation letters"])


def _error(status_code: int, error_code: str, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error_code": error_code, "message": message}, headers=headers)


def _letter_not_found() -> HTTPException:
    return _error(404, "ERR_COVER_LETTER_NOT_FOUND", "Letter not found.")


def _create_cover_letter_response(prompt: str):
    """Single seam for the provider call (tests patch this or `client`)."""
    scoped_client = client.with_options(max_retries=1)
    return scoped_client.responses.create(
        model=COVER_LETTER_MODEL,
        input=prompt,
        temperature=0.4,
        text={
            "format": {
                "type": "json_schema",
                "name": "cover_letter",
                "schema": schemas.CoverLetterStructured.model_json_schema(),
                "strict": True,
            }
        },
        timeout=cover_letter.PROVIDER_TIMEOUT_SECONDS,
    )


def _version_out(version: models.CoverLetterVersion) -> dict:
    grounding = json.loads(version.grounding_json) if version.grounding_json else None
    return {
        "version_id": version.version_id,
        "version_no": version.version_no,
        "kind": version.kind,
        "body_text": version.body_text,
        "grounding": grounding,
        "model": version.model,
        "prompt_version": version.prompt_version,
        "created_at": version.created_at,
    }


def _commit_new_version(db: Session, letter: models.CoverLetter, version: models.CoverLetterVersion, base_version_id: int | None, now) -> None:
    """Adds the version and moves the letter to it only if the letter still
    points at base_version_id (one statement, so two concurrent saves
    cannot both win); otherwise 409 and nothing is written."""
    db.add(version)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _error(409, "ERR_COVER_LETTER_CHANGED", "This letter was changed in another window. Reload it before saving.")
    updated = (
        db.query(models.CoverLetter)
        .filter(
            models.CoverLetter.letter_id == letter.letter_id,
            models.CoverLetter.current_version_id == base_version_id,
        )
        .update({models.CoverLetter.current_version_id: version.version_id, models.CoverLetter.updated_at: now}, synchronize_session=False)
    )
    if updated != 1:
        db.rollback()
        raise _error(409, "ERR_COVER_LETTER_CHANGED", "This letter was changed in another window. Reload it before saving.")
    db.commit()
    db.refresh(letter)


def _letter_out(db: Session, letter: models.CoverLetter, with_versions: bool) -> dict:
    versions = (
        db.query(models.CoverLetterVersion)
        .filter(models.CoverLetterVersion.letter_id == letter.letter_id)
        .order_by(models.CoverLetterVersion.version_no.desc())
        .all()
    )
    current = next((v for v in versions if v.version_id == letter.current_version_id), versions[0] if versions else None)
    out = {
        "letter_id": letter.letter_id,
        "job_id": letter.job_id,
        "profile_id": letter.profile_id,
        "language": letter.language,
        "job_title": letter.job_title,
        "company_name": letter.company_name,
        "created_at": letter.created_at,
        "updated_at": letter.updated_at,
        "current_version": (
            _version_out(current) if with_versions else {**_version_out(current), "grounding": None}
        ) if current else None,
        "version_count": len(versions),
    }
    if with_versions:
        out["versions"] = [_version_out(version) for version in versions]
    return out


def _owned_letter(db: Session, user_id: int, letter_id: int) -> models.CoverLetter:
    letter = (
        db.query(models.CoverLetter)
        .filter(models.CoverLetter.letter_id == letter_id, models.CoverLetter.user_id == user_id)
        .first()
    )
    if letter is None:
        raise _letter_not_found()
    return letter


def _generate(db: Session, *, user_id: int, profile: models.CandidateProfile, job: models.Job, language: str) -> tuple[str, dict]:
    """Runs one guarded, quota-counted generation; returns (text, grounding)."""
    if client is None:
        raise _error(503, "ERR_COVER_LETTER_PROVIDER_NOT_CONFIGURED", ANALYSIS_SERVICE_NOT_CONFIGURED_DETAIL)
    grounding = cover_letter.build_grounding(db, user_id=user_id, profile=profile, job=job)
    prompt = cover_letter.build_prompt(language, grounding)
    try:
        token = cover_letter.acquire_generation(db, user_id)
    except cover_letter.LetterBusy:
        raise _error(409, "ERR_COVER_LETTER_IN_PROGRESS", "A letter is already being written. Wait for it to finish.")
    except cover_letter.LetterQuotaExceeded as exc:
        raise _error(429, "ERR_COVER_LETTER_QUOTA", "You reached the daily limit of letter drafts.", {"Retry-After": str(exc.retry_after_seconds)})
    try:
        try:
            response = _create_cover_letter_response(prompt)
        except Exception as exc:  # noqa: BLE001 - provider errors are mapped, never echoed
            if provider_spend_limit_error_code(exc) is not None:
                raise _error(503, "ERR_COVER_LETTER_PROVIDER_UNAVAILABLE", "Letter drafting is temporarily unavailable. Please try again later.")
            logger.warning("cover letter provider call failed: %s", type(exc).__name__)
            raise _error(502, "ERR_COVER_LETTER_FAILED", "The draft could not be written. Please try again.")
        try:
            output = cover_letter.parse_output(response.output_text, grounding)
        except (ValueError, TypeError):
            raise _error(502, "ERR_COVER_LETTER_FAILED", "The draft could not be written. Please try again.")
    finally:
        cover_letter.release_generation(db, user_id, token)
    stored = grounding.as_dict()
    stored["used_fact_ids"] = output.used_fact_ids
    stored["missing_information"] = output.missing_information
    return output.letter_text, stored


@router.get("/{user_id}/cover-letters")
def list_letters(
    user_id: int,
    current_user: models.User = Depends(require_user_access),
    db: Session = Depends(get_db),
):
    letters = (
        db.query(models.CoverLetter)
        .filter(models.CoverLetter.user_id == user_id)
        .order_by(models.CoverLetter.updated_at.desc(), models.CoverLetter.letter_id.desc())
        .limit(200)
        .all()
    )
    return [_letter_out(db, letter, with_versions=False) for letter in letters]


@router.post("/{user_id}/profiles/{profile_id}/jobs/{job_id}/cover-letters", status_code=201)
def create_letter(
    user_id: int,
    profile_id: int,
    job_id: int,
    payload: schemas.CoverLetterCreate,
    profile: models.CandidateProfile = Depends(get_owned_profile),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # The job must be visible to the profile owner (catalog or their own).
    job = (
        db.query(models.Job)
        .filter(models.Job.job_id == job_id, visible_jobs_clause_for_owner(user_id))
        .first()
    )
    if job is None:
        raise _error(404, "ERR_JOB_NOT_FOUND", "Job not found.")
    existing = (
        db.query(models.CoverLetter)
        .filter(models.CoverLetter.user_id == user_id, models.CoverLetter.job_id == job_id, models.CoverLetter.language == payload.language)
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail={
            "error_code": "ERR_COVER_LETTER_EXISTS",
            "message": "You already have a letter for this job in this language. Open it to edit or regenerate.",
            "letter_id": existing.letter_id,
        })
    job_title, job_company = job.title, job.company_name
    text, grounding = _generate(db, user_id=user_id, profile=profile, job=job, language=payload.language)
    if db.query(models.CoverLetter.letter_id).filter(
        models.CoverLetter.user_id == user_id, models.CoverLetter.job_id == job_id, models.CoverLetter.language == payload.language
    ).first():
        # Another request created it while this draft was written.
        raise _error(409, "ERR_COVER_LETTER_EXISTS", "You already have a letter for this job in this language. Open it to edit or regenerate.")
    now = cover_letter.utc_now()
    letter = models.CoverLetter(
        user_id=user_id, profile_id=profile_id, job_id=job_id, language=payload.language,
        job_title=job_title, company_name=job_company, created_at=now, updated_at=now,
    )
    db.add(letter)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _error(409, "ERR_COVER_LETTER_EXISTS", "You already have a letter for this job in this language. Open it to edit or regenerate.")
    version = models.CoverLetterVersion(
        letter_id=letter.letter_id, version_no=1, kind="generated", body_text=text,
        grounding_json=json.dumps(grounding, ensure_ascii=False), model=COVER_LETTER_MODEL,
        prompt_version=COVER_LETTER_PROMPT_VERSION, user_id=user_id, created_at=now,
    )
    db.add(version)
    db.flush()
    letter.current_version_id = version.version_id
    db.commit()
    db.refresh(letter)
    return _letter_out(db, letter, with_versions=True)


@router.get("/{user_id}/cover-letters/{letter_id}")
def get_letter(
    user_id: int,
    letter_id: int,
    current_user: models.User = Depends(require_user_access),
    db: Session = Depends(get_db),
):
    return _letter_out(db, _owned_letter(db, user_id, letter_id), with_versions=True)


@router.post("/{user_id}/cover-letters/{letter_id}/versions")
def save_letter(
    user_id: int,
    letter_id: int,
    payload: schemas.CoverLetterEdit,
    current_user: models.User = Depends(require_user_access),
    db: Session = Depends(get_db),
):
    """Saves the user's text as a new "edited" version; earlier versions
    stay. Refused (409) when another version was saved meanwhile."""
    letter = _owned_letter(db, user_id, letter_id)
    if payload.base_version_id != letter.current_version_id:
        raise _error(409, "ERR_COVER_LETTER_CHANGED", "This letter was changed in another window. Reload it before saving.")
    now = cover_letter.utc_now()
    version = models.CoverLetterVersion(
        letter_id=letter.letter_id, version_no=cover_letter.next_version_no(db, letter.letter_id), kind="edited",
        body_text=payload.body_text, user_id=user_id, created_at=now,
    )
    _commit_new_version(db, letter, version, payload.base_version_id, now)
    return _letter_out(db, letter, with_versions=True)


@router.post("/{user_id}/cover-letters/{letter_id}/regenerate")
def regenerate_letter(
    user_id: int,
    letter_id: int,
    current_user: models.User = Depends(require_user_access),
    db: Session = Depends(get_db),
):
    """A new generated version from the current profile, CV and job. Every
    earlier version, edited ones included, is kept unchanged."""
    letter = _owned_letter(db, user_id, letter_id)
    profile = (
        db.query(models.CandidateProfile)
        .filter(models.CandidateProfile.profile_id == letter.profile_id, models.CandidateProfile.user_id == user_id)
        .first()
    )
    job = (
        db.query(models.Job)
        .filter(models.Job.job_id == letter.job_id, visible_jobs_clause_for_owner(user_id))
        .first()
        if letter.job_id is not None
        else None
    )
    if profile is None or job is None:
        raise _error(409, "ERR_COVER_LETTER_SOURCE_GONE", "The profile or the job of this letter no longer exists; it can be edited but not regenerated.")
    letter_id_value = letter.letter_id
    text, grounding = _generate(db, user_id=user_id, profile=profile, job=job, language=letter.language)
    letter = _owned_letter(db, user_id, letter_id_value)
    now = cover_letter.utc_now()
    version = models.CoverLetterVersion(
        letter_id=letter.letter_id, version_no=cover_letter.next_version_no(db, letter.letter_id), kind="generated",
        body_text=text, grounding_json=json.dumps(grounding, ensure_ascii=False), model=COVER_LETTER_MODEL,
        prompt_version=COVER_LETTER_PROMPT_VERSION, user_id=user_id, created_at=now,
    )
    # Whatever version is current now (an edit saved meanwhile stays a
    # version of its own and is not overwritten).
    _commit_new_version(db, letter, version, letter.current_version_id, now)
    return _letter_out(db, letter, with_versions=True)


@router.delete("/{user_id}/cover-letters/{letter_id}", status_code=204)
def delete_letter(
    user_id: int,
    letter_id: int,
    current_user: models.User = Depends(require_user_access),
    db: Session = Depends(get_db),
):
    letter = _owned_letter(db, user_id, letter_id)
    db.query(models.CoverLetterVersion).filter(models.CoverLetterVersion.letter_id == letter.letter_id).delete(synchronize_session=False)
    db.delete(letter)
    db.commit()
