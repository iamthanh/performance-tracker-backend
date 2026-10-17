"""Per-ride insights that pros and coaches use beyond NP/IF/TSS, computed on demand from the streams.

- **Body weight** for per-kg values: the weight saved before the workout, else the latest
  body-weight measurement on or before its day.
- **Durability** (fatigue resistance): the best 1-, 5- and 20-minute power *after* the
  ride had already done 10, 20, 30 ... kJ/kg of work (1000, 2000 ... kJ without a body
  weight), next to the same efforts from the start. Professionals' best efforts fall
  progressively with accumulated work, most clearly beyond about 20 kJ/kg, and how
  little they fall separates riders that fresh power does not (Mateo-March et al.
  2022 and 2025; Spragg et al. 2024).
- **W' balance**: how much of the work capacity above critical power (W') is left at
  each second, with Skiba's differential model (Skiba et al. 2015: spent 1:1 above CP,
  recovered in proportion to what is missing and how far below CP the rider is). CP is
  the FTP in effect for the ride; W' comes from the critical-power fit of the trailing
  year's best efforts up to the ride, else ``DEFAULT_W_PRIME_J``. A **match** is a drop
  of at least ``MATCH_J`` within ``MATCH_IDLE_S`` of a high point (GoldenCheetah counts
  matches from 2 kJ), ended once ``MATCH_RECOVERED`` of W' (or half the drop, if less) is
  back, or once W' has not fallen another ``MATCH_PROGRESS_J`` in ``MATCH_IDLE_S``.
- **Efforts**: every stretch above FTP of at least ``MIN_EFFORT_S`` seconds (``MIN_SURGE_S``
  for an anaerobic one, from ``SURGE_OF_FTP``), found on
  a ``SMOOTH_S``-second centred average so a short dip does not split an interval, and
  trimmed to the seconds that were at or above FTP.

All of it needs cycling power; efforts and W' balance also need an FTP.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

import numpy as np

from .analysis import (
    AUTO_WINDOW_DAYS,
    MIN_POWER_SHARE,
    POWER_ZONES,
    _ride_curves,
    critical_power,
    timer_clock,
)
from .db import load_streams
from .nutrition import get_workout_weight
from .queries import downsample, get_activity, sport_family

DURABILITY_DURATIONS_S = (60, 300, 1200)
DURABILITY_STEP_KJ_KG = 10
DURABILITY_STEP_KJ = 1000  # without a body weight
DURABILITY_MAX_STEPS = 6
DEFAULT_W_PRIME_J = 20000.0
MATCH_J = 2000.0
MATCH_RECOVERED = 0.1  # of W'
MATCH_IDLE_S = 300  # a match also ends once W' has not fallen by MATCH_PROGRESS_J for this long
MATCH_PROGRESS_J = 1000.0
SMOOTH_S = 10
MERGE_GAP_S = 10
MIN_EFFORT_S = 30
MIN_SURGE_S = 5  # shorter efforts count when they are anaerobic (sprints, kicks) ...
SURGE_OF_FTP = 1.2  # ... at this share of FTP or more (Coggan Z6 and up)
MAX_EFFORTS = 200


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


# -- body weight --------------------------------------------------------------------------


def ride_weight(conn: sqlite3.Connection, source: str, source_id: str, local_date: str) -> dict[str, Any] | None:
    """``{kg, origin: "pre_workout" | "body", date}`` for per-kg values, or None."""
    saved = get_workout_weight(conn, source, source_id)
    if saved and saved.get("pre_kg"):
        return {"kg": saved["pre_kg"], "origin": "pre_workout", "date": local_date}
    end = (date.fromisoformat(local_date) + timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT ts, value FROM body_measurements WHERE metric = 'weight_kg' AND value > 0 AND ts < ? "
        "ORDER BY ts DESC LIMIT 1",
        (end,),
    ).fetchone()
    return {"kg": row["value"], "origin": "body", "date": row["ts"][:10]} if row else None


# -- durability -----------------------------------------------------------------------------


def durability(power: np.ndarray, weight_kg: float | None) -> dict[str, Any] | None:
    """Best power per duration after each step of accumulated work (see the module docstring).

    ``power`` is the 1 Hz timer series with pauses as 0 W. A cell is None when too little of the
    ride is left after the step for that duration."""
    if power.size < min(DURABILITY_DURATIONS_S):
        return None
    work = np.cumsum(power)  # J done by the end of each second
    per = weight_kg * 1000 if weight_kg else 1000.0  # J per unit (kJ/kg or kJ)
    step = DURABILITY_STEP_KJ_KG if weight_kg else DURABILITY_STEP_KJ
    total = work[-1] / per
    steps = [k * step for k in range(DURABILITY_MAX_STEPS) if k * step <= total]
    sums = np.concatenate(([0.0], work))
    rows = []
    for d in DURABILITY_DURATIONS_S:
        if d > power.size:
            continue
        averages = (sums[d:] - sums[:-d]) / d  # averages[i]: the window starting at second i
        values = []
        for s in steps:
            start = 0 if s == 0 else int(np.searchsorted(work, s * per)) + 1  # the first second after the step
            values.append(_round(averages[start:].max(), 0) if start < averages.size else None)
        rows.append({"duration_s": d, "watts": values})
    return {"unit": "kJ/kg" if weight_kg else "kJ", "steps": steps, "total": _round(total), "rows": rows}


# -- W' balance -----------------------------------------------------------------------------


def w_prime_for(conn: sqlite3.Connection, local_date: str) -> tuple[float, str]:
    """``(W' in J, "fit" | "default")``: from the critical-power fit of the trailing year's best efforts."""
    start = (date.fromisoformat(local_date) - timedelta(days=AUTO_WINDOW_DAYS - 1)).isoformat()
    best: dict[int, float] = {}
    for day, curve in _ride_curves(conn):
        if start <= day <= local_date:
            for d, w in curve.items():
                best[d] = max(best.get(d, 0.0), w)
    fit = critical_power(best)
    return (fit[1], "fit") if fit else (DEFAULT_W_PRIME_J, "default")


def w_balance(power: np.ndarray, cp: float, w_prime: float) -> np.ndarray:
    """W' left at the end of each second (J), Skiba's differential model; it can go below 0 when CP
    or W' is set too low for the ride."""
    out = np.empty(power.size)
    bal = w_prime
    for i, p in enumerate(power):
        if p > cp:
            bal -= p - cp
        else:
            bal += (w_prime - bal) * (cp - p) / w_prime
        out[i] = bal
    return out


def matches(balance: np.ndarray, w_prime: float) -> list[tuple[int, int]]:
    """``(high, low)`` second indices of each drop of at least ``MATCH_J`` within ``MATCH_IDLE_S`` of
    a high point."""
    found: list[tuple[int, int]] = []
    high = low = mark = 0  # mark: when the match last fell by MATCH_PROGRESS_J
    burning = False
    for i, b in enumerate(balance):
        if not burning:
            if b >= balance[high] or i - high >= MATCH_IDLE_S:  # a slow drain is not a match
                high = i
            elif balance[high] - b >= MATCH_J:
                burning, low, mark = True, i, i
            continue
        if b < balance[low]:
            low = i
        if balance[mark] - b >= MATCH_PROGRESS_J:
            mark = i
        recovered = b - balance[low] >= min(MATCH_RECOVERED * w_prime, (balance[high] - balance[low]) / 2)
        if recovered or i - mark >= MATCH_IDLE_S:
            found.append((high, low))
            burning, high = False, i
    if burning:
        found.append((high, low))
    return found


# -- efforts --------------------------------------------------------------------------------


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """``[(start, end)]`` (end exclusive) of the True stretches."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist(), strict=True))


def effort_spans(power: np.ndarray, paused: np.ndarray, ftp: float) -> list[tuple[int, int]]:
    """``[(start, end)]`` seconds of the efforts above ``ftp`` (see the module docstring)."""
    if power.size < SMOOTH_S:
        return []
    riding = (~paused).astype(float)
    kernel = np.ones(SMOOTH_S)
    with np.errstate(invalid="ignore", divide="ignore"):  # the riding seconds of each window only
        smooth = np.convolve(power * riding, kernel, mode="same") / np.convolve(riding, kernel, mode="same")
    above = (smooth >= ftp) & ~paused
    spans: list[list[int]] = []
    for start, end in _runs(above):
        hot = np.flatnonzero(power[start:end] >= ftp)
        if hot.size == 0:
            continue
        start, end = start + int(hot[0]), start + int(hot[-1]) + 1
        if spans and start - spans[-1][1] <= MERGE_GAP_S and not paused[spans[-1][1] : start].any():
            spans[-1][1] = end
        else:
            spans.append([start, end])
    return [
        (s, e) for s, e in spans
        if e - s >= MIN_EFFORT_S or (e - s >= MIN_SURGE_S and power[s:e].mean() >= SURGE_OF_FTP * ftp)
    ]  # fmt: skip


def _mean(values: np.ndarray) -> float | None:
    values = values[~np.isnan(values)]
    return float(values.mean()) if values.size else None


def _max(values: np.ndarray) -> float | None:
    values = values[~np.isnan(values)]
    return float(values.max()) if values.size else None


# -- putting it together --------------------------------------------------------------------


def _ride_power(conn: sqlite3.Connection, source: str, source_id: str) -> dict[str, Any] | None:
    """The ride's 1 Hz series (pauses kept) with its activity, metrics and streams, or None
    without cycling power."""
    activity = get_activity(conn, source, source_id)
    if activity is None or sport_family(activity["activity_type"]) != "cycling":
        return None
    streams = load_streams(conn, source, str(source_id))
    if not streams or not any(v for v in streams.get("power") or [] if v):
        return None
    clock, paused, series = timer_clock(streams, ("power", "heart_rate", "cadence"))
    power = np.nan_to_num(series["power"], nan=0.0)
    if power.size == 0 or float(np.mean(power[~paused] > 0)) < MIN_POWER_SHARE:
        return None
    metrics = conn.execute(
        "SELECT ftp_w FROM activity_metrics WHERE source = ? AND source_id = ?", (source, str(source_id))
    ).fetchone()
    return {
        "activity": activity,
        "streams": streams,
        "clock": clock,
        "power": power,
        "paused": paused,
        "hr": series["heart_rate"],
        "cadence": series["cadence"],
        "ftp": metrics["ftp_w"] if metrics and metrics["ftp_w"] else None,
    }


def activity_insights(conn: sqlite3.Connection, source: str, source_id: str) -> dict[str, Any] | None:
    """Weight, durability, W' balance summary and efforts of one workout (None if it does not
    exist); the parts that need cycling power or an FTP are None without them."""
    activity = get_activity(conn, source, source_id)
    if activity is None:
        return None
    weight = ride_weight(conn, source, str(source_id), activity["local_date"])
    out: dict[str, Any] = {"weight": weight, "durability": None, "w_prime": None, "efforts": None}
    ride = _ride_power(conn, source, source_id)
    if ride is None:
        return out
    power, clock, ftp = ride["power"], ride["clock"], ride["ftp"]
    out["durability"] = durability(power, weight["kg"] if weight else None)
    if not ftp:
        return out
    w_prime, origin = w_prime_for(conn, activity["local_date"])
    balance = w_balance(power, ftp, w_prime)
    low = int(np.argmin(balance))
    out["w_prime"] = {
        "cp_w": _round(ftp, 0),
        "w_prime_j": _round(w_prime, 0),
        "origin": origin,
        "min_j": _round(balance[low], 0),
        "min_at_s": _round(clock[low], 0),
        "matches": [
            {"start_s": _round(clock[h], 0), "low_at_s": _round(clock[lo], 0),
             "spent_j": _round(balance[h] - balance[lo], 0), "low_j": _round(balance[lo], 0)}
            for h, lo in matches(balance, w_prime)
        ],  # fmt: skip
    }
    spans = effort_spans(power, ride["paused"], ftp)
    items = []
    for s, e in spans[:MAX_EFFORTS]:
        avg = float(power[s:e].mean())
        items.append(
            {
                "start_s": _round(clock[s], 0),
                "end_s": _round(clock[e - 1] + 1, 0),
                "duration_s": e - s,
                "avg_power_w": _round(avg, 0),
                "max_power_w": _round(power[s:e].max(), 0),
                "avg_hr": _round(_mean(ride["hr"][s:e]), 0),
                "max_hr": _round(_max(ride["hr"][s:e]), 0),
                "avg_cadence": _round(_mean(ride["cadence"][s:e]), 0),
                "work_kj": _round(power[s:e].sum() / 1000),
                "pct_ftp": _round(avg / ftp * 100, 0),
                "zone": POWER_ZONES.names[POWER_ZONES.index(avg, ftp)],
                "w_prime_spent_j": _round(max(0.0, (balance[s - 1] if s else w_prime) - balance[s:e].min()), 0),
            }
        )
    above = power > ftp
    out["efforts"] = {
        "ftp_w": _round(ftp, 0),
        "count": len(spans),
        "time_above_s": int(above.sum()),
        "work_above_kj": _round(float((power[above] - ftp).sum()) / 1000),
        "items": items,
    }
    return out


def activity_streams(
    conn: sqlite3.Connection, source: str, source_id: str, max_points: int = 2000
) -> dict[str, list[Any]] | None:
    """The workout's channels for charts (like ``queries.activity_streams``) plus ``w_bal`` (W'
    left, J) for a ride with power and an FTP. ``None`` if it has no streams."""
    ride = _ride_power(conn, source, source_id)
    if ride is None or not ride["ftp"]:
        streams = load_streams(conn, source, str(source_id))
        return None if streams is None else downsample(streams, max(10, int(max_points)))
    streams = dict(ride["streams"])
    w_prime, _ = w_prime_for(conn, ride["activity"]["local_date"])
    balance = w_balance(ride["power"], ride["ftp"], w_prime)
    t = np.asarray(streams["time"], dtype=float)
    # The timer clock is increasing; each sample takes the balance at its second.
    at = np.clip(np.searchsorted(ride["clock"], t - t.min(), side="right") - 1, 0, balance.size - 1)
    streams["w_bal"] = [round(float(b), 0) for b in balance[at]]
    return downsample(streams, max(10, int(max_points)))
