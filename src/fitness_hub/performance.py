"""Performance index: what the rider can produce, day by day, as one score and six components.

Fitness (CTL) is how much you have trained; this is how good you are: it is built from best
efforts (outputs), never from training load (inputs).

Components, each from the cycling rides of the trailing ``WINDOW_DAYS`` (like WKO's 90-day
power-duration model), in W/kg: each ride's efforts divided by its own weight (the weight saved
before it, else the trend weight of its day, ``bodyweight``), so a lighter rider at the same watts
rates higher. Rides without a weight do not count for them.

- sprint: best 5 s W/kg;
- anaerobic: best 1 min;
- vo2max: best 5 min;
- threshold: the FTP estimate of the window's W/kg curve (``analysis.estimate_ftp``: critical
  power of the best 3-30 min efforts, at least the best hour; fitted as a ``REF_KG`` rider);
- endurance: best 60 min;
- durability: the best 5 and 20 min started after ``DURABILITY_AFTER_KJ`` of work in a ride, as
  a share of the best 5 and 20 min fresh (averaged over the two): a ratio of watts, which weight
  does not change, so every ride counts.

Each day also says what weight a component's value rests on (``estimated_weight``: a ride whose
weight was estimated across a long gap), and each rise how much came from power and how much
from weight (against the window's previous best ride).

Capacity only shows when the rider goes all out, so a component rises only with a new best in the
window and otherwise falls by at most ``decay_per_week`` (as the auto FTP does). Each day also
says when the component was last *tested*: the latest ride in the window within ``TESTED_SHARE``
of the window's best at its key duration.

Each component is an index, 100 = its baseline: the average of its daily values over the first
``BASELINE_DAYS`` it has values once the first window is full (the days before are not reported).
The score is the weighted geometric mean of the indexes (a big weakness is not hidden by a big
strength), over the components that have a value; it needs the threshold. Aerobic efficiency (EF
of steady rides) is reported alongside, not in the score: heat and pacing move it.
"""

from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Any

import numpy as np

from .analysis import FTP_CURVE_DURATIONS, estimate_ftp
from .bodyweight import daily_weigh_ins, trend
from .queries import dedupe_activities, sport_family
from .util import InputError, now_utc_iso

COMPONENTS = ("sprint", "anaerobic", "vo2max", "threshold", "endurance", "durability")
LABELS = {
    "sprint": "Sprint",
    "anaerobic": "Anaerobic",
    "vo2max": "VO2max",
    "threshold": "Threshold",
    "endurance": "Endurance",
    "durability": "Durability",
}
DESCRIPTIONS = {
    "sprint": "Best 5 s W/kg",
    "anaerobic": "Best 1 min W/kg",
    "vo2max": "Best 5 min W/kg",
    "threshold": "FTP per kg, from your best 3-30 min efforts (at least your best hour)",
    "endurance": "Best 60 min W/kg",
    "durability": "Best 5 and 20 min after 1000 kJ of work, as a share of fresh",
}
# The power-curve duration that shows each component (for "last tested" and what set a new best).
KEY_DURATION_S = {"sprint": 5, "anaerobic": 60, "vo2max": 300, "threshold": 1200, "endurance": 3600}
DURABILITY_AFTER_KJ = 1000
DURABILITY_DURATIONS_S = (300, 1200)
DEFAULT_WEIGHTS = {"threshold": 30, "durability": 20, "vo2max": 15, "endurance": 15, "anaerobic": 10, "sprint": 10}
PRESETS = {
    "road": {"label": "Road / endurance", "weights": DEFAULT_WEIGHTS},
    "climber": {
        "label": "Climber",
        "weights": {"threshold": 30, "durability": 15, "vo2max": 25, "endurance": 20, "anaerobic": 5, "sprint": 5},
    },
    "time_trial": {
        "label": "Time trial",
        "weights": {"threshold": 40, "durability": 20, "vo2max": 10, "endurance": 25, "anaerobic": 3, "sprint": 2},
    },
    "criterium": {
        "label": "Criterium / sprinter",
        "weights": {"threshold": 20, "durability": 10, "vo2max": 15, "endurance": 5, "anaerobic": 25, "sprint": 25},
    },
}
WINDOW_DAYS = 90
REF_KG = 70.0  # the FTP fit runs on a rider of this weight (its W' sanity range is in joules)
GUESSED_WEIGHTS = ("estimated",)  # weight kinds flagged on a component that rests on them
# As the auto FTP. `fitness-hub performance --backtest` on the author's history: error fell from 14 % (no easing) to
# 7.2 % at 1 % a week, with little gain beyond (6.7 % at 3 %); faster easing is also favoured by the test itself, as
# the following weeks rarely hold all-out efforts.
DEFAULT_DECAY_PER_WEEK = 0.01
BASELINE_DAYS = 365
TESTED_SHARE = 0.97
RISE_SHARE = 0.005  # a rise of at least this much is listed as what changed the score
EF_WINDOW_DAYS = 42
EF_MIN_S = 3600  # steady rides: an hour or more ...
EF_MAX_VI = 1.15  # ... ridden evenly
SETTINGS_KEY = "performance"

_CURVE_DURATIONS = tuple(sorted({*KEY_DURATION_S.values(), *FTP_CURVE_DURATIONS, *DURABILITY_DURATIONS_S}))


# -- settings -------------------------------------------------------------------------------------


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """``{weights, decay_per_week}``: the saved ones, else the defaults."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    saved = json.loads(row[0]) if row else {}
    return {
        "weights": {**DEFAULT_WEIGHTS, **saved.get("weights", {})},
        "decay_per_week": saved.get("decay_per_week", DEFAULT_DECAY_PER_WEEK),
    }


def set_settings(conn: sqlite3.Connection, weights: Mapping[str, Any], decay_per_week: Any = None) -> dict[str, Any]:
    """Save the component weights (all six, 0-100, not all 0) and optionally the decay (0-5 % a week)."""
    if set(weights) != set(COMPONENTS):
        raise InputError(f"weights must give exactly {', '.join(COMPONENTS)}")
    clean = {}
    for key, value in weights.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
            raise InputError(f"weight of {key} must be a number from 0 to 100")
        clean[key] = float(value)
    if not any(clean.values()):
        raise InputError("at least one weight must be above 0")
    current = get_settings(conn)
    decay = current["decay_per_week"] if decay_per_week is None else decay_per_week
    if isinstance(decay, bool) or not isinstance(decay, int | float) or not 0 <= decay <= 0.05:
        raise InputError("decay_per_week must be from 0 to 0.05")
    value = {"weights": clean, "decay_per_week": float(decay)}
    with conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTINGS_KEY, json.dumps(value), now_utc_iso()),
        )
    return get_settings(conn)


# -- the rides --------------------------------------------------------------------------------------


@dataclass
class _Rides:
    dates: list[str]  # sorted
    refs: list[dict[str, Any]]  # {source, source_id, name} per ride
    curve: np.ndarray  # rides x _CURVE_DURATIONS, watts, NaN where missing
    fatigued: np.ndarray  # rides x DURABILITY_DURATIONS_S, watts, after DURABILITY_AFTER_KJ
    kg: np.ndarray  # each ride's body weight (NaN without one)
    weight_kind: list[str | None]  # pre_ride, weigh_in, interpolated, estimated, carried; None without one
    wkg: np.ndarray  # curve / kg: what the power components use
    ftp_wkg: np.ndarray  # each ride's own FTP estimate per kg (NaN without one)


def _rides(conn: sqlite3.Connection) -> _Rides:
    marks = ",".join("?" * len(_CURVE_DURATIONS))
    rows = conn.execute(
        f"""
        SELECT a.source, a.source_id, a.local_date, a.activity_type, a.name, p.duration_s, p.watts
        FROM power_curve p JOIN activities a USING (source, source_id)
        WHERE p.duration_s IN ({marks})
        """,
        _CURVE_DURATIONS,
    ).fetchall()
    rides: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if sport_family(r["activity_type"]) != "cycling":
            continue
        ride = rides.setdefault(
            (r["source"], r["source_id"]),
            {"date": r["local_date"], "ref": {"source": r["source"], "source_id": r["source_id"], "name": r["name"]},
             "curve": {}, "fatigued": {}},
        )  # fmt: skip
        ride["curve"][r["duration_s"]] = r["watts"]
    marks = ",".join("?" * len(DURABILITY_DURATIONS_S))
    for r in conn.execute(
        "SELECT source, source_id, duration_s, watts FROM durability_bests "
        f"WHERE after_kj = ? AND duration_s IN ({marks})",
        (DURABILITY_AFTER_KJ, *DURABILITY_DURATIONS_S),
    ):
        ride = rides.get((r["source"], r["source_id"]))
        if ride is not None:
            ride["fatigued"][r["duration_s"]] = r["watts"]
    ordered = sorted(rides.values(), key=lambda x: (x["date"], x["ref"]["source"], x["ref"]["source_id"]))
    curve = np.full((len(ordered), len(_CURVE_DURATIONS)), np.nan)
    fatigued = np.full((len(ordered), len(DURABILITY_DURATIONS_S)), np.nan)
    for i, ride in enumerate(ordered):
        for j, d in enumerate(_CURVE_DURATIONS):
            if d in ride["curve"]:
                curve[i, j] = ride["curve"][d]
        for j, d in enumerate(DURABILITY_DURATIONS_S):
            if d in ride["fatigued"]:
                fatigued[i, j] = ride["fatigued"][d]
    ftp = np.array(
        [estimate_ftp({d: w for d, w in r["curve"].items() if d in FTP_CURVE_DURATIONS}) or np.nan for r in ordered]
    )
    # Each ride's weight: the one saved before it, else the trend weight of its day (bodyweight).
    by_day = {d["date"]: d for d in trend(daily_weigh_ins(conn))}
    pre_ride = {
        (r[0], r[1]): r[2]
        for r in conn.execute("SELECT source, source_id, pre_kg FROM workout_weights WHERE pre_kg > 0")
    }
    kg = np.full(len(ordered), np.nan)
    kinds: list[str | None] = []
    for i, ride in enumerate(ordered):
        saved = pre_ride.get((ride["ref"]["source"], ride["ref"]["source_id"]))
        day = by_day.get(ride["date"])
        if saved:
            kg[i] = saved
            kinds.append("pre_ride")
        elif day:
            kg[i] = day["kg"]
            kinds.append(day["kind"])
        else:
            kinds.append(None)
    return _Rides(
        [r["date"] for r in ordered], [r["ref"] for r in ordered], curve, fatigued, kg, kinds,
        curve / kg[:, None], ftp / kg,
    )  # fmt: skip


def _col(duration_s: int) -> int:
    return _CURVE_DURATIONS.index(duration_s)


def _max(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(finite.max()) if finite.size else None


def _window_measures(rides: _Rides, lo: int, hi: int) -> dict[str, float | None]:
    """Each component's raw measure over rides ``lo:hi`` (the window)."""
    if hi <= lo:
        return dict.fromkeys(COMPONENTS)
    block = rides.wkg[lo:hi]
    envelope = {d: _max(block[:, _col(d)]) for d in _CURVE_DURATIONS}
    out: dict[str, float | None] = {c: envelope[d] for c, d in KEY_DURATION_S.items() if c != "threshold"}
    # The FTP model is linear, so W/kg works as well as watts; its sanity range for W' is in joules, so
    # the fit runs on the curve of a REF_KG rider and the result is scaled back.
    threshold = estimate_ftp({d: w * REF_KG for d, w in envelope.items() if w and d in FTP_CURVE_DURATIONS})
    out["threshold"] = threshold / REF_KG if threshold else None
    # Durability is a ratio of the same ride's watts: weight cancels, so every ride counts.
    watts = {d: _max(rides.curve[lo:hi, _col(d)]) for d in DURABILITY_DURATIONS_S}
    shares = []
    for j, d in enumerate(DURABILITY_DURATIONS_S):
        tired, fresh = _max(rides.fatigued[lo:hi, j]), watts[d]
        if tired and fresh:
            shares.append(min(tired / fresh, 1.0))
    out["durability"] = float(np.mean(shares)) if shares else None
    return out


def _last_tested(rides: _Rides, lo: int, hi: int, component: str, value: float | None) -> str | None:
    """The date of the latest ride in the window within ``TESTED_SHARE`` of the component's value
    that day (so an easy ride after a layoff does not count as a test of an older, higher best):
    its own FTP estimate for the threshold, its share of the window's fresh power for durability."""
    if hi <= lo or not value:
        return None
    if component == "threshold":
        column = rides.ftp_wkg[lo:hi]
    elif component == "durability":
        fresh = _max(rides.curve[lo:hi, _col(DURABILITY_DURATIONS_S[0])])
        if not fresh:
            return None
        column = rides.fatigued[lo:hi, 0] / fresh
    else:
        column = rides.wkg[lo:hi, _col(KEY_DURATION_S[component])]
    with np.errstate(invalid="ignore"):
        near = np.flatnonzero(column >= TESTED_SHARE * value)
    return rides.dates[lo + int(near[-1])] if near.size else None


def _best_index(rides: _Rides, lo: int, hi: int, component: str) -> int | None:
    """The ride among ``lo:hi`` with the highest key measure of the component (W/kg; durability: watts)."""
    if component == "durability":
        column = rides.fatigued[lo:hi, 0]
    else:
        column = rides.wkg[lo:hi, _col(KEY_DURATION_S[component])]
    if hi <= lo or not np.isfinite(column).any():
        return None
    return lo + int(np.nanargmax(column))


def _ride(rides: _Rides, i: int, component: str) -> dict[str, Any]:
    """A ride's reference with its weight and, for a power component, its key effort in watts."""
    out: dict[str, Any] = {**rides.refs[i], "date": rides.dates[i], "weight_kind": rides.weight_kind[i],
                           "kg": None if np.isnan(rides.kg[i]) else round(float(rides.kg[i]), 2)}  # fmt: skip
    if component != "durability":
        watts = rides.curve[i, _col(KEY_DURATION_S[component])]
        out["watts"] = None if np.isnan(watts) else round(float(watts), 1)
    return out


def _split(change: float, kg: float | None, before_kg: float | None) -> dict[str, float] | None:
    """How much of a W/kg rise (``change``: new / previous value) came from weight and how much from
    power, against the weight of the ride the previous value rested on: ``{power_pct, weight_pct}``
    (weight_pct > 0: lighter); (1 + power) x (1 + weight) = change. The power part includes what the
    previous value had eased by."""
    if not kg or not before_kg:
        return None
    weight = before_kg / kg
    return {"power_pct": round(100 * (change / weight - 1), 1), "weight_pct": round(100 * (weight - 1), 1)}


def _efficiency(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    rows = conn.execute(
        """
        SELECT a.source, a.source_id, a.start_time, a.local_date, a.activity_type, m.efficiency_factor
        FROM activity_metrics m JOIN activities a USING (source, source_id)
        WHERE m.efficiency_factor IS NOT NULL AND m.timer_s >= ? AND m.variability_index <= ?
        """,
        (EF_MIN_S, EF_MAX_VI),
    ).fetchall()
    rides = [dict(r) for r in rows if sport_family(r["activity_type"]) == "cycling"]
    # The same ride stored from two services counts once.
    return sorted((r["local_date"], r["efficiency_factor"]) for r in dedupe_activities(rides))


# -- the timeline -----------------------------------------------------------------------------------


def _days(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def raw_timeline(conn: sqlite3.Connection, decay_per_week: float, end: str | None = None) -> dict[str, Any]:
    """Each day's component values (W/kg, or a share for durability), last-tested dates, the weight
    kind of the ride each value rests on (``basis``) and rises, from the first ride to ``end``
    (default today)."""
    rides = _rides(conn)
    if not rides.dates:
        return {"days": [], "events": []}
    last = end or date.today().isoformat()
    daily = (1 - decay_per_week) ** (1 / 7)
    values: dict[str, float | None] = dict.fromkeys(COMPONENTS)
    basis: dict[str, int | None] = dict.fromkeys(COMPONENTS)  # the ride each value rests on
    out_days, events = [], []
    for day in _days(rides.dates[0], max(last, rides.dates[0])):
        start = (date.fromisoformat(day) - timedelta(days=WINDOW_DAYS - 1)).isoformat()
        lo, hi = bisect_left(rides.dates, start), bisect_right(rides.dates, day)
        today = bisect_left(rides.dates, day)
        measures = _window_measures(rides, lo, hi)
        row: dict[str, Any] = {"date": day, "values": {}, "tested": {}, "measured": measures, "basis": {}}
        for c in COMPONENTS:
            previous = values[c]
            decayed = previous * daily if previous is not None else None
            measured = measures[c]
            value = max(v for v in (measured, decayed) if v is not None) if measured or decayed else None
            # A rise is a new best above yesterday's value (not just above where it would have eased to).
            if previous and value and measured and measured > previous * (1 + RISE_SHARE) and today < hi:
                best_today = _best_index(rides, today, hi, c)
                ride = _ride(rides, best_today, c) if best_today is not None else None
                before = basis[c]
                split = None
                if ride and c != "durability" and before is not None and not np.isnan(rides.kg[before]):
                    split = _split(value / previous, ride["kg"], float(rides.kg[before]))
                events.append({"date": day, "component": c, "from": previous, "to": value, "ride": ride,
                               "split": split})  # fmt: skip
            if measured is not None and (decayed is None or measured >= decayed):
                basis[c] = _best_index(rides, lo, hi, c)
            values[c] = value
            kind = rides.weight_kind[basis[c]] if basis[c] is not None and c != "durability" else None
            row["basis"][c] = kind
            row["values"][c] = value
            row["tested"][c] = _last_tested(rides, lo, hi, c, value)
        out_days.append(row)
    return {"days": out_days, "events": events}


def _baselines(days: list[dict[str, Any]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for c in COMPONENTS:
        # From a full window on: the first WINDOW_DAYS only see part of it and read low.
        seen = [d["values"][c] for d in days[WINDOW_DAYS - 1 :] if d["values"][c] is not None][:BASELINE_DAYS]
        out[c] = float(np.mean(seen)) if seen else None
    return out


def score(indexes: Mapping[str, float | None], weights: Mapping[str, float]) -> float | None:
    """The weighted geometric mean of the components' indexes that have a value; None without the
    threshold or any weight."""
    if indexes.get("threshold") is None:
        return None
    parts = [(weights[c], indexes[c]) for c in COMPONENTS if indexes.get(c) and weights.get(c)]
    total = sum(w for w, _ in parts)
    if not total:
        return None
    return math.exp(sum(w * math.log(i) for w, i in parts) / total)


def performance(
    conn: sqlite3.Connection, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """The performance index from ``start_date`` to ``end_date`` (default: everything to today; the
    end is at most today). The timeline always runs to today, so a day's score does not depend on
    the range asked for."""
    today = date.today().isoformat()
    end = min(end_date or today, today)
    if start_date and start_date > end:
        raise InputError(f"start_date {start_date} is after end_date {end}")
    settings = get_settings(conn)
    weights = settings["weights"]
    raw = raw_timeline(conn, settings["decay_per_week"])
    baselines = _baselines(raw["days"])
    reported = raw["days"][WINDOW_DAYS - 1 :]
    first_reported = reported[0]["date"] if reported else None
    efficiency = _efficiency(conn)
    ef_dates = [d for d, _ in efficiency]
    days = []
    # The first window is not full yet: every component reads low, so those days are left out.
    for d in reported:
        if (start_date and d["date"] < start_date) or d["date"] > end:
            continue
        indexes = {
            c: (100 * d["values"][c] / baselines[c]) if d["values"][c] is not None and baselines[c] else None
            for c in COMPONENTS
        }
        ef_start = (date.fromisoformat(d["date"]) - timedelta(days=EF_WINDOW_DAYS - 1)).isoformat()
        ef = [v for _, v in efficiency[bisect_left(ef_dates, ef_start) : bisect_right(ef_dates, d["date"])]]
        total = score(indexes, weights)
        days.append(
            {
                "date": d["date"],
                "score": None if total is None else round(total, 1),
                "components": {
                    c: {
                        "index": None if indexes[c] is None else round(indexes[c], 1),
                        "value": None if d["values"][c] is None else round(d["values"][c], 3),
                        "tested": d["tested"][c],
                        # The ride this value rests on had its weight estimated across a long gap.
                        "estimated_weight": d["basis"][c] in GUESSED_WEIGHTS,
                    }
                    for c in COMPONENTS
                },
                "efficiency": round(median(ef), 3) if ef else None,
            }
        )
    events = [
        {**e, "from": round(e["from"], 3), "to": round(e["to"], 3),
         "index_change": round(100 * (e["to"] - e["from"]) / baselines[e["component"]], 1)}
        for e in raw["events"]
        if first_reported and max(start_date or "", first_reported) <= e["date"] <= end and baselines[e["component"]]
    ]  # fmt: skip
    return {
        "weights": weights,
        "decay_per_week": settings["decay_per_week"],
        "presets": PRESETS,
        "window_days": WINDOW_DAYS,
        "components": [
            {"key": c, "label": LABELS[c], "description": DESCRIPTIONS[c],
             "unit": "share" if c == "durability" else "W/kg",
             "baseline": None if baselines[c] is None else round(baselines[c], 3)}
            for c in COMPONENTS
        ],  # fmt: skip
        "first_ride": raw["days"][0]["date"] if raw["days"] else None,
        # The first day with a full window: the index starts here.
        "starts": first_reported or (
            (date.fromisoformat(raw["days"][0]["date"]) + timedelta(days=WINDOW_DAYS - 1)).isoformat()
            if raw["days"] else None
        ),
        "days": days,
        "events": events,
    }


# -- calibration ------------------------------------------------------------------------------------


def backtest(
    conn: sqlite3.Connection, decays: tuple[float, ...] = (0.0, 0.005, 0.01, 0.02, 0.03), horizon_days: int = 28
) -> list[dict[str, Any]]:
    """How well each decay rate predicts what the rider does next: for every reported day and
    component (but durability), the value against the same measure over the rides of the following
    ``horizon_days`` (for the threshold, the FTP estimate of their combined curve, as the value is).
    Only days whose next rides hold a hard effort count: at least 90 % of the window's measured best
    that day, which does not depend on the decay, so every decay is judged on the same days and
    detraining days count too. ``mae_pct``: mean absolute error; ``bias_pct``: mean signed error
    (negative: the value runs above what the rider then does); ``under_pct``: share of efforts over
    3 % above the value (capacity underestimated)."""
    rides = _rides(conn)
    timelines = [raw_timeline(conn, decay)["days"] for decay in decays]
    errors: list[list[float]] = [[] for _ in decays]
    for t in range(WINDOW_DAYS - 1, len(timelines[0])):
        day = timelines[0][t]
        lo = bisect_right(rides.dates, day["date"])
        hi = bisect_right(rides.dates, (date.fromisoformat(day["date"]) + timedelta(days=horizon_days)).isoformat())
        if hi <= lo:
            continue
        ahead = _window_measures(rides, lo, hi)
        for c in KEY_DURATION_S:
            measured, actual = day["measured"][c], ahead[c]
            predictions = [timeline[t]["values"][c] for timeline in timelines]
            if not measured or not actual or any(p is None for p in predictions) or actual < 0.9 * measured:
                continue  # nothing hard enough to say what the rider could do
            for k, predicted in enumerate(predictions):
                errors[k].append((actual - predicted) / predicted)
    out = []
    for decay, errs in zip(decays, errors, strict=True):
        e = np.asarray(errs)
        out.append(
            {"decay_per_week": decay, "samples": int(e.size),
             "mae_pct": round(100 * float(np.abs(e).mean()), 2) if e.size else None,
             "bias_pct": round(100 * float(e.mean()), 2) if e.size else None,
             "under_pct": round(100 * float((e > 0.03).mean()), 1) if e.size else None}
        )  # fmt: skip
    return out
