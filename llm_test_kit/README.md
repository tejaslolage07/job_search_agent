# AI matcher test kit (OpenAI)

Run the **real two-phase matcher** on your **own collected jobs** and see every verdict and one-word reason: phase 1 (job + profile details) answers `match` / `no match` / `ambiguous`; only
`ambiguous` jobs go to phase 2 (job + resume), which answers `match` / `no match`. Nothing in the main app's database is changed by running it.

## Setup (once)

1. Open the `.env` file in the project root and put your key on the one line: `OPENAI_API_KEY=sk-...` (the file is gitignored and readable only by you; never paste the key into a chat).
2. Check it: `.venv/bin/python llm_test_kit/run_test.py --list-models`. It confirms the key works and lists the models you can use. If the configured model is not in that list,
   set another one in `.env` (`OPENAI_MODEL=...`), in `config/matching.yaml` (`llm.model`), or with `--model`.

```bash
.venv/bin/python llm_test_kit/run_test.py --dry-run     # build the prompts + privacy check, call nothing (works without a key)
.venv/bin/python llm_test_kit/run_test.py               # 8 jobs, 8 at a time
.venv/bin/python llm_test_kit/run_test.py --limit 20 --select reject --source linkedin
.venv/bin/python llm_test_kit/run_test.py --ids 4821,5033          # specific jobs (ids are shown in the dashboard's job drawer and in results.csv)
.venv/bin/python llm_test_kit/run_test.py --model <another-model>   # try another model on the same jobs
```

## What it tests, and the files you edit

The kit is a thin front end over the **real matcher code**, so what you test here is what runs in the dashboard. It uses:

| Thing | Where it lives |
|---|---|
| The prompts | `config/llm_prompt.md` (phase 1, placeholders `{{job_title}} {{company}} {{location}} {{salary}} {{job_description}} {{profile}}`) and `config/llm_prompt_resume.md` (phase 2, `{{resume}}` instead of `{{profile}}`). The reply must be ONE line: `match: <word>`, `no match: <word>` or (phase 1 only) `ambiguous`. `<word>` is the single strongest parameter FOR the match, or AGAINST it for a no match. `## SYSTEM` is the instruction, `## USER` the per-job text. **Read fresh on every run**: edit and re-run. `--prompt` / `--resume-prompt` try another file for one run. |
| Model, reasoning effort, retries, timeout, text limits | the `llm:` section of `config/matching.yaml` (`OPENAI_MODEL` in `.env` overrides the model; `--model` / `--reasoning-effort` (phase 1) / `--resume-reasoning-effort` (phase 2) override them for one run) |
| The key | `OPENAI_API_KEY` in `.env` |
| Kit-only settings (`llm_test_kit/settings.yaml`) | concurrency (**8**), which jobs to sample, optional prices for a cost estimate |

## What you see

For every job, as soon as it finishes:

```
[3/8] #4821  Acme - Backend Engineer  (Bengaluru)
      6.9s | MATCH - strongest parameter: python  [profile said ambiguous -> resume]   |   stored result: match
```

then a table of all jobs (phase-1 answer, final answer, which phase decided) and a summary: final verdict counts, how phase 1 answered and how many jobs went on to the resume check, **which parameters were named most often**, time and jobs per minute,
**peak concurrency actually observed**, tokens (and how many were spent reasoning), the number of rate-limit retries, and an estimated cost if you filled in `pricing`. "Stored result" is what the dashboard currently holds for the job. Everything is also saved in `out/<timestamp>/`: `results.csv`, `results.json` (including the raw reply),
and `prompts/job_<id>.txt` (the exact phase-1 and phase-2 text for each job). `--dry-run` also prints the profile details that would be sent.

**Reply handling:** the reply must be one line in the requested form. Harmless variants (`Match: Python.`, `**match**: python`, a verdict and its word on two lines, `no_match: java`) are
accepted but counted as "needing cleanup", and so are a match / no match **without** a word, a word that is really **two** words, and an explanation attached to `ambiguous`: the
verdict is kept in all those cases. In phase 2 an `ambiguous` reply is not allowed: it is reported as a failed job. Anything that is not a verdict at all (a sentence, a different word) is reported as an off-spec reply for that job. These counts directly measure how well the prompt holds.

## Where the jobs and resume come from

* **Jobs:** your database, `data/job_search_agent.db`; random but reproducible (`seed`); `--select any|accept|review|reject` filters by the job's stored result. Jobs with a description shorter than 200 characters are skipped.
* **Profile:** `config/profile.yaml`; only `Profile.details_text()` is sent (including your current / expected pay and USD rate; not notice period or work authorization). The job's posted pay is sent as its own field.
* **Resume:** all uploaded variants in turn for phase 2 (`--resume NAME` uses one; `--resume-file x.txt` uses plain text instead of the uploaded PDFs).

## What is sent

Phase 1: the job (with its posted pay) and the profile details, **which include your current and expected CTC and the USD rate**. Phase 2: the job and the resume. Notice period, work authorization and resume file paths are never sent. `--dry-run` prints the exact profile text and saves every prompt so you can read it before anything leaves your machine.

## Good to know

* **Cost and limits:** you pay OpenAI per token on your key. The kit prints token counts; no prices are built in because they change: put your model's prices under `pricing:` in `llm_test_kit/settings.yaml` to get an estimate.
* **Rate limits:** a 429 or server error is retried with growing waits (it honours the `Retry-After` header) and counted in the summary. If the retry count keeps growing, lower `concurrency`.
  A rejected key, an unknown model, an exhausted quota or a bad request fails at once with a plain explanation, because retrying cannot help.
* **Reasoning models:** `llm.reasoning_effort` (phase 1) and `llm.resume_reasoning_effort` (phase 2) in `config/matching.yaml` (none / low / medium / high) trade speed and cost for care. Shipped: phase 1 `none`, phase 2 `low`. `max_output_tokens` counts the thinking too, so leave it unset unless you need a hard limit.
* A failed call is reported for that job only; the rest continue and the exit code is 1.
* The key is only read from `.env` (or a real `OPENAI_API_KEY` environment variable, which wins) and is never printed in full or saved in the results.
* Run `.venv/bin/pytest tests/test_llm_test_kit.py tests/test_openai_client.py` to test the kit itself (fake OpenAI, no real calls).
