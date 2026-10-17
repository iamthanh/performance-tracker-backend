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
    assert schema_version(store.conn) == SCHEMA_VERSION == 6
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


def test_a_v3_database_with_data_migrates_to_the_latest(tmp_path) -> None:
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
        assert schema_version(migrated.conn) == SCHEMA_VERSION and migrated.row_counts()["activities"] == 1
        assert nutrition.save_entry(migrated.conn, {"local_date": "2026-10-12", "meal": "during_workout",
                                                    "name": "Gel", "activity_source": "strava",
                                                    "activity_source_id": "1"})  # fmt: skip




def test_workouts_report_kcal_burned(store: Store) -> None:
    """kcal in the activity list and summaries: recorded calories (from any copy), else a ride's kJ of work."""
    from fitness_hub import queries

    ride(store, "s1")  # 2 h at 200 W, no calories: 1440 kJ
    ride(store, "s2", day="2026-10-13")
    store.upsert_activities(
        [
            {"source": "garmin", "source_id": "g2", "start_time": "2026-10-13T16:01:00Z", "local_date": "2026-10-13",
             "activity_type": "cycling", "calories": 1500.0, "raw": {}},  # the same ride, recorded with calories
            {"source": "strava", "source_id": "run", "start_time": "2026-10-14T07:00:00Z", "local_date": "2026-10-14",
             "activity_type": "Run", "calories": 600.0, "raw": {}},
            {"source": "strava", "source_id": "walk", "start_time": "2026-10-14T18:00:00Z", "local_date": "2026-10-14",
             "activity_type": "Walk", "raw": {}},  # nothing known
        ]
    )  # fmt: skip
    analyze(store)
    kcal = {r["source_id"]: r["kcal"] for r in queries.activities_with_metrics(store.conn, "2026-10-12", "2026-10-14")}
    assert kcal == {"s1": pytest.approx(1440), "s2": 1500, "run": 600, "walk": None}
    weeks = queries.period_totals(store.conn, "2026-10-12", "2026-10-14", "week")["periods"]
    assert weeks[0]["total"]["kcal"] == pytest.approx(1440 + 1500 + 600)
    assert weeks[0]["by_sport"]["running"]["kcal"] == 600


# -- drinks (bottles with a recipe) and body weight ------------------------------------------------

MIX_BOTTLE = {"water_ml": 750, "salt_g": 1.5, "maltodextrin_g": 60, "sucrose_g": 30,
              "other": [{"name": "Caffeine powder", "grams": 0.1, "caffeine_mg": 100}]}  # fmt: skip


def test_a_bottle_recipe_becomes_totals(store: Store) -> None:
    ride(store)
    analyze(store)
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "1"}
    entry = nutrition.save_entry(store.conn, {**link, "drink": {**MIX_BOTTLE, "bottles": 2}})
    assert entry and entry["kind"] == "drink" and entry["name"] == "Bottle" and entry["servings"] == 2
    assert entry["recipe"]["salt_g"] == 1.5 and entry["recipe"]["other"][0]["name"] == "Caffeine powder"
    assert entry["fluid_ml"] == 1500 and entry["carbs_g"] == 180 and entry["sugars_g"] == 60
    assert entry["sodium_mg"] == pytest.approx(2 * 1.5 * 393.4) and entry["caffeine_mg"] == 200
    assert entry["kcal"] == 720  # 4 kcal per gram of carbs
    # Edited without its recipe: one bottle fewer scales the totals; the recipe stays.
    fewer = nutrition.save_entry(store.conn, {**link, "servings": 1}, entry["id"])
    assert fewer and fewer["kind"] == "drink" and fewer["carbs_g"] == 90 and fewer["recipe"]["maltodextrin_g"] == 60
    # A new recipe recomputes everything.
    water = nutrition.save_entry(store.conn, {**link, "drink": {"water_ml": 500, "bottles": 1}}, entry["id"])
    assert water and water["fluid_ml"] == 500 and water["carbs_g"] is None and water["sodium_mg"] is None

    fueling = nutrition.workout_fueling(store.conn, "strava", "1")
    assert fueling and fueling["drinks"]["bottles"] == 1 and fueling["drinks"]["water_ml"] == 500


def test_drink_summary_splits_glucose_and_fructose(store: Store) -> None:
    ride(store)
    analyze(store)
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "1"}
    nutrition.save_entry(store.conn, {**link, "drink": {**MIX_BOTTLE, "bottles": 2}})
    nutrition.save_entry(store.conn, {**link, "name": "Gel", "carbs_g": 25})  # food: not a bottle
    drinks = nutrition.workout_fueling(store.conn, "strava", "1")["drinks"]  # type: ignore[index]
    assert drinks == {"bottles": 2, "water_ml": 1500, "salt_g": 3, "maltodextrin_g": 120, "sucrose_g": 60,
                      "other_carbs_g": 0, "glucose_g": 150, "fructose_g": 30, "fructose_per_glucose": 0.2}  # fmt: skip


@pytest.mark.parametrize(
    ("drink", "message"),
    [
        ({}, "at least one ingredient"),
        ({"water_ml": -1}, "zero or more"),
        ({"water_ml": 500, "other": [{"grams": 2}]}, "other\\[0\\].name is required"),
        ({"water_ml": 500, "other": "salt"}, "must be a list"),
        ({"water_ml": 500, "bottles": 0}, "bottles must be positive"),
    ],
)
def test_drink_validation(store: Store, drink: dict, message: str) -> None:
    with pytest.raises(InputError, match=message):
        nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "during_workout", "drink": drink})


def test_an_entry_is_a_food_or_a_drink(store: Store) -> None:
    gel = nutrition.save_food(store.conn, GEL)
    assert gel
    with pytest.raises(InputError, match="not both"):
        nutrition.save_entry(store.conn, {"local_date": "2026-10-12", "meal": "snack", "food_id": gel["id"],
                                          "drink": {"water_ml": 500}})  # fmt: skip


def test_workout_weight_and_sweat_rate(store: Store) -> None:
    ride(store, "s1")  # 2 h
    store.upsert_activities([{"source": "garmin", "source_id": "g1", "start_time": "2026-10-12T16:00:40Z",
                              "local_date": "2026-10-12", "activity_type": "cycling", "raw": {}}])  # fmt: skip
    analyze(store)
    assert nutrition.set_workout_weight(store.conn, "strava", "nope", 70, 69) is None
    saved = nutrition.set_workout_weight(store.conn, "garmin", "g1", 72.0, 70.8, "hot day")
    assert saved and saved["pre_kg"] == 72 and saved["post_kg"] == 70.8
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "s1"}
    nutrition.save_entry(store.conn, {**link, "drink": {"water_ml": 750, "bottles": 2}})
    fueling = nutrition.workout_fueling(store.conn, "strava", "s1")  # the weights were saved on the Garmin copy
    assert fueling and fueling["weight"]["pre_kg"] == 72
    sweat = fueling["sweat"]
    assert sweat["change_kg"] == pytest.approx(-1.2) and sweat["change_pct"] == pytest.approx(-1.67, abs=0.01)
    assert sweat["sweat_l"] == pytest.approx(1.2 + 1.5) and sweat["sweat_l_per_h"] == pytest.approx(2.7 / 2, abs=0.01)
    # Saving on another copy replaces it (one row per workout); saving neither clears.
    nutrition.set_workout_weight(store.conn, "strava", "s1", 72.0, None)
    assert store.conn.execute("SELECT COUNT(*) FROM workout_weights").fetchone()[0] == 1
    assert nutrition.workout_fueling(store.conn, "strava", "s1")["sweat"] is None  # type: ignore[index]
    nutrition.set_workout_weight(store.conn, "strava", "s1", None, None)
    assert nutrition.get_workout_weight(store.conn, "strava", "s1") is None
    with pytest.raises(InputError, match="between 20 and 300 kg"):
        nutrition.set_workout_weight(store.conn, "strava", "s1", 720, 71)


def test_api_drinks_and_weight(client: TestClient) -> None:
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "1"}
    made = client.post("/api/nutrition/entries", json={**link, "drink": {**MIX_BOTTLE, "bottles": 2}})
    assert made.status_code == 201 and made.json()["carbs_g"] == 180 and made.json()["recipe"]["sucrose_g"] == 30
    assert client.post("/api/nutrition/entries", json={**link, "drink": {"water_ml": "500"}}).status_code == 422
    assert client.post("/api/nutrition/entries", json={**link, "drink": {"bottles": 0, "water_ml": 1}}).status_code == 422
    weight = client.put("/api/activities/strava/1/weight", json={"pre_kg": 72.0, "post_kg": 71})
    assert weight.status_code == 200 and weight.json()["post_kg"] == 71
    assert client.put("/api/activities/strava/1/weight", json={"pre_kg": 5}).status_code == 422
    assert client.put("/api/activities/strava/nope/weight", json={"pre_kg": 70}).status_code == 404
    fueling = client.get("/api/activities/strava/1/fueling").json()
    assert fueling["sweat"]["change_kg"] == -1 and fueling["drinks"]["bottles"] == 2
    evil = client.put("/api/activities/strava/1/weight", json={"pre_kg": 70}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


def test_editing_a_bottle(store: Store) -> None:
    ride(store)
    analyze(store)
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "1"}
    entry = nutrition.save_entry(store.conn, {**link, "name": "Morning mix", "drink": {**MIX_BOTTLE, "bottles": 2}})
    assert entry
    # A new recipe keeps the entry's name and bottle count unless they are sent.
    edited = nutrition.save_entry(store.conn, {**link, "drink": {"water_ml": 500, "maltodextrin_g": 40}}, entry["id"])
    assert edited and edited["name"] == "Morning mix" and edited["servings"] == 2 and edited["carbs_g"] == 80
    # Nutrients cannot be set beside a recipe, with a new one or without.
    for body in ({**link, "drink": {"water_ml": 500}, "carbs_g": 10}, {**link, "carbs_g": 10}):
        with pytest.raises(InputError, match="come from its recipe"):
            nutrition.save_entry(store.conn, body, entry["id"])
    # "drink": null makes it a food: the recipe goes.
    food = nutrition.save_entry(store.conn, {**link, "drink": None, "name": "Banana", "carbs_g": 25, "fluid_ml": None,
                                             "kcal": 100, "sugars_g": None, "sodium_mg": None, "caffeine_mg": None},
                                entry["id"])  # fmt: skip
    assert food and food["kind"] == "food" and food["recipe"] is None and food["carbs_g"] == 25 and food["fluid_ml"] is None
    assert nutrition.workout_fueling(store.conn, "strava", "1")["drinks"] is None  # type: ignore[index]


def test_scaled_bottles_are_rounded(store: Store) -> None:
    ride(store)
    analyze(store)
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "1"}
    entry = nutrition.save_entry(store.conn, {**link, "drink": {"salt_g": 1.0, "water_ml": 500, "bottles": 3}})
    assert entry
    two = nutrition.save_entry(store.conn, {**link, "servings": 2}, entry["id"])
    assert two and two["sodium_mg"] == pytest.approx(786.8) and two["sodium_mg"] == round(two["sodium_mg"], 2)


def test_the_weight_note_is_kept_and_needs_a_weight(store: Store) -> None:
    ride(store)
    analyze(store)
    nutrition.set_workout_weight(store.conn, "strava", "1", 72, 71, "hot day")
    again = nutrition.set_workout_weight(store.conn, "strava", "1", 72, 70.5)  # e.g. the UI, which sends no note
    assert again and again["note"] == "hot day" and again["post_kg"] == 70.5
    cleared = nutrition.set_workout_weight(store.conn, "strava", "1", 72, 70.5, None)
    assert cleared and cleared["note"] is None
    with pytest.raises(InputError, match="needs a weight"):
        nutrition.set_workout_weight(store.conn, "strava", "1", None, None, "x")


def test_sweat_rate_uses_the_longest_elapsed_time(store: Store) -> None:
    ride(store, "s1")  # 2 h moving, no elapsed time on this copy
    store.upsert_activities([{"source": "garmin", "source_id": "g1", "start_time": "2026-10-12T16:00:40Z",
                              "local_date": "2026-10-12", "activity_type": "cycling", "duration_s": 3 * 3600.0,
                              "raw": {}}])  # fmt: skip
    analyze(store)
    nutrition.set_workout_weight(store.conn, "strava", "s1", 72, 70.5)
    sweat = nutrition.workout_fueling(store.conn, "strava", "s1")["sweat"]  # type: ignore[index]
    assert sweat["sweat_l_per_h"] == pytest.approx(1.5 / 3)  # over 3 h out, not 2 h moving


def test_api_drink_typos_and_note(client: TestClient) -> None:
    link = {"local_date": "2026-10-12", "meal": "during_workout", "activity_source": "strava", "activity_source_id": "1"}
    typo = client.post("/api/nutrition/entries", json={**link, "drink": {"water_ml": 500, "malto_g": 60}})
    assert typo.status_code == 422 and typo.json()["detail"][0]["loc"][-1] == "malto_g"
    client.put("/api/activities/strava/1/weight", json={"pre_kg": 72.0, "post_kg": 71.0, "note": "hot"})
    kept = client.put("/api/activities/strava/1/weight", json={"pre_kg": 72.0, "post_kg": 70.0})
    assert kept.json()["note"] == "hot"
    assert client.put("/api/activities/strava/1/weight", json={"note": "x"}).status_code == 400


def test_v4_entries_become_foods_on_upgrade(tmp_path) -> None:
    import sqlite3

    from fitness_hub.db import MIGRATIONS, SCHEMA

    path = tmp_path / "fitness.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for version in (2, 3, 4):
        for statement in MIGRATIONS[version]:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO nutrition_entries (local_date, meal, name, kcal, created_at, updated_at) "
                 "VALUES ('2026-10-12', 'lunch', 'Pasta', 700, 'x', 'x')")  # fmt: skip
    conn.commit()
    conn.close()
    with Store(path) as upgraded:
        entry = nutrition.list_entries(upgraded.conn, "2026-10-12", "2026-10-12")[0]
        assert (entry["kind"], entry["recipe"], entry["kcal"]) == ("food", None, 700)
        with pytest.raises(Exception, match="CHECK"):
            upgraded.conn.execute("UPDATE nutrition_entries SET kind = 'soup'")
