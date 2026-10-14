"""Read-side queries over any ``sqlite3.Connection`` (no writes here).

Includes read-time de-duplication: the same workout recorded by several
services is returned once (source rows are never deleted).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from .db import row_counts, sync_states
from .util import parse_date, parse_ts

DEDUPE_WINDOW_S = 180
SOURCE_PRIORITY = ("garmin", "google_health", "strava", "garmin_export")
GROUP_BY = ("week", "month", "day", "type")
MAX_ACTIVITY_LIMIT = 500
MAX_SQL_LIMIT = 1000
SQL_TIMEOUT_S = 10.0

# Coarse sport families so one filter matches every provider's vocabulary
# (Strava "Ride", Garmin "road_biking", Google "OUTDOOR_BIKE" -> cycling).
SPORT_FAMILIES: dict[str, tuple[str, ...]] = {
    "cycling": ("ride", "cycl", "bik"),
    "running": ("run",),
    "swimming": ("swim",),
    "walking": ("walk",),
    "hiking": ("hik",),
    "rowing": ("row",),
    "strength": ("strength", "weight"),
    "skiing": ("ski",),
    "yoga": ("yoga",),
}
_FAMILY_ALIASES = {
    "ride": "cycling",
    "bike": "cycling",
    "biking": "cycling",
    "cycle": "cycling",
    "run": "running",
    "swim": "swimming",
    "walk": "walking",
    "hike": "hiking",
}

ACTIVITY_COLUMNS = (
    "source",
    "source_id",
    "start_time",
    "local_date",
    "activity_type",
    "name",
    "duration_s",
    "moving_s",
    "distance_m",
    "elevation_gain_m",
    "avg_hr",
    "max_hr",
    "avg_power_w",
    "calories",
)


def sport_family(activity_type: str | None) -> str:
    """Normalized sport name; unknown types are returned lower-cased."""
    if not activity_type:
        return "other"
    lowered = activity_type.lower()
    for family, needles in SPORT_FAMILIES.items():
        if any(n in lowered for n in needles):
            return family
    return lowered


def _type_clause(activity_type: str | None) -> tuple[str, list[str]]:
    if not activity_type or not activity_type.strip():
        return "", []
    wanted = activity_type.strip().lower()
    family = _FAMILY_ALIASES.get(wanted, wanted)
    if family in SPORT_FAMILIES:
        needles = SPORT_FAMILIES[family]
        return "(" + " OR ".join("LOWER(activity_type) LIKE ?" for _ in needles) + ")", [f"%{n}%" for n in needles]
    return "LOWER(activity_type) LIKE ?", [f"%{wanted}%"]


def _date_range(
    start_date: str | None, end_date: str | None, default_days: int | None, today: date | None = None
) -> tuple[str | None, str | None]:
    start = parse_date(start_date, "start_date") if start_date else None
    end = parse_date(end_date, "end_date") if end_date else None
    if default_days is not None and start is None:
        anchor = end or today or date.today()
        start = anchor - timedelta(days=default_days - 1)
    if start and end and start > end:
        raise ValueError(f"start_date {start} is after end_date {end}")
    return (start.isoformat() if start else None, end.isoformat() if end else None)


# -- de-duplication ---------------------------------------------------------------------


def _priority(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def dedupe_activities(rows: Iterable[dict[str, Any]], window_s: int = DEDUPE_WINDOW_S) -> list[dict[str, Any]]:
    """Collapse rows from *different* sources whose start times are within
    ``window_s`` seconds. Keeps the highest-priority source
    (garmin > google_health > strava > garmin_export) and lists the others in
    ``also_recorded_by``. Output is sorted by start time, oldest first."""
    timed: list[tuple[datetime, dict[str, Any]]] = []
    untimed: list[dict[str, Any]] = []
    for row in rows:
        ts = parse_ts(row.get("start_time"))
        if ts:
            timed.append((ts, row))
        else:
            untimed.append(row)
    timed.sort(key=lambda item: item[0])

    clusters: list[tuple[datetime, list[dict[str, Any]]]] = []
    open_from = 0  # clusters before this index are out of the window for good
    for ts, row in timed:
        while open_from < len(clusters) and (ts - clusters[open_from][0]).total_seconds() > window_s:
            open_from += 1
        best = None
        for index in range(open_from, len(clusters)):
            anchor, members = clusters[index]
            if any(m["source"] == row["source"] for m in members):
                continue
            gap = abs((ts - anchor).total_seconds())
            if gap <= window_s and (best is None or gap < best[0]):
                best = (gap, index)
        if best is None:
            clusters.append((ts, [row]))
        else:
            clusters[best[1]][1].append(row)

    result = []
    for _, members in clusters:
        members.sort(key=lambda m: _priority(m["source"]))
        keep = dict(members[0])
        keep["also_recorded_by"] = [m["source"] for m in members[1:]]
        result.append(keep)
    for row in untimed:
        result.append({**row, "also_recorded_by": []})
    return result


# -- activities -------------------------------------------------------------------------


def _fetch_activities(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
    activity_type: str | None,
    source: str | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if start:
        clauses.append("local_date >= ?")
        params.append(start)
    if end:
        clauses.append("local_date <= ?")
        params.append(end)
    type_sql, type_params = _type_clause(activity_type)
    if type_sql:
        clauses.append(type_sql)
        params.extend(type_params)
    if source:
        clauses.append("source = ?")
        params.append(source)
    sql = f"SELECT {', '.join(ACTIVITY_COLUMNS)} FROM activities"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY start_time DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def _present(row: dict[str, Any]) -> dict[str, Any]:
    row["sport"] = sport_family(row.get("activity_type"))
    return row


def list_activities(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest first. Cross-source duplicates are merged unless ``source`` is given."""
    limit = max(1, min(int(limit), MAX_ACTIVITY_LIMIT))
    start, end = _date_range(start_date, end_date, None)
    if source:
        rows = _fetch_activities(conn, start, end, activity_type, source, limit)
        return [_present({**r, "also_recorded_by": []}) for r in rows]
    rows = dedupe_activities(_fetch_activities(conn, start, end, activity_type, None))
    rows.sort(key=lambda r: r.get("start_time") or "", reverse=True)
    return [_present(r) for r in rows[:limit]]


def get_activity(
    conn: sqlite3.Connection, source: str, source_id: str, include_raw: bool = False
) -> dict[str, Any] | None:
    columns = ", ".join((*ACTIVITY_COLUMNS, "raw", "updated_at") if include_raw else (*ACTIVITY_COLUMNS, "updated_at"))
    row = conn.execute(
        f"SELECT {columns} FROM activities WHERE source = ? AND source_id = ?", (source, str(source_id))
    ).fetchone()
    if row is None:
        return None
    result = _present(dict(row))
    if include_raw and isinstance(result.get("raw"), str):
        try:
            result["raw"] = json.loads(result["raw"])
        except json.JSONDecodeError:
            pass
    return result


def _period_key(local_date: str, group_by: str) -> str:
    day = date.fromisoformat(local_date)
    if group_by == "week":
        return (day - timedelta(days=day.weekday())).isoformat()  # weeks start Monday
    if group_by == "month":
        return day.strftime("%Y-%m")
    return day.isoformat()


def _periods(start: date, end: date, group_by: str) -> list[str]:
    keys: list[str] = []
    day = start
    while day <= end:
        key = _period_key(day.isoformat(), group_by)
        if not keys or keys[-1] != key:
            keys.append(key)
        day += timedelta(days=1)
    return keys


def _new_bucket() -> dict[str, Any]:
    return {"count": 0, "distance_m": 0.0, "moving_s": 0.0, "elevation_gain_m": 0.0, "calories": 0.0, "hr": []}


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    hr = bucket["hr"]
    return {
        "count": bucket["count"],
        "distance_km": round(bucket["distance_m"] / 1000.0, 2),
        "moving_hours": round(bucket["moving_s"] / 3600.0, 2),
        "elevation_gain_m": round(bucket["elevation_gain_m"], 1),
        "calories": round(bucket["calories"], 1),
        "avg_of_avg_hr": round(sum(hr) / len(hr), 1) if hr else None,
    }


def activity_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    group_by: str = "week",
    activity_type: str | None = None,
) -> dict[str, Any]:
    """Totals per period (weeks start Monday) or per sport, after de-duplication
    so the same workout is never counted twice. Empty periods are included."""
    if group_by not in GROUP_BY:
        raise ValueError(f"group_by must be one of {', '.join(GROUP_BY)}; got {group_by!r}")
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required (YYYY-MM-DD)")
    start, end = _date_range(start_date, end_date, None)
    assert start and end
    rows = dedupe_activities(_fetch_activities(conn, start, end, activity_type, None))

    buckets: dict[str, dict[str, Any]] = {}
    if group_by != "type":
        for key in _periods(date.fromisoformat(start), date.fromisoformat(end), group_by):
            buckets[key] = _new_bucket()
    total = _new_bucket()
    for row in rows:
        key = sport_family(row.get("activity_type")) if group_by == "type" else _period_key(row["local_date"], group_by)
        for bucket in (buckets.setdefault(key, _new_bucket()), total):
            bucket["count"] += 1
            bucket["distance_m"] += row.get("distance_m") or 0.0
            bucket["moving_s"] += row.get("moving_s") or row.get("duration_s") or 0.0
            bucket["elevation_gain_m"] += row.get("elevation_gain_m") or 0.0
            bucket["calories"] += row.get("calories") or 0.0
            if row.get("avg_hr"):
                bucket["hr"].append(row["avg_hr"])
    label = "sport" if group_by == "type" else group_by
    groups = [{label: key, **_finish(bucket)} for key, bucket in sorted(buckets.items())]
    return {
        "start_date": start,
        "end_date": end,
        "group_by": group_by,
        "activity_type": activity_type,
        "groups": groups,
        "total": _finish(total),
    }


# -- daily metrics, sleep, body ---------------------------------------------------------


def daily_metrics(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    metrics: Sequence[str] | None = None,
    source: str | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Rows ``{date, metric, source, value}``; defaults to the last 30 days.
    Sources are not merged: the same metric can differ per source."""
    start, end = _date_range(start_date, end_date, 30, today)
    clauses, params = ["date >= ?"], [start]
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if metrics:
        clauses.append(f"metric IN ({', '.join('?' for _ in metrics)})")
        params.extend(metrics)
    if source:
        clauses.append("source = ?")
        params.append(source)
    sql = (
        "SELECT date, metric, source, value FROM daily_metrics WHERE "
        + " AND ".join(clauses)
        + " ORDER BY date, metric, source"
    )
    return [dict(r) for r in conn.execute(sql, params)]


def sleep_sessions(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    main_only: bool = True,
    source: str | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Sleep sessions by the local date they ended; defaults to the last 30 days."""
    start, end = _date_range(start_date, end_date, 30, today)
    clauses, params = ["local_date >= ?"], [start]
    if end:
        clauses.append("local_date <= ?")
        params.append(end)
    if main_only:
        clauses.append("is_main = 1")
    if source:
        clauses.append("source = ?")
        params.append(source)
    sql = (
        "SELECT source, source_id, local_date, start_time, end_time, minutes_asleep, minutes_awake, "
        "minutes_deep, minutes_light, minutes_rem, is_main FROM sleep_sessions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY local_date, start_time"
    )
    return [dict(r) for r in conn.execute(sql, params)]


def body_measurements(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    metric: str | None = None,
    source: str | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Weight/body-fat samples; defaults to the last 90 days (UTC dates)."""
    start, end = _date_range(start_date, end_date, 90, today)
    clauses, params = ["ts >= ?"], [start]
    if end:
        clauses.append("ts < ?")
        params.append((date.fromisoformat(end) + timedelta(days=1)).isoformat())
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if source:
        clauses.append("source = ?")
        params.append(source)
    sql = "SELECT ts, metric, source, value FROM body_measurements WHERE " + " AND ".join(clauses) + " ORDER BY ts"
    return [dict(r) for r in conn.execute(sql, params)]


def sync_status(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"row_counts": row_counts(conn), "streams": sync_states(conn)}


# -- ad-hoc SQL -------------------------------------------------------------------------

_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def _authorizer(action: int, *args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def validate_select(sql: str) -> str:
    """Return the statement without trailing semicolons, or raise ValueError
    unless it is a single ``SELECT``/``WITH`` statement."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a non-empty string")
    body = _LEADING_COMMENTS.sub("", sql, count=1).strip().rstrip(";").rstrip()
    first = re.match(r"[A-Za-z]+", body)
    if not first or first.group(0).lower() not in ("select", "with"):
        raise ValueError("only a single SELECT or WITH query is allowed")
    if ";" in body:
        raise ValueError("only a single statement is allowed (remove ';')")
    return body


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value


def run_select(conn: sqlite3.Connection, sql: str, limit: int = 200) -> dict[str, Any]:
    """Run one read-only query: ``{columns, rows, truncated}``.

    Defense in depth: statement validation, a SQLite authorizer that only allows
    reads, a time limit, and (in the MCP server) a ``mode=ro`` +
    ``query_only`` connection.
    """
    body = validate_select(sql)
    limit = max(1, min(int(limit), MAX_SQL_LIMIT))
    deadline = time.monotonic() + SQL_TIMEOUT_S
    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = conn.execute(body)
        columns = [c[0] for c in cursor.description or ()]
        fetched = cursor.fetchmany(limit + 1)
    except sqlite3.DatabaseError as exc:
        if "interrupted" in str(exc):
            raise ValueError(f"query exceeded the {SQL_TIMEOUT_S:.0f}s time limit") from None
        if "not authorized" in str(exc) or "prohibited" in str(exc):
            raise ValueError(f"query not allowed: {exc}") from None
        raise ValueError(f"SQL error: {exc}") from None
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
    rows = [[_jsonable(v) for v in tuple(r)] for r in fetched[:limit]]
    return {"columns": columns, "rows": rows, "truncated": len(fetched) > limit}
