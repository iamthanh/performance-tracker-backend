"""Steady riding and heart-rate recoveries in one workout's streams: what the cardio index (``cardio``)
reads, stored per workout by the analysis (``ride_cardio``) so the index never needs the streams.

- A **steady block** is ``BLOCK_S`` seconds whose 30 s power varies by at most ``MAX_CV`` (coefficient
  of variation) around its mean, with at most ``MAX_COAST_SHARE`` of it coasting, heart rate for
  ``MIN_HR_COVERAGE`` of it, no pause, and each minute of the ``LEAD_S`` before it within
  ``LEAD_TOLERANCE`` of the same power. Heart rate follows a change of power with a time constant
  of about a minute (60 s on the author's 1,582 rides with both, 45-90 s for most), so after three
  of them it shows the block's power rather than what came before. Blocks do not overlap. Each is
  stored with when it starts (riding seconds, pauses left out), its average power, heart rate and
  temperature, and the work done before it.
- An **HR recovery** is a hard effort that stops: ``EFFORT_S`` seconds at ``MIN_EFFORT_W`` or more
  followed by at most ``MAX_RECOVERY_SHARE`` of that power. From the highest heart rate in the
  first ``PEAK_SEARCH_S`` seconds of the recovery, the drop over ``RECOVERY_S`` seconds, with the
  power before and during it. Which of them count (hard enough, and easy enough after) depends on
  the FTP and threshold heart rate of the day, so ``cardio`` decides that when it reads them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

BLOCK_S = 300
LEAD_S = 180
LEAD_CHUNK_S = 60  # each minute of the lead is checked, so a surge just before a block is not averaged away
MAX_CV = 0.15
LEAD_TOLERANCE = 0.15
MAX_COAST_SHARE = 0.1
MIN_HR_COVERAGE = 0.95
MIN_BLOCK_W = 50.0
SMOOTH_S = 30
SEARCH_STEP_S = 30  # where a block does not fit, the next try starts this much later
EFFORT_S = 120
MIN_EFFORT_W = 100.0
MAX_RECOVERY_SHARE = 0.5
PEAK_SEARCH_S = 10
RECOVERY_S = 60


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    c = np.concatenate(([0.0], np.cumsum(x)))
    return (c[window:] - c[:-window]) / window


def _window_mean(x: np.ndarray, window: int) -> np.ndarray:
    """Rolling mean that is NaN for a window holding a NaN (a pause), and only for it."""
    gaps = _rolling_mean(np.isnan(x).astype(float), window)
    means = _rolling_mean(np.nan_to_num(x, nan=0.0), window)
    return np.where(gaps > 0, np.nan, means)


def _round(value: float, digits: int = 1) -> float | None:
    return round(float(value), digits) if np.isfinite(value) else None


def steady_blocks(
    power: np.ndarray, hr: np.ndarray, temperature: np.ndarray, is_pause: np.ndarray
) -> list[list[float | None]]:
    """``[[start_s, watts, bpm, temp_c, kj_before]]``: the steady blocks (see the module docstring)
    of 1 Hz series on the timer clock with pauses kept as NaN (``analysis.timer_clock``). A missing
    power sample (not a pause) counts as 0 W, as everywhere in the analysis."""
    n = power.size
    if n < LEAD_S + BLOCK_S:
        return []
    watts = np.where(is_pause, np.nan, np.nan_to_num(power, nan=0.0))
    riding = np.cumsum(~is_pause) - (~is_pause)  # riding seconds before each second
    work = np.concatenate(([0.0], np.cumsum(np.nan_to_num(watts, nan=0.0)))) / 1000
    smooth = _rolling_mean(np.nan_to_num(watts, nan=0.0), SMOOTH_S)
    has_hr = np.isfinite(hr)
    out: list[list[float | None]] = []
    i = LEAD_S
    while i + BLOCK_S <= n:
        block, lead = watts[i : i + BLOCK_S], watts[i - LEAD_S : i]
        mean = float(block.mean()) if np.isfinite(block).all() and np.isfinite(lead).all() else 0.0
        if (
            mean >= MIN_BLOCK_W
            and has_hr[i : i + BLOCK_S].mean() >= MIN_HR_COVERAGE
            and (block <= 0).mean() <= MAX_COAST_SHARE
            and smooth[i : i + BLOCK_S - SMOOTH_S + 1].std() <= MAX_CV * mean
            and np.abs(lead.reshape(-1, LEAD_CHUNK_S).mean(axis=1) - mean).max() <= LEAD_TOLERANCE * mean
        ):
            temps = temperature[i : i + BLOCK_S]
            out.append([
                int(riding[i]), _round(mean), _round(np.nanmean(hr[i : i + BLOCK_S])),
                _round(np.nanmean(temps)) if np.isfinite(temps).any() else None, _round(work[i]),
            ])  # fmt: skip
            i += BLOCK_S
        else:
            i += SEARCH_STEP_S
    return out


def hr_recoveries(power: np.ndarray, hr: np.ndarray, is_pause: np.ndarray) -> list[list[float]]:
    """``[[peak_bpm, drop_bpm, effort_w, recovery_w]]``: each hard effort that stops and the heart
    rate's fall in the following ``RECOVERY_S`` (see the module docstring), on the same series as
    ``steady_blocks``."""
    n = power.size
    if n < EFFORT_S + PEAK_SEARCH_S + RECOVERY_S + 1:
        return []
    watts = np.where(is_pause, np.nan, np.nan_to_num(power, nan=0.0))
    # Rolling means over windows without a pause (NaN there): effort[k] is the EFFORT_S before
    # second k + EFFORT_S, ease[k] the PEAK_SEARCH_S from second k.
    effort = _window_mean(watts, EFFORT_S)
    ease = _window_mean(watts, PEAK_SEARCH_S)
    starts = np.arange(EFFORT_S, n - PEAK_SEARCH_S - RECOVERY_S)
    with np.errstate(invalid="ignore"):
        before = effort[starts - EFFORT_S]
        candidates = starts[(before >= MIN_EFFORT_W) & (ease[starts] <= MAX_RECOVERY_SHARE * before)]
    out: list[list[float]] = []
    next_free = 0
    for i in candidates:
        if i < next_free or not np.isfinite(hr[i : i + PEAK_SEARCH_S]).any():
            continue
        level = float(effort[i - EFFORT_S])
        peak = i + int(np.nanargmax(hr[i : i + PEAK_SEARCH_S]))
        after, falling = watts[peak : peak + RECOVERY_S], hr[peak : peak + RECOVERY_S + 1]
        if falling.size <= RECOVERY_S or not (np.isfinite(after).all() and np.isfinite(falling).all()):
            continue
        if after.mean() > MAX_RECOVERY_SHARE * level:
            continue
        out.append([float(hr[peak]), round(float(hr[peak] - hr[peak + RECOVERY_S]), 1), round(level, 1),
                    round(float(after.mean()), 1)])  # fmt: skip
        next_free = peak + 2 * RECOVERY_S
    return out


def ride_cardio(
    power: np.ndarray, hr: np.ndarray, temperature: np.ndarray, is_pause: np.ndarray, has_gps: bool
) -> dict[str, Any]:
    """What ``ride_cardio`` stores for a workout with power and heart rate."""
    return {
        "has_gps": has_gps,
        "blocks": steady_blocks(power, hr, temperature, is_pause),
        "recoveries": hr_recoveries(power, hr, is_pause),
    }
