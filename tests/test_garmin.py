from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

from fitness_hub.auth import AuthError
from fitness_hub.db import load_daily_series
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

    def get_heart_rates(self, day: str) -> dict:
        if day == "2026-10-02":
            raise KeyError("no all-day HR for this day")
        midnight_ms = 1_790_812_800_000 + (int(day[-2:]) - 1) * 86_400_000  # 2026-10-01T00:00Z
        return {
            "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
            "heartRateValues": [[midnight_ms + 240_000, 58], [midnight_ms, 60], [midnight_ms + 120_000, None]],
        }

    def get_body_composition(self, start: str, end: str) -> dict:
        self.calls.append(("body", (start, end)))
        return {
            "dateWeightList": [
                {"date": 1_790_920_800_000, "weight": 72450.0, "bodyFat": 17.9},
                {"date": 1_791_007_200_000, "weight": 72300.0, "bodyFat": None},
            ]
        }


def make(settings, fake: FakeGarmin, sleeps: list | None = None, activities: bool = True) -> garmin.GarminProvider:
    sleep = sleeps.append if sleeps is not None else (lambda _: None)
    settings = dataclasses.replace(settings, garmin_activities=activities)
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

    # All-day HR: sorted by time, empty samples dropped; a failing day only loses its HR.
    hr = load_daily_series(store.conn, "garmin", "2026-10-01", "heart_rate")
    assert hr == {"time": [1_790_812_800, 1_790_813_040], "heart_rate": [60, 58]}
    assert load_daily_series(store.conn, "garmin", "2026-10-02", "heart_rate") is None
    assert load_daily_series(store.conn, "garmin", "2026-10-03", "heart_rate") is not None
    assert len(body) == 3
    assert store.get_cursor("garmin", "daily") == "2026-10-03"


def test_activities_are_skipped_by_default(settings, store) -> None:
    fake = FakeGarmin()
    results = make(settings, fake, activities=False).sync(store, WINDOW)
    assert [r.stream for r in results] == ["daily", "body"]
    assert not any(name == "activities" for name, _ in fake.calls)
    assert store.row_counts()["activities"] == 0


def test_map_heart_rates() -> None:
    swapped = {
        "heartRateValueDescriptors": [{"key": "heartrate", "index": 0}, {"key": "timestamp", "index": 1}],
        "heartRateValues": [[61, 1_790_812_800_000], "junk", [None, 1_790_812_920_000]],
    }
    row = garmin.map_heart_rates("2026-10-01", swapped)
    assert row["streams"] == {"time": [1_790_812_800], "heart_rate": [61]}
    assert row["series"] == "heart_rate" and row["source"] == "garmin"
    assert garmin.map_heart_rates("2026-10-01", {"heartRateValues": None}) is None
    assert garmin.map_heart_rates("2026-10-01", {"heartRateValues": [[1, None]]}) is None
    assert garmin.map_heart_rates("2026-10-01", None) is None


def test_daily_stops_on_first_failure_and_resumes(settings, store) -> None:
    fake = FakeGarmin(fail_on="2026-10-02")
    results = {r.stream: r for r in make(settings, fake).sync(store, WINDOW)}
    daily = results["daily"]
    assert not daily.ok and "2026-10-02" in daily.error
    assert results["body"].ok  # later streams still run
    assert ("stats", ("2026-10-03",)) not in fake.calls
    state = {s["stream"]: s for s in store.sync_states()}["daily"]
    assert state["cursor"] == "2026-10-01/partial"  # last fully synced day
    assert "2026-10-02" in state["last_error"]

    # next run resumes on the day it stopped (no lookback, not the window start)
    later = SyncWindow(date(2026, 1, 1), date(2026, 10, 3))
    fake2 = FakeGarmin()
    make(settings, fake2).sync(store, later)
    days = [args[0] for name, args in fake2.calls if name == "stats"]
    assert days == ["2026-10-02", "2026-10-03"]
    assert store.get_cursor("garmin", "daily") == "2026-10-03"  # complete: a normal cursor again


class ThrottledGarmin(FakeGarmin):
    """Allows `budget` days of stats per run, then raises like Garmin's throttle."""

    def __init__(self, budget: int):
        super().__init__()
        self.budget = budget

    def get_stats(self, day: str) -> dict:
        if self.budget == 0:
            raise ConnectionError("429 Too Many Requests")
        self.budget -= 1
        return super().get_stats(day)


def test_throttled_backfill_always_moves_forward(settings, store) -> None:
    # lookback (7 days) is larger than what Garmin allows per run (3 days)
    window = SyncWindow(date(2026, 9, 1), date(2026, 9, 20))
    fetched: list[str] = []
    for _ in range(10):
        fake = ThrottledGarmin(budget=3)
        results = {r.stream: r for r in make(settings, fake).sync(store, window)}
        fetched += [args[0] for name, args in fake.calls if name == "stats"]
        if results["daily"].ok:
            break
    days = sorted({d for d in fetched})
    assert results["daily"].ok
    assert days[0] == "2026-09-01" and days[-1] == "2026-09-20" and len(days) == 20
    assert store.get_cursor("garmin", "daily") == "2026-09-20"


def test_partial_cursor_never_skips_or_rewinds_window_start() -> None:
    window = SyncWindow(date(2026, 9, 1), date(2026, 9, 20))
    assert window.start_for("2026-09-05/partial", 7, False) == date(2026, 9, 6)
    assert window.start_for("2026-09-20/partial", 7, False) == date(2026, 9, 20)  # clamped to end
    assert window.start_for("2026-09-05/partial", 7, True) == date(2026, 9, 1)  # --full restarts


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


class GarminConnectConnectionError(Exception):
    """Same name as garminconnect's exception (the provider matches by class name)."""


class GarminConnectAuthenticationError(Exception):
    pass


class GarminConnectTooManyRequestsError(Exception):
    pass


class FlakyGarmin(FakeGarmin):
    def __init__(self, stats_errors: dict[str, Exception], hrv_errors: dict[str, Exception] | None = None):
        super().__init__()
        self.stats_errors = stats_errors
        self.hrv_errors = hrv_errors or {}

    def get_stats(self, day: str) -> dict:
        if day in self.stats_errors:
            self.calls.append(("stats", (day,)))
            raise self.stats_errors[day]
        return super().get_stats(day)

    def get_hrv_data(self, day: str) -> dict:
        if day in self.hrv_errors:
            raise self.hrv_errors[day]
        return super().get_hrv_data(day)


def test_days_without_data_do_not_stall_the_daily_stream(settings, store) -> None:
    # garminconnect raises these for a day with no summary (e.g. before the watch was owned)
    # and for a privacy-protected day: skip the day's stats, keep going.
    fake = FlakyGarmin(
        {
            "2026-10-01": GarminConnectConnectionError("No data received from server"),
            "2026-10-02": GarminConnectAuthenticationError("Authentication error"),
        }
    )
    results = {r.stream: r for r in make(settings, fake).sync(store, WINDOW)}
    assert results["daily"].ok
    days = [args[0] for name, args in fake.calls if name == "stats"]
    assert days == ["2026-10-01", "2026-10-02", "2026-10-03"]
    steps = store.conn.execute("SELECT date FROM daily_metrics WHERE metric = 'steps'").fetchall()
    assert [r[0] for r in steps] == ["2026-10-03"]
    assert store.row_counts()["sleep_sessions"] == 2  # sleep for the empty days is still stored
    assert store.get_cursor("garmin", "daily") == "2026-10-03"


@pytest.mark.parametrize(
    "error",
    [
        GarminConnectTooManyRequestsError("Rate limit exceeded"),
        GarminConnectAuthenticationError("Authentication failed: 401"),
        GarminConnectConnectionError("API client error (500)"),
    ],
)
def test_throttling_auth_and_server_errors_stop_at_that_day(settings, store, error) -> None:
    fake = FlakyGarmin({"2026-10-02": error})
    results = {r.stream: r for r in make(settings, fake).sync(store, WINDOW)}
    assert not results["daily"].ok and "2026-10-02" in results["daily"].error
    assert store.get_cursor("garmin", "daily") == "2026-10-01/partial"


def test_hrv_throttling_stops_but_other_hrv_errors_are_skipped(settings, store) -> None:
    fake = FlakyGarmin({}, hrv_errors={"2026-10-02": GarminConnectTooManyRequestsError("Rate limit exceeded")})
    results = {r.stream: r for r in make(settings, fake).sync(store, WINDOW)}
    assert not results["daily"].ok
    assert store.get_cursor("garmin", "daily") == "2026-10-01/partial"  # 10-02 will be fetched again
    # FakeGarmin raises KeyError for HRV on 10-01: that day was still stored
    assert store.conn.execute("SELECT COUNT(*) FROM daily_metrics WHERE date = '2026-10-01'").fetchone()[0] > 0


def test_classify_error() -> None:
    assert garmin.classify_error(GarminConnectTooManyRequestsError("x")) == "throttled"
    assert garmin.classify_error(ConnectionError("429 Too Many Requests")) == "throttled"
    assert garmin.classify_error(GarminConnectConnectionError("No data received from server")) == "no_data"
    assert garmin.classify_error(GarminConnectAuthenticationError("Authentication error")) == "no_data"
    assert garmin.classify_error(GarminConnectAuthenticationError("Authentication failed: x")) == "auth"
    assert garmin.classify_error(GarminConnectConnectionError("timeout")) == "error"


def test_body_uses_utc_timestamp_over_local_date() -> None:
    # `date` is local wall-clock time as epoch ms (UTC-7: 22:00 local on Oct 10);
    # timestampGMT is the real instant (05:00Z on Oct 11).
    rows = garmin.map_body(
        {"dateWeightList": [{"date": 1_791_669_600_000, "timestampGMT": 1_791_694_800_000, "weight": 70000.0}]}
    )
    assert rows[0]["ts"] == "2026-10-11T05:00:00Z"
    assert garmin.map_body({"dateWeightList": [{"date": 1_791_669_600_000, "weight": 70000.0}]})[0]["ts"] == (
        "2026-10-10T22:00:00Z"
    )


def test_csv_swim_distances_are_metres_or_yards(tmp_path: Path, store) -> None:
    path = tmp_path / "Activities.csv"
    path.write_text(
        "Activity Type,Date,Title,Distance,Calories,Time\n"
        'Pool Swim,2026-10-02 06:00:00,Swim,"1,500",400,0:30:00\n'
        "Running,2026-10-03 07:00:00,Run,10.5,650,0:50:00\n",
        encoding="utf-8",
    )
    garmin.import_garmin_csv(store, path)
    got = dict(store.conn.execute("SELECT activity_type, distance_m FROM activities").fetchall())
    assert got == {"Pool Swim": 1500.0, "Running": 10500.0}
    garmin.import_garmin_csv(store, path, distance_unit="mi")
    got = dict(store.conn.execute("SELECT activity_type, distance_m FROM activities").fetchall())
    assert got["Pool Swim"] == pytest.approx(1500 * 0.9144)
    assert got["Running"] == pytest.approx(10.5 * 1609.344)
