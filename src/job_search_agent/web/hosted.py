"""ASGI entry point for hosting: `uvicorn job_search_agent.web.hosted:app`. Refuses to start without the invitation code and a session secret."""
from __future__ import annotations

import os
from pathlib import Path

from .app import create_app
from .gateway import Gateway


def build() -> Gateway:
    master, secret = os.environ.get("MASTER_CODE", ""), os.environ.get("SESSION_SECRET", "")
    if len(master) < 8 or len(secret) < 32:
        raise SystemExit("MASTER_CODE (8+ characters, the code you share) and SESSION_SECRET (32+ random characters) must be set: "
                         "the hosted dashboard is never served without them. Also set OPENAI_API_KEY.")
    root = Path(os.environ.get("JOB_SEARCH_AGENT_HOME", ".")).resolve()
    defaults = Path(os.environ.get("CONFIG_DEFAULTS", "/app/config-defaults"))
    return Gateway(root, master, secret, defaults, user_app=lambda home: create_app(home.root))


app = build() if os.environ.get("JOB_SEARCH_AGENT_BUILD") != "0" else None
