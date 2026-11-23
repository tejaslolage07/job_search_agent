"""Tiny .env loader (no extra dependency). Lines are KEY=VALUE; '#' starts a comment; quotes and a leading 'export ' are allowed."""
from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value[:1] in ("'", '"') and value.endswith(value[:1]) and len(value) >= 2:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # an unquoted value may carry a trailing ' # comment'
        out[key] = value
    return out


def load_env(path: Path, override: bool = False) -> list[str]:
    """Put the values from `path` into os.environ. Variables that already exist (a real shell export) win unless override=True.
    Empty values are skipped, so a blank 'OPENAI_API_KEY=' never hides a key exported elsewhere. Returns the names that were set."""
    if not path.is_file():
        return []
    loaded = []
    for key, value in parse_env(path.read_text()).items():
        if value and (override or key not in os.environ):
            os.environ[key] = value
            loaded.append(key)
    return loaded
