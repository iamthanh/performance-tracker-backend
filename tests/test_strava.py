from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

import httpx

from conftest import json_response, mock_client, no_sleep
from fitness_hub.providers import SyncWindow
from fitness_hub.providers.strava import StravaProvider, authorize, map_activity

NOW = 1_800_000_000.0
WINDOW = SyncWindow(date(2026, 9, 14), date(2026, 10, 14))


def activity(i: int, day: int = 1) -> dict:
    return {
        "id": 1000 + i,
        "name": f"Ride {i}",
        "sport_type": "Ride",
        "type": "Ride",
        "start_date": f"2026-10-{day:02d}T07:{i % 60:02d}:00Z",
        "start_date_local": f"2026-10-{day:02d}T09:{i % 60:02d}:00Z",
        "elapsed_time": 3700,
        "moving_time": 3600,
        "distance": 30000.5,
        "total_elevation_gain": 250,
        "average_heartrate": 140.2,
        "max_heartrate": 171,
        "average_watts": 201.5,
    }


class FakeStrava:
    def __init__(self, pages: list, refresh_token: str = "rotated-refresh"):
        self.pages = pages
        self.refresh_token = refresh_token
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["old-refresh"]
            return json_response(
                {"access_token": "new-access", "refresh_token": self.refresh_token, "expires_at": int(NOW) + 21600}
            )
        assert request.url.path == "/api/v3/athlete/activities"
        assert request.headers["Authorization"] == "Bearer new-access"
        page = int(request.url.params["page"])
        result = self.pages[page - 1] if page <= len(self.pages) else []
        return result if isinstance(result, httpx.Response) else json_response(result)

    def activity_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/activities")]


def make_provider(settings, tokens, fake) -> StravaProvider:
    tokens.save("strava", {"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": NOW - 10})
    return StravaProvider(settings, tokens, client=mock_client(fake), sleep=no_sleep, now=lambda: NOW)


def test_map_activity() -> None:
    row = map_activity(activity(1))
    assert row["source"] == "strava" and row["source_id"] == "1001"
    assert row["start_time"] == "2026-10-01T07:01:00Z"
    assert row["local_date"] == "2026-10-01"
    assert row["activity_type"] == "Ride"
    assert row["distance_m"] == 30000.5 and row["moving_s"] == 3600 and row["duration_s"] == 3700
    assert row["avg_power_w"] == 201.5 and row["calories"] is None
    assert map_activity({"id": 1}) is None


def test_refresh_rotates_and_persists_token(settings, tokens, store) -> None:
    fake = FakeStrava([[activity(1)]])
    provider = make_provider(settings, tokens, fake)
    [result] = provider.sync(store, WINDOW)
    assert result.ok and result.rows == 1
    saved = tokens.get("strava")
    assert saved["access_token"] == "new-access"
    assert saved["refresh_token"] == "rotated-refresh"
    assert saved["expires_at"] == int(NOW) + 21600
    # the next call reuses the fresh token instead of refreshing again
    provider.sync(store, WINDOW)
    assert sum(r.url.path == "/oauth/token" for r in fake.requests) == 1


def test_two_page_pagination(settings, tokens, store) -> None:
    page1 = [activity(i, day=1 + i % 9) for i in range(200)]
    page2 = [activity(200 + i, day=10) for i in range(3)]
    fake = FakeStrava([page1, page2])
    [result] = make_provider(settings, tokens, fake).sync(store, WINDOW)
    assert result.ok and result.rows == 203
    pages = [int(r.url.params["page"]) for r in fake.activity_requests()]
    assert pages == [1, 2]
    params = fake.activity_requests()[0].url.params
    assert params["per_page"] == "200"
    assert params["after"] == str(int(1_789_344_000))  # 2026-09-14T00:00:00Z
    assert store.row_counts()["activities"] == 203
    assert store.get_cursor("strava", "activities") == "2026-10-10T07:22:00Z"


def test_incremental_rerun_is_idempotent(settings, tokens, store) -> None:
    fake = FakeStrava([[activity(1, day=5), activity(2, day=8)]])
    provider = make_provider(settings, tokens, fake)
    provider.sync(store, WINDOW)
    counts = store.row_counts()
    provider.sync(store, WINDOW)
    assert store.row_counts() == counts
    second_after = int(fake.activity_requests()[-1].url.params["after"])
    # cursor 2026-10-08 minus 7 days lookback = 2026-10-01T00:00:00Z
    assert second_after == 1_790_812_800


def test_rate_limit_is_recorded_not_raised(settings, tokens, store) -> None:
    page1 = [activity(i, day=2) for i in range(200)]
    fake = FakeStrava([page1, httpx.Response(429, text="Rate Limit Exceeded")])
    [result] = make_provider(settings, tokens, fake).sync(store, WINDOW)
    assert not result.ok and "rate limited" in result.error
    assert result.rows == 200
    assert store.row_counts()["activities"] == 200  # rows already written are kept
    state = store.sync_states()[0]
    assert state["cursor"] is None  # no cursor advance on a partial page set
    assert "rate limited" in state["last_error"]
    assert len(fake.activity_requests()) == 2  # 429 is not retried


def test_refresh_rejected_is_actionable(settings, tokens, store) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"message": "Bad Request"}, status=400)

    tokens.save("strava", {"access_token": "a", "refresh_token": "old-refresh", "expires_at": 0})
    provider = StravaProvider(settings, tokens, client=mock_client(handler), sleep=no_sleep, now=lambda: NOW)
    [result] = provider.sync(store, WINDOW)
    assert "fitness-hub auth strava" in result.error


def test_not_configured_without_tokens(settings, tokens) -> None:
    assert not StravaProvider(settings, tokens).configured()


def test_authorize_exchanges_code_and_saves_tokens(settings, tokens) -> None:
    seen = {}

    def flow(url, port, state, **kwargs):
        seen["url"] = url
        return {"code": "the-code", "state": state, "scope": "read,activity:read_all,profile:read_all"}

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["code"] == ["the-code"] and form["grant_type"] == ["authorization_code"]
        return json_response(
            {"access_token": "a1", "refresh_token": "r1", "expires_at": 1_900_000_000, "athlete": {"id": 42}}
        )

    result = authorize(settings, tokens, client=mock_client(handler), flow=flow, echo=lambda _: None)
    assert result == {"athlete_id": 42, "scope": "read,activity:read_all,profile:read_all"}
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback" in seen["url"]
    assert "activity%3Aread_all" in seen["url"]
    saved = tokens.get("strava")
    assert saved["refresh_token"] == "r1" and saved["athlete_id"] == 42
