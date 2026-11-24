"""Find careers pages for catalog rows that have none. Polite (PoliteClient: robots.txt, honest UA, 1 req/s per host).
Guesses the company's domain from its name, requires the homepage to mention the company, then follows a careers/jobs link
(or tries common paths). Results are resumable in data/careers_find.jsonl; nothing is written to the catalog until `apply`.
Usage: python scripts/find_careers.py probe [N]   # N = limit, for a trial
       python scripts/find_careers.py apply       # writes config/india_companies.csv (backup first)
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from job_search_agent.http import FetchError, PoliteClient
from job_search_agent.sources.detect import detect_from_text

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "config" / "india_companies.csv"
OUT = ROOT / "data" / "careers_find.jsonl"
TLDS = (".com", ".in", ".co.in", ".io", ".ai", ".co", ".org", ".net")
PATHS = ("/careers", "/career", "/jobs", "/careers/", "/about/careers", "/company/careers", "/join-us")
LINK = re.compile(r"career|jobs?\b|join us|work with us|openings|opportunit|we.re hiring|recruit", re.I)
STOP = re.compile(r"\b(technologies|technology|systems|software|solutions|services|labs|india|pvt|ltd|limited|private|corporation|corp|"
                  r"group|payments|networks|infotech|analytics|ai|inc|the|and|of|bank|finance|financial)\b", re.I)


def names(company: str) -> tuple[list[str], str]:
    """(domain slugs, token the homepage must contain)."""
    parts = [re.sub(r"\(.*?\)", "", company).strip()] + re.findall(r"\((.*?)\)", company)
    slugs: list[str] = []
    for p in parts:
        base = re.sub(r"[^a-z0-9 ]", "", p.lower().replace("&", "and")).strip()
        core = re.sub(r"\s+", " ", STOP.sub(" ", base)).strip()
        for s in (base.replace(" ", ""), core.replace(" ", ""), core.replace(" ", "-")):
            if len(s) >= 3 and s not in slugs and (s == base.replace(" ", "") or len(s) >= 7):
                slugs.append(s)
    first = parts[0].lower()
    token = re.sub(r"[^a-z0-9 ]", "", STOP.sub(" ", first)).strip() or re.sub(r"[^a-z0-9 ]", "", first)
    return slugs[:4], token


def text_has(token: str, html: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", html.lower()[:60000])
    return re.sub(r"[^a-z0-9]", "", token.lower()) in flat


def resolves(host: str) -> bool:
    try:
        socket.setdefaulttimeout(3)
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def find(client: PoliteClient, company: str, orig: str) -> dict:
    slugs, token = names(company)
    res: dict = {"company": company, "result": "not_found"}
    for slug in slugs:
        for tld in TLDS:
            host = slug + tld
            pre = "www." if resolves("www." + host) else "" if resolves(host) else None
            if pre is None:
                continue
            try:
                r = client.get(f"https://{pre}{host}/")
            except FetchError:
                continue
            if r.status_code != 200 or not text_has(token, r.text):
                continue
            home = str(r.url)
            cands: list[str] = []
            try:
                for a in HTMLParser(r.text).css("a[href]"):
                    href = (a.attributes.get("href") or "").strip()
                    if href and not href.startswith(("mailto:", "tel:", "javascript:")) and (LINK.search(a.text() or "") or LINK.search(href)):
                        cands.append(urljoin(home, href).split("#")[0])
            except Exception:
                pass
            hit = detect_from_text(r.text)
            if hit:
                res.update(result="ats_link", home=home, ats=hit[0], token=hit[1], url=home)
                # prefer the explicit board link found in the page
                for c in cands:
                    if detect_from_text(c):
                        res["url"] = c
                        break
                return res
            seen: list[str] = []
            for c in cands[:6] + [urljoin(home, p) for p in PATHS]:
                if c in seen:
                    continue
                seen.append(c)
                if len(seen) > 8:
                    break
                try:
                    cr = client.get(c)
                except FetchError:
                    continue
                if cr.status_code == 200:
                    h2 = detect_from_text(str(cr.url)) or detect_from_text(cr.text)
                    res.update(result="ok", home=home, url=str(cr.url), ats=h2[0] if h2 else "custom", token=h2[1] if h2 else None)
                    return res
            res.update(result="home_only", home=home, url=home)
            return res
    return res


def probe(limit: int | None) -> None:
    rows = list(csv.DictReader(CSV.open(newline="")))
    todo = [r["company"] for r in rows if not r["careers_url"]]
    done = {json.loads(l)["company"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
    todo = [c for c in todo if c not in done][:limit]
    print(f"{len(todo)} companies to probe", flush=True)

    def work(c: str) -> None:
        client = PoliteClient(min_interval=1.0, max_retries=0, timeout=8)
        try:
            res = find(client, c, c)
        except Exception as exc:  # one company never stops the rest
            res = {"company": c, "result": "error", "error": str(exc)[:120]}
        finally:
            client.close()
        with open(OUT, "a") as fh:
            fh.write(json.dumps(res) + "\n")
        print(c, "->", res.get("result"), res.get("url", ""), flush=True)

    with ThreadPoolExecutor(32) as pool:
        list(pool.map(work, todo))


def apply() -> None:
    shutil.copy(CSV, ROOT / "data" / "india_companies.before-find.csv")
    res = {}
    for l in OUT.read_text().splitlines():
        d = json.loads(l)
        res[d["company"]] = d
    rows = list(csv.DictReader(CSV.open(newline="")))
    fields = list(rows[0].keys())
    n = 0
    for r in rows:
        d = res.get(r["company"])
        if r["careers_url"] or not d or d["result"] not in ("ok", "ats_link"):
            continue
        if not re.search(r"career|jobs?|recruit|opportunit|join|vacanc|opening|hiring|work-?with|workday|lever\.co|greenhouse|ashby", d["url"], re.I):
            continue
        r["careers_url"] = d["url"]
        if d.get("ats") and d["ats"] != "custom":
            r["ats_type"], r["board_token"] = d["ats"], d.get("token") or ""
        r["portal_status"] = {"ok": "ok (auto)", "ats_link": "ok (auto)", "home_only": "home page only (auto)"}[d["result"]]
        r["notes"] = (r["notes"] + " | " if r["notes"] else "") + f"careers URL auto-found from {urlsplit(d['home']).netloc}; verify"
        n += 1
    with CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"updated {n} rows")


if __name__ == "__main__":
    if sys.argv[1] == "probe":
        probe(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        apply()
