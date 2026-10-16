from __future__ import annotations

import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's TestClient warns about httpx; it still works
    from fastapi.testclient import TestClient

from fitness_hub import nutrition
from fitness_hub.analysis import analyze
from fitness_hub.api import create_app
from fitness_hub.config import Settings
from fitness_hub.db import SCHEMA_VERSION, Store, schema_version
from fitness_hub.util import InputError

BASE = "http://localhost:8801"
GEL = {"name": "Maurten Gel 100", "serving": "1 gel (40 g)", "kcal": 100, "carbs_g": 25, "sodium_mg": 20}
MIX = {"name": "Drink mix bottle", "serving": "750 ml bottle", "kcal": 240, "carbs_g": 60, "sodium_mg": 500,
       "fluid_ml": 750}  # fmt: skip


def ride(store: Store, source_id: str = "1", day: str = "2026-10-12", watts: float = 200, seconds: int = 7200) -> None:
    store.upsert_activities(
        [{"source": "strava", "source_id": source_id, "start_time": day + "T16:00:00Z", "local_date": day,
          "activity_type": "Ride", "moving_s": float(seconds), "raw": {}}]  # fmt: skip
    )
    streams = {"time": list(range(seconds)), "power": [watts] * seconds, "heart_rate": [140] * seconds}
    store.upsert_activity_streams([{"source": "strava", "source_id": source_id, "streams": streams}])


@pytest.fixture
def store(settings: Settings):
    with Store(settings.db_path) as s:
        yield s


def test_schema_has_the_nutrition_tables(store: Store) -> None:
    assert schema_version(store.conn) == SCHEMA_VERSION == 4
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"foods", "nutrition_entries"} <= tables


def test_entries_copy_nutrients_from_foods(store: Store) -> None:
    gel = nutrition.save_food(store.conn, GEL)
    assert gel and gel["id"] and gel["archived"] is False and gel["fat_g"] is None
    entry = nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "snack", "food_id": gel["id"],
                                              "servings": 2})  # fmt: skip
    assert entry and entry["name"] == "Maurten Gel 100" and entry["kcal"] == 200 and entry["carbs_g"] == 50
    # Nutrients sent are the entry's own totals; the rest still come from the food.
    custom = nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "snack", "food_id": gel["id"],
                                               "kcal": 90})  # fmt: skip
    assert custom and custom["kcal"] == 90 and custom["carbs_g"] == 25
    # Editing or deleting the food never rewrites what was logged.
    nutrition.save_food(store.conn, {**GEL, "kcal": 1}, gel["id"])
    assert nutrition.get_entry(store.conn, entry["id"])["kcal"] == 200  # type: ignore[index]
    assert nutrition.delete_food(store.conn, gel["id"])
    kept = nutrition.get_entry(store.conn, entry["id"])
    assert kept and kept["food_id"] is None and kept["kcal"] == 200 and kept["name"] == "Maurten Gel 100"
    assert not nutrition.delete_food(store.conn, gel["id"])


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"meal": "brunch"}, "meal must be one of"),
        ({"meal": "lunch", "name": None}, "name is required"),
        ({"meal": "lunch", "kcal": -5}, "kcal must be zero or more"),
        ({"meal": "lunch", "kcal": float("nan")}, "kcal must be zero or more"),
        ({"meal": "lunch", "servings": 0}, "servings must be positive"),
        ({"meal": "lunch", "food_id": 99}, "no saved food 99"),
        ({"meal": "lunch", "local_date": "soon"}, "local_date must be YYYY-MM-DD"),
        ({"meal": "lunch", "consumed_at": "2026-10-12T12:00:00"}, "needs a time zone"),
        ({"meal": "during_workout", "activity_source": "strava"}, "go together"),
        ({"meal": "during_workout", "activity_source": "strava", "activity_source_id": "nope"}, "no workout"),
        ({"meal": "lunch", "offset_s": 60}, "needs a workout"),
    ],
)
def test_entry_validation(store: Store, data: dict, message: str) -> None:
    with pytest.raises(InputError, match=message):
        nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "name": "Pasta", **data})


def test_consumed_at_is_stored_in_utc(store: Store) -> None:
    entry = nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "lunch", "name": "Pasta",
                                              "consumed_at": "2026-10-12T12:30:00-07:00"})  # fmt: skip
    assert entry and entry["consumed_at"] == "2026-10-12T19:30:00Z"


def test_daily_nutrition(store: Store) -> None:
    ride(store)  # 2 h at 200 W: 1440 kJ of work, no recorded calories
    analyze(store)
    store.upsert_daily_metrics(
        [
            {"source": "garmin", "date": "2026-10-12", "metric": "total_calories_kcal", "value": 3600},
            {"source": "google_health", "date": "2026-10-12", "metric": "total_calories_kcal", "value": 9999},
            {"source": "garmin", "date": "2026-10-12", "metric": "active_calories_kcal", "value": 1700},
        ]
    )
    for meal, kcal, carbs in (("breakfast", 600, 90), ("lunch", 900, None), ("during_workout", 480, 120)):
        nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": meal, "name": meal, "kcal": kcal,
                                          "carbs_g": carbs})  # fmt: skip
    days = nutrition.daily_nutrition(store.conn, "2026-10-11", "2026-10-12")
    assert [d["date"] for d in days] == ["2026-10-11", "2026-10-12"]
    empty, day = days
    assert empty["entries"] == 0 and empty["intake"]["kcal"] is None and empty["balance_kcal"] is None
    assert day["entries"] == 3 and day["intake"]["kcal"] == 1980 and day["intake"]["carbs_g"] == 210
    assert day["intake"]["fat_g"] is None  # nothing known
    assert day["workout_kcal"] == pytest.approx(1440)  # kJ of work for a ride without calories
    assert day["total_kcal"] == 3600 and day["active_kcal"] == 1700  # Garmin preferred over Google Health
    assert day["balance_kcal"] == pytest.approx(1980 - 3600)
    with pytest.raises(InputError):
        nutrition.daily_nutrition(store.conn, "2026-10-12", "2026-10-11")


def test_workout_fueling(store: Store) -> None:
    ride(store, watts=180)
    store.set_athlete_setting("ftp_w", "2026-01-01", 240)  # IF 0.75
    analyze(store)
    mix = nutrition.save_food(store.conn, MIX)
    gel = nutrition.save_food(store.conn, GEL)
    assert mix and gel
    link = {"local_date": "2026-10-12", "activity_source": "strava", "activity_source_id": "1"}
    nutrition.save_entry(store.conn, {**link, "meal": "during_workout", "food_id": mix["id"], "servings": 2,
                                      "offset_s": 0})  # fmt: skip
    nutrition.save_entry(store.conn, {**link, "meal": "during_workout", "food_id": gel["id"], "offset_s": 3600})
    nutrition.save_entry(store.conn, {**link, "meal": "during_workout", "name": "Caffeine gel", "carbs_g": 25,
                                      "caffeine_mg": 100, "offset_s": 1800})  # fmt: skip
    nutrition.save_entry(store.conn, {**link, "meal": "post_workout", "name": "Recovery shake", "kcal": 300})
    fueling = nutrition.workout_fueling(store.conn, "strava", "1")
    assert fueling
    assert [e["name"] for e in fueling["entries"]][:3] == ["Drink mix bottle", "Caffeine gel", "Maurten Gel 100"]
    assert fueling["during"]["carbs_g"] == 170 and fueling["during"]["fluid_ml"] == 1500
    assert fueling["per_hour"] == {"kcal": 290.0, "carbs_g": 85.0, "fluid_ml": 750.0, "sodium_mg": 510.0,
                                   "caffeine_mg": 50.0}  # fmt: skip
    assert fueling["burned_kcal"] == pytest.approx(1296)  # 2 h x 180 W = 1296 kJ
    assert fueling["carb_share_estimate"] == pytest.approx(0.65)
    assert fueling["carbs_burned_g_estimate"] == round(1296 * 0.65 / 4)
    assert nutrition.workout_fueling(store.conn, "strava", "missing") is None


def test_carb_share_is_bounded() -> None:
    assert nutrition.carb_share(0.3) == 0.3 and nutrition.carb_share(1.2) == 0.95
    assert nutrition.carb_share(0.75) == pytest.approx(0.65)


# -- API --------------------------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with Store(settings.db_path) as s:
        ride(s)
        analyze(s)
    return TestClient(create_app(settings), base_url=BASE)


def test_api_foods_and_entries(client: TestClient) -> None:
    created = client.post("/api/nutrition/foods", json=GEL)
    assert created.status_code == 201
    gel = created.json()
    assert client.get("/api/nutrition/foods").json() == [gel]
    archived = client.put(f"/api/nutrition/foods/{gel['id']}", json={**GEL, "archived": True}).json()
    assert archived["archived"] is True and client.get("/api/nutrition/foods").json() == []
    assert client.get("/api/nutrition/foods", params={"include_archived": True}).json()[0]["id"] == gel["id"]
    assert client.put("/api/nutrition/foods/999", json=GEL).status_code == 404
    assert client.post("/api/nutrition/foods", json={"name": "", "kcal": 1}).status_code == 422
    assert client.post("/api/nutrition/foods", json={"name": "x", "kcal": -1}).status_code == 422

    body = {"local_date": "2026-10-12", "meal": "during_workout", "food_id": gel["id"], "servings": 3,
            "activity_source": "strava", "activity_source_id": "1", "offset_s": 1200}  # fmt: skip
    entry = client.post("/api/nutrition/entries", json=body)
    assert entry.status_code == 201 and entry.json()["carbs_g"] == 75
    entry_id = entry.json()["id"]
    by_day = client.get("/api/nutrition/entries", params={"start_date": "2026-10-12", "end_date": "2026-10-12"}).json()
    by_ride = client.get("/api/nutrition/entries", params={"activity_source": "strava", "activity_source_id": "1"})
    assert [e["id"] for e in by_day] == [e["id"] for e in by_ride.json()] == [entry_id]
    assert client.get("/api/nutrition/entries").status_code == 400  # unbounded
    assert client.get("/api/nutrition/entries", params={"activity_source": "strava"}).status_code == 400

    edited = client.put(f"/api/nutrition/entries/{entry_id}", json={**body, "servings": 1, "carbs_g": 30})
    assert edited.json()["carbs_g"] == 30 and edited.json()["kcal"] == 100
    assert client.put("/api/nutrition/entries/999", json=body).status_code == 404
    assert client.post("/api/nutrition/entries", json={**body, "meal": "brunch"}).status_code == 422
    unknown = client.post("/api/nutrition/entries", json={**body, "activity_source_id": "x"})
    assert unknown.status_code == 400 and "no workout" in unknown.json()["detail"]

    fueling = client.get("/api/activities/strava/1/fueling").json()
    assert fueling["during"]["carbs_g"] == 30 and fueling["per_hour"]["carbs_g"] == 15
    assert client.get("/api/activities/strava/nope/fueling").status_code == 404
    daily = client.get("/api/nutrition/daily", params={"start_date": "2026-10-12", "end_date": "2026-10-12"}).json()
    assert daily[0]["intake"]["carbs_g"] == 30 and daily[0]["workout_kcal"] == pytest.approx(1440)
    assert client.get("/api/nutrition/daily").status_code == 400

    assert client.delete(f"/api/nutrition/entries/{entry_id}").status_code == 204
    assert client.delete(f"/api/nutrition/entries/{entry_id}").status_code == 404
    assert client.delete(f"/api/nutrition/foods/{gel['id']}").status_code == 204
    assert client.delete(f"/api/nutrition/foods/{gel['id']}").status_code == 404


def test_api_nutrition_writes_from_other_origins_are_refused(client: TestClient) -> None:
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/nutrition/foods", json=GEL, headers=evil).status_code == 403
    assert client.put("/api/nutrition/foods/1", json=GEL, headers=evil).status_code == 403
    assert client.delete("/api/nutrition/entries/1", headers=evil).status_code == 403
    assert client.get("/api/nutrition/foods").json() == []


# -- review follow-ups ---------------------------------------------------------------------------


def test_editing_an_entry_never_rederives_it_from_the_food(store: Store) -> None:
    gel = nutrition.save_food(store.conn, GEL)
    assert gel
    entry = nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "snack", "food_id": gel["id"]})
    assert entry and entry["kcal"] == 100
    nutrition.save_food(store.conn, {**GEL, "kcal": 300}, gel["id"])
    base = {"local_date": "2026-10-12", "meal": "snack", "food_id": gel["id"]}
    noted = nutrition.save_entry(store.conn, {**base, "note": "warm day"}, entry["id"])
    assert noted and noted["kcal"] == 100 and noted["note"] == "warm day"  # the food's new 300 is not used
    doubled = nutrition.save_entry(store.conn, {**base, "servings": 2}, entry["id"])
    assert doubled and doubled["kcal"] == 200 and doubled["carbs_g"] == 50  # scaled from the stored totals
    cleared = nutrition.save_entry(store.conn, {**base, "servings": 2, "sodium_mg": None}, entry["id"])
    assert cleared and cleared["sodium_mg"] is None and cleared["kcal"] == 200  # an explicit null clears
    mix = nutrition.save_food(store.conn, MIX)
    assert mix
    moved = nutrition.save_entry(store.conn, {**base, "food_id": mix["id"], "servings": 1}, entry["id"])
    assert moved and moved["kcal"] == 240 and moved["fluid_ml"] == 750  # another food: refilled from it
    assert nutrition.save_entry(store.conn, base, 999) is None


def test_editing_keeps_where_an_entry_came_from(store: Store) -> None:
    imported = nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "lunch", "name": "Salad",
                                                 "kcal": 400, "source": "cronometer", "source_id": "abc"})  # fmt: skip
    assert imported
    edited = nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "dinner"}, imported["id"])
    assert edited and (edited["source"], edited["source_id"]) == ("cronometer", "abc")
    assert edited["name"] == "Salad" and edited["kcal"] == 400 and edited["meal"] == "dinner"


def test_fueling_and_calories_follow_the_workout_across_services(store: Store) -> None:
    """The same ride from Strava (analysed, shown) and Garmin (calories): entries linked to either copy
    count, and the Garmin copy's calories are used."""
    ride(store, "s1")
    store.upsert_activities([{"source": "garmin", "source_id": "g1", "start_time": "2026-10-12T16:00:40Z",
                              "local_date": "2026-10-12", "activity_type": "cycling", "calories": 1500.0,
                              "raw": {}}])  # fmt: skip
    analyze(store)
    nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "during_workout", "name": "Bar",
                                      "carbs_g": 40, "activity_source": "garmin", "activity_source_id": "g1"})  # fmt: skip
    fueling = nutrition.workout_fueling(store.conn, "strava", "s1")
    assert fueling and fueling["during"]["carbs_g"] == 40 and fueling["burned_kcal"] == 1500
    day = nutrition.daily_nutrition(store.conn, "2026-10-12", "2026-10-12")[0]
    assert day["workout_kcal"] == 1500  # the Garmin copy's calories, not the Strava copy's 1440 kJ
    assert day["partial"] is False


def test_api_rejects_nan_and_booleans_cleanly(client: TestClient) -> None:
    nan = client.post("/api/nutrition/foods", content='{"name": "x", "kcal": NaN}',
                      headers={"Content-Type": "application/json"})  # fmt: skip
    assert nan.status_code == 422 and nan.json()["detail"][0]["loc"][-1] == "kcal"
    assert client.post("/api/nutrition/foods", json={"name": "x", "kcal": True}).status_code == 422
    entry = {"local_date": "2026-10-12", "meal": "snack", "name": "x"}
    assert client.post("/api/nutrition/entries", json={**entry, "food_id": True}).status_code == 422
    assert client.post("/api/nutrition/entries", json={**entry, "kcal": 12}).status_code == 201  # ints are numbers


def test_archived_foods_are_hidden_unless_asked(store: Store) -> None:
    kept = nutrition.save_food(store.conn, GEL)
    gone = nutrition.save_food(store.conn, {**MIX, "archived": True})
    assert kept and gone
    assert [f["id"] for f in nutrition.list_foods(store.conn)] == [kept["id"]]
    assert {f["id"] for f in nutrition.list_foods(store.conn, include_archived=True)} == {kept["id"], gone["id"]}


def test_a_v3_database_with_data_migrates_to_v4(tmp_path) -> None:
    import sqlite3

    from fitness_hub.db import MIGRATIONS, SCHEMA

    path = tmp_path / "fitness.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for version in (2, 3):
        for statement in MIGRATIONS[version]:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO activities (source, source_id, start_time, local_date, activity_type, raw, updated_at) "
                 "VALUES ('strava', '1', '2026-10-12T16:00:00Z', '2026-10-12', 'Ride', '{}', '2026-10-12T18:00:00Z')")  # fmt: skip
    conn.commit()
    conn.close()
    with Store(path) as migrated:
        assert schema_version(migrated.conn) == 4 and migrated.row_counts()["activities"] == 1
        assert nutrition.save_entry(migrated.conn, {"local_date": "2026-10-12", "meal": "during_workout",
                                                    "name": "Gel", "activity_source": "strava",
                                                    "activity_source_id": "1"})  # fmt: skip


def test_mcp_nutrition_tools(settings: Settings) -> None:
    from tests.test_mcp_server import call

    with Store(settings.db_path) as s:
        ride(s)
        analyze(s)
        nutrition.save_entry(s.conn, {"local_date": "2026-10-12", "meal": "during_workout", "name": "Gel",
                                      "carbs_g": 25, "activity_source": "strava", "activity_source_id": "1"})  # fmt: skip
    error, days = call(settings, "nutrition_daily", {"start_date": "2026-10-12", "end_date": "2026-10-12"})
    assert not error and days["days"][0]["intake"]["carbs_g"] == 25  # type: ignore[index]
    error, fueling = call(settings, "workout_fueling", {"source": "strava", "source_id": "1"})
    assert not error and fueling["during"]["carbs_g"] == 25  # type: ignore[index]
