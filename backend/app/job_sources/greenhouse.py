"""Greenhouse Job Board API (public, no key for GET).

GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
returns every published job of the board at once ({"jobs": [...],
"meta": {"total": N}}); there is no pagination. `content` is HTML with
entities escaped. GET /v1/boards/{board_token} returns the board name and
its intro text (`content`).
"""
from __future__ import annotations

from urllib.parse import quote

from .base import (
    BoardFetch,
    HttpClient,
    NormalizedPosting,
    SourceError,
    clean_optional,
    epoch_from_iso,
    is_public_http_url,
    unescaped_html_to_text,
)

SOURCE = "greenhouse"
API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def fetch_board(http: HttpClient, board_token: str, region: str = "global", max_pages: int = 1) -> BoardFetch:
    result = BoardFetch()
    token = quote(board_token, safe="")
    try:
        data = http.get_json(f"{API_BASE}/{token}/jobs", params={"content": "true"})
    except SourceError as exc:
        result.stopped_reason = exc.code
        result.error_message = str(exc)
        return result
    result.pages_checked = 1

    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        result.stopped_reason = "UNEXPECTED_RESPONSE"
        result.error_message = "The response has no jobs list."
        return result

    for job in jobs:
        posting = _posting(board_token, job)
        if isinstance(posting, str):
            result.skipped.append({"reason": posting, "id": job.get("id") if isinstance(job, dict) else None})
        else:
            result.postings.append(posting)

    # The board intro is optional; a failure here never fails the fetch.
    try:
        board = http.get_json(f"{API_BASE}/{token}")
        if isinstance(board, dict):
            about = unescaped_html_to_text(board.get("content"))[:4000]
            if about:
                result.company_about = about
                result.company_about_url = f"{API_BASE}/{token}"
    except SourceError:
        pass

    result.complete = True
    return result


def _posting(board_token: str, job) -> NormalizedPosting | str:
    if not isinstance(job, dict):
        return "INVALID_ITEM"
    job_id = job.get("id")
    title = clean_optional(job.get("title"))
    url = job.get("absolute_url")
    if job_id is None or not title:
        return "MISSING_ID_OR_TITLE"
    if not is_public_http_url(url):
        return "MISSING_URL"
    description = unescaped_html_to_text(job.get("content"))
    if not description:
        return "MISSING_DESCRIPTION"
    location = job.get("location")
    location_name = clean_optional(location.get("name")) if isinstance(location, dict) else None
    return NormalizedPosting(
        source=SOURCE,
        source_key=f"{board_token.lower()}:{job_id}",
        title=title,
        url=url,
        description_text=description,
        company_name=clean_optional(job.get("company_name")),
        location=location_name,
        # Greenhouse has no structured work mode or country field.
        work_mode=None,
        country_code=None,
        source_created_at=epoch_from_iso(job.get("first_published")),
        source_updated_at=epoch_from_iso(job.get("updated_at")),
    )
