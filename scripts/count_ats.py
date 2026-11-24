"""Which ATS/platform sits behind careers pages that returned no jobs? -> data/ats_count.jsonl (polite, read-only)"""
from __future__ import annotations
import json, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from job_search_agent.http import FetchError, PoliteClient

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ats_count.jsonl"
SIG = {
 "oracle_recruiting": r"oraclecloud\.com|/hcmRestApi|CandidateExperience|/sites/CX|requisitions\b",
 "smartrecruiters": r"smartrecruiters\.com", "workable": r"workable\.com|apply\.workable",
 "zoho_recruit": r"zohorecruit|zoho\.(?:com|in)/recruit|recruit\.zoho", "keka": r"keka(?:hr)?\.com",
 "darwinbox": r"darwinbox\.(?:in|com)", "successfactors": r"successfactors\.(?:com|eu)|jobs2web|career\d*\.successfactors",
 "taleo": r"taleo\.net", "icims": r"icims\.com", "recruitee": r"recruitee\.com", "bamboohr": r"bamboohr\.com",
 "freshteam": r"freshteam\.com", "greythr": r"greythr\.com", "peoplestrong": r"peoplestrong\.com", "workday": r"myworkdayjobs\.com|myworkdaysite",
 "lever": r"lever\.co", "greenhouse": r"greenhouse\.io", "ashby": r"ashbyhq\.com", "teamtailor": r"teamtailor\.com",
 "personio": r"personio\.(?:de|com)", "jobvite": r"jobvite\.com", "breezy": r"breezy\.hr", "pinpoint": r"pinpointhq\.com",
 "cutshort": r"cutshort\.io", "naukri_embed": r"naukri\.com", "linkedin_embed": r"linkedin\.com/(?:jobs|company)",
 "hirist/instahyre": r"instahyre\.com|hirist\.(?:tech|com)", "oracle_taleo_brassring": r"brassring\.com", "ukg/ultipro": r"ultipro\.com|ukg\.com",
 "jazzhr/other": r"applytojob\.com|jazz\.co",
}
def one(r):
    c = PoliteClient(min_interval=1.0, max_retries=0, timeout=15)
    res = {"company": r["company"], "url": r["url"], "hits": [], "js_shell": False}
    try:
        resp = c.get(r["url"]); t = resp.text[:600000]
        res["hits"] = [k for k, p in SIG.items() if re.search(p, t + " " + str(resp.url), re.I)]
        res["js_shell"] = len(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", t, flags=re.S).split()) < 150
        res["status"] = resp.status_code
    except FetchError as e:
        res["error"] = str(e)[:100]
    finally:
        c.close()
    with open(OUT, "a") as fh: fh.write(json.dumps(res) + "\n")
if __name__ == "__main__":
    rows = [json.loads(l) for l in open(ROOT / "data" / "test_crawl.jsonl")]
    rows = [r for r in rows if r["jobs"] == 0]
    done = {json.loads(l)["company"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
    rows = [r for r in rows if r["company"] not in done]
    print(len(rows), flush=True)
    with ThreadPoolExecutor(24) as p: list(p.map(one, rows))
