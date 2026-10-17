from __future__ import annotations

import json
import warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's TestClient warns about httpx; it still works
    from fastapi.testclient import TestClient

from fitness_hub import cardio as C
from fitness_hub.analysis import analyze
from fitness_hub.api import create_app
from fitness_hub.cli import main
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.steady import BLOCK_S, LEAD_S, hr_recoveries, steady_blocks
from fitness_hub.util import InputError

BASE = "http://localhost:8801"


def series(*parts: tuple[int, float]) -> np.ndarray:
    return np.concatenate([np.full(n, float(v)) for n, v in parts])


def no_pause(n: int) -> np.ndarray:
    return np.zeros(n, dtype=bool)


# -- steady blocks and recoveries -------------------------------------------------------------


def test_steady_blocks_need_even_power_after_a_settled_lead() -> None:
    # 10 min at 200 W, 10 min alternating 100/300 W every 30 s, 10 min at 150 W.
    surges = np.tile(np.r_[np.full(30, 100.0), np.full(30, 300.0)], 10)
    power = np.concatenate((np.full(600, 200.0), surges, np.full(600, 150.0)))
    hr = np.full(power.size, 140.0)
    temp = np.full(power.size, 21.0)
    blocks = steady_blocks(power, hr, temp, no_pause(power.size))
    # From LEAD_S into the first part; the surges never qualify, and the last part only once 3 min
    # of it lead into a block (1200 + 180), so no block straddles a change.
    assert [b[0] for b in blocks] == [LEAD_S, 1200 + LEAD_S]
    first = blocks[0]
    assert first[1:4] == [200.0, 140.0, 21.0] and first[4] == pytest.approx(200 * LEAD_S / 1000)


def test_a_pause_breaks_a_block_and_riding_time_leaves_it_out() -> None:
    power = series((900, 200.0))
    is_pause = no_pause(900)
    is_pause[400:460] = True  # a café stop
    power[is_pause] = np.nan
    hr = np.where(is_pause, np.nan, 140.0)
    blocks = steady_blocks(power, hr, np.full(900, np.nan), is_pause)
    assert blocks == []  # 400 s before the stop, 440 s after: neither fits lead + block
    long = np.concatenate((power, np.full(600, 200.0)))
    pauses = np.concatenate((is_pause, no_pause(600)))
    blocks = steady_blocks(long, np.where(pauses, np.nan, 140.0), np.full(long.size, np.nan), pauses)
    # The first try with the whole lead after the stop (460 + 180) on the 30 s search grid is 660 timer
    # seconds, 600 of them riding.
    assert blocks[0][0] == 600 and blocks[0][3] is None


def test_heart_rate_gaps_and_coasting_disqualify() -> None:
    power = series((LEAD_S + BLOCK_S, 200.0))
    hr = np.full(power.size, 140.0)
    hr[LEAD_S : LEAD_S + 30] = np.nan  # 10 % of the block without heart rate
    assert steady_blocks(power, hr, np.full(power.size, 20.0), no_pause(power.size)) == []


def test_hr_recovery_after_a_hard_effort() -> None:
    power = series((600, 150.0), (180, 320.0), (200, 60.0), (300, 150.0))
    hr = series((600, 130.0), (180, 165.0), (200, 0.0), (300, 130.0))
    hr[780:783] = (166.0, 167.0, 168.0)  # heart rate lags: it peaks a few seconds after the effort
    hr[783:980] = 168.0 - 0.4 * np.arange(1, 198)  # then falls 24 bpm in the first minute
    events = hr_recoveries(power, hr, no_pause(power.size))
    assert len(events) == 1
    peak, drop, effort, ease = events[0]
    assert peak == 168.0 and drop == pytest.approx(24.0) and effort == 320.0 and ease == 60.0


def test_no_recovery_when_riding_on_or_paused() -> None:
    power = series((600, 150.0), (180, 320.0), (200, 200.0))  # eases only to 62 %
    hr = series((600, 130.0), (180, 165.0), (200, 150.0))
    assert hr_recoveries(power, hr, no_pause(power.size)) == []
    power = series((600, 150.0), (180, 320.0), (200, 40.0))
    is_pause = no_pause(power.size)
    is_pause[800:830] = True  # stopped: a stop is not a recovery on the bike
    power[is_pause] = np.nan
    assert hr_recoveries(power, series((600, 130.0), (180, 165.0), (200, 140.0)), is_pause) == []


def test_analyze_stores_ride_cardio(store: Store) -> None:
    n = 3600
    store.upsert_activities([{"source": "strava", "source_id": "1", "start_time": "2026-01-01T08:00:00Z",
                              "local_date": "2026-01-01", "activity_type": "Ride", "raw": {}}])  # fmt: skip
    streams = {"time": list(range(n)), "power": [200.0] * n, "heart_rate": [140] * n, "temperature": [18] * n}
    store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": streams}])
    analyze(store)
    row = store.conn.execute("SELECT has_gps, blocks, recoveries FROM ride_cardio").fetchone()
    blocks = json.loads(row["blocks"])
    assert row["has_gps"] == 0 and len(blocks) == (n - LEAD_S) // BLOCK_S and json.loads(row["recoveries"]) == []
    # Without heart rate there is nothing to store, and an older row goes.
    streams.pop("heart_rate")
    store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": streams}])
    analyze(store)
    assert store.conn.execute("SELECT count(*) FROM ride_cardio").fetchone()[0] == 0


def test_indoor_rides() -> None:
    assert C.is_indoor("Ride", {}, has_gps=False)  # no position: on a trainer
    assert C.is_indoor("VirtualRide", {}, has_gps=True) and C.is_indoor("Virtual Ride", {}, has_gps=True)
    assert C.is_indoor("indoor_cycling", {}, has_gps=True)
    assert C.is_indoor("Ride", {"Activity Gear": "Wahoo KICKR"}, has_gps=True)
    assert C.is_indoor("Ride", {"trainer": True}, has_gps=True)
    assert not C.is_indoor("Ride", {"Activity Gear": "2020 BMC Roadmachine"}, has_gps=True)
    assert not C.is_indoor("Ride", {"Activity Gear": "Neon green commuter"}, has_gps=True)


# -- the model ---------------------------------------------------------------------------------


def test_corrections_are_recovered_from_the_rides_around_each_one() -> None:
    rng = np.random.default_rng(1)
    n = 600
    days = np.sort(rng.integers(0, 900, n)).astype(float)
    fitness = 150 - 6 * np.sin(days / 150)  # a slow trend the corrections must not take for an effect
    indoor = rng.random(n) < 0.25
    heat = np.where(indoor, 0.0, np.maximum(rng.normal(22, 6, n) - C.HEAT_FROM_C, 0))
    form = np.clip(rng.normal(0, 15, n), -50, 30)
    raw = fitness - 4.0 * indoor + 0.5 * heat + 0.15 * form + rng.normal(0, 1.0, n)
    found, fitted = C.fit_corrections(days, raw, indoor, heat, form)
    assert found["indoor"] == pytest.approx(-4.0, abs=0.4)
    assert found["heat"] == pytest.approx(0.5, abs=0.08)
    assert found["form"] == pytest.approx(0.15, abs=0.03)
    assert all(fitted.values())
    # Too few rides: the defaults, flagged as not fitted.
    few, fitted = C.fit_corrections(days[:20], raw[:20], indoor[:20], heat[:20], form[:20])
    assert few == {k: C.DEFAULTS[k] for k in ("indoor", "heat", "form")} and not any(fitted.values())


START = date(2024, 1, 1)


def day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def add_ride(store: Store, sid: str, d: str, bpm_at_185: float, *, gps: bool = True, temp: float = 18.0,
             watts: tuple[float, ...] = (165, 175, 185, 195, 205, 185), recoveries: list | None = None,
             slope: float = 0.25, activity_type: str = "Ride") -> None:  # fmt: skip
    """A ride whose steady blocks (from 20 min, one per 5 min) sit on heart rate ``bpm_at_185`` at
    185 W and 500 kJ, ``slope`` bpm per W and 4 bpm per 1000 kJ."""
    store.upsert_activities([{"source": "strava", "source_id": sid, "start_time": d + "T08:00:00Z", "local_date": d,
                              "activity_type": activity_type, "name": f"Ride {sid}", "raw": {}}])  # fmt: skip
    blocks = []
    for k, w in enumerate(watts):
        start, kj = 1200 + 300 * k, 300.0 + 60 * k
        blocks.append([start, w, round(bpm_at_185 + slope * (w - 185) + 4 * (kj - 500) / 1000, 2), temp, kj])
    store.replace_ride_cardio("strava", sid, {"has_gps": gps, "blocks": blocks, "recoveries": recoveries or []})


def test_heart_rate_at_the_reference_power_over_the_window(store: Store) -> None:
    for i in range(100):  # every third day; fitter (lower heart rate) from day 150
        add_ride(store, str(i), day(3 * i), 150.0 if 3 * i < 150 else 144.0)
    result = C.cardio(store.conn, end_date=day(300))
    model = result["model"]
    assert model["reference"] == {"power_w": 185.0, "hr_bpm": 147.0, "power_auto": True, "hr_auto": True}
    assert model["slope_bpm_per_w"] == pytest.approx(0.25, abs=0.01)
    assert model["drift_bpm_per_1000kj"] == pytest.approx(4.0, abs=0.2)
    days = {d["date"]: d for d in result["days"]}
    assert day(10) not in days  # fewer than MIN_RIDES in the window
    assert days[day(100)]["hr_at_ref"] == pytest.approx(150.0, abs=0.1)
    assert days[day(290)]["hr_at_ref"] == pytest.approx(144.0, abs=0.1)
    # The same along the slope: 6 bpm lower is 24 W more at the reference heart rate.
    assert days[day(290)]["power_at_ref"] - days[day(100)]["power_at_ref"] == pytest.approx(24.0, abs=1)
    aerobic = days[day(290)]["components"]["aerobic"]
    assert aerobic["value"] == days[day(290)]["power_at_ref"] and aerobic["index"] > 100
    assert days[day(290)]["score"] == aerobic["index"]  # the only scored component with a value


def test_a_ride_outside_the_power_band_or_in_the_warm_up_does_not_count(store: Store) -> None:
    add_ride(store, "easy", day(0), 150.0, watts=(110, 110, 110))  # 41 % below 185 W
    store.upsert_activities([{"source": "strava", "source_id": "warm", "start_time": day(1) + "T08:00:00Z",
                              "local_date": day(1), "activity_type": "Ride", "raw": {}}])  # fmt: skip
    early = [[300, 185, 140, 18, 50], [600, 185, 140, 18, 100]]  # both in the warm-up
    store.replace_ride_cardio("strava", "warm", {"has_gps": True, "blocks": early, "recoveries": []})
    for i in range(10):
        add_ride(store, str(i), day(2 + i), 150.0)
    rides = {r["source_id"]: r for r in C.cardio(store.conn, end_date=day(20))["rides"]}
    assert "easy" not in rides and "warm" not in rides and rides["0"]["blocks"] == 6


def test_resting_heart_rate_and_recovery_components(store: Store) -> None:
    for i in range(60):
        add_ride(store, str(i), day(i), 148.0)
    for d in range(60):
        store.upsert_daily_metrics([{"source": "garmin", "date": day(d), "metric": "resting_hr",
                                     "value": 50.0 if d < 30 else 45.0}])  # fmt: skip
    result = C.cardio(store.conn, end_date=day(59))
    days = {d["date"]: d for d in result["days"]}
    rest = days[day(59)]["components"]["resting_hr"]
    assert rest["value"] == 45.0
    baseline = next(c["baseline"] for c in result["components"] if c["key"] == "resting_hr")
    assert rest["index"] == pytest.approx(100 * baseline / 45.0, abs=0.1)  # lower is fitter: index above 100
    assert days[day(59)]["vo2max_basis"] is None  # no weight: no VO2max estimate
    assert days[day(59)]["components"]["hr_recovery"]["value"] is None  # no thresholds: nothing counts


def test_vo2max_estimate() -> None:
    model = C.Model(185.0, 146.0, (True, True), 0.25, 4.0, {"indoor": 0, "heat": 0, "form": 0}, {}, (0, 0))
    # 185 W at 75 kg costs 10.8 * 185 / 75 + 7 = 33.64 ml/kg/min; 146 bpm is 96 / 140 of the reserve.
    assert C.vo2max(model, 146.0, 75.0, 190.0, 50.0) == pytest.approx(3.5 + 30.14 * 140 / 96, abs=0.01)
    assert C.vo2max(model, 146.0, None, 190.0, 50.0) is None
    assert C.vo2max(model, 195.0, 75.0, 190.0, 50.0) is None  # above max: no estimate


def test_score_is_a_weighted_geometric_mean() -> None:
    weights = C.DEFAULT_WEIGHTS
    assert C.score({"aerobic": 110.0, "resting_hr": 110.0, "hr_recovery": 110.0}, weights) == pytest.approx(110)
    assert C.score({"aerobic": 100.0, "resting_hr": None, "hr_recovery": 150.0}, weights) == pytest.approx(
        100 * 1.5 ** (15 / 75)
    )
    assert C.score({"aerobic": None, "resting_hr": 120.0, "hr_recovery": 120.0}, weights) is None


def test_settings(store: Store) -> None:
    assert C.get_settings(store.conn) == {"reference_power_w": None, "reference_hr_bpm": None,
                                          "weights": C.DEFAULT_WEIGHTS}  # fmt: skip
    saved = C.set_settings(store.conn, {"aerobic": 70, "resting_hr": 30, "hr_recovery": 0}, 200, None)
    assert saved["reference_power_w"] == 200.0 and saved["weights"]["hr_recovery"] == 0
    with pytest.raises(InputError, match="exactly"):
        C.set_settings(store.conn, {"aerobic": 70})
    with pytest.raises(InputError, match="aerobic"):
        C.set_settings(store.conn, {"aerobic": 0, "resting_hr": 30, "hr_recovery": 0})
    with pytest.raises(InputError, match="reference_power_w"):
        C.set_settings(store.conn, C.DEFAULT_WEIGHTS, 20)
    for i in range(10):
        add_ride(store, str(i), day(i), 150.0)
    model = C.cardio(store.conn, end_date=day(9))["model"]
    assert model["reference"]["power_w"] == 200.0 and not model["reference"]["power_auto"]


def test_one_ride_against_its_recent_normal(store: Store) -> None:
    for i in range(10):
        add_ride(store, str(i), day(i), 150.0)
    add_ride(store, "today", day(10), 146.0, gps=False)  # indoors
    result = C.ride_cardio(store.conn, "strava", "today")
    assert result is not None
    assert [b["used"] for b in result["blocks"]] == [True] * 6
    ride, normal = result["ride"], result["normal"]
    assert ride["indoor"] and ride["raw_bpm"] == pytest.approx(146.0, abs=0.1)
    assert normal["rides"] == 10 and normal["bpm"] == pytest.approx(150.0, abs=0.1)  # the others, not itself
    assert normal["difference_bpm"] == pytest.approx(ride["bpm"] - normal["bpm"], abs=0.11)
    assert C.ride_cardio(store.conn, "strava", "missing") is None


def test_backtest_shapes(store: Store) -> None:
    for i in range(30):
        add_ride(store, str(i), day(i), 150.0 + (i % 3))
    names = [r["corrections"] for r in C.backtest(store.conn)]
    assert names == ["none", "indoor", "heat", "form", "all"]


def test_api(settings: Settings) -> None:
    with Store(settings.db_path) as store:
        for i in range(10):
            add_ride(store, str(i), day(i), 150.0)
    client = TestClient(create_app(settings), base_url=BASE)
    body = client.get("/api/cardio", params={"start_date": day(5)}).json()
    assert body["days"][0]["date"] == day(5) and body["model"]["reference"]["power_w"] == 185.0
    assert client.get("/api/cardio/settings").json()["weights"] == C.DEFAULT_WEIGHTS
    put = client.put("/api/cardio/settings", json={"weights": {"aerobic": 50, "resting_hr": 25, "hr_recovery": 25},
                                                   "reference_power_w": 190, "reference_hr_bpm": None})  # fmt: skip
    assert put.status_code == 200 and put.json()["reference_power_w"] == 190
    assert client.put("/api/cardio/settings", json={"weights": {"aerobic": 50}}).status_code == 400
    assert client.get("/api/activities/strava/3/cardio").json()["ride"]["source_id"] == "3"
    assert client.get("/api/activities/strava/nope/cardio").status_code == 404


def test_cli(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cardio"]) == 0
    assert "No rides with power and heart rate" in capsys.readouterr().out
    with Store(Settings.from_env().db_path) as store:
        for i in range(10):
            add_ride(store, str(i), day(i), 150.0)
    assert main(["cardio"]) == 0
    out = capsys.readouterr().out
    assert "bpm at 185 W" in out and "Corrections: indoors" in out
    assert main(["cardio", "--backtest"]) == 0
    assert "CORRECTIONS" in capsys.readouterr().out


def test_recovery_is_measured_from_the_actual_stop() -> None:
    # Exactly two minutes at 300 W after easier riding: an early trigger would mix the 150 W in.
    power = series((600, 150.0), (120, 300.0), (200, 40.0))
    hr = series((600, 130.0), (120, 160.0), (200, 140.0))
    hr[720:724] = 161.0  # heart rate lags the stop by a few seconds
    peak, _, effort, _ = hr_recoveries(power, hr, no_pause(power.size))[0]
    assert effort == 300.0 and peak == 161.0


def test_blocks_carry_their_time_on_the_streams_clock() -> None:
    power = series((900, 200.0))
    is_pause = no_pause(900)
    clock = np.arange(900, dtype=float) + 1000  # the stream's clock: a 1000 s stop before the timer started
    blocks = steady_blocks(power, np.full(900, 140.0), np.full(900, np.nan), is_pause, clock)
    assert blocks[0][0] == LEAD_S and blocks[0][5] == 1000 + LEAD_S


def test_a_days_values_do_not_depend_on_the_range(store: Store) -> None:
    for i in range(150):
        add_ride(store, str(i), day(2 * i), 150.0 - i / 30)
    full = {d["date"]: d for d in C.cardio(store.conn)["days"]}
    part = {d["date"]: d for d in C.cardio(store.conn, day(60), day(80))["days"]}
    assert part and all(part[k] == full[k] for k in part)
    assert max(part) == day(80) and min(part) == day(60)


def test_a_copy_from_another_service_is_the_same_ride(store: Store) -> None:
    for i in range(10):
        add_ride(store, str(i), day(i), 150.0)
    add_ride(store, "today", day(10), 146.0)
    store.upsert_activities([{"source": "garmin", "source_id": "g", "start_time": day(10) + "T08:00:30Z",
                              "local_date": day(10), "activity_type": "road_biking", "raw": {}}])  # fmt: skip
    store.replace_ride_cardio("garmin", "g", {"has_gps": True, "blocks": [], "recoveries": []})
    for asked in (("strava", "today"), ("garmin", "g")):
        result = C.ride_cardio(store.conn, *asked)
        assert result["normal"]["rides"] == 10  # neither copy is in its own normal
        assert result["ride"]["raw_bpm"] == pytest.approx(146.0, abs=0.1)


def test_a_run_with_power_is_not_put_on_the_cycling_scale(store: Store) -> None:
    for i in range(10):
        add_ride(store, str(i), day(i), 150.0)
    add_ride(store, "run", day(10), 160.0, activity_type="Run")
    result = C.ride_cardio(store.conn, "strava", "run")
    assert result["ride"] is None and len(result["blocks"]) == 6 and not any(b["used"] for b in result["blocks"])


def test_corrections_with_slow_form_and_a_fixed_effect() -> None:
    rng = np.random.default_rng(2)
    n = 700
    days = np.sort(rng.integers(0, 1000, n)).astype(float)
    form = 25 * np.sin(days / 20) + rng.normal(0, 3, n)  # form moves over weeks, as TSB does
    fitness = 150 - 5 * np.sin(days / 200)
    heat = np.where(rng.random(n) < 0.3, rng.uniform(0, 10, n), 0.0)
    raw = fitness + 0.5 * heat + 0.15 * form + rng.normal(0, 1.0, n)
    found, fitted = C.fit_corrections(days, raw, np.zeros(n, dtype=bool), heat, form)
    assert found["form"] == pytest.approx(0.15, abs=0.03) and found["heat"] == pytest.approx(0.5, abs=0.08)
    assert found["indoor"] == C.DEFAULTS["indoor"] and not fitted["indoor"]  # no indoor rides: its default
