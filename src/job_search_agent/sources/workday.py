"""Workday: public CXS JSON endpoint that the career site itself calls (no login).

  POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs   (limit<=20)
  GET  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}   (detail)

List pages give title/location/relative date only, so details are fetched just for jobs
we have not seen before, capped per run to keep within the 1 req/s budget.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..http import PoliteClient
from ..models import Company, FetchResult, RawJob
from ..parsers.html import html_to_text, normalize_location
from .base import SourceError, iso_ist

MAX_LIST_PAGES = 10
MAX_DETAIL_FETCHES = 60


def parse_workday_url(careers_url: str) -> tuple[str, str, str]:
    parts = urlsplit(careers_url)
    tenant = parts.netloc.split(".")[0]
    segs = [s for s in parts.path.split("/") if s and not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", s)]
    if not segs:
        raise SourceError(f"cannot find Workday site name in {careers_url}")
    return parts.netloc, tenant, segs[0]


class WorkdaySource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        host, tenant, site = parse_workday_url(company.careers_url)
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        postings: list[dict[str, Any]] = []
        for page in range(MAX_LIST_PAGES):
            resp = client.post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""})
            if resp.status_code != 200:
                raise SourceError(f"Workday list returned HTTP {resp.status_code}")
            batch = resp.json().get("jobPostings", [])
            postings += batch
            if len(batch) < 20:
                break
        jobs, details = [], 0
        for p in postings:
            path = p.get("externalPath", "")
            req = (p.get("bulletFields") or [""])[0] or path.rsplit("_", 1)[-1]
            job = RawJob(external_id=req, url=f"https://{host}/{site}{path}", title=p.get("title", ""),
                         location=normalize_location(p.get("locationsText")), raw=p)
            if req not in known and details < MAX_DETAIL_FETCHES:
                details += 1
                d = client.get(f"{base}{path}")
                if d.status_code == 200:
                    info = d.json().get("jobPostingInfo", {})
                    job.description_text = html_to_text(info.get("jobDescription"))
                    job.posted_at = iso_ist(info.get("startDate"))
                    job.employment_type = info.get("timeType")
                    job.location = normalize_location(info.get("location") or p.get("locationsText"))
                    job.raw = {**p, "detail": info}
            jobs.append(job)
        return FetchResult(jobs, "workday-cxs")
