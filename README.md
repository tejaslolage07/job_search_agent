# job_search_agent

Monitors specific companies' public career pages, detects **new** listings, and ranks them against your profile, newest first.
Discovery and matching only: it never applies, logs in, or sends messages.

## Setup
```bash
/opt/homebrew/bin/python3.12 -m venv .venv && source .venv/bin/activate   # any Python >= 3.12
pip install -e ".[dev]"
cp config/profile.example.yaml config/profile.yaml   # then edit (gitignored)
# optional, only for JS-only custom sites:  pip install -e ".[browser]" && playwright install chromium
pytest
```
Edit `config/companies.csv` (source of truth, re-imported every run). Run from the project root, or set `JOB_SEARCH_AGENT_HOME`.

## Usage
```
job_search_agent run [--company NAME] [--dry-run]     # crawl, diff, save (dry-run: fetch+diff, saves no jobs/events/runs)
job_search_agent match [--since 24h|7d] [--min-score N] [--bucket accept|review|reject|all]
                       [--company NAME] [--format table|json|md] [--limit N] [--rescore]
job_search_agent new [--since 24h|7d]                 # raw newly-seen listings
job_search_agent status                               # last run, per-company counts, failures, expensive fallbacks
job_search_agent show JOB_ID [--raw]                  # full record + event history
job_search_agent explain JOB_ID                       # each AI call made for the job (phase, verdict, word, model, tokens)
```
`match` hides `reject` by default (accept + review); use `--bucket all` to see everything.

## How it works
**Discovery** (per company): detect ATS (`auto`) → Greenhouse / Lever / Ashby public JSON feeds → Workday CXS endpoint →
custom sites: JSON-LD `JobPosting` → sitemap job URLs → HTML selectors (`config/selectors.yaml`) → Playwright (only if `needs_js: true`; logged as an expensive fallback).

Feeds (verified against live boards): `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`,
`api.lever.co/v0/postings/{token}?mode=json`, `api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true`.

**Politeness**: robots.txt honoured, honest User-Agent, ≤1 req/s per host, retry with backoff on 5xx/network errors, and a host is abandoned after a second 403/429.

**Diff**: identity = `(company, external_id)` else hash of normalized URL. *Changed* = title/location/description differs.
*Removed* only after the job is absent from 2 consecutive successful fetches; a failed (or suspiciously empty) fetch never removes anything.

**Matching** (entirely AI-based, OpenAI; there are no rules and no keyword fallback)
- Phase 1: the model gets the job and your **profile details** (years of experience, target titles, seniority range, locations, must-have / nice-to-have skills, dealbreakers, companies to avoid, current / expected CTC, USD rate) plus the job's posted pay and answers `match: <word>` / `no match: <word>` / `ambiguous` (the word is the strongest parameter). Prompt: `config/llm_prompt.md`.
- Phase 2 (only for phase-1 `ambiguous`): the model gets the job and your **resume** in place of the profile details and must answer `match: <word>` or `no match: <word>`. Ambiguous is not allowed (the prompt forbids it and an ambiguous reply is rejected and retried). Prompt: `config/llm_prompt_resume.md`. With several resume variants, one match is enough.
- match = `accept` (score 100), no match = `reject` (0). If a check fails (no key, API error, off-spec reply, unreadable resume) the job stays in `review` ("Not checked", score 50) and is asked again on the next run.
- Needs `OPENAI_API_KEY` in `.env`. Calls run `llm.concurrency` (8) at a time. **What is sent:** the job (including its posted pay), the profile details (**including your current and expected CTC and the USD rate**, so the model can reject jobs that pay below your minimum) in phase 1, and the resume text in phase 2. Notice period, work authorization and resume paths are never sent. Model, effort, retries: the `llm:` section of `config/matching.yaml` (`OPENAI_MODEL` in `.env` overrides the model).
- **Cost controls** (`llm:` in `config/matching.yaml`): Thinking is billed as output, so it is set per phase: `reasoning_effort: none` for phase 1 (quick triage, most of the volume, answers are about 6 tokens) and `resume_reasoning_effort: low` for phase 2 (the careful resume check on the few unclear jobs). `cache: explicit` tells OpenAI to cache only the shared start of each prompt (instructions + profile in phase 1, instructions + resume in phase 2; the `{{cache_here}}` line in the prompt files marks where it ends) and read it back at about a tenth of the input price. Without it OpenAI writes every whole prompt to its cache at a 25% premium and never reads it back. The shared start must be at least about 1,024 tokens or nothing is cached, which is why `config/llm_prompt.md` carries a block of worked examples. Per-call tokens, including cached and cache-write tokens, are stored in the `llm_calls` table together with the full request and response.
- `resume_hash` is NULL in `match_results` unless phase 2 ran.
- Jobs are asked again only when job content (including posted pay), the profile details the AI sees (pay included), or the prompt/model change (or `--rescore`).

**Time zone**: everything is Indian Standard Time (IST, UTC+05:30). Timestamps in the database look like `2026-11-21T00:09:29+05:30`; timestamps that arrive in another zone (a source's `...Z` UTC time, an epoch number) are converted on the way in; a bare date (LinkedIn) stays that IST date at 00:00. The dashboard shows IST whatever the browser's zone is, the Posted filter and "today / yesterday" ages use IST calendar days, and schedules run on IST. Databases from before the switch are converted automatically on the next start (the original is kept next to it as `job_search_agent.db.before-ist.bak`). See `src/job_search_agent/timeutil.py`.

**Ranking**: newest calendar day first (`posted_at`, else `first_seen_at`); within a day higher score first. Score never overrides recency.

Matcher settings live in the `llm:` section of `config/matching.yaml`. Data: `data/job_search_agent.db` (SQLite; raw payloads kept in `jobs.raw_json`).

## Dashboard (local, single user)
```bash
job_search_agent serve            # http://127.0.0.1:8765  (no login: keep it on localhost)
```
Five tabs: **Jobs** (Matched / To review / Not a fit, each with an "Open job" link that opens the posting in a new tab; the tool never applies for you),
**Discover** (choose sources, matching is always AI, LinkedIn search options, start a run, or save it as a schedule: every 2 hours at the fastest),
**Runs** (live progress per stage, log, Cancel), **Companies** (pick career pages from the catalog), **Profile** (everything in `profile.yaml`, resume upload, and a read-only status of the OpenAI key in `.env`).

- **Re-run matching:** after changing your profile or prompts, tick jobs on the Jobs tab and click "Re-run matching for selected" (or "Re-run for all N in this view", or the button in a job's detail panel). It runs as its own run (kind `rematch`, no discovery) that asks the AI again about exactly those jobs, ignoring cached results; earlier results and API calls stay in the database as history. API: `POST /api/jobs/rematch {"ids": [...]}`.
- Runs never overlap; schedules wait for the current run. Schedules only fire while `serve` is running.
- LinkedIn: search options map to the guest-search filters, and the collector is plain Python (`src/job_search_agent/linkedin_collector.py`,
  `httpx` + `selectolax`; no Node needed). Search rows run one after another; "Parallel requests" sets how many result pages / descriptions
  are fetched at once (2-4 is gentler). "Max listings per run" is the only limit and is shared across your searches. Descriptions are
  downloaded only for jobs not already in the database. If LinkedIn refuses a request (429), the collector retries with a doubling wait (2, 4, 8, 16, 32, then 64 s); if it is still refused after 64 s the run stops and keeps what it already collected.
  LinkedIn rows are kept separate from the same company's career-page rows (duplicates are shown on purpose).
- Ad hoc collection to files: `job_search_agent linkedin-collect --target 200 --workers 3` (or `--config run.json`).
  Load an earlier collection: `job_search_agent linkedin-import data/linkedin_jobs.state.json data/linkedin_jobs.jsonl`.
- The OpenAI key lives only in `.env` at the project root (gitignored; see `.env.example`). The dashboard never asks for it, stores it, or shows it, only whether it was found. Test the AI matcher on your own jobs first with `llm_test_kit/run_test.py` (see its README).
