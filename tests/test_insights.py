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
    durability,
    effort_spans,
    matches,
    ride_weight,
    w_balance,
)

BASE = "http://localhost:8801"


def intervals(reps: int = 3, on: int = 60, off: int = 120, hard: float = 350, easy: float = 150) -> list[float]:
    return ([easy] * 600 + ([hard] * on + [easy] * off) * reps) + [easy] * 600


def test_durability_by_kj_per_kg() -> None:
    power = np.full(3600, 300.0)  # 1080 kJ: 14.4 kJ/kg at 75 kg
    result = durability(power, 75)
    assert result is not None and result["unit"] == "kJ/kg" and result["steps"] == [0, 10] and result["total"] == 14.4
    rows = {r["duration_s"]: r["watts"] for r in result["rows"]}
    # 10 kJ/kg (750 kJ) is done after 2500 s: 1100 s are left, too few for 20 minutes.
    assert rows == {60: [300, 300], 300: [300, 300], 1200: [300, None]}


def test_durability_shows_the_fade_and_counts_kj_without_a_weight() -> None:
    power = np.concatenate([np.full(1800, 400.0), np.full(9000, 250.0)])  # hard start, then steady
    result = durability(power, None)
    assert result is not None and result["unit"] == "kJ" and result["steps"] == [0, 1000, 2000]
    five = next(r["watts"] for r in result["rows"] if r["duration_s"] == 300)
    assert five == [400, 250, 250]  # the 720 kJ hard start is over before 1000 kJ
    assert durability(np.full(30, 200.0), 70) is None


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
    assert all(balance[h] - balance[lo] == pytest.approx(3000, abs=50) for h, lo in found)


def test_a_match_ends_when_w_prime_stops_falling() -> None:
    # A hard minute, then a long slow drain just above CP: one match, not one that lasts the whole ride.
    power = np.array([150.0] * 60 + [400.0] * 60 + [302.0] * 3600, dtype=float)
    balance = w_balance(power, cp=300, w_prime=20000)
    (high, low), *rest = matches(balance, 20000)
    assert low - high < 400 and rest == []


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
    assert body["durability"]["unit"] == "kJ/kg" and body["durability"]["steps"][0] == 0
    efforts = body["efforts"]
    assert efforts["count"] == 5 and efforts["items"][0]["duration_s"] == 60
    first = efforts["items"][0]
    assert first["start_s"] == 600 and first["avg_power_w"] == 400 and first["zone"].startswith("Z")
    assert first["pct_ftp"] == round(400 / efforts["ftp_w"] * 100)
    w = body["w_prime"]
    assert w["cp_w"] == efforts["ftp_w"] and w["origin"] in ("fit", "default") and w["min_j"] < w["w_prime_j"]
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


def test_streams_carry_w_balance_for_a_ride(client: TestClient) -> None:
    ride_streams = client.get("/api/activities/strava/2/streams", params={"max_points": 5000}).json()
    assert len(ride_streams["w_bal"]) == len(ride_streams["time"])
    assert ride_streams["w_bal"][0] > 0 and min(ride_streams["w_bal"]) < max(ride_streams["w_bal"])
    assert "w_bal" not in client.get("/api/activities/strava/run/streams").json()


def test_default_w_prime_without_a_fit() -> None:
    assert DEFAULT_W_PRIME_J == 20000
