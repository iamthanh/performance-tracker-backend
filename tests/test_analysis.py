from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from fitness_hub import queries
from fitness_hub.analysis import (
    ANALYSIS_VERSION,
    HR_ZONES,
    POWER_ZONES,
    Timeline,
    analyze,
    auto_thresholds,
    base_metrics,
    best_average,
    decoupling,
    normalized_power,
    power_curve,
    threshold_metrics,
    thresholds_on,
    timer_series,
    zone_seconds,
)
from fitness_hub.cli import main
from fitness_hub.config import Settings
from fitness_hub.db import Store

# -- the 1 Hz timer clock ----------------------------------------------------------------


def test_timer_series_holds_short_gaps_and_drops_pauses() -> None:
    streams = {"time": [0, 1, 4, 20, 21, 21, 22], "power": [100, 200, 300, 400, None, 999, 500]}
    s = timer_series(streams, ("power", "heart_rate"))["power"]
    # 1 -> 4 is a 3 s gap: 200 held for 3 s. 4 -> 20 is a pause: left out. The second t=21 sample is dropped.
    assert np.array_equal(s, [100, 200, 200, 200, 300, 400, np.nan, 500], equal_nan=True)


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


def test_base_metrics() -> None:
    n = 3600
    streams = {"time": list(range(n)), "power": [None] + [250] * (n - 1), "heart_rate": [150] * n}
    metrics, curve = base_metrics(streams)
    assert metrics["timer_s"] == n
    assert metrics["avg_power_w"] == pytest.approx(250 * (n - 1) / n)  # missing power counts as 0 W
    assert metrics["work_kj"] == pytest.approx(250 * (n - 1) / 1000)
    assert metrics["max_power_w"] == 250
    assert metrics["avg_hr"] == 150 and metrics["hr_20min_bpm"] == 150
    assert metrics["power_hist"] == {"0": 1, "250": n - 1}
    assert curve[1200] == 250
    assert metrics["decoupling_pct"] == pytest.approx(0, abs=0.1)  # the 0 W first second barely shows


def test_base_metrics_without_power_or_hr() -> None:
    metrics, curve = base_metrics({"time": [0, 1, 2], "speed": [5, 5, 5]})
    assert metrics == {"timer_s": 3.0}
    assert curve == {}


# -- threshold-dependent metrics -----------------------------------------------------------


def test_zone_boundaries() -> None:
    # 55 % of 200 W = 110 W is still Z1; 111 W is Z2; 301 W (> 150 %) is Z7.
    seconds = zone_seconds({"110": 1, "111": 2, "300": 3, "301": 4}, 200, POWER_ZONES)
    assert seconds == [1, 2, 0, 0, 0, 3, 4]
    assert zone_seconds({"150": 5}, None, POWER_ZONES) is None
    assert zone_seconds({}, 200, POWER_ZONES) is None
    hr = zone_seconds({"128": 1, "129": 1, "158": 1, "160": 1, "171": 1}, 160, HR_ZONES)
    # 80 % of 160 = 128 is Z1, 129 is Z2, 158 (98.75 %) is Z4, 100 % is Z5a, 171 (> 106 %) is Z5c.
    assert hr == [1, 1, 0, 1, 1, 0, 1]


def test_one_hour_at_threshold_is_100_tss() -> None:
    base = {"timer_s": 3600, "normalized_power_w": 250, "avg_power_w": 240, "avg_hr": 160}
    out = threshold_metrics(base, cycling=True, ftp=250, lthr=165)
    assert out["tss"] == pytest.approx(100) and out["intensity_factor"] == pytest.approx(1)
    assert out["tss_method"] == "power"
    assert out["variability_index"] == pytest.approx(250 / 240)
    assert out["efficiency_factor"] == pytest.approx(250 / 160)
    half = threshold_metrics({**base, "timer_s": 7200, "normalized_power_w": 175}, True, 250, 165)
    assert half["tss"] == pytest.approx(2 * 0.7**2 * 100)


def test_hr_tss_for_non_cycling_or_without_ftp() -> None:
    run = threshold_metrics({"timer_s": 3600, "normalized_power_w": 300, "avg_hr": 165}, False, 250, 165)
    assert run["tss_method"] == "hr" and run["tss"] == pytest.approx(100)
    no_ftp = threshold_metrics({"timer_s": 1800, "normalized_power_w": 200, "avg_hr": 150}, True, None, 150)
    assert no_ftp["tss_method"] == "hr" and no_ftp["tss"] == pytest.approx(50)
    nothing = threshold_metrics({"timer_s": 1800}, True, None, None)
    assert nothing["tss"] is None and nothing["tss_method"] is None


# -- thresholds over time --------------------------------------------------------------------


def test_timeline_resolution() -> None:
    timeline = Timeline.from_rows(
        [
            ("2026-01-01", "auto", 200),
            ("2026-03-01", "auto", 210),
            ("2026-03-01", "manual", 250),
            ("2026-05-01", "auto", 205),
        ]
    )
    assert timeline.at("2025-06-01") == 200  # before the first entry: the first entry
    assert timeline.at("2026-02-15") == 200
    assert timeline.at("2026-03-01") == 250  # manual wins a tie
    assert timeline.at("2026-04-30") == 250
    assert timeline.at("2026-05-01") == 205  # a later entry wins
    assert Timeline().at("2026-01-01") is None


def test_auto_thresholds_use_a_trailing_window() -> None:
    points = [
        ("2026-01-01", 300.0),
        ("2026-01-10", 250.0),
        ("2026-03-31", 260.0),
        ("2026-04-01", 240.0),
        ("2026-04-02", None),
    ]
    # 95 % of the best in the trailing window (both ends included): with 90 days, 2026-01-01 is still in
    # the window on 2026-03-31 and drops out on 2026-04-01, when the best becomes 260 W.
    assert auto_thresholds(points, window_days=90) == [("2026-01-01", 285.0), ("2026-04-01", 247.0)]
    assert auto_thresholds(points) == [("2026-01-01", 285.0)]  # the default year keeps the 300 W
    assert auto_thresholds([]) == []


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
    ride(store, "3", "2026-09-03", 0, activity_type="Run", hr=160)  # no power: HR-based TSS
    result = analyze(store)
    assert result["analysed"] == 3 and result["rescored"] == 3

    # Auto FTP = 95 % of the best 20 minutes so far (250 W) = 238 W; threshold HR = 95 % of the
    # rides' 150 bpm = 142 (the run's 160 bpm does not count).
    assert thresholds_on(store.conn, "2026-09-02") == {"ftp_w": 238.0, "threshold_hr": 142.0}
    first = metrics(store, "1")
    assert first["analysis_version"] == ANALYSIS_VERSION
    assert first["ftp_w"] == 238 and first["tss_method"] == "power"
    assert first["tss"] == pytest.approx(3600 * 250 * (250 / 238) / (238 * 3600) * 100)
    run = metrics(store, "3")
    assert run["tss_method"] == "hr" and run["avg_power_w"] is None
    assert run["threshold_hr"] == 142  # running HR does not set the threshold
    assert store.conn.execute("SELECT COUNT(*) FROM power_curve WHERE source_id = '1'").fetchone()[0] == 23  # 1 s-1 h

    assert analyze(store) == {"analysed": 0, "rescored": 0, "ftp_changes": 1, "threshold_hr_changes": 1}


def test_manual_ftp_rescores_without_reanalysing(store: Store) -> None:
    ride(store, "1", "2026-09-01", 250)
    ride(store, "2", "2026-09-10", 250)
    analyze(store)
    store.set_athlete_setting("ftp_w", "2026-09-05", 250)
    result = analyze(store)
    assert result["analysed"] == 0
    assert result["rescored"] == 1  # only the ride after the manual value
    assert metrics(store, "1")["ftp_w"] == 238 and metrics(store, "2")["ftp_w"] == 250
    assert metrics(store, "2")["tss"] == pytest.approx(100)


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
    analyze(store)  # FTP 226 W after the first ride: each ride scores (238/226)^2 * 100
    tss = (238 / 226) ** 2 * 100
    days = queries.training_load(store.conn, "2026-09-01", "2026-09-05")
    assert [d["date"] for d in days] == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"]
    ctl = atl = 0.0
    for d in days:
        today = tss if d["date"] <= "2026-09-03" else 0.0
        assert d["tsb"] == pytest.approx(round(ctl - atl, 1))  # form going into the day
        ctl += (today - ctl) / 42
        atl += (today - atl) / 7
        assert d["tss"] == pytest.approx(round(today, 1))
        assert d["ctl"] == pytest.approx(round(ctl, 1)) and d["atl"] == pytest.approx(round(atl, 1))
    assert queries.training_load(store.conn, "2026-09-03", "2026-09-03")[0]["ctl"] == days[2]["ctl"]
    assert queries.training_load(store.conn, today=date(2026, 9, 10))[-1]["date"] == "2026-09-10"


def test_training_load_counts_a_ride_once(store: Store) -> None:
    ride(store, "1", "2026-09-01", 238)
    ride(store, "g1", "2026-09-01", 238, source="garmin", start="T16:00:30Z")  # the same ride from Garmin
    analyze(store)
    day = queries.training_load(store.conn, "2026-09-01", "2026-09-01")[0]
    assert day["tss"] == pytest.approx(round(metrics(store, "1")["tss"], 1))
    assert queries.training_load(Store(store.path).conn, "2026-01-01", "2026-01-02") == []


def test_best_power_curve_and_activity_analysis(store: Store) -> None:
    ride(store, "old", "2025-06-01", 300, seconds=1200)
    ride(store, "new", "2026-09-01", 250, seconds=1200)
    analyze(store)
    all_time = {r["duration_s"]: r for r in queries.best_power_curve(store.conn)}
    assert all_time[60]["watts"] == 300 and all_time[60]["source_id"] == "old"
    recent = {r["duration_s"]: r for r in queries.best_power_curve(store.conn, "2026-06-01")}
    assert recent[60]["watts"] == 250 and recent[1200]["source_id"] == "new" and 1800 not in recent

    analysis = queries.activity_analysis(store.conn, "strava", "new")
    assert analysis["ftp_w"] == 238  # the 300 W from June 2025 is outside the 365-day window
    assert queries.activity_analysis(store.conn, "strava", "old")["ftp_w"] == 285
    assert [z["zone"] for z in analysis["power_zones"]] == [name for name, _ in POWER_ZONES]
    assert analysis["power_zones"][-1]["upper"] is None
    assert analysis["power_zones"][4]["seconds"] == 1200  # 250 W is 105.04 % of 238 W: just into Z5
    assert sum(z["seconds"] for z in analysis["power_zones"]) == 1200
    assert analysis["power_curve"][0] == {"duration_s": 1, "watts": 250.0}
    assert queries.activity_analysis(store.conn, "strava", "missing") is None


def test_cli_analyze_and_thresholds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("FITNESS_HUB_HOME", str(settings.home))
    with Store(settings.db_path) as store:
        ride(store, "1", "2026-09-01", 250)
    assert main(["analyze"]) == 0
    assert "1 activities analysed" in capsys.readouterr().out
    assert main(["thresholds", "set", "ftp", "260", "--from", "2026-09-01", "--note", "ramp test"]) == 0
    assert "1 re-scored" in capsys.readouterr().out
    assert main(["thresholds", "list"]) == 0
    out = capsys.readouterr().out
    assert "manual" in out and "260" in out and "ramp test" in out and "auto" in out
    assert main(["thresholds", "delete", "ftp", "--from", "2026-09-01"]) == 0
    assert main(["thresholds", "delete", "ftp", "--from", "2026-09-01"]) == 1
