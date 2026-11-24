"""The matcher: phase 1 asks the AI about the job and the profile details. A `no match` is final. A `match` or an `ambiguous` goes on to phase 2, which
asks about the job and the resume and may only answer match or no match: phase 2 always has the last word, so nothing is accepted on the profile alone
(the profile cannot show what the resume lacks). match -> accept, no match -> reject. If the AI cannot answer (no resume, API error, off-spec reply)
the job is left in `review` with method `llm-failed` and is tried again on the next run.

`preload()` reads the resume files (database access) and must be called from the main thread before `score` is used from several threads;
`score` itself only talks to the API.
"""
from __future__ import annotations

from ..config import MatchingConfig, Profile
from .llm import VERDICT_TO_BUCKET, Judgement, LLMJudge
from .resumes import ResumeLibrary, ResumeVariant
from .types import JobInput, MatchOutcome, Reason

EFFECT = {"accept": "plus", "reject": "minus", "review": "neutral"}


def _call_info(j: Judgement) -> dict[str, object]:
    return {"verdict": j.verdict, "reason": j.reason, "clean": j.clean, "raw": j.raw, "model": j.model,
            "tokens_in": j.tokens_in, "tokens_out": j.tokens_out, "reasoning_tokens": j.reasoning_tokens, "retries": j.retries}


def _record(phase: int, variant: str | None, rec: dict[str, object] | None, exc: Exception | None = None) -> dict[str, object]:
    """One llm_calls row. A failure that never reached the API (no record on the exception) still gets a row with the error."""
    base = rec or {"model": "", "request": {}, "response": None, "status": "error", "error": _err(exc) if exc else "", "tokens_in": 0, "tokens_out": 0,
                   "reasoning_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "retries": 0}
    return {**base, "phase": phase, "resume_variant": variant}


def _err(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


class MatchEngine:
    def __init__(self, profile: Profile, cfg: MatchingConfig, resumes: ResumeLibrary, llm: LLMJudge) -> None:
        self.profile, self.cfg, self.resumes, self.llm = profile, cfg, resumes, llm
        self._variants: list[ResumeVariant] = []
        self.resume_error: str | None = None

    def preload(self) -> str | None:
        """Read the resume(s) once. Returns the problem (and remembers it) instead of raising: phase-1 verdicts do not need a resume."""
        try:
            self._variants = self.resumes.variants()
            self.resume_error = None if self._variants else "no resume_paths configured"
        except Exception as exc:  # noqa: BLE001 - missing file, unreadable PDF
            self._variants, self.resume_error = [], _err(exc)
        return self.resume_error

    def _outcome(self, phase: int, verdict: str, calls: list[dict[str, object]], reasons: list[Reason], records: list[dict[str, object]],
                 variant: ResumeVariant | None = None) -> MatchOutcome:
        bucket = VERDICT_TO_BUCKET[verdict]
        inputs = {"llm": {"fingerprint": self.llm.fingerprint, "calls": calls}}
        return MatchOutcome(phase, 100.0 if bucket == "accept" else 0.0, bucket, reasons, inputs, f"llm-{'profile' if phase == 1 else 'resume'}",
                            resume_variant=variant.name if variant else None, resume_hash=variant.file_hash if variant else None, llm_calls=records)

    def _failed(self, phase: int, error: str, calls: list[dict[str, object]], records: list[dict[str, object]]) -> MatchOutcome:
        inputs = {"llm": {"fingerprint": self.llm.fingerprint, "calls": calls, "error": error}}
        return MatchOutcome(phase, 50.0, "review", [Reason("llm_failed", "neutral", f"AI check failed in phase {phase} ({error}); will be retried next run")],
                            inputs, "llm-failed", llm_calls=records)

    @staticmethod
    def _detail(j: Judgement, what: str) -> str:
        return f"{what}: AI judged '{j.verdict}'" + (f", strongest parameter: {j.reason}" if j.reason else "") + ("" if j.clean else " (reply needed cleanup)")

    def score(self, job: JobInput) -> MatchOutcome:
        llm, calls, records = self.llm, [], []
        try:
            p1 = llm.judge_profile(job.title, job.company, job.location, job.description, job.salary_text or "")
        except Exception as exc:  # noqa: BLE001 - OpenAIError, off-spec reply (ValueError), anything else
            records.append(_record(1, None, getattr(exc, "record", None), exc))
            return self._failed(1, _err(exc), calls, records)
        calls.append({"phase": 1, **_call_info(p1)})
        records.append(_record(1, None, p1.record))
        if p1.verdict == "no match":   # a rejection on the profile is final; a match or an unclear job is checked against the resume
            return self._outcome(1, p1.verdict, calls, [Reason("llm_profile", EFFECT[VERDICT_TO_BUCKET[p1.verdict]], self._detail(p1, "profile check"))], records)

        if not self._variants:
            return self._failed(2, f"resume could not be read ({self.resume_error or 'preload() was not called'})", calls, records)
        reasons = [Reason("llm_profile", "plus" if p1.verdict == "match" else "neutral",
                          self._detail(p1, "profile check") + ("; resume compared next to confirm" if p1.verdict == "match" else "; resume compared next"))]
        last: Judgement | None = None
        for v in self._variants:  # several resume variants: any one that matches is enough
            try:
                last = llm.judge_resume(job.title, job.company, job.location, job.description, v.text)
            except Exception as exc:  # noqa: BLE001
                records.append(_record(2, v.name, getattr(exc, "record", None), exc))
                return self._failed(2, f"resume '{v.name}': {_err(exc)}", calls, records)
            calls.append({"phase": 2, "resume_variant": v.name, **_call_info(last)})
            records.append(_record(2, v.name, last.record))
            reasons.append(Reason("llm_resume", EFFECT[VERDICT_TO_BUCKET[last.verdict]], self._detail(last, f"resume '{v.name}'")))
            if last.verdict == "match":
                return self._outcome(2, "match", calls, reasons, records, v)
        assert last is not None
        return self._outcome(2, "no match", calls, reasons, records, self._variants[-1])
