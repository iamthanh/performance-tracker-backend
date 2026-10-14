"""Acceptance checks: a killed sync leaves a consistent DB and resumes;
secrets never reach the logs."""

from __future__ import annotations

import logging
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import json_response, mock_client, no_sleep
from fitness_hub.providers import SyncWindow
from fitness_hub.providers.google_health import GoogleHealthProvider
from fitness_hub.providers.strava import StravaProvider

NOW = 1_800_000_000.0
WINDOW = SyncWindow(date(2026, 9, 14), date(2026, 10, 14))


def strava_activity(i: int) -> dict:
    return {
        "id": i,
        "name": f"Ride {i}",
        "sport_type": "Ride",
        "start_date": f"2026-10-{1 + i % 9:02d}T07:{i % 60:02d}:00Z",
        "start_date_local": f"2026-10-{1 + i % 9:02d}T09:{i % 60:02d}:00",
        "distance": 1000.0,
    }


class Killed(BaseException):
    """Stands in for SIGINT/SIGKILL: not an Exception, so nothing swallows it."""


def test_killed_sync_leaves_consistent_db_and_resumes(settings, tokens, store) -> None:
    pages = {1: [strava_activity(i) for i in range(200)], 2: [strava_activity(200 + i) for i in range(5)]}
    kill_on_page = {2}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page in kill_on_page:
            raise Killed()
        return json_response(pages.get(page, []))

    tokens.save("strava", {"access_token": "a", "refresh_token": "r", "expires_at": NOW + 3600})
    provider = StravaProvider(settings, tokens, client=mock_client(handler), sleep=no_sleep, now=lambda: NOW)

    with pytest.raises(Killed):
        provider.sync(store, WINDOW)
    # page 1 was committed as a whole; nothing claims the stream finished
    assert store.row_counts()["activities"] == 200
    assert store.get_cursor("strava", "activities") is None
    assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    kill_on_page.clear()
    [result] = provider.sync(store, WINDOW)
    assert result.ok and result.rows == 205
    assert store.row_counts()["activities"] == 205
    assert store.get_cursor("strava", "activities") is not None


def test_secrets_never_logged(settings, tokens, store, caplog: pytest.LogCaptureFixture) -> None:
    secrets_ = {"old-refresh-SECRET", "new-access-SECRET", "rotated-refresh-SECRET", "strava-secret", "google-secret"}

    def strava(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            assert parse_qs(request.content.decode())["client_secret"] == ["strava-secret"]
            return json_response(
                {"access_token": "new-access-SECRET", "refresh_token": "rotated-refresh-SECRET", "expires_at": NOW + 99}
            )
        if request.url.params["page"] == "1":
            return httpx.Response(503)  # forces a retry warning
        return httpx.Response(429, text="Rate Limit Exceeded")

    def google(request: httpx.Request) -> httpx.Response:
        return json_response({"error": "invalid_grant"}, status=400)

    tokens.save("strava", {"access_token": "x", "refresh_token": "old-refresh-SECRET", "expires_at": 0})
    tokens.save("google_health", {"access_token": "y", "refresh_token": "old-refresh-SECRET", "expires_at": 0})
    with caplog.at_level(logging.DEBUG):
        StravaProvider(settings, tokens, client=mock_client(strava), sleep=no_sleep, now=lambda: NOW).sync(store, WINDOW)
        GoogleHealthProvider(settings, tokens, client=mock_client(google), sleep=no_sleep, now=lambda: NOW).sync(
            store, WINDOW
        )
    assert caplog.records  # warnings were emitted, so the check below means something
    logged = caplog.text + " ".join(str(s) for s in store.sync_states())
    for secret in secrets_:
        assert secret not in logged
