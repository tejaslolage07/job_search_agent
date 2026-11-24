"""Find Keka / Zoho Recruit / Oracle tenants inside downloaded careers pages, test them with the real adapters (no DB writes),
and write results to data/hosted_test.jsonl. Usage: python scripts/resolve_hosted.py PAGES_DIR"""
from __future__ import annotations
import json, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from job_search_agent.http import PoliteClient
from job_search_agent.models import Company
from job_search_agent.sources import REGISTRY
from job_search_agent.sources.detect import _PATTERNS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "hosted_test.jsonl"
WANT = {"keka", "zoho", "oracle"}
rows = []
for f in Path(sys.argv[1]).glob("*.html"):
    t = f.read_text(errors="ignore")
    m = re.match(r"<!-- (.*?) \| ", t)
    if not m: continue
    for ats, pat in _PATTERNS:
        if ats in WANT and (h := pat.search(t)):
            tok = f"{h.group(1)}|{h.group(2)}" if ats == "oracle" else h.group(1)
            rows.append((m.group(1), ats, tok))
            break
print(len(rows), "tenants found", flush=True)
def one(r):
    name, ats, tok = r
    c = PoliteClient(min_interval=1.0, max_retries=1, timeout=20)
    res = {"company": name, "ats": ats, "token": tok}
    try:
        out = REGISTRY[ats].fetch(Company(0, name, "", ats, tok), c, set(), {})
        res.update(jobs=len(out.jobs), sample=[j.title for j in out.jobs[:3]], loc=[j.location for j in out.jobs[:2]], posted=out.jobs[0].posted_at if out.jobs else None)
    except Exception as e:
        res.update(jobs=0, error=f"{type(e).__name__}: {str(e)[:140]}")
    finally: c.close()
    with open(OUT, "a") as fh: fh.write(json.dumps(res) + "\n")
OUT.unlink(missing_ok=True)
with ThreadPoolExecutor(16) as p: list(p.map(one, rows))
