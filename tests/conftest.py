from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from fitness_hub.auth import TokenStore
from fitness_hub.config import Settings
from fitness_hub.db import Store


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real HTTP attempt fails the test: everything goes through MockTransport."""

    def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected real HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture(autouse=True)
def _restore_logging():
    """The CLI points the root logger at the current stderr (pytest's capture);
    restore it so later tests never log into a closed capture stream."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path / "home",
        strava_client_id="strava-id",
        strava_client_secret="strava-secret",
        google_client_id="google-id",
        google_client_secret="google-secret",
        backfill_days=30,
        lookback_days=7,
    )


@pytest.fixture
def store(settings: Settings):
    with Store(settings.db_path) as s:
        yield s


@pytest.fixture
def tokens(settings: Settings) -> TokenStore:
    return TokenStore(settings.tokens_path)


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler) -> httpx.Client:
    from fitness_hub.http import new_client

    return new_client(httpx.MockTransport(handler))


def no_sleep(_: float) -> None:
    return None


# -- fixtures for the CLI and MCP server tests -------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated FITNESS_HUB_HOME used by Settings.from_env() (CLI and server)."""
    home = tmp_path / "home"
    monkeypatch.setenv("FITNESS_HUB_HOME", str(home))
    monkeypatch.chdir(tmp_path)  # no stray ./.env
    for key in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def env_settings(home: Path) -> Settings:
    """Settings as the CLI and MCP server load them (from the environment)."""
    return Settings.from_env()


def seed_activity(source: str, source_id: str, start: str, activity_type: str, distance_m: float, **extra: Any) -> dict:
    return {
        "source": source,
        "source_id": source_id,
        "start_time": start,
        "local_date": start[:10],
        "activity_type": activity_type,
        "name": f"{activity_type} {source_id}",
        "distance_m": distance_m,
        "moving_s": 3600,
        "elevation_gain_m": 300,
        "avg_hr": 140,
        "raw": {"id": source_id},
        **extra,
    }


@pytest.fixture
def seeded(env_settings: Settings) -> Settings:
    """A database where one ride was recorded by both Garmin and Strava."""
    with Store(env_settings.db_path) as store:
        store.upsert_activities(
            [
                seed_activity("garmin", "g1", "2026-10-06T07:00:05Z", "road_biking", 40000, calories=900),
                seed_activity("strava", "s1", "2026-10-06T07:00:50Z", "Ride", 40100, avg_power_w=215),
                seed_activity("strava", "s2", "2026-10-08T17:00:00Z", "Run", 10000),
            ]
        )
        store.upsert_daily_metrics(
            [
                {"source": "garmin", "date": "2026-10-10", "metric": "resting_hr", "value": 49},
                {"source": "google_health", "date": "2026-10-10", "metric": "resting_hr", "value": 51},
            ]
        )
        store.upsert_sleep_sessions(
            [{"source": "garmin", "source_id": "z", "local_date": "2026-10-10", "minutes_asleep": 431, "is_main": 1}]
        )
        store.upsert_body_measurements(
            [{"source": "garmin", "ts": "2026-10-10T06:00:00Z", "metric": "weight_kg", "value": 72.4}]
        )
        store.record_sync("strava", "activities", cursor="2026-10-08T17:00:00Z", rows=2)
    return env_settings
