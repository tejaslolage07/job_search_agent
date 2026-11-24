"""Detect the ATS behind a careers URL (URL patterns first, then page HTML)."""
from __future__ import annotations

import re

from ..http import FetchError, PoliteClient

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_\-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_\-]+)", re.I)),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_\-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-%]+)", re.I)),
    ("keka", re.compile(r"([A-Za-z0-9\-]+)\.keka(?:hr)?\.com/careers", re.I)),
    ("zoho", re.compile(r"([A-Za-z0-9\-]+\.zohorecruit\.(?:in|com|eu))/jobs", re.I)),
    ("oracle", re.compile(r"([A-Za-z0-9.\-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[A-Za-z\-]+/sites/([A-Za-z0-9_]+)", re.I)),
    ("workday", re.compile(r"([A-Za-z0-9\-]+\.wd\d+\.myworkdayjobs\.com)", re.I)),
]


def detect_from_text(text: str) -> tuple[str, str | None] | None:
    for ats, pat in _PATTERNS:
        m = pat.search(text)
        if m:
            if ats == "oracle":
                return ats, f"{m.group(1)}|{m.group(2)}"
            return ats, (None if ats == "workday" else m.group(1))
    return None


def detect_ats(careers_url: str, client: PoliteClient) -> tuple[str, str | None]:
    hit = detect_from_text(careers_url)
    if hit:
        return hit
    try:
        resp = client.get(careers_url)
    except FetchError:
        return "custom", None
    if resp.status_code == 200:
        hit = detect_from_text(resp.text)
        if hit:
            return hit
    return "custom", None
