"""FastAPI dashboard backend. Single-user, local: bind to 127.0.0.1 (there is no login)."""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from .. import linkedin, service, store, timeutil
from ..config import ConfigError, MatchingConfig, Profile, load_matching
from ..matching.llm import LLMConfigError, load_prompt, resolve_model
from ..discovery_config import DiscoverConfig
from ..env import set_env_value
from .runner import RunController
from .scheduler import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, Scheduler, iso

STATIC = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_RESUME_BYTES = 10 * 1024 * 1024
FINAL = {"finished", "failed", "cancelled"}


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    interval_minutes: int = Field(ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)
    enabled: bool = True
    config: DiscoverConfig


class SchedulePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    interval_minutes: int | None = Field(None, ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)
    enabled: bool | None = None
    config: DiscoverConfig | None = None


class CompanyIn(BaseModel):
    company: str = Field(min_length=1, max_length=120)
    careers_url: str = Field(min_length=8, max_length=500)
    ats_type: str = "auto"
    board_token: str = ""
    tier: str = ""
    active: bool = True
    notes: str = ""


def _http_url(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


def create_app(home_root: Path | str = ".", controller: RunController | None = None, start_scheduler: bool = True) -> FastAPI:
    home = service.Home(Path(home_root).resolve())
    ctl = controller or RunController(home)
    sched = Scheduler(home, ctl)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        home.conn().close()  # create/migrate the database up front
        if start_scheduler:
            sched.start()
        yield
        sched.stop()

    app = FastAPI(title="job_search_agent", lifespan=lifespan)
    app.state.home, app.state.controller, app.state.scheduler = home, ctl, sched

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Block cross-site writes (CSRF / DNS-rebinding against a localhost tool without a login)."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    def db():  # type: ignore[no-untyped-def]
        return home.conn()

    # ---- pages / overview ------------------------------------------------------------------------------------
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        conn = db()
        try:
            counts = {r["bucket"]: r["n"] for r in conn.execute(
                "SELECT m.bucket, COUNT(*) n FROM jobs j JOIN match_results m ON m.id=(SELECT MAX(id) FROM match_results WHERE job_id=j.id) "
                "WHERE j.status='active' GROUP BY m.bucket")}
            total = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='active'").fetchone()[0]
            try:
                cand = (service.read_profile(home) or {}).get("name")
            except Exception:  # an invalid profile.yaml must not break the status call
                cand = None
            return {
                "candidate_name": cand, "home": str(home.root), "profile_exists": home.profile_path.exists(), "resumes": len(service.list_resumes(home)),
                "busy": ctl.busy(), "current_run_id": ctl.current_run_id,
                "openai_key_set": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                "linkedin": {"requests_today": linkedin.requests_today(conn)},
                "jobs": {"active": total, "scored": sum(counts.values()), **{k: counts.get(k, 0) for k in ("accept", "review", "reject")}},
            }
        finally:
            conn.close()

    # ---- profile / resumes / secrets -------------------------------------------------------------------------
    @app.get("/api/profile")
    def get_profile() -> dict[str, Any]:
        try:
            prof = service.read_profile(home)
        except ConfigError as exc:
            return {"exists": True, "invalid": str(exc), "profile": None, "resumes": service.list_resumes(home)}
        return {"exists": prof is not None, "profile": prof, "resumes": service.list_resumes(home)}

    @app.put("/api/profile")
    def put_profile(body: dict[str, Any]) -> dict[str, Any]:
        try:
            service.save_profile(home, body)
        except ValidationError as exc:
            errs = [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]} for e in exc.errors()]
            return JSONResponse({"errors": errs}, status_code=422)  # type: ignore[return-value]
        return {"saved": True}

    def _refresh_profile_resumes() -> None:
        if home.profile_path.exists():
            prof = service.read_profile(home) or {}
            service.save_profile(home, prof)

    @app.post("/api/resumes")
    async def upload_resume(name: str = Form(...), file: UploadFile = File(...)) -> dict[str, Any]:
        try:
            name = service.clean_resume_name(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        data = await file.read(MAX_RESUME_BYTES + 1)
        if len(data) > MAX_RESUME_BYTES:
            raise HTTPException(413, "resume too large (max 10 MB)")
        if not data.startswith(b"%PDF"):
            raise HTTPException(422, "only PDF files are accepted")
        service.save_resume(home, name, data)  # stored under a hash of the name, so the name can be anything
        _refresh_profile_resumes()
        return {"saved": name, "resumes": service.list_resumes(home)}

    @app.delete("/api/resumes")
    def delete_resume(name: str) -> dict[str, Any]:
        if not service.delete_resume(home, name):
            raise HTTPException(404, "no such resume")
        path.unlink()
        _refresh_profile_resumes()
        return {"resumes": service.list_resumes(home)}

    @app.get("/api/llm")
    def llm_status() -> dict[str, Any]:
        """Read-only: the OpenAI key lives in .env (never in the browser). Shows whether it is found, the model, and whether the prompt file is usable."""
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        try:
            cfg = load_matching(home.config / "matching.yaml")
        except ConfigError:
            cfg = MatchingConfig()
        prompt_path, resume_prompt_path, problem = home.config / cfg.llm.prompt_file, home.config / cfg.llm.resume_prompt_file, None
        try:
            for path in (prompt_path, resume_prompt_path):
                load_prompt(path)
        except (LLMConfigError, ConfigError) as exc:
            problem = str(exc)
        return {"openai_key_set": bool(key), "key_hint": f"{key[:3]}...{key[-4:]}" if len(key) > 12 else None, "model": resolve_model(cfg.llm),
                "concurrency": cfg.llm.concurrency, "env_file": str(home.root / ".env"), "prompt_file": str(prompt_path), "resume_prompt_file": str(resume_prompt_path), "prompt_problem": problem}

    @app.put("/api/llm/key")
    def set_llm_key(body: dict[str, Any]) -> dict[str, Any]:
        """Save the OpenAI key to the .env file on this computer and use it right away. It is never returned or shown again."""
        key = str(body.get("key", "")).strip()
        if len(key) < 20 or re.search(r"\s", key):
            raise HTTPException(422, "That does not look like an OpenAI API key (it should start with sk- and have no spaces).")
        set_env_value(home.root / ".env", "OPENAI_API_KEY", key)
        os.environ["OPENAI_API_KEY"] = key
        return {"openai_key_set": True, "key_hint": f"{key[:3]}...{key[-4:]}"}

    # ---- company catalog -------------------------------------------------------------------------------------
    @app.get("/api/catalog")
    def get_catalog(q: str = "", band: str = "", verified: str = "", crawler: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        rows = service.catalog(home)
        ql = q.lower()
        rows = [r for r in rows if (not ql or ql in r["company"].lower() or ql in (r.get("sector") or "").lower())
                and (not band or r["size_band"] == band) and (not verified or r["verified"] == verified)
                and (not crawler or r["crawler_active"] == crawler)]
        keep = ("company", "size_band", "size_basis", "verified", "ats_type", "careers_url", "hq_city", "sector", "portal_status",
                "fallback_portal", "in_crawler", "crawler_active", "notes")
        return {"total": len(rows), "rows": [{k: r.get(k, "") for k in keep} for r in rows[offset: offset + min(limit, 500)]]}

    @app.post("/api/catalog/select")
    def catalog_select(body: dict[str, Any]) -> dict[str, Any]:
        names, active = body.get("names") or [], bool(body.get("active", True))
        by_name = {r["company"].lower(): r for r in service.catalog(home)}
        added, skipped = [], []
        for n in names:
            r = by_name.get(str(n).lower())
            if not r:
                skipped.append({"company": n, "reason": "not in catalog"})
            elif not r["careers_url"] or not _http_url(r["careers_url"]):
                skipped.append({"company": n, "reason": "no working careers URL known (use the fallback portal or add one by hand)"})
            else:
                service.upsert_company(home, {"company": r["company"], "careers_url": r["careers_url"], "ats_type": r["ats_type"] or "auto",
                                              "board_token": r.get("board_token", ""), "tier": r.get("tier", ""),
                                              "active": "true" if active else "false", "notes": f"from catalog ({r['size_band']})"})
                added.append(r["company"])
        return {"updated": added, "skipped": skipped}

    @app.post("/api/companies")
    def add_company(c: CompanyIn) -> dict[str, bool]:
        if not _http_url(c.careers_url):
            raise HTTPException(422, "careers_url must start with http:// or https://")
        if c.ats_type not in ("auto", "greenhouse", "lever", "ashby", "workday", "custom"):
            raise HTTPException(422, "unknown ats_type")
        service.upsert_company(home, {**c.model_dump(), "active": "true" if c.active else "false"})
        return {"saved": True}

    @app.get("/api/companies")
    def crawler_companies() -> list[dict[str, str]]:
        return service.read_companies_csv(home)

    # ---- runs ------------------------------------------------------------------------------------------------
    def _run_row(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["progress"] = json.loads(d.pop("progress_json") or "{}")
        d["errors"] = json.loads(d.pop("errors_json") or "[]")
        d["config"] = json.loads(d.pop("config_json") or "null")
        d.pop("fallbacks_json", None)
        return d

    @app.get("/api/linkedin/defaults")
    def linkedin_defaults() -> dict[str, Any]:
        """Suggested search queries derived from the saved profile (user can edit freely)."""
        try:
            prof = service.read_profile(home)
        except ConfigError:
            prof = None
        if not prof:
            return {"queries": []}
        locs: list[str] = []
        for p in [x.strip() for x in prof["locations"]["preferred"] if x.strip()] or ["India"]:  # no preferred location: search India
            loc = re.sub(r"^remote[\s\-–:,/()]*", "", p, flags=re.I).strip() or "India" if p.lower().startswith("remote") else p
            if loc not in locs:
                locs.append(loc)
        # one search per title and location; full time, any workplace, any experience level, newest first
        queries = [{"keyword": t, "location": loc, "remote_filter": None, "job_type": "full time", "experience_level": None, "sort_by": "recent"}
                   for t in prof["target_titles"][:5] for loc in locs]
        return {"queries": queries[:25]}

    def _require_name() -> None:
        try:
            name = (service.read_profile(home) or {}).get("name")
        except Exception:  # an invalid profile.yaml is reported on the Profile page
            name = None
        if not (name or "").strip():
            raise HTTPException(422, "Please update your profile (add your name) before the job matcher can match jobs.")

    @app.post("/api/runs")
    def start_run(config: DiscoverConfig) -> dict[str, Any]:
        _require_name()
        try:
            run_id = ctl.start(config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @app.post("/api/jobs/rematch")
    def rematch_jobs(body: dict[str, Any]) -> dict[str, Any]:
        """Re-run the AI matching for exactly these jobs (for example after changing the profile), ignoring their cached results."""
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(422, "ids must be a non-empty list of job ids")
        ids = list(dict.fromkeys(ids))[:1000]
        conn = db()
        try:
            marks = ",".join("?" * len(ids))
            have = {r["id"] for r in conn.execute(f"SELECT id FROM jobs WHERE status='active' AND id IN ({marks})", ids)}
        finally:
            conn.close()
        if missing := [i for i in ids if i not in have]:
            raise HTTPException(422, f"not active jobs: {missing[:10]}")
        try:
            run_id = ctl.start(DiscoverConfig(ats={"enabled": False}, match=True, rematch_job_ids=ids), trigger="rematch")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "jobs": len(ids)}

    @app.get("/api/runs")
    def list_runs(limit: int = 30) -> list[dict[str, Any]]:
        conn = db()
        try:
            return [_run_row(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (min(limit, 200),))]
        finally:
            conn.close()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, Any]:
        conn = db()
        try:
            r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not r:
                raise HTTPException(404, "no such run")
            return _run_row(r)
        finally:
            conn.close()

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: int, after: int = 0) -> list[dict[str, Any]]:
        conn = db()
        try:
            return [dict(e) for e in conn.execute("SELECT id, ts, stage, level, message FROM run_events WHERE run_id=? AND id>? ORDER BY id LIMIT 500", (run_id, after))]
        finally:
            conn.close()

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: int) -> StreamingResponse:
        async def gen():  # type: ignore[no-untyped-def]
            last_event, last_sig = 0, ""
            while True:
                conn = db()
                try:
                    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                    if not r:
                        yield "event: error\ndata: {\"detail\": \"no such run\"}\n\n"
                        return
                    row = _run_row(r)
                    events = [dict(e) for e in conn.execute("SELECT id, ts, stage, level, message FROM run_events WHERE run_id=? AND id>? ORDER BY id", (run_id, last_event))]
                finally:
                    conn.close()
                sig = json.dumps([row["status"], row["progress"]], sort_keys=True)
                if events or sig != last_sig:
                    last_sig = sig
                    if events:
                        last_event = events[-1]["id"]
                    yield f"data: {json.dumps({'run': row, 'events': events})}\n\n"
                if row["status"] in FINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(1)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int) -> dict[str, bool]:
        if ctl.current_run_id != run_id:
            raise HTTPException(409, "that run is not in progress")
        return {"cancelling": ctl.cancel()}

    # ---- schedules -------------------------------------------------------------------------------------------
    def _sched_row(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        d["enabled"] = bool(d["enabled"])
        return d

    @app.get("/api/schedules")
    def list_schedules() -> list[dict[str, Any]]:
        conn = db()
        try:
            return [_sched_row(r) for r in conn.execute("SELECT * FROM schedules ORDER BY id")]
        finally:
            conn.close()

    @app.post("/api/schedules")
    def create_schedule(s: ScheduleIn) -> dict[str, Any]:
        _require_name()
        try:
            s.config.validate_runnable()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        now = timeutil.now_ist()
        conn = db()
        try:
            cur = conn.execute("INSERT INTO schedules(name, interval_minutes, enabled, config_json, next_run_at, created_at) VALUES(?,?,?,?,?,?)",
                               (s.name, s.interval_minutes, int(s.enabled), s.config.model_dump_json(), iso(now + timedelta(minutes=s.interval_minutes)), iso(now)))
            conn.commit()
            return _sched_row(conn.execute("SELECT * FROM schedules WHERE id=?", (cur.lastrowid,)).fetchone())
        finally:
            conn.close()

    @app.patch("/api/schedules/{sid}")
    def patch_schedule(sid: int, p: SchedulePatch) -> dict[str, Any]:
        conn = db()
        try:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "no such schedule")
            name = p.name or row["name"]
            interval = p.interval_minutes or row["interval_minutes"]
            enabled = row["enabled"] if p.enabled is None else int(p.enabled)
            config_json = row["config_json"]
            if p.config is not None:
                try:
                    p.config.validate_runnable()
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                config_json = p.config.model_dump_json()
            next_run = row["next_run_at"]
            if p.interval_minutes or (p.enabled and not row["enabled"]):
                next_run = iso(timeutil.now_ist() + timedelta(minutes=interval))
            conn.execute("UPDATE schedules SET name=?, interval_minutes=?, enabled=?, config_json=?, next_run_at=? WHERE id=?",
                         (name, interval, enabled, config_json, next_run, sid))
            conn.commit()
            return _sched_row(conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone())
        finally:
            conn.close()

    @app.delete("/api/schedules/{sid}")
    def delete_schedule(sid: int) -> dict[str, bool]:
        conn = db()
        try:
            conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
            conn.commit()
            return {"deleted": True}
        finally:
            conn.close()

    # ---- jobs ------------------------------------------------------------------------------------------------
    @app.get("/api/jobs")
    def jobs(bucket: str = "accept", q: str = "", company: str = "", source: str = "", since: str = "", min_score: float = 0,
             state: str = "", limit: int = 50, offset: int = 0, phase: str = "", day: str = "") -> dict[str, Any]:
        if phase not in ("", "profile", "resume"):
            raise HTTPException(422, "phase must be profile or resume")
        if bucket not in ("accept", "review", "reject", "all"):
            raise HTTPException(422, "bucket must be accept, review, reject or all")
        conn = db()
        try:
            return service.list_jobs(conn, bucket, q, company, source, since, min_score, state, min(limit, 200), offset, phase, day)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            conn.close()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int) -> dict[str, Any]:
        conn = db()
        try:
            d = service.job_detail(conn, job_id)
            if not d:
                raise HTTPException(404, "no such job")
            return d
        finally:
            conn.close()

    @app.put("/api/jobs/{job_id}/state")
    def job_state(job_id: int, body: dict[str, Any]) -> dict[str, Any]:
        st = body.get("state")
        conn = db()
        try:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise HTTPException(404, "no such job")
            if st in (None, "none"):
                conn.execute("DELETE FROM user_job_state WHERE job_id=?", (job_id,))
            elif st in ("saved", "applied", "hidden"):
                conn.execute("INSERT INTO user_job_state(job_id, state, updated_at) VALUES(?,?,?) "
                             "ON CONFLICT(job_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at", (job_id, st, store.now_iso()))
            else:
                raise HTTPException(422, "state must be saved, applied, hidden or none")
            conn.commit()
            return {"job_id": job_id, "state": None if st in (None, "none") else st}
        finally:
            conn.close()

    return app
