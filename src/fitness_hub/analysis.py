"""Training analysis: TrainingPeaks-style metrics from stored activity streams.

Per activity (``activity_metrics``, ``power_curve``):

- The streams are laid on a 1 Hz *timer* clock: a value is held across a gap of
  up to ``MAX_GAP_S`` seconds (smart recording, dropouts); longer gaps are
  pauses and are left out. Missing power counts as 0 W (coasting).
- Threshold-independent values: average/max power, normalized power (30 s
  rolling average, 4th-power mean), work, HR stats, best 20-minute HR, Pw:HR
  decoupling, the mean-maximal power curve, and power/HR histograms.
- Threshold-dependent values, from the stored histograms so a new FTP never
  needs the streams again: intensity factor, TSS, variability index,
  efficiency factor, time in Coggan power zones and Friel HR zones.

TSS is power-based for cycling (``seconds * NP * IF / (FTP * 3600) * 100``)
and an HR estimate otherwise (``hours * (avg HR / threshold HR)^2 * 100``).

Thresholds (``athlete_settings``): ``auto`` rows are recomputed on every run
(FTP = 95 % of the best 20-minute cycling power, threshold HR = 95 % of the best
20-minute cycling heart rate, both over the trailing ``AUTO_WINDOW_DAYS``). The
value for a date is the latest entry on or before it, ``manual`` winning a tie;
before the first entry, the first entry applies.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from bisect import bisect_right
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
NP_WINDOW_S = 30
DECOUPLING_MIN_S = 600
# A year, so the estimate does not sink in blocks without all-out efforts.
AUTO_WINDOW_DAYS = 365
AUTO_FACTOR = 0.95
AUTO_DURATION_S = 1200
POWER_CURVE_DURATIONS = (
    1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 720, 900, 1200,
    1800, 2400, 3600, 5400, 7200, 10800, 14400, 18000,
)  # fmt: skip

# Upper bounds (inclusive) as a fraction of the threshold; the last zone is open-ended.
POWER_ZONES = (
    ("Z1 Active recovery", 0.55),
    ("Z2 Endurance", 0.75),
    ("Z3 Tempo", 0.90),
    ("Z4 Threshold", 1.05),
    ("Z5 VO2max", 1.20),
    ("Z6 Anaerobic", 1.50),
    ("Z7 Neuromuscular", math.inf),
)
HR_ZONES = (
    ("Z1 Recovery", 0.80),
    ("Z2 Aerobic", 0.89),
    ("Z3 Tempo", 0.93),
    ("Z4 Sub-threshold", 0.99),
    ("Z5a Super-threshold", 1.02),
    ("Z5b Aerobic capacity", 1.06),
    ("Z5c Anaerobic capacity", math.inf),
)


# -- per-activity, threshold-independent ---------------------------------------------------


def timer_series(streams: Mapping[str, list[Any]], channels: Iterable[str]) -> dict[str, np.ndarray]:
    """``{channel: 1 Hz float array}`` on the timer clock (NaN where a value is missing)."""
    t = np.asarray(streams.get("time") or [], dtype=float)
    if t.size == 0:
        return {c: np.empty(0) for c in channels}
    order = np.argsort(t, kind="stable")
    sec = np.round(t[order] - t[order][0]).astype(np.int64)
    keep = np.concatenate(([True], np.diff(sec) > 0))  # one sample per second
    sec = sec[keep]
    repeats = np.ones(sec.size, dtype=np.int64)
    gaps = np.diff(sec)
    hold = gaps <= MAX_GAP_S
    repeats[:-1][hold] = gaps[hold]
    index = np.repeat(np.arange(sec.size), repeats)
    out = {}
    for c in channels:
        values = streams.get(c)
        if values is None:
            out[c] = np.full(index.size, np.nan)
            continue
        arr = np.array([np.nan if v is None else v for v in values], dtype=float)[order][keep]
        out[c] = arr[index]
    return out


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    c = np.concatenate(([0.0], np.cumsum(x)))
    return (c[window:] - c[:-window]) / window


def normalized_power(power: np.ndarray) -> float | None:
    if power.size < NP_WINDOW_S:
        return None
    return float(np.mean(_rolling_mean(power, NP_WINDOW_S) ** 4) ** 0.25)


def power_curve(power: np.ndarray, durations: Iterable[int] = POWER_CURVE_DURATIONS) -> dict[int, float]:
    """Best average power for each duration that fits in the activity."""
    c = np.concatenate(([0.0], np.cumsum(power)))
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
    """Pw:HR drift in percent: (first-half ratio - second-half ratio) / first-half ratio."""
    if power.size < DECOUPLING_MIN_S:
        return None
    half = power.size // 2
    ratios = []
    for p, h in ((power[:half], hr[:half]), (power[half:], hr[half:])):
        h = h[~np.isnan(h)]
        if h.size < 0.8 * p.size or h.mean() <= 0:
            return None
        ratios.append(p.mean() / h.mean())
    if ratios[0] <= 0:
        return None
    return float((ratios[0] - ratios[1]) / ratios[0] * 100)


def base_metrics(streams: Mapping[str, list[Any]]) -> tuple[dict[str, Any], dict[int, float]]:
    """Threshold-independent metrics and the power curve of one activity."""
    series = timer_series(streams, ("power", "heart_rate"))
    raw_power, hr = series["power"], series["heart_rate"]
    has_power = bool(np.any(raw_power > 0))
    has_hr = bool(np.any(hr > 0))
    metrics: dict[str, Any] = {"timer_s": float(raw_power.size)}
    curve: dict[int, float] = {}
    if has_power:
        power = np.nan_to_num(raw_power, nan=0.0)
        metrics.update(
            avg_power_w=float(power.mean()),
            max_power_w=float(power.max()),
            normalized_power_w=normalized_power(power),
            work_kj=float(power.sum() / 1000),
            power_hist=_histogram(power),
        )
        curve = power_curve(power)
        if has_hr:
            metrics["decoupling_pct"] = decoupling(power, hr)
    if has_hr:
        valid = hr[~np.isnan(hr)]
        metrics.update(
            avg_hr=float(valid.mean()),
            max_hr=float(valid.max()),
            hr_20min_bpm=best_average(hr, AUTO_DURATION_S),
            hr_hist=_histogram(hr),
        )
    return metrics, curve


# -- threshold-dependent --------------------------------------------------------------


def zone_seconds(hist: Mapping[str, int] | None, threshold: float | None, zones: tuple) -> list[int] | None:
    if not hist or not threshold:
        return None
    bounds = [upper * threshold for _, upper in zones[:-1]]
    seconds = [0] * len(zones)
    for value, count in hist.items():
        seconds[np.searchsorted(bounds, float(value), side="left")] += count
    return seconds


def threshold_metrics(
    base: Mapping[str, Any], cycling: bool, ftp: float | None, lthr: float | None
) -> dict[str, Any]:
    timer_s = base.get("timer_s") or 0.0
    np_w, avg_w, avg_hr = base.get("normalized_power_w"), base.get("avg_power_w"), base.get("avg_hr")
    out: dict[str, Any] = {
        "ftp_w": ftp,
        "threshold_hr": lthr,
        "intensity_factor": None,
        "tss": None,
        "tss_method": None,
        "variability_index": np_w / avg_w if np_w and avg_w else None,
        "efficiency_factor": np_w / avg_hr if np_w and avg_hr else None,
        "power_zone_s": zone_seconds(base.get("power_hist"), ftp, POWER_ZONES),
        "hr_zone_s": zone_seconds(base.get("hr_hist"), lthr, HR_ZONES),
    }
    if cycling and np_w and ftp:
        intensity = np_w / ftp
        out.update(intensity_factor=intensity, tss=timer_s * np_w * intensity / (ftp * 3600) * 100, tss_method="power")
    elif avg_hr and lthr and timer_s:
        intensity = avg_hr / lthr
        out.update(intensity_factor=intensity, tss=timer_s / 3600 * intensity**2 * 100, tss_method="hr")
    return out


# -- thresholds over time ----------------------------------------------------------------


@dataclass
class Timeline:
    """Dated values of one setting, resolved as described in the module docstring."""

    dates: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str, float]]) -> Timeline:
        best: dict[str, tuple[int, float]] = {}
        for effective_date, origin, value in rows:
            rank = 1 if origin == "manual" else 0
            if effective_date not in best or rank > best[effective_date][0]:
                best[effective_date] = (rank, value)
        dates = sorted(best)
        return cls(dates, [best[d][1] for d in dates])

    def at(self, day: str) -> float | None:
        if not self.dates:
            return None
        i = bisect_right(self.dates, day) - 1
        return self.values[max(i, 0)]


def auto_thresholds(
    points: Iterable[tuple[str, float | None]], window_days: int = AUTO_WINDOW_DAYS
) -> list[tuple[str, float]]:
    """``[(local_date, best 20-min value)]`` -> change points of ``95 % x best in the
    trailing window``, evaluated on each activity date (values rounded to whole units)."""
    by_date: dict[str, float] = {}
    for day, value in points:
        if value and value > 0:
            by_date[day] = max(by_date.get(day, 0.0), value)
    days = sorted(by_date)
    changes: list[tuple[str, float]] = []
    for day in days:
        start = (parse_date(day) - timedelta(days=window_days - 1)).isoformat()
        best = max(v for d, v in by_date.items() if start <= d <= day)
        value = float(round(best * AUTO_FACTOR))
        if not changes or changes[-1][1] != value:
            changes.append((day, value))
    return changes


def _load_timeline(conn: sqlite3.Connection, metric: str) -> Timeline:
    rows = conn.execute(
        "SELECT effective_date, origin, value FROM athlete_settings WHERE metric = ?", (metric,)
    ).fetchall()
    return Timeline.from_rows((r[0], r[1], r[2]) for r in rows)


def _replace_auto(store: Store, metric: str, changes: list[tuple[str, float]]) -> None:
    now = now_utc_iso()
    with store.conn:
        store.conn.execute("DELETE FROM athlete_settings WHERE metric = ? AND origin = 'auto'", (metric,))
        store.conn.executemany(
            "INSERT INTO athlete_settings (metric, effective_date, origin, value, note, updated_at) "
            "VALUES (?, ?, 'auto', ?, ?, ?)",
            [(metric, d, v, f"95% of best 20 min, trailing {AUTO_WINDOW_DAYS} days", now) for d, v in changes],
        )


# -- pipeline -------------------------------------------------------------------------------

# New streams, changed streams (another timestamp or size than the ones analysed),
# an older analysis version, or everything with ``full``.
_PENDING_SQL = """
SELECT s.source, s.source_id, s.encoding, s.data, s.updated_at
FROM activity_streams s
LEFT JOIN activity_metrics m USING (source, source_id)
WHERE m.source IS NULL OR m.analysis_version < ? OR m.streams_updated_at IS NOT s.updated_at
   OR m.streams_bytes IS NOT length(s.data) OR ?
ORDER BY s.source, s.source_id
"""


def analyze(store: Store, *, full: bool = False, progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """Bring ``activity_metrics``, ``power_curve`` and the auto thresholds up to date.

    Only activities whose streams are new or changed (or all, with ``full``) are
    read; every activity whose thresholds changed is re-scored from its stored row.
    """
    conn = store.conn
    pending = conn.execute(_PENDING_SQL, (ANALYSIS_VERSION, 1 if full else 0)).fetchall()
    fresh: set[tuple[str, str]] = set()
    batch: list[dict[str, Any]] = []
    for i, (source, source_id, encoding, data, streams_updated_at) in enumerate(pending, start=1):
        metrics, curve = base_metrics(decode_streams(encoding, data))
        # The threshold-dependent columns stay empty until the rescoring pass below.
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

    activities = conn.execute(
        """
        SELECT m.source, m.source_id, a.local_date, a.activity_type, m.hr_20min_bpm, p.watts AS best_20min_w
        FROM activity_metrics m
        JOIN activities a USING (source, source_id)
        LEFT JOIN power_curve p ON p.source = m.source AND p.source_id = m.source_id AND p.duration_s = ?
        """,
        (AUTO_DURATION_S,),
    ).fetchall()
    dates = {(r["source"], r["source_id"]): r["local_date"] for r in activities}
    cycling = {(r["source"], r["source_id"]) for r in activities if sport_family(r["activity_type"]) == "cycling"}
    rides = [r for r in activities if (r["source"], r["source_id"]) in cycling]
    _replace_auto(store, "ftp_w", auto_thresholds((r["local_date"], r["best_20min_w"]) for r in rides))
    _replace_auto(store, "threshold_hr", auto_thresholds((r["local_date"], r["hr_20min_bpm"]) for r in rides))
    ftp, lthr = _load_timeline(conn, "ftp_w"), _load_timeline(conn, "threshold_hr")

    rescored = 0
    for row in conn.execute("SELECT * FROM activity_metrics").fetchall():
        key = (row["source"], row["source_id"])
        if key not in dates:
            continue  # streams without an activity row: no date to look thresholds up for
        want_ftp, want_lthr = ftp.at(dates[key]), lthr.at(dates[key])
        if key not in fresh and row["ftp_w"] == want_ftp and row["threshold_hr"] == want_lthr:
            continue
        base = dict(row)
        for column in ("power_hist", "hr_hist"):
            base[column] = json.loads(base[column]) if base[column] else None
        batch.append({**base, **threshold_metrics(base, key in cycling, want_ftp, want_lthr)})
        rescored += 1
        if len(batch) >= BATCH:
            store.upsert_activity_metrics(batch)
            batch.clear()
    store.upsert_activity_metrics(batch)
    return {
        "analysed": len(fresh),
        "rescored": rescored,
        "ftp_changes": len(ftp.dates),
        "threshold_hr_changes": len(lthr.dates),
    }


def thresholds_on(conn: sqlite3.Connection, day: date | str) -> dict[str, float | None]:
    """FTP and threshold HR in effect on ``day``."""
    iso = day.isoformat() if isinstance(day, date) else parse_date(day).isoformat()
    return {metric: _load_timeline(conn, metric).at(iso) for metric in ("ftp_w", "threshold_hr")}
