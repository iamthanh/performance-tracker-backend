from __future__ import annotations

import math
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from fitness_hub import queries
from fitness_hub.analysis import (
    ANALYSIS_VERSION,
    HR_ZONES,
    POWER_ZONES,
    Thresholds,
    Timeline,
    analyze,
    auto_ftp,
    auto_thresholds,
    critical_power,
    estimate_ftp,
    base_metrics,
    best_average,
    decoupling,
    hr_tss,
    normalized_power,
    power_curve,
    threshold_metrics,
    thresholds_on,
    timer_series,
    zone_seconds,
)
from fitness_hub.cli import main
from fitness_hub.config import Settings
from fitness_hub.db import SCHEMA, Store


def trimp(hr: float, rest: float = 50, maximum: float = 190) -> float:
    x = min(max((hr - rest) / (maximum - rest), 0.0), 1.0)  # heart-rate reserve, clamped like the code
    return x * 0.64 * math.exp(1.92 * x)


# -- the 1 Hz timer clock ----------------------------------------------------------------


def test_timer_series_holds_short_gaps_and_drops_pauses() -> None:
    streams = {"time": [0, 1, 4, 20, 21, 21, 22], "power": [100, 200, 300, 400, None, 999, 500]}
    s = timer_series(streams, ("power",))["power"]
    # 1 -> 4 is a 3 s gap: 200 held for 3 s. 4 -> 20 is a pause: left out. The second t=21 sample is dropped.
    assert np.array_equal(s, [100, 200, 200, 200, 300, 400, np.nan, 500], equal_nan=True)
    kept = timer_series(streams, ("power",), keep_pauses=True)["power"]
    assert kept.size == 23 and np.isnan(kept[5:20]).all() and kept[20] == 400


def test_timer_series_keeps_sparse_samples_while_moving() -> None:
    # A watch recording every 10 s while running at 3 m/s: moving, not paused. Then a 60 s stop.
    streams = {"time": [0, 10, 20, 80], "distance": [0, 30, 60, 61], "heart_rate": [140, 150, 160, 120]}
    hr = timer_series(streams, ("heart_rate",))["heart_rate"]
    assert hr.size == 22  # 0-9 and 10-19 held, then t=20 and, after the stop, t=80
    assert hr[:10].tolist() == [140] * 10 and hr[10:20].tolist() == [150] * 10 and hr[-2:].tolist() == [160, 120]


def test_timer_series_sorts_and_fills_missing_channels() -> None:
    out = timer_series({"time": [2, 0, 1], "power": [3, 1, 2]}, ("power", "heart_rate"))
    assert out["power"].tolist() == [1, 2, 3]
    assert np.isnan(out["heart_rate"]).all() and out["heart_rate"].size == 3
    assert timer_series({}, ("power",))["power"].size == 0


# -- threshold-independent metrics -------------------------------------------------------


def _np_reference(power: list[float]) -> float:
    rolling = [sum(power[i - 29 : i + 1]) / 30 for i in range(29, len(power))]
    return (sum(r**4 for r in rolling) / len(rolling)) ** 0.25


def test_normalized_power() -> None:
    assert normalized_power(np.full(600, 200.0)) == pytest.approx(200)
    blocks = ([100.0] * 60 + [300.0] * 60) * 5
    value = normalized_power(np.array(blocks))
    assert value == pytest.approx(_np_reference(blocks))
    assert value > 200  # surges cost more than their average
    assert normalized_power(np.full(29, 200.0)) is None


def test_power_curve() -> None:
    power = np.array([100.0] * 100 + [400.0] * 5 + [100.0] * 100)
    curve = power_curve(power, durations=(1, 5, 10, 300))
    assert curve == {1: 400, 5: 400, 10: 250}  # 300 s does not fit in 205 s
    assert power_curve(np.array([300.0, np.nan, 300.0]), durations=(3,)) == {3: 200}  # a pause counts as 0 W


def test_best_average_needs_coverage() -> None:
    hr = np.array([150.0] * 10 + [np.nan] * 10 + [170.0] * 10)
    assert best_average(hr, 10) == 170
    assert best_average(np.array([np.nan] * 5 + [180.0] * 5), 10) is None  # only 50 % covered
    assert best_average(hr, 100) is None


def test_decoupling() -> None:
    power = np.full(1200, 200.0)
    hr = np.concatenate([np.full(600, 140.0), np.full(600, 154.0)])  # HR drifts 10 % at the same power
    assert decoupling(power, hr) == pytest.approx((1 - 1 / 1.1) * 100)
    assert decoupling(power[:500], hr[:500]) is None  # too short
    assert decoupling(power, np.full(1200, np.nan)) is None
    dead_meter = np.concatenate([np.full(600, 150.0), np.zeros(600)])
    assert decoupling(dead_meter, np.full(1200, 140.0)) is None  # the second half has no power
    surge = np.concatenate([np.full(600, 100.0), np.full(600, 300.0)])
    assert decoupling(surge, np.full(1200, 150.0)) is None  # -200 %: not a drift


def test_base_metrics() -> None:
    n = 3600
    streams = {"time": list(range(n)), "power": [None] + [250] * (n - 1), "heart_rate": [150] * n}
    metrics, curve = base_metrics(streams)
    assert metrics["timer_s"] == n
    assert metrics["avg_power_w"] == pytest.approx(250 * (n - 1) / n)  # missing power counts as 0 W
    assert metrics["work_kj"] == pytest.approx(250 * (n - 1) / 1000)
    assert metrics["max_power_w"] == 250
    assert metrics["variability_index"] == pytest.approx(metrics["normalized_power_w"] / metrics["avg_power_w"])
    assert metrics["avg_hr"] == 150 and metrics["hr_20min_bpm"] == 150 and metrics["max_hr"] == 150
    assert metrics["efficiency_factor"] == pytest.approx(metrics["normalized_power_w"] / 150)
    assert metrics["power_hist"] == {"0": 1, "250": n - 1}
    assert curve[1200] == 250
    assert metrics["decoupling_pct"] == pytest.approx(0, abs=0.1)  # the 0 W first second barely shows


def test_base_metrics_ignore_hr_dropouts() -> None:
    streams = {"time": list(range(1200)), "heart_rate": [0] * 590 + [243] * 10 + [150] * 600}
    metrics, _ = base_metrics(streams)
    assert metrics["avg_hr"] == 150 and metrics["max_hr"] == 150  # 0 bpm and a 243 bpm spike are noise
    assert metrics["hr_hist"] == {"150": 600}
    assert metrics["hr_20min_bpm"] is None  # only half of any 20-minute window has heart rate


def test_base_metrics_ignore_a_dead_power_meter() -> None:
    n = 3600
    streams = {"time": list(range(n)), "power": [200] * 300 + [0] * (n - 300), "heart_rate": [140] * n}
    metrics, curve = base_metrics(streams)
    assert "normalized_power_w" not in metrics and curve == {}  # 8 % of the ride has power
    assert metrics["avg_hr"] == 140


def test_power_curve_does_not_join_efforts_across_a_pause() -> None:
    streams = {"time": list(range(600)) + list(range(1200, 1800)), "power": [300] * 1200}
    metrics, curve = base_metrics(streams)
    assert metrics["timer_s"] == 1200
    assert curve[600] == 300
    assert curve[1200] == pytest.approx(150)  # not one 20-minute effort at 300 W


def test_base_metrics_without_power_or_hr() -> None:
    metrics, curve = base_metrics({"time": [0, 1, 2], "speed": [5, 5, 5]})
    assert metrics == {"timer_s": 3.0}
    assert curve == {}


# -- threshold-dependent metrics -----------------------------------------------------------


def test_power_zone_boundaries() -> None:
    # Coggan: up to 55 % of 200 W = 110 W is Z1, 111 W is Z2, 300 W (150 %) is Z6, 301 W is Z7.
    assert zone_seconds({"110": 1, "111": 2, "300": 3, "301": 4}, 200, POWER_ZONES) == [1, 2, 0, 0, 0, 3, 4]
    assert zone_seconds({"150": 5}, None, POWER_ZONES) is None
    assert zone_seconds({}, 200, POWER_ZONES) is None
    assert POWER_ZONES.upper_values(226) == [124, 169, 203, 237, 271, 339, None]  # 169.5 W is already Z3


def test_hr_zone_boundaries() -> None:
    # Friel, threshold 160: Z1 is below 81 % (129.6), Z4 is 94-99 %, Z5a starts at 100 %, Z5c at 107 %.
    seconds = zone_seconds({"129": 1, "130": 1, "158": 1, "160": 1, "171": 1, "172": 1}, 160, HR_ZONES)
    assert seconds == [1, 1, 0, 1, 1, 1, 1]
    assert HR_ZONES.upper_values(170) == [137, 152, 159, 169, 175, 181, None]


def test_one_hour_at_ftp_is_100_tss() -> None:
    base = {"timer_s": 3600, "normalized_power_w": 250, "avg_power_w": 240, "avg_hr": 160}
    out = threshold_metrics(base, cycling=True, th=Thresholds(ftp_w=250, threshold_hr=165))
    assert out["tss"] == pytest.approx(100) and out["intensity_factor"] == pytest.approx(1)
    assert out["tss_method"] == "power"
    half = threshold_metrics({**base, "timer_s": 7200, "normalized_power_w": 175}, True, Thresholds(ftp_w=250))
    assert half["tss"] == pytest.approx(2 * 0.7**2 * 100)


def test_hr_tss_is_trimp_scaled_to_an_hour_at_threshold() -> None:
    th = Thresholds(threshold_hr=165, max_hr=190, resting_hr=50)
    assert hr_tss({"165": 3600}, 3600, 165, 190, 50) == pytest.approx(100)
    easy = hr_tss({"130": 3600}, 7200, 165, 190, 50)  # two hours at 130 bpm
    assert easy == pytest.approx(2 * trimp(130) / trimp(165) * 100)
    assert easy < 2 * (130 / 165) ** 2 * 100  # well below the (HR / threshold)^2 estimate
    run = threshold_metrics({"timer_s": 3600, "normalized_power_w": 300, "hr_hist": {"165": 3600}}, False, th)
    assert run["tss_method"] == "hr" and run["tss"] == pytest.approx(100)
    assert run["intensity_factor"] == pytest.approx(1)  # the IF that gives the same TSS
    # Without a usable max HR, threshold HR is taken as 92 % of max.
    fallback = 3600 * trimp(150, maximum=165 / 0.92) / (3600 * trimp(165, maximum=165 / 0.92)) * 100
    assert hr_tss({"150": 3600}, 3600, 165, None, 50) == pytest.approx(fallback)
    assert hr_tss({"150": 3600}, 3600, 165, 160, 50) == pytest.approx(fallback)
    assert hr_tss({"150": 60}, 60, None, 190, 50) is None
    nothing = threshold_metrics({"timer_s": 1800}, True, Thresholds())
    assert nothing["tss"] is None and nothing["tss_method"] is None


# -- thresholds over time --------------------------------------------------------------------


def test_timeline_manual_values_hold_until_beaten() -> None:
    timeline = Timeline.from_rows(
        [
            ("2026-01-01", "auto", 200),
            ("2026-03-01", "auto", 210),
            ("2026-03-01", "manual", 250),
            ("2026-05-01", "auto", 205),  # an older effort ageing out: the manual value still holds
            ("2026-07-01", "auto", 260),  # a new best above the manual value: auto takes over
            ("2026-09-01", "auto", 240),
        ]
    )
    assert timeline.at("2025-06-01") == 200  # before the first entry: the first entry
    assert timeline.at("2026-02-15") == 200
    assert timeline.at("2026-03-01") == 250
    assert timeline.at("2026-05-01") == 250
    assert timeline.at("2026-07-01") == 260
    assert timeline.at("2026-09-15") == 240
    assert Timeline.from_rows([("2026-03-01", "manual", 250)]).at("2026-01-01") == 250
    assert Timeline().at("2026-01-01") is None


def test_auto_thresholds_use_a_trailing_window() -> None:
    points = [
        ("2025-01-01", 300.0),
        ("2025-03-01", 250.0),
        ("2025-12-31", 260.0),
        ("2026-01-01", 240.0),
        ("2026-01-02", None),
    ]
    # The window includes both ends: 2025-01-01 is in it on 2025-12-31 and drops out on 2026-01-01.
    assert auto_thresholds(points, factor=0.95) == [("2025-01-01", 285.0), ("2026-01-01", 247.0)]
    assert auto_thresholds(points, window_days=90)[-1] == ("2025-12-31", 260.0)  # 300 and 250 aged out
    assert auto_thresholds([]) == []


def test_auto_thresholds_warm_up_before_the_first_value() -> None:
    # The first ride is easy; the first value uses the first four weeks and applies from the first ride.
    points = [("2026-01-01", 150.0), ("2026-01-10", 250.0), ("2026-02-01", 240.0)]
    assert auto_thresholds(points) == [("2026-01-01", 250.0)]
    assert auto_thresholds([("2026-01-01", 150.0)]) == [("2026-01-01", 150.0)]  # all there is


def envelope(cp: float, w_prime: float, durations=(180, 300, 600, 1200, 1800)) -> dict[int, float]:
    return {t: cp + w_prime / t for t in durations}


def test_critical_power_fits_the_best_efforts() -> None:
    cp, w_prime = critical_power(envelope(240, 18000))
    assert cp == pytest.approx(240) and w_prime == pytest.approx(18000)
    assert critical_power({180: 300, 240: 290, 300: 280}) is None  # no long effort: an extrapolation
    assert critical_power({900: 260, 1200: 250, 1800: 240}) is None  # no short effort
    assert critical_power({180: 250, 600: 250, 1200: 250}) is None  # steady power: no W'


def test_estimate_ftp() -> None:
    assert estimate_ftp(envelope(240, 18000)) == pytest.approx(240)
    assert estimate_ftp({**envelope(240, 18000), 3600: 250}) == 250  # an hour at 250 W proves more
    assert estimate_ftp({1200: 260}) == pytest.approx(247)  # no fit: 95 % of the best 20 minutes
    assert estimate_ftp({300: 400}) is None


def test_auto_ftp_falls_gradually_when_efforts_age_out() -> None:
    strong = envelope(260, 20000)
    weak = envelope(220, 20000)
    rides = [("2025-01-01", strong)] + [(f"2025-{m:02d}-01", weak) for m in range(2, 13)]
    rides += [("2026-01-15", weak), ("2026-02-15", weak), ("2026-03-15", weak), ("2026-04-15", weak)]
    changes = dict(auto_ftp(rides))
    assert changes["2025-01-01"] == 260
    assert "2025-12-01" not in changes  # the strong ride is still in the trailing year
    # 2026-01-15: the strong ride has aged out (estimate 220), but FTP falls at most 1 % a week.
    weeks = (date(2026, 1, 15) - date(2025, 12, 1)).days / 7
    assert changes["2026-01-15"] == round(260 * (1 - 0.01 * weeks))
    assert changes["2026-03-15"] == 223 and changes["2026-04-15"] == 220  # it reaches the estimate in the end
    assert auto_ftp([("2026-01-01", strong), ("2026-01-20", envelope(300, 20000))]) == [
        ("2026-01-01", 300.0)  # within the warm-up: the best of the first four weeks, dated to the first ride
    ]
    assert dict(auto_ftp([("2026-01-01", strong), ("2026-03-01", envelope(300, 20000))]))["2026-03-01"] == 300  # rises at once
    assert auto_ftp([]) == []
    # Several rides on one day: the day's best counts.
    assert auto_ftp([("2026-01-01", weak), ("2026-01-01", strong)]) == [("2026-01-01", 260.0)]


def test_auto_thresholds_by_rank_ignore_outliers() -> None:
    maxima = [("2026-01-01", 185.0), ("2026-01-02", 186.0), ("2026-01-03", 184.0), ("2026-02-10", 218.0),
              ("2026-02-11", 210.0), ("2026-02-12", 183.0)]  # fmt: skip
    # Third-highest: the two noisy sessions (218, 210) do not set max HR; one or two activities use the highest.
    assert auto_thresholds(maxima, rank=3) == [("2026-01-01", 184.0), ("2026-02-10", 185.0), ("2026-02-11", 186.0)]
    assert auto_thresholds(maxima[:2], rank=3) == [("2026-01-01", 185.0)]


# -- pipeline ------------------------------------------------------------------------------


def ride(store: Store, source_id: str, day: str, watts: float, seconds: int = 3600, hr: float = 150,
         activity_type: str = "Ride", source: str = "strava", start: str = "T16:00:00Z") -> None:  # fmt: skip
    store.upsert_activities(
        [{"source": source, "source_id": source_id, "start_time": day + start, "local_date": day,
          "activity_type": activity_type, "raw": {}}]  # fmt: skip
    )
    streams = {"time": list(range(seconds)), "power": [watts] * seconds, "heart_rate": [hr] * seconds}
    store.upsert_activity_streams([{"source": source, "source_id": source_id, "streams": streams}])


def metrics(store: Store, source_id: str) -> dict:
    return dict(store.conn.execute("SELECT * FROM activity_metrics WHERE source_id = ?", (source_id,)).fetchone())


def test_analyze_end_to_end(store: Store) -> None:
    ride(store, "1", "2026-09-01", 250)
    ride(store, "2", "2026-09-02", 200)
    ride(store, "3", "2026-09-03", 0, activity_type="Run", hr=170)  # no power: HR-based TSS
    result = analyze(store)
    assert result["analysed"] == 3 and result["rescored"] == 3

    # FTP = 250 W: an hour at 250 W proves it (steady power gives no critical-power fit); threshold HR = the
    # rides' best 20-minute HR (the run's 170 does not count); max HR = the third-highest activity max.
    assert thresholds_on(store.conn, "2026-09-02") == Thresholds(ftp_w=250, threshold_hr=150, max_hr=150)
    first = metrics(store, "1")
    assert first["analysis_version"] == ANALYSIS_VERSION
    assert first["ftp_w"] == 250 and first["tss_method"] == "power"
    assert first["tss"] == pytest.approx(100)  # an hour at FTP
    run = metrics(store, "3")
    assert run["tss_method"] == "hr" and run["avg_power_w"] is None and run["threshold_hr"] == 150
    max_hr = 150 / 0.92  # max HR is not above threshold HR: the fallback applies
    assert run["tss"] == pytest.approx(trimp(170, maximum=max_hr) / trimp(150, maximum=max_hr) * 100)
    assert store.conn.execute("SELECT COUNT(*) FROM power_curve WHERE source_id = '1'").fetchone()[0] == 23  # 1 s-1 h

    assert analyze(store) == {"analysed": 0, "rescored": 0, "ftp_changes": 1, "threshold_hr_changes": 1}


def test_manual_ftp_rescores_without_reanalysing(store: Store) -> None:
    ride(store, "1", "2026-09-01", 250)
    ride(store, "2", "2026-09-10", 250)
    analyze(store)
    store.set_athlete_setting("ftp_w", "2026-09-05", 260)
    result = analyze(store)
    assert result["analysed"] == 0
    assert result["rescored"] == 1  # only the ride after the manual value
    assert metrics(store, "1")["ftp_w"] == 250 and metrics(store, "2")["ftp_w"] == 260
    assert metrics(store, "2")["tss"] == pytest.approx((250 / 260) ** 2 * 100)


def test_type_or_date_changes_are_rescored(store: Store) -> None:
    ride(store, "1", "2026-09-01", 250, activity_type="Workout", hr=170)
    ride(store, "2", "2026-09-01", 250)
    analyze(store)
    assert metrics(store, "1")["tss_method"] == "hr"
    store.conn.execute("UPDATE activities SET activity_type = 'Ride' WHERE source_id = '1'")
    store.conn.commit()
    # Re-scored as a ride; as a ride its 170 bpm also raises the threshold HR, so ride 2 is re-scored too.
    assert analyze(store)["rescored"] == 2
    assert metrics(store, "1")["tss_method"] == "power"
    assert metrics(store, "2")["threshold_hr"] == 170


def test_changed_streams_are_reanalysed(store: Store) -> None:
    ride(store, "1", "2026-09-01", 250)
    analyze(store)
    # A --force re-import within the same second: the timestamp may not change, the size does.
    ride(store, "1", "2026-09-01", 99)
    assert analyze(store)["analysed"] == 1
    assert metrics(store, "1")["avg_power_w"] == 99
    assert analyze(store)["analysed"] == 0
    store.conn.execute("UPDATE activity_streams SET updated_at = '2030-01-01T00:00:00Z'")  # a newer import
    store.conn.commit()
    assert analyze(store)["analysed"] == 1
    assert analyze(store, full=True)["analysed"] == 1


def test_training_load(store: Store) -> None:
    for i in range(1, 4):
        ride(store, str(i), f"2026-09-0{i}", 238, seconds=3600)
    analyze(store)  # an hour at 238 W: FTP 238 W, each ride scores 100
    tss = 100.0
    days = queries.training_load(store.conn, "2026-08-31", "2026-09-05")
    assert days[0]["date"] == "2026-08-31"  # the range starts where asked, before the first ride
    ctl = atl = 0.0
    for d in days:
        today = tss if "2026-09-01" <= d["date"] <= "2026-09-03" else 0.0
        assert d["tsb"] == pytest.approx(round(ctl - atl, 1))  # form going into the day
        ctl += (today - ctl) / 42
        atl += (today - atl) / 7
        assert d["tss"] == pytest.approx(round(today, 1))
        assert d["ctl"] == pytest.approx(round(ctl, 1)) and d["atl"] == pytest.approx(round(atl, 1))
        assert d["unscored"] == 0
    assert queries.training_load(store.conn, "2026-09-03", "2026-09-03")[0]["ctl"] == days[3]["ctl"]
    assert queries.training_load(store.conn, today=date(2026, 9, 10))[-1]["date"] == "2026-09-10"


def test_training_load_counts_a_ride_once_and_reports_unscored(store: Store, tmp_path: Path) -> None:
    ride(store, "1", "2026-09-01", 238)
    analyze(store)
    # The same ride synced from Garmin (the preferred source, but without streams) and a run without streams.
    store.upsert_activities(
        [
            {"source": "garmin", "source_id": "g1", "start_time": "2026-09-01T16:00:30Z", "local_date": "2026-09-01",
             "activity_type": "road_biking", "raw": {}},
            {"source": "garmin", "source_id": "g2", "start_time": "2026-09-02T07:00:00Z", "local_date": "2026-09-02",
             "activity_type": "running", "raw": {}},
        ]
    )  # fmt: skip
    days = queries.training_load(store.conn, "2026-09-01", "2026-09-02")
    assert days[0]["tss"] == pytest.approx(round(metrics(store, "1")["tss"], 1)) and days[0]["unscored"] == 0
    assert days[1]["tss"] == 0 and days[1]["unscored"] == 1
    with Store(tmp_path / "empty.db") as empty:
        assert queries.training_load(empty.conn, "2026-01-01", "2026-01-02") == []


def test_best_power_curve_and_activity_analysis(store: Store) -> None:
    ride(store, "old", "2025-06-01", 300, seconds=1200)
    ride(store, "new", "2026-09-01", 250, seconds=1200)
    ride(store, "run", "2026-09-02", 400, seconds=600, activity_type="Run")
    analyze(store)
    all_time = {r["duration_s"]: r for r in queries.best_power_curve(store.conn)}
    assert all_time[60]["watts"] == 300 and all_time[60]["source_id"] == "old"  # running power is left out
    assert {r["duration_s"]: r for r in queries.best_power_curve(store.conn, activity_type=None)}[60]["watts"] == 400
    recent = {r["duration_s"]: r for r in queries.best_power_curve(store.conn, "2026-06-01")}
    assert recent[60]["watts"] == 250 and recent[1200]["source_id"] == "new" and 1800 not in recent

    analysis = queries.activity_analysis(store.conn, "strava", "new")
    assert analysis["ftp_w"] == 238  # the 300 W from June 2025 is outside the 365-day window
    assert queries.activity_analysis(store.conn, "strava", "old")["ftp_w"] == 285
    assert [z["zone"] for z in analysis["power_zones"]] == list(POWER_ZONES.names)
    assert analysis["power_zones"][3]["upper"] == 249 and analysis["power_zones"][-1]["upper"] is None
    assert analysis["power_zones"][4]["seconds"] == 1200  # 250 W is 105.04 % of 238 W: just into Z5
    assert analysis["power_curve"][0] == {"duration_s": 1, "watts": 250.0}
    assert queries.activity_analysis(store.conn, "strava", "missing") is None


def test_v1_database_with_data_migrates_and_analyses(tmp_path: Path) -> None:
    path = tmp_path / "fitness.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO activities (source, source_id, start_time, local_date, activity_type, raw, updated_at) "
        "VALUES ('strava', '1', '2026-09-01T16:00:00Z', '2026-09-01', 'Ride', '{}', '2026-09-01T18:00:00Z')"
    )
    conn.commit()
    conn.close()
    with Store(path) as store:  # v1 -> v3, keeping the row
        assert store.row_counts()["activities"] == 1
        ride(store, "1", "2026-09-01", 250)
        assert analyze(store)["analysed"] == 1
        assert metrics(store, "1")["tss_method"] == "power"


def test_threshold_values_are_validated(store: Store) -> None:
    for bad in (0, -5, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="positive number"):
            store.set_athlete_setting("ftp_w", "2026-09-01", bad)


def test_cli_analyze_and_thresholds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("FITNESS_HUB_HOME", str(settings.home))
    with Store(settings.db_path) as store:
        ride(store, "1", "2026-09-01", 250)
    assert main(["analyze"]) == 0
    assert "1 activities analysed" in capsys.readouterr().out
    assert main(["thresholds", "set", "ftp", "252.5", "--from", "2026-09-01", "--note", "ramp test"]) == 0
    assert "1 re-scored" in capsys.readouterr().out
    assert main(["thresholds", "set", "resting-hr", "48", "--from", "2026-01-01"]) == 0
    assert main(["thresholds", "list"]) == 0
    out = capsys.readouterr().out
    assert "manual" in out and "252.5" in out and "ramp test" in out and "auto" in out and "resting_hr" in out
    assert main(["thresholds", "set", "ftp", "0", "--from", "2026-09-01"]) == 1
    assert "positive number" in capsys.readouterr().err
    assert main(["thresholds", "delete", "ftp", "--from", "2026-09-01"]) == 0
    assert main(["thresholds", "delete", "ftp", "--from", "2026-09-01"]) == 1
    assert main(["thresholds"]) == 0  # no action: list
