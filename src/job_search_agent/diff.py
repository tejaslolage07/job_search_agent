"""Diff a successful fetch against stored state and apply it.

Rules: "changed" = title/location/description differ. "removed" only after the job is
missing from 2 consecutive *successful* fetches of its company. A failed fetch never
reaches this module (callers skip it), so it can never mark anything removed.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import store
from .models import RawJob, sha256_text
from .parsers.html import normalize_location, normalize_ws

REMOVAL_AFTER_MISSES = 2


def description_hash(text: str | None) -> str:
    return sha256_text(normalize_ws(text or ""))


@dataclass
class Plan:
    new: list[RawJob] = field(default_factory=list)
    reappeared: list[tuple[sqlite3.Row, RawJob]] = field(default_factory=list)
    changed: list[tuple[sqlite3.Row, RawJob, list[str]]] = field(default_factory=list)
    unchanged: list[tuple[sqlite3.Row, RawJob]] = field(default_factory=list)
    missed: list[sqlite3.Row] = field(default_factory=list)  # missing once, still active
    removed: list[sqlite3.Row] = field(default_factory=list)
    duplicates: int = 0


def compute_plan(existing: dict[str, sqlite3.Row], fetched: list[RawJob]) -> Plan:
    plan = Plan()
    seen: dict[str, RawJob] = {}
    for job in fetched:
        job.location = normalize_location(job.location)
        if job.identity in seen:
            plan.duplicates += 1
            continue
        seen[job.identity] = job
    for ident, job in seen.items():
        row = existing.get(ident)
        if row is None:
            plan.new.append(job)
            continue
        if row["status"] == "removed":
            plan.reappeared.append((row, job))
            continue
        if job.raw.get("stub"):  # URL still listed but not re-fetched: nothing to compare
            plan.unchanged.append((row, job))
            continue
        fields = []
        if normalize_ws(job.title) != row["title"]:
            fields.append("title")
        if job.location != (row["location"] or ""):
            fields.append("location")
        if job.description_text is not None and description_hash(job.description_text) != row["description_hash"]:
            fields.append("description")
        (plan.changed.append((row, job, fields)) if fields else plan.unchanged.append((row, job)))
    for ident, row in existing.items():
        if ident not in seen and row["status"] == "active":
            (plan.removed if row["missed_count"] + 1 >= REMOVAL_AFTER_MISSES else plan.missed).append(row)
    return plan


def _values(job: RawJob) -> dict[str, object]:
    return {
        "url": job.url, "title": normalize_ws(job.title), "location": job.location,
        "department": job.department, "employment_type": job.employment_type, "posted_at": job.posted_at,
        "workplace_type": job.workplace_type, "salary_text": job.salary_text,
        "raw_json": json.dumps(job.raw, default=str),
    }


def apply_plan(conn: sqlite3.Connection, run_id: int, company_id: int, plan: Plan, source: str = "ats") -> None:
    ts = store.now_iso()
    for job in plan.new:
        desc = normalize_ws(job.description_text or "")
        v = _values(job)
        cur = conn.execute(
            """INSERT INTO jobs(company_id,external_id,url,title,location,department,employment_type,posted_at,
               description_text,description_hash,raw_json,first_seen_at,last_seen_at,status,workplace_type,salary_text,source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
            (company_id, job.identity, v["url"], v["title"], v["location"], v["department"], v["employment_type"],
             v["posted_at"], desc, description_hash(desc), v["raw_json"], ts, ts, v["workplace_type"], v["salary_text"], source),
        )
        store.add_event(conn, run_id, cur.lastrowid, "new", {"title": v["title"], "url": job.url})
    for row, job in plan.reappeared:
        conn.execute("UPDATE jobs SET status='active', missed_count=0, last_seen_at=? WHERE id=?", (ts, row["id"]))
        store.add_event(conn, run_id, row["id"], "new", {"reappeared": True, "title": row["title"]})
    for row, job, fields in plan.changed:
        v = _values(job)
        sets = ", ".join(f"{k}=?" for k in v)
        args = list(v.values())
        if "description" in fields:
            desc = normalize_ws(job.description_text or "")
            sets += ", description_text=?, description_hash=?"
            args += [desc, description_hash(desc)]
        conn.execute(f"UPDATE jobs SET {sets}, last_seen_at=?, missed_count=0 WHERE id=?", [*args, ts, row["id"]])
        store.add_event(conn, run_id, row["id"], "changed", {
            "fields": fields, "old_title": row["title"], "old_location": row["location"], "old_description_hash": row["description_hash"]})
    for row, job in plan.unchanged:
        conn.execute("UPDATE jobs SET last_seen_at=?, missed_count=0 WHERE id=?", (ts, row["id"]))
    for row in plan.missed:
        conn.execute("UPDATE jobs SET missed_count=missed_count+1 WHERE id=?", (row["id"],))
    for row in plan.removed:
        conn.execute("UPDATE jobs SET status='removed', missed_count=missed_count+1 WHERE id=?", (row["id"],))
        store.add_event(conn, run_id, row["id"], "removed", {"title": row["title"]})
    conn.commit()
