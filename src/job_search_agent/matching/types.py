from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Reason:
    rule: str
    effect: str  # reject | accept | neutral | plus | minus
    detail: str

    def text(self) -> str:
        return f"{self.rule}: {self.detail}"

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "effect": self.effect, "detail": self.detail}


@dataclass
class MatchOutcome:
    phase: int  # 1 = decided from the profile details, 2 = decided from the resume
    final_score: float  # 100 match, 0 no match, 50 when the AI could not decide (the job is retried next run)
    bucket: str  # accept | reject | review (review only when the AI check failed)
    reasons: list[Reason]
    inputs: dict[str, Any]
    method: str  # llm-profile | llm-resume | llm-failed
    resume_variant: str | None = None
    resume_hash: str | None = None  # None whenever phase 2 did not run
    llm_calls: list[dict[str, Any]] = field(default_factory=list)  # one record per API call (full request/response), saved to llm_calls

    def top_reasons(self, n: int = 3) -> list[str]:
        return [r.text() for r in self.reasons[:n]]


@dataclass
class JobInput:
    """Everything the matcher may look at for one job (no profile, no resume, no raw payload)."""

    company: str
    tier: int | None
    title: str
    location: str
    workplace_type: str | None
    description: str
    salary_text: str | None
    extra: dict[str, Any] = field(default_factory=dict)
