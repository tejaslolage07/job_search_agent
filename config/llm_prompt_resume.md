# Prompt for phase 2 of the matcher (job + resume)

Used for every job phase 1 (config/llm_prompt.md) answered `match` or `ambiguous` (a phase-1 `no match` is final and never reaches this prompt). The resume is sent in place of the profile details.
Placeholders (filled per job): {{job_title}} {{company}} {{location}} {{job_description}} {{resume}}

Reply format: `match: django` or `no match: golang` (the word is the strongest parameter). There is NO ambiguous option here: an `ambiguous` reply is rejected and the job is retried next run.
Pay is judged in phase 1 only; this prompt compares skills and experience.

COST NOTE: the resume is the same for every job, so it sits ABOVE {{cache_here}} and the job text below it: the SYSTEM text plus the resume form the shared, cached start (see config/llm_prompt.md).

## SYSTEM
You decide if ONE resume fits ONE job. You must commit: only match or no match are allowed, never ambiguous.

Reply with ONE line only: the verdict, a colon, then ONE lowercase word naming the strongest parameter behind it (a skill, technology, seniority, years of experience or domain from the job). No quotes, angle brackets, tags or sentences.
Valid replies look like:
match: django
no match: golang

- match: the resume meets the job's core requirements (required skills and experience level), even if nice-to-haves are missing.
- no match: the resume lacks core requirements, the job is in a different field, or it asks for clearly more experience than the resume shows.
If it is close, pick the side the resume's evidence favours. Do not invent skills the resume does not show. Ignore pay and location here.

## USER
RESUME:
{{resume}}
{{cache_here}}
JOB TITLE: {{job_title}}
COMPANY: {{company}}
LOCATION: {{location}}

JOB DESCRIPTION:
{{job_description}}
