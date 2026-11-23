"""Crawl orchestration and match scoring over stored jobs."""
from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from . import diff, store
from .config import MatchingConfig, Profile
from .http import PoliteClient
from .matching.engine import MatchEngine
from .matching.types import JobInput
from .models import Company, sha256_text
from .sources import REGISTRY
from .sources.base import SourceError
from .sources.detect import detect_ats, detect_from_text

log = logging.getLogger(__name__)

TOKEN_ATS = {"greenhouse", "lever", "ashby"}


@dataclass
class CompanyOutcome:
    company: str
    method: str | None = None
    fetched: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0
    error: str | None = None
    fallbacks: list[str] = field(default_factory=list)


@dataclass
class CrawlSummary:
    run_id: int | None
    outcomes: list[CompanyOutcome]
    dry_run: bool

    @property
    def new(self) -> int: return sum(o.new for o in self.outcomes)
    @property
    def changed(self) -> int: return sum(o.changed for o in self.outcomes)
    @property
    def removed(self) -> int: return sum(o.removed for o in self.outcomes)
    @property
    def errors(self) -> list[CompanyOutcome]: return [o for o in self.outcomes if o.error]


def _resolve_source(conn: sqlite3.Connection, company: Company, client: PoliteClient, dry_run: bool) -> Company:
    ats, token = company.ats_type, company.board_token
    if ats == "auto":
        ats, found = detect_ats(company.careers_url, client)
        token = token or found
        log.info("%s: detected ATS=%s token=%s", company.name, ats, token)
        if not dry_run:
            store.save_detection(conn, company.id, ats, token)
    elif ats in TOKEN_ATS and not token:
        hit = detect_from_text(company.careers_url)
        token = hit[1] if hit else None
    company.ats_type, company.board_token = ats, token
    return company


ProgressFn = Callable[[dict[str, Any]], None]


def crawl(conn: sqlite3.Connection, client: PoliteClient, companies: list[Company], selectors: dict[str, Any],
          dry_run: bool = False, run_id: int | None = None, on_progress: ProgressFn | None = None,
          cancelled: Callable[[], bool] | None = None) -> CrawlSummary:
    """Crawl `companies`. With run_id=None this owns the run row (CLI); with a run_id the caller (dashboard runner)
    owns it and is responsible for finish_run. `on_progress` gets one dict per company start/finish."""
    owns_run = run_id is None and not dry_run
    if owns_run:
        run_id = store.start_run(conn)
    outcomes: list[CompanyOutcome] = []
    fallbacks_log: list[dict[str, Any]] = []
    emit = on_progress or (lambda e: None)
    for idx, company in enumerate(companies):
        if cancelled and cancelled():
            break
        emit({"event": "company_start", "company": company.name, "index": idx, "total": len(companies)})
        out = CompanyOutcome(company.name)
        outcomes.append(out)
        try:
            company = _resolve_source(conn, company, client, dry_run)
            source = REGISTRY.get(company.ats_type)
            if source is None:
                raise SourceError(f"unknown ats_type '{company.ats_type}'")
            existing = store.company_jobs(conn, company.id)
            result = source.fetch(company, client, set(existing), selectors)
            out.method, out.fetched, out.fallbacks = result.method, len(result.jobs), result.fallbacks
            if not result.jobs and any(r["status"] == "active" for r in existing.values()):
                raise SourceError("feed returned 0 jobs but jobs are stored; treating as a failed fetch (nothing removed)")
            plan = diff.compute_plan(existing, result.jobs)
            out.new, out.changed, out.removed = len(plan.new) + len(plan.reappeared), len(plan.changed), len(plan.removed)
            if not dry_run:
                diff.apply_plan(conn, run_id, company.id, plan)  # type: ignore[arg-type]
                conn.execute("UPDATE companies SET last_checked_at=?, discovery_method=? WHERE id=?",
                             (store.now_iso(), result.method, company.id))
                conn.commit()
            for fb in result.fallbacks:
                fallbacks_log.append({"company": company.name, "fallback": fb})
            emit({"event": "company_done", "company": company.name, "index": idx, "total": len(companies), "method": out.method,
                  "fetched": out.fetched, "new": out.new, "changed": out.changed, "removed": out.removed})
        except Exception as exc:  # one company failing must not stop the rest, nor touch its stored jobs
            out.error = f"{type(exc).__name__}: {exc}"
            log.warning("%s failed: %s", company.name, out.error)
            if not dry_run:
                store.add_event(conn, run_id, None, "error", {"company": company.name, "error": out.error})
                conn.commit()
            emit({"event": "company_error", "company": company.name, "index": idx, "total": len(companies), "error": out.error})
    if owns_run:
        store.finish_run(conn, run_id, len(companies), sum(o.new for o in outcomes), sum(o.changed for o in outcomes),  # type: ignore[arg-type]
                         sum(o.removed for o in outcomes),
                         [{"company": o.company, "error": o.error} for o in outcomes if o.error], fallbacks_log)
    return CrawlSummary(run_id, outcomes, dry_run)


def job_hash(row: sqlite3.Row) -> str:
    return sha256_text("|".join([row["title"] or "", row["location"] or "", row["workplace_type"] or "",
                                 row["salary_text"] or "", row["description_hash"] or ""]))[:16]


def to_input(row: sqlite3.Row) -> JobInput:
    return JobInput(company=row["company_name"], tier=row["tier"], title=row["title"], location=row["location"] or "",
                    workplace_type=row["workplace_type"], description=row["description_text"] or "",
                    salary_text=row["salary_text"])


def _needs_redo(last: sqlite3.Row, engine: MatchEngine) -> bool:
    """A failed AI check, or a verdict made with a different prompt / model, is redone."""
    if last["method"] == "llm-failed":
        return True
    try:
        llm = (json.loads(last["reasons_json"] or "{}").get("inputs") or {}).get("llm") or {}
    except ValueError:
        return True
    return llm.get("fingerprint") != engine.llm.fingerprint


def score_jobs(conn: sqlite3.Connection, engine: MatchEngine, profile: Profile, cfg: MatchingConfig,
               rows: list[sqlite3.Row], run_id: int | None, force: bool = False,
               on_progress: ProgressFn | None = None, cancelled: Callable[[], bool] | None = None) -> int:
    """Score jobs whose (job content, profile details, prompt/model) changed since their last result, or whose last AI check failed.

    Jobs are judged `llm.concurrency` at a time and each result is saved as soon as it arrives (so an interrupted run keeps what it
    finished; the rest are simply scored next time)."""
    ph, ch, scored = profile.matching_hash(), cfg.config_hash(), 0
    emit = on_progress or (lambda e: None)
    todo: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        jh = job_hash(row)
        last = store.latest_match(conn, row["id"])
        if (last and not force and last["job_hash"] == jh and last["profile_hash"] == ph and last["matching_config_hash"] == ch
                and not _needs_redo(last, engine)):
            continue
        todo.append((row, jh))
    emit({"event": "scored", "done": len(rows) - len(todo), "total": len(rows), "newly_scored": 0})
    if not todo or (cancelled and cancelled()):
        return 0
    engine.preload()  # resume files are read here, on this thread, once
    emit({"event": "llm", "done": 0, "total": len(todo)})
    with ThreadPoolExecutor(max_workers=engine.llm.cfg.concurrency) as pool:
        futures = {pool.submit(engine.score, to_input(row)): (row, jh) for row, jh in todo}
        for done, fut in enumerate(as_completed(futures), start=1):
            row, jh = futures[fut]
            out = fut.result()
            match_id = store.insert_match(
                conn, run_id, row["id"], stage_decided=out.phase, stage1_score=None, stage1_confidence=None, final_score=out.final_score,
                bucket=out.bucket, reasons_json=json.dumps({"reasons": [r.as_dict() for r in out.reasons], "inputs": out.inputs}, default=str),
                resume_variant=out.resume_variant, resume_hash=out.resume_hash, profile_hash=ph, matching_config_hash=ch, method=out.method, job_hash=jh)
            for call in out.llm_calls:
                store.insert_llm_call(conn, match_id, run_id, row["id"], call)
            scored += 1
            if scored % 25 == 0:
                conn.commit()
            emit({"event": "llm", "done": done, "total": len(todo)})
            emit({"event": "scored", "done": len(rows) - len(todo) + done, "total": len(rows), "newly_scored": scored})
            if cancelled and cancelled():
                for f in futures:
                    f.cancel()
                break
    conn.commit()
    return scored
