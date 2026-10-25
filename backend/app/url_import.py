"""Preview a job from a URL, for the user to correct and save as a personal
manual job (POST /jobs/manual). Nothing is saved here.

Two ways in, nothing else:
1. A supported ATS posting link (Greenhouse, Lever, Ashby): the posting is
   read from the provider's public API on its fixed host.
2. Any other public page that robots.txt allows: the page's JobPosting
   JSON-LD (schema.org) is read.
Unsupported sites (LinkedIn and other platforms behind logins), blocked
pages, and pages without a usable JobPosting fall back to pasting the text:
no field is ever guessed or made up. No login, cookie or CAPTCHA handling.

Fetching an arbitrary page is guarded against SSRF:
- http/https only, default ports only, no credentials in the URL;
- every host is resolved and must resolve only to public addresses, and the
  connection goes to that checked address (no second DNS lookup, so DNS
  rebinding cannot swap it); TLS is still verified against the host name;
- redirects are followed by hand (at most 3), each hop checked the same way;
- connect/read timeouts, an overall deadline and a response size cap;
- only HTML is parsed, and every text field is reduced to plain text.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3
from bs4 import BeautifulSoup

from .job_sources import HttpClient, SourceError
from .job_sources import ashby
from .job_sources.base import (
    clean_optional,
    clean_text,
    country_code_or_none,
    html_to_text,
    unescaped_html_to_text,
)


USER_AGENT = "Apply101/1.0 (job import preview; +https://github.com/UBADKK/Apply101)"
ROBOTS_USER_AGENT = "Apply101"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
DEADLINE_SECONDS = 20
MAX_URL_LENGTH = 2000
MAX_JSON_LD_BLOCKS = 20
MAX_JSON_LD_CHARS = 500_000

# Sites whose job pages sit behind logins or forbid automated access: never
# fetched, the user pastes the text instead.
UNSUPPORTED_HOST_SUFFIXES = (
    "linkedin.com", "indeed.com", "indeed.de", "glassdoor.com", "glassdoor.de",
    "xing.com", "stepstone.de", "monster.com", "monster.de", "kariyer.net",
    "secretcv.com", "yenibiris.com",
)

_GREENHOUSE_HOSTS = {
    "boards.greenhouse.io": "https://boards-api.greenhouse.io/v1/boards",
    "job-boards.greenhouse.io": "https://boards-api.greenhouse.io/v1/boards",
    "job-boards.eu.greenhouse.io": "https://boards-api.eu.greenhouse.io/v1/boards",
}
_LEVER_HOSTS = {
    "jobs.lever.co": "https://api.lever.co/v0/postings",
    "jobs.eu.lever.co": "https://api.eu.lever.co/v0/postings",
}
_ASHBY_HOST = "jobs.ashbyhq.com"
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")


class ImportError_(Exception):
    """A preview that cannot be made. `code` is stable and machine-readable;
    every code means: paste the job text instead."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ImportPreview:
    source: str
    url: str
    title: str | None = None
    company_name: str | None = None
    location: str | None = None
    description_text: str | None = None
    work_mode: str | None = None
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "url": self.url,
            "fields": {
                "title": self.title,
                "company_name": self.company_name,
                "location": self.location,
                "description_text": self.description_text,
            },
            "work_mode": self.work_mode,
            "missing": self.missing,
            "notes": self.notes,
        }


# --- URL checks ---------------------------------------------------------------

def check_url(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of an importable URL, or ImportError_."""
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL_LENGTH:
        raise ImportError_("ERR_IMPORT_URL_INVALID", "Enter a full http(s) link.")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ImportError_("ERR_IMPORT_URL_INVALID", "Enter a full http(s) link.")
    if parts.username or parts.password:
        raise ImportError_("ERR_IMPORT_URL_INVALID", "Links with credentials are not accepted.")
    default_port = 443 if parts.scheme == "https" else 80
    try:
        port = parts.port or default_port
    except ValueError:
        raise ImportError_("ERR_IMPORT_URL_INVALID", "The link has an invalid port.")
    if port != default_port:
        raise ImportError_("ERR_IMPORT_URL_BLOCKED", "Only standard web ports are allowed.")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ImportError_("ERR_IMPORT_URL_INVALID", "The link has an invalid host name.")
    return parts.scheme, host, port


def is_unsupported_host(host: str) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in UNSUPPORTED_HOST_SUFFIXES)


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_loopback or address.is_link_local or address.is_private)


def resolve_public(host: str, port: int, resolver: Callable = socket.getaddrinfo) -> str:
    """The first address of host, if every address it resolves to is
    public; otherwise ImportError_. A literal IP is checked the same way."""
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The site's address could not be found.")
    addresses = [info[4][0] for info in infos]
    if not addresses or not all(_public_address(address) for address in addresses):
        raise ImportError_("ERR_IMPORT_URL_BLOCKED", "This address is not a public website.")
    return addresses[0]


# --- guarded fetch ------------------------------------------------------------

@dataclass
class FetchedPage:
    url: str
    status: int
    content_type: str
    body: bytes


def _open(scheme: str, host: str, port: int, address: str, path: str, accept: str, read_timeout: float):
    timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=read_timeout)
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            address, port=port, timeout=timeout, retries=False,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
            server_hostname=host, assert_hostname=host,
        )
    else:
        pool = urllib3.HTTPConnectionPool(address, port=port, timeout=timeout, retries=False)
    headers = {"Host": host if port in (80, 443) else f"{host}:{port}", "User-Agent": USER_AGENT, "Accept": accept, "Connection": "close"}
    try:
        response = pool.urlopen("GET", path, headers=headers, redirect=False, preload_content=False, assert_same_host=False)
    except BaseException:
        pool.close()
        raise
    # One pool per request (it is pinned to one checked address); closed
    # by guarded_get when the response is done.
    response.close_pool = pool.close
    return response


def guarded_get(
    url: str,
    *,
    max_bytes: int,
    accept: str = "text/html,application/xhtml+xml",
    resolver: Callable = socket.getaddrinfo,
    opener: Callable = _open,
    clock: Callable[[], float] = time.monotonic,
) -> FetchedPage:
    deadline = clock() + DEADLINE_SECONDS
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port = check_url(current)
        if is_unsupported_host(host):
            raise ImportError_("ERR_IMPORT_SITE_UNSUPPORTED", "This site cannot be imported. Paste the job text instead.")
        address = resolve_public(host, port, resolver)
        parts = urlsplit(current)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        remaining = deadline - clock()
        if remaining <= 0:
            raise ImportError_("ERR_IMPORT_TIMEOUT", "The site took too long to answer.")
        try:
            response = opener(scheme, host, port, address, path, accept, min(READ_TIMEOUT_SECONDS, remaining))
        except urllib3.exceptions.TimeoutError:
            raise ImportError_("ERR_IMPORT_TIMEOUT", "The site took too long to answer.")
        except (urllib3.exceptions.HTTPError, OSError):
            raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The page could not be loaded.")
        try:
            status = response.status
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The page redirects nowhere.")
                current = urljoin(current, location)
                continue
            body = bytearray()
            for chunk in response.stream(64 * 1024):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ImportError_("ERR_IMPORT_TOO_LARGE", "The page is too large to import.")
                if clock() > deadline:
                    raise ImportError_("ERR_IMPORT_TIMEOUT", "The site took too long to answer.")
            return FetchedPage(
                url=current,
                status=status,
                content_type=(response.headers.get("Content-Type") or "").split(";")[0].strip().lower(),
                body=bytes(body),
            )
        except urllib3.exceptions.HTTPError:
            raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The page could not be loaded.")
        finally:
            response.release_conn()
            close_pool = getattr(response, "close_pool", None)
            if close_pool is not None:
                close_pool()
    raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The page redirects too often.")


def robots_allows(url: str, fetch: Callable[..., FetchedPage]) -> bool:
    """robots.txt of the URL's host allows our user agent to read it. A
    missing robots.txt (404/410) allows; 401/403, server errors and fetch
    failures do not."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        page = fetch(robots_url, max_bytes=MAX_ROBOTS_BYTES, accept="text/plain")
    except ImportError_:
        return False
    if page.status in (404, 410):
        return True
    if page.status != 200:
        return False
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(page.body.decode("utf-8", errors="replace").splitlines())
    return parser.can_fetch(ROBOTS_USER_AGENT, url)


# --- JSON-LD JobPosting ----------------------------------------------------------

def _is_job_posting(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.split("/")[-1] == "JobPosting" for k in kinds)


def _walk(node: Any, depth: int = 0):
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)
    elif isinstance(node, dict):
        if _is_job_posting(node):
            yield node
        graph = node.get("@graph")
        if graph is not None:
            yield from _walk(graph, depth + 1)


def _text_of(value: Any, max_chars: int = 300) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    return clean_optional(value, max_chars)


def _location(posting: dict) -> tuple[str | None, str | None]:
    locations = posting.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]
    names = []
    country = None
    for location in locations if isinstance(locations, list) else []:
        if not isinstance(location, dict):
            continue
        address = location.get("address")
        if isinstance(address, str):
            names.append(clean_text(address)[:200])
            continue
        if not isinstance(address, dict):
            continue
        locality = clean_optional(address.get("addressLocality"), 100)
        region = clean_optional(address.get("addressRegion"), 100)
        country_value = address.get("addressCountry")
        country_name = _text_of(country_value, 100)
        country = country or country_code_or_none(country_name)
        parts = [part for part in (locality, region, country_name) if part]
        if parts:
            names.append(", ".join(parts))
    return ("; ".join(dict.fromkeys(names))[:300] or None), country


def parse_job_posting_json_ld(html_text: str, page_url: str) -> ImportPreview:
    soup = BeautifulSoup(html_text, "html.parser")
    postings = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:MAX_JSON_LD_BLOCKS]:
        raw = script.string or script.get_text() or ""
        if len(raw) > MAX_JSON_LD_CHARS:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        postings.extend(_walk(data))
    if not postings:
        raise ImportError_("ERR_IMPORT_NO_JOB_DATA", "No structured job data was found on this page. Paste the job text instead.")
    posting = postings[0]
    location, _country = _location(posting)
    work_mode = None
    location_type = posting.get("jobLocationType")
    if isinstance(location_type, str) and location_type.upper() == "TELECOMMUTE":
        work_mode = "remote"
        location = location or "Remote"
    preview = ImportPreview(
        source="json_ld",
        url=page_url,
        title=_text_of(posting.get("title")),
        company_name=_text_of(posting.get("hiringOrganization")),
        location=location,
        description_text=html_to_text(posting.get("description")) if isinstance(posting.get("description"), str) else None,
        work_mode=work_mode,
    )
    if work_mode == "remote":
        preview.notes.append("The page says the job is remote; it does not say from which countries.")
    return _finish(preview)


def _finish(preview: ImportPreview) -> ImportPreview:
    for name in ("title", "company_name", "location", "description_text"):
        if not getattr(preview, name):
            preview.missing.append(name)
    if "title" in preview.missing or "description_text" in preview.missing:
        raise ImportError_(
            "ERR_IMPORT_INCOMPLETE",
            "The page does not include the job title and full text. Paste the job text instead.",
        )
    return preview


# --- supported ATS links -----------------------------------------------------------

def _ats_target(host: str, path: str) -> tuple[str, str, str, str] | None:
    segments = [segment for segment in path.split("/") if segment]
    if host in _GREENHOUSE_HOSTS and len(segments) >= 3 and segments[1] == "jobs":
        token, job_id = segments[0], segments[2]
        if _SEGMENT.match(token) and job_id.isdigit():
            return "greenhouse", _GREENHOUSE_HOSTS[host], token, job_id
    if host in _LEVER_HOSTS and len(segments) >= 2:
        site, posting_id = segments[0], segments[1]
        if _SEGMENT.match(site) and _ID.match(posting_id):
            return "lever", _LEVER_HOSTS[host], site, posting_id
    if host == _ASHBY_HOST and len(segments) >= 2:
        board, posting_id = segments[0], segments[1]
        if _SEGMENT.match(board) and _ID.match(posting_id):
            return "ashby", ashby.API_BASE, board, posting_id
    return None


def _preview_from_ats(url: str, target: tuple[str, str, str, str], http: HttpClient) -> ImportPreview:
    source, base, token, posting_id = target
    try:
        if source == "greenhouse":
            data = http.get_json(f"{base}/{token}/jobs/{posting_id}")
            if not isinstance(data, dict):
                raise ImportError_("ERR_IMPORT_NO_JOB_DATA", "The posting could not be read.")
            location = data.get("location")
            preview = ImportPreview(
                source=source,
                url=url,
                title=clean_optional(data.get("title")),
                company_name=clean_optional(data.get("company_name")),
                location=clean_optional(location.get("name")) if isinstance(location, dict) else None,
                description_text=unescaped_html_to_text(data.get("content")) or None,
            )
        elif source == "lever":
            data = http.get_json(f"{base}/{token}/{posting_id}", params={"mode": "json"})
            from .job_sources import lever

            posting = lever._posting(token, data)
            if isinstance(posting, str):
                raise ImportError_("ERR_IMPORT_INCOMPLETE", "The posting does not include the job title and text. Paste the job text instead.")
            preview = ImportPreview(
                source=source, url=url, title=posting.title, location=posting.location,
                description_text=posting.description_text, work_mode=posting.work_mode,
            )
        else:
            data = http.get_json(f"{base}/{token}")
            jobs = data.get("jobs") if isinstance(data, dict) else None
            match = next((job for job in jobs or [] if isinstance(job, dict) and job.get("id") == posting_id), None)
            if match is None:
                raise ImportError_("ERR_IMPORT_NOT_FOUND", "The posting was not found; it may be closed.")
            posting = ashby.posting_from_item(token, match)
            if isinstance(posting, str):
                raise ImportError_("ERR_IMPORT_INCOMPLETE", "The posting does not include the job title and text. Paste the job text instead.")
            preview = ImportPreview(
                source=source, url=url, title=posting.title, location=posting.location,
                description_text=posting.description_text, work_mode=posting.work_mode,
            )
    except SourceError as exc:
        if exc.code == "HTTP_404":
            raise ImportError_("ERR_IMPORT_NOT_FOUND", "The posting was not found; it may be closed.")
        raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The job board did not answer. Try again later or paste the job text.")
    if preview.work_mode == "remote":
        preview.notes.append("The posting says the job is remote; it does not say from which countries.")
    if not preview.company_name:
        preview.notes.append("The job board does not name the company in the posting; add it yourself.")
    return _finish(preview)


def preview_url(
    url: str,
    *,
    http: HttpClient | None = None,
    fetch: Callable[..., FetchedPage] = guarded_get,
) -> ImportPreview:
    scheme, host, _port = check_url(url)
    url = url.strip()
    if is_unsupported_host(host):
        raise ImportError_("ERR_IMPORT_SITE_UNSUPPORTED", "This site cannot be imported. Paste the job text instead.")
    target = _ats_target(host, urlsplit(url).path)
    if target is not None:
        return _preview_from_ats(url, target, http or HttpClient(timeout_seconds=10, max_retries=0))

    if not robots_allows(url, fetch):
        raise ImportError_("ERR_IMPORT_BLOCKED", "This site does not allow automated reading of the page. Paste the job text instead.")
    page = fetch(url, max_bytes=MAX_PAGE_BYTES)
    if page.status in (401, 403):
        raise ImportError_("ERR_IMPORT_BLOCKED", "The page needs a login or blocks automated access. Paste the job text instead.")
    if page.status == 404 or page.status == 410:
        raise ImportError_("ERR_IMPORT_NOT_FOUND", "The page was not found; the job may be closed.")
    if page.status != 200:
        raise ImportError_("ERR_IMPORT_FETCH_FAILED", "The page could not be loaded.")
    if page.content_type not in ("text/html", "application/xhtml+xml"):
        raise ImportError_("ERR_IMPORT_NO_JOB_DATA", "The link is not a web page. Paste the job text instead.")
    preview = parse_job_posting_json_ld(page.body.decode("utf-8", errors="replace"), url)
    return preview
