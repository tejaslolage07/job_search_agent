import json
from pathlib import Path

import httpx

from job_search_agent.http import BlockedError, PoliteClient, RobotsDisallowed
from job_search_agent.models import Company
from job_search_agent.sources import REGISTRY
from job_search_agent.sources.detect import detect_from_text
from job_search_agent.sources.generic import parse_jsonld_jobs

FX = Path(__file__).parent / "fixtures"


def client(handler, **kw):
    def wrapped(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(req)
    return PoliteClient(min_interval=0, transport=httpx.MockTransport(wrapped), sleep=lambda s: None, **kw)


def co(ats, token="acme", url="https://x.test"):
    return Company(1, "Acme", url, ats, token)


def test_greenhouse_fixture():
    c = client(lambda r: httpx.Response(200, content=(FX / "greenhouse.json").read_bytes()))
    res = REGISTRY["greenhouse"].fetch(co("greenhouse"), c, set(), {})
    j = res.jobs[0]
    assert j.external_id == "101" and j.location == "Bengaluru, India" and j.workplace_type == "hybrid"
    assert j.posted_at == "2026-11-15T20:30:00+05:30" and "Build APIs in Python and Django." in j.description_text


def test_lever_fixture():
    c = client(lambda r: httpx.Response(200, content=(FX / "lever.json").read_bytes()))
    j = REGISTRY["lever"].fetch(co("lever"), c, set(), {}).jobs[0]
    assert j.title == "AI Engineer" and j.workplace_type == "remote" and "FastAPI" in j.description_text
    assert j.salary_text == "INR 3000000-4000000" and j.posted_at.startswith("2025-11-15")


def test_ashby_fixture_skips_unlisted():
    c = client(lambda r: httpx.Response(200, content=(FX / "ashby.json").read_bytes()))
    jobs = REGISTRY["ashby"].fetch(co("ashby"), c, set(), {}).jobs
    assert [j.external_id for j in jobs] == ["u-1"] and jobs[0].salary_text == "₹25L – ₹35L"


def test_jsonld():
    jobs = parse_jsonld_jobs((FX / "jobposting.html").read_text(), "https://x.test/careers")
    assert jobs[0].external_id == "J-9" and jobs[0].location == "Bengaluru, IN" and jobs[0].posted_at.startswith("2026-11-12")


def test_detect():
    assert detect_from_text("https://job-boards.greenhouse.io/anthropic") == ("greenhouse", "anthropic")
    assert detect_from_text('<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">') == ("greenhouse", "acme")
    assert detect_from_text("https://jobs.lever.co/palantir") == ("lever", "palantir")
    assert detect_from_text("https://jobs.ashbyhq.com/ashby") == ("ashby", "ashby")
    assert detect_from_text("https://nvidia.wd5.myworkdayjobs.com/Site")[0] == "workday"
    assert detect_from_text("https://example.com/careers") is None


def test_robots_disallow_respected():
    def handler(req):
        return httpx.Response(200, text="User-agent: *\nDisallow: /private") if req.url.path == "/robots.txt" else httpx.Response(200, text="ok")
    c = PoliteClient(min_interval=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.get("https://x.test/public").status_code == 200
    try:
        c.get("https://x.test/private/jobs")
        raise AssertionError("should be blocked")
    except RobotsDisallowed:
        pass


def test_stops_on_repeated_429_without_retry_storm():
    calls = []
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(429)
    c = client(handler, max_retries=5)
    try:
        c.get("https://x.test/a")
        raise AssertionError
    except BlockedError:
        pass
    assert len(calls) == 2 and "x.test" in c.blocked
    try:
        c.get("https://x.test/b")
    except BlockedError:
        pass
    assert len(calls) == 2  # no further requests to a blocked host


def test_rate_limit_one_per_second_per_host():
    now = [0.0]
    sleeps = []
    def sleep(s):
        sleeps.append(s)
        now[0] += s
    c = PoliteClient(min_interval=1.0, transport=httpx.MockTransport(lambda r: httpx.Response(404 if "robots" in r.url.path else 200)),
                     sleep=sleep, clock=lambda: now[0])
    c.get("https://x.test/a")
    c.get("https://x.test/b")
    assert sum(sleeps) >= 1.0
