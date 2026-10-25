"""The job sources, their connectors and their reuse terms.

terms_checked records whether the source's published terms (and, where
reachable, its API documentation) were read; it never means reuse is
permitted. The terms_note says what was read and found. A source with auto_publish False is
never synced into the shared catalog without an explicit acknowledgement:
per-company sources when an admin enables a board with acknowledge_terms
(routers/catalog_admin.py); Arbeitnow, which has no boards, on every sync
with acknowledge_arbeitnow_terms (catalog_sync.run_sync) and on the legacy
POST /jobs/fetch and /jobs/fetch-pages routes.
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
        # The project owner decided (2026-10-25) that Arbeitnow is not
        # published automatically either, because of section 2 below: every
        # sync of it needs an explicit acknowledgement.
        auto_publish=False,
        terms_note=(
            "Checked 2026-10-25 (arbeitnow.com/terms, section 11 API): the API is "
            "provided as is, using it requires a link back to Arbeitnow.com on the "
            "platform (shown with every Arbeitnow job in the pool, Jobs for me and "
            "the job page), and the permission can be "
            "revoked at any time. Section 2 limits copying the website's own "
            "materials to personal, non-commercial viewing; because of it the project "
            "owner turned automatic publishing off (2026-10-25): a sync includes "
            "Arbeitnow only with acknowledge_arbeitnow_terms."
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
        terms_checked=True,
        auto_publish=False,
        terms_note=(
            "Checked 2026-10-25: greenhouse.com/legal publishes only customer agreements "
            "(Master subscription agreement, DPA) and policies, and contains no terms that "
            "allow or forbid third parties to reuse postings from the public Job Board API. "
            "developers.greenhouse.io (the API documentation) could not be reached, so its "
            "own wording is not checked. Without an explicit permission, boards stay off until an admin "
            "acknowledges this (ideally with the company's consent)."
        ),
        fetch_board=greenhouse.fetch_board,
    ),
    "lever": SourceInfo(
        source="lever",
        label="Lever Postings",
        per_company=True,
        regions=("global", "eu"),
        docs_url="https://github.com/lever/postings-api",
        terms_url="https://www.lever.co/legal/terms-of-service",
        attribution="Job listing published on the company's Lever job site; apply on the original posting.",
        attribution_url=None,
        terms_checked=True,
        auto_publish=False,
        terms_note=(
            "Checked 2026-10-25: the postings API README describes the API as a way "
            "for a company to build its own job site and states no reuse or "
            "attribution terms; lever.co/legal/terms-of-service governs Lever customers "
            "(Order Form) and says nothing about third parties reading public postings. "
            "Without an explicit permission, boards stay off until an admin acknowledges "
            "this (ideally with the company's consent)."
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
        terms_checked=True,
        auto_publish=False,
        terms_note=(
            "Checked 2026-10-25: the Job Postings API documentation describes it as data "
            "for an organization's own careers page; for partners that ingest postings, "
            "Ashby documents Dedicated Partner Job Feeds (customers opt in; contact "
            "integrations@ashbyhq.com). ashbyhq.com/terms is the customer agreement and "
            "says nothing about third parties. Prefer a partner feed; enable a board "
            "only with acknowledgement (ideally with the company's consent)."
        ),
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
