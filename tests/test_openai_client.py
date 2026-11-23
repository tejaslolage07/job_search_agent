from __future__ import annotations

import json
import os

import httpx
import pytest

from job_search_agent.env import load_env, parse_env
from job_search_agent.matching.openai_client import OpenAIChat, OpenAIError

KEY = "sk-test-SECRET1234567890"


def chat_ok(text="match: python", **usage):
    return httpx.Response(200, json={"model": "gpt-test-2026", "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 120, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 100},
                                               "completion_tokens_details": {"reasoning_tokens": 4}, **usage}})


def client(handler, **kw) -> OpenAIChat:
    sleeps: list[float] = []
    c = OpenAIChat(KEY, "gpt-test", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    c.slept = sleeps  # type: ignore[attr-defined]
    return c


# ---------------------------------------------------------------- .env ----------------------------------------------------------
def test_parse_env_handles_comments_quotes_export_and_equals_in_values():
    text = '# comment\n\nOPENAI_API_KEY=sk-abc=def\nexport A="quoted # not a comment"\nB=\'single\'\nC=plain # trailing comment\nEMPTY=\nnot a line\n  D = spaced  \n'
    assert parse_env(text) == {"OPENAI_API_KEY": "sk-abc=def", "A": "quoted # not a comment", "B": "single", "C": "plain", "EMPTY": "", "D": "spaced"}


def test_load_env_never_overrides_the_shell_and_skips_blank_values(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("OPENAI_API_KEY=from-file\nOTHER=x\nBLANK=\n")
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.delenv("BLANK", raising=False)
    assert load_env(f) == ["OTHER"] and os.environ["OPENAI_API_KEY"] == "from-shell" and "BLANK" not in os.environ
    assert load_env(f, override=True) == ["OPENAI_API_KEY", "OTHER"] and os.environ["OPENAI_API_KEY"] == "from-file"
    assert load_env(tmp_path / "missing.env") == []
    monkeypatch.delenv("OTHER", raising=False)


def test_a_blank_key_in_the_env_file_does_not_hide_a_key_exported_elsewhere(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=\n")
    monkeypatch.setenv("OPENAI_API_KEY", "exported")
    load_env(tmp_path / ".env", override=True)
    assert os.environ["OPENAI_API_KEY"] == "exported"


# ---------------------------------------------------------------- requests and replies ----------------------------------------------------------
def test_request_shape_and_reply_parsing():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return chat_ok()
    c = client(handler)
    r = c.complete("SYS", "USER")
    assert seen["url"] == "https://api.openai.com/v1/chat/completions" and seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {"model": "gpt-test", "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]}  # no temperature etc.
    assert (r.text, r.tokens_in, r.tokens_out, r.reasoning_tokens, r.cached_tokens, r.model, r.retries) == ("match: python", 120, 9, 4, 100, "gpt-test-2026", 0)


def test_optional_parameters_are_sent_only_when_set():
    bodies = []
    c = client(lambda req: (bodies.append(json.loads(req.content)), chat_ok())[1], reasoning_effort="low", max_output_tokens=500)
    c.complete("s", "u")
    assert bodies[0]["reasoning_effort"] == "low" and bodies[0]["max_completion_tokens"] == 500


def test_the_key_never_appears_in_repr_or_errors():
    c = client(lambda req: httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}", "code": "invalid_api_key"}}))
    assert KEY not in repr(c) and "gpt-test" in repr(c)
    with pytest.raises(OpenAIError) as e:
        c.complete("s", "u")
    assert "OPENAI_API_KEY" in str(e.value) and KEY not in str(e.value) and e.value.status == 401


def test_a_missing_key_is_refused_up_front():
    with pytest.raises(OpenAIError, match="OPENAI_API_KEY"):
        OpenAIChat("", "m")


# ---------------------------------------------------------------- failures that must NOT be retried ----------------------------------------------------------
@pytest.mark.parametrize("status,body,expect", [
    (404, {"error": {"message": "The model `nope` does not exist", "code": "model_not_found"}}, "was not found or this key cannot use it"),
    (400, {"error": {"message": "Unsupported parameter: 'reasoning_effort'", "code": "unsupported_parameter"}}, "Unsupported parameter"),
    (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, "out of quota or credit"),
    (403, {"error": {"message": "Country not supported"}}, "OpenAI refused the request")])
def test_permanent_errors_fail_immediately_with_a_clear_message(status, body, expect):
    calls = []
    c = client(lambda req: (calls.append(1), httpx.Response(status, json=body))[1])
    with pytest.raises(OpenAIError, match=expect):
        c.complete("s", "u")
    assert len(calls) == 1 and c.slept == []  # one attempt, no waiting


# ---------------------------------------------------------------- failures that ARE retried ----------------------------------------------------------
def test_rate_limit_is_retried_honouring_retry_after_and_counted():
    answers = iter([httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}), chat_ok()])
    c = client(lambda req: next(answers))
    r = c.complete("s", "u")
    assert r.text == "match: python" and r.retries == 1 and c.slept == [7.0]


def test_server_errors_and_timeouts_are_retried_with_growing_waits():
    answers = iter([httpx.Response(503, json={"error": {"message": "overloaded"}}), httpx.ReadTimeout("slow"), chat_ok()])

    def handler(req):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    c = client(handler)
    r = c.complete("s", "u")
    assert r.retries == 2 and len(c.slept) == 2 and 1.0 <= c.slept[0] <= 1.6 and 2.0 <= c.slept[1] <= 2.6


def test_it_gives_up_after_max_retries_and_says_so():
    calls = []
    c = client(lambda req: (calls.append(1), httpx.Response(500, json={"error": {"message": "boom"}}))[1], max_retries=2)
    with pytest.raises(OpenAIError, match=r"server error \(500\).*gave up after 3 attempts"):
        c.complete("s", "u")
    assert len(calls) == 3 and len(c.slept) == 2


def test_network_failure_is_retried_then_reported():
    def handler(req):
        raise httpx.ConnectError("no route")
    c = client(handler, max_retries=1)
    with pytest.raises(OpenAIError, match="could not reach OpenAI"):
        c.complete("s", "u")


# ---------------------------------------------------------------- odd replies ----------------------------------------------------------
@pytest.mark.parametrize("message,finish,expect", [
    ({"content": ""}, "length", "token limit was used up"),
    ({"content": None}, "stop", "empty reply"),
    ({"content": None, "refusal": "I can't help with that"}, "stop", "the model refused")])
def test_unusable_replies_are_explained(message, finish, expect):
    c = client(lambda req: httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}], "usage": {}}))
    with pytest.raises(OpenAIError, match=expect):
        c.complete("s", "u")


def test_list_models_sorted_and_errors_mapped():
    c = client(lambda req: httpx.Response(200, json={"data": [{"id": "b-model"}, {"id": "a-model"}]}))
    assert c.list_models() == ["a-model", "b-model"]
    bad = client(lambda req: httpx.Response(401, json={"error": {"message": "bad"}}))
    with pytest.raises(OpenAIError, match="rejected the API key"):
        bad.list_models()
