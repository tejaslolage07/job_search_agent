"""Check hiring-portal URLs from the shared xlsx. Polite: robots.txt, honest UA, 1 req/s per host, stop on repeated 403/429.
Usage: python scripts/check_portals.py XLSX  -> data/portal_check.jsonl
LinkedIn search links are skipped (login-gated; never crawled)."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import openpyxl

from job_search_agent.http import BlockedError, FetchError, PoliteClient, RobotsDisallowed
from job_search_agent.sources.detect import detect_from_text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "portal_check.jsonl"
CAREER_WORDS = re.compile(r"career|jobs?\b|openings?|vacanc|join us|work with us|we.re hiring|opportunit", re.I)


def check(client: PoliteClient, company: str, url: str) -> dict:
    res = {"company": company, "url": url, "status": None, "final_url": None, "verdict": None, "ats": None, "token": None}
    try:
        r = client.get(url)
    except RobotsDisallowed:
        res["verdict"] = "robots_disallow"
        return res
    except BlockedError:
        res["verdict"] = "blocked_403_429"
        return res
    except FetchError as exc:
        res["verdict"] = "unreachable"
        res["error"] = str(exc)[:160]
        return res
    res["status"], res["final_url"] = r.status_code, str(r.url)
    if r.status_code in (404, 410):
        res["verdict"] = "dead"
    elif r.status_code in (401, 403):
        res["verdict"] = "blocked_403_429"
    elif r.status_code >= 400:
        res["verdict"] = f"http_{r.status_code}"
    else:
        text = r.text[:400_000] if "html" in r.headers.get("content-type", "html") else ""
        hit = detect_from_text(str(r.url)) or detect_from_text(text)
        if hit:
            res["ats"], res["token"] = hit
        res["verdict"] = "ok" if CAREER_WORDS.search(str(r.url) + " " + text) else "loads_no_career_words"
    return res


def run(xlsx: str) -> None:
    ws = openpyxl.load_workbook(xlsx)["Companies"]
    hdr = [c.value for c in ws[1]]
    rows = [dict(zip(hdr, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    todo = [(r["Company"], r["Hiring portal"]) for r in rows if r["Hiring portal"] and "linkedin.com" not in r["Hiring portal"]]
    by_host: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for c, u in todo:
        by_host[urlsplit(u).netloc].append((c, u))
    done = set()
    if OUT.exists():
        done = {json.loads(l)["company"] for l in OUT.read_text().splitlines()}

    def work(items: list[tuple[str, str]]) -> None:
        client = PoliteClient(min_interval=1.0, max_retries=1, timeout=20)
        try:
            for c, u in items:
                if c in done:
                    continue
                res = check(client, c, u)
                with open(OUT, "a") as fh:
                    fh.write(json.dumps(res) + "\n")
        finally:
            client.close()

    print(f"{len(todo)} portals on {len(by_host)} hosts", flush=True)
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(work, by_host.values()))


if __name__ == "__main__":
    run(sys.argv[1])
