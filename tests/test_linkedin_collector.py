from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from job_search_agent import linkedin, store
from job_search_agent import linkedin_collector as lc
from job_search_agent.linkedin_collector import LinkedInCollector


# ---------------------------------------------------------------------------------------------------------------------
# fake LinkedIn
# ---------------------------------------------------------------------------------------------------------------------
def card_html(kw: str, idx: int) -> str:
    jid = f"{ord(kw[0])}{1000000 + idx}"
    return (f'<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{jid}"><a class="base-card__full-link" '
            f'href="https://in.linkedin.com/jobs/view/{kw}-role-at-co-{jid}?trk=public"></a>'
            f'<h3 class="base-search-card__title">{kw} role {idx}</h3><h4 class="base-search-card__subtitle">Co {kw}</h4>'
            f'<span class="job-search-card__location">Bengaluru, India</span><time datetime="2026-11-17"></time></div></li>')


DETAIL = ('<div class="top-card-layout__card"><h2 class="top-card-layout__title">Backend Engineer</h2>'
          '<a class="topcard__org-name-link">Acme</a><span class="topcard__flavor--bullet">Bengaluru</span>'
          '<span class="posted-time-ago__text">1 day ago</span><span class="num-applicants__caption">12 applicants</span></div>'
          '<div class="show-more-less-html__markup">Build APIs<br>in Python&nbsp;now<br><ul><li>Django</li><li>FastAPI</li></ul><p>2+ years</p></div>'
          '<ul><li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Seniority level</h3>'
          '<span class="description__job-criteria-text">Entry level</span></li>'
          '<li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Employment type</h3>'
          '<span class="description__job-criteria-text">Full-time</span></li></ul>')


class FakeLinkedIn:
    """Results per keyword: A/B unlimited, C 5, EMPTY 0, GAP 100 (requests with start%20==10 come back short, 6 cards)."""
    TOTAL = {"A": 10**9, "B": 10**9, "C": 5, "EMPTY": 0, "GAP": 100}

    def __init__(self, delay: float = 0.0) -> None:
        self.delay, self.log, self.inflight, self.max_inflight = delay, [], 0, 0
        self.fail: dict[str, list[int]] = {}   # url-substring -> status codes to return first (then normal)
        self.detail_status: dict[str, int] = {}
        self.times: list[tuple[str, float]] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.log.append(url)
        self.times.append((url, time.monotonic()))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            for sub, codes in self.fail.items():
                if sub in url and codes:
                    return httpx.Response(codes.pop(0))
            if "authwall" in url:
                return httpx.Response(200, text="login")
            if "seeMoreJobPostings" in url:
                kw, start = request.url.params["keywords"], int(request.url.params["start"])
                size = 6 if kw == "GAP" and start % 20 == 10 else 10
                n = max(0, min(size, self.TOTAL[kw] - start))
                return httpx.Response(200, text="<ul>" + "".join(card_html(kw, start + i) for i in range(n)) + "</ul>")
            jid = url.rsplit("/", 1)[1]
            return httpx.Response(self.detail_status.get(jid, 200), text=DETAIL)
        finally:
            self.inflight -= 1

    def search_keywords(self) -> list[str]:
        return [httpx.URL(u).params["keywords"] for u in self.log if "seeMoreJobPostings" in u]


def collector(fake: FakeLinkedIn, **kw) -> LinkedInCollector:
    return LinkedInCollector(transport=httpx.MockTransport(fake.handler), pace=(0, 0), backoff_unit=kw.pop("backoff_unit", 0), **kw)


def cfg(tmp_path: Path, queries: list[str], max_listings: int, workers: int = 4, skip_all: bool = True, **extra) -> dict:
    skip = [f"{ord(k[0])}{1000000 + i}" for k in "ABC" for i in range(400)] + [f"{ord('G')}{1000000 + i}" for i in range(400)] if skip_all else []
    return {"queries": [{"keyword": q, "location": "India"} for q in queries], "window": "24hr", "maxListings": max_listings, "workers": workers,
            "out": str(tmp_path / "d.jsonl"), "skipIds": skip, **extra}


def run(c: LinkedInCollector, config: dict):
    events: list[dict] = []
    code, summary = c.run(config, events.append)
    return code, summary, events


def cards_of(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "d.state.json").read_text())["cards"]


# ---------------------------------------------------------------------------------------------------------------------
# URLs, headers, parsing
# ---------------------------------------------------------------------------------------------------------------------
def test_search_url_filters_and_unknown_values_are_ignored():
    u = httpx.URL(lc.search_url({"keyword": "backend engineer", "location": "India", "jobType": "full time", "remoteFilter": "remote",
                                 "experienceLevel": "entry level", "sortBy": "recent"}, "24hr", 20))
    assert dict(u.params) == {"keywords": "backend engineer", "location": "India", "start": "20", "f_TPR": "r86400", "f_JT": "F", "f_WT": "2",
                              "f_E": "2", "sortBy": "DD"}
    u = httpx.URL(lc.search_url({"keyword": "x", "location": "y", "jobType": "bogus", "sortBy": "relevant"}, "nonsense", 0))
    assert dict(u.params) == {"keywords": "x", "location": "y", "start": "0", "sortBy": "R"}


def test_headers_match_the_library_set_with_random_user_agent():
    h = lc.headers()
    assert set(h) == {"User-Agent", "Accept", "Accept-Language", "Accept-Encoding", "Referer", "X-Requested-With", "Connection",
                      "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "Cache-Control", "Pragma"}
    assert h["X-Requested-With"] == "XMLHttpRequest" and h["Accept"].startswith("application/json") and "br" not in h["Accept-Encoding"]
    assert len({lc.headers()["User-Agent"] for _ in range(40)}) > 5 and len(lc._USER_AGENTS) == 822


def test_parse_cards_skips_incomplete_cards_and_strips_tracking():
    html = card_html("A", 1) + '<li><div class="base-card"><h3 class="base-search-card__title">No company</h3></div></li>'
    cards = lc.parse_cards("<ul>" + html + "</ul>")
    assert len(cards) == 1 and cards[0]["id"] == f"{ord('A')}1000001" and "?" not in cards[0]["jobUrl"]
    assert cards[0]["position"] == "A role 1" and cards[0]["date"] == "2026-11-17" and cards[0]["salary"] is None
    urn_only = '<li><div class="base-card" data-entity-urn="urn:li:jobPosting:777"><h3 class="base-search-card__title">T</h3><h4 class="base-search-card__subtitle">C</h4></div></li>'
    assert lc.parse_cards(urn_only)[0]["id"] == "777"


def test_parse_detail_text_criteria_and_meta():
    d = lc.parse_detail(DETAIL)
    assert d["description"] == "Build APIs\nin Python now\n- Django\n- FastAPI\n\n2+ years"
    assert d["criteria"] == {"Seniority level": "Entry level", "Employment type": "Full-time"}
    assert (d["title"], d["company"], d["location"], d["applicants"], d["posted"]) == ("Backend Engineer", "Acme", "Bengaluru", "12 applicants", "1 day ago")
    assert lc.parse_detail("<html></html>")["description"] is None


# ---------------------------------------------------------------------------------------------------------------------
# search phase
# ---------------------------------------------------------------------------------------------------------------------
def test_shares_split_evenly_and_rows_run_one_at_a_time(tmp_path):
    fake = FakeLinkedIn(delay=0.01)
    code, summary, events = run(collector(fake), cfg(tmp_path, ["A", "B", "C"], 25))
    done = {e["keyword"]: (e["found"], e["reason"]) for e in events if e["stage"] == "search_done"}
    assert code == 0 and summary["uniqueListings"] == 25
    assert done == {"A": (10, "share_reached"), "B": (10, "share_reached"), "C": (5, "share_reached")}
    kws = fake.search_keywords()
    blocks = [k for i, k in enumerate(kws) if i == 0 or k != kws[i - 1]]
    assert blocks == ["A", "B", "C"]  # never interleaved


def test_unused_share_carries_over_to_later_rows(tmp_path):
    code, summary, events = run(collector(FakeLinkedIn()), cfg(tmp_path, ["EMPTY", "B"], 20))
    done = {e["keyword"]: (e["found"], e["reason"]) for e in events if e["stage"] == "search_done"}
    assert summary["uniqueListings"] == 20 and done == {"EMPTY": (0, "no_more_results"), "B": (20, "share_reached")}


def test_short_pages_in_the_middle_leave_no_gaps(tmp_path):
    code, summary, _ = run(collector(FakeLinkedIn()), cfg(tmp_path, ["GAP"], 100))
    idx = sorted(int(c["position"].split()[-1]) for c in cards_of(tmp_path).values())
    assert idx == list(range(100)) and summary["uniqueListings"] == 100


def test_concurrency_never_exceeds_workers_and_pages_do_overlap(tmp_path):
    fake = FakeLinkedIn(delay=0.02)
    run(collector(fake), cfg(tmp_path, ["A"], 80, workers=3))
    assert fake.max_inflight == 3
    fake1 = FakeLinkedIn(delay=0.02)
    run(collector(fake1), cfg(tmp_path / "w1", ["A"], 80, workers=1)) if (tmp_path / "w1").mkdir() is None else None
    assert fake1.max_inflight == 1


def test_max_listings_is_a_hard_ceiling(tmp_path):
    code, summary, _ = run(collector(FakeLinkedIn()), cfg(tmp_path, ["A", "B"], 17, workers=8))
    assert summary["uniqueListings"] == 17


def test_duplicates_across_rows_are_counted_once(tmp_path):
    code, summary, _ = run(collector(FakeLinkedIn()), cfg(tmp_path, ["A", "A"], 30))  # same search twice -> same ids
    assert summary["uniqueListings"] <= 30 and len(cards_of(tmp_path)) == summary["uniqueListings"]
    assert len({c["id"] for c in cards_of(tmp_path).values()}) == summary["uniqueListings"]


# ---------------------------------------------------------------------------------------------------------------------
# descriptions
# ---------------------------------------------------------------------------------------------------------------------
def test_descriptions_fetched_for_new_jobs_only_and_files_written(tmp_path):
    first = f"{ord('A')}1000000"
    config = cfg(tmp_path, ["A"], 10, skip_all=False, skipIds=[first])
    fake = FakeLinkedIn()
    code, summary, events = run(collector(fake), config)
    recs = [json.loads(line) for line in (tmp_path / "d.jsonl").read_text().split("\n") if line.strip()]
    assert code == 0 and len(recs) == 9 and first not in {r["id"] for r in recs}
    assert recs[0]["note"] == "ok" and recs[0]["detail"]["description"].startswith("Build APIs") and recs[0]["card"]["id"] == recs[0]["id"]
    assert summary["fetchedThisRun"] == 9 and summary["withDescription"] == 9 and (tmp_path / "d.summary.json").exists()
    assert events[-1]["stage"] == "details" and events[-1]["done"] == 9 and events[-1]["total"] == 9
    # resume: nothing left to do, no network at all
    fake2 = FakeLinkedIn()
    code2, summary2, _ = run(collector(fake2), config)
    assert fake2.log == [] and summary2["fetchedThisRun"] == 0 and summary2["detailRecords"] == 9


def test_gone_job_is_recorded_not_an_error(tmp_path):
    fake = FakeLinkedIn()
    fake.detail_status[f"{ord('C')}1000001"] = 404
    code, summary, _ = run(collector(fake), cfg(tmp_path, ["C"], 5, skip_all=False))
    assert code == 0 and summary["notes"]["gone"] == 1 and summary["blocked"] is None


def test_output_is_compatible_with_the_existing_ingestion(tmp_path):
    run(collector(FakeLinkedIn()), cfg(tmp_path, ["A"], 12, skip_all=False))
    conn = store.connect(":memory:")
    rid = store.start_run(conn, "discover")
    counts = linkedin.ingest(conn, rid, tmp_path / "d.state.json", tmp_path / "d.jsonl")
    assert counts["new"] == 12
    row = conn.execute("SELECT title, description_text, employment_type, source FROM jobs LIMIT 1").fetchone()
    assert row["source"] == "linkedin" and row["employment_type"] == "Full-time" and "Django" in row["description_text"]


# ---------------------------------------------------------------------------------------------------------------------
# error policy
# ---------------------------------------------------------------------------------------------------------------------
def test_two_429s_then_success_recovers_and_is_counted(tmp_path):
    fake = FakeLinkedIn()
    fake.fail["seeMoreJobPostings"] = [429, 429]
    code, summary, _ = run(collector(fake), cfg(tmp_path, ["A"], 10))
    assert code == 0 and summary["uniqueListings"] == 10 and summary["statusCounts"]["429"] == 2 and summary["blocked"] is None


def test_backoff_doubles_up_to_64s_then_stops_and_keeps_partial_results(tmp_path):
    config = cfg(tmp_path, ["A"], 40, workers=1, skip_all=False)
    state = {"n": 0}
    stamps: list[float] = []
    fake = FakeLinkedIn()
    orig = fake.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            state["n"] += 1
            if state["n"] > 1:  # first page fine, then 429 forever
                stamps.append(time.monotonic())
                return httpx.Response(429)
        return await orig(request)
    events: list[dict] = []
    c = LinkedInCollector(transport=httpx.MockTransport(handler), pace=(0, 0), backoff_unit=0.01)  # 2..64 s become 0.02..0.64 s
    code, summary = c.run(config, events.append)
    assert code == 2 and "Still refused after backing off up to 64 s (Rate limit reached (429))" in summary["blocked"]
    assert len(stamps) == 7  # the failed request, then retries after waits of 2, 4, 8, 16, 32, 64 s; the 7th failure stops the run
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    for got, want in zip(gaps, [0.02, 0.04, 0.08, 0.16, 0.32, 0.64]):
        assert want * 0.9 <= got <= want * 2.5, (gaps, want)  # doubling, 2 -> 64 (scaled)
    waits = [e["seconds"] for e in events if e["stage"] == "backoff"]
    assert waits == [2, 4, 8, 16, 32, 64]
    assert summary["uniqueListings"] == 10 and (tmp_path / "d.state.json").exists()  # what we had was saved


def test_five_429s_in_a_row_are_survived_by_backing_off(tmp_path):
    fake = FakeLinkedIn()
    fake.fail["seeMoreJobPostings"] = [429] * 5  # used to stop the run at the 3rd
    code, summary, _ = run(collector(fake), cfg(tmp_path, ["A"], 10))
    assert code == 0 and summary["uniqueListings"] == 10 and summary["statusCounts"]["429"] == 5 and summary["blocked"] is None


@pytest.mark.parametrize("status", [403, 999, 401])
def test_hard_blocks_stop_immediately_without_retrying(tmp_path, status):
    fake = FakeLinkedIn()
    fake.fail["seeMoreJobPostings"] = [status] * 10
    code, summary, _ = run(collector(fake), cfg(tmp_path, ["A"], 10))
    assert code == 2 and f"HTTP {status}" in summary["blocked"] and summary["requestsThisRun"] == 1


def test_login_wall_redirect_is_a_block(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if "authwall" in str(request.url):
            return httpx.Response(200, text="login")
        return httpx.Response(302, headers={"Location": "https://www.linkedin.com/authwall?trk=x"})
    code, summary, _ = run(LinkedInCollector(transport=httpx.MockTransport(handler), pace=(0, 0), backoff_unit=0), cfg(tmp_path, ["A"], 10))
    assert code == 2 and "authwall" in summary["blocked"]


def test_backoff_is_shared_so_other_workers_wait_too(tmp_path):
    fake = FakeLinkedIn(delay=0.01)
    fake.fail["seeMoreJobPostings"] = []  # first search call fails once
    first = {"done": False}
    orig = fake.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url) and not first["done"]:
            first["done"] = True
            fake.times.append(("FAIL", time.monotonic()))
            return httpx.Response(429)
        return await orig(request)
    c = LinkedInCollector(transport=httpx.MockTransport(handler), pace=(0, 0), backoff_unit=0.15)  # 429 -> 2^1 * 0.15 = 0.3 s shared wait
    code, summary, _ = run(c, cfg(tmp_path, ["A"], 40, workers=4))
    t_fail = next(t for u, t in fake.times if u == "FAIL")
    later = [t for u, t in fake.times if u != "FAIL" and t > t_fail]
    assert code == 0 and later and min(later) - t_fail >= 0.25  # nobody sent anything during the shared cool-off


def test_cancel_stops_cleanly_with_partial_results(tmp_path):
    fake = FakeLinkedIn(delay=0.02)
    c = LinkedInCollector(transport=httpx.MockTransport(fake.handler), pace=(0.1, 0.1), backoff_unit=0)
    result = {}
    t = threading.Thread(target=lambda: result.update(zip(("code", "summary"), c.run(cfg(tmp_path, ["A"], 500, workers=1), lambda e: None))))
    t.start()
    time.sleep(0.5)
    c.terminate()
    t.join(5)
    assert not t.is_alive() and result["code"] == 130 and result["summary"]["cancelled"] is True
    assert 0 < result["summary"]["uniqueListings"] < 500


# ---------------------------------------------------------------------------------------------------------------------
# request starts are spread evenly (no simultaneous bursts)
# ---------------------------------------------------------------------------------------------------------------------
def _starts(fake: FakeLinkedIn, only: str = "") -> list[float]:
    return sorted(t for u, t in fake.times if only in u and u != "FAIL")


def test_request_starts_are_spaced_not_bursty(tmp_path):
    fake = FakeLinkedIn(delay=0.01)
    c = LinkedInCollector(transport=httpx.MockTransport(fake.handler), pace=(0.4, 0.4), backoff_unit=0)  # gap = 0.4 / 8 = 0.05 s
    run(c, cfg(tmp_path, ["A"], 90, workers=8))
    starts = _starts(fake)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) >= 9 and min(gaps) >= 0.04  # a wave of 8 pages used to leave at the same instant (gap ~0)


def test_retries_after_a_shared_wait_are_also_staggered(tmp_path):
    fake = FakeLinkedIn(delay=0.01)
    seen = {"n": 0}
    orig = fake.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url) and int(request.url.params["start"]) > 0:
            seen["n"] += 1
            if seen["n"] <= 8:  # the whole first wave of 8 gets refused
                fake.times.append((str(request.url), time.monotonic()))
                return httpx.Response(429)
        return await orig(request)
    c = LinkedInCollector(transport=httpx.MockTransport(handler), pace=(0.4, 0.4), backoff_unit=0.1)
    code, summary, _ = run(c, cfg(tmp_path, ["A"], 90, workers=8))
    assert code == 0 and summary["statusCounts"]["429"] == 8
    starts = _starts(fake, "start=")
    retry_starts = starts[-9:]
    assert min(b - a for a, b in zip(retry_starts, retry_starts[1:])) >= 0.04  # 8 retries did not leave together again


def test_one_worker_is_not_slowed_by_the_gate(tmp_path):
    fake = FakeLinkedIn(delay=0.0)
    c = LinkedInCollector(transport=httpx.MockTransport(fake.handler), pace=(0.05, 0.05), backoff_unit=0)
    t0 = time.monotonic()
    run(c, cfg(tmp_path, ["A"], 30, workers=1))
    assert time.monotonic() - t0 < 1.5  # 3 pages x ~0.05 s pause; the gate (0.05 s) must not add on top of the pause


def test_gate_is_off_when_there_is_no_pause(tmp_path):
    c = LinkedInCollector(transport=httpx.MockTransport(FakeLinkedIn().handler), pace=(0, 0), backoff_unit=0)
    assert c._start_gap(8) == 0
