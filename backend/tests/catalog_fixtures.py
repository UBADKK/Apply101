"""Offline fixtures for the job-source connectors: synthetic responses in
the documented public formats of Greenhouse, Lever, Ashby and Arbeitnow,
and a fake HTTP session. No test using them touches the network."""

import copy
from urllib.parse import urlencode


def greenhouse_jobs(board="acme"):
    return {
        "jobs": [
            {
                "id": 101,
                "title": "Backend Engineer (Python)",
                "updated_at": "2026-10-20T10:00:00-04:00",
                "first_published": "2026-10-01T09:00:00-04:00",
                "location": {"name": "Berlin, Germany"},
                "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/101",
                "company_name": "Acme GmbH",
                "content": "&lt;p&gt;Build APIs with &lt;strong&gt;Python&lt;/strong&gt; and FastAPI.&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;",
            },
            {
                "id": 102,
                "title": "Data Analyst",
                "updated_at": "2026-10-21T10:00:00Z",
                "first_published": None,
                "location": {"name": "Remote"},
                "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/102",
                "content": "&lt;p&gt;SQL and dashboards.&lt;/p&gt;",
            },
            {"id": 103, "title": "No description", "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/103", "content": ""},
        ],
        "meta": {"total": 3},
    }


def greenhouse_board():
    return {"name": "Acme", "content": "&lt;p&gt;Acme builds tools for logistics teams.&lt;/p&gt;"}


def lever_postings(site="leverco", count=2, offset=0):
    postings = []
    for index in range(offset, offset + count):
        postings.append({
            "id": f"aaaa-{index:04d}",
            "text": f"Frontend Engineer {index}",
            "categories": {"location": "Istanbul, Turkey", "commitment": "Full-time", "team": "Web"},
            "country": "TR",
            "workplaceType": "hybrid",
            "hostedUrl": f"https://jobs.lever.co/{site}/aaaa-{index:04d}",
            "applyUrl": f"https://jobs.lever.co/{site}/aaaa-{index:04d}/apply",
            "createdAt": 1760000000000 + index,
            "descriptionPlain": "Work on our React app.",
            "lists": [{"text": "Requirements", "content": "<li>TypeScript</li><li>React</li>"}],
            "additionalPlain": "Turkish and English.",
        })
    return postings


def ashby_board(board="ashbyco"):
    return {
        "apiVersion": "1",
        "jobs": [
            {
                "id": "1111-2222",
                "title": "Platform Engineer",
                "location": "Remote (EU)",
                "isListed": True,
                "isRemote": True,
                "workplaceType": "Remote",
                "descriptionHtml": "<p>Kubernetes and Go.</p>",
                "descriptionPlain": "Kubernetes and Go.",
                "publishedAt": "2026-10-10T08:00:00.000+00:00",
                "address": {"postalAddress": {"addressCountry": "DE", "addressLocality": "Munich"}},
                "jobUrl": f"https://jobs.ashbyhq.com/{board}/1111-2222",
                "applyUrl": f"https://jobs.ashbyhq.com/{board}/1111-2222/application",
            },
            {
                "id": "3333-4444",
                "title": "Hidden role",
                "isListed": False,
                "descriptionPlain": "Not listed.",
                "jobUrl": f"https://jobs.ashbyhq.com/{board}/3333-4444",
            },
        ],
    }


def arbeitnow_page(items):
    return {"data": items, "links": {}, "meta": {}}


def arbeitnow_item(slug, url=None, title="Werkstudent Python", description="<p>Python in Berlin.</p>", company="Beta AG"):
    return {
        "slug": slug,
        "company_name": company,
        "title": title,
        "description": description,
        "remote": False,
        "url": url or f"https://www.arbeitnow.com/jobs/companies/beta/{slug}",
        "tags": [],
        "job_types": [],
        "location": "Berlin",
        "created_at": 1760000000,
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return copy.deepcopy(self._payload)


class FakeSession:
    """Maps "url?query" (or url alone) to a FakeResponse or a list of them
    (returned in order, the last one repeating). Records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        key = url + ("?" + urlencode(params) if params else "")
        self.calls.append(key)
        target = self.routes.get(key, self.routes.get(url))
        if target is None:
            return FakeResponse(404, {"error": "not found"})
        if isinstance(target, Exception):
            raise target
        if isinstance(target, list):
            response = target.pop(0) if len(target) > 1 else target[0]
        else:
            response = target
        if isinstance(response, Exception):
            raise response
        return response
