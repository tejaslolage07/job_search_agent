"""Minimal OpenAI Chat Completions client over httpx (no SDK dependency).

Handles what a matcher needs: auth, optional reasoning effort, token usage, and failures that are explained in plain words.
Rate limits (429), server errors (5xx) and timeouts are retried with backoff (honouring Retry-After); a bad key, an unknown model, a bad
request or an exhausted quota fail at once because retrying cannot help. The API key is never written to an error message or repr.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.openai.com/v1"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class OpenAIError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable
        self.request: dict[str, Any] = {}      # the body that was sent (set by OpenAIChat.complete); never contains the API key
        self.response: Any = None              # the last response body received (parsed JSON, or text), None if there was none


@dataclass
class Reply:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0     # part of tokens_out spent thinking (reasoning models)
    cached_tokens: int = 0        # part of tokens_in served from the prompt cache (billed at the cheap cached rate)
    cache_write_tokens: int = 0   # part of tokens_in written to the prompt cache (some models bill this above the plain input rate)
    model: str = ""
    finish_reason: str = ""
    retries: int = 0              # how many attempts were repeated because of rate limits / server errors / timeouts
    request: dict[str, Any] = field(default_factory=dict)    # the exact body sent
    response: dict[str, Any] = field(default_factory=dict)   # the full response body received


def _error_body(resp: httpx.Response) -> tuple[str, str | None]:
    try:
        err = resp.json().get("error") or {}
        return str(err.get("message") or resp.text[:200]), err.get("code")
    except ValueError:
        return resp.text[:200], None


def error_for(resp: httpx.Response, model: str) -> OpenAIError:
    message, code = _error_body(resp)
    s = resp.status_code
    if s == 401:
        return OpenAIError("OpenAI rejected the API key (401). Check OPENAI_API_KEY in your .env file.", s, code)
    if s == 403:
        return OpenAIError(f"OpenAI refused the request (403): {message}", s, code)
    if s == 404:
        return OpenAIError(f"model '{model}' was not found or this key cannot use it (404). Run the kit with --list-models to see what is available.", s, code)
    if s == 429 and code == "insufficient_quota":
        return OpenAIError("OpenAI says this account is out of quota or credit (429 insufficient_quota). Check billing; retrying will not help.", s, code)
    if s in RETRY_STATUSES:
        return OpenAIError(f"OpenAI {'rate limit' if s == 429 else 'server error'} ({s}): {message}", s, code, retryable=True)
    return OpenAIError(f"OpenAI rejected the request ({s}): {message}", s, code)


class OpenAIChat:
    def __init__(self, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 120.0, max_retries: int = 3,
                 reasoning_effort: str | None = None, max_output_tokens: int | None = None, cache: str = "", cache_ttl: str = "30m",
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise OpenAIError("no OpenAI API key: set OPENAI_API_KEY in your .env file")
        self.model, self.base_url, self.max_retries = model, base_url.rstrip("/"), max_retries
        self.reasoning_effort, self.max_output_tokens, self._sleep = reasoning_effort, max_output_tokens, sleep
        self.cache, self.cache_ttl = cache, cache_ttl
        self._client = httpx.Client(timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {api_key}"})

    def __repr__(self) -> str:  # never show the key
        return f"OpenAIChat(model={self.model!r}, base_url={self.base_url!r})"

    def close(self) -> None:
        self._client.close()

    def payload(self, system: str, user: str | list[str], reasoning_effort: str | None = None) -> dict[str, Any]:
        """`user` is the text, or [shared start, per-job text]. In explicit cache mode only the shared start is marked for caching: without a mark OpenAI
        writes the WHOLE prompt to its cache (billed above the plain input rate) and can only read it back on an exact repeat, which never happens per job."""
        if isinstance(user, list) and self.cache == "explicit" and len(user) == 2:
            content: Any = [{"type": "text", "text": user[0], "prompt_cache_breakpoint": {"mode": "explicit"}}, {"type": "text", "text": user[1]}]
        else:
            content = "".join(user) if isinstance(user, list) else user
        body: dict[str, Any] = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        if self.cache == "explicit":
            body["prompt_cache_options"] = {"mode": "explicit", "ttl": self.cache_ttl}
        effort = self.reasoning_effort if reasoning_effort is None else reasoning_effort   # per-call value ("" = send nothing) beats the client's default
        if effort:
            body["reasoning_effort"] = effort      # only models that reason accept this
        if self.max_output_tokens:
            body["max_completion_tokens"] = self.max_output_tokens  # reasoning models count their thinking in this limit: keep it generous
        return body

    def _wait(self, attempt: int, resp: httpx.Response | None) -> float:
        retry_after = resp.headers.get("retry-after", "") if resp is not None else ""
        if retry_after.replace(".", "", 1).isdigit():
            return min(float(retry_after), 30.0)
        return min(2.0 ** attempt, 20.0) + random.uniform(0, 0.5)

    def complete(self, system: str, user: str | list[str], reasoning_effort: str | None = None) -> Reply:
        last: OpenAIError | None = None
        payload = self.payload(system, user, reasoning_effort)
        for attempt in range(self.max_retries + 1):
            resp: httpx.Response | None = None
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code != 200:
                    raise error_for(resp, self.model)
                data = resp.json()
                try:
                    reply = self._parse(data)
                except OpenAIError as exc:   # a 200 whose content is unusable (refusal, cut off, empty): keep what came back
                    exc.response = data
                    raise
                reply.retries, reply.request, reply.response = attempt, payload, data
                return reply
            except OpenAIError as exc:
                exc.request = payload
                if exc.response is None and resp is not None:
                    try:
                        exc.response = resp.json()
                    except ValueError:
                        exc.response = resp.text[:5000]
                if not exc.retryable:
                    raise
                last = exc
            except httpx.TimeoutException:
                last = OpenAIError("the request to OpenAI timed out", retryable=True)
            except httpx.TransportError as exc:
                last = OpenAIError(f"could not reach OpenAI ({type(exc).__name__})", retryable=True)
            if attempt < self.max_retries:
                self._sleep(self._wait(attempt, resp))
        final = OpenAIError(f"{last} (gave up after {self.max_retries + 1} attempts)", getattr(last, "status", None), getattr(last, "code", None))
        final.request, final.response = payload, getattr(last, "response", None)
        raise final

    def _parse(self, data: dict[str, Any]) -> Reply:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text, finish = message.get("content") or "", choice.get("finish_reason") or ""
        if message.get("refusal"):
            raise OpenAIError(f"the model refused: {str(message['refusal'])[:150]}")
        if finish == "length" and not text.strip():
            raise OpenAIError("the reply was cut off before any text appeared (the token limit was used up, often by reasoning): raise max_output_tokens or lower reasoning_effort")
        if not text.strip():
            raise OpenAIError("OpenAI returned an empty reply")
        u = data.get("usage") or {}
        return Reply(text=text, tokens_in=int(u.get("prompt_tokens") or 0), tokens_out=int(u.get("completion_tokens") or 0),
                     reasoning_tokens=int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
                     cached_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                     cache_write_tokens=int((u.get("prompt_tokens_details") or {}).get("cache_write_tokens") or 0), model=str(data.get("model") or self.model), finish_reason=finish)

    def list_models(self) -> list[str]:
        resp = self._client.get(f"{self.base_url}/models")
        if resp.status_code != 200:
            raise error_for(resp, self.model)
        return sorted(m["id"] for m in resp.json().get("data", []))
