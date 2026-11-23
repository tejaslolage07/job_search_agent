"""SQLite persistence."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import timeutil
from .models import Company

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, careers_url TEXT NOT NULL,
  ats_type TEXT NOT NULL DEFAULT 'auto', board_token TEXT, tier INTEGER, active INTEGER NOT NULL DEFAULT 1,
  last_checked_at TEXT, discovery_method TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
  external_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, location TEXT, department TEXT,
  employment_type TEXT, posted_at TEXT, description_text TEXT, description_hash TEXT, raw_json TEXT,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','removed')),
  workplace_type TEXT, salary_text TEXT, missed_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(company_id, external_id));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, companies_checked INTEGER DEFAULT 0,
  new_count INTEGER DEFAULT 0, changed_count INTEGER DEFAULT 0, removed_count INTEGER DEFAULT 0,
  errors_json TEXT, fallbacks_json TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), job_id INTEGER REFERENCES jobs(id),
  type TEXT NOT NULL CHECK(type IN ('new','changed','removed','error')), detail_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match_results(
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), run_id INTEGER,
  stage_decided INTEGER NOT NULL, stage1_score REAL, stage1_confidence REAL, final_score REAL NOT NULL,
  bucket TEXT NOT NULL CHECK(bucket IN ('accept','review','reject')), reasons_json TEXT,
  resume_variant TEXT, resume_hash TEXT, profile_hash TEXT NOT NULL, matching_config_hash TEXT NOT NULL,
  method TEXT NOT NULL, job_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY, match_id INTEGER REFERENCES match_results(id), job_id INTEGER NOT NULL REFERENCES jobs(id), run_id INTEGER,
  created_at TEXT NOT NULL, phase INTEGER NOT NULL, resume_variant TEXT, model TEXT, status TEXT NOT NULL CHECK(status IN ('ok','error')), error TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0, request_json TEXT NOT NULL, response_json TEXT);
CREATE TABLE IF NOT EXISTS resume_cache(file_hash TEXT PRIMARY KEY, text TEXT NOT NULL, parsed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_events(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, ts TEXT NOT NULL, stage TEXT, level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL, data_json TEXT);
CREATE TABLE IF NOT EXISTS user_job_state(
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL CHECK(state IN ('saved','applied','hidden')),
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, interval_minutes INTEGER NOT NULL CHECK(interval_minutes >= 120),
  enabled INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL, next_run_at TEXT, last_run_at TEXT, last_run_id INTEGER,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS linkedin_usage(day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_run_events ON run_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id, status);
CREATE INDEX IF NOT EXISTS idx_match_job ON match_results(job_id, id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_job ON llm_calls(job_id, id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
"""


def now_iso() -> str:
    return timeutil.now_iso()


MIGRATIONS = {  # columns added after the first release; applied idempotently
    "jobs": {"source": "TEXT NOT NULL DEFAULT 'ats'"},
    "runs": {"kind": "TEXT", "status": "TEXT NOT NULL DEFAULT 'finished'", "stage": "TEXT", "progress_json": "TEXT",
             "config_json": "TEXT", "trigger": "TEXT"},
    "llm_calls": {"cache_write_tokens": "INTEGER NOT NULL DEFAULT 0"},
}


TIME_COLUMNS = {  # every column that holds a timestamp; rows written before the switch to IST end in 'Z' (UTC) and are converted once
    "companies": ["last_checked_at"], "jobs": ["posted_at", "first_seen_at", "last_seen_at"], "runs": ["started_at", "finished_at"],
    "events": ["created_at"], "match_results": ["created_at"], "llm_calls": ["created_at"], "resume_cache": ["parsed_at"], "run_events": ["ts"],
    "user_job_state": ["updated_at"], "schedules": ["next_run_at", "last_run_at", "created_at"],
}


def migrate_times_to_ist(conn: sqlite3.Connection, path: str | Path) -> int:
    """Convert old UTC ('...Z') timestamps to IST. Safe to run on every start (it only touches values ending in 'Z'). Before the first real conversion
    of a database file it keeps a copy next to it as '<name>.before-ist.bak'. Returns the number of values converted."""
    todo = {t: [c for c in cols if conn.execute(f"SELECT 1 FROM {t} WHERE {c} LIKE '%Z' LIMIT 1").fetchone()] for t, cols in TIME_COLUMNS.items()}
    todo = {t: cols for t, cols in todo.items() if cols}
    if not todo:
        return 0
    if str(path) != ":memory:":
        backup = Path(f"{path}.before-ist.bak")
        if not backup.exists():
            conn.commit()
            dest = sqlite3.connect(str(backup))
            with dest:
                conn.backup(dest)
            dest.close()
    conn.create_function("ist_time", 1, timeutil.migrate_legacy, deterministic=True)
    n = 0
    for table, cols in todo.items():
        for col in cols:
            n += conn.execute(f"UPDATE {table} SET {col} = ist_time({col}) WHERE {col} LIKE '%Z'").rowcount
    conn.commit()
    return n


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()
    migrate_times_to_ist(conn, path)
    return conn


def _bool(v: str | None) -> bool:
    return (v or "true").strip().lower() in {"true", "1", "yes", "y"}


def import_companies(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    """CSV is the source of truth. A previously detected ATS survives while ats_type=auto
    and careers_url is unchanged; companies missing from the CSV become inactive."""
    names = []
    for r in rows:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        names.append(name)
        url = (r.get("careers_url") or "").strip()
        ats = (r.get("ats_type") or "auto").strip().lower() or "auto"
        token = (r.get("board_token") or "").strip() or None
        tier = int(r["tier"]) if (r.get("tier") or "").strip().isdigit() else None
        cur = conn.execute("SELECT * FROM companies WHERE name=?", (name,)).fetchone()
        if cur and ats == "auto" and cur["careers_url"] == url and cur["ats_type"] != "auto":
            ats, token = cur["ats_type"], token or cur["board_token"]
        conn.execute(
            """INSERT INTO companies(name,careers_url,ats_type,board_token,tier,active,notes)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET careers_url=excluded.careers_url, ats_type=excluded.ats_type,
                 board_token=excluded.board_token, tier=excluded.tier, active=excluded.active, notes=excluded.notes""",
            (name, url, ats, token, tier, int(_bool(r.get("active"))), (r.get("notes") or "").strip()),
        )
    if names:
        marks = ",".join("?" * len(names))
        conn.execute(f"UPDATE companies SET active=0 WHERE name NOT IN ({marks})", names)
    conn.commit()


def row_to_company(r: sqlite3.Row) -> Company:
    return Company(r["id"], r["name"], r["careers_url"], r["ats_type"], r["board_token"], r["tier"], bool(r["active"]), r["notes"] or "")


def list_companies(conn: sqlite3.Connection, name: str | None = None, only_active: bool = True) -> list[Company]:
    q, args = "SELECT * FROM companies WHERE 1=1", []
    if only_active:
        q += " AND active=1"
    if name:
        q += " AND lower(name)=lower(?)"
        args.append(name)
    return [row_to_company(r) for r in conn.execute(q + " ORDER BY id", args)]


def save_detection(conn: sqlite3.Connection, company_id: int, ats: str, token: str | None) -> None:
    conn.execute("UPDATE companies SET ats_type=?, board_token=COALESCE(?,board_token) WHERE id=?", (ats, token, company_id))
    conn.commit()


def start_run(conn: sqlite3.Connection, kind: str = "crawl", config: dict[str, Any] | None = None, trigger: str = "manual") -> int:
    cur = conn.execute("INSERT INTO runs(started_at, kind, status, config_json, trigger) VALUES(?,?,'running',?,?)",
                       (now_iso(), kind, json.dumps(config) if config else None, trigger))
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def set_run_progress(conn: sqlite3.Connection, run_id: int, stage: str, progress: dict[str, Any]) -> None:
    row = conn.execute("SELECT progress_json FROM runs WHERE id=?", (run_id,)).fetchone()
    cur = json.loads(row["progress_json"]) if row and row["progress_json"] else {}
    cur[stage] = progress
    conn.execute("UPDATE runs SET stage=?, progress_json=? WHERE id=?", (stage, json.dumps(cur), run_id))
    conn.commit()


def log_run_event(conn: sqlite3.Connection, run_id: int, stage: str, message: str, level: str = "info",
                  data: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO run_events(run_id, ts, stage, level, message, data_json) VALUES(?,?,?,?,?,?)",
                 (run_id, now_iso(), stage, level, message, json.dumps(data) if data else None))
    conn.commit()


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute("UPDATE runs SET status=?, finished_at=COALESCE(finished_at, ?) WHERE id=?", (status, now_iso(), run_id))
    conn.commit()


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: int, checked: int, new: int, changed: int, removed: int,
               errors: list[dict[str, Any]], fallbacks: list[dict[str, Any]], status: str = "finished") -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?,companies_checked=?,new_count=?,changed_count=?,removed_count=?,errors_json=?,fallbacks_json=?,status=? WHERE id=?",
        (now_iso(), checked, new, changed, removed, json.dumps(errors), json.dumps(fallbacks), status, run_id),
    )
    conn.commit()


def add_event(conn: sqlite3.Connection, run_id: int | None, job_id: int | None, type_: str, detail: dict[str, Any]) -> None:
    conn.execute("INSERT INTO events(run_id,job_id,type,detail_json,created_at) VALUES(?,?,?,?,?)",
                 (run_id, job_id, type_, json.dumps(detail, default=str), now_iso()))


def company_jobs(conn: sqlite3.Connection, company_id: int) -> dict[str, sqlite3.Row]:
    return {r["external_id"]: r for r in conn.execute("SELECT * FROM jobs WHERE company_id=?", (company_id,))}


def latest_match(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM match_results WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def insert_match(conn: sqlite3.Connection, run_id: int | None, job_id: int, **f: Any) -> int:
    cols = ["job_id", "run_id", "created_at", *f]
    cur = conn.execute(f"INSERT INTO match_results({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       [job_id, run_id, now_iso(), *f.values()])
    return int(cur.lastrowid or 0)


def insert_llm_call(conn: sqlite3.Connection, match_id: int | None, run_id: int | None, job_id: int, call: dict[str, Any]) -> None:
    """One row per API call: the entire request body and response body as JSON, plus the token counts as columns."""
    conn.execute(
        "INSERT INTO llm_calls(match_id, job_id, run_id, created_at, phase, resume_variant, model, status, error, tokens_in, tokens_out, reasoning_tokens, "
        "cached_tokens, cache_write_tokens, retries, request_json, response_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, job_id, run_id, now_iso(), call["phase"], call.get("resume_variant"), call.get("model"), call["status"], call.get("error"),
         call.get("tokens_in", 0), call.get("tokens_out", 0), call.get("reasoning_tokens", 0), call.get("cached_tokens", 0), call.get("cache_write_tokens", 0), call.get("retries", 0),
         json.dumps(call.get("request") or {}, ensure_ascii=False), None if call.get("response") is None else json.dumps(call["response"], ensure_ascii=False)))
