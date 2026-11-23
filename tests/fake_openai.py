"""A fake OpenAI Chat Completions server for tests (an httpx MockTransport). No network, no real key.

The reply depends on markers placed in the job text. Phase 1 is the profile prompt, phase 2 the resume prompt (it contains "RESUME:").
  PHASE1_AMB -> phase 1 says 'ambiguous', phase 2 answers normally      VERDICT_AMB -> 'ambiguous' in BOTH phases (phase 2 must reject it)
  VERDICT_NO -> 'no match: golang' in phase 1          P2_NO -> 'no match: golang' in phase 2
  SHOUT -> 'Match: Python.', TWOWORDS, NOWORD, OFFSPEC (a sentence), FAILME (HTTP 400). Anything else -> 'match: python'.
Each request is also recorded with its phase (1 or 2).
"""
from __future__ import annotations

import json
import threading
import time

import httpx


class FakeOpenAI:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay, self.lock, self.requests, self.events = delay, threading.Lock(), [], []
        self.models = ["gpt-6-luna", "gpt-5-mini", "gpt-4o", "text-embedding-3-small", "whisper-1"]
        self.rate_limit_once: set[str] = set()   # markers whose first request gets a 429 (retry-after: 0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            if "FAILKEY" in req.headers["authorization"]:
                return httpx.Response(401, json={"error": {"message": "bad key", "code": "invalid_api_key"}})
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        raw = json.loads(req.content)
        body = json.loads(req.content)   # `body`: message text flattened to a string (what most tests read); `raw_body`: exactly as sent
        for m in body["messages"]:
            if isinstance(m["content"], list):
                m["content"] = "".join(part["text"] for part in m["content"])
        prompt = body["messages"][1]["content"]
        with self.lock:
            self.requests.append({"auth": req.headers["authorization"], "body": body, "url": str(req.url), "phase": 2 if "RESUME:" in prompt else 1, "raw_body": raw})
            self.events.append((time.monotonic(), 1))
        try:
            if self.delay:
                time.sleep(self.delay)
            for marker in list(self.rate_limit_once):
                if marker in prompt:
                    self.rate_limit_once.discard(marker)
                    return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down", "code": "rate_limit_exceeded"}})
            if "FAILME" in prompt:
                return httpx.Response(400, json={"error": {"message": "this job broke the fake", "code": "bad"}})
            phase2 = "RESUME:" in prompt
            reply = "match: python"
            if "VERDICT_AMB" in prompt or ("PHASE1_AMB" in prompt and not phase2):
                reply = "ambiguous"
            elif ("VERDICT_NO" in prompt and not phase2) or ("P2_NO" in prompt and phase2):
                reply = "no match: golang"
            elif "SHOUT" in prompt:
                reply = "Match: Python."
            elif "TWOWORDS" in prompt:
                reply = "match: python django"
            elif "NOWORD" in prompt:
                reply = "match"
            elif "OFFSPEC" in prompt:
                reply = "I think this is a match because the stack fits."
            return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 1000, "completion_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 5}}})
        finally:
            with self.lock:
                self.events.append((time.monotonic(), -1))

    def peak(self) -> int:
        """True maximum number of requests that were in flight at the same moment."""
        now = peak = 0
        for _, d in sorted(self.events, key=lambda e: (e[0], e[1])):
            now += d
            peak = max(peak, now)
        return peak
