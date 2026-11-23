"""LinkedIn discovery: collection lives in linkedin_collector.py; this module holds the request counter and ingests its output into the jobs table.

LinkedIn employers become synthetic companies named '<Employer> [LinkedIn]' (ats_type='linkedin', inactive) so that a
company we also crawl via its ATS keeps its own, separate rows: both versions of a job are shown (by design), and the
ATS crawl's 'missing twice => removed' logic can never touch LinkedIn rows.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import diff, store, timeutil
from .discovery_config import LinkedInConfig
from .models import RawJob

SUFFIX = " [LinkedIn]"


def display_company(name: str) -> str:
    return name[: -len(SUFFIX)] if name.endswith(SUFFIX) else name


# ---- usage bookkeeping --------------------------------------------------------------------------------------
def today() -> str:
    return timeutil.today_ist().isoformat()


def requests_today(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT requests FROM linkedin_usage WHERE day=?", (today(),)).fetchone()
    return int(row["requests"]) if row else 0


def record_usage(conn: sqlite3.Connection, n: int) -> None:
    conn.execute("INSERT INTO linkedin_usage(day, requests) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET requests=requests+excluded.requests",
                 (today(), n))
    conn.commit()


# ---- collector config ------------------------------------------------------------------------------------------------
def known_description_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT external_id FROM jobs WHERE source='linkedin' AND COALESCE(description_text,'')<>''")
    return [r["external_id"][3:] for r in rows if r["external_id"].startswith("li:")]


def collector_config(cfg: LinkedInConfig, out: Path, skip_ids: list[str]) -> dict[str, Any]:
    return {
        "queries": [{"keyword": q.keyword, "location": q.location, "jobType": q.job_type, "remoteFilter": q.remote_filter,
                     "experienceLevel": q.experience_level, "sortBy": q.sort_by} for q in cfg.queries],
        "window": cfg.window, "maxListings": cfg.max_listings, "workers": cfg.workers, "out": str(out), "skipIds": skip_ids,
    }


# ---- ingestion -------------------------------------------------------------------------------------------------------
def _company_id(conn: sqlite3.Connection, employer: str) -> int:
    name = employer.strip() + SUFFIX
    conn.execute("INSERT OR IGNORE INTO companies(name, careers_url, ats_type, active, notes) VALUES(?,?,?,0,?)",
                 (name, "https://www.linkedin.com/jobs", "linkedin", "created from LinkedIn discovery"))
    return int(conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()["id"])


def to_raw_job(card: dict[str, Any], rec: dict[str, Any] | None) -> RawJob:
    detail = (rec or {}).get("detail") or {}
    crit = detail.get("criteria") or {}
    date = card.get("date")
    return RawJob(
        external_id=f"li:{card['id']}", url=card["jobUrl"], title=card["position"], location=card.get("location") or "",
        department=crit.get("Industries"), employment_type=crit.get("Employment type"),
        posted_at=timeutil.date_only_to_ist_iso(date) if date else None, description_text=detail.get("description"),
        salary_text=card.get("salary"),
        raw={"card": card, "criteria": crit, "applicants": detail.get("applicants"), "posted_text": detail.get("posted")})


def ingest(conn: sqlite3.Connection, run_id: int, state_path: Path, out_path: Path) -> dict[str, int]:
    """Insert/update LinkedIn jobs. Never marks anything removed (a search result's absence proves nothing)."""
    cards = json.loads(state_path.read_text())["cards"] if state_path.exists() else {}
    details: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        for line in out_path.read_text().split("\n"):  # NOT splitlines(): U+2028 etc. can occur inside JSON strings
            if line.strip():
                r = json.loads(line)
                details[r["id"]] = r
    by_company: dict[str, list[RawJob]] = {}
    for cid, card in cards.items():
        by_company.setdefault(card["company"], []).append(to_raw_job(card, details.get(cid)))
    counts = {"new": 0, "changed": 0, "seen": 0, "companies": len(by_company)}
    for employer, jobs in by_company.items():
        company_id = _company_id(conn, employer)
        plan = diff.compute_plan(store.company_jobs(conn, company_id), jobs)
        plan.missed, plan.removed = [], []
        diff.apply_plan(conn, run_id, company_id, plan, source="linkedin")
        counts["new"] += len(plan.new) + len(plan.reappeared)
        counts["changed"] += len(plan.changed)
        counts["seen"] += len(plan.unchanged)
    return counts
