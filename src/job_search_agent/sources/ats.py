"""Greenhouse, Lever, Ashby adapters using their public JSON job feeds.

Endpoints verified against live public boards (anthropic / palantir / ashby):
  Greenhouse  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  Lever       GET https://api.lever.co/v0/postings/{token}?mode=json   (EU: api.eu.lever.co)
  Ashby       GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
"""
from __future__ import annotations

from typing import Any

from ..http import PoliteClient
from ..models import Company, FetchResult, RawJob
from ..parsers.html import html_to_text, normalize_location, unescape_then_text
from .base import SourceError, iso_ist, norm_workplace


def _json(client: PoliteClient, url: str) -> Any:
    resp = client.get(url)
    if resp.status_code == 404:
        raise SourceError(f"board not found (404): {url} - check board_token")
    if resp.status_code != 200:
        raise SourceError(f"{url} returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"{url} did not return JSON") from exc


def _need_token(company: Company) -> str:
    if not company.board_token:
        raise SourceError(f"{company.name}: no board_token and none detectable from {company.careers_url}")
    return company.board_token


class GreenhouseSource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        token = _need_token(company)
        data = _json(client, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        jobs = []
        for j in data.get("jobs", []):
            meta = {m.get("name", "").lower(): m.get("value") for m in j.get("metadata") or []}
            ranges = j.get("pay_input_ranges") or []
            salary = "; ".join(
                f"{r.get('currency_type', '')} {int(r['min_cents']) // 100}-{int(r['max_cents']) // 100}"
                for r in ranges if r.get("min_cents") is not None and r.get("max_cents") is not None) or None
            depts = j.get("departments") or []
            jobs.append(RawJob(
                external_id=str(j["id"]), url=j["absolute_url"], title=j.get("title", ""),
                location=normalize_location((j.get("location") or {}).get("name")),
                department=depts[0]["name"] if depts else None,
                employment_type=meta.get("employment type") if isinstance(meta.get("employment type"), str) else None,
                posted_at=iso_ist(j.get("first_published") or j.get("updated_at")),
                description_text=unescape_then_text(j.get("content")),
                workplace_type=norm_workplace(meta.get("location type") if isinstance(meta.get("location type"), str) else None),
                salary_text=salary, raw=j))
        return FetchResult(jobs, "greenhouse-json")


class LeverSource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        token = _need_token(company)
        host = "api.eu.lever.co" if "eu.lever.co" in company.careers_url else "api.lever.co"
        data = _json(client, f"https://{host}/v0/postings/{token}?mode=json")
        jobs = []
        for j in data:
            cats = j.get("categories") or {}
            parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
            for lst in j.get("lists") or []:
                parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            parts.append(j.get("additionalPlain") or html_to_text(j.get("additional")))
            sr = j.get("salaryRange") or {}
            salary = None
            if sr.get("min") is not None and sr.get("max") is not None:
                salary = f"{sr.get('currency', '')} {int(sr['min'])}-{int(sr['max'])}"
            locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
            jobs.append(RawJob(
                external_id=j["id"], url=j["hostedUrl"], title=j.get("text", ""),
                location=normalize_location(" | ".join(locs)), department=cats.get("department") or cats.get("team"),
                employment_type=cats.get("commitment"), posted_at=iso_ist(j.get("createdAt")),
                description_text="\n\n".join(p for p in parts if p), workplace_type=norm_workplace(j.get("workplaceType")),
                salary_text=salary, raw=j))
        return FetchResult(jobs, "lever-json")


class AshbySource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        token = _need_token(company)
        data = _json(client, f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
            comp = (j.get("compensation") or {}).get("compensationTierSummary")
            jobs.append(RawJob(
                external_id=j["id"], url=j.get("jobUrl") or j.get("applyUrl", ""), title=j.get("title", ""),
                location=normalize_location(" | ".join(dict.fromkeys(l for l in locs if l))),
                department=j.get("department") or j.get("team"), employment_type=j.get("employmentType"),
                posted_at=iso_ist(j.get("publishedAt")),
                description_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                workplace_type="remote" if j.get("isRemote") else norm_workplace(j.get("workplaceType")),
                salary_text=comp, raw=j))
        return FetchResult(jobs, "ashby-json")
