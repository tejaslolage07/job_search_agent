"""Build config/india_companies.csv: candidate names (catalog_names.py) + live verification of public ATS feeds.

Verification uses the same PoliteClient as the crawler (robots.txt, honest UA, 1 req/s per host, stop on repeated 403/429).
A board is 'verified' only if >= 40% of its jobs are India-located (guards against slug collisions with unrelated firms).
Usage: python scripts/build_catalog.py probe   # slow (~30-40 min), resumable, writes data/catalog_probe.jsonl
       python scripts/build_catalog.py write   # builds config/india_companies.csv from probe results
"""
from __future__ import annotations

import csv
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
from catalog_names import RAW  # noqa: E402

from job_search_agent.http import BlockedError, FetchError, PoliteClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "data" / "catalog_probe.jsonl"
OUT = ROOT / "config" / "india_companies.csv"

# Not India-HQ (foreign firms' India arms) or product/sub-brand names rather than companies.
DROP = {n.lower() for n in """Cognizant India|Capgemini India|Accenture India|Amazon India Development Centre|Nagarro India|EPAM India|
GlobalLogic India|Virtusa India|Publicis Sapient India|Hexagon India|Sprinklr India|Clari India|Onfido India|Gojek India|Nutanix India|
Rubrik India|Cohesity India|Zscaler India|Pubmatic India|Bluecore India|Synechron India|Infogain India|Altimetrik India|Aptean India|
Chetu India|Crossover India|Itransition India|Imaginovation India|Devsinc India|Trading Technologies India|AlphaGrep India|
Tower Research India|Squarepoint India|Jane Street India|Zest AI India|Lucid Motors India|Alphawave India|Microchip India|
Airbnb India|SoftwareONE India|Skillsoft India|Genpact India|ThoughtWorks India|Innovaccer India|Innovaccer|Druva|Icertis|Lightspeed India|
Tekion India|Zoho Books|Zoho CRM|Zoho Creator|Zoho Desk|Zoho Learn|Zoho Backstage|Zoho Invoice|Zoho Schools|Zoho Corporation Pvt|
Mphasis NeXT|Mphasis Digital|Tata Elxsi Design|Tata Elxsi Medical|Tata Consultancy Services iON|Persistent AI|Zensar AI|Wipro Ventures|
Ola Cabs|Ola Cars|Ola Dash|Ola Financial|Ola Maps|Swiggy Instamart|Zomato Hyperpure|Zomato Ads|Zomato Labs|Dunzo Digital|Dunzo Daily|
Meesho Supply|Paytm Insider|Paytm Money|Byju's Whitehat|Cleartax Cleartrip|Razorpay X|Gupshup Technology|Gupshup AI|Sharechat Moj|
Naukri Campus|Cure.fit Tech|Practo Tech|Ultrahuman Health|IndMoney Global|Univest Tech|Whatfix Tech|Navi Technologies|Vymo Inc|
Zeta Tech|Zeta Suite|Pinelabs Plural|Kellton|Lenskart Solutions|Blinkit Labs|Healthkart|Tata Elxsi Medical|Vodafone Idea|
Gupshup|LatentView|Cleartax|Mindtree|Zomato|Jio Haptik|Ola Krutrim|Joshsoftware|Kovai|Dhan HQ|Money View|Sarvam|Rapido Captain|Navi Tech""".replace("\n", "").split("|")}

SUFFIX = re.compile(r"\b(technologies|technology|systems|software|solutions|services|labs|india|pvt|ltd|limited|corporation|corp|group|payments|"
                    r"networks|infotech|analytics|ai)\b", re.I)
INDIA = re.compile(r"india|bengaluru|bangalore|mumbai|delhi|gurugram|gurgaon|hyderabad|pune|chennai|noida|kolkata|ahmedabad|kochi|jaipur", re.I)


def candidates() -> list[tuple[str, str, str]]:
    seen, out = set(), []
    for band, block in RAW.items():
        for line in block.strip().splitlines():
            if not line.strip():
                continue
            name, basis = [p.strip() for p in line.split("|")]
            if name.lower() in DROP or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append((name, band, basis))
    return out


def slugs(name: str) -> list[str]:
    base = re.sub(r"\(.*?\)", "", name).replace("&", "and").lower()
    full = re.sub(r"[^a-z0-9]", "", base)
    hyph = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    stripped = re.sub(r"[^a-z0-9]", "", SUFFIX.sub("", base))
    first = re.sub(r"[^a-z0-9]", "", base.split()[0]) if base.split() else ""
    return [s for s in dict.fromkeys([full, hyph, stripped]) if len(s) >= 3] + ([first] if len(first) >= 5 and first not in (full, stripped) else [])


def _india_share(texts: list[str]) -> tuple[int, int]:
    return sum(1 for t in texts if INDIA.search(t or "")), len(texts)


ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
                   lambda d: [(j.get("location") or {}).get("name", "") for j in d.get("jobs", [])]),
    "lever": ("https://api.lever.co/v0/postings/{s}?mode=json",
              lambda d: [" ".join((j.get("categories") or {}).get("allLocations") or [(j.get("categories") or {}).get("location", "")]) for j in d]
              if isinstance(d, list) else []),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{s}",
              lambda d: [j.get("location", "") + " " + " ".join(x.get("location", "") for x in j.get("secondaryLocations", []))
                         for j in d.get("jobs", [])]),
}


def probe_ats(ats: str, cands: list[tuple[str, str, str]], done: set[tuple[str, str]], lock: threading.Lock) -> None:
    url_t, locs = ENDPOINTS[ats]
    client = PoliteClient(min_interval=1.0, max_retries=1)
    consecutive_errors = 0
    try:
        for name, _, _ in cands:
            if (ats, name) in done:
                continue
            hit, errored = None, False
            for s in slugs(name):
                try:
                    r = client.get(url_t.format(s=s))
                except BlockedError as exc:
                    print(f"[{ats}] BLOCKED, stopping this ATS: {exc}", flush=True)
                    return
                except FetchError:
                    errored = True  # 5xx / network: unknown, NOT 'not found'
                    continue
                if r.status_code != 200:
                    continue
                try:
                    india, total = _india_share(locs(r.json()))
                except Exception:
                    continue
                if total and india / total >= 0.4:
                    hit = {"token": s, "india": india, "total": total}
                    break
            if errored and not hit:
                consecutive_errors += 1
                print(f"[{ats}] {name}: fetch errors, not recorded (will retry on next probe run)", flush=True)
                if consecutive_errors >= 5:
                    print(f"[{ats}] {ats} unhealthy (5 companies in a row errored); stopping this ATS", flush=True)
                    return
                continue
            consecutive_errors = 0
            with lock, PROBE.open("a") as fh:
                fh.write(json.dumps({"ats": ats, "company": name, "hit": hit}) + "\n")
            if hit:
                print(f"[{ats}] {name}: {hit}", flush=True)
    finally:
        client.close()


def run_probe() -> None:
    PROBE.parent.mkdir(exist_ok=True)
    done = set()
    if PROBE.exists():
        done = {(r["ats"], r["company"]) for r in map(json.loads, PROBE.read_text().splitlines())}
    cands, lock = candidates(), threading.Lock()
    print(f"{len(cands)} candidates, {len(done)} probes already done", flush=True)
    with ThreadPoolExecutor(3) as pool:
        for ats in ENDPOINTS:
            pool.submit(probe_ats, ats, cands, done, lock)


def write_csv() -> None:
    hits: dict[str, tuple[str, dict]] = {}
    probed: set[tuple[str, str]] = set()
    for r in map(json.loads, PROBE.read_text().splitlines()):
        probed.add((r["ats"], r["company"]))
        if r["hit"] and r["company"] not in hits:
            hits[r["company"]] = (r["ats"], r["hit"])
    order = {"large": 0, "medium": 1, "small": 2, "micro": 3}
    rows = []
    for name, band, basis in candidates():
        ats, hit = hits.get(name, (None, None))
        portal = f"https://www.linkedin.com/jobs/search/?keywords={quote(name + ' software engineer')}&location=India"
        if hit:
            tok = hit["token"]
            url = {"greenhouse": f"https://job-boards.greenhouse.io/{tok}", "lever": f"https://jobs.lever.co/{tok}",
                   "ashby": f"https://jobs.ashbyhq.com/{tok}"}[ats]
            note = f"verified public {ats} feed: {hit['india']} of {hit['total']} jobs in India"
        else:
            url, ats, tok = "", "custom", ""
            missing = [a for a in ENDPOINTS if (a, name) not in probed]
            note = ("no public Greenhouse/Lever/Ashby feed found" if not missing
                    else f"no feed found on probed ATS; NOT probed on: {', '.join(missing)} (re-run probe)")
            note += "; find careers page manually or browse fallback_portal"
        rows.append({"company": name, "careers_url": url, "ats_type": ats if hit else "custom", "board_token": tok if hit else "",
                     "tier": "", "active": "false", "notes": note, "size_band": band,
                     "size_basis": "revenue (approx)" if basis == "R" else "employees (approx)",
                     "verified": "yes" if hit else "no", "fallback_portal": "" if hit else portal})
    rows.sort(key=lambda r: (order[r["size_band"]], r["verified"] != "yes", r["company"].lower()))
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    v = sum(r["verified"] == "yes" for r in rows)
    print(f"wrote {OUT}: {len(rows)} companies, {v} verified")
    for b in order:
        print(f"  {b}: {sum(r['size_band'] == b for r in rows)} total, {sum(r['size_band'] == b and r['verified'] == 'yes' for r in rows)} verified")


if __name__ == "__main__":
    {"probe": run_probe, "write": write_csv}[sys.argv[1]]()
