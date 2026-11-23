"""Shared application services used by the dashboard (and reusable by the CLI)."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import timedelta, datetime
from pathlib import Path
from typing import Any

import yaml

from . import report, store, timeutil
from .config import MatchingConfig, Profile, load_matching, load_profile
from .linkedin import display_company
from .matching.engine import MatchEngine
from .matching.llm import LLMConfigError, build_judge
from .matching.resumes import ResumeLibrary

COMPANY_COLUMNS = ["company", "careers_url", "ats_type", "board_token", "tier", "active", "notes"]


@dataclass
class Home:
    root: Path

    @property
    def config(self) -> Path: return self.root / "config"
    @property
    def data(self) -> Path: return self.root / "data"
    @property
    def db(self) -> Path: return self.data / "job_search_agent.db"
    @property
    def resumes(self) -> Path: return self.data / "resumes"
    @property
    def profile_path(self) -> Path: return self.config / "profile.yaml"

    def conn(self) -> sqlite3.Connection:
        return store.connect(self.db)


# ---- profile + resumes -----------------------------------------------------------------------------------------------
def list_resumes(home: Home) -> list[dict[str, Any]]:
    if not home.resumes.exists():
        return []
    return [{"name": p.stem, "file": p.name, "bytes": p.stat().st_size} for p in sorted(home.resumes.glob("*.pdf"))]


def resume_paths(home: Home) -> dict[str, str]:
    return {r["name"]: str((home.resumes / r["file"]).resolve()) for r in list_resumes(home)}


def save_profile(home: Home, data: dict[str, Any]) -> Profile:
    """Validate (raises pydantic.ValidationError) and write config/profile.yaml. resume_paths always come from uploads."""
    data = {**data, "resume_paths": resume_paths(home)}
    profile = Profile.model_validate(data)
    home.config.mkdir(parents=True, exist_ok=True)
    home.profile_path.write_text(yaml.safe_dump(profile.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True))
    return profile


def read_profile(home: Home) -> dict[str, Any] | None:
    return load_profile(home.profile_path).model_dump() if home.profile_path.exists() else None


# ---- companies.csv + catalog -----------------------------------------------------------------------------------------
def read_companies_csv(home: Home) -> list[dict[str, str]]:
    path = home.config / "companies.csv"
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def write_companies_csv(home: Home, rows: list[dict[str, str]]) -> None:
    with (home.config / "companies.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COMPANY_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def catalog(home: Home) -> list[dict[str, str]]:
    path = home.config / "india_companies.csv"
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    active = {r["company"].lower(): r.get("active", "true").lower() in ("true", "1", "yes") for r in read_companies_csv(home)}
    for r in rows:
        r["in_crawler"] = "yes" if r["company"].lower() in active else "no"
        r["crawler_active"] = "yes" if active.get(r["company"].lower()) else "no"
    return rows


def upsert_company(home: Home, row: dict[str, str]) -> None:
    rows = read_companies_csv(home)
    for existing in rows:
        if existing["company"].lower() == row["company"].lower():
            existing.update({k: v for k, v in row.items() if k in COMPANY_COLUMNS})
            break
    else:
        rows.append({c: row.get(c, "") for c in COMPANY_COLUMNS})
    write_companies_csv(home, rows)


# ---- matching engine ---------------------------------------------------------------------------------------------------
def build_engine(conn: sqlite3.Connection, home: Home, transport: Any = None) -> tuple[MatchEngine, Profile, MatchingConfig]:
    """Raises LLMConfigError (key missing, bad prompt file) when the AI matcher cannot run; there is no fallback."""
    profile = load_profile(home.profile_path)
    cfg = load_matching(home.config / "matching.yaml")
    judge = build_judge(profile, cfg.llm, home.config, transport)
    return MatchEngine(profile, cfg, ResumeLibrary(profile.resume_paths, conn, base_dir=home.config), judge), profile, cfg


def active_job_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id "
                        "WHERE j.status='active'").fetchall()


# ---- job listing -----------------------------------------------------------------------------------------------------
JOB_SQL = """
SELECT j.id, j.title, j.location, j.url, j.salary_text, j.posted_at, j.first_seen_at, j.source, j.status, j.employment_type, c.name AS company_name,
       m.final_score, m.bucket, m.stage_decided, m.resume_hash, m.resume_variant, m.reasons_json, m.method, m.created_at AS scored_at,
       s.state AS user_state
FROM jobs j JOIN companies c ON c.id=j.company_id
JOIN match_results m ON m.id=(SELECT MAX(id) FROM match_results WHERE job_id=j.id)
LEFT JOIN user_job_state s ON s.job_id=j.id
WHERE j.status='active'"""


def _record(r: sqlite3.Row, now: datetime) -> dict[str, Any]:
    reasons = json.loads(r["reasons_json"] or "{}")
    order = {"reject": 0, "accept": 0, "plus": 1, "minus": 1, "neutral": 2}
    top = [f"{x['rule']}: {x['detail']}" for x in sorted(reasons.get("reasons", []), key=lambda x: order.get(x["effect"], 2))[:3]]
    d = {k: r[k] for k in r.keys() if k != "reasons_json"}
    d.update(company=display_company(r["company_name"]), age=report.age(r, now), salary=r["salary_text"] or "not stated",
             resume_read=r["resume_hash"] is not None, top_reasons=top)
    return d


def list_jobs(conn: sqlite3.Connection, bucket: str = "accept", q: str = "", company: str = "", source: str = "",
              since: str = "", min_score: float = 0, state: str = "", limit: int = 50, offset: int = 0, phase: str = "") -> dict[str, Any]:
    """`phase` (profile | resume) keeps only jobs the AI decided from the profile details / from the resume. `phases` counts both, before that filter."""
    cutoff = report.calendar_cutoff(since) if since else None  # oldest included day, 'YYYY-MM-DD'; raises ValueError if malformed
    rows = conn.execute(JOB_SQL).fetchall()
    now = timeutil.now_ist()
    counts = {"accept": 0, "review": 0, "reject": 0}
    phases = {"profile": 0, "resume": 0}
    wanted = {"profile": 1, "resume": 2}.get(phase)
    out = []
    for r in rows:
        if q and q.lower() not in (r["title"] + " " + r["company_name"] + " " + (r["location"] or "")).lower():
            continue
        if company and company.lower() not in r["company_name"].lower():
            continue
        if source and r["source"] != source:
            continue
        if cutoff and (r["posted_at"] or r["first_seen_at"])[:10] < cutoff:
            continue
        if r["final_score"] < min_score:
            continue
        if state == "none" and r["user_state"]:
            continue
        if state in ("saved", "applied", "hidden") and r["user_state"] != state:
            continue
        if not state and r["user_state"] == "hidden":
            continue
        if r["bucket"] != "review":  # a failed AI check has no deciding phase
            phases["profile" if r["stage_decided"] == 1 else "resume"] += 1
        if wanted is not None and (r["bucket"] == "review" or r["stage_decided"] != wanted):
            continue
        counts[r["bucket"]] += 1
        if bucket in ("all", r["bucket"]):
            out.append(_record(r, now))
    ranked = report.rank(out)
    by_company: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for j in out:  # the cut of the jobs in this view (selected bucket and filters)
        by_company[j["company"]] = by_company.get(j["company"], 0) + 1
        by_source[j["source"]] = by_source.get(j["source"], 0) + 1
    by_day: dict[str, int] = {}
    for j in out:
        day = (j["posted_at"] or j["first_seen_at"])[:10]
        by_day[day] = by_day.get(day, 0) + 1
    today = now.date()
    days = [((today - timedelta(days=i)).isoformat(), by_day.get((today - timedelta(days=i)).isoformat(), 0)) for i in range(13, -1, -1)]  # the last 14 calendar days, oldest first
    breakdown = {"companies": sorted(by_company.items(), key=lambda kv: (-kv[1], kv[0].lower())), "sources": sorted(by_source.items(), key=lambda kv: -kv[1]), "days": days}
    return {"counts": counts, "phases": phases, "breakdown": breakdown, "total": len(ranked), "all_ids": [j["id"] for j in ranked[:1000]], "jobs": ranked[offset: offset + limit]}


def job_detail(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    j = conn.execute("SELECT j.*, c.name AS company_name FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?", (job_id,)).fetchone()
    if not j:
        return None
    m = store.latest_match(conn, job_id)
    s = conn.execute("SELECT state FROM user_job_state WHERE job_id=?", (job_id,)).fetchone()
    events = [dict(e) for e in conn.execute("SELECT created_at, type, detail_json FROM events WHERE job_id=? ORDER BY id", (job_id,))]
    d = {k: j[k] for k in ("id", "title", "location", "url", "posted_at", "first_seen_at", "last_seen_at", "source", "status", "employment_type",
                           "department", "workplace_type", "salary_text", "description_text")}
    d["company"] = display_company(j["company_name"])
    d["user_state"] = s["state"] if s else None
    d["events"] = events
    d["match"] = None
    if m:
        data = json.loads(m["reasons_json"] or "{}")
        d["match"] = {"bucket": m["bucket"], "final_score": m["final_score"], "stage_decided": m["stage_decided"], "method": m["method"],
                      "resume_read": m["resume_hash"] is not None, "resume_variant": m["resume_variant"],
                      "reasons": data.get("reasons", []), "inputs": data.get("inputs", {}), "scored_at": m["created_at"]}
    return d
