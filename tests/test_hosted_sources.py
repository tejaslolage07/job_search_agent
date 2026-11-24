import html
import json

import httpx

from job_search_agent.http import PoliteClient, _rfc_allows
from job_search_agent.models import Company
from job_search_agent.sources import REGISTRY
from job_search_agent.sources.detect import detect_from_text

GUID = "617745cb-e730-42a7-aac7-0cac7e3a365b"


def client(handler):
    def wrapped(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(req)
    return PoliteClient(min_interval=0, transport=httpx.MockTransport(wrapped), sleep=lambda s: None)


def test_detect_new_platforms():
    assert detect_from_text("https://acme.keka.com/careers") == ("keka", "acme")
    assert detect_from_text("https://acme.zohorecruit.in/jobs/Careers") == ("zoho", "acme.zohorecruit.in")
    assert detect_from_text("https://eofh.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs") == ("oracle", "eofh.fa.em2.oraclecloud.com|CX_1")


def test_keka():
    def h(r):
        if r.url.path == "/careers":
            return httpx.Response(200, text=f"<script>fetch('/ats/documents/{GUID}/careerportal/x.html')</script>")
        assert r.url.path == f"/careers/api/embedjobs/default/active/{GUID}"
        return httpx.Response(200, json=[{"id": 7, "title": "SDE", "description": "<p>Build</p>", "publishedOn": "2026-11-01T10:00:00Z",
                                          "jobLocations": [{"name": "Pune"}]}])
    j = REGISTRY["keka"].fetch(Company(1, "Acme", "", "keka", "acme"), client(h), set(), {}).jobs[0]
    assert j.external_id == "7" and j.location == "Pune" and j.url.endswith("/careers/jobdetails/7") and j.description_text == "Build"


def test_zoho_recruit_any_key_order():
    jobs = [{"Client_Name": {"name": "x"}, "Posting_Title": "Eng", "City": "Noida", "State": "UP", "Country": "India", "id": "9",
             "Publish": True, "Date_Opened": "2026-11-03", "Job_Description": "Do things"}]
    page = f'<input type="hidden" value="{html.escape(json.dumps(jobs))}" id="jobs">'
    j = REGISTRY["zoho"].fetch(Company(1, "Acme", "", "zoho", "acme.zohorecruit.in"), client(lambda r: httpx.Response(200, text=page)), set(), {}).jobs[0]
    assert j.title == "Eng" and j.location == "Noida, UP, India" and j.url == "https://acme.zohorecruit.in/jobs/Careers/9"


def test_oracle_pages_and_url():
    def h(r):
        if "recruitingCEJobRequisitionDetails" in str(r.url):
            return httpx.Response(200, json={"items": [{"ExternalDescriptionStr": "<p>Desc</p>"}]})
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 1, "requisitionList": [
            {"Id": "5", "Title": "Officer", "PostedDate": "2026-11-23", "PrimaryLocation": "Jalna, India", "secondaryLocations": []}]}]})
    j = REGISTRY["oracle"].fetch(Company(1, "Acme", "", "oracle", "h.fa.em2.oraclecloud.com|CX_1"), client(h), set(), {}).jobs[0]
    assert j.url == "https://h.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/5" and j.description_text == "Desc"


def test_robots_longest_match_beats_first_match():
    lines = "User-agent: *\nDisallow: /\nAllow: /careers".splitlines()
    assert _rfc_allows(lines, "https://a.test/careers/api/x")
    assert not _rfc_allows(lines, "https://a.test/ats/documents/x")
