"""Dry-run the real source adapters on catalog rows (no DB writes). Usage: python scripts/test_crawl.py STATUS  -> data/test_crawl.jsonl"""
from __future__ import annotations

import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from job_search_agent.http import PoliteClient
from job_search_agent.models import Company
from job_search_agent.sources import REGISTRY
from job_search_agent.sources.detect import detect_ats, detect_from_text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "test_crawl.jsonl"


def one(r: dict) -> None:
    client = PoliteClient(min_interval=1.0, max_retries=1, timeout=15)
    res = {"company": r["company"], "url": r["careers_url"]}
    try:
        ats, token = r["ats_type"] or "auto", r["board_token"] or None
        if ats == "auto":
            ats, found = detect_ats(r["careers_url"], client)
            token = token or found
        elif ats in ("greenhouse", "lever", "ashby") and not token:
            hit = detect_from_text(r["careers_url"])
            token = hit[1] if hit else None
        c = Company(id=0, name=r["company"], careers_url=r["careers_url"], ats_type=ats, board_token=token)
        out = REGISTRY[ats].fetch(c, client, set(), {})
        res.update(ats=ats, method=out.method, jobs=len(out.jobs), fallbacks=out.fallbacks,
                   sample=[j.title for j in out.jobs[:3]])
    except Exception as exc:
        res.update(jobs=0, error=f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        client.close()
    with open(OUT, "a") as fh:
        fh.write(json.dumps(res) + "\n")


if __name__ == "__main__":
    status = sys.argv[1]
    rows = [r for r in csv.DictReader(open(ROOT / "config" / "india_companies.csv", newline="")) if r["portal_status"] == status and r["careers_url"]]
    done = {json.loads(l)["company"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
    rows = [r for r in rows if r["company"] not in done]
    print(len(rows), "to test", flush=True)
    with ThreadPoolExecutor(24) as pool:
        list(pool.map(one, rows))
