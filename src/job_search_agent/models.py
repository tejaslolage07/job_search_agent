"""Plain data containers shared across modules."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_|gh_src|gh_jid|lever-|ref$|source$|src$)")


def normalize_url(url: str) -> str:
    """Lowercase host, drop fragment, tracking params and trailing slash."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k.lower())]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def url_identity(url: str) -> str:
    return "url:" + sha256_text(normalize_url(url))[:24]


@dataclass
class RawJob:
    """A job as returned by a source adapter, before persistence.

    description_text=None means "not fetched this run" (the diff keeps the stored text).
    """

    title: str
    url: str
    external_id: str | None = None
    location: str = ""
    department: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None  # ISO-8601 IST, e.g. 2026-11-01T15:30:00+05:30
    description_text: str | None = None
    workplace_type: str | None = None  # remote | hybrid | onsite
    salary_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.external_id or url_identity(self.url)


@dataclass
class Company:
    id: int
    name: str
    careers_url: str
    ats_type: str = "auto"
    board_token: str | None = None
    tier: int | None = None
    active: bool = True
    notes: str = ""


@dataclass
class FetchResult:
    jobs: list[RawJob]
    method: str  # e.g. greenhouse-json, jsonld, sitemap, html, playwright
    fallbacks: list[str] = field(default_factory=list)  # expensive paths taken
