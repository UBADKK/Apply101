"""Shared job visibility rules.

- Admins see every job.
- A normal user sees ownerless jobs (created_by_user_id IS NULL) and jobs
  they own (created_by_user_id == their user_id).

A job that exists but is not visible to the caller must be
indistinguishable from a job that does not exist (same 404 status and
body), so callers never learn that another user's job ID exists.
"""

from fastapi import HTTPException, status
from sqlalchemy import or_, true
from sqlalchemy.orm import Session

from . import models


JOB_NOT_FOUND_DETAIL = "Job not found."


def visible_jobs_clause(current_user: models.User):
    """SQL filter clause for jobs visible to current_user. Apply it in the
    query itself (before offset/limit/count), never by post-filtering."""
    if current_user.is_admin:
        return true()

    return or_(
        models.Job.created_by_user_id.is_(None),
        models.Job.created_by_user_id == current_user.user_id,
    )


def get_visible_job_or_404(
    db: Session,
    job_id: int,
    current_user: models.User,
) -> models.Job:
    """Returns the job when it exists and is visible to current_user;
    otherwise raises the same 404 a nonexistent job_id gets."""
    job = (
        db.query(models.Job)
        .filter(
            models.Job.job_id == job_id,
            visible_jobs_clause(current_user),
        )
        .first()
    )

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=JOB_NOT_FOUND_DETAIL,
        )

    return job
