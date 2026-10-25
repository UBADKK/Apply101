"""The job sources, their connectors and their reuse terms.

terms_checked records whether the source's terms of use were checked
against the official documentation. A source with auto_publish False is
never synced into the shared catalog until an admin enables a board of it
explicitly with acknowledge_terms (see routers/catalog_admin.py), after
checking the terms named here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import arbeitnow, ashby, greenhouse, lever


@dataclass(frozen=True)
class SourceInfo:
    source: str
    label: str
    # Per-company boards (True) or one global feed (False, Arbeitnow).
    per_company: bool
    regions: tuple[str, ...]
    docs_url: str
    terms_url: str
    attribution: str
    # Where the attribution links to (Arbeitnow requires a link back).
    attribution_url: str | None
    terms_checked: bool
    auto_publish: bool
    terms_note: str
    fetch_board: Callable


_UNCHECKED_NOTE = (
    "Terms not checked: the provider's documentation and terms pages could not "
    "be reached from the session that added this connector (2026-10-25). "
    "Check them before enabling a board."
)

SOURCES: dict[str, SourceInfo] = {
    "arbeitnow": SourceInfo(
        source="arbeitnow",
        label="Arbeitnow",
        per_company=False,
        regions=("global",),
        docs_url="https://www.arbeitnow.com/blog/job-board-api",
        terms_url="https://www.arbeitnow.com/terms",
        attribution="Job listing from Arbeitnow (arbeitnow.com); apply on the original posting.",
        attribution_url="https://www.arbeitnow.com",
        terms_checked=True,
        # Already used by the project through POST /jobs/fetch before the
        # multi-source catalog; kept as it was.
        auto_publish=True,
        terms_note=(
            "Checked 2026-10-25 (arbeitnow.com/terms, section 11 API): the API is "
            "provided as is, using it requires a link back to Arbeitnow.com on the "
            "platform (shown with every Arbeitnow job in the pool, Jobs for me and "
            "the job page), and the permission can be "
            "revoked at any time. Section 2 limits copying the website's own "
            "materials to personal, non-commercial viewing; the project owner should "
            "confirm that showing API jobs in Apply101 fits."
        ),
        fetch_board=arbeitnow.fetch_board,
    ),
    "greenhouse": SourceInfo(
        source="greenhouse",
        label="Greenhouse Job Board",
        per_company=True,
        regions=("global",),
        docs_url="https://developers.greenhouse.io/job-board.html",
        terms_url="https://www.greenhouse.com/legal",
        attribution="Job listing published on the company's Greenhouse job board; apply on the original posting.",
        attribution_url=None,
        terms_checked=False,
        auto_publish=False,
        terms_note=_UNCHECKED_NOTE,
        fetch_board=greenhouse.fetch_board,
    ),
    "lever": SourceInfo(
        source="lever",
        label="Lever Postings",
        per_company=True,
        regions=("global", "eu"),
        docs_url="https://github.com/lever/postings-api",
        terms_url="https://www.lever.co/terms",
        attribution="Job listing published on the company's Lever job site; apply on the original posting.",
        attribution_url=None,
        terms_checked=False,
        auto_publish=False,
        terms_note=(
            "Documentation read 2026-10-25 (github.com/lever/postings-api README): it "
            "describes the API as a way to build a job site and states no reuse or "
            "attribution terms. lever.co/terms could not be reached. Check them "
            "before enabling a board."
        ),
        fetch_board=lever.fetch_board,
    ),
    "ashby": SourceInfo(
        source="ashby",
        label="Ashby job board",
        per_company=True,
        regions=("global",),
        docs_url="https://developers.ashbyhq.com/docs/public-job-posting-api",
        terms_url="https://www.ashbyhq.com/terms",
        attribution="Job listing published on the company's Ashby job board; apply on the original posting.",
        attribution_url=None,
        terms_checked=False,
        auto_publish=False,
        terms_note=_UNCHECKED_NOTE,
        fetch_board=ashby.fetch_board,
    ),
}


def source_info(source: str) -> SourceInfo | None:
    return SOURCES.get(source)


def get_connector(source: str) -> Callable:
    info = SOURCES.get(source)
    if info is None:
        raise KeyError(source)
    return info.fetch_board
