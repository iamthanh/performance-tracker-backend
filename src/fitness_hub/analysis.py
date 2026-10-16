"""Training analysis: TrainingPeaks-style metrics from stored activity streams.

Per activity (``activity_metrics``, ``power_curve``):

- The streams are laid on a 1 Hz *timer* clock. A gap of up to ``MAX_GAP_S``
  seconds is filled by holding the previous value (smart recording, dropouts);
  so is a longer gap that the athlete covered at ``MOVING_SPEED_MPS`` or more
  (sparse GPS-watch recording). Other gaps are pauses and are left out. Missing
  power counts as 0 W (coasting); heart rate outside ``MIN_HR_BPM`` to
  ``MAX_HR_BPM`` (a strap that lost contact, sensor spikes) counts as missing.
- Power is only used when at least ``MIN_POWER_SHARE`` of the timer seconds
  have power above 0 W, so a dying power meter does not set the ride's TSS.
- Threshold-independent values: average/max power, normalized power (30 s
  rolling average, 4th-power mean), work, variability index, HR stats, best
  20-minute HR, efficiency factor, Pw:HR decoupling, the mean-maximal power
  curve and power/HR histograms. Power curve and best 20-minute values never
  join efforts across a pause.
- Threshold-dependent values, from the stored histograms so a new threshold
  never needs the streams again: intensity factor, TSS and time in Coggan power
  zones and Friel HR zones.

TSS is power-based for cycling (``seconds * NP * IF / (FTP * 3600) * 100``).
Otherwise it is an HR-based TSS from Banister's TRIMP, scaled so that an hour at
threshold heart rate scores 100; its intensity factor is the one that gives the
same TSS (``sqrt(TSS / (hours * 100))``).

Thresholds (``athlete_settings``): ``auto`` rows are recomputed on every run
from rides over the trailing ``AUTO_WINDOW_DAYS``. FTP is estimated from the
whole power curve (``auto_ftp``): the critical power of the best 3- to
30-minute efforts, never below the best hour, falling by at most
``FTP_DECLINE_PER_WEEK`` so it does not drop off a cliff when a big effort
ages out of the window. Threshold HR = the best 20-minute heart rate, max HR =
the third-highest best-30-second heart rate of any activity. Resting HR has no
auto value (``DEFAULT_RESTING_HR``).
A ``manual`` value holds from its date until the next manual value, unless the
automatic value later rises above it (a new best effort); before the first
value, the first one applies.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from bisect import bisect_left, bisect_right, insort
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from .db import Store, decode_streams
from .queries import sport_family
from .util import now_utc_iso, parse_date

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 1
BATCH = 200
MAX_GAP_S = 5
MOVING_SPEED_MPS = 1.0
MAX_MOVING_GAP_S = 600
MIN_HR_BPM = 30  # below: a strap that lost contact
MAX_HR_BPM = 220  # above: sensor noise (cadence lock, static spikes)
MIN_POWER_SHARE = 0.25
NP_WINDOW_S = 30
DECOUPLING_MIN_S = 600
DECOUPLING_MAX_PCT = 50.0
AUTO_WINDOW_DAYS = 365  # a year, so the estimate does not sink in blocks without all-out efforts
AUTO_WARMUP_DAYS = 28  # the first auto value uses the first four weeks of efforts, not just the first ride
AUTO_DURATION_S = 1200
FTP_FACTOR = 0.95  # FTP from the best 20 minutes, when the efforts do not support a critical-power fit
# Critical power (CP) is fitted to the best efforts between these durations: work = CP * t + W'.
CP_MIN_S = 180
CP_MAX_S = 1800
CP_SHORT_S = 420  # the fit needs an effort this short or shorter ...
CP_LONG_S = 720  # ... and one this long or longer, or it is an extrapolation
FTP_HOUR_S = 3600  # an hour at X W proves an FTP of at least X
FTP_DECLINE_PER_WEEK = 0.01  # at most: fitness fades gradually, it does not vanish when an effort turns 366 days old
MAX_HR_WINDOW_S = 30
MAX_HR_RANK = 3  # the third-highest activity max: one or two noisy sessions do not set it
DEFAULT_RESTING_HR = 50.0
THRESHOLD_OF_MAX_HR = 0.92  # typical; used when max HR is unknown or not above threshold HR
POWER_CURVE_DURATIONS = (
    1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 720, 900, 1200,
    1800, 2400, 3600, 5400, 7200, 10800, 14400, 18000,
)  # fmt: skip


@dataclass(frozen=True)
class Zones:
    """Zone names and boundaries as fractions of a threshold. ``inclusive``: a
    value equal to a boundary belongs to the lower zone (Coggan's "up to 55 %");
    otherwise to the upper one (Friel's "81 % and above")."""

    names: tuple[str, ...]
    bounds: tuple[float, ...]  # len(names) - 1 boundaries between consecutive zones
    inclusive: bool

    def index(self, value: float, threshold: float) -> int:
        side = "left" if self.inclusive else "right"
        return int(np.searchsorted([b * threshold for b in self.bounds], value, side=side))

    def upper_values(self, threshold: float) -> list[int | None]:
        """The highest whole value (W or bpm) in each zone; ``None`` for the open top zone."""
        out: list[int | None] = []
        for b in self.bounds:
            limit = b * threshold
            out.append(math.floor(limit) if self.inclusive else math.ceil(limit) - 1)
        return [*out, None]


POWER_ZONES = Zones(
    ("Z1 Active recovery", "Z2 Endurance", "Z3 Tempo", "Z4 Threshold", "Z5 VO2max", "Z6 Anaerobic",
     "Z7 Neuromuscular"),
    (0.55, 0.75, 0.90, 1.05, 1.20, 1.50),
    inclusive=True,
)  # fmt: skip
HR_ZONES = Zones(
    ("Z1 Recovery", "Z2 Aerobic", "Z3 Tempo", "Z4 Sub-threshold", "Z5a Super-threshold", "Z5b Aerobic capacity",
     "Z5c Anaerobic capacity"),
    (0.81, 0.90, 0.94, 1.00, 1.03, 1.07),
    inclusive=False,
)  # fmt: skip


# -- per-activity, threshold-independent ---------------------------------------------------


def _channel(streams: Mapping[str, list[Any]], name: str) -> np.ndarray | None:
    values = streams.get(name)
    if values is None:
        return None
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def timer_series(
    streams: Mapping[str, list[Any]], channels: Iterable[str], *, keep_pauses: bool = False
) -> dict[str, np.ndarray]:
    """``{channel: 1 Hz float array}`` on the timer clock (NaN where a value is missing).

    With ``keep_pauses`` the paused seconds are kept as NaN instead of being left
    out, so rolling windows cannot join efforts across a stop.
    """
    t = np.asarray(streams.get("time") or [], dtype=float)
    if t.size == 0:
        return {c: np.empty(0) for c in channels}
    order = np.argsort(t, kind="stable")
    sec = np.round(t[order] - t[order][0]).astype(np.int64)
    keep = np.concatenate(([True], np.diff(sec) > 0))  # one sample per second
    sec = sec[keep]
    gaps = np.diff(sec)
    moving = gaps <= MAX_GAP_S
    distance = _channel(streams, "distance")
    if distance is not None:
        travelled = np.diff(distance[order][keep])
        with np.errstate(invalid="ignore"):
            covered = np.nan_to_num(travelled / gaps, nan=0.0) >= MOVING_SPEED_MPS
        moving |= covered & (gaps <= MAX_MOVING_GAP_S)
    held = np.ones(sec.size, dtype=np.int64)
    held[:-1][moving] = gaps[moving]
    paused = np.zeros(sec.size, dtype=np.int64)
    if keep_pauses:
        paused[:-1][~moving] = gaps[~moving] - 1
    # Each sample covers its own second plus held seconds (a copy of it) and paused seconds (NaN).
    span = held + paused
    index = np.repeat(np.arange(sec.size), span)
    offset = np.arange(index.size) - np.repeat(np.cumsum(span) - span, span)  # position within each span
    is_pause = offset >= np.repeat(held, span)
    out = {}
    for c in channels:
        values = _channel(streams, c)
        if values is None:
            out[c] = np.full(index.size, np.nan)
            continue
        series = values[order][keep][index]
        series[is_pause] = np.nan
        out[c] = series
    return out


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    c = np.concatenate(([0.0], np.cumsum(x)))
    return (c[window:] - c[:-window]) / window


def normalized_power(power: np.ndarray) -> float | None:
    if power.size < NP_WINDOW_S:
        return None
    return float(np.mean(_rolling_mean(power, NP_WINDOW_S) ** 4) ** 0.25)


def power_curve(power: np.ndarray, durations: Iterable[int] = POWER_CURVE_DURATIONS) -> dict[int, float]:
    """Best average power for each duration that fits in the activity (NaN counts as 0 W)."""
    c = np.concatenate(([0.0], np.cumsum(np.nan_to_num(power, nan=0.0))))
    return {d: float(np.max(c[d:] - c[:-d]) / d) for d in durations if d <= power.size}


def best_average(values: np.ndarray, window: int, min_coverage: float = 0.9) -> float | None:
    """Best ``window``-second average of a series with gaps (NaN), counting only
    windows where at least ``min_coverage`` of the samples exist."""
    if values.size < window:
        return None
    valid = ~np.isnan(values)
    sums = _rolling_mean(np.where(valid, values, 0.0), window) * window
    counts = _rolling_mean(valid.astype(float), window) * window
    ok = counts >= min_coverage * window
    if not ok.any():
        return None
    return float(np.max(sums[ok] / counts[ok]))


def _histogram(values: np.ndarray) -> dict[str, int]:
    values = values[~np.isnan(values)]
    if values.size == 0:
        return {}
    counts = np.bincount(np.clip(np.round(values), 0, None).astype(np.int64))
    return {str(v): int(n) for v, n in enumerate(counts) if n}


def decoupling(power: np.ndarray, hr: np.ndarray) -> float | None:
    """Pw:HR drift in percent, (first-half ratio - second-half ratio) / first-half ratio,
    over the seconds that have heart rate. ``None`` when it would not mean anything:
    too short, too little HR, nearly no power, or an implausible drift."""
    valid = ~np.isnan(hr)
    if valid.sum() < DECOUPLING_MIN_S or valid.sum() < 0.8 * hr.size:
        return None
    p, h = power[valid], hr[valid]
    half = p.size // 2
    ratios = []
    for part_p, part_h in ((p[:half], h[:half]), (p[half:], h[half:])):
        if np.mean(part_p > 0) < MIN_POWER_SHARE:
            return None
        ratios.append(part_p.mean() / part_h.mean())
    value = (ratios[0] - ratios[1]) / ratios[0] * 100
    return float(value) if abs(value) <= DECOUPLING_MAX_PCT else None


def base_metrics(streams: Mapping[str, list[Any]]) -> tuple[dict[str, Any], dict[int, float]]:
    """Threshold-independent metrics and the power curve of one activity."""
    series = timer_series(streams, ("power", "heart_rate"))
    paused = timer_series(streams, ("power", "heart_rate"), keep_pauses=True)
    for s in (series, paused):
        hr_values = s["heart_rate"]
        with np.errstate(invalid="ignore"):
            hr_values[(hr_values < MIN_HR_BPM) | (hr_values > MAX_HR_BPM)] = np.nan
    raw_power, hr = series["power"], series["heart_rate"]
    has_power = raw_power.size > 0 and float(np.mean(raw_power > 0)) >= MIN_POWER_SHARE
    has_hr = bool(np.any(~np.isnan(hr)))
    metrics: dict[str, Any] = {"timer_s": float(raw_power.size)}
    curve: dict[int, float] = {}
    if has_power:
        power = np.nan_to_num(raw_power, nan=0.0)
        np_w = normalized_power(power)
        avg_w = float(power.mean())
        metrics.update(
            avg_power_w=avg_w,
            max_power_w=float(power.max()),
            normalized_power_w=np_w,
            work_kj=float(power.sum() / 1000),
            variability_index=np_w / avg_w if np_w and avg_w else None,
            power_hist=_histogram(power),
        )
        curve = power_curve(paused["power"])
    if has_hr:
        avg_hr = float(np.nanmean(hr))
        metrics.update(
            avg_hr=avg_hr,
            max_hr=best_average(paused["heart_rate"], MAX_HR_WINDOW_S, min_coverage=1.0),
            hr_20min_bpm=best_average(paused["heart_rate"], AUTO_DURATION_S),
            hr_hist=_histogram(hr),
        )
        if has_power:
            np_w = metrics["normalized_power_w"]
            metrics["efficiency_factor"] = np_w / avg_hr if np_w else None
            metrics["decoupling_pct"] = decoupling(np.nan_to_num(raw_power, nan=0.0), hr)
    return metrics, curve


# -- threshold-dependent --------------------------------------------------------------


def zone_seconds(hist: Mapping[str, int] | None, threshold: float | None, zones: Zones) -> list[int] | None:
    if not hist or not threshold:
        return None
    seconds = [0] * len(zones.names)
    for value, count in hist.items():
        seconds[zones.index(float(value), threshold)] += count
    return seconds


def _trimp_weight(hr: float, resting: float, maximum: float) -> float:
    reserve = min(max((hr - resting) / (maximum - resting), 0.0), 1.0)
    return reserve * 0.64 * math.exp(1.92 * reserve)


def hr_tss(
    hist: Mapping[str, int] | None, timer_s: float, lthr: float | None, max_hr: float | None, resting_hr: float
) -> float | None:
    """TRIMP-based TSS: the activity's TRIMP over an hour at threshold heart rate, x 100."""
    if not hist or not lthr or not timer_s or lthr <= resting_hr:
        return None
    if not max_hr or max_hr <= lthr:
        max_hr = lthr / THRESHOLD_OF_MAX_HR
    total = sum(hist.values())
    per_second = sum(_trimp_weight(float(v), resting_hr, max_hr) * n for v, n in hist.items()) / total
    return timer_s * per_second / (3600 * _trimp_weight(lthr, resting_hr, max_hr)) * 100


@dataclass(frozen=True)
class Thresholds:
    ftp_w: float | None = None
    threshold_hr: float | None = None
    max_hr: float | None = None
    resting_hr: float | None = None


def threshold_metrics(base: Mapping[str, Any], cycling: bool, th: Thresholds) -> dict[str, Any]:
    timer_s = base.get("timer_s") or 0.0
    np_w = base.get("normalized_power_w")
    out: dict[str, Any] = {
        "ftp_w": th.ftp_w,
        "threshold_hr": th.threshold_hr,
        "intensity_factor": None,
        "tss": None,
        "tss_method": None,
        "power_zone_s": zone_seconds(base.get("power_hist"), th.ftp_w, POWER_ZONES),
        "hr_zone_s": zone_seconds(base.get("hr_hist"), th.threshold_hr, HR_ZONES),
    }
    if cycling and np_w and th.ftp_w:
        intensity = np_w / th.ftp_w
        out.update(intensity_factor=intensity, tss=timer_s * np_w * intensity / (th.ftp_w * 3600) * 100,
                   tss_method="power")  # fmt: skip
        return out
    resting = th.resting_hr or DEFAULT_RESTING_HR
    tss = hr_tss(base.get("hr_hist"), timer_s, th.threshold_hr, th.max_hr, resting)
    if tss is not None:
        out.update(intensity_factor=math.sqrt(tss / (timer_s / 3600 * 100)), tss=tss, tss_method="hr")
    return out


# -- thresholds over time ----------------------------------------------------------------


@dataclass
class Timeline:
    """Dated values of one setting, resolved as described in the module docstring."""

    manual: list[tuple[str, float]] = field(default_factory=list)  # sorted by date
    auto: list[tuple[str, float]] = field(default_factory=list)

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str, float]]) -> Timeline:
        manual, auto = [], []
        for effective_date, origin, value in rows:
            (manual if origin == "manual" else auto).append((effective_date, value))
        return cls(sorted(manual), sorted(auto))

    @staticmethod
    def _latest(entries: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
        i = bisect_right(entries, (day, math.inf)) - 1
        return entries[i] if i >= 0 else None

    def at(self, day: str) -> float | None:
        manual, auto = self._latest(self.manual, day), self._latest(self.auto, day)
        if manual is None and auto is None:
            first = min(self.manual[:1] + self.auto[:1], default=None)
            return first[1] if first else None
        if manual is None:
            return auto[1] if auto else None
        # The manual value holds unless an automatic value after it rose above it (a new best).
        surpassed = any(manual[0] < d <= day and v > manual[1] for d, v in self.auto)
        return auto[1] if surpassed and auto else manual[1]


def auto_thresholds(
    points: Iterable[tuple[str, float | None]],
    factor: float = 1.0,
    window_days: int = AUTO_WINDOW_DAYS,
    warmup_days: int = AUTO_WARMUP_DAYS,
    rank: int = 1,
) -> list[tuple[str, float]]:
    """``[(local_date, best value of an activity)]`` -> change points of ``factor x``
    the ``rank``-th highest value in the trailing window (the highest there is if
    fewer), evaluated on each activity date and rounded to whole units. The first
    value is taken once ``warmup_days`` of efforts are in and is dated back to
    the first activity."""
    entries = sorted((day, value) for day, value in points if value and value > 0)
    if not entries:
        return []
    first = entries[0][0]
    warm_until = (parse_date(first) + timedelta(days=warmup_days - 1)).isoformat()

    def pick(values: list[float]) -> float:
        return float(round(values[-min(rank, len(values))] * factor))

    changes = [(first, pick(sorted(v for d, v in entries if d <= warm_until)))]
    window: deque[tuple[str, float]] = deque()
    ordered: list[float] = []  # the window's values, sorted
    for i, (day, value) in enumerate(entries):
        window.append((day, value))
        insort(ordered, value)
        start = (parse_date(day) - timedelta(days=window_days - 1)).isoformat()
        while window[0][0] < start:
            ordered.pop(bisect_left(ordered, window.popleft()[1]))
        if day <= warm_until or (i + 1 < len(entries) and entries[i + 1][0] == day):
            continue  # still warming up, or more activities on the same day
        value = pick(ordered)
        if changes[-1][1] != value:
            changes.append((day, value))
    return changes


def critical_power(best: Mapping[int, float]) -> tuple[float, float] | None:
    """``(CP, W')`` fitted by least squares to work = CP * t + W' over the best efforts of
    ``CP_MIN_S`` to ``CP_MAX_S``; None without both a short and a long effort, or for a fit
    that makes no physiological sense (W' or CP not positive)."""
    points = sorted((t, w) for t, w in best.items() if CP_MIN_S <= t <= CP_MAX_S and w > 0)
    if len(points) < 3 or points[0][0] > CP_SHORT_S or points[-1][0] < CP_LONG_S:
        return None
    t = np.array([p[0] for p in points], dtype=float)
    work = np.array([p[0] * p[1] for p in points])
    cp, w_prime = np.linalg.lstsq(np.vstack([t, np.ones_like(t)]).T, work, rcond=None)[0]
    return (float(cp), float(w_prime)) if cp > 0 and w_prime > 0 else None


def estimate_ftp(best: Mapping[int, float]) -> float | None:
    """FTP from a power-duration envelope (best watts by duration): critical power, or 95 % of the
    best 20 minutes when there is no fit; never below the best hour."""
    fit = critical_power(best)
    base = fit[0] if fit else (best[AUTO_DURATION_S] * FTP_FACTOR if best.get(AUTO_DURATION_S) else None)
    hour = best.get(FTP_HOUR_S)
    return max(v for v in (base, hour) if v) if base or hour else None


FTP_CURVE_DURATIONS = tuple(d for d in POWER_CURVE_DURATIONS if CP_MIN_S <= d <= CP_MAX_S or d == FTP_HOUR_S)


def auto_ftp(
    rides: Iterable[tuple[str, Mapping[int, float]]],
    window_days: int = AUTO_WINDOW_DAYS,
    warmup_days: int = AUTO_WARMUP_DAYS,
    decline_per_week: float = FTP_DECLINE_PER_WEEK,
) -> list[tuple[str, float]]:
    """``[(local_date, {duration_s: best watts})]`` per ride -> FTP change points.

    On each ride date the trailing window's best power per duration is taken and
    ``estimate_ftp`` applied. The FTP rises at once with a better estimate but falls
    by at most ``decline_per_week``. The first value uses ``warmup_days`` of rides and
    is dated back to the first ride (like ``auto_thresholds``)."""
    entries = sorted(((day, curve) for day, curve in rides if curve), key=lambda e: e[0])
    if not entries:
        return []
    first = entries[0][0]
    warm_until = (parse_date(first) + timedelta(days=warmup_days - 1)).isoformat()
    window: deque[tuple[str, Mapping[int, float]]] = deque()
    ordered: dict[int, list[float]] = {d: [] for d in FTP_CURVE_DURATIONS}  # each duration's values, sorted

    def envelope() -> dict[int, float]:
        return {d: values[-1] for d, values in ordered.items() if values}

    changes: list[tuple[str, float]] = []
    current: tuple[str, float] | None = None  # (date, unrounded FTP)
    for i, (day, curve) in enumerate(entries):
        window.append((day, curve))
        for d, w in curve.items():
            if d in ordered:
                insort(ordered[d], w)
        start = (parse_date(day) - timedelta(days=window_days - 1)).isoformat()
        while window[0][0] < start:
            for d, w in window.popleft()[1].items():
                if d in ordered:
                    ordered[d].pop(bisect_left(ordered[d], w))
        following = entries[i + 1][0] if i + 1 < len(entries) else None
        if following is not None and (following == day or following <= warm_until):
            continue  # more rides on the same day, or still warming up
        estimate = estimate_ftp(envelope())
        if estimate is None:
            continue
        if current is None:
            value, dated = estimate, first
        else:
            weeks = (parse_date(day) - parse_date(current[0])).days / 7
            value, dated = max(estimate, current[1] * (1 - decline_per_week * weeks)), day
        current = (day, value)
        if not changes or changes[-1][1] != round(value):
            changes.append((dated, float(round(value))))
    return changes


def _ride_curves(conn: sqlite3.Connection) -> list[tuple[str, dict[int, float]]]:
    """Each ride's best power at the durations ``auto_ftp`` uses."""
    marks = ",".join("?" * len(FTP_CURVE_DURATIONS))
    rows = conn.execute(
        f"""
        SELECT a.source, a.source_id, a.local_date, a.activity_type, p.duration_s, p.watts
        FROM power_curve p JOIN activities a USING (source, source_id)
        WHERE p.duration_s IN ({marks})
        """,
        FTP_CURVE_DURATIONS,
    ).fetchall()
    curves: dict[tuple[str, str], tuple[str, dict[int, float]]] = {}
    for r in rows:
        if sport_family(r["activity_type"]) == "cycling":
            curves.setdefault((r["source"], r["source_id"]), (r["local_date"], {}))[1][r["duration_s"]] = r["watts"]
    return list(curves.values())


def _load_timeline(conn: sqlite3.Connection, metric: str) -> Timeline:
    rows = conn.execute(
        "SELECT effective_date, origin, value FROM athlete_settings WHERE metric = ?", (metric,)
    ).fetchall()
    return Timeline.from_rows((r[0], r[1], r[2]) for r in rows)


def _replace_auto(store: Store, metric: str, changes: list[tuple[str, float]], note: str) -> None:
    now = now_utc_iso()
    with store.conn:
        store.conn.execute("DELETE FROM athlete_settings WHERE metric = ? AND origin = 'auto'", (metric,))
        store.conn.executemany(
            "INSERT INTO athlete_settings (metric, effective_date, origin, value, note, updated_at) "
            "VALUES (?, ?, 'auto', ?, ?, ?)",
            [(metric, d, v, note, now) for d, v in changes],
        )


def thresholds_on(conn: sqlite3.Connection, day: date | str) -> Thresholds:
    """The thresholds in effect on ``day``."""
    iso = day.isoformat() if isinstance(day, date) else parse_date(day).isoformat()
    return Thresholds(**{m: _load_timeline(conn, m).at(iso) for m in ("ftp_w", "threshold_hr", "max_hr", "resting_hr")})


# -- pipeline -------------------------------------------------------------------------------

# New streams, changed streams (another timestamp or size than the ones analysed),
# an older analysis version, or everything with ``full``.
_PENDING_SQL = """
SELECT s.source, s.source_id
FROM activity_streams s
LEFT JOIN activity_metrics m USING (source, source_id)
WHERE m.source IS NULL OR m.analysis_version < ? OR m.streams_updated_at IS NOT s.updated_at
   OR m.streams_bytes IS NOT length(s.data) OR ?
ORDER BY s.source, s.source_id
"""
_METRIC_THRESHOLDS = ("ftp_w", "threshold_hr", "max_hr", "resting_hr")


def analyze(store: Store, *, full: bool = False, progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """Bring ``activity_metrics``, ``power_curve`` and the auto thresholds up to date.

    Only activities whose streams are new or changed (or all, with ``full``) are
    read; every activity whose thresholds, date or sport changed is re-scored
    from its stored row.
    """
    conn = store.conn
    pending = conn.execute(_PENDING_SQL, (ANALYSIS_VERSION, 1 if full else 0)).fetchall()
    fresh: set[tuple[str, str]] = set()
    batch: list[dict[str, Any]] = []
    for i, (source, source_id) in enumerate(pending, start=1):
        encoding, data, streams_updated_at = conn.execute(
            "SELECT encoding, data, updated_at FROM activity_streams WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        metrics, curve = base_metrics(decode_streams(encoding, data))
        # The threshold-dependent columns stay empty until the re-scoring pass below.
        batch.append(
            {
                "source": source,
                "source_id": source_id,
                "analysis_version": ANALYSIS_VERSION,
                "streams_updated_at": streams_updated_at,
                "streams_bytes": len(data),
                **metrics,
            }
        )
        store.replace_power_curve(source, source_id, curve)
        fresh.add((source, source_id))
        if len(batch) >= BATCH:
            store.upsert_activity_metrics(batch)
            batch.clear()
        if progress:
            progress(i, len(pending))
    store.upsert_activity_metrics(batch)
    batch.clear()

    activities = conn.execute(
        """
        SELECT m.source, m.source_id, a.local_date, a.activity_type, m.hr_20min_bpm, m.max_hr
        FROM activity_metrics m
        JOIN activities a USING (source, source_id)
        """
    ).fetchall()
    info = {(r["source"], r["source_id"]): (r["local_date"], sport_family(r["activity_type"])) for r in activities}
    rides = [r for r in activities if sport_family(r["activity_type"]) == "cycling"]
    window = f"trailing {AUTO_WINDOW_DAYS} days"
    _replace_auto(store, "ftp_w", auto_ftp(_ride_curves(conn)),
                  f"critical power of best 3-30 min efforts (at least the best hour), {window}; "
                  f"falls at most {FTP_DECLINE_PER_WEEK:.0%} a week")  # fmt: skip
    _replace_auto(store, "threshold_hr", auto_thresholds((r["local_date"], r["hr_20min_bpm"]) for r in rides),
                  f"best 20 min heart rate, {window}")  # fmt: skip
    _replace_auto(store, "max_hr",
                  auto_thresholds(((r["local_date"], r["max_hr"]) for r in activities), rank=MAX_HR_RANK),
                  f"third-highest best 30 s heart rate, {window}")  # fmt: skip
    timelines = {m: _load_timeline(conn, m) for m in _METRIC_THRESHOLDS}

    rescored = 0
    for row in conn.execute("SELECT * FROM activity_metrics").fetchall():
        key = (row["source"], row["source_id"])
        if key not in info:
            continue  # streams without an activity row: no date to look thresholds up for
        local_date, sport = info[key]
        th = Thresholds(**{m: timelines[m].at(local_date) for m in _METRIC_THRESHOLDS})
        scored_with = (local_date, sport, th.ftp_w, th.threshold_hr, th.max_hr, th.resting_hr)
        if key not in fresh and row["scored_with"] == json.dumps(scored_with):
            continue
        base = dict(row)
        for column in ("power_hist", "hr_hist"):
            base[column] = json.loads(base[column]) if base[column] else None
        update = threshold_metrics(base, sport == "cycling", th)
        batch.append({**base, **update, "scored_with": json.dumps(scored_with)})
        rescored += 1
        if len(batch) >= BATCH:
            store.upsert_activity_metrics(batch)
            batch.clear()
    store.upsert_activity_metrics(batch)
    return {
        "analysed": len(fresh),
        "rescored": rescored,
        "ftp_changes": len(timelines["ftp_w"].manual) + len(timelines["ftp_w"].auto),
        "threshold_hr_changes": len(timelines["threshold_hr"].manual) + len(timelines["threshold_hr"].auto),
    }
