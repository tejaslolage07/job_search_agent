#!/usr/bin/env python3
"""Test kit for the AI matcher: run the REAL two-phase matcher on YOUR collected jobs and show every verdict (with a one-word reason).

    phase 1: job + profile details -> match / no match / ambiguous       phase 2 (only if ambiguous): job + resume -> match / no match

    .venv/bin/python llm_test_kit/run_test.py                 # 8 jobs, 8 at a time
    .venv/bin/python llm_test_kit/run_test.py --dry-run       # build the prompts, call nothing (no key needed)
    .venv/bin/python llm_test_kit/run_test.py --list-models   # smoke test: checks your key and shows the models it can use
    .venv/bin/python llm_test_kit/run_test.py --limit 20 --select any --model <other-model> --reasoning-effort low

This is a thin front end over the REAL matcher code (MatchEngine): the prompts are config/llm_prompt.md (phase 1) and config/llm_prompt_resume.md (phase 2), the model / reasoning effort / retries / text limits come from
the `llm:` section of config/matching.yaml, the key comes from OPENAI_API_KEY in .env, and every call goes through the same judge the matcher uses
(job text with its posted pay, profile details including your pay but not notice period / work authorization, and the resume text). So what you test here is what runs.

It takes real jobs from your database (data/job_search_agent.db), your profile and your resume, runs `concurrency` jobs at a time, prints every result
as it arrives plus a summary (including how many jobs needed phase 2), and saves everything under llm_test_kit/out/<timestamp>/.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

try:
    from job_search_agent import store, timeutil
    from job_search_agent.cli import resolve_home
    from job_search_agent.config import ConfigError, load_matching, load_profile
    from job_search_agent.env import load_env
    from job_search_agent.matching.engine import MatchEngine
    from job_search_agent.matching.llm import LLMConfigError, LLMJudge, load_prompt, make_client, resolve_model
    from job_search_agent.matching.openai_client import DEFAULT_BASE_URL, OpenAIError
    from job_search_agent.matching.resumes import ResumeLibrary, ResumeVariant
    from job_search_agent.matching.types import JobInput
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Could not import job_search_agent ({exc}).\nRun this with the project's Python:  .venv/bin/python llm_test_kit/run_test.py")

KIT = Path(__file__).resolve().parent
KitError = LLMConfigError                 # a problem the user can fix; printed without a traceback
BUCKET_TO_VERDICT = {"accept": "match", "reject": "no match", "review": "AI check failed"}
TRANSPORT = None                          # the tests plug a fake httpx transport in here; in real use it stays None


# ---------------------------------------------------------------------------------------------------------------------
# settings (kit-only: which jobs, how many at once, optional prices). Model / effort / retries / prompt live in config/matching.yaml.
# ---------------------------------------------------------------------------------------------------------------------
def load_settings(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    s = yaml.safe_load(path.read_text()) or {}
    s.setdefault("sample", {})
    overrides = {("concurrency",): args.concurrency, ("sample", "select"): args.select, ("sample", "source"): args.source,
                 ("sample", "limit"): args.limit, ("sample", "seed"): args.seed}
    for keys, value in overrides.items():
        if value is not None:
            node = s
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
    return s


# ---------------------------------------------------------------------------------------------------------------------
# jobs and resume
# ---------------------------------------------------------------------------------------------------------------------
JOB_SQL = """
SELECT j.id, j.title, j.location, j.source, j.salary_text, j.url, j.description_text, c.name AS company,
       m.bucket
FROM jobs j JOIN companies c ON c.id = j.company_id
LEFT JOIN match_results m ON m.id = (SELECT MAX(id) FROM match_results WHERE job_id = j.id)
WHERE j.status = 'active' AND length(COALESCE(j.description_text, '')) >= ?"""


def select_jobs(conn: Any, sample: dict[str, Any], ids: list[int] | None) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(JOB_SQL, (int(sample.get("min_description_chars", 200)),))]
    for r in rows:
        r["company"] = re.sub(r" \[LinkedIn\]$", "", r["company"])
    if ids:
        by_id = {r["id"]: r for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise KitError(f"job id(s) {missing} not found, not active, or have no usable description")
        return [by_id[i] for i in ids]
    select, source = sample.get("select", "any"), sample.get("source", "")
    keep = {"any": lambda r: True}.get(select, lambda r: r["bucket"] == select)
    if select not in ("any", "accept", "review", "reject"):
        raise KitError("sample.select must be one of: any, accept, review, reject (the matcher's last stored result for the job)")
    rows = [r for r in rows if keep(r) and (not source or r["source"] == source)]
    random.Random(sample.get("seed", 1)).shuffle(rows)
    return rows[: int(sample.get("limit", 8))]


def load_resumes(home: Path, conn: Any, resume_file: Path | None, profile: Any | None) -> dict[str, ResumeVariant]:
    if resume_file:
        return {resume_file.stem: ResumeVariant(resume_file.stem, resume_file, "", resume_file.read_text())}
    if profile is None or not profile.resume_paths:
        raise KitError("no resume found: upload one in the dashboard (Profile tab), or pass --resume-file some_resume.txt")
    return {v.name: v for v in ResumeLibrary(profile.resume_paths, conn, base_dir=home / "config").variants()}


# ---------------------------------------------------------------------------------------------------------------------
# calling the judge
# ---------------------------------------------------------------------------------------------------------------------
def api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def mask_key(key: str) -> str:
    return f"{key[:3]}...{key[-4:]} ({len(key)} characters)" if len(key) > 12 else "(too short to be a real key)"


def estimate_cost(settings: dict[str, Any], tokens_in: int, tokens_out: int) -> float | None:
    """Only when you filled in `pricing` in settings.yaml: prices change, so none are built in."""
    pr = settings.get("pricing") or {}
    if pr.get("input_per_million") is None or pr.get("output_per_million") is None:
        return None
    return tokens_in / 1e6 * float(pr["input_per_million"]) + tokens_out / 1e6 * float(pr["output_per_million"])


@dataclass
class Result:
    job: dict[str, Any]
    ok: bool = False
    error: str = ""
    seconds: float = 0.0
    verdict: str = ""        # match | no match (the final answer)
    reason: str = ""         # the one-word strongest parameter behind the final answer
    phase: int = 0           # 1 = decided from the profile details, 2 = decided from the resume
    calls: int = 0
    clean: bool = True       # False when a reply needed trimming (e.g. "Match.") to become the requested form
    raw: str = ""            # the final reply exactly as the model sent it
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    retries: int = 0         # attempts repeated after a rate limit / server error / timeout
    cost_usd: float | None = None
    phase1: str = ""         # the phase-1 verdict (match / no match / ambiguous)


class Tracker:
    """Counts how many jobs are being judged at once, so the summary can prove the concurrency."""

    def __init__(self) -> None:
        self.lock, self.now, self.peak = threading.Lock(), 0, 0

    def enter(self) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        with self.lock:
            self.now -= 1


def call_model(job: dict[str, Any], engine: MatchEngine, settings: dict[str, Any], tracker: Tracker) -> Result:
    res, t0 = Result(job), time.monotonic()
    tracker.enter()
    try:
        out = engine.score(JobInput(company=job["company"], tier=None, title=job["title"], location=job["location"] or "", workplace_type=None,
                                    description=job["description_text"], salary_text=job["salary_text"]))
        calls = out.inputs["llm"]["calls"]
        res.calls, res.phase = len(calls), out.phase
        res.tokens_in, res.tokens_out = sum(c["tokens_in"] for c in calls), sum(c["tokens_out"] for c in calls)
        res.reasoning_tokens, res.retries = sum(c["reasoning_tokens"] for c in calls), sum(c["retries"] for c in calls)
        res.phase1 = calls[0]["verdict"] if calls else ""
        res.cost_usd = estimate_cost(settings, res.tokens_in, res.tokens_out)
        if out.method == "llm-failed":
            res.error = out.inputs["llm"]["error"]
        else:
            last = calls[-1]
            res.ok, res.verdict, res.reason, res.clean, res.raw = True, last["verdict"], last["reason"], all(c["clean"] for c in calls), last["raw"]
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the test
        res.error = f"{type(exc).__name__}: {exc}"
    finally:
        tracker.leave()
        res.seconds = time.monotonic() - t0
    return res


# ---------------------------------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------------------------------
def stored(job: dict[str, Any]) -> str:
    return BUCKET_TO_VERDICT.get(job["bucket"] or "", "not scored yet")


def show_result(i: int, n: int, r: Result, lock: threading.Lock) -> None:
    j = r.job
    head = f"[{i:>{len(str(n))}}/{n}] #{j['id']}  {j['company']} - {j['title']}" + (f"  ({j['location']})" if j["location"] else "")
    with lock:
        if not r.ok:
            print(f"{head}\n         FAILED after {r.seconds:.1f}s: {r.error}\n", flush=True)
            return
        note = "" if r.clean else "   (a reply needed cleanup)"
        how = "profile details" if r.phase == 1 else "profile said ambiguous -> resume"
        why = f" - strongest parameter: {r.reason}" if r.reason else ""
        print(f"{head}\n         {r.seconds:.1f}s | {r.verdict.upper()}{why}  [{how}]{note}   |   stored result: {stored(j)}\n", flush=True)


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize(results: list[Result], wall: float, tracker: Tracker, concurrency: int) -> str:
    ok = [r for r in results if r.ok]
    lines = ["", "=" * 110, f"{'id':>6}  {'company':<20} {'title':<30} {'phase 1':<10} {'final':<9} {'by':<8} {'strongest parameter':<19} {'secs':>5}", "-" * 110]
    for r in results:
        lines.append(f"{r.job['id']:>6}  {clip(r.job['company'], 20):<20} {clip(r.job['title'], 30):<30} {(r.phase1 or '-'):<10} {(r.verdict if r.ok else 'ERROR'):<9} "
                     f"{('profile' if r.phase == 1 else 'resume' if r.phase == 2 else '-'):<8} {clip(r.reason or '-', 19):<19} {r.seconds:>5.1f}")
    lines += ["-" * 110, f"jobs: {len(results)} | succeeded: {len(ok)} | failed: {len(results) - len(ok)} | jobs with a reply needing cleanup: {sum(not r.clean for r in ok)}"]
    if ok:
        lat = [r.seconds for r in ok]
        lines.append("final verdicts: " + ", ".join(f"{v} {sum(r.verdict == v for r in ok)}" for v in ("match", "no match")))
        lines.append(f"phase 1 answered: " + ", ".join(f"{v} {sum(r.phase1 == v for r in results)}" for v in ("match", "no match", "ambiguous"))
                     + f"  ->  {sum(r.phase == 2 for r in ok)} went on to the resume check ({sum(r.calls for r in results)} API calls in total)")
        for v in ("match", "no match"):
            words = Counter(r.reason for r in ok if r.verdict == v and r.reason)
            if words:
                lines.append(f"strongest parameter behind '{v}': " + ", ".join(f"{w} x{n}" for w, n in words.most_common(8)))
        lines.append(f"time: wall {wall:.1f}s for {len(results)} jobs ({len(results) / wall * 60:.1f} jobs/min) | per job: mean {statistics.mean(lat):.1f}s, max {max(lat):.1f}s")
        lines.append(f"concurrency: configured {concurrency}, peak jobs in flight {tracker.peak}")
        reasoning = sum(r.reasoning_tokens for r in ok)
        lines.append(f"tokens: {sum(r.tokens_in for r in results):,} in / {sum(r.tokens_out for r in results):,} out" + (f" ({reasoning:,} of the output spent reasoning)" if reasoning else ""))
        costs = [r.cost_usd for r in results if r.cost_usd is not None]
        lines.append(f"estimated cost: ${sum(costs):.4f} (from the prices you set in settings.yaml)" if costs
                     else "estimated cost: not shown (put your model's prices under `pricing:` in settings.yaml to see one)")
        retried = sum(r.retries for r in results)
        lines.append(f"rate-limit / server-error retries: {retried}" + ("  <- lower the concurrency if this keeps growing" if retried >= len(results) else ""))
    lines.append("=" * 110)
    return "\n".join(lines)


def save_outputs(out_dir: Path, results: list[Result], prompts: dict[int, str], settings: dict[str, Any], request: dict[str, Any]) -> None:
    (out_dir / "prompts").mkdir(parents=True, exist_ok=True)
    for jid, text in prompts.items():
        (out_dir / "prompts" / f"job_{jid}.txt").write_text(text)
    rows = [{"job_id": r.job["id"], "company": r.job["company"], "title": r.job["title"], "location": r.job["location"], "source": r.job["source"], "url": r.job["url"],
             "stored_result": stored(r.job), "ok": r.ok, "error": r.error, "phase1_verdict": r.phase1 or None, "final_verdict": r.verdict or None, "decided_by_phase": r.phase or None,
             "reason": r.reason or None, "reply_was_clean": r.clean if r.ok else None, "raw_reply": r.raw if r.ok else None, "api_calls": r.calls, "seconds": round(r.seconds, 2),
             "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "reasoning_tokens": r.reasoning_tokens, "retries": r.retries, "cost_usd": r.cost_usd} for r in results]
    (out_dir / "results.json").write_text(json.dumps({"settings": settings, "request": request, "results": rows}, indent=2))
    with (out_dir / "results.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["job_id"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ask the OpenAI judge about your collected jobs and show the results.")
    p.add_argument("--home", type=Path, help="project folder (default: auto-detected)")
    p.add_argument("--settings", type=Path, default=KIT / "settings.yaml")
    p.add_argument("--prompt", type=Path, help="phase-1 prompt file (default: the matcher's own, config/llm_prompt.md)")
    p.add_argument("--resume-prompt", type=Path, help="phase-2 prompt file (default: config/llm_prompt_resume.md)")
    p.add_argument("--select", help="any | accept | review | reject (the stored result of the job)")
    p.add_argument("--source", help="linkedin | ats")
    p.add_argument("--limit", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--ids", help="comma-separated job ids (overrides --select/--limit)")
    p.add_argument("--resume", help="use only this resume variant for phase 2 (default: all of them)")
    p.add_argument("--resume-file", type=Path, help="use this plain-text resume instead of the uploaded PDFs")
    p.add_argument("--model", help="OpenAI model id (default: OPENAI_MODEL from .env, else llm.model in config/matching.yaml)")
    p.add_argument("--reasoning-effort", help="phase 1: none | low | medium | high (default: llm.reasoning_effort in config/matching.yaml)")
    p.add_argument("--resume-reasoning-effort", help="phase 2: none | low | medium | high (default: llm.resume_reasoning_effort in config/matching.yaml)")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--list-models", action="store_true", help="check the API key and list the models it can use, then stop")
    p.add_argument("--dry-run", action="store_true", help="build prompts, call nothing")
    p.add_argument("--out-dir", type=Path)
    return p.parse_args(argv)


def list_models(cfg: Any, model: str) -> int:
    key = api_key()
    if not key:
        raise KitError("OPENAI_API_KEY is not set: put it in .env first")
    client = make_client(cfg, key, TRANSPORT, model=model)
    try:
        models = client.list_models()
    except OpenAIError as exc:
        raise KitError(str(exc)) from exc
    finally:
        client.close()
    chat = [m for m in models if re.match(r"^(gpt|o\d|chatgpt)", m)]
    print(f"API key accepted ({mask_key(key)}). {len(models)} models available; chat-capable ones:\n  " + "\n  ".join(chat or models))
    print(f"\nconfigured model: {model}" + ("" if model in models else "   <- NOT in the list above: set another in .env (OPENAI_MODEL), config/matching.yaml (llm.model) or --model"))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KitError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


def run(args: argparse.Namespace) -> int:
    home = (args.home or resolve_home(None)).resolve()
    load_env(home / ".env")                      # OPENAI_API_KEY (and optionally OPENAI_MODEL); a real shell variable wins
    settings = load_settings(args.settings, args)
    try:
        cfg = load_matching(home / "config" / "matching.yaml").llm
    except ConfigError as exc:
        raise KitError(f"config/matching.yaml is invalid:\n{exc}") from exc
    if args.reasoning_effort is not None:
        cfg = cfg.model_copy(update={"reasoning_effort": args.reasoning_effort})
    if args.resume_reasoning_effort is not None:
        cfg = cfg.model_copy(update={"resume_reasoning_effort": args.resume_reasoning_effort})
    model = args.model or resolve_model(cfg)     # --model beats OPENAI_MODEL in .env, which beats config/matching.yaml
    if args.list_models:
        return list_models(cfg, model)
    concurrency = int(settings.get("concurrency") or cfg.concurrency)
    if concurrency < 1:
        raise KitError("concurrency must be at least 1")

    prompt_path, resume_prompt_path = args.prompt or home / "config" / cfg.prompt_file, args.resume_prompt or home / "config" / cfg.resume_prompt_file
    phase1, phase2 = load_prompt(prompt_path), load_prompt(resume_prompt_path)
    if not (home / "config" / "profile.yaml").exists():
        raise KitError("no profile yet: save one in the dashboard (Profile tab) or copy config/profile.example.yaml to config/profile.yaml")
    try:
        profile = load_profile(home / "config" / "profile.yaml")
    except ConfigError as exc:
        raise KitError(f"your profile.yaml is invalid:\n{exc}") from exc
    db = home / "data" / "job_search_agent.db"
    if not db.exists():
        raise KitError(f"no database at {db}: run a discovery first")
    conn = store.connect(db)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    jobs = select_jobs(conn, settings["sample"], ids)
    if not jobs:
        raise KitError("no jobs match the sample settings (try --select any, or collect more jobs)")
    resumes = load_resumes(home, conn, args.resume_file, profile)
    if args.resume and args.resume not in resumes:
        raise KitError(f"resume variant '{args.resume}' not found; available: {sorted(resumes)}")
    if args.resume:
        resumes = {args.resume: resumes[args.resume]}

    request = {"url": f"{cfg.base_url or DEFAULT_BASE_URL}/chat/completions", "model": model, "reasoning_effort": cfg.reasoning_effort or None, "resume_reasoning_effort": cfg.resume_reasoning_effort or None,
               "max_output_tokens": cfg.max_output_tokens, "prompt_file": str(prompt_path), "resume_prompt_file": str(resume_prompt_path)}
    key = api_key()
    if not key and not args.dry_run:
        raise KitError(f"OPENAI_API_KEY is not set. Put it in {home / '.env'} (one line: OPENAI_API_KEY=sk-...), then run again. "
                       "You can still try --dry-run without a key.")

    client = make_client(cfg, key or "dry-run", TRANSPORT, model=model)
    judge = LLMJudge(client, phase1, phase2, cfg, profile)    
    engine = MatchEngine(profile, load_matching(home / "config" / "matching.yaml"), type("Fixed", (), {"variants": lambda self: list(resumes.values())})(), judge)
    engine.preload()
    prompts: dict[int, str] = {}
    for j in jobs:
        p1 = judge.profile_prompt(j["title"], j["company"], j["location"] or "", j["description_text"], j["salary_text"] or "")
        p2 = "\n\n".join(f"##### PHASE 2 (only if phase 1 says ambiguous), resume variant {name}\n{judge.resume_prompt(j['title'], j['company'], j['location'] or '', j['description_text'], v.text)}"
                          for name, v in resumes.items())
        prompts[j["id"]] = (f"##### PHASE 1 SYSTEM PROMPT\n{phase1[0]}\n\n##### PHASE 1 USER PROMPT\n{p1}\n\n##### PHASE 2 SYSTEM PROMPT\n{phase2[0]}\n\n{p2}\n")

    print("=" * 110)
    print(f"AI matcher test (phase 1 profile: match / no match / ambiguous -> phase 2 resume: match / no match)  |  model {model}  |  {len(jobs)} jobs  |  concurrency {concurrency}  |  home {home}")
    print(f"prompts: {prompt_path} (phase 1), {resume_prompt_path} (phase 2)   settings: {args.settings}")
    print(f"sample: select={settings['sample'].get('select')} source={settings['sample'].get('source') or 'all'} seed={settings['sample'].get('seed')}  |  resume variants: {sorted(resumes)}")
    print("sent: job text + posted pay, profile details (including your current / expected pay; not notice period or work authorization), resume text in phase 2")
    print(f"request: POST {request['url']}  reasoning_effort: phase 1 {request['reasoning_effort'] or 'default'}, phase 2 {request['resume_reasoning_effort'] or 'default'}  |  API key: " + (mask_key(key) if key else "not needed for --dry-run"))
    print("profile details sent in phase 1:\n  " + judge.profile_text.replace("\n", "\n  "))
    print("=" * 110 + "\n")

    out_dir = args.out_dir or KIT / "out" / timeutil.now_ist().strftime("%Y%m%d-%H%M%S")
    if args.dry_run:
        client.close()
        save_outputs(out_dir, [], prompts, settings, request)
        print(f"DRY RUN: nothing was sent. The {len(prompts)} prompts that would be sent are in {out_dir / 'prompts'}")
        first = next(iter(prompts.values()))
        print("\n--- first job's phase-1 prompts, exactly as they would be sent ---\n" + first[:2200] + ("\n[...]" if len(first) > 2200 else ""))
        return 0

    tracker, lock, results = Tracker(), threading.Lock(), []
    t0 = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(call_model, j, engine, settings, tracker) for j in jobs]
            for done, fut in enumerate(as_completed(futures), start=1):
                r = fut.result()
                results.append(r)
                show_result(done, len(jobs), r, lock)
    finally:
        client.close()
    wall = time.monotonic() - t0
    order = {j["id"]: i for i, j in enumerate(jobs)}
    results.sort(key=lambda r: order[r.job["id"]])
    print(summarize(results, wall, tracker, concurrency))
    save_outputs(out_dir, results, prompts, settings, request)
    print(f"\nSaved: {out_dir}/results.csv, results.json and prompts/ (the exact text sent for each job)")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
