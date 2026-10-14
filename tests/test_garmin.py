from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fitness_hub.auth import AuthError
from fitness_hub.providers import SyncWindow
from fitness_hub.providers import garmin

WINDOW = SyncWindow(date(2026, 10, 1), date(2026, 10, 3))

ACTIVITY = {
    "activityId": 987,
    "activityName": "Lunch Ride",
    "startTimeLocal": "2026-10-02 12:00:05",
    "startTimeGMT": "2026-10-02 10:00:05",
    "activityType": {"typeKey": "road_biking"},
    "duration": 3700.5,
    "movingDuration": 3600.0,
    "distance": 30123.4,
    "elevationGain": 321.0,
    "averageHR": 139.0,
    "maxHR": 172.0,
    "avgPower": 205.0,
    "calories": 812.0,
}


def sleep_payload(day: str, start_ms: int) -> dict:
    return {
        "dailySleepDTO": {
            "id": start_ms,
            "calendarDate": day,
            "sleepStartTimestampGMT": start_ms,
            "sleepEndTimestampGMT": start_ms + 8 * 3600 * 1000,
            "sleepTimeSeconds": 7 * 3600,
            "awakeSleepSeconds": 3600,
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 14400,
            "remSleepSeconds": 5400,
            "sleepScores": {"overall": {"value": 81}},
        }
    }


class FakeGarmin:
    def __init__(self, fail_on: str | None = None, login_error: Exception | None = None):
        self.fail_on = fail_on
        self.login_error = login_error
        self.calls: list[tuple[str, tuple]] = []

    def login(self, tokenstore: str) -> None:
        self.calls.append(("login", (tokenstore,)))
        if self.login_error:
            raise self.login_error

    def get_activities_by_date(self, start: str, end: str) -> list:
        self.calls.append(("activities", (start, end)))
        return [ACTIVITY, {"activityId": None}]

    def get_stats(self, day: str) -> dict:
        self.calls.append(("stats", (day,)))
        if day == self.fail_on:
            raise ConnectionError("429 Too Many Requests")
        return {
            "totalSteps": 10000 + int(day[-2:]),
            "restingHeartRate": 50,
            "averageStressLevel": -1,
            "bodyBatteryHighestValue": 90,
            "bodyBatteryLowestValue": 20,
        }

    def get_sleep_data(self, day: str) -> dict:
        self.calls.append(("sleep", (day,)))
        if day == "2026-10-02":
            return {"dailySleepDTO": {"calendarDate": day}}  # no sleep recorded
        return sleep_payload(day, 1_790_892_000_000 + int(day[-2:]) * 86_400_000)

    def get_hrv_data(self, day: str) -> dict:
        if day == "2026-10-01":
            raise KeyError("no HRV on this device")
        return {"hrvSummary": {"calendarDate": day, "lastNightAvg": 45}}

    def get_body_composition(self, start: str, end: str) -> dict:
        self.calls.append(("body", (start, end)))
        return {
            "dateWeightList": [
                {"date": 1_790_920_800_000, "weight": 72450.0, "bodyFat": 17.9},
                {"date": 1_791_007_200_000, "weight": 72300.0, "bodyFat": None},
            ]
        }


def make(settings, fake: FakeGarmin, sleeps: list | None = None) -> garmin.GarminProvider:
    sleep = sleeps.append if sleeps is not None else (lambda _: None)
    return garmin.GarminProvider(settings, client_factory=lambda: fake, sleep=sleep)


def test_map_activity() -> None:
    row = garmin.map_activity(ACTIVITY)
    assert row["source_id"] == "987"
    assert row["start_time"] == "2026-10-02T10:00:05Z"
    assert row["local_date"] == "2026-10-02"
    assert row["activity_type"] == "road_biking"
    assert row["moving_s"] == 3600 and row["avg_power_w"] == 205 and row["calories"] == 812


def test_sync_end_to_end(settings, store) -> None:
    fake = FakeGarmin()
    sleeps: list[float] = []
    results = make(settings, fake, sleeps).sync(store, WINDOW)
    assert [(r.stream, r.ok) for r in results] == [("activities", True), ("daily", True), ("body", True)]
    assert {s["stream"] for s in store.sync_states()} == {"login", "activities", "daily", "body"}
    assert fake.calls[0] == ("login", (str(settings.garmin_token_dir),))
    assert ("activities", ("2026-10-01", "2026-10-03")) in fake.calls
    assert sleeps == [0.4, 0.4]  # pause between the three days

    counts = store.row_counts()
    assert counts["activities"] == 1
    assert counts["sleep_sessions"] == 2  # 10-02 had no sleep
    metrics = {
        (r["date"], r["metric"]): r["value"]
        for r in store.conn.execute("SELECT date, metric, value FROM daily_metrics")
    }
    assert metrics[("2026-10-03", "steps")] == 10003
    assert metrics[("2026-10-01", "resting_hr")] == 50
    assert metrics[("2026-10-03", "sleep_score")] == 81
    assert metrics[("2026-10-03", "hrv_rmssd_ms")] == 45
    assert ("2026-10-01", "hrv_rmssd_ms") not in metrics  # HRV error swallowed
    assert not any(m == "avg_stress" for _, m in metrics)  # -1 means no data

    sleep = store.conn.execute("SELECT * FROM sleep_sessions WHERE local_date = '2026-10-03'").fetchone()
    assert sleep["minutes_deep"] == 90 and sleep["minutes_asleep"] == 420 and sleep["is_main"] == 1

    body = {(r["ts"], r["metric"]): r["value"] for r in store.conn.execute("SELECT * FROM body_measurements")}
    assert body[("2026-10-02T06:00:00Z", "weight_kg")] == pytest.approx(72.45)  # grams -> kg
    assert body[("2026-10-02T06:00:00Z", "body_fat_pct")] == 17.9
    assert len(body) == 3
    assert store.get_cursor("garmin", "daily") == "2026-10-03"


def test_daily_stops_on_first_failure_and_resumes(settings, store) -> None:
    fake = FakeGarmin(fail_on="2026-10-02")
    results = {r.stream: r for r in make(settings, fake).sync(store, WINDOW)}
    daily = results["daily"]
    assert not daily.ok and "2026-10-02" in daily.error
    assert results["body"].ok  # later streams still run
    assert ("stats", ("2026-10-03",)) not in fake.calls
    state = {s["stream"]: s for s in store.sync_states()}["daily"]
    assert state["cursor"] == "2026-10-01"  # last fully synced day
    assert "2026-10-02" in state["last_error"]

    # next run resumes from the cursor minus the lookback, not the window start
    later = SyncWindow(date(2026, 1, 1), date(2026, 10, 3))
    fake2 = FakeGarmin()
    make(settings, fake2).sync(store, later)
    days = [args[0] for name, args in fake2.calls if name == "stats"]
    assert days[0] == "2026-09-24" and days[-1] == "2026-10-03"


def test_login_failure_is_recorded(settings, store) -> None:
    fake = FakeGarmin(login_error=RuntimeError("Username and password are required"))
    [result] = make(settings, fake).sync(store, WINDOW)
    assert result.stream == "login" and "fitness-hub auth garmin" in result.error
    assert len(fake.calls) == 1


def test_configured_requires_token_file(settings) -> None:
    provider = make(settings, FakeGarmin())
    assert not provider.configured()
    settings.garmin_token_dir.mkdir(parents=True)
    (settings.garmin_token_dir / garmin.TOKEN_FILE).write_text("{}")
    assert provider.configured()


def test_authorize_saves_tokens_never_password(settings) -> None:
    created = {}

    class FakeLibrary:
        def __init__(self, email, password, prompt_mfa):
            created.update(email=email, password=password)
            self.prompt_mfa = prompt_mfa

        def login(self, tokenstore: str) -> None:
            assert self.prompt_mfa() == "123456"
            Path(tokenstore, garmin.TOKEN_FILE).write_text(json.dumps({"di_token": "t", "di_refresh_token": "r"}))

        def get_full_name(self) -> str:
            return "Test Athlete"

    answers = iter(["me@example.com", "123456"])
    result = garmin.authorize(
        settings,
        client_factory=FakeLibrary,
        prompt=lambda _: next(answers),
        prompt_secret=lambda _: "hunter2-secret",
        echo=lambda _: None,
    )
    assert result["name"] == "Test Athlete"
    assert created == {"email": "me@example.com", "password": "hunter2-secret"}
    token_file = settings.garmin_token_dir / garmin.TOKEN_FILE
    assert (token_file.stat().st_mode & 0o777) == 0o600
    for path in settings.home.rglob("*"):
        if path.is_file():
            assert b"hunter2-secret" not in path.read_bytes()


def test_authorize_login_failure_mentions_csv_fallback(settings) -> None:
    class Broken:
        def __init__(self, **kwargs):
            pass

        def login(self, tokenstore: str) -> None:
            raise RuntimeError("403 Forbidden (bot protection)")

    with pytest.raises(AuthError, match="import-garmin-csv"):
        garmin.authorize(
            settings, client_factory=Broken, prompt=lambda _: "me@example.com", prompt_secret=lambda _: "pw"
        )


# -- CSV import -------------------------------------------------------------------------

CSV_KM = (
    "﻿Activity Type,Date,Favorite,Title,Distance,Calories,Time,Avg HR,Max HR,Total Ascent,Avg Power\n"
    'Cycling,2026-10-02 12:00:05,false,"Lunch Ride, long",30.12,"1,234",1:01:40,139,172,"1,021",205\n'
    "Running,2026-10-03 07:15:00,false,Easy Run,10.00,650,48:30,145,160,--,--\n"
    "Bogus,not-a-date,false,x,1,1,1:00,1,1,1,1\n"
)


def test_csv_import_km(tmp_path: Path, store) -> None:
    path = tmp_path / "Activities.csv"
    path.write_text(CSV_KM, encoding="utf-8")
    assert garmin.import_garmin_csv(store, path) == {"imported": 2, "skipped": 1}
    rows = {r["activity_type"]: dict(r) for r in store.conn.execute("SELECT * FROM activities")}
    ride = rows["Cycling"]
    assert ride["source"] == "garmin_export"
    assert ride["distance_m"] == pytest.approx(30120)
    assert ride["calories"] == 1234 and ride["elevation_gain_m"] == 1021
    assert ride["duration_s"] == 3700 and ride["name"] == "Lunch Ride, long"
    assert ride["start_time"] == "2026-10-02T12:00:05"  # zone unknown: stored naive
    assert ride["local_date"] == "2026-10-02"
    run = rows["Running"]
    assert run["duration_s"] == 48 * 60 + 30 and run["elevation_gain_m"] is None and run["avg_power_w"] is None
    # re-import is idempotent
    garmin.import_garmin_csv(store, path)
    assert store.row_counts()["activities"] == 2


def test_csv_import_miles_and_timezone(tmp_path: Path, store) -> None:
    path = tmp_path / "Activities.csv"
    path.write_text(CSV_KM, encoding="utf-8")
    garmin.import_garmin_csv(store, path, distance_unit="mi", timezone="America/New_York")
    ride = store.conn.execute("SELECT * FROM activities WHERE activity_type = 'Cycling'").fetchone()
    assert ride["distance_m"] == pytest.approx(30.12 * 1609.344)
    assert ride["elevation_gain_m"] == pytest.approx(1021 * 0.3048)
    assert ride["start_time"] == "2026-10-02T16:00:05Z"  # EDT is UTC-4


def test_csv_import_rejects_other_files(tmp_path: Path, store) -> None:
    path = tmp_path / "other.csv"
    path.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="Garmin"):
        garmin.import_garmin_csv(store, path)
    with pytest.raises(ValueError, match="distance_unit"):
        garmin.import_garmin_csv(store, path, distance_unit="furlongs")


def test_parse_hms() -> None:
    assert garmin.parse_hms("1:02:03") == 3723
    assert garmin.parse_hms("02:03") == 123
    assert garmin.parse_hms("00:45.5") == 45.5
    assert garmin.parse_hms("--") is None
