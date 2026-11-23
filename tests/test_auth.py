from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_search_agent.web.auth import BasicAuthMiddleware


def _client() -> TestClient:
    inner = FastAPI()

    @inner.get("/secret")
    def secret() -> dict[str, str]:
        return {"x": "y"}

    return TestClient(BasicAuthMiddleware(inner, "me", "correct-horse-battery"))


def _h(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_requires_login() -> None:
    r = _client().get("/secret")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]


@pytest.mark.parametrize("user,pw", [("me", "wrong"), ("you", "correct-horse-battery"), ("", "")])
def test_wrong_credentials(user: str, pw: str) -> None:
    assert _client().get("/secret", headers=_h(user, pw)).status_code == 401


def test_garbage_header() -> None:
    assert _client().get("/secret", headers={"Authorization": "Basic !!!"}).status_code == 401


def test_right_credentials_and_health() -> None:
    c = _client()
    assert c.get("/secret", headers=_h("me", "correct-horse-battery")).json() == {"x": "y"}
    assert c.get("/healthz").text == "ok"


def test_hosted_refuses_without_password(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JOB_SEARCH_AGENT_BUILD", "0")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    from job_search_agent.web import hosted

    with pytest.raises(SystemExit):
        hosted.build()
