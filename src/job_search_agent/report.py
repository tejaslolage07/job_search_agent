"""Ranking and output formatting."""
from __future__ import annotations

import json
import sqlite3
import math
from datetime import date, datetime, timedelta
from typing import Any

import re

from . import timeutil


def parse_since(value: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([hdw])", value.strip().lower())
    if not m:
        raise ValueError(f"--since must look like 24h, 7d or 2w (got '{value}')")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n * (7 if unit == "w" else 1))


def _dt(s: str) -> datetime:
    return timeutil.parse(s)


def recency(row: sqlite3.Row | dict[str, Any]) -> str:
    return row["posted_at"] or row["first_seen_at"]


def calendar_cutoff(since: str, now: datetime | None = None) -> str:
    """'YYYY-MM-DD' (IST) of the oldest day a Posted filter includes. Dates, not hours: LinkedIn gives only a date, so
    'today' = today only, '2d' = today + yesterday (matches LinkedIn's own '24 hours'), '7d' = today + 6 earlier days.
    Legacy hour values ('24h', '48h') still work: they cover today plus enough earlier days."""
    s = since.strip().lower()
    if s == "today":
        days = 1
    elif m := re.fullmatch(r"(\d+)d", s):
        days = int(m.group(1))
    elif m := re.fullmatch(r"(\d+)h", s):
        days = math.ceil(int(m.group(1)) / 24) + 1
    else:
        raise ValueError(f"Posted filter must be 'today' or look like 2d, 7d, 30d (got '{since}')")
    if days < 1:
        raise ValueError("Posted filter must cover at least one day")
    return ((now.astimezone(timeutil.IST) if now else timeutil.now_ist()).date() - timedelta(days=days - 1)).isoformat()


def age(row: sqlite3.Row | dict[str, Any], now: datetime | None = None) -> str:
    """Age in calendar days (IST): the data only has dates, so 'today' / 'yesterday' / '3d', never invented hours."""
    days = ((now.astimezone(timeutil.IST) if now else timeutil.now_ist()).date() - date.fromisoformat(recency(row)[:10])).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days}d"


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest calendar day first; within a day, higher score first; then newest timestamp.
    Score can never move a job across a day boundary."""
    return sorted(rows, key=lambda r: (recency(r)[:10], r["final_score"], recency(r)), reverse=True)


def to_record(row: sqlite3.Row, job: sqlite3.Row, now: datetime | None = None) -> dict[str, Any]:
    """`row` is a match_results row, `job` the joined jobs row (with company_name)."""
    reasons = json.loads(row["reasons_json"] or "{}")
    top, order = [], {"reject": 0, "accept": 0, "plus": 1, "minus": 1, "neutral": 2}
    for r in sorted(reasons.get("reasons", []), key=lambda r: order.get(r["effect"], 2))[:3]:
        top.append(f"{r['rule']}: {r['detail']}")
    d = {k: row[k] for k in row.keys()}
    d.update({k: job[k] for k in ("title", "location", "url", "posted_at", "first_seen_at", "company_name")})
    d["id"] = job["id"]
    d.update(age=age(job, now), resume_read=row["resume_hash"] is not None, top_reasons=top)
    return d


def _clip(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


COLS = [("age", 9), ("company", 14), ("title", 38), ("location", 24), ("score", 5),
        ("phase", 5), ("resume", 6), ("variant", 8), ("top reasons", 60), ("url", 70)]


def _cells(r: dict[str, Any]) -> list[str]:
    return [r["age"], r["company_name"], r["title"], r["location"] or "", f"{r['final_score']:.0f}",
            str(r["stage_decided"]), "yes" if r["resume_read"] else "no", r["resume_variant"] or "-",
            " | ".join(r["top_reasons"]), r["url"]]


def render_table(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    def line(cells: list[str]) -> str:
        return "  ".join(_clip(c, w).ljust(w) for c, w in zip(cells, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths]), *[line(r) for r in rows]])


def format_matches(records: list[dict[str, Any]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps([{k: r[k] for k in ("id", "age", "company_name", "title", "location", "final_score",
                            "stage_decided", "resume_read", "resume_variant", "bucket", "top_reasons", "url", "posted_at")}
                           for r in records], indent=2)
    heads = [c for c, _ in COLS]
    if fmt == "md":
        out = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
        out += ["| " + " | ".join(c.replace("|", "/") for c in _cells(r)) + " |" for r in records]
        return "\n".join(out)
    return render_table(heads, [_cells(r) for r in records], [w for _, w in COLS])
