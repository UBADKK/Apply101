"""Ashby public job posting API (no key).

GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}
returns {"jobs": [...]} with every listed posting at once; there is no
pagination. Postings with isListed == false are skipped.
"""
from __future__ import annotations

from urllib.parse import quote

from .base import (
    BoardFetch,
    HttpClient,
    NormalizedPosting,
    SourceError,
    clean_optional,
    clean_text,
    country_code_or_none,
    epoch_from_iso,
    html_to_text,
    is_public_http_url,
    MAX_DESCRIPTION_CHARS,
)

SOURCE = "ashby"
API_BASE = "https://api.ashbyhq.com/posting-api/job-board"

_WORKPLACE_TYPES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def fetch_board(http: HttpClient, board_token: str, region: str = "global", max_pages: int = 1) -> BoardFetch:
    result = BoardFetch()
    try:
        data = http.get_json(f"{API_BASE}/{quote(board_token, safe='')}", params={"includeCompensation": "false"})
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
    result.complete = True
    return result


def posting_from_item(board_token: str, job) -> NormalizedPosting | str:
    return _posting(board_token, job)


def _posting(board_token: str, job) -> NormalizedPosting | str:
    if not isinstance(job, dict):
        return "INVALID_ITEM"
    if job.get("isListed") is False:
        return "NOT_LISTED"
    job_id = job.get("id")
    title = clean_optional(job.get("title"))
    url = job.get("jobUrl")
    if not job_id or not title:
        return "MISSING_ID_OR_TITLE"
    if not is_public_http_url(url):
        return "MISSING_URL"
    plain = job.get("descriptionPlain")
    description = (clean_text(plain) if isinstance(plain, str) and plain.strip() else html_to_text(job.get("descriptionHtml")))[:MAX_DESCRIPTION_CHARS]
    if not description:
        return "MISSING_DESCRIPTION"
    workplace = job.get("workplaceType")
    work_mode = _WORKPLACE_TYPES.get(workplace.lower()) if isinstance(workplace, str) else None
    if work_mode is None and job.get("isRemote") is True:
        work_mode = "remote"
    country = None
    address = job.get("address")
    if isinstance(address, dict) and isinstance(address.get("postalAddress"), dict):
        country = country_code_or_none(address["postalAddress"].get("addressCountry"))
    return NormalizedPosting(
        source=SOURCE,
        source_key=f"{board_token.lower()}:{job_id}",
        title=title,
        url=url,
        description_text=description,
        company_name=None,
        location=clean_optional(job.get("location")),
        work_mode=work_mode,
        country_code=country,
        source_created_at=epoch_from_iso(job.get("publishedAt")),
        source_updated_at=None,
    )
