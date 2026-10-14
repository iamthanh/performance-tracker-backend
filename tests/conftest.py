from __future__ import annotations

import json
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
