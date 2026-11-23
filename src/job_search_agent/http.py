"""Polite HTTP: robots.txt, honest UA, 1 req/s/host, backoff, stop on repeated 403/429."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"job_search_agent/{__version__} (personal job-discovery tool; honours robots.txt)"
ROBOTS_TOKEN = "job_search_agent"


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """Repeated 403/429 from a host: we stop instead of working around it."""


class RobotsDisallowed(FetchError):
    pass


class PoliteClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        respect_robots: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
        )
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}  # None => disallow all (5xx / unreachable)
        self._strikes: dict[str, int] = {}
        self.blocked: set[str] = set()
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    # -- robots -----------------------------------------------------------
    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = RobotFileParser()
        try:
            resp = self._send("GET", origin + "/robots.txt", check_robots=False)
        except BlockedError:
            raise
        except FetchError:
            self._robots[origin] = None
            return None
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            self._robots[origin] = rp
        elif resp.status_code >= 500:
            self._robots[origin] = None
        else:  # 4xx: no robots file (RFC 9309) => everything allowed
            rp.parse([])
            self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        return rp is not None and rp.can_fetch(ROBOTS_TOKEN, url)

    def sitemaps(self, url: str) -> list[str]:
        rp = self._robots_for(url)
        return list(rp.site_maps() or []) if rp else []

    # -- core -------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        wait = self.min_interval - (self._clock() - self._last.get(host, -1e9))
        if wait > 0:
            self._sleep(wait)
        self._last[host] = self._clock()

    def _send(self, method: str, url: str, check_robots: bool = True, **kw: Any) -> httpx.Response:
        host = urlsplit(url).netloc
        if host in self.blocked:
            raise BlockedError(f"{host} blocked us earlier in this run (403/429); not retrying")
        if check_robots and self.respect_robots and not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            self.request_count += 1
            try:
                resp = self._client.request(method, url, **kw)
            except httpx.TooManyRedirects as exc:  # a redirect loop will not fix itself: no retries
                raise FetchError(f"redirect loop at {url}") from exc
            except httpx.HTTPError as exc:  # transport errors, bad encodings, invalid URLs...
                last_exc = exc
                self._sleep(min(2**attempt, 30))
                continue
            if resp.status_code in (403, 429):
                self._strikes[host] = self._strikes.get(host, 0) + 1
                if self._strikes[host] >= 2:
                    self.blocked.add(host)
                    raise BlockedError(f"{url} returned {resp.status_code} repeatedly; stopping for {host}")
                retry_after = resp.headers.get("Retry-After", "")
                self._sleep(min(float(retry_after), 60) if retry_after.isdigit() else 5 * (attempt + 1))
                continue
            if resp.status_code >= 500:
                last_exc = FetchError(f"{url} returned {resp.status_code}")
                self._sleep(min(2**attempt, 30))
                continue
            self._strikes[host] = 0
            return resp
        raise FetchError(f"giving up on {url}: {last_exc}")

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self._send("GET", url, **kw)

    def post_json(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        return self._send("POST", url, json=payload)
