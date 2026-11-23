"""What a discovery run does. Stored as JSON on runs/schedules, validated here."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

WINDOWS = ("24hr", "past week", "past month")


class LinkedInQuery(BaseModel):
    keyword: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=120)
    job_type: Literal["full time", "part time", "contract", "temporary", "volunteer", "internship"] | None = None
    remote_filter: Literal["on site", "remote", "hybrid"] | None = None
    experience_level: Literal["internship", "entry level", "associate", "senior", "director", "executive"] | None = None
    sort_by: Literal["recent", "relevant"] = "recent"


class LinkedInConfig(BaseModel):
    enabled: bool = False
    queries: list[LinkedInQuery] = []
    window: Literal["24hr", "past week", "past month"] = "24hr"
    max_listings: int = Field(100, ge=1, le=1000)
    workers: int = Field(2, ge=1, le=6)

    @field_validator("queries")
    @classmethod
    def _cap(cls, v: list[LinkedInQuery]) -> list[LinkedInQuery]:
        if len(v) > 30:
            raise ValueError("at most 30 search queries per run")
        return v


class AtsConfig(BaseModel):
    enabled: bool = True
    companies: list[str] | None = None  # None = every active company in companies.csv


class DiscoverConfig(BaseModel):
    ats: AtsConfig = AtsConfig()
    linkedin: LinkedInConfig = LinkedInConfig()
    match: bool = True
    rematch_job_ids: list[int] | None = Field(None, max_length=1000)   # set: skip discovery and re-run the AI matching for exactly these jobs, ignoring cached results

    def validate_runnable(self) -> None:
        if self.rematch_job_ids is not None:
            if not self.rematch_job_ids:
                raise ValueError("no jobs selected")
            return
        if not (self.ats.enabled or self.linkedin.enabled or self.match):
            raise ValueError("nothing to do: enable company boards, LinkedIn, or matching")
        if self.linkedin.enabled and not self.linkedin.queries:
            raise ValueError("LinkedIn is enabled but has no search queries")
