"""Arbeitnow job board API (public, no key).

GET https://www.arbeitnow.com/api/job-board-api?page=N returns
{"data": [...], "links": {...}, "meta": {...}}; pages are read until an
empty page. Arbeitnow has no per-company boards, so board_token is ignored.
"""
from __future__ import annotations

from .base import (
    BoardFetch,
    HttpClient,
    NormalizedPosting,
    SourceError,
    clean_optional,
    html_to_text,
    is_public_http_url,
)

SOURCE = "arbeitnow"
API_URL = "https://www.arbeitnow.com/api/job-board-api"


def fetch_board(http: HttpClient, board_token: str = "", region: str = "global", max_pages: int = 10) -> BoardFetch:
    result = BoardFetch()
    for page in range(1, max_pages + 1):
        try:
            data = http.get_json(API_URL, params={"page": page})
        except SourceError as exc:
            result.stopped_reason = exc.code
            result.error_message = str(exc)
            return result
        result.pages_checked += 1
        jobs = data.get("data") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            result.stopped_reason = "UNEXPECTED_RESPONSE"
            result.error_message = "The response has no data list."
            return result
        if not jobs:
            result.complete = True
            return result
        for job in jobs:
            posting = _posting(job)
            if isinstance(posting, str):
                result.skipped.append({"reason": posting, "id": job.get("slug") if isinstance(job, dict) else None})
            else:
                result.postings.append(posting)
    result.stopped_reason = "MAX_PAGES_REACHED"
    return result


def _posting(job) -> NormalizedPosting | str:
    if not isinstance(job, dict):
        return "INVALID_ITEM"
    url = job.get("url")
    title = clean_optional(job.get("title"))
    if not is_public_http_url(url):
        return "MISSING_URL"
    if not title:
        return "MISSING_ID_OR_TITLE"
    description = html_to_text(job.get("description"))
    if not description:
        return "MISSING_DESCRIPTION"
    slug = clean_optional(job.get("slug"), 500)
    created = job.get("created_at")
    return NormalizedPosting(
        source=SOURCE,
        # Existing Arbeitnow jobs were keyed by URL; the URL stays the key.
        source_key=slug or url,
        title=title,
        url=url,
        description_text=description,
        company_name=clean_optional(job.get("company_name")),
        location=clean_optional(job.get("location")),
        work_mode="remote" if job.get("remote") is True else None,
        country_code=None,
        source_created_at=created if isinstance(created, int) and not isinstance(created, bool) else None,
        source_updated_at=None,
    )
