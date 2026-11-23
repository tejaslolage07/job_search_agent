"""Append companies from the shared xlsx that are missing from config/india_companies.csv, using data/portal_check.jsonl results."""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import openpyxl

sys.path.insert(0, str(Path(__file__).parent))
from build_catalog import ENDPOINTS, _india_share  # noqa: E402

from job_search_agent.http import FetchError, PoliteClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "India_Software_Hiring_Companies_v2.xlsx"
CATALOG = ROOT / "config" / "india_companies.csv"
STOP = {"technologies", "technology", "systems", "software", "solutions", "services", "labs", "india", "pvt", "ltd", "limited", "corporation",
        "corp", "group", "private", "the", "and", "of", "ai", "tech", "infotech", "bank", "finance", "financial"}
# Share a name prefix with an existing row but are different companies: keep as NEW.
NOT_DUP = {"Aditya Birla Capital", "Bajaj Finance", "Bajaj Finserv", "ICICI Lombard", "Jio Financial Services", "L&T Technology Services",
           "Larsen & Toubro", "Tata AIA", "L&T Semiconductor Technologies", "TeamLease Services", "HDFC Life", "Tata Capital"}
VERDICT_TEXT = {"ok": "Checked: loads, careers wording found", "dead": "Checked: dead (404/410)",
                "blocked_403_429": "Checked: 401/403 (bot-blocked; may work in a browser)", "robots_disallow": "Checked: robots.txt disallows automated fetch",
                "unreachable": "Checked: unreachable / redirect loop", "loads_no_career_words": "Checked: loads, careers wording not in static HTML (JS page?)"}


def toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower().replace("&", "and")) if t not in STOP}


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", s.lower()))


def is_dup(name: str, existing: list[str]) -> bool:
    if any(squash(name) == squash(e) for e in existing):
        return True
    if name in NOT_DUP:
        return False
    t = toks(name)
    for e in existing:
        te = toks(e)
        if t and te and (t <= te or te <= t) and len(t & te) / max(len(t), len(te)) >= 0.5:
            return True
    return False


def verify_feed(client: PoliteClient, ats: str, token: str) -> tuple[bool, str]:
    url_t, locs = ENDPOINTS[ats]
    try:
        r = client.get(url_t.format(s=token))
        if r.status_code != 200:
            return False, f"feed returned HTTP {r.status_code}"
        india, total = _india_share(locs(r.json()))
        return total > 0, f"feed has {total} jobs ({india} India)"
    except (FetchError, ValueError) as exc:
        return False, f"feed check failed: {str(exc)[:60]}"


def main() -> None:
    ws = openpyxl.load_workbook(XLSX)["Companies"]
    hdr = [c.value for c in ws[1]]
    rows = [dict(zip(hdr, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    checks = {j["company"]: j for j in map(json.loads, (ROOT / "data" / "portal_check.jsonl").read_text().splitlines())}
    cat = list(csv.DictReader(CATALOG.open()))
    existing = [r["company"] for r in cat]
    client = PoliteClient(min_interval=1.0, max_retries=1)
    added, skipped = [], []
    for r in rows:
        name = r["Company"].strip()
        if is_dup(name, existing + [a["company"] for a in added]):
            skipped.append(name)
            continue
        chk = checks.get(name)
        portal = r["Hiring portal"]
        linkedin = "linkedin.com" in portal
        working = chk and chk["verdict"] in ("ok", "loads_no_career_words")
        careers = (chk["final_url"] or portal) if working else ""
        ats, token, verified, note = "custom", "", "no", ""
        if working and chk["ats"] in ENDPOINTS:
            ok, msg = verify_feed(client, chk["ats"], chk["token"])
            ats, token, verified = chk["ats"], chk["token"], "yes" if ok else "no"
            note = f"{chk['ats']} board detected on careers page; {msg}"
            if ok:
                careers = {"greenhouse": f"https://job-boards.greenhouse.io/{token}", "lever": f"https://jobs.lever.co/{token}",
                           "ashby": f"https://jobs.ashbyhq.com/{token}"}[ats]
        elif working and chk["ats"] == "workday":
            ats, note = "workday", "Workday detected; adapter untested end to end"
        elif working:
            note = "careers page loads; no public ATS feed detected (needs JSON-LD/sitemap/selectors)"
        elif linkedin:
            note = "no official careers page supplied; use fallback_portal"
        else:
            note = f"supplied portal not working ({chk['verdict'] if chk else 'unchecked'}); see portal_url_original"
        fallback = portal if linkedin else f"https://www.linkedin.com/jobs/search/?keywords={quote(name + ' software engineer')}&location=India"
        added.append({
            "company": name, "careers_url": careers, "ats_type": ats, "board_token": token, "tier": "", "active": "false", "notes": note,
            "size_band": str(r["Size tier"]).lower(), "size_basis": f"{r['Tier basis / source']} [xlsx tiers]", "verified": verified,
            "fallback_portal": "" if (verified == "yes") else fallback, "hq_city": r["HQ city"] or "", "sector": r["Sector"] or "",
            "portal_status": "n/a (LinkedIn search link)" if linkedin else (chk["verdict"] if chk else "unchecked"),
            "portal_url_original": "" if (linkedin or working) else portal})
    client.close()
    fields = list(cat[0]) + [f for f in ["hq_city", "sector", "portal_status", "portal_url_original"] if f not in cat[0]]
    allrows = [{**{f: "" for f in fields}, **r} for r in cat + added]
    order = {"large": 0, "medium": 1, "small": 2, "micro": 3}
    allrows.sort(key=lambda r: (order[r["size_band"]], r["verified"] != "yes", r["company"].lower()))
    with CATALOG.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(allrows)
    print(f"xlsx rows {len(rows)} | already present {len(skipped)} | added {len(added)} | catalog now {len(allrows)}")
    print("newly verified feeds:", [(a["company"], a["ats_type"], a["board_token"]) for a in added if a["verified"] == "yes"])
    # checked copy of the user's workbook (original untouched)
    wb = openpyxl.load_workbook(XLSX)
    sheet = wb["Companies"]
    col = hdr.index("Link check") + 1
    for i, r in enumerate(rows, start=2):
        c = checks.get(r["Company"])
        if c:
            sheet.cell(i, col).value = VERDICT_TEXT.get(c["verdict"], c["verdict"]) + (f" [{c['ats']}:{c['token'] or ''}]" if c["ats"] else "")
    wb.save(XLSX.with_name(XLSX.stem + "_checked.xlsx"))


if __name__ == "__main__":
    main()
