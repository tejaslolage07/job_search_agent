from __future__ import annotations

import pytest
from conftest import PROFILE
from fastapi.testclient import TestClient

from job_search_agent.web.app import create_app
from job_search_agent.web.gateway import Gateway, check_password, hash_password, sign, unsign

BODY = {k: v for k, v in PROFILE.items() if k != "resume_paths"}
CODE = "family-code-123"


@pytest.fixture
def gw(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaZZZZ")
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    return Gateway(tmp_path / "vol", CODE, "s" * 40, defaults, user_app=lambda home: create_app(home.root, start_scheduler=False), secure_cookie=False)


def client(gw):  # type: ignore[no-untyped-def]
    return TestClient(gw, follow_redirects=False)


def signup(c, email="a@example.com", pw="longpassword", code=CODE):  # type: ignore[no-untyped-def]
    return c.post("/signup", data={"email": email, "password": pw, "master_code": code})


def test_logged_out_sees_only_landing(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        r = c.get("/")
        assert r.status_code == 200 and "Create an account" in r.text and "Never applies" in r.text
        assert c.get("/api/state").status_code == 401
        assert c.get("/anything").status_code == 303
        assert c.get("/healthz").text == "ok"


def test_signup_needs_master_code(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        r = signup(c, code="wrong")
        assert r.headers["location"] == "/?e=code" and "set-cookie" not in r.headers
        assert "invitation code is not valid" in c.get("/?e=code").text
        assert c.get("/api/state").status_code == 401


def test_signup_then_use_then_logout(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        assert signup(c).headers["location"] == "/"
        assert c.get("/api/state").status_code == 200
        assert "Job Search Agent" in c.get("/").text and "Create an account" not in c.get("/").text
        c.get("/logout")
        c.cookies.clear()
        assert c.get("/api/state").status_code == 401


def test_login_and_wrong_password(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        signup(c)
        c.cookies.clear()
        assert c.post("/login", data={"email": "a@example.com", "password": "nope-nope-nope"}).headers["location"] == "/?e=login"
        assert c.post("/login", data={"email": "A@Example.com", "password": "longpassword"}).headers["location"] == "/"
        assert c.get("/api/state").status_code == 200


def test_duplicate_and_validation(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        signup(c)
        assert signup(c).headers["location"] == "/?e=exists"
        assert signup(c, email="bad").headers["location"] == "/?e=email"
        assert signup(c, email="b@example.com", pw="short").headers["location"] == "/?e=pw"


def test_users_are_isolated(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as a, client(gw) as b:
        signup(a, "a@example.com")
        signup(b, "b@example.com")
        assert a.put("/api/profile", json=BODY).status_code == 200
        assert a.get("/api/profile").json()["profile"]["target_titles"] == BODY["target_titles"]
        assert b.get("/api/profile").json()["profile"] is None


def test_shared_key_is_hidden_and_locked(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        signup(c)
        data = c.get("/api/llm").json()
        assert data["openai_key_set"] is True and data["key_hint"] is None and data["env_file"] is None
        assert "ZZZZ" not in c.get("/api/llm").text
        assert c.put("/api/llm/key", json={"key": "sk-" + "x" * 30}).status_code == 403


def test_throttle_blocks_code_guessing(gw) -> None:  # type: ignore[no-untyped-def]
    with client(gw) as c:
        for _ in range(10):
            signup(c, code="guess")
        assert signup(c, code=CODE).headers["location"] == "/?e=slow"


def test_tampered_or_expired_cookie_rejected(gw) -> None:  # type: ignore[no-untyped-def]
    secret = b"s" * 40
    assert unsign(secret, sign(secret, 7)) == 7
    assert unsign(secret, sign(secret, 7) + "x") is None
    assert unsign(b"other" * 10, sign(secret, 7)) is None
    assert unsign(secret, sign(secret, 7, now=0), now=10**10) is None
    with client(gw) as c:
        c.cookies.set("jsa_session", sign(secret, 1))  # valid signature, but no such user
        assert c.get("/api/state").status_code == 401


def test_password_hash_roundtrip() -> None:
    h = hash_password("hunter2hunter2")
    assert check_password("hunter2hunter2", h) and not check_password("x", h) and "hunter2" not in h
