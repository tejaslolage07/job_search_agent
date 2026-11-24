"""Hosted career-site platforms with public JSON: Keka, Zoho Recruit, Oracle Recruiting Cloud.

  Keka     GET https://{tenant}.keka.com/careers/api/embedjobs/default/active/{tenant-guid}   (robots.txt allows /careers)
  Zoho     GET https://{tenant}.zohorecruit.{in|com|eu}/jobs/Careers   job list is an HTML-escaped JSON <input id="jobs">
  Oracle   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;siteNumber={site},...
board_token: keka = tenant subdomain; zoho = full host; oracle = "host|siteNumber".
Darwinbox is deliberately not supported: its API answers 403 (bot protection) to non-browser clients.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

from ..http import PoliteClient
from ..models import Company, FetchResult, RawJob
from ..parsers.html import html_to_text, normalize_location
from .base import SourceError, iso_ist

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
MAX_DETAILS = 40  # Oracle: description fetches per run (only jobs not seen before)
MAX_JOBS = 1000


def _need(company: Company) -> str:
    if not company.board_token:
        raise SourceError(f"{company.name}: no board_token and none detectable from {company.careers_url}")
    return company.board_token


class KekaSource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        tenant = _need(company)
        base = f"https://{tenant}.keka.com/careers"
        page = client.get(base)
        if page.status_code != 200:
            raise SourceError(f"{base} returned HTTP {page.status_code}")
        m = re.search(r"embedjobs/(?:js/|default/active/)?(" + UUID.pattern + ")", page.text, re.I) or UUID.search(page.text)
        if not m:
            raise SourceError(f"{base}: tenant id not found in page")
        guid = m.group(1) if m.lastindex else m.group(0)
        resp = client.get(f"{base}/api/embedjobs/default/active/{guid}")
        if resp.status_code != 200:
            raise SourceError(f"Keka jobs API returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError("Keka jobs API did not return JSON") from exc
        jobs = []
        for j in data if isinstance(data, list) else []:
            locs = [l.get("name") or ", ".join(x for x in (l.get("city"), l.get("state"), l.get("countryName")) if x)
                    for l in j.get("jobLocations") or [] if isinstance(l, dict)]
            jobs.append(RawJob(
                external_id=str(j["id"]), url=f"{base}/jobdetails/{j['id']}", title=j.get("title", ""),
                location=normalize_location(" | ".join(x for x in locs if x)),
                department=j.get("departmentName") or None, employment_type=None,
                posted_at=iso_ist(j.get("publishedOn") or j.get("postedOn")),
                description_text=html_to_text(j.get("description")), raw=j))
        return FetchResult(jobs, "keka-json")


class ZohoRecruitSource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        host = _need(company)
        base = f"https://{host}/jobs/Careers"
        page = client.get(base)
        if page.status_code != 200:
            raise SourceError(f"{base} returned HTTP {page.status_code}")
        m = re.search(r'value="(\[\{[^"]*)"\s+id="jobs"', page.text) or re.search(r'id="jobs"[^>]*value="(\[\{[^"]*)"', page.text)
        if not m:
            raise SourceError(f"{base}: job list not found in page")
        try:
            data = json.loads(html.unescape(m.group(1)))
        except ValueError as exc:
            raise SourceError(f"{base}: job list is not valid JSON") from exc
        jobs = []
        for j in data:
            if j.get("Publish") in (False, "False", "false"):
                continue
            loc = ", ".join(x for x in (j.get("City"), j.get("State"), j.get("Country")) if x)
            jobs.append(RawJob(
                external_id=str(j["id"]), url=f"{base}/{j['id']}", title=j.get("Posting_Title") or j.get("Job_Opening_Name") or "",
                location=normalize_location(loc), department=j.get("Industry") or None, employment_type=j.get("Job_Type"),
                posted_at=iso_ist(j.get("Date_Opened")), description_text=html_to_text(j.get("Job_Description")), raw=j))
        return FetchResult(jobs, "zoho-recruit")


class OracleSource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        token = _need(company)
        if "|" not in token:
            raise SourceError(f"{company.name}: oracle board_token must be 'host|siteNumber'")
        host, site = token.split("|", 1)
        api = f"https://{host}/hcmRestApi/resources/latest"
        rows: list[dict[str, Any]] = []
        offset, total = 0, None
        while total is None or offset < min(total, MAX_JOBS):
            resp = client.get(f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
                              f"&finder=findReqs;siteNumber={site},limit=25,offset={offset},sortBy=POSTING_DATES_DESC")
            if resp.status_code != 200:
                raise SourceError(f"Oracle requisitions API returned HTTP {resp.status_code}")
            try:
                item = (resp.json().get("items") or [{}])[0]
            except ValueError as exc:
                raise SourceError("Oracle requisitions API did not return JSON") from exc
            page = item.get("requisitionList") or []
            total = int(item.get("TotalJobsCount") or 0)
            rows += page
            offset += 25
            if not page:
                break
        jobs, details = [], 0
        for j in rows:
            jid = str(j["Id"])
            desc = j.get("ShortDescriptionStr") or None
            if jid not in known and details < MAX_DETAILS:
                details += 1
                d = client.get(f'{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="{jid}",siteNumber={site}')
                if d.status_code == 200:
                    try:
                        di = (d.json().get("items") or [{}])[0]
                        desc = html_to_text(" ".join(str(di.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))) or desc
                    except ValueError:
                        pass
            locs = [j.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in j.get("secondaryLocations") or []]
            wp = (j.get("WorkplaceTypeCode") or j.get("WorkplaceType") or "").lower()
            jobs.append(RawJob(
                external_id=jid, url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}", title=j.get("Title", ""),
                location=normalize_location(" | ".join(dict.fromkeys(l for l in locs if l))),
                department=j.get("JobFamily") or j.get("Organization") or None, employment_type=j.get("ContractType") or j.get("JobType"),
                posted_at=iso_ist(j.get("PostedDate")), description_text=desc,
                workplace_type={"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(wp.replace("_", "-")), raw=j))
        return FetchResult(jobs, "oracle-rest")
