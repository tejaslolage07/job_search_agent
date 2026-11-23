"""ASGI entry point for hosting: `uvicorn job_search_agent.web.hosted:app`. Refuses to start without a login."""
from __future__ import annotations

import os
from pathlib import Path

from ..env import load_env
from .app import create_app
from .auth import BasicAuthMiddleware


def build():  # type: ignore[no-untyped-def]
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("DASHBOARD_PASSWORD must be set (at least 12 characters): the hosted dashboard is never served without a login.")
    home = Path(os.environ.get("JOB_SEARCH_AGENT_HOME", ".")).resolve()
    load_env(home / ".env")
    return BasicAuthMiddleware(create_app(home), os.environ.get("DASHBOARD_USER", "admin"), password)


app = build() if os.environ.get("JOB_SEARCH_AGENT_BUILD") != "0" else None
