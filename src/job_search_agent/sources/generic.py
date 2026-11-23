"""Custom career sites: JSON-LD JobPosting -> sitemap -> per-company HTML selectors -> Playwright."""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..http import FetchError, PoliteClient
from ..models import Company, FetchResult, RawJob, url_identity
from ..parsers.html import html_to_text, normalize_location, normalize_ws
from .base import SourceError, iso_ist, norm_workplace
from .playwright_fallback import render_html

log = logging.getLogger(__name__)

DEFAULT_JOB_URL = r"/(?:jobs?|careers?|positions?|openings?|vacanc\w+)/[^/?#]+"
MAX_PAGES_DEFAULT = 40


# -- JSON-LD ---------------------------------------------------------------
def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _location_text(posting: dict[str, Any]) -> str:
    out = []
    locs = posting.get("jobLocation") or []
    for loc in locs if isinstance(locs, list) else [locs]:
        addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
        if isinstance(addr, str):
            out.append(addr)
            continue
        country = addr.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        out.append(", ".join(str(x) for x in [addr.get("addressLocality"), addr.get("addressRegion"), country] if x))
    return " | ".join(o for o in out if o)


def _salary_text(posting: dict[str, Any]) -> str | None:
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return None
    val = base.get("value", {})
    cur = base.get("currency") or (val.get("currency") if isinstance(val, dict) else "") or ""
    if isinstance(val, dict) and val.get("minValue") is not None:
        return f"{cur} {val.get('minValue')}-{val.get('maxValue', val.get('minValue'))}"
    if isinstance(val, (int, float)):
        return f"{cur} {val}"
    return None


def parse_jsonld_jobs(html: str, page_url: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except ValueError:
            continue
        for obj in _walk(data):
            t = obj.get("@type")
            if t != "JobPosting" and not (isinstance(t, list) and "JobPosting" in t):
                continue
            ident = obj.get("identifier")
            ident = ident.get("value") if isinstance(ident, dict) else ident
            jobs.append(RawJob(
                external_id=str(ident) if ident else None, url=urljoin(page_url, obj.get("url") or page_url),
                title=normalize_ws(str(obj.get("title", ""))), location=normalize_location(_location_text(obj)),
                department=obj.get("occupationalCategory") if isinstance(obj.get("occupationalCategory"), str) else None,
                employment_type=str(obj["employmentType"]) if obj.get("employmentType") else None,
                posted_at=iso_ist(obj.get("datePosted")), description_text=html_to_text(obj.get("description")),
                workplace_type="remote" if obj.get("jobLocationType") == "TELECOMMUTE" else None,
                salary_text=_salary_text(obj), raw=obj))
    return [j for j in jobs if j.title]


# -- sitemap ---------------------------------------------------------------
def _locs(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", xml, re.S)


def sitemap_job_urls(client: PoliteClient, base_url: str, pattern: str) -> list[str]:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(base_url))
    queue = client.sitemaps(base_url) or [origin + "/sitemap.xml"]
    rx, urls, fetched = re.compile(pattern, re.I), [], 0
    while queue and fetched < 6:
        sm = queue.pop(0)
        fetched += 1
        try:
            resp = client.get(sm)
        except FetchError:
            continue
        if resp.status_code != 200:
            continue
        locs = _locs(resp.text)
        if "<sitemapindex" in resp.text[:2000]:
            queue += [l for l in locs if re.search(r"job|career|position|opening", l, re.I)][:5]
        else:
            urls += [l for l in locs if rx.search(urlsplit(l).path) and urlsplit(l).netloc == urlsplit(base_url).netloc]
    return list(dict.fromkeys(urls))


# -- selectors -------------------------------------------------------------
def parse_selector_jobs(html: str, page_url: str, cfg: dict[str, Any]) -> list[RawJob]:
    if not cfg.get("job_card"):
        return []
    jobs = []
    for card in HTMLParser(html).css(cfg["job_card"]):
        title_node = card.css_first(cfg["title"]) if cfg.get("title") else card.css_first("a")
        link_node = card.css_first(cfg["link"]) if cfg.get("link") else title_node
        href = link_node.attributes.get("href") if link_node else None
        if not title_node or not href:
            continue
        loc = card.css_first(cfg["location"]) if cfg.get("location") else None
        dept = card.css_first(cfg["department"]) if cfg.get("department") else None
        jobs.append(RawJob(title=normalize_ws(title_node.text()), url=urljoin(page_url, href),
                           location=normalize_location(loc.text() if loc else ""),
                           department=normalize_ws(dept.text()) if dept else None,
                           raw={"source": "html-selectors", "card_html": card.html[:2000]}))
    return jobs


def _page_to_job(html: str, url: str) -> RawJob | None:
    jobs = parse_jsonld_jobs(html, url)
    if jobs:
        j = jobs[0]
        j.url = j.url if j.url != url else url
        return j
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    title = normalize_ws(h1.text()) if h1 else ""
    if not title:
        return None
    body = tree.body
    return RawJob(title=title, url=url, description_text=html_to_text(body.html if body else ""),
                  raw={"source": "html-page"})


class GenericSource:
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult:
        cfg = selectors.get(company.name, {})
        url = company.careers_url
        fallbacks: list[str] = []
        html = ""
        try:
            resp = client.get(url)
            html = resp.text if resp.status_code == 200 else ""
        except FetchError as exc:
            if cfg.get("needs_js") is not True:
                raise
            log.warning("%s: plain fetch failed (%s)", company.name, exc)

        jobs = parse_jsonld_jobs(html, url) if html else []
        if jobs:
            return FetchResult(jobs, "jsonld")

        urls = sitemap_job_urls(client, url, cfg.get("sitemap_pattern") or DEFAULT_JOB_URL)
        if urls:
            cap = int(cfg.get("max_pages", MAX_PAGES_DEFAULT))
            by_id = {u: url_identity(u) for u in urls}
            out, fetched = [], 0
            for u in urls:
                if by_id[u] in known:
                    out.append(RawJob(title="", url=u, raw={"source": "sitemap", "stub": True}))
                    continue
                if fetched >= cap:
                    continue
                fetched += 1
                try:
                    r = client.get(u)
                except FetchError:
                    continue
                job = _page_to_job(r.text, u) if r.status_code == 200 else None
                if job:
                    out.append(job)
            # stubs only prove the URL is still listed; the title is filled from the stored row by the diff
            return FetchResult([j for j in out if j.title or j.raw.get("stub")], "sitemap")

        jobs = parse_selector_jobs(html, url, cfg) if html else []
        if jobs:
            return FetchResult(jobs, "html-selectors")

        if cfg.get("needs_js") is True:
            if not client.allowed(url):
                raise SourceError(f"robots.txt disallows rendering {url}")
            fallbacks.append("playwright")
            log.warning("%s: falling back to Playwright (expensive)", company.name)
            rendered = render_html(url)
            jobs = parse_jsonld_jobs(rendered, url) or parse_selector_jobs(rendered, url, cfg)
            if jobs:
                return FetchResult(jobs, "playwright", fallbacks)
        raise SourceError(
            f"{company.name}: no jobs found via JSON-LD, sitemap or selectors"
            + ("" if cfg else f" (add selectors for it in config/selectors.yaml)"))
