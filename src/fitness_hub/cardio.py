"""Cardio index: how fit the heart and lungs are, from heart rate against power, day by day.

The fitter the aerobic system, the fewer beats it takes to deliver the same power (a bigger stroke
volume, more blood, more mitochondria). Raw heart rate moves with much else, so every value is put
on the same footing first. The figures in brackets are from the author's 1,582 rides with both
power and heart rate, where each correction lowered the error of predicting a ride from the ones
before it (``backtest``: from 4.9 to 4.3 bpm).

- **Steady riding only** (``steady``): 5 min blocks of even power, after 3 min at that power (heart
  rate settles with a time constant of about a minute), from ``WARMUP_S`` into the ride (earlier
  blocks read 3-6 bpm low: the heart is still warming up), within ``REFERENCE_BAND`` of the
  reference power (heart rate is linear in power there).
- **The same power**: each block is moved along the rider's own heart rate / power slope to the
  reference power (``reference_power_w``; by default the median power of all steady riding, rounded
  to 5 W) [0.27 bpm per W, steady from year to year], and back to ``DRIFT_REF_KJ`` of work done
  (heart rate creeps up with work, up to about ``DRIFT_CAP_KJ``) [4 bpm per 1000 kJ]. Both slopes
  are fitted within rides (each ride its own level), over all of them.
- **The same conditions**: a ride's value (the median of its blocks) is corrected to outdoors,
  ``HEAT_FROM_C`` or cooler and fresh (form, TSB, of 0): indoor rides read differently (another
  power meter, a fan) [5 bpm lower]; heat raises heart rate above ``HEAT_FROM_C`` [0.6 bpm per
  degree, by the device's temperature]; and fatigue *lowers* it [0.18 bpm per point of TSB below 0:
  5 bpm in a hard block], which would otherwise read as fitness. Each effect is fitted to how
  far each ride is from the rides within ``NEIGHBOUR_DAYS`` of it (so a season of fitness does not
  pass for heat or form), all three together, repeatedly until they settle (backfitting).
  Bounded by ``CORRECTION_BOUNDS``; with too few rides for one, its default (``DEFAULTS``).

Components, each day from the rides before it:

- **aerobic** (scored): the median corrected heart rate at the reference power over the last
  ``WINDOW_DAYS``, with at least ``MIN_RIDES``; reported both ways: heart rate at the reference
  power (lower is fitter), and power at the reference heart rate along the slope (higher is fitter;
  the index uses this one, as a share of watts).
- **resting_hr** (scored): the median of the daily resting heart rates (``daily_metrics``, any
  source) over the last ``RESTING_WINDOW_DAYS``. Lower is fitter.
- **hr_recovery** (scored): how far heart rate falls in the minute after a hard effort (from
  ``RECOVERY_PEAK_OF_LTHR`` of threshold heart rate, after two minutes from ``RECOVERY_EFFORT_OF_FTP``
  of FTP) when the rider then eases right off (at most ``RECOVERY_EASE_OF_FTP`` of FTP: riding on
  keeps it up, 28 bpm when coasting against 4 at 70 % of FTP), corrected to a peak at threshold heart
  rate and recovery at 20 % of FTP; the median over ``RECOVERY_WINDOW_DAYS``.
- **drift** (shown, not scored): heart rate rise per 1000 kJ at the same power within long steady
  rides. On the author's rides it barely moved over the years and mostly followed the heat.

Each scored component is an index, 100 = the average of its first ``BASELINE_DAYS`` of values
(resting heart rate inverted, so up is fitter), and the score their weighted geometric mean
(``DEFAULT_WEIGHTS``), over those with a value; it needs the aerobic one. Unlike the performance
index it falls as well as rises: aerobic fitness comes and goes.

``vo2max`` estimates VO2max from the aerobic value (as watches do from heart rate and power): the
oxygen cost of the reference power (ACSM: 10.8 ml/min per W per kg + 7), over the share of heart
rate reserve it takes (Swain: about the share of VO2 reserve), with the day's weight, max heart
rate (auto or set) and resting heart rate (measured, else set, else ``DEFAULT_RESTING_HR``,
flagged).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any

import numpy as np

from .analysis import DEFAULT_RESTING_HR, _load_timeline
from .bodyweight import daily_weigh_ins, trend
from .nutrition import same_workout
from .queries import dedupe_activities, sport_family, training_load
from .util import InputError, now_utc_iso

COMPONENTS = ("aerobic", "resting_hr", "hr_recovery", "drift")
SCORED = ("aerobic", "resting_hr", "hr_recovery")
LABELS = {"aerobic": "Aerobic efficiency", "resting_hr": "Resting heart rate", "hr_recovery": "HR recovery",
          "drift": "Cardiac drift"}  # fmt: skip
DESCRIPTIONS = {
    "aerobic": "Power at your reference heart rate on steady riding, corrected for heat, indoors and fatigue",
    "resting_hr": "Median daily resting heart rate over the last week",
    "hr_recovery": "Heart rate drop in the minute after a hard effort, when you ease right off",
    "drift": "Heart rate rise per 1000 kJ at the same power on long steady rides (not scored: mostly heat)",
}
UNITS = {"aerobic": "W", "resting_hr": "bpm", "hr_recovery": "bpm", "drift": "bpm/1000 kJ"}
DEFAULT_WEIGHTS = {"aerobic": 60.0, "resting_hr": 25.0, "hr_recovery": 15.0}
SETTINGS_KEY = "cardio"

WARMUP_S = 1200
REFERENCE_BAND = 0.35
REFERENCE_ROUND_W = 5
DRIFT_CAP_KJ = 1500.0
DRIFT_REF_KJ = 500.0
MIN_RIDE_BLOCKS = 2
MIN_SLOPE_RIDE_BLOCKS = 3
MIN_FIT_BLOCKS = 200
HEAT_FROM_C = 25.0
NEIGHBOUR_DAYS = 14
MIN_NEIGHBOURS = 4
MIN_FIT_RIDES = 60  # rides with neighbours, for the corrections
MIN_EFFECT_RIDES = 10  # indoor rides, or outdoor rides above HEAT_FROM_C, to fit that effect
BACKFIT_ROUNDS = 25
DEFAULTS = {"slope": 0.27, "drift": 4.0, "indoor": 0.0, "heat": 0.4, "form": 0.1}
SLOPE_BOUNDS = (0.1, 0.6)
DRIFT_BOUNDS = (0.0, 10.0)
CORRECTION_BOUNDS = {"indoor": (-10.0, 10.0), "heat": (0.0, 1.5), "form": (0.0, 0.3)}
WINDOW_DAYS = 42
MIN_RIDES = 5
RESTING_WINDOW_DAYS = 7
MIN_RESTING_DAYS = 3
RECOVERY_WINDOW_DAYS = 90
MIN_RECOVERY_RIDES = 3
RECOVERY_PEAK_OF_LTHR = 0.92
RECOVERY_EFFORT_OF_FTP = 0.85
RECOVERY_EASE_OF_FTP = 0.4
RECOVERY_REF_EASE = 0.2
MIN_RECOVERY_FIT = 50
RECOVERY_DEFAULTS = (8.0, -37.6)  # bpm per unit of peak / LTHR - 1, and of recovery power / FTP - 0.2
RECOVERY_BOUNDS = ((0.0, 40.0), (-80.0, 0.0))
DRIFT_WINDOW_DAYS = 90
MIN_DRIFT_BLOCKS = 6
MIN_DRIFT_SPAN_KJ = 600.0
MIN_DRIFT_RIDES = 3
BASELINE_DAYS = 365
_TRAINER = re.compile(r"\b(kickr|trainer|turbo|tacx|wattbike|zwift|rouvy|saris|rollers|indoor|virtual)\b", re.I)
_INDOOR_TYPES = ("virtual", "indoor", "trainer")  # VirtualRide, Virtual Ride, indoor_cycling, ...


# -- settings -------------------------------------------------------------------------------------


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """``{reference_power_w, reference_hr_bpm (None: automatic), weights}``: saved, else defaults."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    saved = json.loads(row[0]) if row else {}
    return {
        "reference_power_w": saved.get("reference_power_w"),
        "reference_hr_bpm": saved.get("reference_hr_bpm"),
        "weights": {**DEFAULT_WEIGHTS, **saved.get("weights", {})},
    }


def _number(value: Any, name: str, low: float, high: float, allow_none: bool) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        automatic = " (or null: automatic)" if allow_none else ""
        raise InputError(f"{name} must be a number from {low:g} to {high:g}{automatic}")
    return float(value)


def set_settings(
    conn: sqlite3.Connection,
    weights: Mapping[str, Any],
    reference_power_w: Any = None,
    reference_hr_bpm: Any = None,
) -> dict[str, Any]:
    """Save the weights (all three scored components, 0-100, aerobic above 0) and the reference
    power (50-600 W) and heart rate (60-220 bpm); None for either reference: automatic."""
    if set(weights) != set(SCORED):
        raise InputError(f"weights must give exactly {', '.join(SCORED)}")
    clean = {k: _number(v, f"weight of {k}", 0, 100, False) for k, v in weights.items()}
    if not clean["aerobic"]:
        raise InputError("the aerobic component must weigh more than 0: the score rests on it")
    value = {
        "reference_power_w": _number(reference_power_w, "reference_power_w", 50, 600, True),
        "reference_hr_bpm": _number(reference_hr_bpm, "reference_hr_bpm", 60, 220, True),
        "weights": clean,
    }
    with conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTINGS_KEY, json.dumps(value), now_utc_iso()),
        )
    return get_settings(conn)


# -- the rides --------------------------------------------------------------------------------------


def is_indoor(activity_type: str | None, raw: Mapping[str, Any], has_gps: bool) -> bool:
    """Ridden indoors: no position recorded, a virtual or indoor sport, Strava's trainer flag, or
    gear named like a trainer (a Wahoo KICKR ride is stored as a plain "Ride")."""
    if not has_gps or any(word in (activity_type or "").lower() for word in _INDOOR_TYPES):
        return True
    if raw.get("trainer") is True:
        return True
    gear = raw.get("Activity Gear") or raw.get("Bike") or (raw.get("gear") or {}).get("name") or ""
    return bool(isinstance(gear, str) and _TRAINER.search(gear))


@dataclass
class _Ride:
    source: str
    source_id: str
    name: str | None
    date: str
    indoor: bool
    blocks: list[list[float | None]]  # [start_s, watts, bpm, temp_c, kj_before]
    recoveries: list[list[float]]  # [peak_bpm, drop_bpm, effort_w, recovery_w]
    tsb: float = 0.0
    temp_c: float | None = None  # average over the blocks used
    used: list[int] = field(default_factory=list)  # blocks in the aerobic value
    raw_bpm: float | None = None  # median of the used blocks at the reference power and work
    bpm: float | None = None  # corrected to outdoors, cool and fresh
    recovery: float | None = None  # median corrected drop
    recovery_count: int = 0
    drift: float | None = None

    @property
    def ref(self) -> dict[str, Any]:
        return {"source": self.source, "source_id": self.source_id, "name": self.name, "date": self.date}

    def heat(self) -> float:
        return 0.0 if self.indoor or self.temp_c is None else max(self.temp_c - HEAT_FROM_C, 0.0)


def _load_rides(conn: sqlite3.Connection) -> list[_Ride]:
    rows = conn.execute(
        """
        SELECT a.source, a.source_id, a.start_time, a.local_date, a.activity_type, a.name, a.raw,
               c.has_gps, c.blocks, c.recoveries
        FROM ride_cardio c JOIN activities a USING (source, source_id)
        """
    ).fetchall()
    rides = [dict(r) for r in rows if sport_family(r["activity_type"]) == "cycling"]
    out = []
    for r in sorted(dedupe_activities(rides), key=lambda r: (r["local_date"], r["start_time"], r["source"])):
        try:
            raw = json.loads(r["raw"]) if r["raw"] else {}
        except ValueError:
            raw = {}
        out.append(
            _Ride(r["source"], r["source_id"], r["name"], r["local_date"],
                  is_indoor(r["activity_type"], raw if isinstance(raw, dict) else {}, bool(r["has_gps"])),
                  json.loads(r["blocks"]), json.loads(r["recoveries"]))
        )  # fmt: skip
    return out


def _form(conn: sqlite3.Connection, first: str) -> dict[str, float]:
    """Form (TSB) going into each day, as the Fitness page shows it."""
    return {d["date"]: d["tsb"] for d in training_load(conn, first, date.today().isoformat())}


# -- the model ---------------------------------------------------------------------------------------


@dataclass
class Model:
    reference_w: float
    reference_bpm: float
    reference_auto: tuple[bool, bool]
    slope: float  # bpm per W
    drift: float  # bpm per 1000 kJ, up to DRIFT_CAP_KJ
    corrections: dict[str, float]  # indoor (bpm), heat (bpm per degree above HEAT_FROM_C), form (bpm per TSB)
    fitted: dict[str, bool]
    recovery: tuple[float, float]
    rides: int = 0
    blocks: int = 0

    def correction(self, ride: _Ride) -> dict[str, float]:
        """What each condition added to the ride's heart rate (bpm; subtracted to correct it)."""
        c = self.corrections
        return {"indoor": c["indoor"] * ride.indoor, "heat": c["heat"] * ride.heat(), "form": c["form"] * ride.tsb}

    def power_at(self, bpm: float) -> float:
        """Power at the reference heart rate, from heart rate ``bpm`` at the reference power."""
        return self.reference_w + (self.reference_bpm - bpm) / self.slope

    def as_dict(self) -> dict[str, Any]:
        c = self.corrections
        return {
            "reference": {"power_w": self.reference_w, "hr_bpm": self.reference_bpm,
                          "power_auto": self.reference_auto[0], "hr_auto": self.reference_auto[1]},
            "slope_bpm_per_w": round(self.slope, 3),
            "drift_bpm_per_1000kj": round(self.drift, 2),
            "corrections": {"indoor_bpm": round(c["indoor"], 2), "heat_bpm_per_c": round(c["heat"], 3),
                            "heat_from_c": HEAT_FROM_C, "form_bpm_per_tsb": round(c["form"], 3)},
            "fitted": self.fitted,
            "rides": self.rides,
            "blocks": self.blocks,
        }  # fmt: skip


def _warm(ride: _Ride) -> list[tuple[int, float, float, float | None, float]]:
    """The ride's blocks from WARMUP_S on: ``(index, watts, bpm, temp_c, kj_before)``."""
    return [(i, b[1], b[2], b[3], b[4]) for i, b in enumerate(ride.blocks)
            if b[0] >= WARMUP_S and b[1] is not None and b[2] is not None and b[4] is not None]  # fmt: skip


def _demeaned(groups: Sequence[np.ndarray]) -> np.ndarray:
    return np.concatenate([g - g.mean(axis=0) for g in groups])


def _fit_slopes(rides: Sequence[_Ride]) -> tuple[float, float, bool, int]:
    """Heart rate per W and per 1000 kJ (capped) within rides: ``(slope, drift, fitted, blocks)``."""
    groups = []
    for ride in rides:
        warm = _warm(ride)
        if len(warm) >= MIN_SLOPE_RIDE_BLOCKS:
            groups.append(np.array([(w, min(kj, DRIFT_CAP_KJ) / 1000, bpm) for _, w, bpm, _, kj in warm]))
    count = sum(len(g) for g in groups)
    if count < MIN_FIT_BLOCKS:
        return DEFAULTS["slope"], DEFAULTS["drift"], False, count
    data = _demeaned(groups)
    coef = np.linalg.lstsq(data[:, :2], data[:, 2], rcond=None)[0]
    return float(np.clip(coef[0], *SLOPE_BOUNDS)), float(np.clip(coef[1], *DRIFT_BOUNDS)), True, count


def _neighbour_bases(days: np.ndarray, values: np.ndarray) -> np.ndarray:
    """For each ride, the median of the others within NEIGHBOUR_DAYS (NaN with fewer than
    MIN_NEIGHBOURS)."""
    out = np.full(values.size, np.nan)
    for i, d in enumerate(days):
        lo, hi = np.searchsorted(days, d - NEIGHBOUR_DAYS), np.searchsorted(days, d + NEIGHBOUR_DAYS, side="right")
        others = np.concatenate((values[lo:i], values[i + 1 : hi]))
        if others.size >= MIN_NEIGHBOURS:
            out[i] = np.median(others)
    return out


def fit_corrections(
    days: np.ndarray, raw: np.ndarray, indoor: np.ndarray, heat: np.ndarray, form: np.ndarray
) -> tuple[dict[str, float], dict[str, bool]]:
    """The indoor, heat and form effects on the rides' heart rates (see the module docstring), by
    backfitting against each ride's neighbours; an effect without enough rides keeps its default."""
    names = ("indoor", "heat", "form")
    columns = np.vstack([indoor.astype(float), heat, form]).T
    enough = {"indoor": int(indoor.sum()) >= MIN_EFFECT_RIDES, "heat": int((heat > 0).sum()) >= MIN_EFFECT_RIDES,
              "form": True}  # fmt: skip
    coef = np.array([0.0 if enough[n] else DEFAULTS[n] for n in names])
    free = np.array([enough[n] for n in names])
    if raw.size < MIN_FIT_RIDES:
        return {n: DEFAULTS[n] for n in names}, dict.fromkeys(names, False)
    fitted = False
    for _ in range(BACKFIT_ROUNDS):
        bases = _neighbour_bases(days, raw - columns @ coef)
        ok = np.isfinite(bases)
        if ok.sum() < MIN_FIT_RIDES:
            break
        target = raw[ok] - bases[ok] - columns[ok][:, ~free] @ coef[~free]
        new = np.linalg.lstsq(columns[ok][:, free], target, rcond=None)[0]
        new = np.array([np.clip(v, *CORRECTION_BOUNDS[n]) for v, n in zip(new, np.array(names)[free], strict=True)])
        change = float(np.abs(new - coef[free]).max()) if new.size else 0.0
        coef[free], fitted = new, True
        if change < 1e-3:
            break
    if not fitted:
        return {n: DEFAULTS[n] for n in names}, dict.fromkeys(names, False)
    values = {n: float(v) for n, v in zip(names, coef, strict=True)}
    return values, {n: bool(f) for n, f in zip(names, free, strict=True)}


def _references(rides: Sequence[_Ride], settings: Mapping[str, Any]) -> tuple[float, float, tuple[bool, bool]] | None:
    warm = [(w, bpm) for ride in rides for _, w, bpm, _, _ in _warm(ride)]
    power, bpm = settings.get("reference_power_w"), settings.get("reference_hr_bpm")
    if not warm and (power is None or bpm is None):
        return None
    auto_power = REFERENCE_ROUND_W * round(median(w for w, _ in warm) / REFERENCE_ROUND_W) if warm else None
    auto_bpm = float(round(median(b for _, b in warm))) if warm else None
    return (float(power or auto_power), float(bpm or auto_bpm), (power is None, bpm is None))  # type: ignore[arg-type]


def _ride_value(ride: _Ride, model: Model) -> None:
    """The ride's heart rate at the reference power and work, from its blocks in the band."""
    band = REFERENCE_BAND * model.reference_w
    values, temps, used = [], [], []
    for i, w, bpm, temp, kj in _warm(ride):
        if abs(w - model.reference_w) <= band:
            values.append(bpm - model.slope * (w - model.reference_w)
                          - model.drift * (min(kj, DRIFT_CAP_KJ) - DRIFT_REF_KJ) / 1000)  # fmt: skip
            used.append(i)
            if temp is not None:
                temps.append(temp)
    ride.used = used if len(values) >= MIN_RIDE_BLOCKS else []
    ride.raw_bpm = float(median(values)) if len(values) >= MIN_RIDE_BLOCKS else None
    ride.temp_c = float(np.mean(temps)) if temps and ride.raw_bpm is not None else None


def _ride_drift(ride: _Ride) -> None:
    warm = _warm(ride)
    if len(warm) < MIN_DRIFT_BLOCKS or max(b[4] for b in warm) - min(b[4] for b in warm) < MIN_DRIFT_SPAN_KJ:
        return
    data = np.array([(w, kj / 1000, 1.0, bpm) for _, w, bpm, _, kj in warm])
    ride.drift = float(np.linalg.lstsq(data[:, :3], data[:, 3], rcond=None)[0][1])


def _recovery_events(rides: Sequence[_Ride], conn: sqlite3.Connection) -> list[tuple[int, float, float, float]]:
    """``(ride index, drop, peak / LTHR - 1, recovery power / FTP - RECOVERY_REF_EASE)`` for the
    recoveries that count with the day's thresholds."""
    lthr, ftp = _load_timeline(conn, "threshold_hr"), _load_timeline(conn, "ftp_w")
    out = []
    for k, ride in enumerate(rides):
        lt, ft = lthr.at(ride.date), ftp.at(ride.date)
        if not lt or not ft:
            continue
        for peak, drop, effort, ease in ride.recoveries:
            hard = peak >= RECOVERY_PEAK_OF_LTHR * lt and effort >= RECOVERY_EFFORT_OF_FTP * ft
            if hard and ease <= RECOVERY_EASE_OF_FTP * ft:
                out.append((k, drop, peak / lt - 1, ease / ft - RECOVERY_REF_EASE))
    return out


def _fit_recovery(events: Sequence[tuple[int, float, float, float]]) -> tuple[float, float]:
    if len(events) < MIN_RECOVERY_FIT:
        return RECOVERY_DEFAULTS
    data = np.array([(peak, ease, 1.0, drop) for _, drop, peak, ease in events])
    coef = np.linalg.lstsq(data[:, :3], data[:, 3], rcond=None)[0]
    return (float(np.clip(coef[0], *RECOVERY_BOUNDS[0])), float(np.clip(coef[1], *RECOVERY_BOUNDS[1])))


def build(conn: sqlite3.Connection, settings: Mapping[str, Any] | None = None) -> tuple[Model | None, list[_Ride]]:
    """The fitted model and every ride (de-duplicated, oldest first) with its values."""
    settings = settings or get_settings(conn)
    rides = _load_rides(conn)
    refs = _references(rides, settings)
    if not rides or refs is None:
        return None, rides
    form = _form(conn, rides[0].date)
    for ride in rides:
        ride.tsb = form.get(ride.date, 0.0)
    slope, drift, slopes_fitted, blocks = _fit_slopes(rides)
    model = Model(*refs, slope, drift, {}, {"slope": slopes_fitted}, RECOVERY_DEFAULTS, blocks=blocks)
    for ride in rides:
        _ride_value(ride, model)
        _ride_drift(ride)
    valued = [r for r in rides if r.raw_bpm is not None]
    days = np.array([date.fromisoformat(r.date).toordinal() for r in valued], dtype=float)
    corrections, fitted = fit_corrections(
        days, np.array([r.raw_bpm for r in valued], dtype=float), np.array([r.indoor for r in valued]),
        np.array([r.heat() for r in valued], dtype=float), np.array([r.tsb for r in valued], dtype=float),
    )  # fmt: skip
    model.corrections, model.fitted, model.rides = corrections, {**model.fitted, **fitted}, len(valued)
    for ride in valued:
        ride.bpm = ride.raw_bpm - sum(model.correction(ride).values())  # type: ignore[operator]
    events = _recovery_events(rides, conn)
    model.recovery = _fit_recovery(events)
    model.fitted["recovery"] = len(events) >= MIN_RECOVERY_FIT
    per_ride: dict[int, list[float]] = {}
    for k, drop, peak, ease in events:
        per_ride.setdefault(k, []).append(drop - model.recovery[0] * peak - model.recovery[1] * ease)
    for k, drops in per_ride.items():
        rides[k].recovery, rides[k].recovery_count = float(median(drops)), len(drops)
    return model, rides


# -- the timeline -------------------------------------------------------------------------------------


def _windowed(dates: list[str], values: list[float], day: str, window: int, minimum: int) -> tuple[float | None, int]:
    start = (date.fromisoformat(day) - timedelta(days=window - 1)).isoformat()
    chunk = values[bisect_left(dates, start) : bisect_right(dates, day)]
    return (float(median(chunk)) if len(chunk) >= minimum else None), len(chunk)


def _resting(conn: sqlite3.Connection) -> tuple[list[str], list[float]]:
    by_day: dict[str, list[float]] = {}
    for day, value in conn.execute("SELECT date, value FROM daily_metrics WHERE metric = 'resting_hr' AND value > 0"):
        by_day.setdefault(day, []).append(value)
    days = sorted(by_day)
    return days, [float(median(by_day[d])) for d in days]


def vo2max(model: Model, bpm: float, kg: float | None, max_hr: float | None, resting: float) -> float | None:
    """VO2max (ml/kg/min) from heart rate ``bpm`` at the reference power (see the module docstring)."""
    if not kg or not max_hr or not resting < bpm < max_hr:
        return None
    oxygen = 10.8 * model.reference_w / kg + 7.0  # ml/kg/min at the reference power (ACSM, cycling)
    return 3.5 + (oxygen - 3.5) * (max_hr - resting) / (bpm - resting)


def score(indexes: Mapping[str, float | None], weights: Mapping[str, float]) -> float | None:
    """The weighted geometric mean of the scored components' indexes that have a value; None
    without the aerobic one."""
    if not indexes.get("aerobic"):
        return None
    parts = [(weights[c], indexes[c]) for c in SCORED if indexes.get(c) and weights.get(c)]
    total = sum(w for w, _ in parts)
    return math.exp(sum(w * math.log(i) for w, i in parts) / total) if total else None


def _index(component: str, value: float | None, baseline: float | None) -> float | None:
    if value is None or not baseline or value <= 0:
        return None
    return 100 * (baseline / value if component == "resting_hr" else value / baseline)


def _ride_out(ride: _Ride, model: Model) -> dict[str, Any]:
    return {
        **ride.ref, "indoor": ride.indoor, "temp_c": None if ride.temp_c is None else round(ride.temp_c, 1),
        "tsb": round(ride.tsb, 1), "blocks": len(ride.used),
        "raw_bpm": None if ride.raw_bpm is None else round(ride.raw_bpm, 1),
        "bpm": None if ride.bpm is None else round(ride.bpm, 1),
        "power_w": None if ride.bpm is None else round(model.power_at(ride.bpm), 1),
        "corrections": {k: round(v, 1) + 0.0 for k, v in model.correction(ride).items()},  # never -0.0
        "recovery_bpm": None if ride.recovery is None else round(ride.recovery, 1),
        "drift": None if ride.drift is None else round(ride.drift, 2),
    }  # fmt: skip


def cardio(conn: sqlite3.Connection, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """The cardio index from ``start_date`` to ``end_date`` (default: all of it to today; the end is
    at most today), with the model it rests on and the rides in the range. A day's values do not
    depend on the range."""
    today = date.today().isoformat()
    end = min(end_date or today, today)
    if start_date and start_date > end:
        raise InputError(f"start_date {start_date} is after end_date {end}")
    settings = get_settings(conn)
    weights = settings["weights"]
    model, rides = build(conn, settings)
    rest_dates, rest_values = _resting(conn)
    meta = {
        "weights": weights,
        "settings": settings,
        "window_days": WINDOW_DAYS,
        "components": [
            {"key": c, "label": LABELS[c], "description": DESCRIPTIONS[c], "unit": UNITS[c], "scored": c in SCORED,
             "lower_is_better": c in ("resting_hr", "drift"), "baseline": None}
            for c in COMPONENTS
        ],  # fmt: skip
    }
    if model is None:
        return {**meta, "model": None, "days": [], "rides": []}
    aerobic = [r for r in rides if r.bpm is not None]
    a_dates, a_values = [r.date for r in aerobic], [float(r.bpm) for r in aerobic]  # type: ignore[arg-type]
    recovering = [r for r in rides if r.recovery is not None]
    r_dates, r_values = [r.date for r in recovering], [float(r.recovery) for r in recovering]  # type: ignore[arg-type]
    drifting = [r for r in rides if r.drift is not None]
    d_dates, d_values = [r.date for r in drifting], [float(r.drift) for r in drifting]  # type: ignore[arg-type]
    max_hr, resting_setting = _load_timeline(conn, "max_hr"), _load_timeline(conn, "resting_hr")
    weight = {d["date"]: d for d in trend(daily_weigh_ins(conn))}
    form = _form(conn, rides[0].date)
    first = min([rides[0].date, *rest_dates[:1]])
    raw_days = []
    day = date.fromisoformat(first)
    while day.isoformat() <= end:
        iso = day.isoformat()
        bpm, count = _windowed(a_dates, a_values, iso, WINDOW_DAYS, MIN_RIDES)
        values = {
            "aerobic": None if bpm is None else model.power_at(bpm),
            "resting_hr": _windowed(rest_dates, rest_values, iso, RESTING_WINDOW_DAYS, MIN_RESTING_DAYS)[0],
            "hr_recovery": _windowed(r_dates, r_values, iso, RECOVERY_WINDOW_DAYS, MIN_RECOVERY_RIDES)[0],
            "drift": _windowed(d_dates, d_values, iso, DRIFT_WINDOW_DAYS, MIN_DRIFT_RIDES)[0],
        }
        raw_days.append((iso, bpm, count, values))
        day += timedelta(days=1)
    baselines: dict[str, float | None] = {}
    for c in COMPONENTS:
        seen = [v[c] for _, _, _, v in raw_days if v[c] is not None][:BASELINE_DAYS]
        baselines[c] = float(np.mean(seen)) if seen else None
    days = []
    for iso, bpm, count, values in raw_days:
        if (start_date and iso < start_date) or not any(v is not None for v in values.values()):
            continue
        indexes = {c: _index(c, values[c], baselines[c]) for c in SCORED}
        total = score(indexes, weights)
        measured_rest = values["resting_hr"]
        set_rest = resting_setting.at(iso)
        rest = measured_rest or set_rest or DEFAULT_RESTING_HR
        kg_day = weight.get(iso)
        estimate = vo2max(model, bpm, kg_day["kg"] if kg_day else None, max_hr.at(iso), rest) if bpm else None
        days.append(
            {
                "date": iso,
                "score": None if total is None else round(total, 1),
                "hr_at_ref": None if bpm is None else round(bpm, 1),
                "power_at_ref": None if values["aerobic"] is None else round(values["aerobic"], 1),
                "rides": count,
                "tsb": None if iso not in form else round(form[iso], 1),
                "vo2max": None if estimate is None else round(estimate, 1),
                "vo2max_basis": None if estimate is None else {
                    "resting": "measured" if measured_rest else "setting" if set_rest else "assumed",
                    "resting_hr": round(rest, 1), "max_hr": max_hr.at(iso), "kg": round(kg_day["kg"], 2),
                    "weight_kind": kg_day["kind"],
                },
                "components": {
                    c: {"value": None if values[c] is None else round(values[c], 2),
                        "index": None if c not in SCORED or indexes[c] is None else round(indexes[c], 1)}
                    for c in COMPONENTS
                },
            }
        )  # fmt: skip
    for c in meta["components"]:
        c["baseline"] = None if baselines[c["key"]] is None else round(baselines[c["key"]], 2)
    in_range = [
        _ride_out(r, model) for r in rides
        if (r.bpm is not None or r.recovery is not None) and (not start_date or r.date >= start_date) and r.date <= end
    ]  # fmt: skip
    return {**meta, "model": model.as_dict(), "days": days, "rides": in_range}


# -- one ride ---------------------------------------------------------------------------------------


def ride_cardio(conn: sqlite3.Connection, source: str, source_id: str) -> dict[str, Any] | None:
    """One ride against the rider's recent normal: its steady blocks (which ones count), its heart
    rate at the reference power before and after the corrections, the median of the other rides of
    the ``WINDOW_DAYS`` up to it, and its HR recoveries. None without stored steady riding data."""
    row = conn.execute(
        "SELECT a.local_date, a.activity_type, a.name, a.raw, c.has_gps, c.blocks, c.recoveries "
        "FROM ride_cardio c JOIN activities a USING (source, source_id) WHERE c.source = ? AND c.source_id = ?",
        (source, source_id),
    ).fetchone()
    if row is None:
        return None
    model, rides = build(conn)
    copies = {(c["source"], c["source_id"]) for c in same_workout(conn, source, source_id)} | {(source, source_id)}
    ride = next((r for r in rides if (r.source, r.source_id) in copies), None)
    if ride is None:  # not a cycling ride (or its copy was kept): values from this row alone
        raw = json.loads(row["raw"]) if row["raw"] else {}
        ride = _Ride(source, source_id, row["name"], row["local_date"],
                     is_indoor(row["activity_type"], raw if isinstance(raw, dict) else {}, bool(row["has_gps"])),
                     json.loads(row["blocks"]), json.loads(row["recoveries"]))  # fmt: skip
        if model is not None:
            ride.tsb = _form(conn, ride.date).get(ride.date, 0.0)
            _ride_value(ride, model)
            if ride.raw_bpm is not None:
                ride.bpm = ride.raw_bpm - sum(model.correction(ride).values())
    blocks = [
        {"start_s": b[0], "watts": b[1], "bpm": b[2], "temp_c": b[3], "kj_before": b[4], "used": i in ride.used,
         "warm_up": b[0] < WARMUP_S}
        for i, b in enumerate(ride.blocks)
    ]  # fmt: skip
    if model is None:
        return {"model": None, "blocks": blocks, "ride": None, "normal": None, "recoveries": []}
    others = [r for r in rides if r.bpm is not None and (r.source, r.source_id) not in copies]
    start = (date.fromisoformat(ride.date) - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    window = [float(r.bpm) for r in others if start <= r.date <= ride.date]  # type: ignore[arg-type]
    normal = float(median(window)) if len(window) >= MIN_RIDES else None
    lthr, ftp = _load_timeline(conn, "threshold_hr").at(ride.date), _load_timeline(conn, "ftp_w").at(ride.date)
    recoveries = [
        {"peak_bpm": p, "drop_bpm": d, "effort_w": e, "recovery_w": w,
         "counts": bool(lthr and ftp and p >= RECOVERY_PEAK_OF_LTHR * lthr and e >= RECOVERY_EFFORT_OF_FTP * ftp
                        and w <= RECOVERY_EASE_OF_FTP * ftp)}
        for p, d, e, w in ride.recoveries
    ]  # fmt: skip
    conditions = sum(model.correction(ride).values())
    return {
        "model": model.as_dict(),
        "blocks": blocks,
        "ride": _ride_out(ride, model),
        # The normal at this ride's conditions (indoor, heat, form), to set against its own blocks.
        "normal": None if normal is None else {
            "bpm": round(normal, 1), "power_w": round(model.power_at(normal), 1), "rides": len(window),
            "bpm_at_conditions": round(normal + conditions, 1),
            "difference_bpm": None if ride.bpm is None else round(ride.bpm - normal, 1),
        },
        "recoveries": recoveries,
    }  # fmt: skip


# -- calibration --------------------------------------------------------------------------------------


def backtest(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """How well each set of corrections predicts a ride's heart rate at the reference power from the
    median of the corrected rides of the ``WINDOW_DAYS`` before it (put back to the ride's own
    conditions): ``{corrections, mae_bpm, sd_bpm, rides}`` for none, each alone with its fitted
    value, and all three as fitted (what the index uses)."""
    model, rides = build(conn)
    if model is None:
        return []
    valued = [r for r in rides if r.raw_bpm is not None]
    days = np.array([date.fromisoformat(r.date).toordinal() for r in valued])
    raw = np.array([r.raw_bpm for r in valued], dtype=float)
    columns = np.array([(r.indoor, r.heat(), r.tsb) for r in valued], dtype=float)
    fitted = np.array([model.corrections[n] for n in ("indoor", "heat", "form")])
    variants = {"none": np.zeros(3), "indoor": fitted * [1, 0, 0], "heat": fitted * [0, 1, 0],
                "form": fitted * [0, 0, 1], "all": fitted}  # fmt: skip
    out = []
    for name, coef in variants.items():
        corrected = raw - columns @ coef
        errors = []
        for i, d in enumerate(days):
            lo, hi = np.searchsorted(days, d - WINDOW_DAYS), np.searchsorted(days, d)
            if hi - lo >= MIN_RIDES:
                errors.append(raw[i] - (np.median(corrected[lo:hi]) + columns[i] @ coef))
        e = np.asarray(errors)
        out.append({"corrections": name, "rides": int(e.size),
                    "mae_bpm": round(float(np.abs(e).mean()), 2) if e.size else None,
                    "sd_bpm": round(float(e.std()), 2) if e.size else None})  # fmt: skip
    return out
