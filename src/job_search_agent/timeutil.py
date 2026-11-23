"""All times in this project are Indian Standard Time (IST, UTC+05:30, no daylight saving).

Stored timestamps look like 2026-11-21T00:09:29+05:30. Calendar days (the dashboard's Posted filter, "today" / "yesterday" ages) are IST days.
Anything that arrives in another zone (a source's "...Z" timestamp, an epoch number) is converted on the way in, so strings in the database all
share one offset and compare correctly as text. LinkedIn only gives a date: it is kept as that IST date at 00:00.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")
FMT = "%Y-%m-%dT%H:%M:%S%z"


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def fmt(dt: datetime) -> str:
    """'2026-11-21T00:09:29+05:30' (a naive datetime is taken as IST)."""
    dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
    s = dt.strftime(FMT)
    return f"{s[:-2]}:{s[-2:]}"


def now_iso() -> str:
    return fmt(now_ist())


def parse(value: str) -> datetime:
    """A stored or incoming ISO-8601 string ('...Z', '...+05:30', or with no zone = UTC, as sources send it) -> an aware IST datetime."""
    s = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).astimezone(IST)


def to_ist_iso(value: str | int | float | None) -> str | None:
    """ISO-8601 text or epoch seconds / milliseconds -> IST ISO text; None when it cannot be read."""
    if value in (None, ""):
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", value.strip()):
        return date_only_to_ist_iso(value.strip())   # a bare date is a calendar day, not an instant
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = float(value)
            return fmt(datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UTC))
        return fmt(parse(str(value)))
    except (ValueError, OverflowError, OSError):
        return None


def date_only_to_ist_iso(day: str) -> str:
    """A calendar date without a time (LinkedIn) -> that IST date at midnight, e.g. '2026-11-19' -> '2026-11-19T00:00:00+05:30'."""
    return f"{day}T00:00:00+05:30"


_LEGACY_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")


def migrate_legacy(value: str | None) -> str | None:
    """A UTC timestamp written before the switch to IST ('...Z') -> IST. A UTC midnight ('T00:00:00Z') was a date-only value, so it keeps
    its date at IST midnight instead of moving to 05:30. Anything else (already IST, plain dates, NULL) is returned unchanged."""
    if value is None or not _LEGACY_Z.match(value):
        return value
    if value[10:] == "T00:00:00Z":
        return f"{value[:10]}T00:00:00+05:30"
    return fmt(parse(value))
