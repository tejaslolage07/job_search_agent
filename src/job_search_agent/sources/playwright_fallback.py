"""Headless rendering, used ONLY as a last resort for JS-only custom pages."""
from __future__ import annotations

from ..http import USER_AGENT
from .base import SourceError


def render_html(url: str, timeout_ms: int = 45_000) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise SourceError("playwright not installed: pip install playwright && playwright install chromium") from exc
    try:  # pragma: no cover - needs a browser
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover
        raise SourceError(f"playwright render failed ({exc}); try `playwright install chromium`") from exc
