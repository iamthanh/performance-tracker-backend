from __future__ import annotations

import warnings

import numpy as np
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's TestClient warns about httpx; it still works
    from fastapi.testclient import TestClient

from fitness_hub import nutrition
from fitness_hub.analysis import analyze, timer_clock
from fitness_hub.api import create_app
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.insights import (
    DEFAULT_W_PRIME_J,
    cp_model,
    durability,
    effort_spans,
    matches,
    ride_weight,
    w_balance,
)

BASE = "http://localhost:8801"


def intervals(reps: int = 3, on: int = 60, off: int = 120, hard: float = 350, easy: float = 150) -> list[float]:
    return ([easy] * 600 + ([hard] * on + [easy] * off) * reps) + [easy] * 600


def test_durability_in_100_kj_steps() -> None:
    power = np.full(3600, 300.0)  # 1080 kJ
    result = durability(power)
    assert result is not None and result["unit"] == "kJ" and result["total"] == 1080
    assert result["steps"] == list(range(0, 1001, 100))  # 1000 kJ leaves 80 kJ, more than half a step
    rows = {r["duration_s"]: r["watts"] for r in result["rows"]}
    # 700 kJ is done after 2334 s: 1266 s are left, enough for 20 minutes; 800 kJ leaves too few.
    assert rows[60] == [300] * 11 and rows[1200] == [300] * 8 + [None] * 3
    assert durability(np.full(3400, 300.0))["steps"][-1] == 900  # 1020 kJ: 1000 leaves under half a step
    assert durability(np.full(3000, 1000 / 3))["steps"][-1] == 900  # exactly 1000 kJ from float watts
    assert durability(np.full(30, 200.0)) is None


def test_durability_shows_the_fade_and_coarsens_very_long_rides() -> None:
    power = np.concatenate([np.full(1800, 400.0), np.full(9000, 250.0)])  # hard start, then steady
    five = next(r["watts"] for r in durability(power)["rows"] if r["duration_s"] == 300)
    assert five[:7] == [400] * 7 and five[8:20] == [250] * 12  # the 720 kJ start is over by 800 kJ
    long = durability(np.full(12 * 3600, 300.0))  # 12960 kJ: over 100 steps of 100 kJ
    assert long["steps"][1] == 200 and len(long["steps"]) <= 101


def test_w_balance_spends_above_cp_and_recovers_below() -> None:
    power = np.array([400.0] * 60 + [100.0] * 600)
    balance = w_balance(power, cp=300, w_prime=20000)
    assert balance[59] == pytest.approx(14000)  # 60 s at 100 W over CP
    assert balance[60] == pytest.approx(14000 + 6000 * 200 / 20000)  # recovers part of what is missing
    assert 14000 < balance[-1] < 20000 and np.all(np.diff(balance[60:]) > 0)


def test_matches_are_drops_of_2kj_or_more() -> None:
    power = np.array(intervals(reps=3, on=30, hard=400) + [320.0] * 10, dtype=float)  # 3 kJ each; a tiny last dig
    balance = w_balance(power, cp=300, w_prime=20000)
    found = matches(balance, 20000)
    assert len(found) == 3
    assert all(balance[h] - balance[lo] == pytest.approx(3000, abs=50) for _, h, lo in found)
    assert [start for start, _, _ in found] == [601, 751, 901]  # the second second of each dig


def test_a_match_ends_when_w_prime_stops_falling() -> None:
    # A hard minute, then a long slow drain just above CP: one match, not one that lasts the whole ride.
    power = np.array([150.0] * 60 + [400.0] * 60 + [302.0] * 3600, dtype=float)
    balance = w_balance(power, cp=300, w_prime=20000)
    (start, high, low), *rest = matches(balance, 20000)
    assert start == 61 and low - high < 400 and rest == []


def test_efforts_above_ftp() -> None:
    power = np.array(intervals(), dtype=float)
    power[630] = 200  # a dip inside the first interval does not split it
    paused = np.zeros(power.size, dtype=bool)
    spans = effort_spans(power, paused, ftp=250)
    assert spans == [(600, 660), (780, 840), (960, 1020)]
    paused[630:640] = True  # a stop in the middle splits the first one
    power[630:640] = 0
    assert effort_spans(power, paused, ftp=250)[:2] == [(600, 630), (640, 660)]
    assert effort_spans(np.full(600, 200.0), np.zeros(600, dtype=bool), ftp=250) == []


def test_effort_edges_and_short_surges() -> None:
    # Just above FTP between easy blocks: the whole effort, to the second (FTP 250 W).
    power = np.array([150.0] * 300 + [260.0] * 60 + [150.0] * 300 + [262.0] * 35 + [150.0] * 300)
    paused = np.zeros(power.size, dtype=bool)
    assert effort_spans(power, paused, ftp=250) == [(300, 360), (660, 695)]
    # A 6 s kick at 128 % of FTP then coasting counts; 6 s at 110 % does not.
    # A 6 s dip to 90 % of FTP inside a 5-minute effort at 101 % does not split it; a 10 s soft-pedal (50 %) does.
    steady = np.array([150.0] * 60 + [252.0] * 150 + [225.0] * 6 + [252.0] * 144 + [125.0] * 10 + [252.0] * 60)
    assert effort_spans(steady, np.zeros(steady.size, dtype=bool), ftp=250) == [(60, 360), (370, 430)]
    kick = np.array([150.0] * 100 + [320.0] * 6 + [0.0] * 100 + [275.0] * 6 + [0.0] * 100)
    assert effort_spans(kick, np.zeros(kick.size, dtype=bool), ftp=250) == [(100, 106)]


def test_cp_model_pairs_cp_and_w_prime_from_the_fit(settings: Settings) -> None:
    # Best efforts of a rider with CP 300 W and W' 20 kJ; the ride's FTP (280 W) does not change the pair.
    curve = {t: 300 + 20000 / t for t in (180, 300, 420, 600, 1200, 1800)}
    with Store(settings.db_path) as store:
        ride(store, "cp", "2026-10-01", [200.0] * 60)
        store.replace_power_curve("strava", "cp", curve)
        cp, w_prime, origin = cp_model(store.conn, "2026-10-01", 280)
        assert origin == "fit" and cp == pytest.approx(300) and w_prime == pytest.approx(20000)
        assert cp_model(store.conn, "2026-09-01", 280) == (280, DEFAULT_W_PRIME_J, "ftp")  # before any ride


def test_timer_clock_places_seconds_on_the_stream_clock() -> None:
    streams = {"time": [0, 1, 2, 60, 61], "power": [100, 100, 100, 200, 200]}  # a 58 s stop
    clock, paused, series = timer_clock(streams, ("power",))
    assert clock.tolist() == list(range(62)) and paused.sum() == 57
    assert np.isnan(series["power"][10]) and series["power"][60] == 200


# -- API ---------------------------------------------------------------------------------------


def ride(store: Store, source_id: str, day: str, power: list[float], activity_type: str = "Ride") -> None:
    store.upsert_activities(
        [{"source": "strava", "source_id": source_id, "start_time": day + "T16:00:00Z", "local_date": day,
          "activity_type": activity_type, "moving_s": float(len(power)), "raw": {}}]  # fmt: skip
    )
    streams = {"time": list(range(len(power))), "power": power, "heart_rate": [150] * len(power)}
    store.upsert_activity_streams([{"source": "strava", "source_id": source_id, "streams": streams}])


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with Store(settings.db_path) as store:
        ride(store, "1", "2026-10-01", [250.0] * 3600)
        ride(store, "2", "2026-10-03", intervals(reps=5, hard=400))
        ride(store, "run", "2026-10-04", [250.0] * 1800, activity_type="Run")
        store.conn.execute(
            "INSERT INTO body_measurements (source, ts, metric, value, updated_at) "
            "VALUES ('withings', '2026-09-20T07:00:00Z', 'weight_kg', 72.5, 'x')"
        )
        store.conn.commit()
        analyze(store)
    return TestClient(create_app(settings), base_url=BASE)


def test_insights_for_a_ride(client: TestClient) -> None:
    body = client.get("/api/activities/strava/2/insights").json()
    assert body["weight"] == {"kg": 72.5, "origin": "body", "date": "2026-09-20"}
    assert body["durability"]["unit"] == "kJ" and body["durability"]["steps"][0] == 0
    efforts = body["efforts"]
    assert efforts["count"] == 5 and efforts["items"][0]["duration_s"] == 60
    first = efforts["items"][0]
    assert first["start_s"] == 600 and first["avg_power_w"] == 400 and first["zone"].startswith("Z")
    assert first["pct_ftp"] == round(400 / efforts["ftp_w"] * 100)
    w = body["w_prime"]
    assert w["origin"] in ("fit", "ftp") and w["min_j"] < w["w_prime_j"]
    assert w["origin"] == "fit" or w["cp_w"] == efforts["ftp_w"]
    assert len(w["matches"]) == 5


def test_insights_use_the_pre_workout_weight(client: TestClient, settings: Settings) -> None:
    with Store(settings.db_path) as store:
        nutrition.set_workout_weight(store.conn, "strava", "2", 70.0, 69.0)
        assert ride_weight(store.conn, "strava", "2", "2026-10-03")["origin"] == "pre_workout"
        assert ride_weight(store.conn, "strava", "1", "2026-09-01") is None  # before any measurement
    assert client.get("/api/activities/strava/2/insights").json()["weight"]["kg"] == 70.0


def test_insights_without_cycling_power(client: TestClient) -> None:
    run = client.get("/api/activities/strava/run/insights").json()
    assert run["durability"] is None and run["efforts"] is None and run["w_prime"] is None
    assert client.get("/api/activities/strava/nope/insights").status_code == 404


def test_w_balance_lines_up_with_the_stream_clock(settings: Settings) -> None:
    from fitness_hub.insights import activity_streams

    # Unsorted samples, a non-zero start and a 2-minute stop; the hard minute starts right after it.
    times = [*range(1000, 1300), *range(1420, 1720)]
    power = [150.0] * 300 + [450.0] * 60 + [150.0] * 240
    order = np.random.default_rng(1).permutation(len(times))
    with Store(settings.db_path) as store:
        store.upsert_activities([{"source": "strava", "source_id": "p", "start_time": "2026-10-05T16:00:00Z",
                                  "local_date": "2026-10-05", "activity_type": "Ride", "raw": {}}])  # fmt: skip
        streams = {"time": [times[i] for i in order], "power": [power[i] for i in order]}
        store.upsert_activity_streams([{"source": "strava", "source_id": "p", "streams": streams}])
        store.conn.execute(
            "INSERT INTO athlete_settings (metric, effective_date, origin, value, updated_at) "
            "VALUES ('ftp_w', '2026-01-01', 'manual', 300, 'x')"
        )
        analyze(store)
        out = activity_streams(store.conn, "strava", "p", max_points=10000)
    by_time = dict(zip(out["time"], out["w_bal"], strict=True))
    assert by_time[1000] == by_time[1299] == 20000  # full until the hard minute
    assert by_time[1420] == 20000 - 150 and by_time[1479] == 20000 - 60 * 150  # 150 W over CP each second


def test_streams_carry_w_balance_for_a_ride(client: TestClient) -> None:
    ride_streams = client.get("/api/activities/strava/2/streams", params={"max_points": 5000}).json()
    assert len(ride_streams["w_bal"]) == len(ride_streams["time"])
    assert ride_streams["w_bal"][0] > 0 and min(ride_streams["w_bal"]) < max(ride_streams["w_bal"])
    assert "w_bal" not in client.get("/api/activities/strava/run/streams").json()


def test_default_w_prime_without_a_fit() -> None:
    assert DEFAULT_W_PRIME_J == 20000
