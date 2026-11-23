from __future__ import annotations

import pytest

from job_search_agent import store
from job_search_agent.config import MatchingConfig, Profile, load_matching
from job_search_agent.matching.resumes import ResumeLibrary, ResumeVariant
from job_search_agent.matching.types import JobInput
from job_search_agent.models import Company, RawJob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROFILE = {
    "current_ctc_lpa": 20, "expected_ctc_lpa": {"min": 28, "target": 35}, "usd_to_inr": 88, "years_experience": 2,
    "locations": {"preferred": ["Bengaluru", "Remote-India"], "accept_hybrid": True, "accept_onsite_outside_preferred": False},
    "target_titles": ["Backend Engineer", "AI Engineer", "Software Engineer"],
    "seniority": {"min_level": "junior", "max_level": "mid", "reject_titles": ["intern", "principal", "staff", "director"]},
    "must_have_keywords": ["Python", "Django", "FastAPI"], "nice_to_have_keywords": ["LLM", "Kubernetes"],
    "exclude_keywords": ["security clearance"], "exclude_companies": ["BadCorp"],
    "resume_paths": {"backend": "resume.pdf"},
}


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(PROFILE)


@pytest.fixture
def mcfg() -> MatchingConfig:
    return load_matching(ROOT / "config" / "matching.yaml")


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.import_companies(c, [{"company": "Acme", "careers_url": "https://x", "ats_type": "greenhouse", "board_token": "acme", "tier": "1", "active": "true"}])
    return c


@pytest.fixture
def company(conn) -> Company:
    return store.list_companies(conn)[0]


def job(ext: str = "1", title: str = "Backend Engineer", desc: str = "Python Django FastAPI role. 2+ years of experience.",
        location: str = "Bengaluru", **kw) -> RawJob:
    return RawJob(external_id=ext, url=f"https://acme.test/jobs/{ext}", title=title, location=location, description_text=desc, **kw)


def ji(title="Backend Engineer", location="Bengaluru", desc="Python Django FastAPI. 2+ years of experience.", salary=None, tier=None, wp=None) -> JobInput:
    return JobInput("Acme", tier, title, location, wp, desc, salary)


class ExplodingLibrary(ResumeLibrary):
    """Fails loudly if anything touches the resume."""

    def __init__(self) -> None:
        super().__init__({"backend": "resume.pdf"})

    def variants(self):  # type: ignore[override]
        raise AssertionError("the resume was read although phase 1 decided")


class FakeLibrary(ResumeLibrary):
    def __init__(self, text: str = "Python Django FastAPI Kubernetes LLM backend engineer") -> None:
        super().__init__({"backend": "resume.pdf"})
        self._text = text

    def variants(self):  # type: ignore[override]
        self.load_count += 1
        self._variants = [ResumeVariant("backend", Path("resume.pdf"), "deadbeef", self._text)]
        return self._variants
