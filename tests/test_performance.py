from __future__ import annotations

import warnings
from datetime import date, timedelta

import numpy as np
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's TestClient warns about httpx; it still works
    from fastapi.testclient import TestClient

from fitness_hub import performance as P
from fitness_hub.analysis import analyze, fatigued_bests
from fitness_hub.api import create_app
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.util import InputError

BASE = "http://localhost:8801"


def curve(scale: float = 1.0) -> dict[int, float]:
    """A plausible best-effort curve: 5 s 900 W ... 60 min 230 W, times ``scale``."""
    base = {5: 900, 60: 450, 180: 330, 300: 300, 420: 285, 600: 275, 720: 270, 900: 265, 1200: 260,
            1800: 245, 3600: 230}  # fmt: skip
    return {d: w * scale for d, w in base.items()}


KG = 75.0


def weigh(store: Store, day: str, kg: float) -> None:
    row = {"source": "weight_csv", "ts": f"{day}T12:00:00Z", "metric": "weight_kg", "value": kg}
    store.upsert_body_measurements([row])


def add_ride(store: Store, source_id: str, day: str, c: dict[int, float], fatigued: dict[int, float] | None = None,
             activity_type: str = "Ride", kg: float | None = KG) -> None:  # fmt: skip
    """A ride with its best efforts, weighed that day (``kg``; None: no weigh-in)."""
    if kg:
        weigh(store, day, kg)
    store.upsert_activities(
        [{"source": "strava", "source_id": source_id, "start_time": day + "T08:00:00Z", "local_date": day,
          "activity_type": activity_type, "name": f"Ride {source_id}", "raw": {}}]  # fmt: skip
    )
    store.replace_power_curve("strava", source_id, c)
    if fatigued:
        store.replace_durability_bests("strava", source_id, {(1000, d): w for d, w in fatigued.items()})


def day(offset: int) -> str:
    return (date(2020, 1, 1) + timedelta(days=offset)).isoformat()  # in the past: the index ends today


@pytest.fixture
def store(settings: Settings):
    with Store(settings.db_path) as s:
        yield s


def test_fatigued_bests_start_after_the_work() -> None:
    power = np.array([300.0] * 3600 + [200.0] * 3600)  # 1080 kJ in the first hour
    bests = fatigued_bests(power)
    # 1000 kJ is done after 3334 s: the best 5 min after it is 266 s at 300 W and 34 s at 200 W.
    assert bests[(1000, 300)] == pytest.approx((266 * 300 + 34 * 200) / 300)
    assert 200 < bests[(1000, 1200)] < 300 and bests[(1500, 300)] == 200  # 1800 kJ in all
    assert (2000, 300) not in bests  # never reached


def test_analyze_stores_durability_bests(store: Store) -> None:
    store.upsert_activities([{"source": "strava", "source_id": "1", "start_time": "2026-01-01T08:00:00Z",
                              "local_date": "2026-01-01", "activity_type": "Ride", "raw": {}}])  # fmt: skip
    streams = {"time": list(range(7200)), "power": [250.0] * 7200}
    store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": streams}])
    analyze(store)
    rows = store.conn.execute("SELECT after_kj, duration_s, watts FROM durability_bests ORDER BY 1, 2").fetchall()
    assert [tuple(r) for r in rows][:2] == [(1000, 300, 250.0), (1000, 1200, 250.0)]


def test_components_rise_with_a_best_and_fade_slowly_without(store: Store) -> None:
    add_ride(store, "1", day(0), curve(1.0))
    add_ride(store, "2", day(30), curve(0.9))  # not a best: nothing rises
    add_ride(store, "3", day(120), curve(1.1))  # a new best after the first ride left the window
    days = {d["date"]: d for d in P.raw_timeline(store.conn, 0.01, end=day(130))["days"]}
    assert days[day(0)]["values"]["vo2max"] == 300 / KG  # W/kg
    # Ride 1 leaves the 90-day window after day 89; from then the value eases from 300 W (ride 2: 270 W).
    assert days[day(89)]["values"]["vo2max"] == 300 / KG
    assert days[day(100)]["values"]["vo2max"] == pytest.approx(max(270, 300 * 0.99 ** (11 / 7)) / KG)
    assert days[day(120)]["values"]["vo2max"] == pytest.approx(330 / KG)
    assert days[day(130)]["tested"]["vo2max"] == day(120)
    events = P.raw_timeline(store.conn, 0.01, end=day(130))["events"]
    rises = [(e["date"], e["component"]) for e in events]
    assert (day(120), "vo2max") in rises and not any(d == day(30) for d, _ in rises)
    assert next(e for e in events if e["date"] == day(120))["ride"]["source_id"] == "3"


def test_durability_is_the_share_of_fresh_power_kept(store: Store) -> None:
    add_ride(store, "1", day(0), curve(), fatigued={300: 270, 1200: 247})
    first = P.raw_timeline(store.conn, 0.0, end=day(0))["days"][0]
    assert first["values"]["durability"] == pytest.approx((270 / 300 + 247 / 260) / 2)


def test_score_is_a_weighted_geometric_mean() -> None:
    weights = P.DEFAULT_WEIGHTS
    assert P.score(dict.fromkeys(P.COMPONENTS, 110.0), weights) == pytest.approx(110)
    mixed = {**dict.fromkeys(P.COMPONENTS, 100.0), "sprint": 150.0}
    assert P.score(mixed, weights) == pytest.approx(100 * 1.5**0.1)  # sprint weighs 10 %
    assert P.score({**mixed, "durability": None}, weights) == pytest.approx(100 * 1.5 ** (10 / 80))  # left out
    assert P.score({**mixed, "threshold": None}, weights) is None


def test_performance_indexes_against_the_baseline(store: Store) -> None:
    for i in range(0, 600, 7):  # a steady rider: every index ends up at 100
        add_ride(store, str(i), day(i), curve(), fatigued={300: 270, 1200: 234})
    add_ride(store, "best", day(601), curve(1.2))
    result = P.performance(store.conn, start_date=day(590), end_date=day(605))
    assert result["days"][0]["date"] == day(590)
    before, after = result["days"][0], result["days"][-1]
    assert before["components"]["vo2max"]["index"] == pytest.approx(100, abs=0.5)
    assert before["score"] == pytest.approx(100, abs=1)
    assert after["components"]["vo2max"]["index"] == pytest.approx(120, abs=0.5)
    assert after["score"] > 110
    changes = {e["component"]: e for e in result["events"]}
    assert changes["vo2max"]["date"] == day(601) and changes["vo2max"]["index_change"] == pytest.approx(20, abs=0.5)
    assert {c["key"] for c in result["components"]} == set(P.COMPONENTS)


def test_the_first_window_is_not_reported(store: Store) -> None:
    add_ride(store, "1", day(0), curve())
    days = P.performance(store.conn, end_date=day(100))["days"]
    assert days[0]["date"] == day(89) and days[-1]["date"] == day(100)


def test_no_rises_without_a_new_best_even_with_fast_easing(store: Store) -> None:
    for i in range(0, 120, 3):
        add_ride(store, str(i), day(i), curve())  # the same efforts over and over
    events = P.raw_timeline(store.conn, 0.05, end=day(120))["events"]
    assert [e for e in events if e["date"] > day(0)] == []


def test_a_days_score_does_not_depend_on_the_range(store: Store) -> None:
    for i in range(0, 700, 6):
        add_ride(store, str(i), day(i), curve(1 + (i % 60) / 300))
    short = P.performance(store.conn, start_date=day(200), end_date=day(200))["days"][0]
    full = next(d for d in P.performance(store.conn)["days"] if d["date"] == day(200))
    assert short == full


def test_an_easy_ride_after_a_layoff_is_not_a_test(store: Store) -> None:
    add_ride(store, "best", day(0), curve())
    add_ride(store, "easy", day(150), curve(0.7))  # 70 % of the (slowly easing) value
    last = P.raw_timeline(store.conn, 0.005, end=day(150))["days"][-1]
    assert last["values"]["vo2max"] > 0.9 * 300 / KG and last["tested"]["vo2max"] is None


def test_range_checks_and_warm_up_events(store: Store) -> None:
    add_ride(store, "1", day(0), curve())
    add_ride(store, "2", day(40), curve(1.1))  # a rise while the first window fills
    result = P.performance(store.conn)
    assert result["starts"] == day(89) and result["first_ride"] == day(0)
    assert result["events"] == []  # before the index starts
    assert result["days"][-1]["date"] == date.today().isoformat()
    future = P.performance(store.conn, end_date="2999-01-01")["days"]
    assert future[-1]["date"] == date.today().isoformat()  # never past today
    with pytest.raises(InputError):
        P.performance(store.conn, start_date=day(100), end_date=day(99))


def test_power_is_per_kg_of_each_rides_weight(store: Store) -> None:
    add_ride(store, "heavy", day(0), curve(), kg=80.0)
    add_ride(store, "light", day(10), curve(0.98), kg=70.0)  # 2 % less power, but lighter: a new best
    timeline = P.raw_timeline(store.conn, 0.01, end=day(10))
    days = {d["date"]: d for d in timeline["days"]}
    assert days[day(0)]["values"]["vo2max"] == pytest.approx(300 / 80)
    rise = next(e for e in timeline["events"] if e["component"] == "vo2max")
    light_kg = rise["ride"]["kg"]  # the trend weight of that day: on its way from 80 to 70 kg
    assert 70 < light_kg < 80 and rise["ride"]["watts"] == pytest.approx(294)
    assert days[day(10)]["values"]["vo2max"] == pytest.approx(294 / light_kg, abs=0.001)
    assert rise["split"] == {"power_pct": -2.0, "weight_pct": pytest.approx(100 * (80 / light_kg - 1), abs=0.05)}
    # Durability is a ratio of watts: weight cancels.
    assert days[day(10)]["values"]["durability"] is None or days[day(10)]["values"]["durability"] <= 1


def test_rides_without_a_weight_do_not_count_and_estimated_weights_are_flagged(store: Store) -> None:
    add_ride(store, "unweighed", day(0), curve(), kg=None)
    assert all(d["values"]["vo2max"] is None for d in P.raw_timeline(store.conn, 0.01, end=day(5))["days"])
    weigh(store, day(10), 75.0)
    add_ride(store, "guess", day(60), curve(1.1), kg=None)  # inside a 100-day gap: an estimated weight
    weigh(store, day(110), 75.0)
    result = P.performance(store.conn, end_date=day(150))
    flagged = {d["date"]: d["components"]["vo2max"]["estimated_weight"] for d in result["days"]}
    assert flagged[day(100)] is True  # the window's best rests on the estimated-weight ride
    assert flagged[day(150)] is True  # still the value in effect, eased


def test_runs_do_not_count(store: Store) -> None:
    add_ride(store, "run", day(0), curve(), activity_type="Run")
    assert P.performance(store.conn)["days"] == []


def test_settings(store: Store) -> None:
    assert P.get_settings(store.conn) == {"weights": P.DEFAULT_WEIGHTS, "decay_per_week": P.DEFAULT_DECAY_PER_WEEK}
    climber = P.PRESETS["climber"]["weights"]
    saved = P.set_settings(store.conn, climber, 0.01)
    assert saved["weights"]["vo2max"] == 25 and saved["decay_per_week"] == 0.01
    assert P.set_settings(store.conn, P.DEFAULT_WEIGHTS)["decay_per_week"] == 0.01  # kept
    for bad in ({**climber, "sprint": -1}, {k: 0 for k in P.COMPONENTS}, {"sprint": 10}, {**climber, "x": 1}):
        with pytest.raises(InputError):
            P.set_settings(store.conn, bad)
    with pytest.raises(InputError):
        P.set_settings(store.conn, climber, 0.2)


def test_backtest_shapes(store: Store) -> None:
    for i in range(0, 200, 5):
        add_ride(store, str(i), day(i), curve(1 + (i % 20) / 100))
    rows = P.backtest(store.conn, decays=(0.0, 0.01))
    assert [r["decay_per_week"] for r in rows] == [0.0, 0.01]
    assert rows[0]["samples"] == rows[1]["samples"] > 0 and rows[0]["mae_pct"] is not None


def test_api(settings: Settings) -> None:
    with Store(settings.db_path) as store:
        for i in range(0, 200, 7):
            add_ride(store, str(i), day(i), curve())
    client = TestClient(create_app(settings), base_url=BASE)
    body = client.get("/api/performance", params={"start_date": day(150)}).json()
    assert body["days"][0]["date"] == day(150) and body["weights"] == P.DEFAULT_WEIGHTS
    assert set(body["days"][0]["components"]) == set(P.COMPONENTS)
    assert client.get("/api/performance", params={"start_date": "nope"}).status_code == 400
    put = client.put("/api/performance/settings", json={"weights": P.PRESETS["time_trial"]["weights"]})
    assert put.status_code == 200 and put.json()["weights"]["threshold"] == 40
    assert client.get("/api/performance/settings").json()["weights"]["threshold"] == 40
    assert client.put("/api/performance/settings", json={"weights": {"sprint": 1}}).status_code == 400
    assert client.put("/api/performance/settings", json={"weights": P.DEFAULT_WEIGHTS, "x": 1}).status_code == 422
