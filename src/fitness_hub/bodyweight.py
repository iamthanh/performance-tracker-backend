"""Daily body weight: a smoothed trend from the weigh-ins, filled between them, each day labelled
with where its value comes from. Nothing here is stored: the weigh-ins in ``body_measurements``
stay exactly as measured, and the trend is derived from them on every read.

- A day's **weigh-in** is the mean of that day's distinct measurements (every source, and the
  weights saved before rides; the same reading synced to two services counts once), on the date
  in this machine's time zone.
- The **trend** smooths the day-to-day swings (water, food): on each weigh-in day it moves
  ``TREND_ALPHA`` of the way to the weigh-in, more after a gap (``1 - (1 - TREND_ALPHA) ** days``),
  so it catches up after weeks without weighing (the "trend weight" of The Hacker's Diet).
- Between two weigh-ins the trend is a straight line, labelled **interpolated** when they are at
  most ``INTERPOLATE_MAX_GAP_DAYS`` apart (weight changes slowly enough that the line is close)
  and **estimated** beyond (a guess: the change may have come at any time in the gap), up to
  ``MAX_ESTIMATE_GAP_DAYS``; a longer gap has no values. The trend is smoothed on weigh-in days
  too: ``kg`` is always the trend and ``weigh_in`` the measured value.
- After the latest weigh-in it is **carried** forward, flat, for ``CARRY_DAYS``; then there is no
  value. Before the first weigh-in there is none either.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from .util import InputError

TREND_ALPHA = 0.1
INTERPOLATE_MAX_GAP_DAYS = 42
MAX_ESTIMATE_GAP_DAYS = 365
CARRY_DAYS = 30
KINDS = ("weigh_in", "interpolated", "estimated", "carried")


def _local_day(ts: str, source: str) -> str:
    """The day a weigh-in belongs to: a CSV import stores a day's value at noon UTC of that day; any
    other source stores the real instant, whose date is the one in this machine's time zone (with the
    daylight saving time of that date), so an evening weigh-in in California stays on its evening."""
    if source == "weight_csv" and ts.endswith("T12:00:00Z"):
        return ts[:10]
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def daily_weigh_ins(conn: sqlite3.Connection) -> list[tuple[str, float, int]]:
    """``[(date, kg, count)]``, one per day with a weigh-in, oldest first: the mean of its distinct
    weigh-ins (the same reading synced to two services counts once) and how many there were. A
    weight saved before a ride (``workout_weights.pre_kg``) is a weigh-in on the ride's day."""
    readings: dict[str, set[tuple[str, float]]] = {}
    for ts, source, value in conn.execute(
        "SELECT ts, source, value FROM body_measurements WHERE metric = 'weight_kg' AND value > 0"
    ):
        readings.setdefault(_local_day(ts, source), set()).add((ts, round(value, 2)))
    for local_date, start_time, kg in conn.execute(
        "SELECT a.local_date, a.start_time, w.pre_kg FROM workout_weights w "
        "JOIN activities a USING (source, source_id) WHERE w.pre_kg > 0"
    ):
        readings.setdefault(local_date, set()).add((start_time or local_date, round(kg, 2)))
    return [
        (day, sum(v for _, v in values) / len(values), len(values))
        for day, values in sorted(readings.items())
    ]


def _day(iso: str, offset: int = 0) -> str:
    return (date.fromisoformat(iso) + timedelta(days=offset)).isoformat()


def trend(weigh_ins: list[tuple[str, float, int]] | list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Every day from the first weigh-in to ``CARRY_DAYS`` after the last:
    ``{date, kg (the trend: smoothed, also on a weigh-in day), kind, weigh_in (the day's measured kg,
    else None), weigh_ins (how many it averages), gap_days, last_weigh_in}``. ``gap_days`` is the
    length of the gap a filled day sits in (days since the last weigh-in, when carried);
    ``last_weigh_in`` the date of the latest weigh-in up to the day. A gap over ``MAX_ESTIMATE_GAP_DAYS``
    is left empty: a line across years says nothing."""
    out: list[dict[str, Any]] = []
    value: float | None = None
    previous: str | None = None
    for day, kg, *count in weigh_ins:
        if value is None or previous is None:
            value = kg
        else:
            gap = (date.fromisoformat(day) - date.fromisoformat(previous)).days
            alpha = 1 - (1 - TREND_ALPHA) ** gap
            start, target = value, value + alpha * (kg - value)
            kind = "interpolated" if gap <= INTERPOLATE_MAX_GAP_DAYS else "estimated"
            if gap <= MAX_ESTIMATE_GAP_DAYS:
                for k in range(1, gap):
                    out.append({"date": _day(previous, k), "kg": start + (target - start) * k / gap,
                                "kind": kind, "weigh_in": None, "weigh_ins": 0, "gap_days": gap,
                                "last_weigh_in": previous})  # fmt: skip
            value = target
        out.append({"date": day, "kg": value, "kind": "weigh_in", "weigh_in": kg, "weigh_ins": count[0] if count else 1,
                    "gap_days": 0, "last_weigh_in": day})  # fmt: skip
        previous = day
    if previous is not None and value is not None:
        for k in range(1, CARRY_DAYS + 1):
            out.append({"date": _day(previous, k), "kg": value, "kind": "carried", "weigh_in": None, "weigh_ins": 0,
                        "gap_days": k, "last_weigh_in": previous})  # fmt: skip
    for d in out:
        d["kg"] = round(d["kg"], 2)
        if d["weigh_in"] is not None:
            d["weigh_in"] = round(d["weigh_in"], 2)
    return out


def weight_trend(
    conn: sqlite3.Connection, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """The labelled daily trend in the range (all of it by default), and how many days of each kind."""
    end = min(end_date or date.today().isoformat(), date.today().isoformat())  # carried days stop today
    if start_date and start_date > end:
        raise InputError(f"start_date {start_date} is after end_date {end}")
    days = [
        d for d in trend(daily_weigh_ins(conn)) if (not start_date or d["date"] >= start_date) and d["date"] <= end
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
