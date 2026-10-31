"""Lever Postings API (public, no key for GET).

GET https://api.lever.co/v0/postings/{site}?mode=json&skip=N&limit=M
(EU-hosted accounts: https://api.eu.lever.co/v0/postings/{site}) returns a
JSON list of published postings. Pages are read until a short page.
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
    epoch_from_millis,
    html_to_text,
    is_public_http_url,
    MAX_DESCRIPTION_CHARS,
)

SOURCE = "lever"
API_BASES = {
    "global": "https://api.lever.co/v0/postings",
    "eu": "https://api.eu.lever.co/v0/postings",
}
PAGE_SIZE = 100

_WORKPLACE_TYPES = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}


def fetch_board(http: HttpClient, board_token: str, region: str = "global", max_pages: int = 20) -> BoardFetch:
    result = BoardFetch()
    base = API_BASES.get(region)
    if base is None:
        result.stopped_reason = "INVALID_REGION"
        result.error_message = f"Unknown Lever region {region!r}."
        return result
    site = quote(board_token, safe="")

    for page in range(max_pages):
        try:
            data = http.get_json(
                f"{base}/{site}",
                params={"mode": "json", "skip": page * PAGE_SIZE, "limit": PAGE_SIZE},
            )
        except SourceError as exc:
            result.stopped_reason = exc.code
            result.error_message = str(exc)
            return result
        result.pages_checked += 1
        if not isinstance(data, list):
            result.stopped_reason = "UNEXPECTED_RESPONSE"
            result.error_message = "The response is not a list."
            return result
        for item in data:
            posting = _posting(board_token, item)
            if isinstance(posting, str):
                result.skipped.append({"reason": posting, "id": item.get("id") if isinstance(item, dict) else None})
            else:
                result.postings.append(posting)
        if len(data) < PAGE_SIZE:
            result.complete = True
            return result

    result.stopped_reason = "MAX_PAGES_REACHED"
    return result


def _posting(board_token: str, item) -> NormalizedPosting | str:
    if not isinstance(item, dict):
        return "INVALID_ITEM"
    posting_id = item.get("id")
    title = clean_optional(item.get("text"))
    url = item.get("hostedUrl")
    if not posting_id or not title:
        return "MISSING_ID_OR_TITLE"
    if not is_public_http_url(url):
        return "MISSING_URL"
    description = _description(item)
    if not description:
        return "MISSING_DESCRIPTION"
    categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
    workplace = item.get("workplaceType")
    return NormalizedPosting(
        source=SOURCE,
        source_key=f"{board_token.lower()}:{posting_id}",
        title=title,
        url=url,
        description_text=description,
        company_name=None,
        location=clean_optional(categories.get("location")),
        work_mode=_WORKPLACE_TYPES.get(workplace.lower()) if isinstance(workplace, str) else None,
        country_code=country_code_or_none(item.get("country")),
        source_created_at=epoch_from_millis(item.get("createdAt")),
        source_updated_at=None,
    )


def _description(item: dict) -> str:
    parts = [clean_text(item.get("descriptionPlain")) if isinstance(item.get("descriptionPlain"), str) else html_to_text(item.get("description"))]
    lists = item.get("lists")
    for entry in lists if isinstance(lists, list) else []:
        if isinstance(entry, dict):
            heading = clean_text(entry.get("text")) if isinstance(entry.get("text"), str) else ""
            body = html_to_text(entry.get("content"))
            if heading or body:
                parts.append("\n".join(p for p in (heading, body) if p))
    additional = item.get("additionalPlain")
    parts.append(clean_text(additional) if isinstance(additional, str) else html_to_text(item.get("additional")))
    return "\n\n".join(p for p in parts if p)[:MAX_DESCRIPTION_CHARS]
