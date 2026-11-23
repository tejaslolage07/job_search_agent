"""The AI matcher (OpenAI). Two phases, both answering with one line and one word for the strongest parameter:
    phase 1  job + profile details  -> `match` / `no match` / `ambiguous`      (config/llm_prompt.md)
    phase 2  job + resume           -> `match` / `no match`  (never ambiguous)   (config/llm_prompt_resume.md); only for phase-1 `ambiguous`
What is put into a prompt: the job (including its posted salary text), the profile details (Profile.details_text: skills, locations, titles and
your current / expected pay; not notice period or work authorization) in phase 1, and the resume text in phase 2.
The test kit (llm_test_kit/run_test.py) uses these same functions.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import LLMCfg, Profile
from .openai_client import DEFAULT_BASE_URL, OpenAIChat, OpenAIError

VERDICTS = ("match", "no match", "ambiguous")   # phase 1
FINAL_VERDICTS = ("match", "no match")           # phase 2: ambiguous is not allowed
VERDICT_TO_BUCKET = {"match": "accept", "no match": "reject", "ambiguous": "review"}
BUCKET_TO_VERDICT = {b: v for v, b in VERDICT_TO_BUCKET.items()}
PLACEHOLDERS = {"job_title", "company", "location", "job_description", "salary", "profile", "resume", "cache_here"}
CACHE_MARK = "\x00"   # where {{cache_here}} sits: the text before it is the shared start of every prompt (cacheable), the text after it is per job
VERDICT_RE = re.compile(r"^(no[ _-]?match|match|ambiguous)\s*(?:[:\-–—]\s*(.*))?$", re.I | re.S)


class LLMConfigError(Exception):
    """The AI judge cannot run (no key, bad prompt file, ...). The message says how to fix it."""


# ---- prompt file ----------------------------------------------------------------------------------------------------
def load_prompt(path: Path) -> tuple[str, str]:
    """llm_prompt.md -> (system prompt, user template). Sections start with '## SYSTEM' and '## USER'; anything above is ignored."""
    if not path.is_file():
        raise LLMConfigError(f"prompt file not found: {path}")
    sections: dict[str, list[str]] = {}
    current = None
    for line in path.read_text().splitlines():
        m = re.match(r"##\s+(SYSTEM|USER)\s*$", line.strip())
        if m:
            current = m.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    if "SYSTEM" not in sections or "USER" not in sections:
        raise LLMConfigError(f"{path} must contain a '## SYSTEM' section and a '## USER' section")
    system, user = "\n".join(sections["SYSTEM"]).strip(), "\n".join(sections["USER"]).strip()
    if not system or not user:
        raise LLMConfigError(f"{path}: the SYSTEM and USER sections must not be empty")
    for text in (system, user):
        unknown = set(re.findall(r"\{\{(\w+)\}\}", text)) - PLACEHOLDERS
        if unknown:
            raise LLMConfigError(f"{path}: unknown placeholder(s) {sorted('{{' + u + '}}' for u in unknown)}; allowed: {sorted(PLACEHOLDERS)}")
    return system, user


def render(template: str, **fields: str) -> str:
    """Fill {{placeholders}} in a single pass, so text inside a job or resume is never treated as a placeholder."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: fields.get(m.group(1), ""), template)


# ---- reading the reply ----------------------------------------------------------------------------------------------------
def _untag(word: str) -> str:
    """A model may copy a placeholder literally: 'skills' inside word tags, or an empty location tag. Keep the word; the reply is then flagged as needing cleanup."""
    m = re.fullmatch(r"<(\w+)>\s*(.*?)\s*(?:</\1>)?", word)
    return (m.group(2) or m.group(1)).strip() if m else word


def split_reply(raw: str) -> tuple[str, str] | None:
    """'match: python' / 'Match - Python.' / 'match' + newline + 'python' -> ('match', 'python'); None when it is not a verdict at all."""
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not 1 <= len(lines) <= 2:
        return None
    text = re.sub(r"[*`]", "", lines[0] if len(lines) == 1 else f"{lines[0]}: {lines[1]}").strip("\"' . !")  # markdown bold/code can sit anywhere
    m = VERDICT_RE.match(text)
    if not m:
        return None
    return re.sub(r"[ _-]+", " ", m.group(1).lower()), _untag((m.group(2) or "").strip().strip("\"'`*. !").lower())


def is_clean(raw: str, verdict: str, reason: str) -> bool:
    """Exactly the requested form: 'match: word' / 'no match: word' on one line, or a bare 'ambiguous'."""
    if verdict == "ambiguous":
        return raw.strip() == "ambiguous"
    return bool(reason) and " " not in reason and raw.strip() == f"{verdict}: {reason}"


def parse_text(raw: str, allowed: tuple[str, ...] = VERDICTS) -> dict[str, Any]:
    """The model's reply text -> {verdict, reason, clean, raw}. Raises ValueError when it is not an allowed verdict
    (phase 2 passes FINAL_VERDICTS, so an 'ambiguous' reply there is an error, never a result).
    A valid verdict with a missing / multi-word / extra explanation is accepted but flagged clean=False."""
    if not raw.strip():
        raise ValueError("empty reply")
    parts = split_reply(raw)
    if parts is None or parts[0] not in allowed:
        shown = " ".join(raw.split())
        want = ", ".join(f"'{v}: <word>'" if v != "ambiguous" else "'ambiguous'" for v in allowed)
        raise ValueError(f"off-spec reply (expected {want}): '{shown[:90]}{'...' if len(shown) > 90 else ''}'")
    verdict, reason = parts
    return {"verdict": verdict, "reason": reason, "clean": is_clean(raw, verdict, reason), "raw": raw}


# ---- the judge --------------------------------------------------------------------------------------------------------------
@dataclass
class Judgement:
    verdict: str
    reason: str
    clean: bool
    raw: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    retries: int = 0
    record: dict[str, Any] | None = None   # what is stored in the llm_calls table: the full request and response, tokens, status


def resolve_model(cfg: LLMCfg) -> str:
    return os.environ.get("OPENAI_MODEL", "").strip() or cfg.model


def make_client(cfg: LLMCfg, api_key: str, transport: httpx.BaseTransport | None = None, model: str | None = None) -> OpenAIChat:
    return OpenAIChat(api_key, model or resolve_model(cfg), base_url=cfg.base_url or DEFAULT_BASE_URL, timeout=cfg.timeout_seconds, max_retries=cfg.max_retries,
                      reasoning_effort=cfg.reasoning_effort or None, max_output_tokens=cfg.max_output_tokens, cache=cfg.cache, cache_ttl=cfg.cache_ttl, transport=transport)


def user_parts(template: str, cfg: LLMCfg, title: str, company: str, location: str, description: str, profile: str = "", resume: str = "",
               salary: str = "") -> list[str]:
    """The user message, filled in (job text cut to its configured length), as [shared start, per-job text] when the template has {{cache_here}}, else [whole text]."""
    clean = lambda t: t.replace(CACHE_MARK, "")  # noqa: E731 - text from a job or resume can never fake the marker
    text = render(template, cache_here=CACHE_MARK, job_title=clean(title), company=clean(company), location=clean(location or "not stated"),
                  job_description=clean(description[: cfg.max_jd_chars]), salary=clean(salary or "not stated"), profile=clean(profile), resume=clean(resume[: cfg.max_resume_chars]))
    return text.split(CACHE_MARK, 1) if CACHE_MARK in text else [text]


def user_prompt(template: str, cfg: LLMCfg, title: str, company: str, location: str, description: str, profile: str = "", resume: str = "",
                salary: str = "") -> str:
    """The same message as one plain text (what the kit saves and prints)."""
    return "".join(user_parts(template, cfg, title, company, location, description, profile, resume, salary))


class LLMJudge:
    """Thread-safe: the judge methods only talk to the API (no database), so several can run at once."""

    def __init__(self, client: OpenAIChat, phase1: tuple[str, str], phase2: tuple[str, str], cfg: LLMCfg, profile: Profile) -> None:
        self.client, self.phase1, self.phase2, self.cfg = client, phase1, phase2, cfg
        self.profile_text = profile.details_text()
        # Changes whenever a prompt, the model or the effort changes: verdicts made with a different fingerprint are re-made.
        self.fingerprint = hashlib.sha256("\x00".join([*phase1, *phase2, client.model, cfg.reasoning_effort, cfg.resume_reasoning_effort]).encode()).hexdigest()[:12]

    def _ask(self, prompts: tuple[str, str], allowed: tuple[str, ...], user: list[str], effort: str) -> Judgement:
        """Every outcome leaves a `record` (on the Judgement, or as `.record` on the exception) holding the entire request and response."""
        try:
            reply = self.client.complete(prompts[0], user, effort)
        except OpenAIError as exc:
            exc.record = {"model": self.client.model, "request": exc.request, "response": exc.response, "status": "error", "error": str(exc),
                          "tokens_in": 0, "tokens_out": 0, "reasoning_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "retries": 0}
            raise
        record = {"model": reply.model, "request": reply.request, "response": reply.response, "status": "ok", "error": None, "tokens_in": reply.tokens_in,
                  "tokens_out": reply.tokens_out, "reasoning_tokens": reply.reasoning_tokens, "cached_tokens": reply.cached_tokens,
                  "cache_write_tokens": reply.cache_write_tokens, "retries": reply.retries}
        try:
            out = parse_text(reply.text, allowed)
        except ValueError as exc:   # the call worked (and was paid for) but the reply is not an allowed verdict
            exc.record = {**record, "status": "error", "error": str(exc)}  # type: ignore[attr-defined]
            raise
        return Judgement(out["verdict"], out["reason"], out["clean"], out["raw"], reply.model, reply.tokens_in, reply.tokens_out, reply.reasoning_tokens, reply.retries, record)

    def profile_parts(self, title: str, company: str, location: str, description: str, salary: str = "") -> list[str]:
        return user_parts(self.phase1[1], self.cfg, title, company, location, description, profile=self.profile_text, salary=salary)

    def resume_parts(self, title: str, company: str, location: str, description: str, resume: str) -> list[str]:
        return user_parts(self.phase2[1], self.cfg, title, company, location, description, resume=resume)

    def profile_prompt(self, title: str, company: str, location: str, description: str, salary: str = "") -> str:
        return "".join(self.profile_parts(title, company, location, description, salary))

    def resume_prompt(self, title: str, company: str, location: str, description: str, resume: str) -> str:
        return "".join(self.resume_parts(title, company, location, description, resume))

    def judge_profile(self, title: str, company: str, location: str, description: str, salary: str = "") -> Judgement:
        """Phase 1: match / no match / ambiguous. Raises OpenAIError (API problem) or ValueError (reply is not a verdict)."""
        return self._ask(self.phase1, VERDICTS, self.profile_parts(title, company, location, description, salary), self.cfg.reasoning_effort)

    def judge_resume(self, title: str, company: str, location: str, description: str, resume: str) -> Judgement:
        """Phase 2: match / no match only. An 'ambiguous' reply raises ValueError."""
        return self._ask(self.phase2, FINAL_VERDICTS, self.resume_parts(title, company, location, description, resume), self.cfg.resume_reasoning_effort)


def build_judge(profile: Profile, cfg: LLMCfg, config_dir: Path, transport: httpx.BaseTransport | None = None) -> LLMJudge:
    """Everything needed to run the AI matcher, or an LLMConfigError saying what is missing."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise LLMConfigError("OPENAI_API_KEY is not set: put it in the .env file at the project root, then restart")
    phase1, phase2 = load_prompt(config_dir / cfg.prompt_file), load_prompt(config_dir / cfg.resume_prompt_file)
    return LLMJudge(make_client(cfg, key, transport), phase1, phase2, cfg, profile)
