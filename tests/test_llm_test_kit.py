"""Tests for llm_test_kit/run_test.py (the real two-phase matcher on stored jobs) against a fake OpenAI server (no real calls, no real key).
The prompt parsing, privacy guard and judge it relies on are shared with the matcher and tested in tests/test_llm_matcher.py."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE, ROOT
from fake_openai import FakeOpenAI

from job_search_agent import store
from job_search_agent.matching import llm as shared

spec = importlib.util.spec_from_file_location("run_test", ROOT / "llm_test_kit" / "run_test.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["run_test"] = rt  # dataclasses look the module up by name
spec.loader.exec_module(rt)  # type: ignore[union-attr]

KEY = "sk-test-FAKEKEY1234567890"


@pytest.fixture
def kit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "data").mkdir()
    shutil.copy(ROOT / "config" / "matching.yaml", home / "config" / "matching.yaml")
    shutil.copy(ROOT / "config" / "llm_prompt.md", home / "config" / "llm_prompt.md")
    shutil.copy(ROOT / "config" / "llm_prompt_resume.md", home / "config" / "llm_prompt_resume.md")
    (home / "config" / "profile.yaml").write_text(yaml.safe_dump(PROFILE))
    conn = store.connect(home / "data" / "job_search_agent.db")
    conn.execute("INSERT INTO companies(name, careers_url, ats_type, active) VALUES('Acme [LinkedIn]','https://x','linkedin',0)")
    for i in range(1, 11):
        marker = {3: "FAILME ", 7: "PHASE1_AMB ", 8: "PHASE1_AMB P2_NO ", 9: "VERDICT_NO ", 10: "VERDICT_NO "}.get(i, "")
        desc = marker + f"Job {i}: build backend services in Python and Django. " * 8
        conn.execute("INSERT INTO jobs(company_id, external_id, url, title, location, posted_at, description_text, description_hash, first_seen_at, last_seen_at, status, source) "
                     "VALUES(1,?,?,?,?,?,?,'h','2026-11-18T00:00:00Z','2026-11-18T00:00:00Z','active',?)",
                     (f"e{i}", f"https://l/{i}", f"Engineer {i}", "Bengaluru", "2026-11-18T00:00:00Z", desc, "linkedin" if i % 2 else "ats"))
        store.insert_match(conn, None, i, stage_decided=1, stage1_score=None, stage1_confidence=None, final_score=100 if i <= 8 else 0, bucket="accept" if i <= 8 else "reject",
                           reasons_json=json.dumps({"reasons": [], "inputs": {}}), resume_variant=None, resume_hash=None, profile_hash="p", matching_config_hash="c",
                           method="llm-profile", job_hash="j")
    conn.execute("UPDATE jobs SET description_text='short' WHERE id=2")  # too short to test on
    conn.commit()
    conn.close()
    resume = tmp_path / "resume.txt"
    resume.write_text("Python Django FastAPI developer with backend experience.")
    fake = FakeOpenAI(delay=0.4)
    monkeypatch.setattr(rt, "TRANSPORT", fake.transport)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    return {"home": home, "resume": resume, "fake": fake, "out": tmp_path / "out", "tmp": tmp_path}


def run_kit(kit, *extra, prompt: Path | None = None) -> int:
    argv = ["--home", str(kit["home"]), "--resume-file", str(kit["resume"]), "--out-dir", str(kit["out"])]
    if prompt:
        argv += ["--prompt", str(prompt)]
    return rt.main([*argv, *extra])


def set_llm(kit, **values) -> None:
    """Change the `llm:` section of the home's config/matching.yaml (where model, effort, retries live)."""
    p = kit["home"] / "config" / "matching.yaml"
    cfg = yaml.safe_load(p.read_text())
    cfg["llm"].update(values)
    p.write_text(yaml.safe_dump(cfg))


def mark(kit, job_id: int, marker: str) -> None:
    conn = store.connect(kit["home"] / "data" / "job_search_agent.db")
    conn.execute("UPDATE jobs SET description_text = ? || ' ' || description_text WHERE id = ?", (marker, job_id))
    conn.commit()


def results(kit) -> dict[int, dict]:
    return {r["job_id"]: r for r in json.loads((kit["out"] / "results.json").read_text())["results"]}


# ---------------------------------------------------------------- the kit is a front end over the matcher's own code ----------------------------------------------------------
def test_the_kit_uses_the_matchers_own_engine_judge_and_buckets():
    assert rt.KitError is shared.LLMConfigError and rt.LLMJudge is shared.LLMJudge and rt.MatchEngine.__module__.endswith("matching.engine")
    assert rt.BUCKET_TO_VERDICT["accept"] == "match" and rt.BUCKET_TO_VERDICT["reject"] == "no match"
    assert not (ROOT / "llm_test_kit" / "prompt.md").exists()                      # no prompt copy in the kit: config/llm_prompt.md and config/llm_prompt_resume.md only


def test_the_kit_sends_the_matchers_prompt_files_profile_details_and_the_resume(kit):
    assert run_kit(kit, "--ids", "1,7") == 0
    reqs = {(r["phase"], "Engineer 1" in r["body"]["messages"][1]["content"]): r for r in kit["fake"].requests}
    system1, _ = shared.load_prompt(kit["home"] / "config" / "llm_prompt.md")
    system2, _ = shared.load_prompt(kit["home"] / "config" / "llm_prompt_resume.md")
    p1 = reqs[(1, True)]
    assert p1["body"]["messages"][0]["content"] == system1
    user1 = p1["body"]["messages"][1]["content"]
    assert "Must-have skills: Python, Django, FastAPI" in user1 and "JOB TITLE: Engineer 1" in user1 and "Acme" in user1 and "Python Django FastAPI developer" not in user1
    p2 = reqs[(2, False)]                                                           # job 7 was ambiguous in phase 1
    assert p2["body"]["messages"][0]["content"] == system2 and "Python Django FastAPI developer" in p2["body"]["messages"][1]["content"]
    assert "Must-have skills" not in p2["body"]["messages"][1]["content"]
    saved = (kit["out"] / "prompts" / "job_1.txt").read_text()
    assert system1 in saved and user1 in saved and system2 in saved                  # the saved file holds exactly what is (or would be) sent


def test_a_prompt_edited_in_config_is_used_on_the_next_run(kit):
    p = kit["home"] / "config" / "llm_prompt.md"
    p.write_text(p.read_text().replace("You decide if ONE job fits ONE candidate, using the candidate's profile details.", "You are a very strict hiring manager."))
    assert run_kit(kit, "--ids", "1") == 0
    assert "very strict hiring manager" in kit["fake"].requests[0]["body"]["messages"][0]["content"]


def test_default_concurrency_comes_from_settings_yaml_and_is_four():
    assert yaml.safe_load((ROOT / "llm_test_kit" / "settings.yaml").read_text())["concurrency"] == 8


# ---------------------------------------------------------------- job selection ----------------------------------------------------------
def test_selection_default_skips_short_descriptions_and_is_reproducible(kit):
    conn = store.connect(kit["home"] / "data" / "job_search_agent.db")
    s = {"select": "any", "limit": 50, "seed": 1, "min_description_chars": 200}
    ids = [j["id"] for j in rt.select_jobs(conn, s, None)]
    assert sorted(ids) == [1, 3, 4, 5, 6, 7, 8, 9, 10]
    assert ids == [j["id"] for j in rt.select_jobs(conn, s, None)]
    assert len(rt.select_jobs(conn, {**s, "limit": 3}, None)) == 3
    assert {j["source"] for j in rt.select_jobs(conn, {**s, "source": "ats"}, None)} == {"ats"}
    assert {j["id"] for j in rt.select_jobs(conn, {**s, "select": "reject"}, None)} == {9, 10}
    assert all(j["company"] == "Acme" for j in rt.select_jobs(conn, s, None))
    assert [j["id"] for j in rt.select_jobs(conn, s, [5, 1])] == [5, 1]
    with pytest.raises(rt.KitError, match="not found"):
        rt.select_jobs(conn, s, [2])
    with pytest.raises(rt.KitError, match="must be one of"):
        rt.select_jobs(conn, {**s, "select": "stage2"}, None)


# ---------------------------------------------------------------- end to end (fake OpenAI) ----------------------------------------------------------
def test_concurrency_is_four_and_results_are_shown_and_saved(kit, capsys):
    assert run_kit(kit, "--ids", "1,3,4,5,6,7,8,9", "--concurrency", "4") == 1       # job 3 is rigged to fail
    out = capsys.readouterr().out
    peak = kit["fake"].peak()
    assert peak == 4, f"the fake server saw {peak} requests in flight at once; kit output was:\n{out}"
    assert "concurrency: configured 4, peak jobs in flight 4" in out
    assert out.count("| MATCH - ") == 5 and out.count("| NO MATCH - ") == 2 and out.count("FAILED after") == 1
    assert "final verdicts: match 5, no match 2" in out and "succeeded: 7 | failed: 1" in out
    assert "phase 1 answered: match 4, no match 1, ambiguous 2  ->  6 went on to the resume check (13 API calls in total)" in out
    assert "MATCH - strongest parameter: python  [profile said match -> resume]" in out and "NO MATCH - strongest parameter: golang  [profile details]" in out
    assert "[profile said ambiguous -> resume]" in out
    assert "strongest parameter behind 'match': python x5" in out and "strongest parameter behind 'no match': golang x2" in out
    rows = results(kit)
    assert len(rows) == 8 and sum(r["ok"] for r in rows.values()) == 7
    assert {r["final_verdict"] for r in rows.values() if r["ok"]} == {"match", "no match"}                  # never ambiguous
    assert (rows[7]["phase1_verdict"], rows[7]["final_verdict"], rows[7]["decided_by_phase"], rows[7]["api_calls"]) == ("ambiguous", "match", 2, 2)
    assert (rows[8]["phase1_verdict"], rows[8]["final_verdict"], rows[8]["decided_by_phase"]) == ("ambiguous", "no match", 2)
    assert (rows[1]["phase1_verdict"], rows[1]["final_verdict"], rows[1]["decided_by_phase"], rows[1]["api_calls"], rows[1]["stored_result"]) == ("match", "match", 2, 2, "match")     # a phase-1 match is confirmed by the resume
    assert all(r["tokens_in"] == 1000 and r["reasoning_tokens"] == 5 for r in rows.values() if r["ok"] and r["api_calls"] == 1) and rows[9]["api_calls"] == 1      # job 9: a phase-1 no match, one call only
    assert (kit["out"] / "results.csv").exists() and len(list((kit["out"] / "prompts").glob("job_*.txt"))) == 8


def test_settings_from_matching_yaml_reach_the_request_and_model_precedence(kit, monkeypatch):
    def last_two():   # one job = a phase-1 request then a phase-2 request (a match is confirmed by the resume)
        r = kit["fake"].requests
        assert [x["phase"] for x in r[-2:]] == [1, 2]
        return r[-2]["body"], r[-1]["body"]

    assert run_kit(kit, "--ids", "1") == 0
    p1, p2 = last_two()
    assert p1["model"] == "gpt-6-luna" and p1["reasoning_effort"] == "none" and set(p1) == {"model", "messages", "reasoning_effort", "prompt_cache_options"}   # phase 1: no thinking + explicit caching
    assert p2["reasoning_effort"] == "low"                                                                                                                  # phase 2: a little thinking
    set_llm(kit, reasoning_effort="")
    assert run_kit(kit, "--ids", "1") == 0 and "reasoning_effort" not in last_two()[0]      # blank = the model's own default: nothing extra is sent
    set_llm(kit, reasoning_effort="low", resume_reasoning_effort="high", max_output_tokens=800)
    assert run_kit(kit, "--ids", "1") == 0
    p1, p2 = last_two()
    assert p1["reasoning_effort"] == "low" and p2["reasoning_effort"] == "high" and p1["max_completion_tokens"] == p2["max_completion_tokens"] == 800
    assert run_kit(kit, "--ids", "1", "--reasoning-effort", "medium", "--resume-reasoning-effort", "none") == 0
    p1, p2 = last_two()
    assert p1["reasoning_effort"] == "medium" and p2["reasoning_effort"] == "none"          # the flags beat the file, one per phase
    monkeypatch.setenv("OPENAI_MODEL", "model-from-env")
    assert run_kit(kit, "--ids", "1") == 0 and last_two()[0]["model"] == "model-from-env"                  # .env beats matching.yaml
    assert run_kit(kit, "--ids", "1", "--model", "model-from-cli") == 0 and last_two()[1]["model"] == "model-from-cli"   # --model beats both


def test_one_failed_job_does_not_stop_the_rest_and_the_reason_is_shown(kit, capsys):
    assert run_kit(kit, "--ids", "1,3,4") == 1
    out = capsys.readouterr().out
    assert "succeeded: 2 | failed: 1" in out and "OpenAI rejected the request (400): this job broke the fake" in out


def test_reply_that_needs_cleanup_is_accepted_but_counted(kit, capsys):
    mark(kit, 6, "SHOUT")
    assert run_kit(kit, "--ids", "6,1") == 0
    out = capsys.readouterr().out
    assert "(a reply needed cleanup)" in out and "jobs with a reply needing cleanup: 1" in out
    rows = results(kit)
    assert (rows[6]["final_verdict"], rows[6]["reason"], rows[6]["reply_was_clean"], rows[6]["raw_reply"]) == ("match", "python", False, "Match: Python.")


def test_a_match_with_two_words_or_no_word_keeps_the_verdict_but_is_flagged(kit, capsys):
    mark(kit, 5, "TWOWORDS")
    mark(kit, 6, "NOWORD")
    assert run_kit(kit, "--ids", "5,6,1") == 0
    out = capsys.readouterr().out
    rows = results(kit)
    assert (rows[5]["final_verdict"], rows[5]["reason"], rows[5]["reply_was_clean"]) == ("match", "python django", False)
    assert (rows[6]["final_verdict"], rows[6]["reason"], rows[6]["reply_was_clean"]) == ("match", None, False)
    assert "jobs with a reply needing cleanup: 2" in out and "succeeded: 3 | failed: 0" in out


def test_off_spec_reply_is_an_error_for_that_job_only(kit, capsys):
    mark(kit, 5, "OFFSPEC")
    assert run_kit(kit, "--ids", "5,1,4") == 1
    out = capsys.readouterr().out
    assert "off-spec reply (expected 'match: <word>', 'no match: <word>', 'ambiguous')" in out and "succeeded: 2 | failed: 1" in out


def test_a_rate_limit_is_retried_and_counted_in_the_summary(kit, capsys):
    kit["fake"].rate_limit_once.add("Job 4")
    assert run_kit(kit, "--ids", "1,4,5") == 0
    out = capsys.readouterr().out
    assert "succeeded: 3 | failed: 0" in out and "rate-limit / server-error retries: 1" in out
    assert results(kit)[4]["retries"] == 1 and len(kit["fake"].requests) == 7   # 3 jobs x (phase 1 + phase 2) + 1 repeated attempt


def test_cost_is_estimated_only_when_prices_are_filled_in(kit, capsys):
    assert run_kit(kit, "--ids", "1") == 0
    assert "estimated cost: not shown" in capsys.readouterr().out
    s = yaml.safe_load((ROOT / "llm_test_kit" / "settings.yaml").read_text())
    s["pricing"] = {"input_per_million": 1.0, "output_per_million": 4.0}
    settings = kit["tmp"] / "priced.yaml"
    settings.write_text(yaml.safe_dump(s))
    assert run_kit(kit, "--ids", "1", "--settings", str(settings)) == 0
    assert "estimated cost: $0.0021 " in capsys.readouterr().out      # two calls (phase 1 and the resume check): 2 x (1000 in * $1/M + 12 out * $4/M) = 0.0021


# ---------------------------------------------------------------- the key and .env ----------------------------------------------------------
def test_missing_key_stops_before_any_call_with_instructions_but_dry_run_still_works(kit, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    assert run_kit(kit, "--ids", "1") == 2
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY is not set" in err and ".env" in err and "--dry-run" in err and kit["fake"].requests == []
    assert run_kit(kit, "--ids", "1", "--dry-run") == 0 and kit["fake"].requests == []


def test_key_is_read_from_the_env_file_and_a_real_environment_variable_wins(kit, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    (kit["home"] / ".env").write_text("OPENAI_API_KEY=sk-from-the-env-file-0000\n# a comment\n")
    assert run_kit(kit, "--ids", "1") == 0 and kit["fake"].requests[-1]["auth"] == "Bearer sk-from-the-env-file-0000"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-shell-1111")
    assert run_kit(kit, "--ids", "1") == 0 and kit["fake"].requests[-1]["auth"] == "Bearer sk-from-the-shell-1111"


def test_the_key_never_appears_in_output_or_saved_files(kit, capsys):
    assert run_kit(kit, "--ids", "1,4") == 0
    out = capsys.readouterr()
    assert KEY not in out.out + out.err                           # the full key is never printed
    assert f"{KEY[:3]}...{KEY[-4:]}" in out.out                   # only the masked form is shown
    for f in kit["out"].rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text()


def test_list_models_checks_the_key_and_flags_a_model_that_is_not_available(kit, capsys):
    assert run_kit(kit, "--list-models") == 0
    out = capsys.readouterr().out
    assert "API key accepted" in out and "gpt-6-luna" in out and "gpt-4o" in out and "whisper" not in out and "text-embedding" not in out and "<- NOT in the list" not in out
    assert run_kit(kit, "--list-models", "--model", "gpt-does-not-exist") == 0
    assert "configured model: gpt-does-not-exist   <- NOT in the list above" in capsys.readouterr().out


def test_list_models_reports_a_rejected_key_clearly(kit, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-FAILKEY-000000000")
    assert run_kit(kit, "--list-models") == 2
    assert "rejected the API key" in capsys.readouterr().err


def test_dry_run_calls_nothing_but_writes_the_prompts(kit, capsys):
    assert run_kit(kit, "--dry-run", "--limit", "3") == 0
    out = capsys.readouterr().out
    assert kit["fake"].requests == [] and "DRY RUN: nothing was sent" in out and "sent: job text + posted pay, profile details" in out
    assert len(list((kit["out"] / "prompts").glob("job_*.txt"))) == 3


def test_an_unreachable_server_is_reported_per_job_not_as_a_crash(kit, capsys, monkeypatch):
    import httpx

    def refuse(req):
        raise httpx.ConnectError("no route to host")
    monkeypatch.setattr(rt, "TRANSPORT", httpx.MockTransport(refuse))
    set_llm(kit, max_retries=0)
    assert run_kit(kit, "--ids", "1") == 1
    assert "could not reach OpenAI" in capsys.readouterr().out
