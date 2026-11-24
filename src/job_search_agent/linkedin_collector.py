"""LinkedIn collector over LinkedIn's public guest endpoints (approach, selectors and error policy follow the upstream
github.com/tejaslolage07/linkedin-jobs-api library).

Phase 1: guest search pages (~10 cards each). Search rows run ONE AT A TIME; inside a row up to `workers` result pages are
         fetched at once. `maxListings` is the only limit: each row gets an equal share of what is still missing, so a row that
         runs out of results passes its unused share to the later rows. Pages are fetched at offsets stepping by the page size
         learned from the row's first page; a short page in the middle leaves a gap, which is re-fetched from where that page
         ended (overlaps are removed by job id), so nothing is skipped.
Phase 2: one guest job-detail request per new job id (ids in `skipIds` are skipped) for the description and criteria,
         with the same `workers` concurrency.
Both phases share one limit on requests in flight, and every worker pauses 2-3 s after each of its requests (the library's delay).
Request STARTS are also spread evenly across all workers (about pause / workers apart, retries included): without that, every wave of
pages and every retry after a shared wait leaves in one simultaneous burst, which LinkedIn refuses even when the average rate is fine.

Error policy: a failed request (429, other non-200, timeout/network error) is retried after an exponentially growing wait:
2, 4, 8, 16, 32, then 64 s. If it is still refused after the 64 s wait, the run stops and keeps what it collected. The wait is
shared: while one worker backs off, every worker waits. 403 / 999 / 401 / a login wall stop the run at once.
404/410 on a detail page means "job gone".

Files (same formats as before, so old data keeps working): <out>.jsonl (one record per description), <out>.state.json
(listings + per-search progress, for resume), <out>.summary.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import timeutil

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
TPR = {"past month": "r2592000", "past week": "r604800", "24hr": "r86400"}
JOB_TYPE = {"full time": "F", "full-time": "F", "part time": "P", "part-time": "P", "contract": "C", "temporary": "T",
            "volunteer": "V", "internship": "I"}
REMOTE = {"on-site": "1", "on site": "1", "remote": "2", "hybrid": "3"}
EXPERIENCE = {"internship": "1", "entry level": "2", "associate": "3", "senior": "4", "director": "5", "executive": "6"}
AUTH_WALL = re.compile(r"authwall|/login|checkpoint/challenge")
LINKEDIN_RESULT_LIMIT = 1000  # LinkedIn serves only the first 1,000 results of a search: a request with start >= 1000 gets HTTP 400, whatever the filters
MAX_BACKOFF_SECONDS = 64  # waits double 2, 4, 8 ... 64 s; a failure after the 64 s wait stops the run
DEFAULT_QUERIES = [{"keyword": k, "location": loc} for k, loc in [
    ("software engineer", "India"), ("backend engineer", "India"), ("python developer", "India"), ("AI engineer", "India"),
    ("software engineer", "Bengaluru"), ("backend developer", "Bengaluru"), ("forward deployed engineer", "India"),
    ("django developer", "India"), ("fastapi", "India"), ("machine learning engineer", "India"), ("software developer", "Hyderabad"),
    ("software engineer", "Pune"), ("software engineer", "Mumbai"), ("software engineer", "Delhi"), ("software engineer", "Chennai"),
    ("full stack developer", "India"), ("generative ai engineer", "India"), ("data engineer", "India"), ("sde", "India"),
    ("software engineer remote", "India")]]

_USER_AGENTS: list[str] = json.loads((Path(__file__).parent / "data" / "user_agents.json").read_text())


class Blocked(Exception):
    """LinkedIn refused us (or kept failing): stop, do not work around it."""


class Cancelled(Exception):
    pass


def headers() -> dict[str, str]:
    """The upstream library's header set with a uniformly random User-Agent per request.
    One difference: Accept-Encoding omits 'br' because httpx cannot decode Brotli without an extra package."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.linkedin.com/jobs",
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


# ---- URLs and parsing -------------------------------------------------------------------------------------------------
def search_url(q: dict[str, Any], window: str, offset: int) -> str:
    p: dict[str, str] = {"keywords": q.get("keyword") or "", "location": q.get("location") or "", "start": str(offset)}
    if window in TPR:
        p["f_TPR"] = TPR[window]
    for key, table, param in (("jobType", JOB_TYPE, "f_JT"), ("remoteFilter", REMOTE, "f_WT"), ("experienceLevel", EXPERIENCE, "f_E")):
        v = (q.get(key) or "").lower()
        if v in table:
            p[param] = table[v]
    p["sortBy"] = "R" if (q.get("sortBy") or "recent") == "relevant" else "DD"
    return f"{SEARCH_URL}?{urlencode(p)}"


def job_id(url: str | None) -> str | None:
    m = re.search(r"-(\d{6,})(?:\?|$)", url or "") or re.search(r"/view/(\d{6,})", url or "")
    return m.group(1) if m else None


def parse_cards(html: str) -> list[dict[str, Any]]:
    cards = []
    for li in HTMLParser(html).css("li"):
        def txt(sel: str) -> str:
            n = li.css_first(sel)
            return n.text().strip() if n else ""
        position, company = txt(".base-search-card__title"), txt(".base-search-card__subtitle")
        link = li.css_first(".base-card__full-link")
        url = (link.attributes.get("href") if link else "") or ""
        urn = li.css_first("[data-entity-urn]")
        jid = job_id(url) or ((urn.attributes.get("data-entity-urn") or "").split(":")[-1] if urn else None) or None
        if not position or not company or not jid:
            continue
        time_el, salary = li.css_first("time"), txt(".job-search-card__salary-info")
        cards.append({"id": jid, "position": position, "company": company, "jobUrl": url.split("?")[0], "location": txt(".job-search-card__location"),
                      "date": (time_el.attributes.get("datetime") if time_el else None) or None, "salary": re.sub(r"\s+", " ", salary) or None})
    return cards


def text_of(node: Any) -> str:
    html = node.inner_html or ""
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(p|div|h\d|ul|ol)>", "\n", html, flags=re.I)
    html = re.sub(r"<li[^>]*>", "- ", html, flags=re.I)
    html = re.sub(r"</li>", "\n", html, flags=re.I)
    text = HTMLParser(f"<div>{html}</div>").css_first("div").text().replace(" ", " ")
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def parse_detail(html: str) -> dict[str, Any]:
    tree = HTMLParser(html)

    def first(sel: str) -> str | None:
        n = tree.css_first(sel)
        return (n.text().strip() or None) if n else None
    criteria: dict[str, str] = {}
    for item in tree.css(".description__job-criteria-item"):
        k, v = item.css_first(".description__job-criteria-subheader"), item.css_first(".description__job-criteria-text")
        criteria[k.text().strip() if k else ""] = v.text().strip() if v else ""
    desc = tree.css_first(".show-more-less-html__markup")
    return {"title": first(".top-card-layout__title, .topcard__title"),
            "company": first(".topcard__org-name-link, .top-card-layout__card .topcard__flavor"),
            "location": first(".topcard__flavor--bullet"), "posted": first(".posted-time-ago__text"),
            "applicants": first(".num-applicants__caption"), "criteria": criteria, "description": text_of(desc) if desc else None}


def _now_iso() -> str:
    return timeutil.now_ist().isoformat(timespec="milliseconds")


class LinkedInCollector:
    """Run with `run(cfg, on_progress)`; `terminate()` (from another thread) stops it cooperatively."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, pace: tuple[float, float] = (2.0, 3.0),
                 backoff_unit: float = 1.0) -> None:
        self._transport, self._pace_range, self._backoff_unit = transport, pace, backoff_unit
        self._cancel = threading.Event()

    def terminate(self) -> None:
        self._cancel.set()

    # ---- plumbing ------------------------------------------------------------------------------------------------
    async def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            if self._cancel.is_set():
                raise Cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.25))

    def _start_gap(self, workers: int) -> float:
        """Minimum time between the starts of any two requests: the average per-worker pause divided across the workers."""
        return (self._pace_range[0] + self._pace_range[1]) / 2 / workers

    async def _gate(self) -> None:
        """Spread request starts evenly across all workers (no simultaneous bursts), without lowering steady-state throughput."""
        if self._gap <= 0:
            return
        async with self._gate_lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self._gap
        if start > now:
            await self._sleep(start - now)

    async def _pace(self) -> None:
        await self._sleep(random.uniform(*self._pace_range))

    async def _get(self, url: str) -> httpx.Response:
        consecutive = 0
        while True:
            wait = self._cool_until - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            if self._cancel.is_set():
                raise Cancelled()
            await self._gate()
            t0, failure = time.monotonic(), ""
            try:
                r = await self._client.get(url, headers=headers())
                self.requests += 1
                self.latencies.append(time.monotonic() - t0)
                self.status[str(r.status_code)] = self.status.get(str(r.status_code), 0) + 1
                final = str(r.url)
                if r.status_code in (403, 999, 401) or AUTH_WALL.search(final):
                    raise Blocked(f"HTTP {r.status_code} at {url[:90]} (final: {final[:70]})")
                if r.status_code in (200, 404, 410):
                    return r
                failure = "Rate limit reached (429)" if r.status_code == 429 else f"HTTP {r.status_code}"
            except Blocked:
                raise
            except httpx.HTTPError as exc:  # timeout / network error / redirect loop
                self.requests += 1
                failure = type(exc).__name__
            consecutive += 1
            self.errors += 1
            wait = 2 ** consecutive  # seconds (before scaling by backoff_unit, which only tests change)
            log.info("  error (attempt %d): %s", consecutive, failure)
            if wait > MAX_BACKOFF_SECONDS:
                raise Blocked(f"Still refused after backing off up to {MAX_BACKOFF_SECONDS} s ({failure}) at {url[:90]}")
            backoff = wait * self._backoff_unit
            self._cool_until = max(self._cool_until, time.monotonic() + backoff)  # everyone waits, not just this worker
            self._emit({"stage": "backoff", "seconds": wait, "attempt": consecutive, "reason": failure,
                        "requests": self.requests, "rateLimited": self.status.get("429", 0)})
            await self._sleep(backoff)

    async def _slot(self, fn: Callable[[], Any]) -> Any:
        async with self._sem:
            try:
                return await fn()
            finally:
                if self._aborted is None and not self._cancel.is_set():
                    await self._pace()

    # ---- phases --------------------------------------------------------------------------------------------------
    def run(self, cfg: dict[str, Any], on_progress: Callable[[dict[str, Any]], None]) -> tuple[int, dict[str, Any]]:
        """Returns (code, summary): code 0 = ok, 2 = blocked/error, 130 = cancelled."""
        return asyncio.run(self._run(cfg, on_progress))

    async def _run(self, cfg: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> tuple[int, dict[str, Any]]:
        out = Path(cfg["out"])
        state_path, summary_path = out.with_suffix(".state.json"), out.with_suffix(".summary.json")
        queries: list[dict[str, Any]] = cfg.get("queries") or DEFAULT_QUERIES
        window, max_listings = cfg.get("window") or "past month", int(cfg.get("maxListings") or 1000)
        workers = max(1, int(cfg.get("workers") or 1))
        skip = {str(i) for i in cfg.get("skipIds") or []}
        out.parent.mkdir(parents=True, exist_ok=True)
        done: dict[str, dict[str, Any]] = {}
        if out.exists():
            for line in out.read_text().split("\n"):  # NOT splitlines(): U+2028 etc. can occur inside JSON strings
                if line.strip():
                    rec = json.loads(line)
                    done[rec["id"]] = rec
        state: dict[str, Any] = json.loads(state_path.read_text()) if state_path.exists() else {"cards": {}}
        state.setdefault("search", [])
        cards: dict[str, dict[str, Any]] = state["cards"]

        def save() -> None:
            state_path.write_text(json.dumps(state))

        self.requests, self.errors, self.status, self.latencies = 0, 0, {}, []
        self._cool_until, self._aborted, self._emit = 0.0, None, emit
        self._sem = asyncio.Semaphore(workers)
        self._gap, self._next_start, self._gate_lock = self._start_gap(workers), 0.0, asyncio.Lock()
        started, blocked, cancelled = time.monotonic(), None, False
        desc_done = desc_total = detail_errors = 0
        log.info("max %d listings | %d queries | window %r | resume: %d ids known, %d with details", max_listings, len(queries), window, len(cards), len(done))
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, max_redirects=5, transport=self._transport) as client:
            self._client = client
            try:
                # ---------- phase 1: listings ----------
                searches = [state["search"][i] if i < len(state["search"]) else {"offset": 0, "exhausted": False, "pageSize": 0, "done": False}
                            for i in range(len(queries))]
                state["search"] = searches

                async def fetch_page(q: dict[str, Any], offset: int) -> list[dict[str, Any]] | None:
                    async def go() -> list[dict[str, Any]] | None:
                        if self._aborted is not None:
                            return None
                        try:
                            r = await self._get(search_url(q, window, offset))
                            return parse_cards(r.text) if r.status_code == 200 else []
                        except (Blocked, Cancelled) as exc:
                            self._aborted = self._aborted or exc
                            return None
                    return await self._slot(go)

                def take(page: list[dict[str, Any]]) -> int:
                    n = 0
                    for c in page:
                        if c["id"] not in cards and len(cards) < max_listings:
                            cards[c["id"]] = c
                            n += 1
                    return n

                async def run_search(i: int, share: int) -> None:
                    q, st, found = queries[i], searches[i], 0

                    def progress() -> None:
                        emit({"stage": "search", "query": i + 1, "queries": len(queries), "keyword": q.get("keyword"), "location": q.get("location"),
                              "unique": len(cards), "max": max_listings, "requests": self.requests, "rateLimited": self.status.get("429", 0)})
                    while self._aborted is None and not st["exhausted"] and found < share and len(cards) < max_listings:
                        if not st["pageSize"]:  # the first page teaches us the page size
                            page = await fetch_page(q, st["offset"])
                            if page is None:
                                return
                            if not page:
                                st["exhausted"] = True
                                break
                            st["pageSize"] = len(page)
                            st["offset"] += len(page)
                            found += take(page)
                            progress()
                            save()
                            continue
                        k = min(workers, max(1, math.ceil((share - found) / st["pageSize"])))
                        offsets = [o for o in (st["offset"] + j * st["pageSize"] for j in range(k)) if o < LINKEDIN_RESULT_LIMIT]
                        if not offsets:   # LinkedIn would answer 400 from offset 1,000 on: this search has given everything it can
                            st["exhausted"], st["hitLimit"] = True, True
                            break
                        pages = await asyncio.gather(*(fetch_page(q, o) for o in offsets))
                        if any(p is None for p in pages):
                            return
                        nxt = st["offset"]
                        for j, page in enumerate(pages):
                            found += take(page)
                            if not page:
                                st["exhausted"] = True
                                break
                            nxt = offsets[j] + len(page)
                            if len(page) < st["pageSize"]:
                                break  # short page: continue from where it ended (re-fetching any overlap)
                        st["offset"] = nxt
                        progress()
                        save()
                    if self._aborted is None:
                        emit({"stage": "search_done", "keyword": q.get("keyword"), "location": q.get("location"), "found": found,
                              "reason": "linkedin_limit" if st.get("hitLimit") else "no_more_results" if st["exhausted"] else "share_reached", "unique": len(cards)})

                for i in range(len(queries)):
                    if self._aborted is not None or len(cards) >= max_listings:
                        break
                    if searches[i]["done"]:
                        continue  # finished in an earlier (resumed) run
                    await run_search(i, math.ceil((max_listings - len(cards)) / (len(queries) - i)))
                    if self._aborted is None:
                        searches[i]["done"] = True
                    save()
                if self._aborted is not None:
                    raise self._aborted

                # ---------- phase 2: descriptions ----------
                ids = list(cards)[:max_listings]
                todo = [i for i in ids if i not in done and i not in skip]
                desc_total = len(todo)
                emit({"stage": "details", "done": 0, "total": desc_total, "skippedKnown": sum(1 for i in ids if i in skip), "requests": self.requests})
                nxt_i = 0

                async def worker() -> None:
                    nonlocal nxt_i, desc_done, detail_errors
                    while self._aborted is None:
                        if nxt_i >= len(todo):
                            return
                        jid = todo[nxt_i]
                        nxt_i += 1
                        try:
                            r = await self._get(DETAIL_URL.format(id=jid))
                        except (Blocked, Cancelled) as exc:
                            self._aborted = self._aborted or exc
                            return
                        detail, note = None, "ok"
                        if r.status_code == 200:
                            detail = parse_detail(r.text)
                            if not detail["description"]:
                                note = "no_description_in_html"
                        elif r.status_code in (404, 410):
                            note = "gone"
                        else:
                            note = f"http_{r.status_code}"
                        if note != "ok":
                            detail_errors += 1
                        rec = {"id": jid, "card": cards[jid], "detail": detail, "note": note, "fetchedAt": _now_iso()}
                        with out.open("a") as fh:
                            fh.write(json.dumps(rec) + "\n")
                        done[jid] = rec
                        desc_done += 1
                        if desc_done % 10 == 0 or desc_done == desc_total:
                            log.info("[detail] %d/%d | statuses %s", desc_done, desc_total, self.status)
                            emit({"stage": "details", "done": desc_done, "total": desc_total, "requests": self.requests,
                                  "rateLimited": self.status.get("429", 0), "failed": detail_errors})
                        try:
                            await self._pace()
                        except Cancelled as exc:
                            self._aborted = self._aborted or exc
                            return
                await asyncio.gather(*(worker() for _ in range(workers)))
                if self._aborted is not None:
                    raise self._aborted
            except Blocked as exc:
                blocked = str(exc)
                log.warning("BLOCKED - stopping, no retry: %s", exc)
            except Cancelled:
                cancelled = True
        recs = list(done.values())
        lens = sorted(len(r["detail"]["description"]) for r in recs if r.get("detail") and r["detail"].get("description"))
        lat = sorted(self.latencies)
        summary = {
            "max": max_listings, "uniqueListings": len(cards), "detailRecords": len(recs), "withDescription": len(lens), "fetchedThisRun": desc_done,
            "notes": {n: sum(1 for r in recs if r["note"] == n) for n in {r["note"] for r in recs}},
            "descChars": {"median": lens[len(lens) // 2], "min": lens[0], "max": lens[-1]} if lens else None,
            "requestsThisRun": self.requests, "statusCounts": self.status,
            "latencyMs": {"p50": round(lat[len(lat) // 2] * 1000), "p95": round(lat[int(len(lat) * 0.95)] * 1000)} if lat else None,
            "minutesThisRun": round((time.monotonic() - started) / 60, 1), "blocked": blocked, "cancelled": cancelled,
        }
        summary_path.write_text(json.dumps(summary, indent=2))
        return (130 if cancelled else 2 if blocked else 0), summary
