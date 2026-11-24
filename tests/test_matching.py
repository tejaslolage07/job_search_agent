"""Ranking, profile details, the rescoring triggers of the scoring loop, and profile validation. The AI matcher itself is in test_llm_matcher.py."""
import json

import pytest
from conftest import FakeLibrary
from fake_openai import FakeOpenAI

from job_search_agent import pipeline, report, store
from job_search_agent.matching.engine import MatchEngine
from job_search_agent.matching.llm import LLMJudge, load_prompt
from job_search_agent.matching.openai_client import OpenAIChat


def test_newest_first_even_when_older_scores_higher():
    rows = [
        {"id": 1, "final_score": 99, "posted_at": "2026-11-10T09:00:00Z", "first_seen_at": "2026-11-17T00:00:00Z"},
        {"id": 2, "final_score": 10, "posted_at": "2026-11-16T09:00:00Z", "first_seen_at": "2026-11-17T00:00:00Z"},
        {"id": 3, "final_score": 50, "posted_at": "2026-11-16T20:00:00Z", "first_seen_at": "2026-11-17T00:00:00Z"},
        {"id": 4, "final_score": 80, "posted_at": None, "first_seen_at": "2026-11-17T01:00:00Z"},
    ]
    assert [r["id"] for r in report.rank(rows)] == [4, 3, 2, 1]
    rows[1]["final_score"] = 60  # same day: higher score first
    assert [r["id"] for r in report.rank(rows)][1:3] == [2, 3]


def test_profile_details_text_carries_pay_but_not_notice_period_work_authorization_or_resume_paths(profile):
    p = profile.model_copy(update={"notice_period_days": 45, "work_authorization": "H1B holder"})
    text = p.details_text()
    assert "Python, Django, FastAPI" in text and "Bengaluru" in text and "Years of experience: 2" in text and "BadCorp" in text
    assert "Current CTC: 20 LPA" in text and "Expected CTC: minimum 28 LPA, target 35 LPA" in text and "rate for converting dollar salaries: 88" in text
    for hidden in ("45", "H1B", "resume.pdf", "notice"):
        assert hidden not in text


def test_profile_hash_follows_what_the_ai_sees_and_nothing_else(profile):
    base = profile.matching_hash()
    assert profile.model_copy(update={"notice_period_days": 10, "work_authorization": "x"}).matching_hash() == base   # never sent: re-scores nothing
    assert profile.model_copy(update={"current_ctc_lpa": 99}).matching_hash() != base                                 # pay is sent now
    p2 = profile.model_copy(deep=True)
    p2.expected_ctc_lpa.min = 30
    assert p2.matching_hash() != base
    p3 = profile.model_copy(deep=True)
    p3.must_have_keywords.append("Rust")
    assert p3.matching_hash() != base


def _insert(conn, company):
    from conftest import job
    from job_search_agent import diff
    rid = store.start_run(conn)
    diff.apply_plan(conn, rid, company.id, diff.compute_plan({}, [job("1")]))
    return conn.execute("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id").fetchall()


def _engine(profile, mcfg, fake):
    system, user = load_prompt(__import__("conftest").ROOT / "config" / "llm_prompt.md")
    p2 = load_prompt(__import__("conftest").ROOT / "config" / "llm_prompt_resume.md")
    client = OpenAIChat("sk-test-1234567890", "gpt-test", transport=fake.transport, sleep=lambda s: None)
    return MatchEngine(profile, mcfg, FakeLibrary(), LLMJudge(client, (system, user), p2, mcfg.llm, profile))


def test_rescoring_triggers(conn, company, profile, mcfg):
    rows = _insert(conn, company)
    fake = FakeOpenAI()
    assert pipeline.score_jobs(conn, _engine(profile, mcfg, fake), profile, mcfg, rows, None) == 1
    assert pipeline.score_jobs(conn, _engine(profile, mcfg, fake), profile, mcfg, rows, None) == 0  # cached

    p2 = profile.model_copy(deep=True)
    p2.seniority.reject_titles.append("engineer")  # profile change the AI sees
    assert pipeline.score_jobs(conn, _engine(p2, mcfg, fake), p2, mcfg, rows, None) == 1

    p3 = p2.model_copy(update={"notice_period_days": 5})  # a field the AI never sees: nothing to redo
    assert pipeline.score_jobs(conn, _engine(p3, mcfg, fake), p3, mcfg, rows, None) == 0
    p4 = p2.model_copy(update={"current_ctc_lpa": 40})  # pay is sent, so a pay change re-scores
    assert pipeline.score_jobs(conn, _engine(p4, mcfg, fake), p4, mcfg, rows, None) == 1

    conn.execute("UPDATE jobs SET description_hash='changed' WHERE id=1")  # job content change
    rows = conn.execute("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id").fetchall()
    assert pipeline.score_jobs(conn, _engine(p2, mcfg, fake), p2, mcfg, rows, None) == 1


def test_a_stored_match_was_confirmed_by_the_resume_and_has_no_stage1_scores(conn, company, profile, mcfg):
    rows = _insert(conn, company)
    pipeline.score_jobs(conn, _engine(profile, mcfg, FakeOpenAI()), profile, mcfg, rows, None)
    m = store.latest_match(conn, 1)
    assert m["stage_decided"] == 2 and m["resume_hash"] == "deadbeef" and m["stage1_score"] is None and m["method"] == "llm-resume" and m["bucket"] == "accept"
    assert "inputs" in json.loads(m["reasons_json"])


def test_profile_validation_message(tmp_path):
    from job_search_agent.config import ConfigError, load_profile
    p = tmp_path / "profile.yaml"
    p.write_text("years_experience: 2\n")
    with pytest.raises(ConfigError) as e:
        load_profile(p)
    assert "expected_ctc_lpa" in str(e.value) and "usd_to_inr" in str(e.value)


def test_a_profile_without_must_have_keywords_is_valid_and_says_so_to_the_ai(profile):
    from job_search_agent.config import Profile
    data = profile.model_dump()
    data["must_have_keywords"] = []
    p = Profile.model_validate(data)
    assert "Must-have skills: none listed" in p.details_text() and "Nice-to-have skills: LLM, Kubernetes" in p.details_text()
    assert p.matching_hash() != profile.matching_hash()


def test_a_profile_without_preferred_locations_is_valid_and_tells_the_ai_any_location_is_fine(profile):
    from job_search_agent.config import Profile
    data = profile.model_dump()
    data["locations"]["preferred"] = []
    p = Profile.model_validate(data)
    text = p.details_text()
    assert "Preferred locations: none listed (no location preference: any location is acceptable)" in text
    assert "outside the preferred locations" not in text and "\n\n" not in text and text.endswith("USD to INR rate for converting dollar salaries: 88")
    assert p.matching_hash() != profile.matching_hash()


def test_work_authorization_allows_only_indian_citizen_and_normalises_older_spellings(profile):
    from pydantic import ValidationError

    from job_search_agent.config import Profile
    base = profile.model_dump()
    for ok in ("Indian citizen", "India citizen", "indian citizen", " Indian Citizenship "):
        assert Profile.model_validate({**base, "work_authorization": ok}).work_authorization == "Indian citizen"
    for blank in (None, "", "  "):
        assert Profile.model_validate({**base, "work_authorization": blank}).work_authorization is None
    with pytest.raises(ValidationError, match="Indian citizen"):
        Profile.model_validate({**base, "work_authorization": "H1B holder"})
