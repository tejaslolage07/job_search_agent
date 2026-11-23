"""Load and validate profile.yaml, matching.yaml, companies.csv, selectors.yaml."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .models import sha256_text

LEVELS = ["intern", "junior", "mid", "senior", "staff", "principal", "director"]


class ConfigError(Exception):
    pass


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float = Field(gt=0)
    target: float = Field(gt=0)


class Locations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred: list[str] = []   # optional: empty means no location preference
    accept_hybrid: bool
    accept_onsite_outside_preferred: bool = True   # on unless the user turns it off


class Seniority(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_level: str
    max_level: str
    reject_titles: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> Seniority:
        for v in (self.min_level, self.max_level):
            if v not in LEVELS:
                raise ValueError(f"level '{v}' must be one of {LEVELS}")
        if LEVELS.index(self.min_level) > LEVELS.index(self.max_level):
            raise ValueError("min_level is above max_level")
        return self


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None              # shown as initials on the dashboard; never sent to the AI
    current_ctc_lpa: float = Field(gt=0)
    expected_ctc_lpa: Expected
    usd_to_inr: float = Field(gt=0)
    years_experience: float = Field(ge=0)
    locations: Locations
    target_titles: list[str] = Field(min_length=1)
    seniority: Seniority
    max_required_years: float | None = None
    must_have_keywords: list[str] = []   # optional: the AI reads these as plain text, so an empty list is fine
    nice_to_have_keywords: list[str] = []
    exclude_keywords: list[str] = []
    exclude_companies: list[str] = []
    notice_period_days: int | None = None
    work_authorization: str | None = None
    resume_paths: dict[str, str] = {}

    @field_validator("expected_ctc_lpa")
    @classmethod
    def _order(cls, v: Expected) -> Expected:
        if v.target < v.min:
            raise ValueError("target must be >= min")
        return v

    def details_text(self) -> str:
        """The profile as it is shown to the AI: the only door from the profile into a prompt. It includes pay (current and expected CTC, USD rate).
        Name, notice period, work authorization and the resume paths are not included."""
        loc = self.locations
        lines = [
            f"Years of experience: {self.years_experience:g}",
            f"Target titles: {', '.join(self.target_titles)}",
            f"Seniority wanted: {self.seniority.min_level} to {self.seniority.max_level}",
            f"Titles to avoid: {', '.join(self.seniority.reject_titles) or 'none'}",
            f"Max years of experience the job may ask for: {f'{self.max_years:g}' if self.max_years is not None else 'no limit'}",
            f"Preferred locations: {', '.join(loc.preferred)}" if loc.preferred else "Preferred locations: none listed (no location preference: any location is acceptable)",
            f"Hybrid roles acceptable: {'yes' if loc.accept_hybrid else 'no'}",
            f"Onsite or remote roles outside the preferred locations acceptable: {'yes' if loc.accept_onsite_outside_preferred else 'no'}" if loc.preferred else "",
            f"Must-have skills: {', '.join(self.must_have_keywords) or 'none listed'}",
            f"Nice-to-have skills: {', '.join(self.nice_to_have_keywords) or 'none'}",
            f"Dealbreaker words: {', '.join(self.exclude_keywords) or 'none'}",
            f"Companies to avoid: {', '.join(self.exclude_companies) or 'none'}",
            f"Current CTC: {self.current_ctc_lpa:g} LPA (lakhs per annum)",
            f"Expected CTC: minimum {self.expected_ctc_lpa.min:g} LPA, target {self.expected_ctc_lpa.target:g} LPA",
            f"USD to INR rate for converting dollar salaries: {self.usd_to_inr:g}",
        ]
        return "\n".join(line for line in lines if line)

    @property
    def max_years(self) -> float | None:
        """The most years a posting may ask for; None (left blank) means there is no limit."""
        return self.max_required_years

    def matching_hash(self) -> str:
        """Changes exactly when the profile text the AI sees changes."""
        return sha256_text(self.details_text())[:16]


class LLMCfg(BaseModel):
    """The AI matcher (OpenAI). The API key is never here: it comes from OPENAI_API_KEY in .env."""
    model: str = "gpt-6-luna"          # OPENAI_MODEL in .env overrides this
    reasoning_effort: str = ""         # phase 1 (profile check). "" = the model's default; none / low / medium / high for models that reason
    resume_reasoning_effort: str = ""  # phase 2 (resume check), same values
    max_output_tokens: int | None = None
    max_retries: int = 3               # repeats after a rate limit / server error / timeout
    timeout_seconds: float = 120
    concurrency: int = Field(8, ge=1, le=16)   # AI calls running at once
    max_jd_chars: int = 6000
    max_resume_chars: int | None = 50000   # safety ceiling only (about 12 pages of text): a normal resume is never cut. null = no limit at all
    prompt_file: str = "llm_prompt.md"            # phase 1 (profile), inside config/
    resume_prompt_file: str = "llm_prompt_resume.md"  # phase 2 (resume, only for phase-1 "ambiguous")
    base_url: str = ""
    cache: Literal["explicit", "implicit"] = "explicit"   # explicit: only the shared start of a prompt is cached (cheap reads); implicit: OpenAI caches every whole prompt (extra write charge)
    cache_ttl: Literal["30m"] = "30m"                      # how long the shared start stays cached


class MatchingConfig(BaseModel):
    llm: LLMCfg = LLMCfg()

    def config_hash(self) -> str:
        """Everything that decides a verdict lives in the profile text and the AI prompt/model, which carry their own hashes."""
        return "llm-v1"


def _fmt_validation(path: Path, exc: ValidationError) -> str:
    lines = [f"Invalid {path}:"]
    for e in exc.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "(root)"
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"Missing {path}. Copy config/profile.example.yaml to config/profile.yaml and edit it.")
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc


def load_profile(path: Path) -> Profile:
    data = _load_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_fmt_validation(path, exc)) from exc


def load_matching(path: Path) -> MatchingConfig:
    data = _load_yaml(path) if path.exists() else {}
    try:
        return MatchingConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_fmt_validation(path, exc)) from exc


def load_companies_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ConfigError(f"Missing {path}")
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    need = {"company", "careers_url"}
    if rows and not need <= set(rows[0]):
        raise ConfigError(f"{path} needs columns company, careers_url, ats_type, board_token, tier, active, notes")
    return rows


def load_selectors(path: Path) -> dict[str, Any]:
    return _load_yaml(path) if path.exists() else {}
