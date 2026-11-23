from conftest import job

from job_search_agent import diff, store


def run_once(conn, company, jobs, ok=True):
    rid = store.start_run(conn)
    if ok:  # a failed fetch never reaches the diff
        plan = diff.compute_plan(store.company_jobs(conn, company.id), jobs)
        diff.apply_plan(conn, rid, company.id, plan)
        return plan


def status(conn, ext):
    return conn.execute("SELECT status, missed_count FROM jobs WHERE external_id=?", (ext,)).fetchone()


def test_new_then_idempotent(conn, company):
    assert len(run_once(conn, company, [job("1"), job("2")]).new) == 2
    p = run_once(conn, company, [job("1"), job("2")])
    assert not p.new and not p.changed and not p.removed


def test_changed_detected_by_title_location_description(conn, company):
    run_once(conn, company, [job("1")])
    assert run_once(conn, company, [job("1", title="Backend Engineer II")]).changed[0][2] == ["title"]
    assert run_once(conn, company, [job("1", title="Backend Engineer II", location="Pune")]).changed[0][2] == ["location"]
    p = run_once(conn, company, [job("1", title="Backend Engineer II", location="Pune", desc="new text")])
    assert p.changed[0][2] == ["description"]
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='changed'").fetchone()[0] == 3


def test_removed_only_after_two_missing_successful_runs(conn, company):
    run_once(conn, company, [job("1"), job("2")])
    run_once(conn, company, [job("1")])
    assert status(conn, "2")["status"] == "active"
    run_once(conn, company, [job("1")])
    assert status(conn, "2")["status"] == "removed"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='removed'").fetchone()[0] == 1


def test_reappearing_job_resets_miss_counter(conn, company):
    run_once(conn, company, [job("1"), job("2")])
    run_once(conn, company, [job("1")])
    run_once(conn, company, [job("1"), job("2")])
    assert status(conn, "2")["missed_count"] == 0
    run_once(conn, company, [job("1")])
    assert status(conn, "2")["status"] == "active"


def test_failed_fetch_never_removes(conn, company):
    from job_search_agent.http import PoliteClient
    from job_search_agent.pipeline import crawl
    import httpx

    run_once(conn, company, [job("1")])
    transport = httpx.MockTransport(lambda r: httpx.Response(500) if "robots" not in r.url.path else httpx.Response(404))
    client = PoliteClient(min_interval=0, max_retries=1, transport=transport, sleep=lambda s: None)
    for _ in range(3):
        summary = crawl(conn, client, store.list_companies(conn), {})
        assert summary.errors
    assert status(conn, "1")["status"] == "active" and status(conn, "1")["missed_count"] == 0


def test_empty_feed_is_treated_as_failure(conn, company):
    from job_search_agent.http import PoliteClient
    from job_search_agent.pipeline import crawl
    import httpx

    run_once(conn, company, [job("1")])
    transport = httpx.MockTransport(lambda r: httpx.Response(404) if "robots" in r.url.path else httpx.Response(200, json={"jobs": []}))
    summary = crawl(conn, PoliteClient(min_interval=0, transport=transport, sleep=lambda s: None), store.list_companies(conn), {})
    assert summary.errors and status(conn, "1")["missed_count"] == 0


def test_duplicates_collapse(conn, company):
    p = run_once(conn, company, [job("1"), job("1"), job("2")])
    assert len(p.new) == 2 and p.duplicates == 1
    from job_search_agent.models import RawJob
    a = RawJob(title="T", url="https://a.test/job/1/?utm_source=x#frag")
    b = RawJob(title="T", url="https://A.test/job/1")
    assert a.identity == b.identity
