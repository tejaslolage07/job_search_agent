from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sys
from conftest import PROFILE, ROOT
from fastapi.testclient import TestClient

from job_search_agent import store, timeutil
from job_search_agent.http import PoliteClient
from job_search_agent.web.app import create_app
from job_search_agent.web.runner import RunController

FX = Path(__file__).parent / "fixtures"
PROFILE_BODY = {**{k: v for k, v in PROFILE.items() if k != "resume_paths"}, "name": "Test Candidate"}  # a run needs the name saved
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


class FakeCollector:
    """Stands in for the Node collector: writes the same files, emits the same progress events."""

    def __init__(self, cards: list[dict], code: int = 0, blocked: str | None = None, hang: threading.Event | None = None) -> None:
        self.cards, self.code, self.blocked, self.hang = cards, code, blocked, hang
        self.terminated = threading.Event()

    def terminate(self) -> None:
        self.terminated.set()

    def run(self, cfg: dict, on_progress):  # type: ignore[no-untyped-def]
        out = Path(cfg["out"])
        on_progress({"stage": "search", "query": 1, "queries": 1, "keyword": "x", "location": "India", "page": 1, "unique": len(self.cards),
                     "max": cfg["maxListings"], "requests": 3, "rateLimited": 0})
        if self.hang is not None:
            self.terminated.wait(5)
            return 143, None
        state = {"cards": {c["id"]: c for c in self.cards}, "q": 1, "page": 0, "offset": 0}
        out.with_suffix(".state.json").write_text(json.dumps(state))
        with out.open("w") as fh:
            for c in self.cards:
                detail = {"description": f"{c['position']} role. Python Django FastAPI. 2+ years of experience.", "criteria": {"Employment type": "Full-time"},
                          "applicants": "12 applicants", "posted": "1 day ago"}
                fh.write(json.dumps({"id": c["id"], "card": c, "detail": detail, "note": "ok"}) + "\n")
        on_progress({"stage": "details", "done": len(self.cards), "total": len(self.cards), "requests": 3 + len(self.cards), "rateLimited": 1, "failed": 0})
        return self.code, {"requestsThisRun": 3 + len(self.cards), "blocked": self.blocked}


def card(i: int, company: str = "Acme", title: str = "Backend Engineer", location: str = "Bengaluru, Karnataka, India") -> dict:
    return {"id": str(4000000 + i), "position": title, "company": company, "location": location, "date": timeutil.today_ist().isoformat(),
            "jobUrl": f"https://in.linkedin.com/jobs/view/{title.lower().replace(' ', '-')}-at-{company.lower()}-{4000000 + i}", "salary": None}


def greenhouse_transport() -> httpx.MockTransport:
    body = (FX / "greenhouse.json").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(200, content=body)
    return httpx.MockTransport(handler)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "matching.yaml", tmp_path / "config" / "matching.yaml")
    shutil.copy(ROOT / "config" / "llm_prompt.md", tmp_path / "config" / "llm_prompt.md")
    shutil.copy(ROOT / "config" / "llm_prompt_resume.md", tmp_path / "config" / "llm_prompt_resume.md")
    (tmp_path / "config" / "companies.csv").write_text(
        "company,careers_url,ats_type,board_token,tier,active,notes\nAcme,https://boards.greenhouse.io/acme,greenhouse,acme,1,true,\n")
    (tmp_path / "config" / "india_companies.csv").write_text(
        "company,careers_url,ats_type,board_token,tier,active,notes,size_band,size_basis,verified,fallback_portal\n"
        "Verified Co,https://jobs.lever.co/verified,lever,verified,,false,,small,employees (approx),yes,\n"
        "No Url Co,,custom,,,false,,micro,employees (approx),no,https://www.linkedin.com/jobs/search/?keywords=x\n")
    return tmp_path


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch):
    """Every test has a (fake) key, and make_client always plugs in a fake OpenAI, so no test can reach the real API."""
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


def make_client(home, collector=None, openai=None):
    from fake_openai import FakeOpenAI
    openai = openai or FakeOpenAI()
    ctl = RunController(__import__("job_search_agent.service", fromlist=["Home"]).Home(home),
                        client_factory=lambda: PoliteClient(min_interval=0, transport=greenhouse_transport(), sleep=lambda s: None),
                        collector_factory=lambda: collector or FakeCollector([]), openai_transport=openai.transport)
    return TestClient(create_app(home, controller=ctl, start_scheduler=False)), ctl


def wait_done(client, ctl, run_id):
    ctl.wait(20)
    return client.get(f"/api/runs/{run_id}").json()


# ---- profile / resumes / secrets -------------------------------------------------------------------------------------
def test_profile_validation_errors_are_per_field(home):
    c, _ = make_client(home)
    r = c.put("/api/profile", json={"years_experience": 2})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"expected_ctc_lpa", "usd_to_inr", "locations", "target_titles"} <= fields
    assert c.get("/api/profile").json()["exists"] is False


def test_profile_save_and_roundtrip(home):
    c, _ = make_client(home)
    assert c.put("/api/profile", json=PROFILE_BODY).json() == {"saved": True}
    got = c.get("/api/profile").json()
    assert got["exists"] and got["profile"]["expected_ctc_lpa"]["min"] == 28
    assert (home / "config" / "profile.yaml").exists()


def test_work_authorization_accepts_only_the_one_dropdown_value(home):
    c, _ = make_client(home)
    assert c.put("/api/profile", json={**PROFILE_BODY, "work_authorization": "Indian citizen"}).json() == {"saved": True}
    assert c.get("/api/profile").json()["profile"]["work_authorization"] == "Indian citizen"
    assert c.put("/api/profile", json={**PROFILE_BODY, "work_authorization": "India citizen"}).json() == {"saved": True}      # an older spelling is normalised
    assert c.get("/api/profile").json()["profile"]["work_authorization"] == "Indian citizen"
    r = c.put("/api/profile", json={**PROFILE_BODY, "work_authorization": "H1B holder"})
    assert r.status_code == 422 and "work_authorization" in {e["field"] for e in r.json()["errors"]}


def test_the_profile_can_be_saved_without_preferred_locations_or_must_have_keywords(home):
    c, _ = make_client(home)
    body = {**PROFILE_BODY, "locations": {**PROFILE_BODY["locations"], "preferred": []}}
    assert c.put("/api/profile", json=body).json() == {"saved": True} and c.get("/api/profile").json()["profile"]["locations"]["preferred"] == []
    assert c.put("/api/profile", json={**PROFILE_BODY, "must_have_keywords": []}).json() == {"saved": True}
    assert c.get("/api/profile").json()["profile"]["must_have_keywords"] == []


def test_resume_upload_rules_and_profile_link(home):
    c, _ = make_client(home)
    c.put("/api/profile", json=PROFILE_BODY)
    assert c.post("/api/resumes", data={"name": "  "}, files={"file": ("r.pdf", PDF, "application/pdf")}).status_code == 422
    assert c.post("/api/resumes", data={"name": "backend"}, files={"file": ("r.pdf", b"not a pdf", "application/pdf")}).status_code == 422
    assert c.post("/api/resumes", data={"name": "backend"}, files={"file": ("r.pdf", PDF, "application/pdf")}).status_code == 200
    prof = c.get("/api/profile").json()["profile"]
    assert list(prof["resume_paths"]) == ["backend"] and Path(prof["resume_paths"]["backend"]).exists()
    assert c.delete("/api/resumes", params={"name": "backend"}).json()["resumes"] == []
    assert c.get("/api/profile").json()["profile"]["resume_paths"] == {}


def test_there_is_no_endpoint_for_saving_keys_any_more(home):
    c, _ = make_client(home)
    assert c.get("/api/secrets").status_code == 404 and c.put("/api/secrets", json={"anthropic_api_key": "x"}).status_code in (404, 405)
    assert not (home / "data" / "secrets.json").exists()


def test_cross_origin_writes_blocked(home):
    c, _ = make_client(home)
    body = {"names": [], "active": True}
    assert c.post("/api/catalog/select", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/catalog/select", json=body, headers={"Origin": "http://testserver"}).status_code == 200


KEY = "sk-test-FAKEKEY1234567890"


def test_the_openai_key_can_be_saved_from_the_page_and_is_never_echoed_back(home, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c, _ = make_client(home)
    assert c.put("/api/llm/key", json={"key": "short"}).status_code == 422
    r = c.put("/api/llm/key", json={"key": "sk-test-ABCDEFGHIJKLMNOP1234"})
    assert r.status_code == 200 and "ABCDEFGHIJKLMNOP" not in r.text
    assert c.get("/api/llm").json()["openai_key_set"] is True
    env = (Path(home) / ".env").read_text()
    assert "OPENAI_API_KEY=sk-test-ABCDEFGHIJKLMNOP1234" in env and oct((Path(home) / ".env").stat().st_mode & 0o777) == "0o600"
    c.put("/api/llm/key", json={"key": "sk-test-ZZZZZZZZZZZZZZZZZZZZ"})
    assert (Path(home) / ".env").read_text().count("OPENAI_API_KEY=") == 1  # replaced, not appended


def test_llm_status_shows_whether_the_key_in_env_is_found_without_ever_revealing_it(home, monkeypatch):
    c, _ = make_client(home)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    x = c.get("/api/llm").json()
    assert x["openai_key_set"] is False and x["key_hint"] is None and x["env_file"] == str(home / ".env") and c.get("/api/state").json()["openai_key_set"] is False
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    r = c.get("/api/llm")
    x = r.json()
    assert x["openai_key_set"] is True and x["key_hint"] == "sk-...7890" and x["model"] == "gpt-6-luna" and x["concurrency"] == 8 and x["prompt_problem"] is None
    for url in ("/api/llm", "/api/state", "/api/profile"):
        assert KEY not in c.get(url).text                                  # the key itself never leaves the server
    assert c.get("/api/state").json()["openai_key_set"] is True
    monkeypatch.setenv("OPENAI_MODEL", "model-from-env")
    assert c.get("/api/llm").json()["model"] == "model-from-env"


def test_llm_status_reports_a_missing_or_broken_prompt_file(home):
    c, _ = make_client(home)
    assert c.get("/api/llm").json()["prompt_problem"] is None
    (home / "config" / "llm_prompt.md").write_text("## SYSTEM\nhi\n## USER\n{{nope}}\n")
    assert "unknown placeholder" in c.get("/api/llm").json()["prompt_problem"]
    (home / "config" / "llm_prompt.md").unlink()
    assert "prompt file not found" in c.get("/api/llm").json()["prompt_problem"]


# ---- catalog ---------------------------------------------------------------------------------------------------------
def test_catalog_select_skips_rows_without_url(home):
    c, _ = make_client(home)
    r = c.post("/api/catalog/select", json={"names": ["Verified Co", "No Url Co", "Nope"], "active": True}).json()
    assert r["updated"] == ["Verified Co"] and {s["company"] for s in r["skipped"]} == {"No Url Co", "Nope"}
    names = {x["company"]: x["active"] for x in c.get("/api/companies").json()}
    assert names["Verified Co"] == "true"
    rows = {x["company"]: x for x in c.get("/api/catalog?verified=yes").json()["rows"]}
    assert rows["Verified Co"]["crawler_active"] == "yes"


# ---- runs ------------------------------------------------------------------------------------------------------------
def test_full_run_ats_then_match_and_job_views(home):
    c, ctl = make_client(home)
    c.put("/api/profile", json=PROFILE_BODY)
    rid = c.post("/api/runs", json={"ats": {"enabled": True}, "match": True}).json()["run_id"]
    run = wait_done(c, ctl, rid)
    assert run["status"] == "finished" and run["new_count"] == 1 and not run["errors"]
    assert run["progress"]["ats"]["state"] == "done" and run["progress"]["matching"]["state"] == "done"
    assert run["progress"]["matching"]["decided_stage1"] == 1 and run["progress"]["matching"]["decided_stage2"] == 0
    msgs = [e["message"] for e in c.get(f"/api/runs/{rid}/events").json()]
    assert any("Acme: 1 jobs via greenhouse-json" in m for m in msgs)
    jobs = c.get("/api/jobs?bucket=all").json()
    assert jobs["total"] == 1 and jobs["jobs"][0]["company"] == "Acme" and jobs["jobs"][0]["bucket"] == "accept"
    j = c.get(f"/api/jobs/{jobs['jobs'][0]['id']}").json()
    assert j["match"]["resume_read"] is False and j["match"]["reasons"] and j["url"].startswith("https://")
    # second run: idempotent
    rid2 = c.post("/api/runs", json={"ats": {"enabled": True}, "match": True}).json()["run_id"]
    assert wait_done(c, ctl, rid2)["new_count"] == 0


def test_run_requires_something_to_do_and_rejects_parallel_runs(home):
    c, ctl = make_client(home, FakeCollector([], hang=threading.Event()))
    assert c.post("/api/runs", json={"ats": {"enabled": False}, "match": False}).status_code == 422
    c.put("/api/profile", json=PROFILE_BODY)
    assert c.post("/api/runs", json={"ats": {"enabled": False}, "linkedin": {"enabled": True}, "match": False}).status_code == 422  # no queries
    body = {"ats": {"enabled": False}, "match": False, "linkedin": {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}}
    rid = c.post("/api/runs", json=body).json()["run_id"]
    assert c.post("/api/runs", json=body).status_code == 409
    assert c.post(f"/api/runs/{rid}/cancel").json() == {"cancelling": True}
    assert wait_done(c, ctl, rid)["status"] == "cancelled"


def test_linkedin_stage_ingests_and_keeps_duplicates_separate(home):
    cards = [card(1, "Acme", "Backend Engineer"), card(2, "OtherCo", "AI Engineer")]
    c, ctl = make_client(home, FakeCollector(cards))
    c.put("/api/profile", json=PROFILE_BODY)
    body = {"ats": {"enabled": True}, "match": True, "linkedin": {"enabled": True, "queries": [{"keyword": "backend", "location": "India", "remote_filter": "remote"}]}}
    run = wait_done(c, ctl, c.post("/api/runs", json=body).json()["run_id"])
    assert run["status"] == "finished" and run["new_count"] == 3
    assert run["progress"]["linkedin"]["state"] == "done" and run["progress"]["linkedin"]["descriptions_done"] == 2
    jobs = c.get("/api/jobs?bucket=all").json()["jobs"]
    acme = [j for j in jobs if j["company"] == "Acme"]
    assert {j["source"] for j in acme} == {"ats", "linkedin"}  # same company, both versions shown
    assert all(j["company"] != "OtherCo [LinkedIn]" for j in jobs)  # suffix never shown to the user
    assert c.get("/api/jobs?bucket=all&source=linkedin").json()["total"] == 2
    st = c.get("/api/state").json()
    cut = c.get("/api/jobs?bucket=all").json()["breakdown"]  # the company and job-portal cuts shown on the Jobs page
    assert dict(cut["companies"])["Acme"] == len(acme) and sum(n for _, n in cut["sources"]) == len(jobs)
    assert {k for k, _ in cut["sources"]} == {"ats", "linkedin"}
    assert len(cut["days"]) == 14 and sum(n for _, n in cut["days"]) == len(jobs)  # the posted-day chart on the Dashboard
    today = cut["days"][-1][0]
    assert c.get(f"/api/jobs?bucket=all&day={today}").json()["total"] == cut["days"][-1][1]  # a bar on the chart opens exactly that day's jobs
    assert c.get("/api/jobs?bucket=all&day=yesterday").status_code == 422
    only = c.get("/api/jobs?bucket=all&company=Acme").json()
    assert only["total"] == len(acme) and all(j["company"] == "Acme" for j in only["jobs"])  # clicking a company on the Dashboard opens its jobs
    assert st["linkedin"]["requests_today"] == 5


def test_ats_crawl_never_removes_linkedin_jobs(home):
    c, ctl = make_client(home, FakeCollector([card(1, "Acme")]))
    c.put("/api/profile", json=PROFILE_BODY)
    li = {"enabled": True, "queries": [{"keyword": "backend", "location": "India"}]}
    wait_done(c, ctl, c.post("/api/runs", json={"ats": {"enabled": True}, "match": False, "linkedin": li}).json()["run_id"])
    for _ in range(3):  # ATS-only runs: LinkedIn rows are not in that feed, must stay active
        wait_done(c, ctl, c.post("/api/runs", json={"ats": {"enabled": True}, "match": False}).json()["run_id"])
    conn = store.connect(home / "data" / "job_search_agent.db")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source='linkedin' AND status='active'").fetchone()[0] == 1


def test_linkedin_block_keeps_partial_results_and_does_not_block_later_runs(home):
    c, ctl = make_client(home, FakeCollector([card(1, "Acme")], code=2, blocked="HTTP 429 ..."))
    li = {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}
    c.put("/api/profile", json=PROFILE_BODY)
    body = {"ats": {"enabled": False}, "match": False, "linkedin": li}
    run = wait_done(c, ctl, c.post("/api/runs", json=body).json()["run_id"])
    assert run["progress"]["linkedin"]["state"] == "blocked" and run["new_count"] == 1  # partial results kept
    assert any("LinkedIn stopped us" in e["error"] and "Partial results were saved" in e["error"] for e in run["errors"])
    assert "paused_until" not in c.get("/api/state").json()["linkedin"]
    run2 = wait_done(c, ctl, c.post("/api/runs", json=body).json()["run_id"])
    assert run2["progress"]["linkedin"]["state"] == "blocked"  # it tried again (no hidden timer skipping the step)


def test_linkedin_defaults_come_from_profile(home):
    c, _ = make_client(home)
    assert c.get("/api/linkedin/defaults").json() == {"queries": []}
    c.put("/api/profile", json=PROFILE_BODY)
    qs = c.get("/api/linkedin/defaults").json()["queries"]
    assert {"keyword": "Backend Engineer", "location": "Bengaluru"} == {k: qs[0][k] for k in ("keyword", "location")}
    assert {q["location"] for q in qs} == set(PROFILE_BODY["locations"]["preferred"]) - {"Remote-India"} | {"India"}  # every selected location; Remote-India means India
    assert {q["keyword"] for q in qs} == set(PROFILE_BODY["target_titles"])
    assert all(q["job_type"] == "full time" and q["remote_filter"] is None and q["experience_level"] is None and q["sort_by"] == "recent" for q in qs)


# ---- schedules -------------------------------------------------------------------------------------------------------
def test_schedule_minimum_two_hours(home):
    c, _ = make_client(home)
    cfg = {"ats": {"enabled": True}, "match": False}
    c.put("/api/profile", json=PROFILE_BODY)
    assert c.post("/api/schedules", json={"name": "x", "interval_minutes": 60, "config": cfg}).status_code == 422
    assert c.post("/api/schedules", json={"name": "x", "interval_minutes": 119, "config": cfg}).status_code == 422
    r = c.post("/api/schedules", json={"name": "every 2h", "interval_minutes": 120, "config": cfg})
    assert r.status_code == 200 and r.json()["enabled"] is True
    sid = r.json()["id"]
    assert c.patch(f"/api/schedules/{sid}", json={"interval_minutes": 30}).status_code == 422
    assert c.patch(f"/api/schedules/{sid}", json={"interval_minutes": 1440}).json()["interval_minutes"] == 1440
    assert c.post("/api/schedules", json={"name": "bad", "interval_minutes": 120, "config": {"ats": {"enabled": False}, "match": False}}).status_code == 422


def test_scheduler_fires_when_due_and_advances_next_run(home):
    c, ctl = make_client(home)
    c.put("/api/profile", json=PROFILE_BODY)
    sched = c.app.state.scheduler
    sid = c.post("/api/schedules", json={"name": "daily", "interval_minutes": 1440, "config": {"ats": {"enabled": True}, "match": True}}).json()["id"]
    assert sched.tick() is None  # not due yet
    later = datetime.now(UTC) + timedelta(hours=25)
    run_id = sched.tick(later)
    assert run_id is not None
    ctl.wait(20)
    row = c.get("/api/schedules").json()[0]
    assert row["last_run_id"] == run_id and row["next_run_at"] > timeutil.fmt(later)
    assert c.get(f"/api/runs/{run_id}").json()["trigger"] == "schedule:daily"
    assert sched.tick(later) is None  # next one is a day later
    c.patch(f"/api/schedules/{sid}", json={"enabled": False})
    assert sched.tick(later + timedelta(days=3)) is None  # disabled schedules never fire


def test_scheduler_skips_while_a_run_is_active(home):
    hang = threading.Event()
    c, ctl = make_client(home, FakeCollector([], hang=hang))
    li = {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}
    c.put("/api/profile", json=PROFILE_BODY)
    rid = c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": li}).json()["run_id"]
    c.post("/api/schedules", json={"name": "s", "interval_minutes": 120, "config": {"ats": {"enabled": True}, "match": False}})
    assert c.app.state.scheduler.tick(datetime.now(UTC) + timedelta(hours=3)) is None
    c.post(f"/api/runs/{rid}/cancel")
    ctl.wait(10)


# ---- job states ------------------------------------------------------------------------------------------------------
def test_job_user_states_and_hidden_filter(home):
    c, ctl = make_client(home)
    c.put("/api/profile", json=PROFILE_BODY)
    wait_done(c, ctl, c.post("/api/runs", json={"ats": {"enabled": True}, "match": True}).json()["run_id"])
    jid = c.get("/api/jobs?bucket=all").json()["jobs"][0]["id"]
    assert c.put(f"/api/jobs/{jid}/state", json={"state": "bogus"}).status_code == 422
    assert c.put(f"/api/jobs/{jid}/state", json={"state": "saved"}).json()["state"] == "saved"
    assert c.get("/api/jobs?bucket=all&state=saved").json()["total"] == 1
    c.put(f"/api/jobs/{jid}/state", json={"state": "hidden"})
    assert c.get("/api/jobs?bucket=all").json()["total"] == 0  # hidden jobs leave the default view
    assert c.get("/api/jobs?bucket=all&state=hidden").json()["total"] == 1
    c.put(f"/api/jobs/{jid}/state", json={"state": "none"})
    assert c.get("/api/jobs?bucket=all").json()["total"] == 1
    assert c.get("/api/jobs/99999").status_code == 404


def test_sse_stream_ends_with_final_state(home):
    c, ctl = make_client(home)
    rid = c.post("/api/runs", json={"ats": {"enabled": True}, "match": False}).json()["run_id"]
    c.put("/api/profile", json=PROFILE_BODY)
    ctl.wait(20)
    with c.stream("GET", f"/api/runs/{rid}/stream") as r:
        text = "".join(r.iter_text())
    assert "event: end" in text and '"status": "finished"' in text


def test_run_log_explains_why_each_search_stopped(home):
    class Collector(FakeCollector):
        def run(self, cfg, on_progress):  # type: ignore[no-untyped-def]
            assert "maxPagesPerQuery" not in cfg and "maxRequests" not in cfg  # max listings is the only limit
            on_progress({"stage": "search_done", "keyword": "software engineer", "location": "India", "found": 60, "reason": "share_reached", "unique": 60})
            on_progress({"stage": "search_done", "keyword": "backend", "location": "India", "found": 12, "reason": "no_more_results", "unique": 72})
            return super().run(cfg, on_progress)

    c, ctl = make_client(home, Collector([card(1)]))
    li = {"enabled": True, "queries": [{"keyword": "software engineer", "location": "India"}]}
    c.put("/api/profile", json=PROFILE_BODY)
    rid = c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": li}).json()["run_id"]
    wait_done(c, ctl, rid)
    msgs = [e["message"] for e in c.get(f"/api/runs/{rid}/events").json()]
    assert any("collected its share of the listing limit (60 new listings)" in m for m in msgs)
    assert any("found everything LinkedIn had (12 new listings)" in m for m in msgs)
    # the removed setting is silently ignored if an old saved schedule still contains it
    assert c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": {**li, "max_pages_per_query": 5}}).status_code in (200, 409)


def test_unknown_or_incomplete_collector_messages_do_not_fail_the_run(home):
    class Collector(FakeCollector):
        def run(self, cfg, on_progress):  # type: ignore[no-untyped-def]
            on_progress({"stage": "something_new_in_a_later_version", "x": 1})
            on_progress({"stage": "details"})  # missing done/total
            on_progress({"stage": "search"})  # missing everything
            on_progress({})
            return super().run(cfg, on_progress)

    c, ctl = make_client(home, Collector([card(1)]))
    li = {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}
    c.put("/api/profile", json=PROFILE_BODY)
    run = wait_done(c, ctl, c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": li}).json()["run_id"])
    assert run["status"] == "finished" and not run["errors"] and run["new_count"] == 1


def test_real_python_collector_runs_inside_the_dashboard_and_can_be_cancelled(home):
    """No fakes for the collector itself: LinkedInCollector (httpx + selectolax) in the runner thread, only the network is mocked."""
    from test_linkedin_collector import FakeLinkedIn

    from job_search_agent.linkedin_collector import LinkedInCollector
    from job_search_agent.service import Home

    fake = FakeLinkedIn(delay=0.01)
    ctl = RunController(Home(home), client_factory=lambda: PoliteClient(min_interval=0, transport=greenhouse_transport(), sleep=lambda s: None),
                        collector_factory=lambda: LinkedInCollector(transport=httpx.MockTransport(fake.handler), pace=(0, 0), backoff_unit=0))
    c = TestClient(create_app(home, controller=ctl, start_scheduler=False))
    c.put("/api/profile", json=PROFILE_BODY)
    setup_resume(c, home)
    body = {"ats": {"enabled": False}, "match": True, "linkedin": {"enabled": True, "workers": 3, "max_listings": 25,
            "queries": [{"keyword": "A", "location": "India"}, {"keyword": "B", "location": "India"}]}}
    run = wait_done(c, ctl, c.post("/api/runs", json=body).json()["run_id"])
    assert run["status"] == "finished" and run["new_count"] == 25 and not run["errors"]
    li = run["progress"]["linkedin"]
    assert li["state"] == "done" and li["unique"] == 25 and li["descriptions_done"] == 25
    assert run["progress"]["matching"]["state"] == "done"
    msgs = [e["message"] for e in c.get(f"/api/runs/{run['id']}/events").json()]
    assert any("collected its share" in m for m in msgs)
    assert c.get("/api/jobs?bucket=all&source=linkedin").json()["total"] == 25 and fake.max_inflight <= 3

    # cancel a long one through the API
    slow = FakeLinkedIn(delay=0.05)
    ctl2 = RunController(Home(home), client_factory=lambda: PoliteClient(min_interval=0, transport=greenhouse_transport(), sleep=lambda s: None),
                         collector_factory=lambda: LinkedInCollector(transport=httpx.MockTransport(slow.handler), pace=(0.2, 0.2), backoff_unit=0))
    c2 = TestClient(create_app(home, controller=ctl2, start_scheduler=False))
    big = {"ats": {"enabled": False}, "match": False, "linkedin": {"enabled": True, "workers": 1, "max_listings": 900, "queries": [{"keyword": "A", "location": "India"}]}}
    rid = c2.post("/api/runs", json=big).json()["run_id"]
    time.sleep(0.8)
    assert c2.post(f"/api/runs/{rid}/cancel").json() == {"cancelling": True}
    ctl2.wait(10)
    assert c2.get(f"/api/runs/{rid}").json()["status"] == "cancelled" and not ctl2.busy()


def test_long_backoff_waits_are_explained_in_the_run_log(home):
    class Collector(FakeCollector):
        def run(self, cfg, on_progress):  # type: ignore[no-untyped-def]
            for s in (2, 4, 8, 16):
                on_progress({"stage": "backoff", "seconds": s, "attempt": 1, "reason": "Rate limit reached (429)"})
            return super().run(cfg, on_progress)

    c, ctl = make_client(home, Collector([card(1)]))
    li = {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}
    c.put("/api/profile", json=PROFILE_BODY)
    rid = c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": li}).json()["run_id"]
    wait_done(c, ctl, rid)
    ev = c.get(f"/api/runs/{rid}/events").json()
    waits = [e for e in ev if "refusing requests" in e["message"]]
    assert [e["message"].split("waiting ")[1].split(" s")[0] for e in waits] == ["8", "16"]  # 2 s and 4 s are routine, not logged
    assert all(e["level"] == "warning" for e in waits)


@pytest.mark.parametrize("workers,status", [(1, 200), (6, 200), (7, 422), (8, 422), (10, 422), (12, 422), (0, 422)])
def test_parallel_requests_setting_accepts_1_to_6(home, workers, status):
    c, ctl = make_client(home, FakeCollector([card(1)]))
    li = {"enabled": True, "workers": workers, "queries": [{"keyword": "x", "location": "India"}]}
    c.put("/api/profile", json=PROFILE_BODY)
    r = c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": li})
    assert r.status_code == status
    ctl.wait(10)


def test_posted_filter_api_accepts_calendar_values_and_legacy_hours_and_rejects_garbage(home):
    c, _ = make_client(home)
    for ok in ("", "today", "2d", "7d", "30d", "24h"):  # '24h' = a filter saved in the browser before Posted became calendar days
        assert c.get(f"/api/jobs?bucket=all&since={ok}").status_code == 200, ok
    assert c.get("/api/jobs?bucket=all&since=yesterday").status_code == 422
    assert c.get("/api/jobs?bucket=all&since=0d").status_code == 422


# ---- the AI judge inside a dashboard run -----------------------------------------------------------------------------
def setup_resume(c, home):
    """Upload a resume through the API and pre-fill the parsed-text cache so no real PDF parsing is needed."""
    import hashlib
    assert c.post("/api/resumes", data={"name": "backend"}, files={"file": ("r.pdf", PDF, "application/pdf")}).status_code == 200
    conn = store.connect(home / "data" / "job_search_agent.db")
    conn.execute("INSERT OR REPLACE INTO resume_cache(file_hash, text, parsed_at) VALUES(?,?,?)",
                 (hashlib.sha256(PDF).hexdigest(), "Python Django FastAPI developer with backend experience.", store.now_iso()))
    conn.commit()


AI_BODY = {"ats": {"enabled": False}, "match": True,
           "linkedin": {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}}


def test_a_run_matches_with_the_ai_and_uses_the_resume_only_for_ambiguous_jobs(home):
    from fake_openai import FakeOpenAI
    fake = FakeOpenAI(delay=0.05)
    cards = [card(i, f"Co{i}", "Data Analyst PHASE1_AMB") for i in range(1, 7)] + [card(10, "Acme", "Backend Engineer")]
    c, ctl = make_client(home, FakeCollector(cards), openai=fake)
    c.put("/api/profile", json=PROFILE_BODY)
    setup_resume(c, home)
    run = wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    m = run["progress"]["matching"]
    assert run["status"] == "finished" and not run["errors"] and m["state"] == "done"
    assert (m["llm_done"], m["llm_total"]) == (7, 7) and len(fake.requests) == 7 + 6          # all 7 asked about the profile; the 6 ambiguous ones then about the resume
    assert (m["decided_stage1"], m["decided_stage2"]) == (1, 6) and m["buckets"] == {"accept": 7}
    assert (m["run_profile"], m["run_resume"]) == (1, 6)                                                      # this run's split, shown on the Runs tab
    assert fake.peak() <= 8 and all(r["auth"] == f"Bearer {KEY}" for r in fake.requests)
    msgs = [e["message"] for e in c.get(f"/api/runs/{run['id']}/events").json()]
    assert any("AI matcher: model gpt-6-luna, 8 calls at a time" in x for x in msgs) and any("Checked 7 job(s) this run: 1 decided from the profile details, 6 needed the resume" in x for x in msgs)
    jobs = c.get("/api/jobs?bucket=all").json()["jobs"]
    ai = [j for j in jobs if j["title"].startswith("Data Analyst")]
    assert len(ai) == 6 and all(j["bucket"] == "accept" and j["stage_decided"] == 2 and j["resume_read"] for j in ai)
    detail = c.get(f"/api/jobs/{ai[0]['id']}").json()["match"]
    assert detail["method"] == "llm-resume" and any(r["rule"] == "llm_resume" and "python" in r["detail"] for r in detail["reasons"])
    assert [x["verdict"] for x in detail["inputs"]["llm"]["calls"]] == ["ambiguous", "match"]
    split = c.get("/api/jobs?bucket=all").json()
    assert split["phases"] == {"profile": 1, "resume": 6}                                                      # the Jobs tab shows the same split
    only_resume = c.get("/api/jobs?bucket=all&phase=resume").json()
    assert only_resume["total"] == 6 and {j["stage_decided"] for j in only_resume["jobs"]} == {2} and only_resume["phases"] == split["phases"]
    assert c.get("/api/jobs?bucket=all&phase=profile").json()["total"] == 1 and c.get("/api/jobs?bucket=all&phase=nope").status_code == 422
    clear = next(j for j in jobs if j["title"] == "Backend Engineer")
    assert clear["stage_decided"] == 1 and not clear["resume_read"]
    # a second run: nothing changed, so nobody is asked again
    run2 = wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    assert run2["progress"]["matching"]["newly_scored"] == 0 and len(fake.requests) == 13


def test_matching_needs_the_key_there_is_no_non_ai_fallback_and_it_says_so(home, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cards = [card(i, f"Co{i}", "Data Analyst") for i in range(1, 4)]
    c, ctl = make_client(home, FakeCollector(cards))
    c.put("/api/profile", json=PROFILE_BODY)
    setup_resume(c, home)
    run = wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    assert run["status"] == "finished" and run["progress"]["matching"]["state"] == "skipped"          # discovery still worked
    assert any(e["company"] == "(matching)" and "OPENAI_API_KEY is not set" in e["error"] for e in run["errors"])
    assert c.get("/api/jobs?bucket=all").json()["total"] == 0                                         # nothing was scored: no keyword fallback exists
    assert c.get("/api/jobs/1").json()["match"] is None


def test_ai_failures_in_a_run_leave_the_job_unchecked_and_are_retried_next_run(home):
    from fake_openai import FakeOpenAI
    fake = FakeOpenAI()
    cards = [card(1, "Co1", "Data Analyst FAILME"), card(2, "Co2", "Data Analyst")]            # the fake rejects the first one
    c, ctl = make_client(home, FakeCollector(cards), openai=fake)
    c.put("/api/profile", json=PROFILE_BODY)
    setup_resume(c, home)
    run = wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    m = run["progress"]["matching"]
    assert m["buckets"] == {"accept": 1, "review": 1} and len(fake.requests) == 2
    assert any("1 AI check(s) failed" in e["message"] for e in c.get(f"/api/runs/{run['id']}/events").json())
    jobs = {j["title"]: j for j in c.get("/api/jobs?bucket=all").json()["jobs"]}
    assert jobs["Data Analyst FAILME"]["bucket"] == "review" and jobs["Data Analyst"]["bucket"] == "accept"
    fake.requests.clear()
    run2 = wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    assert len(fake.requests) == 1 and run2["progress"]["matching"]["newly_scored"] == 1      # only the failed job is asked again


def test_rematch_reruns_the_ai_for_exactly_the_selected_jobs_even_when_nothing_changed(home):
    from fake_openai import FakeOpenAI
    fake = FakeOpenAI()
    cards = [card(i, f"Co{i}", "Backend Engineer") for i in range(1, 4)]
    c, ctl = make_client(home, FakeCollector(cards), openai=fake)
    c.put("/api/profile", json=PROFILE_BODY)
    setup_resume(c, home)
    wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    jobs = c.get("/api/jobs?bucket=all").json()
    assert jobs["total"] == 3 and sorted(jobs["all_ids"]) == sorted(j["id"] for j in jobs["jobs"])                      # the view reports every id, for "re-run all in this view"
    fake.requests.clear()

    pick = jobs["jobs"][0]["id"]
    r = c.post("/api/jobs/rematch", json={"ids": [pick]})
    assert r.status_code == 200 and r.json()["jobs"] == 1
    run = wait_done(c, ctl, r.json()["run_id"])
    assert run["kind"] == "rematch" and run["status"] == "finished" and run["progress"]["matching"]["newly_scored"] == 1 and "ats" not in run["progress"]
    assert len(fake.requests) == 1                                                                                      # one job, one phase-1 call; no discovery, nobody else re-asked
    msgs = [e["message"] for e in c.get(f"/api/runs/{run['id']}/events").json()]
    assert any("Re-running the AI matching for 1 selected job(s)" in m for m in msgs)

    # after a profile change the selected job is judged again with the new profile text; the others keep their old verdict until a normal run
    c.put("/api/profile", json={**PROFILE_BODY, "expected_ctc_lpa": {"min": 24, "target": 35}})
    fake.requests.clear()
    run2 = wait_done(c, ctl, c.post("/api/jobs/rematch", json={"ids": [pick]}).json()["run_id"])
    assert len(fake.requests) == 1 and "minimum 24 LPA" in json.dumps(fake.requests[0]["raw_body"]) and run2["status"] == "finished"
    conn = store.connect(home / "data" / "job_search_agent.db")
    n = {r["job_id"]: r["n"] for r in conn.execute("SELECT job_id, COUNT(*) n FROM match_results GROUP BY job_id")}
    assert n[pick] == 3 and all(v == 1 for k, v in n.items() if k != pick)                                              # history kept: three results for the re-run job, one for the rest
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE job_id=?", (pick,)).fetchone()[0] == 3


def test_rematch_rejects_bad_requests_and_a_run_in_progress(home):
    c, ctl = make_client(home, FakeCollector([], hang=threading.Event()))
    c.put("/api/profile", json=PROFILE_BODY)
    assert c.post("/api/jobs/rematch", json={"ids": []}).status_code == 422
    assert c.post("/api/jobs/rematch", json={}).status_code == 422
    assert c.post("/api/jobs/rematch", json={"ids": ["x"]}).status_code == 422
    assert c.post("/api/jobs/rematch", json={"ids": [True]}).status_code == 422
    r = c.post("/api/jobs/rematch", json={"ids": [4242]})
    assert r.status_code == 422 and "not active jobs" in r.json()["detail"]
    body = {"ats": {"enabled": False}, "match": False, "linkedin": {"enabled": True, "queries": [{"keyword": "x", "location": "India"}]}}
    rid = c.post("/api/runs", json=body).json()["run_id"]
    conn = store.connect(home / "data" / "job_search_agent.db")
    conn.execute("INSERT INTO companies(name, careers_url, ats_type, active) VALUES('Acme','https://x','greenhouse',0)")
    conn.execute("INSERT INTO jobs(company_id, external_id, url, title, location, description_text, description_hash, first_seen_at, last_seen_at, status, source) "
                 "VALUES(1,'e1','https://x/1','T','L','d','h','2026-11-18T00:00:00Z','2026-11-18T00:00:00Z','active','ats')")
    conn.commit()
    assert c.post("/api/jobs/rematch", json={"ids": [1]}).status_code == 409                                            # a run is already in progress
    c.post(f"/api/runs/{rid}/cancel")
    wait_done(c, ctl, rid)


def test_a_missing_resume_is_reported_clearly_when_jobs_need_the_resume_check(home):
    cards = [card(i, f"Co{i}", "Data Analyst PHASE1_AMB") for i in range(1, 4)] + [card(9, "Acme", "Backend Engineer")]
    c, ctl = make_client(home, FakeCollector(cards))
    c.put("/api/profile", json=PROFILE_BODY)                                   # note: no resume uploaded, so resume_paths is empty
    run = wait_done(c, ctl, c.post("/api/runs", json=AI_BODY).json()["run_id"])
    assert run["progress"]["matching"]["buckets"] == {"accept": 1, "review": 3}
    err = next(e for e in run["errors"] if e["company"] == "(AI check)")["error"]
    assert "4 job(s) were left 'Not checked'" in err and "no resume_paths configured" in err and "Upload your resume on the Profile tab" in err


def test_discovery_and_schedules_need_the_candidate_name_saved_first(home):
    c, _ = make_client(home)
    body = {"ats": {"enabled": True}, "match": False}
    sched = {"name": "x", "interval_minutes": 120, "enabled": True, "config": body}
    for call in (lambda: c.post("/api/runs", json=body), lambda: c.post("/api/schedules", json=sched)):
        r = call()  # no profile at all
        assert r.status_code == 422 and "update your profile" in r.json()["detail"]
    c.put("/api/profile", json={**PROFILE_BODY, "name": "  "})  # a blank name does not count
    assert c.post("/api/runs", json=body).status_code == 422
    c.put("/api/profile", json=PROFILE_BODY)
    assert c.post("/api/schedules", json=sched).status_code == 200


def test_variant_names_are_free_text_and_never_reach_the_file_path(home):
    c, _ = make_client(home)
    c.put("/api/profile", json=PROFILE_BODY)
    for name in ("Backend (2026) – v2", "../../evil", "a/b\\c"):
        assert c.post("/api/resumes", data={"name": name}, files={"file": ("r.pdf", PDF, "application/pdf")}).status_code == 200
    resumes = c.get("/api/profile").json()["resumes"]
    assert {r["name"] for r in resumes} == {"Backend (2026) – v2", "../../evil", "a/b\\c"}
    assert all(len(r["file"]) == 20 and "/" not in r["file"] for r in resumes)  # 16 hex characters + .pdf
    assert not list(Path(home).glob("*evil*")) and not list((Path(home) / "data").glob("*evil*"))
    assert set(c.get("/api/profile").json()["profile"]["resume_paths"]) == {r["name"] for r in resumes}
    assert c.delete("/api/resumes", params={"name": "../../evil"}).status_code == 200
    assert c.delete("/api/resumes", params={"name": "../../evil"}).status_code == 404
    assert len(c.get("/api/profile").json()["resumes"]) == 2


def test_blank_preferred_locations_never_become_blank_search_locations(home):
    c, _ = make_client(home)
    c.put("/api/profile", json={**PROFILE_BODY, "locations": {**PROFILE_BODY["locations"], "preferred": ["", "  "]}})
    assert c.get("/api/profile").json()["profile"]["locations"]["preferred"] == []
    assert {q["location"] for q in c.get("/api/linkedin/defaults").json()["queries"]} == {"India"}
    li = {"enabled": True, "max_listings": 10000, "queries": [{"keyword": "x", "location": "India"}]}
    assert c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": {**li, "max_listings": 10001}}).status_code == 422
    assert c.post("/api/runs", json={"ats": {"enabled": False}, "match": False, "linkedin": li}).status_code == 200
