"""Shared types and the bounded HTTP client of the job-source connectors.

A connector turns one public job board into NormalizedPosting objects:

    fetch_board(http, board_token, region, max_pages) -> BoardFetch

BoardFetch.complete is True only when every page of the board was read;
catalog_sync closes jobs only after a complete fetch. Connectors never
write to the database and never follow links found in postings.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup


USER_AGENT = "Apply101/1.0 (job catalog sync)"

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RETRIES = 3
# A Retry-After longer than this ends the fetch as rate limited instead of
# blocking a sync for minutes.
MAX_RETRY_AFTER_SECONDS = 60
MAX_DESCRIPTION_CHARS = 60_000

WORK_MODES = ("remote", "hybrid", "onsite")

_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")


class SourceError(RuntimeError):
    """A board could not be read. `code` is a stable machine-readable
    reason (e.g. "HTTP_404", "RATE_LIMITED", "TIMEOUT", "INVALID_JSON")."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NormalizedPosting:
    source: str
    # Unique within the source, e.g. "gitlab:4012345".
    source_key: str
    title: str
    url: str
    description_text: str
    company_name: str | None = None
    location: str | None = None
    work_mode: str | None = None
    country_code: str | None = None
    source_created_at: int | None = None
    source_updated_at: int | None = None


@dataclass
class BoardFetch:
    postings: list[NormalizedPosting] = field(default_factory=list)
    complete: bool = False
    pages_checked: int = 0
    # Stable reason when the fetch stopped early, else None.
    stopped_reason: str | None = None
    error_message: str | None = None
    skipped: list[dict] = field(default_factory=list)
    # Optional public text about the company (e.g. Greenhouse board intro)
    # and its source URL.
    company_about: str | None = None
    company_about_url: str | None = None


class HttpClient:
    """GETs JSON with a timeout, bounded retries and 429 backoff.

    `session` is anything with a requests-like `get`; tests pass a fake.
    `sleep` is injectable so tests never wait.
    """

    def __init__(
        self,
        session: Any | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session if session is not None else requests.Session()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.sleep = sleep

    def get_json(self, url: str, params: dict | None = None) -> Any:
        attempt = 0
        while True:
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
            except requests.exceptions.Timeout as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self.sleep(2 ** attempt)
                    continue
                raise SourceError("TIMEOUT", "The source did not answer in time.") from exc
            except requests.exceptions.RequestException as exc:
                raise SourceError("REQUEST_ERROR", type(exc).__name__) from exc

            status = response.status_code
            if status == 429 or status in (502, 503, 504):
                wait = _retry_after_seconds(response.headers.get("Retry-After"))
                if wait is None:
                    wait = 2 ** (attempt + 1)
                if attempt < self.max_retries and wait <= MAX_RETRY_AFTER_SECONDS:
                    attempt += 1
                    self.sleep(wait)
                    continue
                code = "RATE_LIMITED" if status == 429 else f"HTTP_{status}"
                raise SourceError(code, f"The source answered HTTP {status}.")
            if 300 <= status < 400:
                raise SourceError(f"HTTP_{status}", "The source answered with a redirect.")
            if status >= 400:
                raise SourceError(f"HTTP_{status}", f"The source answered HTTP {status}.")
            try:
                return response.json()
            except ValueError as exc:
                raise SourceError("INVALID_JSON", "The source answered with invalid JSON.") from exc


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def html_to_text(raw_html: str | None) -> str:
    """Plain text of an HTML fragment; markup, scripts and styles are
    dropped, never rendered."""
    if not isinstance(raw_html, str):
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "object", "embed"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return clean_text(text)[:MAX_DESCRIPTION_CHARS]


def unescaped_html_to_text(escaped_html: str | None) -> str:
    """For sources that send HTML entity-escaped markup (Greenhouse)."""
    if not isinstance(escaped_html, str):
        return ""
    return html_to_text(html.unescape(escaped_html))


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS.sub("", value).strip()


def clean_optional(value: Any, max_chars: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    text = clean_text(value)[:max_chars]
    return text or None


def country_code_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _COUNTRY_CODE.match(code) else None


def epoch_from_iso(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def epoch_from_millis(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value // 1000)


def is_public_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://")) and len(value) <= 2000
