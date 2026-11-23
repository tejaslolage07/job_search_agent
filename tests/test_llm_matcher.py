"""The two-phase AI matcher (prompts, privacy guard, reply parsing, engine, scoring loop), against a fake OpenAI."""
from __future__ import annotations

import json
import re

import pytest
from conftest import ROOT, ExplodingLibrary, FakeLibrary, ji
from fake_openai import FakeOpenAI

from job_search_agent import diff, pipeline, store
from job_search_agent.config import Profile
from job_search_agent.matching import llm as L
from job_search_agent.matching.engine import MatchEngine
from job_search_agent.matching.llm import LLMConfigError, LLMJudge, build_judge
from job_search_agent.matching.openai_client import OpenAIChat
from job_search_agent.models import RawJob

PROMPT = ROOT / "config" / "llm_prompt.md"
PROMPT2 = ROOT / "config" / "llm_prompt_resume.md"


def make_judge(profile, mcfg, fake=None, system=None, concurrency=None) -> tuple[LLMJudge, FakeOpenAI]:
    fake = fake or FakeOpenAI()
    sys_p, user = L.load_prompt(PROMPT)
    cfg = mcfg.llm.model_copy(update={"concurrency": concurrency} if concurrency else {})
    client = OpenAIChat("sk-test-1234567890", "gpt-test", transport=fake.transport, sleep=lambda s: None, cache=cfg.cache, cache_ttl=cfg.cache_ttl)
    return LLMJudge(client, (system or sys_p, user), L.load_prompt(PROMPT2), cfg, profile), fake


def engine(profile, mcfg, judge, lib=None) -> MatchEngine:
    eng = MatchEngine(profile, mcfg, lib or FakeLibrary(), judge)
    eng.preload()
    return eng


# ------------------------------------------------------------------------- prompt file, privacy guard, reply parsing ---------------------
def test_phase1_prompt_takes_the_profile_and_posted_pay_and_allows_all_three_answers():
    system, user = L.load_prompt(PROMPT)
    assert "{{profile}}" in user and "{{salary}}" in user and "expected minimum" in system and "{{job_description}}" in user and "{{job_title}}" in user and "{{resume}}" not in user
    for word in L.VERDICTS:
        assert word in system
    assert "match: django" in system and "no match: seniority" in system and "angle brackets" in system and "give just the verdict" in system


def test_phase2_prompt_takes_the_resume_and_forbids_ambiguous():
    system, user = L.load_prompt(PROMPT2)
    assert "{{resume}}" in user and "{{job_description}}" in user and "{{profile}}" not in user
    assert "match: django" in system and "no match: golang" in system and "never ambiguous" in system
    assert not any(line.strip() == "ambiguous" for line in system.splitlines())      # it is not offered as a form to reply with


@pytest.mark.parametrize("reply,expected", [
    ("match: python", ("match", "python")), ("no match: golang", ("no match", "golang")), ("ambiguous", ("ambiguous", "")),
    ("Match: Python.", ("match", "python")), ('"No match: Seniority"', ("no match", "seniority")), ("match - django", ("match", "django")),
    ("no_match: java", ("no match", "java")), ("match\npython", ("match", "python")), ("  ambiguous \n", ("ambiguous", "")), ("**match**: docker", ("match", "docker")),
    ("match", ("match", "")), ("no match: ci/cd", ("no match", "ci/cd")), ("match: full-stack", ("match", "full-stack"))])
def test_split_reply_reads_the_verdict_and_the_word(reply, expected):
    assert L.split_reply(reply) == expected


@pytest.mark.parametrize("reply", ["", "maybe", "match, because the stack fits", "I think this is a match", "not a match", "matches", "mismatch", "match\npython\nextra", "yes: python"])
def test_split_reply_rejects_everything_that_is_not_a_verdict(reply):
    assert L.split_reply(reply) is None


@pytest.mark.parametrize("raw,verdict,reason,clean", [
    ("match: python", "match", "python", True), ("no match: golang", "no match", "golang", True), ("ambiguous", "ambiguous", "", True),
    ("Match: Python.", "match", "python", False), ("match", "match", "", False), ("match: python django", "match", "python django", False),
    ("ambiguous: python", "ambiguous", "python", False), ("match\npython", "match", "python", False)])
def test_is_clean_requires_exactly_the_requested_form(raw, verdict, reason, clean):
    assert L.split_reply(raw) == (verdict, reason) and L.is_clean(raw, verdict, reason) is clean


@pytest.mark.parametrize("reply,word", [("no match: <word>skills</word>", "skills"), ("no match: <location></location>", "location"), ("match: <word>python", "python")])
def test_a_copied_placeholder_is_untagged_and_flagged_for_cleanup(reply, word):
    out = L.parse_text(reply)
    assert out["reason"] == word and out["clean"] is False and out["verdict"] == reply.split(":")[0]


@pytest.mark.parametrize("path", [PROMPT, PROMPT2])
def test_shipped_prompts_contain_no_angle_brackets_a_model_could_copy(path):
    system, user = L.load_prompt(path)
    assert not re.search(r"<\w+>", system)


def test_parse_text_flags_cleanup_and_rejects_off_spec():
    assert L.parse_text("match: python") == {"verdict": "match", "reason": "python", "clean": True, "raw": "match: python"}
    assert L.parse_text("Match: Python.")["clean"] is False and L.parse_text("match")["clean"] is False and L.parse_text("ambiguous")["clean"] is True
    with pytest.raises(ValueError, match="off-spec reply"):
        L.parse_text("It is a match because of Python.")
    with pytest.raises(ValueError, match="empty reply"):
        L.parse_text("  ")


def test_prompt_file_errors_are_clear(tmp_path):
    p = tmp_path / "p.md"
    with pytest.raises(LLMConfigError, match="not found"):
        L.load_prompt(p)
    p.write_text("## SYSTEM\nhi\n")
    with pytest.raises(LLMConfigError, match="USER"):
        L.load_prompt(p)
    p.write_text("## SYSTEM\nhi\n## USER\n{{nope}}\n")
    with pytest.raises(LLMConfigError, match="unknown placeholder"):
        L.load_prompt(p)


def test_render_fills_once_and_never_re_expands_job_text():
    assert L.render("T: {{job_title}} / R: {{resume}}", job_title="Dev {{resume}}", resume="SECRET") == "T: Dev {{resume}} / R: SECRET"


def test_phase2_parsing_rejects_ambiguous_but_phase1_accepts_it():
    assert L.parse_text("ambiguous")["verdict"] == "ambiguous"
    for reply in ("ambiguous", "Ambiguous.", "maybe"):
        with pytest.raises(ValueError, match="off-spec reply"):
            L.parse_text(reply, L.FINAL_VERDICTS)
    assert L.parse_text("no match: golang", L.FINAL_VERDICTS)["verdict"] == "no match"


# ------------------------------------------------------------------------- building the judge ---------------------------------------------
def test_build_judge_explains_what_is_missing(profile, mcfg, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match=r"OPENAI_API_KEY is not set.*\.env"):
        build_judge(profile, mcfg.llm, ROOT / "config")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890")
    with pytest.raises(LLMConfigError, match="prompt file not found"):
        build_judge(profile, mcfg.llm, tmp_path)
    j = build_judge(profile, mcfg.llm, ROOT / "config")
    assert j.client.model == "gpt-6-luna" and len(j.fingerprint) == 12


def test_model_precedence_env_beats_config_and_an_explicit_model_beats_both(mcfg, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert L.resolve_model(mcfg.llm) == "gpt-6-luna"
    monkeypatch.setenv("OPENAI_MODEL", "model-from-env")
    assert L.resolve_model(mcfg.llm) == "model-from-env"
    assert L.make_client(mcfg.llm, "sk-test-1234567890", model="explicit").model == "explicit"


def test_the_fingerprint_changes_with_prompt_model_and_effort_but_not_with_the_job(profile, mcfg):
    a, _ = make_judge(profile, mcfg)
    b, _ = make_judge(profile, mcfg)
    c, _ = make_judge(profile, mcfg, system=a.phase1[0] + " Be strict.")
    d = LLMJudge(OpenAIChat("sk-test-1234567890", "another-model"), a.phase1, a.phase2, mcfg.llm, profile)
    e = LLMJudge(OpenAIChat("sk-test-1234567890", "gpt-test"), a.phase1, a.phase2, mcfg.llm.model_copy(update={"reasoning_effort": "high"}), profile)
    f = LLMJudge(OpenAIChat("sk-test-1234567890", "gpt-test"), a.phase1, (a.phase2[0] + " Be strict.", a.phase2[1]), mcfg.llm, profile)
    assert a.fingerprint == b.fingerprint and len({a.fingerprint, c.fingerprint, d.fingerprint, e.fingerprint, f.fingerprint}) == 5


def test_the_llm_section_does_not_feed_the_config_hash(mcfg):
    base = mcfg.config_hash()
    assert mcfg.model_copy(update={"llm": mcfg.llm.model_copy(update={"model": "other", "concurrency": 9})}).config_hash() == base


# ------------------------------------------------------------------------- engine ---------------------------------------------------------
JOB = dict(title="Backend Engineer", desc="Python Django FastAPI role. 2+ years of experience.")


def test_phase1_match_and_no_match_never_touch_the_resume(profile, mcfg):
    judge, fake = make_judge(profile, mcfg)
    lib = FakeLibrary()
    eng = engine(profile, mcfg, judge, lib)
    yes = eng.score(ji(**JOB))
    no = eng.score(ji(title="Backend Engineer", desc="VERDICT_NO Java only."))
    assert (yes.bucket, yes.phase, yes.method, yes.final_score, yes.resume_hash) == ("accept", 1, "llm-profile", 100.0, None)
    assert (no.bucket, no.phase, no.method, no.final_score) == ("reject", 1, "llm-profile", 0.0)
    assert [r["phase"] for r in fake.requests] == [1, 1]
    assert all("RESUME:" not in r["body"]["messages"][1]["content"] for r in fake.requests)   # the resume was not even in the prompt


@pytest.mark.parametrize("marker,bucket,verdict", [("PHASE1_AMB", "accept", "match"), ("PHASE1_AMB P2_NO", "reject", "no match")])
def test_ambiguous_goes_to_the_resume_and_phase2_decides(profile, mcfg, marker, bucket, verdict):
    judge, fake = make_judge(profile, mcfg)
    out = engine(profile, mcfg, judge).score(ji(title="Data Analyst", desc=f"{marker} analytics work"))
    assert (out.bucket, out.phase, out.method) == (bucket, 2, "llm-resume") and out.resume_hash == "deadbeef" and out.resume_variant == "backend"
    assert [r["phase"] for r in fake.requests] == [1, 2]
    calls = out.inputs["llm"]["calls"]
    assert [c["verdict"] for c in calls] == ["ambiguous", verdict] and out.inputs["llm"]["fingerprint"] == judge.fingerprint
    assert calls[1]["model"] == "gpt-test" and calls[1]["tokens_in"] == 1000
    assert "Python Django FastAPI Kubernetes LLM backend engineer" in fake.requests[1]["body"]["messages"][1]["content"]   # the resume went in place of the profile
    assert "CANDIDATE PROFILE DETAILS" not in fake.requests[1]["body"]["messages"][1]["content"]
    assert any(r.rule == "llm_resume" and verdict in r.detail for r in out.reasons)


def test_phase2_can_never_end_ambiguous_an_ambiguous_reply_is_a_failure_to_retry(profile, mcfg):
    judge, _ = make_judge(profile, mcfg)
    out = engine(profile, mcfg, judge).score(ji(title="Data Analyst", desc="VERDICT_AMB analytics work"))
    assert out.method == "llm-failed" and out.bucket == "review" and out.phase == 2
    assert "off-spec reply" in out.inputs["llm"]["error"] and any(r.rule == "llm_failed" for r in out.reasons)


def test_with_several_resumes_any_match_is_enough_and_all_must_say_no_to_reject(profile, mcfg):
    from pathlib import Path

    from job_search_agent.matching.resumes import ResumeVariant

    class Two(FakeLibrary):
        def variants(self):
            self._variants = [ResumeVariant("a", Path("a.pdf"), "ha", "resume A P2_NO"), ResumeVariant("b", Path("b.pdf"), "hb", "resume B")]
            return self._variants

    judge, fake = make_judge(profile, mcfg)
    out = engine(profile, mcfg, judge, Two()).score(ji(title="Data Analyst", desc="PHASE1_AMB analytics"))
    assert (out.bucket, out.resume_variant) == ("accept", "b") and [r["phase"] for r in fake.requests] == [1, 2, 2]
    judge2, fake2 = make_judge(profile, mcfg)
    out2 = engine(profile, mcfg, judge2, Two()).score(ji(title="Data Analyst", desc="PHASE1_AMB P2_NO analytics"))
    assert out2.bucket == "reject" and [r["phase"] for r in fake2.requests] == [1, 2, 2]


@pytest.mark.parametrize("marker,phase,fragment", [("FAILME", 1, "this job broke the fake"), ("OFFSPEC", 1, "off-spec reply")])
def test_when_phase1_fails_the_job_is_unscored_and_says_why(profile, mcfg, marker, phase, fragment):
    judge, _ = make_judge(profile, mcfg)
    out = engine(profile, mcfg, judge).score(ji(title="Backend Engineer", desc=f"{marker} Python"))
    assert (out.method, out.bucket, out.phase, out.final_score) == ("llm-failed", "review", phase, 50.0)
    assert fragment in out.inputs["llm"]["error"] and any(r.rule == "llm_failed" and fragment in r.detail for r in out.reasons)


def test_when_the_resume_cannot_be_read_only_the_ambiguous_jobs_fail(profile, mcfg):
    from job_search_agent.matching.resumes import ResumeLibrary

    judge, _ = make_judge(profile, mcfg)
    eng = MatchEngine(profile, mcfg, ResumeLibrary({"backend": "/nonexistent.pdf"}), judge)
    assert eng.preload() and "not found" in eng.resume_error
    assert eng.score(ji(**JOB)).bucket == "accept"                                           # phase 1 needs no resume
    out = eng.score(ji(title="Data Analyst", desc="PHASE1_AMB analytics"))
    assert out.method == "llm-failed" and "resume could not be read" in out.inputs["llm"]["error"] and out.resume_hash is None


def test_phase1_sends_job_posted_pay_and_profile_pay_and_phase2_sends_the_resume_but_never_notice_period_or_work_authorization(profile, mcfg):
    p = profile.model_copy(update={"notice_period_days": 45, "work_authorization": "H1B holder"})
    judge, fake = make_judge(p, mcfg)
    eng = engine(p, mcfg, judge, FakeLibrary("Python Django developer. I built OCR pipelines."))
    for i in range(4):
        eng.score(ji(title=f"Data Analyst {i}", salary="$120,000 - $150,000", desc=f"PHASE1_AMB Job {i}: Python analytics work in Bengaluru. 2+ years of experience."))
    eng.score(ji(**JOB))
    assert [r["phase"] for r in fake.requests] == [1, 2] * 4 + [1]
    system1, _ = L.load_prompt(PROMPT)
    system2, _ = L.load_prompt(PROMPT2)
    for req in fake.requests:
        blob = json.dumps(req["body"])
        user = req["body"]["messages"][1]["content"]
        assert req["body"]["messages"][0]["content"] == (system1 if req["phase"] == 1 else system2) and set(req["body"]) == {"model", "messages", "prompt_cache_options", "reasoning_effort"}
        assert ("Must-have skills: Python, Django, FastAPI" in blob) == (req["phase"] == 1)                      # profile details only in phase 1
        assert ("I built OCR pipelines" in blob) == (req["phase"] == 2)                                         # resume only in phase 2
        if req["phase"] == 1:
            assert "Current CTC: 20 LPA" in user and "Expected CTC: minimum 28 LPA, target 35 LPA" in user and "USD to INR rate for converting dollar salaries: 88" in user
            assert ("POSTED PAY: $120,000 - $150,000" in user) or ("POSTED PAY: not stated" in user)             # the job's posted pay is a field of its own
        else:
            assert "CTC" not in user                                                                           # phase 2 compares the resume only
        for word in ("notice", "h1b", "work_authorization", "resume.pdf"):
            assert word not in blob.lower()


# ------------------------------------------------------------------------- the scoring loop (database) ----------------------------------------
def add_jobs(conn, company, n_ambiguous: int, n_clear: int = 2, marks: dict[int, str] | None = None) -> list:
    marks = marks or {}
    jobs = [RawJob(external_id=f"a{i}", url=f"https://x/a{i}", title=f"Data Analyst {i}", location="Bengaluru",
                   description_text=f"PHASE1_AMB {marks.get(i, '')} Job {i}: Python and Django analytics work. 2+ years of experience.") for i in range(n_ambiguous)]
    jobs += [RawJob(external_id=f"s{i}", url=f"https://x/s{i}", title="Backend Engineer", location="Bengaluru",
                    description_text=f"Python Django FastAPI role {i}. 2+ years of experience.") for i in range(n_clear)]
    rid = store.start_run(conn)
    diff.apply_plan(conn, rid, company.id, diff.compute_plan({}, jobs))
    return conn.execute("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id").fetchall()


def methods(conn) -> dict[str, int]:
    rows = conn.execute("SELECT method, COUNT(*) n FROM match_results GROUP BY method").fetchall()
    return {r["method"]: r["n"] for r in rows}


def test_jobs_are_judged_in_parallel_up_to_the_configured_concurrency_and_saved(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=6)
    judge, fake = make_judge(profile, mcfg, FakeOpenAI(delay=0.25), concurrency=3)
    events: list[dict] = []
    n = pipeline.score_jobs(conn, MatchEngine(profile, mcfg, FakeLibrary(), judge), profile, mcfg, rows, None, on_progress=events.append)
    assert n == 8 and fake.peak() == 3 and len(fake.requests) == 8 + 6             # every job asked in phase 1; the 6 ambiguous ones again in phase 2
    assert methods(conn) == {"llm-resume": 6, "llm-profile": 2}
    llm_events = [e for e in events if e["event"] == "llm"]
    assert llm_events[0] == {"event": "llm", "done": 0, "total": 8} and llm_events[-1] == {"event": "llm", "done": 8, "total": 8}
    saved = json.loads(conn.execute("SELECT reasons_json FROM match_results WHERE method='llm-resume' LIMIT 1").fetchone()[0])
    assert [c["phase"] for c in saved["inputs"]["llm"]["calls"]] == [1, 2] and any(r["rule"] == "llm_resume" for r in saved["reasons"])


def test_results_stay_cached_until_the_prompt_or_model_changes(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=3)
    judge, fake = make_judge(profile, mcfg)
    run = lambda j: pipeline.score_jobs(conn, MatchEngine(profile, mcfg, FakeLibrary(), j), profile, mcfg, rows, None)  # noqa: E731
    assert run(judge) == 5
    before = len(fake.requests)
    assert run(judge) == 0 and len(fake.requests) == before                      # nothing changed: nothing re-judged
    stricter, fake2 = make_judge(profile, mcfg, system=judge.phase1[0] + " Be strict.")
    assert run(stricter) == 5 and len(fake2.requests) == 5 + 3                   # a new prompt: everything is asked again
    assert run(stricter) == 0


def test_the_resume_is_read_once_and_only_when_there_is_work(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=4)
    lib = FakeLibrary()
    judge, _ = make_judge(profile, mcfg)
    assert pipeline.score_jobs(conn, MatchEngine(profile, mcfg, lib, judge), profile, mcfg, rows, None) == 6 and lib.load_count == 1
    assert pipeline.score_jobs(conn, MatchEngine(profile, mcfg, lib, judge), profile, mcfg, rows, None) == 0 and lib.load_count == 1   # nothing to do: not read again


def test_failed_ai_calls_are_retried_on_the_next_run_and_only_those(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=5, marks={1: "FAILME", 3: "FAILME"})
    judge, fake = make_judge(profile, mcfg)
    eng = lambda: MatchEngine(profile, mcfg, FakeLibrary(), judge)  # noqa: E731
    assert pipeline.score_jobs(conn, eng(), profile, mcfg, rows, None) == 7
    assert methods(conn) == {"llm-resume": 3, "llm-failed": 2, "llm-profile": 2}
    fake.requests.clear()
    assert pipeline.score_jobs(conn, eng(), profile, mcfg, rows, None) == 2 and len(fake.requests) == 2   # only the failed ones are asked again (and fail again)
    assert methods(conn)["llm-failed"] == 4                                                              # history is kept; the latest result per job is what counts


def test_a_cancelled_run_keeps_finished_results_and_the_rest_are_done_next_time(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=0, n_clear=8)
    judge, fake = make_judge(profile, mcfg, FakeOpenAI(delay=0.05), concurrency=1)
    eng = lambda: MatchEngine(profile, mcfg, FakeLibrary(), judge)  # noqa: E731
    first = pipeline.score_jobs(conn, eng(), profile, mcfg, rows, None, cancelled=lambda: len(fake.requests) >= 3)
    assert 1 <= first < 8
    assert pipeline.score_jobs(conn, eng(), profile, mcfg, rows, None) == 8 - first          # exactly the unfinished ones
    assert methods(conn) == {"llm-profile": 8}


# ------------------------------------------------------------------------- every call is stored in full (llm_calls) -----------------------------
def calls_for(conn, title_part: str) -> list:
    return conn.execute("SELECT c.* FROM llm_calls c JOIN jobs j ON j.id=c.job_id WHERE j.title LIKE ? ORDER BY c.id", (f"%{title_part}%",)).fetchall()


def test_every_call_is_saved_with_its_entire_request_and_response_and_token_counts(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=1, n_clear=1)
    judge, fake = make_judge(profile, mcfg)
    pipeline.score_jobs(conn, MatchEngine(profile, mcfg, FakeLibrary("Python Django developer. I built OCR pipelines."), judge), profile, mcfg, rows, None)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == len(fake.requests) == 3                  # 1 clear job (phase 1) + 1 ambiguous (phases 1 and 2)
    clear = calls_for(conn, "Backend Engineer")
    assert len(clear) == 1
    c = clear[0]
    assert (c["phase"], c["status"], c["error"], c["tokens_in"], c["tokens_out"], c["reasoning_tokens"], c["retries"], c["model"]) == (1, "ok", None, 1000, 12, 5, 0, "gpt-test")
    req, resp = json.loads(c["request_json"]), json.loads(c["response_json"])
    sent = next(r["body"] for r in fake.requests if "JOB TITLE: Backend Engineer" in r["body"]["messages"][1]["content"])
    sent_raw = next(r["raw_body"] for r in fake.requests if "JOB TITLE: Backend Engineer" in r["body"]["messages"][1]["content"])
    assert req == sent_raw and sent["messages"][1]["content"] == "".join(p["text"] for p in sent_raw["messages"][1]["content"])   # the stored request is exactly what was sent
    assert req["messages"][0]["role"] == "system" and "Current CTC: 20 LPA" in c["request_json"] and "sk-" not in c["request_json"]   # never the API key
    assert resp["choices"][0]["message"]["content"] == "match: python" and resp["usage"]["prompt_tokens"] == 1000 and resp["usage"]["completion_tokens"] == 12
    amb = calls_for(conn, "Data Analyst")
    assert [(a["phase"], a["resume_variant"], a["status"]) for a in amb] == [(1, None, "ok"), (2, "backend", "ok")]
    assert "I built OCR pipelines" in amb[1]["request_json"]
    m = conn.execute("SELECT id FROM match_results ORDER BY id").fetchall()
    assert {r["match_id"] for r in amb} <= {x["id"] for x in m} and len({r["match_id"] for r in amb}) == 1          # linked to the result they produced
    assert sum(r["tokens_in"] for r in conn.execute("SELECT * FROM llm_calls")) == 3000


def test_failed_calls_are_saved_too_with_the_error_and_whatever_came_back(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=0, n_clear=0)
    jobs = [RawJob(external_id="f", url="https://x/f", title="Backend FAILME", location="Bengaluru", description_text="FAILME Python"),
            RawJob(external_id="o", url="https://x/o", title="Backend OFFSPEC", location="Bengaluru", description_text="OFFSPEC Python"),
            RawJob(external_id="a", url="https://x/a", title="Backend AMBIG", location="Bengaluru", description_text="VERDICT_AMB Python")]
    diff.apply_plan(conn, store.start_run(conn), company.id, diff.compute_plan({}, jobs))
    rows = conn.execute("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id").fetchall()
    judge, _ = make_judge(profile, mcfg)
    pipeline.score_jobs(conn, MatchEngine(profile, mcfg, FakeLibrary(), judge), profile, mcfg, rows, None)
    api = calls_for(conn, "FAILME")[0]                                                                              # HTTP 400: nothing was spent, but the request and error body are kept
    assert (api["phase"], api["status"], api["tokens_in"]) == (1, "error", 0) and "this job broke the fake" in api["error"]
    assert json.loads(api["request_json"])["messages"] and "this job broke the fake" in json.dumps(json.loads(api["response_json"]))
    off = calls_for(conn, "OFFSPEC")[0]                                                                             # the call worked and was paid for, but the reply is not a verdict
    assert (off["status"], off["tokens_in"], off["tokens_out"]) == ("error", 1000, 12) and "off-spec reply" in off["error"]
    assert "I think this is a match" in off["response_json"]
    amb = calls_for(conn, "AMBIG")                                                                                  # ambiguous in phase 1 is fine; ambiguous in phase 2 is an error row
    assert [(a["phase"], a["status"]) for a in amb] == [(1, "ok"), (2, "error")] and "off-spec reply" in amb[1]["error"]
    assert conn.execute("SELECT COUNT(*) FROM match_results WHERE method='llm-failed'").fetchone()[0] == 3


def test_a_retried_run_adds_new_call_rows_and_keeps_the_old_ones(conn, company, profile, mcfg):
    rows = add_jobs(conn, company, n_ambiguous=0, n_clear=0)
    diff.apply_plan(conn, store.start_run(conn), company.id, diff.compute_plan({}, [RawJob(external_id="f", url="https://x/f", title="Backend FAILME", location="Bengaluru", description_text="FAILME")]))
    rows = conn.execute("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id").fetchall()
    judge, _ = make_judge(profile, mcfg)
    for _ in range(2):
        pipeline.score_jobs(conn, MatchEngine(profile, mcfg, FakeLibrary(), judge), profile, mcfg, rows, None)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE status='error'").fetchone()[0] == 2


# ------------------------------------------------------------------------- cacheable layout ---------------------------------------------------
def test_phase1_requests_for_different_jobs_share_one_long_identical_start(profile, mcfg):
    judge, _ = make_judge(profile, mcfg)
    a = judge.profile_prompt("Backend Engineer", "Acme", "Bengaluru", "Python role", "")
    b = judge.profile_prompt("Data Analyst", "Zeta", "Pune", "Excel work", "$100k")
    common = len(__import__("os").path.commonprefix([a, b]))
    assert "USD to INR rate" in a[:common]                                                                            # the whole profile is inside the shared start
    assert "JOB TITLE" in a[common - 20:common + 20]                                                                  # ...and the first per-job text comes right after it
    assert a.index("CANDIDATE PROFILE DETAILS") < a.index("JOB TITLE")


def test_phase2_puts_the_resume_before_the_job_so_the_resume_is_part_of_the_shared_start(profile, mcfg):
    judge, _ = make_judge(profile, mcfg)
    a = judge.resume_prompt("Backend Engineer", "Acme", "Bengaluru", "Python role", "MY RESUME TEXT")
    b = judge.resume_prompt("Data Analyst", "Zeta", "Pune", "Excel work", "MY RESUME TEXT")
    common = len(__import__("os").path.commonprefix([a, b]))
    assert "MY RESUME TEXT" in a[:common] and a.index("RESUME:") < a.index("JOB TITLE")


def test_cache_write_tokens_are_read_from_the_usage_block_and_stored(conn, company, profile, mcfg):
    from job_search_agent.matching.openai_client import OpenAIChat
    reply = OpenAIChat("sk-test-1234567890", "m")._parse({"choices": [{"message": {"content": "match: x"}, "finish_reason": "stop"}], "model": "m",
                                                          "usage": {"prompt_tokens": 1787, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 700}}})
    assert (reply.cached_tokens, reply.cache_write_tokens) == (1000, 700)
    rows = add_jobs(conn, company, n_ambiguous=0, n_clear=1)
    store.insert_llm_call(conn, None, None, rows[0]["id"], {"phase": 1, "status": "ok", "tokens_in": 1787, "tokens_out": 9, "cached_tokens": 1000, "cache_write_tokens": 700,
                                                           "request": {}, "response": {}})
    r = conn.execute("SELECT cached_tokens, cache_write_tokens FROM llm_calls").fetchone()
    assert (r["cached_tokens"], r["cache_write_tokens"]) == (1000, 700)


def test_explicit_cache_mode_marks_only_the_shared_start_and_implicit_mode_sends_nothing_special(profile, mcfg):
    judge, fake = make_judge(profile, mcfg)
    judge.judge_profile("Backend Engineer", "Acme", "Bengaluru", "Python role")
    body = fake.requests[0]["raw_body"]
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    shared, tail = body["messages"][1]["content"]
    assert shared["prompt_cache_breakpoint"] == {"mode": "explicit"} and "prompt_cache_breakpoint" not in tail
    assert "CANDIDATE PROFILE DETAILS" in shared["text"] and "USD to INR rate" in shared["text"] and "JOB TITLE: Backend Engineer" in tail["text"]   # per-job text is after the mark
    assert "\x00" not in json.dumps(body)
    judge2, fake2 = make_judge(profile, mcfg.model_copy(update={"llm": mcfg.llm.model_copy(update={"cache": "implicit"})}))
    judge2.judge_profile("Backend Engineer", "Acme", "Bengaluru", "Python role")
    body2 = fake2.requests[0]["raw_body"]
    assert "prompt_cache_options" not in body2 and isinstance(body2["messages"][1]["content"], str)


def test_job_text_cannot_fake_the_cache_marker(profile, mcfg):
    judge, fake = make_judge(profile, mcfg)
    judge.judge_profile("Backend\x00 Engineer", "Acme", "Bengaluru", "Python \x00 role")
    shared, tail = fake.requests[0]["raw_body"]["messages"][1]["content"]
    assert "Python  role" in tail["text"] and "\x00" not in shared["text"] + tail["text"]


def test_the_shipped_prompts_have_a_cache_marker_after_the_shared_part_and_the_shared_part_is_long_enough(profile, mcfg):
    judge, _ = make_judge(profile, mcfg)
    for parts, system in ((judge.profile_parts("T", "C", "L", "D"), judge.phase1[0]), (judge.resume_parts("T", "C", "L", "D", "R" * 4000), judge.phase2[0])):
        assert len(parts) == 2 and "JOB TITLE" not in parts[0] and "JOB TITLE" in parts[1]
        assert len(system) + len(parts[0]) > 4400          # about 1,100 tokens at ~4 characters per token: above OpenAI's 1,024-token minimum for caching


def test_phase1_thinks_not_at_all_and_phase2_thinks_a_little_by_default_and_each_phase_has_its_own_setting(profile, mcfg):
    assert (mcfg.llm.reasoning_effort, mcfg.llm.resume_reasoning_effort) == ("none", "low")        # what ships in matching.yaml
    judge, fake = make_judge(profile, mcfg)
    judge.judge_profile("T", "C", "L", "PHASE1_AMB Python")
    judge.judge_resume("T", "C", "L", "Python", "RESUME")
    assert [(r["phase"], r["body"]["reasoning_effort"]) for r in fake.requests] == [(1, "none"), (2, "low")]
    blank = mcfg.llm.model_copy(update={"reasoning_effort": "", "resume_reasoning_effort": "high"})
    judge2, fake2 = make_judge(profile, mcfg.model_copy(update={"llm": blank}))
    judge2.judge_profile("T", "C", "L", "Python")
    judge2.judge_resume("T", "C", "L", "Python", "RESUME")
    assert "reasoning_effort" not in fake2.requests[0]["body"] and fake2.requests[1]["body"]["reasoning_effort"] == "high"   # blank = send nothing (the model's default)


def test_changing_either_phases_effort_changes_the_fingerprint_so_verdicts_are_redone(profile, mcfg):
    base, _ = make_judge(profile, mcfg)
    p1, _ = make_judge(profile, mcfg.model_copy(update={"llm": mcfg.llm.model_copy(update={"reasoning_effort": "low"})}))
    p2, _ = make_judge(profile, mcfg.model_copy(update={"llm": mcfg.llm.model_copy(update={"resume_reasoning_effort": "high"})}))
    assert len({base.fingerprint, p1.fingerprint, p2.fingerprint}) == 3


def test_a_normal_resume_is_never_cut_the_ceiling_is_only_a_safety_net_and_null_means_no_limit(profile, mcfg):
    assert mcfg.llm.max_resume_chars == 50000
    long_resume = "SKILL " * 5000 + "THE-LAST-LINE"                    # 30,000 characters: far beyond a real resume's cut-off in the old 6,000 / 10,000 settings
    judge, fake = make_judge(profile, mcfg)
    judge.judge_resume("T", "C", "L", "Python", long_resume)
    assert "THE-LAST-LINE" in json.dumps(fake.requests[0]["raw_body"])                    # all of it was sent
    ceiling = mcfg.model_copy(update={"llm": mcfg.llm.model_copy(update={"max_resume_chars": 1000})})
    judge2, fake2 = make_judge(profile, ceiling)
    judge2.judge_resume("T", "C", "L", "Python", long_resume)
    assert "THE-LAST-LINE" not in json.dumps(fake2.requests[0]["raw_body"])               # a ceiling still cuts what is above it
    unlimited = mcfg.model_copy(update={"llm": mcfg.llm.model_copy(update={"max_resume_chars": None})})
    judge3, fake3 = make_judge(profile, unlimited)
    judge3.judge_resume("T", "C", "L", "Python", "X" * 200000 + "THE-END")
    assert "THE-END" in json.dumps(fake3.requests[0]["raw_body"])                         # null: no limit at all
