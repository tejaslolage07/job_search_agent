"""Posted filter, age and ranking all work by calendar day in IST (UTC+05:30): LinkedIn only gives a date, never a time."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from job_search_agent import report, service, store, timeutil

NOW = datetime(2026, 11, 18, 17, 45, tzinfo=UTC)  # 23:15 IST on the 18th: the evening on which 'last 24h' wrongly hid 205 jobs dated the day before
NOW_PAST_MIDNIGHT_IST = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)  # still the 18th in UTC, already 00:30 on the 19th in IST


def _day(n: int) -> str:
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.mark.parametrize("since,oldest", [
    ("today", "2026-11-18"), ("2d", "2026-11-17"), ("3d", "2026-11-16"), ("7d", "2026-11-12"), ("30d", "2026-10-20"),
    ("24h", "2026-11-17"),   # legacy saved filter: LinkedIn's '24 hours' = yesterday and today
    ("48h", "2026-11-16"), ("12h", "2026-11-17")])
def test_calendar_cutoff(since, oldest):
    assert report.calendar_cutoff(since, NOW) == oldest


@pytest.mark.parametrize("bad", ["", "abc", "0d", "-1d", "1w", "3 days"])
def test_calendar_cutoff_rejects_garbage(bad):
    with pytest.raises(ValueError):
        report.calendar_cutoff(bad, NOW)


def test_age_is_in_calendar_days_not_invented_hours():
    row = lambda posted: {"posted_at": posted, "first_seen_at": "2026-11-01T00:00:00+05:30"}  # noqa: E731
    assert report.age(row("2026-11-18T00:00:00+05:30"), NOW) == "today"
    assert report.age(row("2026-11-17T00:00:00+05:30"), NOW) == "yesterday"   # used to read '41h'
    assert report.age(row("2026-11-15T00:00:00+05:30"), NOW) == "3d"
    assert report.age(row("2026-11-18T21:40:00+05:30"), NOW) == "today"       # exact timestamps use the same rule
    assert report.age({"posted_at": None, "first_seen_at": "2026-11-17T09:00:00+05:30"}, NOW) == "yesterday"  # falls back to first seen


def _seed(conn, dates: dict[str, str]):
    conn.execute("INSERT INTO companies(name, careers_url, ats_type, active) VALUES('Acme','https://x','linkedin',0)")
    for ext, d in dates.items():
        conn.execute("INSERT INTO jobs(company_id, external_id, url, title, location, posted_at, description_text, description_hash, first_seen_at, last_seen_at, "
                     "status, source) VALUES(1,?,?,?,?,?,'d','h','2026-11-18T21:52:51+05:30','2026-11-18T21:52:51+05:30','active','linkedin')",
                     (ext, f"https://l/{ext}", f"Job {ext}", "Bengaluru", f"{d}T00:00:00+05:30"))
        jid = conn.execute("SELECT id FROM jobs WHERE external_id=?", (ext,)).fetchone()["id"]
        store.insert_match(conn, None, jid, stage_decided=1, stage1_score=50, stage1_confidence=0.9, final_score=50, bucket="review",
                           reasons_json=json.dumps({"reasons": [], "inputs": {}}), resume_variant=None, resume_hash=None, profile_hash="p",
                           matching_config_hash="c", method="stage1-rules", job_hash="j")
    conn.commit()


def _titles(conn, since: str) -> list[str]:
    return sorted(j["title"] for j in service.list_jobs(conn, "all", since=since)["jobs"])


def test_posted_filter_includes_yesterdays_date_only_jobs_in_the_evening(monkeypatch):
    conn = store.connect(":memory:")
    _seed(conn, {"today": _day(0), "yesterday": _day(1), "three": _day(3), "ten": _day(10)})
    monkeypatch.setattr(timeutil, "now_ist", lambda: NOW.astimezone(timeutil.IST))
    assert _titles(conn, "today") == ["Job today"]
    assert _titles(conn, "2d") == ["Job today", "Job yesterday"]          # the 205 jobs that used to vanish
    assert _titles(conn, "24h") == ["Job today", "Job yesterday"]         # an old saved filter behaves the same
    assert _titles(conn, "3d") == ["Job today", "Job yesterday"]          # three: today + 2 earlier days = 18, 17, 16 -> 'three' is dated 15
    assert _titles(conn, "7d") == ["Job three", "Job today", "Job yesterday"]
    assert _titles(conn, "30d") == ["Job ten", "Job three", "Job today", "Job yesterday"]
    assert _titles(conn, "") == ["Job ten", "Job three", "Job today", "Job yesterday"]


def test_ranking_is_by_day_then_score_unchanged():
    rows = [{"id": 1, "final_score": 99, "posted_at": "2026-11-17T00:00:00+05:30", "first_seen_at": "2026-11-18T00:00:00+05:30"},
            {"id": 2, "final_score": 10, "posted_at": "2026-11-18T00:00:00+05:30", "first_seen_at": "2026-11-18T00:00:00+05:30"},
            {"id": 3, "final_score": 60, "posted_at": "2026-11-18T00:00:00+05:30", "first_seen_at": "2026-11-18T00:00:00+05:30"}]
    assert [r["id"] for r in report.rank(rows)] == [3, 2, 1]


def test_the_day_changes_at_midnight_ist_not_midnight_utc(monkeypatch):
    assert report.calendar_cutoff("today", NOW) == "2026-11-18"
    assert report.calendar_cutoff("today", NOW_PAST_MIDNIGHT_IST) == "2026-11-19"          # the 19th has started in IST
    assert report.calendar_cutoff("2d", NOW_PAST_MIDNIGHT_IST) == "2026-11-18"
    row = {"posted_at": "2026-11-18T00:00:00+05:30", "first_seen_at": "2026-11-18T00:00:00+05:30"}
    assert report.age(row, NOW) == "today" and report.age(row, NOW_PAST_MIDNIGHT_IST) == "yesterday"
    conn = store.connect(":memory:")
    _seed(conn, {"nineteenth": "2026-11-19", "eighteenth": "2026-11-18"})
    monkeypatch.setattr(timeutil, "now_ist", lambda: NOW_PAST_MIDNIGHT_IST.astimezone(timeutil.IST))
    assert _titles(conn, "today") == ["Job nineteenth"]
    assert _titles(conn, "2d") == ["Job eighteenth", "Job nineteenth"]


def test_timeutil_converts_everything_to_ist_and_keeps_bare_dates_as_ist_days():
    assert timeutil.to_ist_iso("2026-11-15T15:00:00Z") == "2026-11-15T20:30:00+05:30"
    assert timeutil.to_ist_iso("2026-11-15T20:30:00+05:30") == "2026-11-15T20:30:00+05:30"
    assert timeutil.to_ist_iso("2026-11-15T10:00:00-05:00") == "2026-11-15T20:30:00+05:30"
    assert timeutil.to_ist_iso("2026-11-15T15:00:00") == "2026-11-15T20:30:00+05:30"          # no zone: sources send UTC
    assert timeutil.to_ist_iso(1793000000000) == timeutil.to_ist_iso(1793000000)               # epoch milliseconds and seconds
    assert timeutil.to_ist_iso("2026-11-19") == "2026-11-19T00:00:00+05:30"                    # LinkedIn style date: stays that day
    assert timeutil.to_ist_iso("garbage") is None and timeutil.to_ist_iso(None) is None
    assert timeutil.now_iso().endswith("+05:30") and timeutil.fmt(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == "2026-01-01T05:30:00+05:30"


def test_old_utc_rows_are_converted_once_with_a_backup_and_dates_keep_their_day(tmp_path):
    import sqlite3
    path = tmp_path / "data" / "j.db"
    conn = store.connect(path)
    conn.execute("INSERT INTO companies(name, careers_url, ats_type, active, last_checked_at) VALUES('Acme','https://x','greenhouse',0,'2026-11-20T18:44:43Z')")
    conn.execute("INSERT INTO jobs(company_id, external_id, url, title, location, posted_at, description_text, description_hash, first_seen_at, last_seen_at, status, source) "
                 "VALUES(1,'a','https://x/a','T','L','2026-11-19T00:00:00Z','d','h','2026-11-20T18:44:43Z','2026-11-20T18:50:00Z','active','linkedin')")
    conn.execute("INSERT INTO jobs(company_id, external_id, url, title, location, posted_at, description_text, description_hash, first_seen_at, last_seen_at, status, source) "
                 "VALUES(1,'b','https://x/b','T2','L','2026-11-01T10:00:00Z','d','h','2026-11-20T18:44:43+05:30','2026-11-20T18:50:00+05:30','active','ats')")
    conn.execute("INSERT INTO run_events(run_id, ts, stage, level, message) VALUES(1,'2026-11-20T18:44:43Z','run','info','x')")
    conn.commit()
    conn.close()
    conn = store.connect(path)                                                   # the next start converts what is still UTC
    jobs = {r["external_id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}
    assert jobs["a"]["posted_at"] == "2026-11-19T00:00:00+05:30"                 # a date-only value keeps its date
    assert jobs["a"]["first_seen_at"] == "2026-11-21T00:14:43+05:30" and jobs["a"]["last_seen_at"] == "2026-11-21T00:20:00+05:30"
    assert jobs["b"]["posted_at"] == "2026-11-01T15:30:00+05:30"                 # a real timestamp moves to IST
    assert jobs["b"]["first_seen_at"] == "2026-11-20T18:44:43+05:30"             # values already in IST are left alone
    assert conn.execute("SELECT last_checked_at FROM companies").fetchone()[0] == "2026-11-21T00:14:43+05:30"
    assert conn.execute("SELECT ts FROM run_events").fetchone()[0] == "2026-11-21T00:14:43+05:30"
    backup = path.with_name("j.db.before-ist.bak")
    old = sqlite3.connect(backup).execute("SELECT first_seen_at FROM jobs WHERE external_id='a'").fetchone()[0]
    assert old == "2026-11-20T18:44:43Z"                                         # the backup still has the original UTC values
    before = [tuple(r) for r in conn.execute("SELECT first_seen_at, posted_at FROM jobs ORDER BY id")]
    conn.close()
    assert store.migrate_times_to_ist(store.connect(path), path) == 0            # nothing left to convert, and running it again changes nothing
    assert [tuple(r) for r in store.connect(path).execute("SELECT first_seen_at, posted_at FROM jobs ORDER BY id")] == before
