"""Small parsing helpers shared by providers and queries."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

_DURATION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)s\s*$")


def utc_iso(dt: datetime) -> str:
    """Format as ``YYYY-MM-DDTHH:MM:SSZ``; naive datetimes are taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` allowed). Naive values are assumed UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def ts_to_utc_iso(value: Any) -> str | None:
    dt = parse_ts(value)
    return utc_iso(dt) if dt else None


def epoch_ms_to_utc_iso(value: Any) -> str | None:
    ms = to_float(value)
    if ms is None:
        return None
    return utc_iso(datetime.fromtimestamp(ms / 1000.0, tz=UTC))


def to_float(value: Any) -> float | None:
    """Coerce numbers and numeric strings (Google sends int64 as strings)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def parse_duration_s(value: Any) -> float | None:
    """Parse a protobuf JSON duration such as ``"1800s"`` or ``"12.5s"``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            return float(match.group(1))
    return None


class InputError(ValueError):
    """Invalid input from a caller (a bad date, range or value), as opposed to a fault."""


def parse_date(value: str | date, field: str = "date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise InputError(f"{field} must be YYYY-MM-DD, got {value!r}") from None


def date_from_parts(parts: Any) -> str | None:
    """``{"year": 2026, "month": 1, "day": 2}`` -> ``"2026-01-02"``."""
    if not isinstance(parts, Mapping):
        return None
    try:
        return date(int(parts["year"]), int(parts["month"]), int(parts["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def civil_date(value: Any) -> str | None:
    """Local date of a civil time given as ``"YYYY-MM-DD[THH:mm:ss]"`` or a
    ``CivilDateTime`` object (``{"date": {...}, "time": {...}}``)."""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            return None
    if isinstance(value, Mapping):
        if "date" in value:
            inner = value["date"]
            if isinstance(inner, str):
                return civil_date(inner)
            return date_from_parts(inner)
        return date_from_parts(value)
    return None


def daterange(start: date, end: date) -> Iterator[date]:
    """Inclusive range of days."""
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def cursor_date(cursor: str | None) -> date | None:
    """Day part of a cursor stored either as a date or a timestamp."""
    if not cursor:
        return None
    try:
        return date.fromisoformat(cursor[:10])
    except ValueError:
        return None


def now_utc_iso() -> str:
    return utc_iso(datetime.now(UTC))
