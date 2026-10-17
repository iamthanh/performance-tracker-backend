"""Nutrition: meals and fueling during workouts, saved foods, daily energy in and out.

- ``nutrition_entries``: one row per thing eaten or drunk. Each entry stores its own
  nutrient totals (servings applied), copied when it is logged, so editing or deleting
  a saved food never rewrites history. An entry can point at the workout it fuelled
  (``activity_source``/``activity_source_id``), with the time into it (``offset_s``).
- ``foods``: saved items with nutrients per serving, for one-tap logging.
- Daily totals and a workout's fueling are computed from the entries, never stored.

Energy out: a workout's kcal is its recorded calories, else its mechanical work in kJ
(for cycling, kJ of work ~ kcal burned: ~24 % gross efficiency cancels 4.184 kJ/kcal).
A day's total is the watch's total calories (Garmin, else Google Health), when synced.

Carbohydrate burned during a workout is a rough estimate from its intensity factor
(the crossover concept: the harder the effort, the larger the share of energy from
carbohydrate), meant for comparing with carbohydrate eaten, not as a measurement.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import queries
from .util import InputError, now_utc_iso, parse_date, parse_ts

NUTRIENTS = (
    "kcal",
    "carbs_g",
    "sugars_g",
    "protein_g",
    "fat_g",
    "fiber_g",
    "sodium_mg",
    "potassium_mg",
    "caffeine_mg",
    "fluid_ml",
)
MEALS = ("breakfast", "lunch", "dinner", "snack", "pre_workout", "during_workout", "post_workout")
DURING = "during_workout"
# Daily calorie totals by preference, like the health charts.
CALORIE_SOURCES = ("garmin", "google_health")
KCAL_PER_G_CARB = 4.0
# Share of energy from carbohydrate by intensity factor: ~1/3 at recovery pace, ~2/3 at
# IF 0.75, nearly all near threshold (Romijn et al. 1993; Brooks & Mercier 1994). Rough.
CARB_SHARE_SLOPE = 1.6
CARB_SHARE_OFFSET = -0.55
CARB_SHARE_RANGE = (0.3, 0.95)
MAX_TEXT = 200

_FOOD_FIELDS = ("name", "brand", "serving", *NUTRIENTS, "note", "archived")
_ENTRY_FIELDS = (
    "source",
    "source_id",
    "local_date",
    "consumed_at",
    "meal",
    "activity_source",
    "activity_source_id",
    "offset_s",
    "food_id",
    "name",
    "servings",
    *NUTRIENTS,
    "note",
)


# -- validation ---------------------------------------------------------------------------


def _text(value: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise InputError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise InputError(f"{field} must be text")
    value = value.strip()
    if len(value) > limit:
        raise InputError(f"{field} is longer than {limit} characters")
    return value


def _amount(value: Any, field: str, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{field} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise InputError(f"{field} must be {'positive' if positive else 'zero or more'}, got {value!r}")
    return value


def _nutrients(data: Mapping[str, Any]) -> dict[str, float | None]:
    return {n: _amount(data.get(n), n) for n in NUTRIENTS}


def _utc(value: Any, field: str) -> str | None:
    text = _text(value, field)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{field} must be an ISO date-time, got {text!r}") from exc
    if moment.tzinfo is None:
        raise InputError(f"{field} needs a time zone (e.g. 2026-10-16T12:30:00Z)")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- foods ----------------------------------------------------------------------------------


def _food(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["archived"] = bool(out["archived"])
    return out


def list_foods(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict[str, Any]]:
    """Saved foods by name (archived ones only on request)."""
    where = "" if include_archived else "WHERE archived = 0"
    return [_food(r) for r in conn.execute(f"SELECT * FROM foods {where} ORDER BY name COLLATE NOCASE, id")]


def get_food(conn: sqlite3.Connection, food_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()
    return _food(row) if row else None


def save_food(conn: sqlite3.Connection, data: Mapping[str, Any], food_id: int | None = None) -> dict[str, Any] | None:
    """Create a food, or replace food ``food_id`` (None if it does not exist)."""
    values = {
        "name": _text(data.get("name"), "name", required=True),
        "brand": _text(data.get("brand"), "brand"),
        "serving": _text(data.get("serving"), "serving"),
        **_nutrients(data),
        "note": _text(data.get("note"), "note", limit=1000),
        "archived": 1 if data.get("archived") else 0,
    }
    now = now_utc_iso()
    with conn:
        if food_id is None:
            cursor = conn.execute(
                f"INSERT INTO foods ({', '.join(_FOOD_FIELDS)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(_FOOD_FIELDS))}, ?, ?)",
                (*(values[f] for f in _FOOD_FIELDS), now, now),
            )
            food_id = int(cursor.lastrowid or 0)
        else:
            cursor = conn.execute(
                f"UPDATE foods SET {', '.join(f'{f} = ?' for f in _FOOD_FIELDS)}, updated_at = ? WHERE id = ?",
                (*(values[f] for f in _FOOD_FIELDS), now, food_id),
            )
            if cursor.rowcount == 0:
                return None
    return get_food(conn, food_id)


def delete_food(conn: sqlite3.Connection, food_id: int) -> bool:
    """Delete a saved food. Entries logged from it keep their copied name and nutrients."""
    with conn:
        conn.execute("UPDATE nutrition_entries SET food_id = NULL WHERE food_id = ?", (food_id,))
        return conn.execute("DELETE FROM foods WHERE id = ?", (food_id,)).rowcount > 0


# -- entries ---------------------------------------------------------------------------------


def _entry(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out.pop("raw", None)
    return out


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM nutrition_entries WHERE id = ?", (entry_id,)).fetchone()
    return _entry(row) if row else None


def list_entries(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    activity: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Entries in a date range and/or for one workout (linked to it or to a copy of it from
    another service), in the order they were eaten."""
    clauses, params = [], []
    if start_date:
        clauses.append("local_date >= ?")
        params.append(parse_date(start_date, "start_date").isoformat())
    if end_date:
        clauses.append("local_date <= ?")
        params.append(parse_date(end_date, "end_date").isoformat())
    if activity:
        copies = same_workout(conn, *activity) or [{"source": activity[0], "source_id": activity[1]}]
        clauses.append("(" + " OR ".join(["(activity_source = ? AND activity_source_id = ?)"] * len(copies)) + ")")
        for c in copies:
            params.extend((c["source"], c["source_id"]))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM nutrition_entries {where} "
        "ORDER BY local_date, offset_s IS NULL, offset_s, consumed_at IS NULL, consumed_at, id"
    )
    return [_entry(r) for r in conn.execute(sql, params)]


def _entry_values(
    conn: sqlite3.Connection, data: Mapping[str, Any], existing: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validated columns for a new entry, or for replacing ``existing``.

    Nutrients given in ``data`` (a key that is present, even as None to clear it) are the
    entry's own totals. Others: on create, the food's per serving x servings; on update,
    the stored totals, scaled when only the servings of a food entry changed, or refilled
    from a different food when the food changed. A food edited after logging never changes
    an entry unless the entry is re-pointed at it.
    """
    meal = data.get("meal")
    if meal not in MEALS:
        raise InputError(f"meal must be one of {', '.join(MEALS)}; got {meal!r}")
    servings = _amount(data.get("servings", existing["servings"] if existing else 1.0), "servings", positive=True)
    servings = servings or 1.0
    food = None
    food_id = data.get("food_id")
    if food_id is not None:
        if isinstance(food_id, bool) or not isinstance(food_id, int):
            raise InputError("food_id must be a whole number")
        food = get_food(conn, food_id)
        if food is None and not (existing and existing["food_id"] == food_id):
            raise InputError(f"no saved food {food_id}")
    given = {n: _amount(data[n], n) for n in NUTRIENTS if n in data}
    if existing is None or (food is not None and existing["food_id"] != food_id):
        base = {n: (food[n] * servings if food and food[n] is not None else None) for n in NUTRIENTS}
    else:
        ratio = servings / existing["servings"] if existing["food_id"] is not None and existing["servings"] else 1.0
        base = {n: (existing[n] * ratio if existing[n] is not None else None) for n in NUTRIENTS}
    nutrients = {**base, **given}

    activity_source = _text(data.get("activity_source"), "activity_source")
    activity_source_id = _text(data.get("activity_source_id"), "activity_source_id")
    if (activity_source is None) != (activity_source_id is None):
        raise InputError("activity_source and activity_source_id go together")
    unchanged_link = existing is not None and (existing["activity_source"], existing["activity_source_id"]) == (
        activity_source,
        activity_source_id,
    )
    if activity_source and not unchanged_link and not same_workout(conn, activity_source, activity_source_id or ""):
        raise InputError(f"no workout {activity_source}/{activity_source_id}")
    offset_s = _amount(data.get("offset_s"), "offset_s")
    if offset_s is not None and not activity_source:
        raise InputError("offset_s (time into the workout) needs a workout")
    name = _text(data.get("name"), "name", required=food is None and not existing) or (
        food["name"] if food else existing["name"] if existing else None
    )
    return {
        "source": _text(data.get("source"), "source") or "manual",
        "source_id": _text(data.get("source_id"), "source_id"),
        "local_date": parse_date(data.get("local_date"), "local_date").isoformat(),
        "consumed_at": _utc(data.get("consumed_at"), "consumed_at"),
        "meal": meal,
        "activity_source": activity_source,
        "activity_source_id": activity_source_id,
        "offset_s": offset_s,
        "food_id": food_id,
        "name": name,
        "servings": servings,
        **nutrients,
        "note": _text(data.get("note"), "note", limit=1000),
    }


# An edit never changes where an entry came from: importers own (source, source_id).
_UPDATE_FIELDS = tuple(f for f in _ENTRY_FIELDS if f not in ("source", "source_id"))


def save_entry(conn: sqlite3.Connection, data: Mapping[str, Any], entry_id: int | None = None) -> dict[str, Any] | None:
    """Log an entry, or replace entry ``entry_id`` (None if it does not exist). See ``_entry_values``."""
    now = now_utc_iso()
    if entry_id is None:
        values = _entry_values(conn, data)
        with conn:
            cursor = conn.execute(
                f"INSERT INTO nutrition_entries ({', '.join(_ENTRY_FIELDS)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(_ENTRY_FIELDS))}, ?, ?)",
                (*(values[f] for f in _ENTRY_FIELDS), now, now),
            )
        return get_entry(conn, int(cursor.lastrowid or 0))
    existing = get_entry(conn, entry_id)
    if existing is None:
        return None
    values = _entry_values(conn, data, existing)
    with conn:
        conn.execute(
            f"UPDATE nutrition_entries SET {', '.join(f'{f} = ?' for f in _UPDATE_FIELDS)}, updated_at = ? WHERE id = ?",
            (*(values[f] for f in _UPDATE_FIELDS), now, entry_id),
        )
    return get_entry(conn, entry_id)


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM nutrition_entries WHERE id = ?", (entry_id,)).rowcount > 0


# -- the same workout across sources -------------------------------------------------------


def same_workout(conn: sqlite3.Connection, source: str, source_id: str) -> list[dict[str, Any]]:
    """The workout and its copies from other services: activities of other sources starting
    within ``queries.DEDUPE_WINDOW_S`` of it (how the activity lists merge duplicates). Entries
    and calories may sit on any copy, and which copy a list shows can change after an import."""
    row = conn.execute(
        "SELECT source, source_id, start_time, calories FROM activities WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    if row is None:
        return []
    start = parse_ts(row["start_time"])
    copies = [dict(row)]
    if start is None:
        return copies
    window = timedelta(seconds=queries.DEDUPE_WINDOW_S)
    for other in conn.execute(
        "SELECT source, source_id, start_time, calories FROM activities WHERE local_date BETWEEN ? AND ? AND source != ?",
        ((start - timedelta(days=1)).date().isoformat(), (start + timedelta(days=1)).date().isoformat(), source),
    ):
        ts = parse_ts(other["start_time"])
        if ts is not None and abs(ts - start) <= window:
            copies.append(dict(other))
    return copies


def _best_calories(copies: list[dict[str, Any]]) -> float | None:
    """Recorded calories from the preferred source that has them."""
    known = [c for c in copies if c.get("calories")]
    return float(min(known, key=lambda c: queries.SOURCE_PRIORITY.index(c["source"])
                     if c["source"] in queries.SOURCE_PRIORITY else 99)["calories"]) if known else None  # fmt: skip


# -- summaries -------------------------------------------------------------------------------


def _sum(entries: list[dict[str, Any]]) -> dict[str, float | None]:
    """Nutrient totals; None where no entry has a value."""
    totals: dict[str, float | None] = {}
    for n in NUTRIENTS:
        values = [e[n] for e in entries if e[n] is not None]
        totals[n] = round(sum(values), 1) if values else None
    return totals


def daily_nutrition(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Per day from ``start_date`` to ``end_date``: intake totals (``entries`` counted),
    workout kcal, the watch's total and active kcal, and ``balance_kcal`` (intake minus
    total, when both are known). Every day is listed."""
    if not start_date or not end_date:
        raise InputError("start_date and end_date are required (YYYY-MM-DD)")
    start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if end < start:
        raise InputError("end_date is before start_date")
    if (end - start).days > 3660:
        raise InputError("at most ten years at a time")
    days: dict[str, dict[str, Any]] = {}
    day = start
    while day <= end:
        days[day.isoformat()] = {"entries": [], "workout_kcal": None, "total_kcal": None, "active_kcal": None}
        day += timedelta(days=1)
    for entry in list_entries(conn, start.isoformat(), end.isoformat()):
        days[entry["local_date"]]["entries"].append(entry)
    for row in queries.activities_with_metrics(conn, start.isoformat(), end.isoformat()):
        kcal = row["kcal"]  # recorded calories (from any copy of the workout), else kJ of work for rides
        if kcal is not None and row["local_date"] in days:
            bucket = days[row["local_date"]]
            bucket["workout_kcal"] = (bucket["workout_kcal"] or 0.0) + kcal
    rank = {s: i for i, s in enumerate(CALORIE_SOURCES)}
    best: dict[tuple[str, str], tuple[int, float]] = {}
    for r in conn.execute(
        "SELECT source, date, metric, value FROM daily_metrics WHERE date BETWEEN ? AND ? "
        "AND metric IN ('total_calories_kcal', 'active_calories_kcal') AND value IS NOT NULL",
        (start.isoformat(), end.isoformat()),
    ):
        key = (r["date"], r["metric"])
        r_rank = rank.get(r["source"], len(rank))
        if key not in best or r_rank < best[key][0]:
            best[key] = (r_rank, r["value"])
    for (day_iso, metric), (_, value) in best.items():
        days[day_iso]["total_kcal" if metric == "total_calories_kcal" else "active_kcal"] = value
    out = []
    for day_iso, d in days.items():
        intake = _sum(d["entries"]) if d["entries"] else {n: None for n in NUTRIENTS}
        balance = (
            round(intake["kcal"] - d["total_kcal"], 1)
            if intake["kcal"] is not None and d["total_kcal"] is not None
            else None
        )
        out.append(
            {
                "date": day_iso,
                "entries": len(d["entries"]),
                "intake": intake,
                "workout_kcal": None if d["workout_kcal"] is None else round(d["workout_kcal"], 1),
                "total_kcal": d["total_kcal"],
                "active_kcal": d["active_kcal"],
                "balance_kcal": balance,
                # Today's totals are still growing (the watch's total covers the day so far).
                "partial": day_iso >= date.today().isoformat(),
            }
        )
    return out


def carb_share(intensity_factor: float) -> float:
    """Rough share of a workout's energy from carbohydrate at this intensity factor."""
    low, high = CARB_SHARE_RANGE
    return min(max(CARB_SHARE_SLOPE * intensity_factor + CARB_SHARE_OFFSET, low), high)


def workout_fueling(conn: sqlite3.Connection, source: str, source_id: str) -> dict[str, Any] | None:
    """A workout's fueling: its entries, what was taken during it (totals and per hour of
    moving time), the energy it burned and a rough estimate of the carbohydrate burned.
    None if the workout does not exist."""
    activity = queries.get_activity(conn, source, source_id)
    if activity is None:
        return None
    metrics = conn.execute(
        "SELECT timer_s, work_kj, intensity_factor FROM activity_metrics WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    row = {**activity, **(dict(metrics) if metrics else {}), "sport": queries.sport_family(activity["activity_type"])}

    entries = list_entries(conn, activity=(source, source_id))
    during = [e for e in entries if e["meal"] == DURING]
    totals = _sum(during)
    seconds = row.get("moving_s") or row.get("timer_s") or row.get("duration_s") or 0.0
    hours = seconds / 3600

    def per_hour(nutrient: str) -> float | None:
        value = totals[nutrient]
        return round(value / hours, 1) if value is not None and hours > 0 else None

    burned = queries.workout_kcal(row, _best_calories(same_workout(conn, source, source_id)))
    intensity = row.get("intensity_factor")
    share = carb_share(intensity) if intensity else None
    carbs_burned = round(burned * share / KCAL_PER_G_CARB) if burned and share else None
    return {
        "activity": {"source": source, "source_id": source_id, "name": activity.get("name"),
                     "local_date": activity["local_date"], "moving_s": seconds or None},  # fmt: skip
        "entries": entries,
        "during": totals,
        "per_hour": {n: per_hour(n) for n in ("kcal", "carbs_g", "fluid_ml", "sodium_mg", "caffeine_mg")},
        "burned_kcal": None if burned is None else round(burned, 1),
        "intensity_factor": intensity,
        "carb_share_estimate": None if share is None else round(share, 2),
        "carbs_burned_g_estimate": carbs_burned,
        "carbs_burned_g_per_h_estimate": round(carbs_burned / hours) if carbs_burned and hours > 0 else None,
    }
