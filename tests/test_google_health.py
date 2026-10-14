from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import json_response, mock_client, no_sleep
from fitness_hub.auth import AuthError
from fitness_hub.providers import SyncWindow
from fitness_hub.providers import google_health as gh

NOW = 1_800_000_000.0
WINDOW = SyncWindow(date(2026, 9, 2), date(2026, 10, 14))  # 43 days inclusive


def d(day: date) -> dict:
    return {"year": day.year, "month": day.month, "day": day.day}


EXERCISE_1 = {
    "name": "users/123/dataTypes/exercise/dataPoints/ex1",
    "exercise": {
        "interval": {
            "startTime": "2026-10-01T07:00:00Z",
            "endTime": "2026-10-01T08:00:30Z",
            "civilStartTime": "2026-10-01T09:00:00",
        },
        "exerciseType": "OUTDOOR_BIKE",
        "displayName": "Bike",
        "activeDuration": "3500s",
        "metricsSummary": {
            "caloriesKcal": 650.5,
            "distanceMillimeters": "30000000",
            "averageHeartRateBeatsPerMinute": "141",
            "elevationGainMillimeters": "250000",
        },
    },
}
SLEEP_1 = {
    "name": "users/123/dataTypes/sleep/dataPoints/sl1",
    "sleep": {
        "interval": {
            "startTime": "2026-10-01T22:30:00Z",
            "endTime": "2026-10-02T06:30:00Z",
            "civilEndTime": {"date": {"year": 2026, "month": 10, "day": 2}, "time": {"hours": 8}},
        },
        "type": "STAGES",
        "metadata": {"mainSleep": True, "nap": False},
        "summary": {
            "minutesAsleep": "440",
            "minutesAwake": "40",
            "stagesSummary": [
                {"type": "DEEP", "minutes": "80", "count": "3"},
                {"type": "LIGHT", "minutes": "250", "count": "20"},
                {"type": "REM", "minutes": "110", "count": "5"},
                {"type": "AWAKE", "minutes": "40", "count": "9"},
            ],
        },
    },
}
OCT1 = date(2026, 10, 1)
DAILY = {
    "daily-resting-heart-rate": {"dailyRestingHeartRate": {"date": d(OCT1), "beatsPerMinute": "52"}},
    "daily-heart-rate-variability": {
        "dailyHeartRateVariability": {"date": d(OCT1), "averageHeartRateVariabilityMilliseconds": 48.2}
    },
    "daily-oxygen-saturation": {"dailyOxygenSaturation": {"date": d(OCT1), "averagePercentage": 96.5}},
    "daily-respiratory-rate": {"dailyRespiratoryRate": {"date": d(OCT1), "breathsPerMinute": 14.2}},
}
SAMPLES = {
    "weight": {"weight": {"sampleTime": {"physicalTime": "2026-10-01T06:00:00Z"}, "weightGrams": "72500"}},
    "body-fat": {"bodyFat": {"sampleTime": {"physicalTime": "2026-10-01T06:00:00Z"}, "percentage": 18.5}},
}
ROLLUP_PAYLOADS = {
    "steps": {"steps": {"countSum": "8430"}},
    "distance": {"distance": {"millimetersSum": "5000000"}},
    "total-calories": {"totalCalories": {"kcalSum": 2300.5}},
    "floors": {"floors": {"countSum": "10"}},
    "active-zone-minutes": {
        "activeZoneMinutes": {"sumInCardioHeartZone": "10", "sumInPeakHeartZone": "5", "sumInFatBurnHeartZone": "20"}
    },
}
LIST_RE = re.compile(r"^/v4/users/me/dataTypes/([a-z0-9-]+)/dataPoints(:dailyRollUp)?$")


def civil_to_date(civil: dict) -> date:
    return date(civil["date"]["year"], civil["date"]["month"], civil["date"]["day"])


class FakeGoogle:
    def __init__(self, forbidden: set[str] = frozenset(), token_error: dict | None = None):
        self.forbidden = forbidden
        self.token_error = token_error
        self.lists: dict[str, list[httpx.Request]] = defaultdict(list)
        self.rollups: dict[str, list[dict]] = defaultdict(list)
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            if self.token_error:
                return json_response(self.token_error, status=400)
            return json_response({"access_token": "fresh", "expires_in": 3599, "token_type": "Bearer"})
        assert request.headers["Authorization"] == "Bearer fresh"
        match = LIST_RE.match(request.url.path)
        assert match, request.url.path
        data_type, rollup = match.groups()
        if data_type in self.forbidden:
            return json_response({"error": {"code": 403, "status": "PERMISSION_DENIED"}}, status=403)
        if rollup:
            body = json.loads(request.content)
            self.rollups[data_type].append(body)
            start, end = civil_to_date(body["range"]["start"]), civil_to_date(body["range"]["end"])
            points = []
            day = start
            while day < end:
                points.append({"civilStartTime": {"date": d(day), "time": {}}, **ROLLUP_PAYLOADS[data_type]})
                day += timedelta(days=1)
            return json_response({"rollupDataPoints": points})
        self.lists[data_type].append(request)
        params = request.url.params
        if data_type == "exercise":
            if "pageToken" not in params:
                return json_response({"dataPoints": [EXERCISE_1], "nextPageToken": "p2"})
            second = json.loads(json.dumps(EXERCISE_1))
            second["name"] = "users/123/dataTypes/exercise/dataPoints/ex0"
            second["exercise"]["interval"]["startTime"] = "2026-09-20T07:00:00Z"
            second["exercise"]["interval"]["endTime"] = "2026-09-20T07:30:00Z"
            second["exercise"]["interval"]["civilStartTime"] = "2026-09-20T09:00:00"
            return json_response({"dataPoints": [second]})
        if data_type == "sleep":
            return json_response({"dataPoints": [SLEEP_1]})
        if data_type in DAILY:
            return json_response({"dataPoints": [DAILY[data_type]]})
        if data_type in SAMPLES:
            return json_response({"dataPoints": [SAMPLES[data_type]]})
        return json_response({})


def provider(settings, tokens, fake: FakeGoogle) -> gh.GoogleHealthProvider:
    tokens.save("google_health", {"access_token": "stale", "refresh_token": "r", "expires_at": NOW - 1})
    return gh.GoogleHealthProvider(settings, tokens, client=mock_client(fake), sleep=no_sleep, now=lambda: NOW)


# -- mappers -------------------------------------------------------------------------


def test_naming_helpers() -> None:
    assert gh.snake("body-fat") == "body_fat"
    assert gh.camel("body-fat") == "bodyFat"
    assert gh.camel("daily-heart-rate-variability") == "dailyHeartRateVariability"


def test_map_exercise() -> None:
    row = gh.map_exercise(EXERCISE_1)
    assert row["source_id"] == "ex1"
    assert row["start_time"] == "2026-10-01T07:00:00Z"
    assert row["local_date"] == "2026-10-01"
    assert row["duration_s"] == 3630 and row["moving_s"] == 3500
    assert row["distance_m"] == 30000 and row["elevation_gain_m"] == 250
    assert row["avg_hr"] == 141 and row["calories"] == 650.5
    assert row["activity_type"] == "OUTDOOR_BIKE" and row["name"] == "Bike"


def test_map_sleep() -> None:
    row = gh.map_sleep(SLEEP_1)
    assert row["local_date"] == "2026-10-02"  # date the sleep ended
    assert (row["minutes_deep"], row["minutes_light"], row["minutes_rem"]) == (80, 250, 110)
    assert row["minutes_asleep"] == 440 and row["minutes_awake"] == 40
    assert row["is_main"] == 1


def test_map_daily_and_samples() -> None:
    hr = gh.map_daily("daily-resting-heart-rate", DAILY["daily-resting-heart-rate"])
    assert hr == {**hr, "date": "2026-10-01", "metric": "resting_hr", "value": 52.0}
    hrv = gh.map_daily("daily-heart-rate-variability", DAILY["daily-heart-rate-variability"])
    assert hrv["metric"] == "hrv_rmssd_ms" and hrv["value"] == 48.2
    weight = gh.map_sample("weight", SAMPLES["weight"])
    assert weight["metric"] == "weight_kg" and weight["value"] == 72.5 and weight["ts"] == "2026-10-01T06:00:00Z"
    fat = gh.map_sample("body-fat", SAMPLES["body-fat"])
    assert fat["metric"] == "body_fat_pct" and fat["value"] == 18.5


def test_unverified_field_falls_back_to_first_numeric(caplog: pytest.LogCaptureFixture) -> None:
    dp = {"dailyVo2Max": {"date": d(OCT1), "cardioFitnessScore": "47.5"}}
    with caplog.at_level(logging.WARNING):
        row = gh.map_daily("daily-vo2-max", dp)
    assert row["value"] == 47.5
    assert "cardioFitnessScore" in caplog.text
    assert row["raw"] == dp


def test_rollup_values() -> None:
    def value(t: str) -> float:
        return gh.map_rollup(t, {"civilStartTime": {"date": d(OCT1)}, **ROLLUP_PAYLOADS[t]})["value"]

    assert value("steps") == 8430
    assert value("distance") == 5000
    assert value("total-calories") == 2300.5
    assert value("floors") == 10
    assert value("active-zone-minutes") == 35


def test_rollup_chunks_respect_limits() -> None:
    chunks = gh.rollup_chunks(WINDOW.start, WINDOW.end, 14)
    assert len(chunks) == 4
    assert chunks[0] == (date(2026, 9, 2), date(2026, 9, 16))
    assert chunks[-1][1] == date(2026, 10, 15)  # closed-open: end is exclusive
    assert all((b - a).days <= 14 for a, b in chunks)
    assert len(gh.rollup_chunks(WINDOW.start, WINDOW.end, 90)) == 1


# -- end to end ------------------------------------------------------------------------


def test_sync_end_to_end(settings, tokens, store) -> None:
    fake = FakeGoogle(forbidden={"daily-vo2-max"})
    results = provider(settings, tokens, fake).sync(store, WINDOW)
    by_stream = {r.stream: r for r in results}

    # one failing stream (403) does not block the others
    assert not by_stream["daily-vo2-max"].ok and "403" in by_stream["daily-vo2-max"].error
    assert all(r.ok for s, r in by_stream.items() if s != "daily-vo2-max"), results
    assert fake.token_calls == 1 and tokens.get("google_health")["access_token"] == "fresh"

    # pagination token and filters
    ex = fake.lists["exercise"]
    assert [r.url.params.get("pageToken") for r in ex] == [None, "p2"]
    assert ex[0].url.params["pageSize"] == "25"
    assert ex[0].url.params["filter"] == 'exercise.interval.civil_start_time >= "2026-09-02"'
    assert fake.lists["sleep"][0].url.params["filter"] == 'sleep.interval.civil_end_time >= "2026-09-02"'
    assert fake.lists["daily-resting-heart-rate"][0].url.params["filter"] == (
        'daily_resting_heart_rate.date >= "2026-09-02" AND daily_resting_heart_rate.date < "2026-10-15"'
    )
    assert "pageSize" not in fake.lists["daily-resting-heart-rate"][0].url.params
    assert fake.lists["body-fat"][0].url.params["filter"] == (
        'body_fat.sample_time.physical_time >= "2026-09-02T00:00:00Z"'
    )
    for requests in fake.lists.values():
        for request in requests:
            filter_ = request.url.params["filter"]
            field_names = re.findall(r"([a-z_.]+) (?:>=|<) ", filter_)
            assert field_names and all("-" not in f for f in field_names)
            assert " OR " not in filter_ and "<=" not in filter_
            assert set(re.findall(r"[<>=!]+", filter_)) <= {">=", "<"}

    # rollups chunked to <=14 days for total-calories and <=90 days otherwise
    assert len(fake.rollups["total-calories"]) == 4
    assert len(fake.rollups["steps"]) == 1
    for data_type, bodies in fake.rollups.items():
        limit = 14 if data_type == "total-calories" else 90
        for body in bodies:
            span = civil_to_date(body["range"]["end"]) - civil_to_date(body["range"]["start"])
            assert 0 < span.days <= limit
            assert body["windowSizeDays"] == 1

    # stored data
    counts = store.row_counts()
    assert counts["activities"] == 2 and counts["sleep_sessions"] == 1 and counts["body_measurements"] == 2
    metric = {
        (r["metric"]): r["value"]
        for r in store.conn.execute("SELECT metric, value FROM daily_metrics WHERE date = '2026-10-01'")
    }
    assert metric == {
        "steps": 8430,
        "distance_m": 5000,
        "total_calories_kcal": 2300.5,
        "floors": 10,
        "active_zone_minutes": 35,
        "resting_hr": 52,
        "hrv_rmssd_ms": 48.2,
        "spo2_pct": 96.5,
        "respiratory_rate_bpm": 14.2,
    }
    assert store.conn.execute("SELECT COUNT(*) FROM daily_metrics WHERE metric = 'steps'").fetchone()[0] == 43
    states = {s["stream"]: s for s in store.sync_states()}
    assert states["rollup:steps"]["cursor"] == "2026-10-14"
    assert states["daily-vo2-max"]["last_error"] and states["daily-vo2-max"]["cursor"] is None


def test_incremental_rerun_is_idempotent(settings, tokens, store) -> None:
    fake = FakeGoogle()
    p = provider(settings, tokens, fake)
    p.sync(store, WINDOW)
    counts = store.row_counts()
    p.sync(store, WINDOW)
    assert store.row_counts() == counts
    # second run starts at cursor (2026-10-14) minus 7 days of lookback
    assert fake.lists["exercise"][-1].url.params["filter"] == 'exercise.interval.civil_start_time >= "2026-10-07"'
    assert len(fake.rollups["total-calories"]) == 4 + 1


def test_invalid_grant_is_actionable(settings, tokens, store) -> None:
    fake = FakeGoogle(token_error={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    results = provider(settings, tokens, fake).sync(store, WINDOW)
    assert len(results) == 1 and results[0].stream == "auth"
    assert "fitness-hub auth google" in results[0].error and "7 days" in results[0].error
    assert not fake.lists and not fake.rollups


def test_authorize_with_pkce_and_identity(settings, tokens) -> None:
    seen: dict = {}

    def flow(url, port, state, **kwargs):
        seen["url"] = url
        return {"code": "c0de", "state": state}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            seen["form"] = form
            return json_response(
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 3599,
                    "scope": " ".join(gh.SCOPES),
                }
            )
        assert request.url.path == "/v4/users/me/identity"
        return json_response({"legacyUserId": "ABC123", "healthUserId": "h-1"})

    result = gh.authorize(settings, tokens, client=mock_client(handler), flow=flow, echo=lambda _: None, now=lambda: NOW)
    assert result == {"health_user_id": "h-1", "missing_scopes": []}
    url_params = parse_qs(seen["url"].split("?", 1)[1])
    assert url_params["access_type"] == ["offline"] and url_params["prompt"] == ["consent"]
    assert url_params["code_challenge_method"] == ["S256"]
    assert url_params["redirect_uri"] == ["http://localhost:8765/callback"]
    assert seen["form"]["code_verifier"][0] and seen["form"]["redirect_uri"] == ["http://localhost:8765/callback"]
    saved = tokens.get("google_health")
    assert saved["refresh_token"] == "rt" and saved["health_user_id"] == "h-1"
    assert saved["expires_at"] == int(NOW + 3599)


def test_authorize_account_not_linked(settings, tokens) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return json_response({"access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": ""})
        return json_response(
            {"error": {"code": 400, "status": "FAILED_PRECONDITION", "details": [{"reason": "ACCOUNT_NOT_LINKED"}]}},
            status=400,
        )

    with pytest.raises(AuthError, match="Google Health"):
        gh.authorize(
            settings,
            tokens,
            client=mock_client(handler),
            flow=lambda url, port, state, **kw: {"code": "c", "state": state},
            echo=lambda _: None,
        )
