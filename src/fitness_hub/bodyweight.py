"""Daily body weight: a smoothed trend from the weigh-ins, filled between them, each day labelled
with where its value comes from. Nothing here is stored: the weigh-ins in ``body_measurements``
stay exactly as measured, and the trend is derived from them on every read.

- A day's **weigh-in** is the mean of that day's measurements (every source; a scale synced to
  two services counts once per value, and several weigh-ins a day average).
- The **trend** smooths the day-to-day swings (water, food): on each weigh-in day it moves
  ``TREND_ALPHA`` of the way to the weigh-in, more after a gap (``1 - (1 - TREND_ALPHA) ** days``),
  so it catches up after weeks without weighing (the "trend weight" of The Hacker's Diet).
- Between two weigh-ins the trend is a straight line, labelled **interpolated** when they are at
  most ``INTERPOLATE_MAX_GAP_DAYS`` apart (weight changes slowly enough that the line is close)
  and **estimated** beyond (a guess: the change may have come at any time in the gap).
- After the latest weigh-in it is **carried** forward, flat, for ``CARRY_DAYS``; then there is no
  value. Before the first weigh-in there is none either.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

TREND_ALPHA = 0.1
INTERPOLATE_MAX_GAP_DAYS = 42
CARRY_DAYS = 30
KINDS = ("weigh_in", "interpolated", "estimated", "carried")


def daily_weigh_ins(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """``[(date, kg)]``, one per day with a weigh-in (the mean of its values), oldest first."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, avg(value) FROM body_measurements "
            "WHERE metric = 'weight_kg' AND value > 0 GROUP BY day ORDER BY day"
        )
    ]


def _day(iso: str, offset: int = 0) -> str:
    return (date.fromisoformat(iso) + timedelta(days=offset)).isoformat()


def trend(weigh_ins: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Every day from the first weigh-in to ``CARRY_DAYS`` after the last:
    ``{date, kg (the trend), kind, weigh_in (kg, on a weigh-in day, else None), gap_days, last_weigh_in}``.
    ``gap_days`` is the length of the gap a filled day sits in (days since the last weigh-in, when
    carried); ``last_weigh_in`` the date of the latest weigh-in up to the day."""
    out: list[dict[str, Any]] = []
    value: float | None = None
    previous: str | None = None
    for day, kg in weigh_ins:
        if value is None or previous is None:
            value = kg
        else:
            gap = (date.fromisoformat(day) - date.fromisoformat(previous)).days
            alpha = 1 - (1 - TREND_ALPHA) ** gap
            start, target = value, value + alpha * (kg - value)
            kind = "interpolated" if gap <= INTERPOLATE_MAX_GAP_DAYS else "estimated"
            for k in range(1, gap):
                out.append({"date": _day(previous, k), "kg": start + (target - start) * k / gap, "kind": kind,
                            "weigh_in": None, "gap_days": gap, "last_weigh_in": previous})  # fmt: skip
            value = target
        out.append({"date": day, "kg": value, "kind": "weigh_in", "weigh_in": kg, "gap_days": 0, "last_weigh_in": day})
        previous = day
    if previous is not None and value is not None:
        for k in range(1, CARRY_DAYS + 1):
            out.append({"date": _day(previous, k), "kg": value, "kind": "carried", "weigh_in": None, "gap_days": k,
                        "last_weigh_in": previous})  # fmt: skip
    for d in out:
        d["kg"] = round(d["kg"], 2)
        if d["weigh_in"] is not None:
            d["weigh_in"] = round(d["weigh_in"], 2)
    return out


def weight_trend(
    conn: sqlite3.Connection, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """The labelled daily trend in the range (all of it by default), and how many days of each kind."""
    days = [
        d for d in trend(daily_weigh_ins(conn))
        if (not start_date or d["date"] >= start_date) and (not end_date or d["date"] <= end_date)
    ]  # fmt: skip
    return {
        "days": days,
        "counts": {k: sum(1 for d in days if d["kind"] == k) for k in KINDS},
        "interpolate_max_gap_days": INTERPOLATE_MAX_GAP_DAYS,
        "carry_days": CARRY_DAYS,
    }


def weight_on(conn: sqlite3.Connection, day: str) -> dict[str, Any] | None:
    """The labelled trend on ``day``, or None (before the first weigh-in, or over ``CARRY_DAYS`` after
    the last)."""
    return next((d for d in trend(daily_weigh_ins(conn)) if d["date"] == day), None)
