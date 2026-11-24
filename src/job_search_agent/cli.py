"""job_search_agent CLI."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__, pipeline, report, store, timeutil
from .env import load_env
from .config import ConfigError, load_companies_csv, load_matching, load_profile, load_selectors
from .http import PoliteClient
from .matching.engine import MatchEngine
from .matching.llm import LLMConfigError, build_judge
from .matching.resumes import ResumeLibrary

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Monitor company career pages, detect new jobs, rank them against your profile.")


def resolve_home(home: Path | None) -> Path:
    """Explicit --home / env wins. Otherwise use ./ when it looks like a project (has config/), else the folder this package was
    installed from, so running the command from e.g. ~/Downloads does not silently start an empty second copy of everything."""
    if home is not None:
        return home.resolve()
    if (Path.cwd() / "config").is_dir():
        return Path.cwd()
    project = Path(__file__).resolve().parents[2]
    return project if (project / "config").is_dir() else Path.cwd()


class Ctx:
    def __init__(self, home: Path, verbose: bool) -> None:
        self.home, self.verbose = home, verbose
        self.config = home / "config"
        self.db = home / "data" / "job_search_agent.db"

    def conn(self) -> sqlite3.Connection:
        return store.connect(self.db)


@app.callback()
def main(ctx: typer.Context,
         home: Annotated[Path | None, typer.Option(envvar="JOB_SEARCH_AGENT_HOME", help="Project root holding config/ and data/ "
                                                    "(default: the current folder if it has config/, else this project's folder)")] = None,
         verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
         version: Annotated[bool, typer.Option("--version")] = False) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s")
    ctx.obj = Ctx(resolve_home(home), verbose)
    load_env(ctx.obj.home / ".env")  # secrets such as OPENAI_API_KEY; a real shell variable always wins


def _fail(msg: str) -> typer.Exit:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _cutoff(since: str | None) -> str | None:
    if not since:
        return None
    try:
        return timeutil.fmt(timeutil.now_ist() - report.parse_since(since))
    except ValueError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def run(ctx: typer.Context,
        company: Annotated[str | None, typer.Option("--company", help="Only this company (name from companies.csv)")] = None,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Fetch and diff, but write no jobs/events/runs")] = False) -> None:
    """Crawl active companies, diff against stored jobs, save."""
    c: Ctx = ctx.obj
    try:
        rows = load_companies_csv(c.config / "companies.csv")
        selectors = load_selectors(c.config / "selectors.yaml")
    except ConfigError as exc:
        raise _fail(str(exc))
    conn = c.conn()
    store.import_companies(conn, rows)
    companies = store.list_companies(conn, name=company)
    if not companies:
        raise _fail(f"No active company matches '{company}'." if company else "No active companies in companies.csv.")
    client = PoliteClient()
    try:
        summary = pipeline.crawl(conn, client, companies, selectors, dry_run=dry_run)
    finally:
        client.close()
    for o in summary.outcomes:
        if o.error:
            typer.secho(f"  ✗ {o.company}: {o.error}", fg=typer.colors.RED)
        else:
            fb = f"  [fallback: {', '.join(o.fallbacks)}]" if o.fallbacks else ""
            typer.echo(f"  ✓ {o.company}: {o.method}, fetched {o.fetched} | new {o.new} changed {o.changed} removed {o.removed}{fb}")
    tag = "DRY RUN (nothing saved) " if dry_run else f"Run #{summary.run_id} "
    typer.echo(f"{tag}— companies {len(summary.outcomes)}, new {summary.new}, changed {summary.changed}, "
               f"removed {summary.removed}, errors {len(summary.errors)}, requests {client.request_count}")
    if summary.outcomes and len(summary.errors) == len(summary.outcomes):
        raise typer.Exit(2)


@app.command()
def match(ctx: typer.Context,
          since: Annotated[str | None, typer.Option(help="Only jobs posted (or first seen) on these calendar days (IST): today, 2d (=today+yesterday), 7d")] = None,
          min_score: Annotated[float, typer.Option(help="Minimum final_score")] = 0,
          bucket: Annotated[str | None, typer.Option(help="accept|review|reject|all (default: accept+review)")] = None,
          company: Annotated[str | None, typer.Option(help="Only this company")] = None,
          fmt: Annotated[str, typer.Option("--format", help="table|json|md")] = "table",
          limit: Annotated[int | None, typer.Option(help="Show at most N rows")] = None,
          rescore: Annotated[bool, typer.Option("--rescore", help="Ignore cached results")] = False) -> None:
    """Match active jobs with the AI (profile check, then a resume check for every job it does not reject; needs OPENAI_API_KEY in .env) and rank newest-first."""
    c: Ctx = ctx.obj
    if bucket not in (None, "accept", "review", "reject", "all"):
        raise _fail("--bucket must be accept, review, reject or all")
    if fmt not in ("table", "json", "md"):
        raise _fail("--format must be table, json or md")
    try:
        profile = load_profile(c.config / "profile.yaml")
        cfg = load_matching(c.config / "matching.yaml")
    except ConfigError as exc:
        raise _fail(str(exc))
    try:
        cutoff = report.calendar_cutoff(since) if since else None
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    conn = c.conn()
    q = ("SELECT j.*, c.name AS company_name, c.tier FROM jobs j JOIN companies c ON c.id=j.company_id "
         "WHERE j.status='active'")
    args: list[Any] = []
    if company:
        q += " AND lower(c.name)=lower(?)"
        args.append(company)
    if cutoff:
        q += " AND substr(COALESCE(j.posted_at, j.first_seen_at), 1, 10) >= ?"
        args.append(cutoff)
    jobs = conn.execute(q, args).fetchall()
    try:
        judge = build_judge(profile, cfg.llm, c.config)
    except LLMConfigError as exc:
        raise _fail(str(exc))
    engine = MatchEngine(profile, cfg, ResumeLibrary(profile.resume_paths, conn, base_dir=c.config), judge)
    last_run = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    n = pipeline.score_jobs(conn, engine, profile, cfg, jobs, last_run, force=rescore)
    marks = ",".join("?" * len(jobs)) or "NULL"
    res = {r["job_id"]: r for r in conn.execute(
        f"SELECT * FROM match_results WHERE id IN (SELECT MAX(id) FROM match_results WHERE job_id IN ({marks}) GROUP BY job_id)",
        [j["id"] for j in jobs])}
    now = timeutil.now_ist()
    records = []
    for j in jobs:
        m = res.get(j["id"])
        if not m:
            continue
        if bucket in (None,) and m["bucket"] == "reject":
            continue
        if bucket not in (None, "all") and m["bucket"] != bucket:
            continue
        if m["final_score"] < min_score:
            continue
        records.append(report.to_record(m, j, now))
    ranked = report.rank(records)
    if limit:
        ranked = ranked[:limit]
    typer.echo(report.format_matches(ranked, fmt))
    p1 = sum(1 for m in res.values() if m["stage_decided"] == 1)
    failed = sum(1 for m in res.values() if m["bucket"] == "review")
    typer.echo(f"\n{len(ranked)} shown of {len(jobs)} jobs in window ({n} newly scored). "
               f"Decided from profile: {p1}, from resume: {len(res) - p1}; resume sent for {len(res) - p1} job(s)"
               f"{f'; {failed} AI check(s) failed (retried next run)' if failed else ''}.",
               err=(fmt == "json"))


@app.command()
def new(ctx: typer.Context,
        since: Annotated[str, typer.Option(help="Window, e.g. 24h or 7d")] = "24h") -> None:
    """Raw newly-seen listings (no matching)."""
    c: Ctx = ctx.obj
    cutoff = _cutoff(since)
    rows = c.conn().execute(
        """SELECT j.*, c.name AS company_name, e.created_at AS seen FROM events e JOIN jobs j ON j.id=e.job_id
           JOIN companies c ON c.id=j.company_id WHERE e.type='new' AND e.created_at>=? ORDER BY e.id DESC""", (cutoff,)).fetchall()
    out = [[report.age(r), r["company_name"], r["title"], r["location"] or "", r["url"]] for r in rows]
    typer.echo(report.render_table(["age", "company", "title", "location", "url"], out, [9, 14, 44, 26, 80]))
    typer.echo(f"\n{len(rows)} new listing(s) in the last {since}.")


@app.command()
def status(ctx: typer.Context) -> None:
    """Last run, per-company counts and failures."""
    conn = ctx.obj.conn()
    run_ = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not run_:
        typer.echo("No runs yet. Try: job_search_agent run")
        return
    typer.echo(f"Last run #{run_['id']}: {run_['started_at']} → {run_['finished_at']}  checked={run_['companies_checked']} "
               f"new={run_['new_count']} changed={run_['changed_count']} removed={run_['removed_count']}")
    errors = json.loads(run_["errors_json"] or "[]")
    fallbacks = json.loads(run_["fallbacks_json"] or "[]")
    rows = conn.execute(
        """SELECT c.name, c.ats_type, c.discovery_method, c.last_checked_at, c.active,
           SUM(j.status='active') AS act, SUM(j.status='removed') AS rem FROM companies c
           LEFT JOIN jobs j ON j.company_id=c.id GROUP BY c.id ORDER BY c.id""").fetchall()
    typer.echo(report.render_table(
        ["company", "ats", "method", "active", "removed", "last ok", "enabled"],
        [[r["name"], r["ats_type"], r["discovery_method"] or "-", str(r["act"] or 0), str(r["rem"] or 0),
          r["last_checked_at"] or "never", "yes" if r["active"] else "no"] for r in rows], [20, 10, 16, 6, 7, 21, 7]))
    for e in errors:
        typer.secho(f"FAILED {e['company']}: {e['error']}", fg=typer.colors.RED)
    for f in fallbacks:
        typer.secho(f"EXPENSIVE FALLBACK {f['company']}: {f['fallback']}", fg=typer.colors.YELLOW)


def _job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT j.*, c.name AS company_name FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?", (job_id,)).fetchone()
    if not row:
        raise _fail(f"No job with id {job_id}")
    return row


@app.command()
def show(ctx: typer.Context, job_id: int, raw: Annotated[bool, typer.Option("--raw", help="Include raw payload")] = False) -> None:
    """Full job record and event history."""
    conn = ctx.obj.conn()
    j = _job(conn, job_id)
    for k in ("id", "company_name", "title", "location", "workplace_type", "department", "employment_type", "salary_text",
              "posted_at", "first_seen_at", "last_seen_at", "status", "missed_count", "external_id", "url", "description_hash"):
        typer.echo(f"{k:18} {j[k]}")
    typer.echo("\n--- description ---\n" + (j["description_text"] or "(not fetched)")[:4000])
    if raw:
        typer.echo("\n--- raw_json ---\n" + (j["raw_json"] or ""))
    typer.echo("\n--- events ---")
    for e in conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,)):
        typer.echo(f"{e['created_at']}  run#{e['run_id']}  {e['type']:8} {e['detail_json']}")


@app.command()
def explain(ctx: typer.Context, job_id: int) -> None:
    """Every rule that fired and the exact inputs used."""
    conn = ctx.obj.conn()
    j = _job(conn, job_id)
    m = store.latest_match(conn, job_id)
    if not m:
        raise _fail(f"Job {job_id} has not been scored yet. Run: job_search_agent match")
    typer.echo(f"{j['company_name']} — {j['title']}  ({j['location']})\n{j['url']}\n")
    typer.echo(f"bucket={m['bucket']}  decided in phase {m['stage_decided']} ({'profile details' if m['stage_decided'] == 1 else 'resume'})  method={m['method']}")
    typer.echo(f"resume_sent={'yes' if m['resume_hash'] else 'no'}  variant={m['resume_variant']}  resume_hash={m['resume_hash']}")
    typer.echo(f"profile_hash={m['profile_hash']}  matching_config_hash={m['matching_config_hash']}  scored_at={m['created_at']}\n")
    data = json.loads(m["reasons_json"])
    typer.echo("AI verdicts:")
    for r in data["reasons"]:
        typer.echo(f"  [{r['effect']:7}] {r['rule']}: {r['detail']}")
    typer.echo("\nCalls made (no profile text or resume text is stored here, only verdicts):\n" + json.dumps(data["inputs"], indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()


@app.command()
def serve(ctx: typer.Context,
          host: Annotated[str, typer.Option(help="Interface to bind. Keep 127.0.0.1: the dashboard has no login")] = "127.0.0.1",
          port: Annotated[int, typer.Option()] = 8765) -> None:
    """Start the local dashboard (single user, no login)."""
    import uvicorn

    from .web.app import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(f"WARNING: binding {host} exposes the dashboard (and your profile and keys) without any login.", fg=typer.colors.YELLOW, err=True)
    c: Ctx = ctx.obj
    typer.echo(f"Dashboard: http://{host}:{port}   (data in {c.home})")
    uvicorn.run(create_app(c.home), host=host, port=port, log_level="warning")


@app.command("linkedin-import")
def linkedin_import(ctx: typer.Context,
                    state_file: Annotated[Path, typer.Argument(help="e.g. data/linkedin_jobs.state.json (the listings)")],
                    details_file: Annotated[Path, typer.Argument(help="e.g. data/linkedin_jobs.jsonl (the descriptions)")]) -> None:
    """Load an earlier LinkedIn collection (written by `linkedin-collect`; the dashboard does this itself) into the database. Safe to repeat."""
    from . import linkedin

    for f in (state_file, details_file):
        if not f.exists():
            raise _fail(f"{f} not found")
    conn = ctx.obj.conn()
    run_id = store.start_run(conn, kind="import", trigger="cli")
    counts = linkedin.ingest(conn, run_id, state_file, details_file)
    store.set_run_status(conn, run_id, "finished")
    typer.echo(f"Imported LinkedIn jobs from {counts['companies']} employers: {counts['new']} new, {counts['changed']} changed, {counts['seen']} already known.")


@app.command("linkedin-collect")
def linkedin_collect(ctx: typer.Context,
                     config: Annotated[Path | None, typer.Option(help="JSON config (same shape the dashboard uses); other options are ignored")] = None,
                     target: Annotated[int, typer.Option(help="Max listings")] = 1000,
                     workers: Annotated[int, typer.Option(min=1, max=6, help="Parallel requests (searching and descriptions), 1-6; 2-4 is gentler")] = 2,
                     window: Annotated[str, typer.Option(help="24hr | past week | past month")] = "past month",
                     out: Annotated[Path, typer.Option(help="Output .jsonl (also writes .state.json and .summary.json next to it)")] = Path("data/linkedin_jobs.jsonl"),
                     skip_file: Annotated[Path | None, typer.Option(help="JSON list of job ids whose descriptions are already known")] = None) -> None:
    """Collect LinkedIn listings and descriptions to files (ad hoc; the dashboard does this for you). Stops on a block."""
    import json as _json

    from .linkedin_collector import DEFAULT_QUERIES, LinkedInCollector

    if config:
        cfg = _json.loads(config.read_text())
    else:
        cfg = {"queries": DEFAULT_QUERIES, "window": window, "maxListings": target, "workers": workers, "out": str(out),
               "skipIds": _json.loads(skip_file.read_text()) if skip_file else []}
    logging.getLogger("job_search_agent.linkedin_collector").setLevel(logging.INFO)
    code, summary = LinkedInCollector().run(cfg, lambda e: None)
    typer.echo(_json.dumps(summary, indent=2))
    if code == 2:
        typer.secho("Stopped: LinkedIn blocked us. Wait a while before trying again.", fg=typer.colors.RED, err=True)
    raise typer.Exit(code if code != 130 else 1)
