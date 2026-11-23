"""Runs discovery in a background thread, one run at a time, with progress persisted to SQLite."""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .. import linkedin, pipeline, service, store
from ..config import ConfigError, load_selectors
from ..discovery_config import DiscoverConfig
from ..http import PoliteClient
from ..linkedin_collector import LinkedInCollector
from ..matching.llm import LLMConfigError

log = logging.getLogger(__name__)


class RunController:
    def __init__(self, home: service.Home, client_factory: Callable[[], PoliteClient] = PoliteClient,
                 collector_factory: Callable[[], LinkedInCollector] = LinkedInCollector, openai_transport: Any = None) -> None:
        self.home = home
        self.client_factory = client_factory
        self.collector_factory = collector_factory
        self.openai_transport = openai_transport  # tests plug a fake OpenAI in here
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._collector: LinkedInCollector | None = None
        self.current_run_id: int | None = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: DiscoverConfig, trigger: str = "manual") -> int:
        config.validate_runnable()
        with self._guard:
            if self.busy():
                raise RuntimeError("a run is already in progress")
            conn = self.home.conn()
            rematch = config.rematch_job_ids is not None
            run_id = store.start_run(conn, kind="rematch" if rematch else "discover", config=config.model_dump(), trigger=trigger)
            store.log_run_event(conn, run_id, "run", f"Run #{run_id} started ({trigger}): re-running the AI matching for {len(config.rematch_job_ids or [])} selected job(s)"
                                if rematch else f"Run #{run_id} started ({trigger})")
            conn.close()
            self._cancel.clear()
            self.current_run_id = run_id
            self._thread = threading.Thread(target=self._execute, args=(run_id, config), daemon=True, name=f"run-{run_id}")
            self._thread.start()
            return run_id

    def cancel(self) -> bool:
        if not self.busy():
            return False
        self._cancel.set()
        if self._collector:
            self._collector.terminate()
        return True

    def wait(self, timeout: float | None = None) -> None:  # used by tests
        if self._thread:
            self._thread.join(timeout)

    # ---- the run -------------------------------------------------------------------------------------------------
    def _execute(self, run_id: int, config: DiscoverConfig) -> None:
        conn = self.home.conn()
        ev = lambda stage, msg, level="info", data=None: store.log_run_event(conn, run_id, stage, msg, level, data)  # noqa: E731
        prog = lambda stage, d: store.set_run_progress(conn, run_id, stage, d)  # noqa: E731
        errors: list[dict[str, Any]] = []
        fallbacks: list[dict[str, Any]] = []
        totals = {"new": 0, "changed": 0, "removed": 0, "checked": 0}
        status = "finished"
        try:
            rematch = config.rematch_job_ids is not None   # a rematch run does no discovery at all
            if config.ats.enabled and not rematch:
                self._ats_stage(conn, run_id, config, totals, errors, fallbacks, ev, prog)
            if config.linkedin.enabled and not rematch and not self._cancel.is_set():
                self._linkedin_stage(conn, run_id, config, totals, errors, ev, prog)
            if config.match and not self._cancel.is_set():
                self._match_stage(conn, run_id, config, errors, ev, prog)
            if self._cancel.is_set():
                status = "cancelled"
                ev("run", "Run cancelled", "warning")
            else:
                ev("run", f"Run finished: {totals['new']} new, {totals['changed']} changed, {totals['removed']} removed, {len(errors)} problem(s)")
        except Exception as exc:  # noqa: BLE001 - last resort: never leave a run stuck in 'running'
            log.exception("run %s crashed", run_id)
            if self._collector:
                self._collector.terminate()  # never leave a LinkedIn collector running behind a failed run
            status = "failed"
            errors.append({"company": "(run)", "error": f"{type(exc).__name__}: {exc}"})
            ev("run", f"Run failed: {type(exc).__name__}: {exc}", "error")
        finally:
            store.finish_run(conn, run_id, totals["checked"], totals["new"], totals["changed"], totals["removed"], errors, fallbacks, status)
            self.current_run_id = None
            conn.close()

    def _ats_stage(self, conn, run_id, config, totals, errors, fallbacks, ev, prog) -> None:  # type: ignore[no-untyped-def]
        try:
            store.import_companies(conn, service.read_companies_csv(self.home))
            selectors = load_selectors(self.home.config / "selectors.yaml")
        except ConfigError as exc:
            errors.append({"company": "(config)", "error": str(exc)})
            ev("ats", str(exc), "error")
            return
        companies = store.list_companies(conn)
        if config.ats.companies is not None:
            wanted = {n.lower() for n in config.ats.companies}
            companies = [c for c in companies if c.name.lower() in wanted]
        if not companies:
            ev("ats", "No active companies to crawl (enable some on the Companies tab)", "warning")
            prog("ats", {"state": "skipped", "done": 0, "total": 0})
            return
        ev("ats", f"Crawling {len(companies)} company board(s)")
        st = {"state": "running", "done": 0, "total": len(companies), "current": None, "new": 0, "changed": 0, "removed": 0, "failed": 0}
        prog("ats", st)

        def on_progress(e: dict[str, Any]) -> None:
            if e["event"] == "company_start":
                st["current"] = e["company"]
            else:
                st["done"] = e["index"] + 1
                if e["event"] == "company_done":
                    for k in ("new", "changed", "removed"):
                        st[k] += e[k]
                    ev("ats", f"{e['company']}: {e['fetched']} jobs via {e['method']} (new {e['new']}, changed {e['changed']}, removed {e['removed']})")
                else:
                    st["failed"] += 1
                    ev("ats", f"{e['company']}: {e['error']}", "error")
            prog("ats", st)

        client = self.client_factory()
        try:
            summary = pipeline.crawl(conn, client, companies, selectors, run_id=run_id, on_progress=on_progress, cancelled=self._cancel.is_set)
        finally:
            client.close()
        totals["new"] += summary.new
        totals["changed"] += summary.changed
        totals["removed"] += summary.removed
        totals["checked"] += len(summary.outcomes)
        errors.extend({"company": o.company, "error": o.error} for o in summary.errors)
        for o in summary.outcomes:
            fallbacks.extend({"company": o.company, "fallback": f} for f in o.fallbacks)
        st["state"] = "cancelled" if self._cancel.is_set() else "done"
        st["current"] = None
        prog("ats", st)

    def _linkedin_stage(self, conn, run_id, config, totals, errors, ev, prog) -> None:  # type: ignore[no-untyped-def]
        li = config.linkedin
        workdir = self.home.data / "linkedin" / f"run_{run_id}"
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "details.jsonl"
        known = linkedin.known_description_ids(conn)
        collector_cfg = linkedin.collector_config(li, out, known)
        (workdir / "config.json").write_text(json.dumps({**collector_cfg, "skipIds": f"{len(known)} ids already have descriptions"}, indent=2))
        ev("linkedin", f"LinkedIn discovery: {len(li.queries)} search(es), up to {li.max_listings} listings")
        st: dict[str, Any] = {"state": "running", "phase": "search", "unique": 0, "max": li.max_listings, "descriptions_done": 0,
                              "descriptions_total": 0, "requests": 0, "rate_limited": 0}
        prog("linkedin", st)
        last_logged = {"search": 0, "details": 0}

        def on_progress(e: dict[str, Any]) -> None:
            stage = e.get("stage")
            if stage == "search_done":
                what = f"\"{e.get('keyword')}\" / {e.get('location')}"
                if e.get("reason") == "share_reached":
                    ev("linkedin", f"Search {what} collected its share of the listing limit ({e.get('found', 0)} new listings)")
                else:
                    ev("linkedin", f"Search {what} found everything LinkedIn had ({e.get('found', 0)} new listings)")
                return
            if stage == "backoff":
                if e.get("seconds", 0) >= 8:  # short waits are routine; long ones need explaining
                    ev("linkedin", f"LinkedIn is refusing requests ({e.get('reason')}); waiting {e.get('seconds')} s before retrying", "warning")
                return
            if stage not in ("search", "details"):
                return  # a message type this version doesn't know: ignore it rather than fail the run
            st["phase"], st["requests"], st["rate_limited"] = stage, e.get("requests", 0), e.get("rateLimited", st["rate_limited"])
            if stage == "search":
                unique = e.get("unique", st["unique"])
                st.update(unique=unique, query=f"{e.get('keyword')} / {e.get('location')}", query_index=e.get("query"), queries=e.get("queries"))
                if unique - last_logged["search"] >= 50:
                    last_logged["search"] = unique
                    ev("linkedin", f"Found {unique} listings so far")
            else:
                done, total = e.get("done", st["descriptions_done"]), e.get("total", st["descriptions_total"])
                st.update(descriptions_done=done, descriptions_total=total, failed=e.get("failed", 0))
                if done - last_logged["details"] >= 50 or (done == total and total):
                    last_logged["details"] = done
                    ev("linkedin", f"Descriptions: {done}/{total} ({e.get('rateLimited', 0)} rate-limit responses handled)")
            prog("linkedin", st)

        collector = self.collector_factory()
        self._collector = collector
        try:
            code, summary = collector.run(collector_cfg, on_progress)
        except RuntimeError as exc:
            errors.append({"company": "LinkedIn", "error": str(exc)})
            ev("linkedin", str(exc), "error")
            st["state"] = "failed"
            prog("linkedin", st)
            return
        finally:
            self._collector = None
        if summary:
            linkedin.record_usage(conn, int(summary.get("requestsThisRun", 0)))
        counts = linkedin.ingest(conn, run_id, out.with_suffix(".state.json"), out)
        totals["new"] += counts["new"]
        totals["changed"] += counts["changed"]
        ev("linkedin", f"Saved LinkedIn jobs: {counts['new']} new, {counts['changed']} changed, {counts['seen']} already known")
        if self._cancel.is_set():
            st["state"] = "cancelled"
        elif code == 2:
            reason = (summary or {}).get("blocked") or "blocked"
            msg = f"LinkedIn stopped us ({reason}). Partial results were saved; wait a while before running LinkedIn again."
            errors.append({"company": "LinkedIn", "error": msg})
            ev("linkedin", msg, "error")
            st["state"] = "blocked"
        elif code != 0:
            errors.append({"company": "LinkedIn", "error": f"collector exited with code {code}"})
            ev("linkedin", f"Collector exited with code {code}", "error")
            st["state"] = "failed"
        else:
            st["state"] = "done"
        prog("linkedin", st)

    def _match_stage(self, conn, run_id, config, errors, ev, prog) -> None:  # type: ignore[no-untyped-def]
        try:
            engine, profile, cfg = service.build_engine(conn, self.home, transport=self.openai_transport)
        except (ConfigError, LLMConfigError, RuntimeError) as exc:  # no key, no profile, bad prompt file: there is no non-AI fallback
            errors.append({"company": "(matching)", "error": str(exc)})
            ev("matching", f"Matching skipped: {exc}", "error")
            prog("matching", {"state": "skipped", "reason": str(exc)})
            return
        rows = service.active_job_rows(conn)
        if config.rematch_job_ids is not None:
            wanted = set(config.rematch_job_ids)
            rows = [r for r in rows if r["id"] in wanted]
            ev("matching", f"Re-running the AI matching for {len(rows)} selected job(s) with your current profile and prompts (earlier results are kept as history)")
        else:
            ev("matching", f"Scoring {len(rows)} active job(s) (only new or changed ones are re-scored)")
        st = {"state": "running", "done": 0, "total": len(rows), "newly_scored": 0}
        prog("matching", st)

        ev("matching", f"AI matcher: model {engine.llm.client.model}, {cfg.llm.concurrency} calls at a time (job + profile details; the resume only for unclear jobs; never pay)")

        def on_progress(e: dict[str, Any]) -> None:
            if e["event"] == "llm":
                st.update(llm_done=e["done"], llm_total=e["total"])
            else:
                st.update(done=e["done"], newly_scored=e["newly_scored"])
            prog("matching", st)

        n = pipeline.score_jobs(conn, engine, profile, cfg, rows, run_id, force=config.rematch_job_ids is not None, on_progress=on_progress,
                                cancelled=self._cancel.is_set)
        dist = conn.execute("SELECT m.bucket, m.stage_decided, COUNT(*) AS n FROM match_results m JOIN jobs j ON j.id=m.job_id "
                            "WHERE j.status='active' AND m.id IN (SELECT MAX(id) FROM match_results GROUP BY job_id) "
                            "GROUP BY m.bucket, m.stage_decided").fetchall()
        st.update(state="cancelled" if self._cancel.is_set() else "done", newly_scored=n,
                  )
        st["buckets"] = {}
        for r in dist:
            st["buckets"][r["bucket"]] = st["buckets"].get(r["bucket"], 0) + r["n"]
        st["decided_stage1"] = sum(r["n"] for r in dist if r["stage_decided"] == 1)
        st["decided_stage2"] = sum(r["n"] for r in dist if r["stage_decided"] == 2)
        this = {r["stage_decided"]: r["n"] for r in conn.execute("SELECT stage_decided, COUNT(*) AS n FROM match_results WHERE run_id=? AND bucket!='review' "
                                                                 "GROUP BY stage_decided", (run_id,))}
        st["run_profile"], st["run_resume"] = this.get(1, 0), this.get(2, 0)
        prog("matching", st)
        failed = st["buckets"].get("review", 0)
        if failed and engine.resume_error:   # the usual cause: phase 1 was unsure, but there is no resume to compare with
            msg = (f"{failed} job(s) were left 'Not checked': phase 1 was unsure about them and the resume could not be read ({engine.resume_error}). "
                   "Upload your resume on the Profile tab, then select those jobs on the Jobs tab and click Re-run matching (the next run will also pick them up).")
            errors.append({"company": "(AI check)", "error": msg})
            ev("matching", msg, "error")
        ev("matching", f"Checked {n} job(s) this run: {st['run_profile']} decided from the profile details, {st['run_resume']} needed the resume "
                       f"(all active jobs: {st['decided_stage1']} / {st['decided_stage2']})"
                       f"{f'; {failed} AI check(s) failed and will be retried next run' if failed else ''}", "error" if failed else "info")
