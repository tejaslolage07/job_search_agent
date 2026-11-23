from __future__ import annotations

from .. import timeutil
from typing import Any, Protocol

from ..http import PoliteClient
from ..models import Company, FetchResult


class SourceError(Exception):
    pass


class Source(Protocol):
    def fetch(self, company: Company, client: PoliteClient, known: set[str], selectors: dict[str, Any]) -> FetchResult: ...


def iso_ist(value: str | int | float | None) -> str | None:
    """Parse ISO-8601 strings or epoch (s or ms) into IST text, 'YYYY-MM-DDTHH:MM:SS+05:30' (see timeutil)."""
    return timeutil.to_ist_iso(value)


def norm_workplace(value: str | None) -> str | None:
    v = (value or "").lower().replace("-", "").replace(" ", "")
    return {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "inoffice": "onsite"}.get(v)
