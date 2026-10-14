from __future__ import annotations

import os
import sqlite3
import stat
from datetime import date
from pathlib import Path

import httpx
import pytest

from conftest import json_response, mock_client
from fitness_hub.auth import AuthError, TokenStore, expires_soon, make_pkce, wait_for_callback
from fitness_hub.config import ConfigError, Settings, parse_dotenv
from fitness_hub.db import Store, open_readonly
from fitness_hub.http import ApiError, RateLimited, request_json
from fitness_hub.providers import SyncWindow, run_stream

ACTIVITY = {
    "source": "strava",
    "source_id": "1",
    "start_time": "2026-10-01T07:00:00Z",
    "local_date": "2026-10-01",
    "activity_type": "Ride",
    "name": "Morning Ride",
    "distance_m": 30000.0,
    "raw": {"id": 1},
}


# -- db ------------------------------------------------------------------------


def test_upserts_are_idempotent(store: Store) -> None:
    for _ in range(3):
        assert store.upsert_activities([ACTIVITY]) == 1
        store.upsert_daily_metrics(
            [{"source": "garmin", "date": "2026-10-01", "metric": "steps", "value": 9000, "raw": {}}]
        )
        store.upsert_sleep_sessions([{"source": "garmin", "source_id": "s1", "local_date": "2026-10-01", "raw": {}}])
        store.upsert_body_measurements(
            [{"source": "garmin", "ts": "2026-10-01T06:00:00Z", "metric": "weight_kg", "value": 70.1}]
        )
    assert store.row_counts() == {
        "activities": 1,
        "daily_metrics": 1,
        "sleep_sessions": 1,
        "body_measurements": 1,
    }


def test_upsert_updates_existing_row(store: Store) -> None:
    store.upsert_activities([ACTIVITY])
    store.upsert_activities([{**ACTIVITY, "name": "Renamed", "raw": {"id": 1, "v": 2}}])
    row = store.conn.execute("SELECT name, raw FROM activities").fetchone()
    assert row["name"] == "Renamed"
    assert '"v":2' in row["raw"]


def test_daily_metric_without_value_is_skipped(store: Store) -> None:
    n = store.upsert_daily_metrics([{"source": "x", "date": "2026-10-01", "metric": "steps", "value": None}])
    assert n == 0
    assert store.row_counts()["daily_metrics"] == 0


def test_activity_requires_key_fields(store: Store) -> None:
    with pytest.raises(ValueError):
        store.upsert_activities([{**ACTIVITY, "start_time": None}])


def test_record_sync_keeps_cursor_and_last_success_on_error(store: Store) -> None:
    store.record_sync("strava", "activities", cursor="2026-10-01T07:00:00Z", rows=5)
    first = store.sync_states()[0]
    assert first["cursor"] == "2026-10-01T07:00:00Z"
    assert first["last_success"] and first["last_error"] is None

    store.record_sync("strava", "activities", error="boom")
    after_error = store.sync_states()[0]
    assert after_error["cursor"] == "2026-10-01T07:00:00Z"
    assert after_error["last_success"] == first["last_success"]
    assert after_error["last_error"] == "boom"

    # success without a cursor must not erase the stored one; it clears the error
    store.record_sync("strava", "activities", rows=0)
    ok_again = store.sync_states()[0]
    assert ok_again["cursor"] == "2026-10-01T07:00:00Z"
    assert ok_again["last_error"] is None

    store.record_sync("strava", "activities", cursor="2026-10-05T07:00:00Z")
    assert store.get_cursor("strava", "activities") == "2026-10-05T07:00:00Z"


def test_run_stream_records_partial_progress_and_error(store: Store) -> None:
    def failing(progress):
        progress.rows = 3
        progress.cursor = "2026-10-02"
        raise RuntimeError("throttled")

    result = run_stream(store, "garmin", "daily", failing)
    assert not result.ok and result.rows == 3
    state = store.sync_states()[0]
    assert state["cursor"] == "2026-10-02"
    assert "throttled" in state["last_error"]
    assert state["last_success"] is None


def test_readonly_connection_cannot_write(settings: Settings) -> None:
    conn = open_readonly(settings.db_path)  # creates file + schema when missing
    try:
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM activities")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil (x)")
    finally:
        conn.close()


def test_database_file_is_private(settings: Settings, store: Store) -> None:
    mode = stat.S_IMODE(os.stat(settings.db_path).st_mode)
    assert mode == 0o600


# -- tokens ----------------------------------------------------------------------


def test_token_store_is_0600_and_atomic(tokens: TokenStore) -> None:
    tokens.save("strava", {"access_token": "a", "refresh_token": "r", "expires_at": 1})
    tokens.save("google", {"access_token": "g"})
    assert stat.S_IMODE(os.stat(tokens.path).st_mode) == 0o600
    assert tokens.get("strava")["refresh_token"] == "r"
    assert tokens.get("google") == {"access_token": "g"}
    assert [p.name for p in tokens.path.parent.iterdir() if p.name.startswith(".tokens.")] == []
    tokens.delete("google")
    assert tokens.get("google") is None


def test_expires_soon() -> None:
    assert expires_soon({}, now=1000)
    assert expires_soon({"access_token": "x", "expires_at": 1030}, now=1000)
    assert not expires_soon({"access_token": "x", "expires_at": 2000}, now=1000)


def test_pkce_challenge_is_s256() -> None:
    import base64
    import hashlib

    verifier, challenge = make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_wait_for_callback_validates_state() -> None:
    import socket
    import threading
    import urllib.error
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    results: dict = {}

    def serve() -> None:
        try:
            results["params"] = wait_for_callback(port, "good-state", timeout=10)
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            results["error"] = exc

    thread = threading.Thread(target=serve)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/callback?state=bad&code=x", timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            break
        except urllib.error.URLError:
            import time

            time.sleep(0.05)
    with urllib.request.urlopen(base + "/callback?state=good-state&code=abc", timeout=2) as resp:
        assert resp.status == 200
    thread.join(5)
    assert results.get("params", {}).get("code") == "abc"


def test_wait_for_callback_times_out() -> None:
    with pytest.raises(AuthError, match="timed out"):
        wait_for_callback(0, "s", timeout=0.2)


# -- http ------------------------------------------------------------------------


def test_request_json_retries_with_backoff_and_retry_after() -> None:
    calls = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return json_response({"ok": True})

    assert request_json(mock_client(handler), "GET", "https://api.test/x", sleep=sleeps.append) == {"ok": True}
    assert sleeps == [2.0, 7.0]


def test_request_json_raises_rate_limited_when_wait_too_long() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "900"})

    with pytest.raises(RateLimited):
        request_json(mock_client(handler), "GET", "https://api.test/x", sleep=sleeps.append)
    assert sleeps == []


def test_request_json_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(ApiError) as info:
        request_json(mock_client(handler), "GET", "https://api.test/x", sleep=sleeps.append)
    assert info.value.status == 502
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_request_json_errors_and_empty_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/empty":
            return httpx.Response(204)
        return httpx.Response(404, text="nope")

    client = mock_client(handler)
    assert request_json(client, "GET", "https://api.test/empty") is None
    with pytest.raises(ApiError) as info:
        request_json(client, "GET", "https://api.test/missing?secret=1")
    assert info.value.status == 404
    assert "secret" not in str(info.value)


def test_request_json_429_not_retried_when_excluded() -> None:
    sleeps: list[float] = []
    client = mock_client(lambda r: httpx.Response(429))
    with pytest.raises(RateLimited):
        request_json(client, "GET", "https://api.test/x", retry_statuses={502}, sleep=sleeps.append)
    assert sleeps == []


# -- config / window ---------------------------------------------------------------


def test_dotenv_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".env").write_text("STRAVA_CLIENT_ID=from-cwd\nBACKFILL_DAYS=10\nOAUTH_PORT=9000\n")
    (home / ".env").write_text("export STRAVA_CLIENT_ID='from-home'\nBACKFILL_DAYS=20 # comment\n")
    s = Settings.from_env({"FITNESS_HUB_HOME": str(home), "BACKFILL_DAYS": "30"}, cwd=tmp_path)
    assert s.strava_client_id == "from-home"
    assert s.backfill_days == 30
    assert s.oauth_port == 9000
    assert s.db_path == home / "fitness.db"
    assert s.redirect_uri == "http://localhost:9000/callback"


def test_config_rejects_bad_int(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"FITNESS_HUB_HOME": str(tmp_path), "LOOKBACK_DAYS": "seven"}, cwd=tmp_path)


def test_garmin_activities_flag(tmp_path: Path) -> None:
    base = {"FITNESS_HUB_HOME": str(tmp_path)}
    assert Settings.from_env(base, cwd=tmp_path).garmin_activities is False
    assert Settings.from_env({**base, "GARMIN_ACTIVITIES": "true"}, cwd=tmp_path).garmin_activities is True
    assert Settings.from_env({**base, "GARMIN_ACTIVITIES": "0"}, cwd=tmp_path).garmin_activities is False
    with pytest.raises(ConfigError, match="GARMIN_ACTIVITIES"):
        Settings.from_env({**base, "GARMIN_ACTIVITIES": "sometimes"}, cwd=tmp_path)


def test_parse_dotenv_ignores_noise() -> None:
    assert parse_dotenv("# c\n\nNOEQUALS\nA=1\nB=\"two words\"\n") == {"A": "1", "B": "two words"}


def test_sync_window_start_for() -> None:
    w = SyncWindow(date(2026, 9, 1), date(2026, 10, 14))
    assert w.start_for(None, 7, False) == date(2026, 9, 1)
    assert w.start_for("2026-10-10T08:00:00Z", 7, False) == date(2026, 10, 3)
    assert w.start_for("2026-10-10", 7, True) == date(2026, 9, 1)
    assert w.start_for("2026-12-01", 7, False) == date(2026, 10, 14)  # clamped to end
    explicit = SyncWindow(date(2026, 1, 1), date(2026, 10, 14), explicit_start=True)
    assert explicit.start_for("2026-10-10", 7, False) == date(2026, 1, 1)


def test_dotenv_quoted_value_with_inline_comment() -> None:
    parsed = parse_dotenv('A="abc123"  # from strava\nB=\'x # y\'\nC=plain # note\nD="unterminated\n')
    assert parsed == {"A": "abc123", "B": "x # y", "C": "plain", "D": '"unterminated'}


def test_token_store_is_safe_across_processes(tokens: TokenStore) -> None:
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        f"""
        from fitness_hub.auth import TokenStore
        store = TokenStore({str(tokens.path)!r})
        for i in range(400):
            store.save("other", {{"n": i}})
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    for i in range(400):
        tokens.save("mine", {"n": i})
    assert child.wait(60) == 0
    # without the cross-process lock one side regularly overwrites the other's last write
    assert tokens.get("mine") == {"n": 399}
    assert tokens.get("other") == {"n": 399}


def test_wait_for_callback_survives_an_idle_connection() -> None:
    import socket
    import threading
    import time
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    results: dict = {}
    thread = threading.Thread(target=lambda: results.update(params=wait_for_callback(port, "st", timeout=20)))
    thread.start()
    idle = None
    for _ in range(100):
        try:
            idle = socket.create_connection(("127.0.0.1", port), timeout=2)  # connects, never sends
            break
        except OSError:
            time.sleep(0.05)
    assert idle is not None
    try:
        started = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?state=st&code=c", timeout=5) as resp:
            assert resp.status == 200
        thread.join(5)
        assert results["params"]["code"] == "c"
        assert time.monotonic() - started < 5
    finally:
        idle.close()


@pytest.mark.parametrize(
    ("method", "error", "idempotent", "retried"),
    [
        ("GET", httpx.ReadTimeout("slow"), None, True),
        ("POST", httpx.ReadTimeout("slow"), None, False),  # may have been processed: never replay
        ("POST", httpx.ConnectError("refused"), None, True),  # never reached the server
        ("POST", httpx.ReadTimeout("slow"), True, True),  # e.g. Google dailyRollUp
    ],
)
def test_request_json_transport_retry_respects_idempotency(method, error, idempotent, retried) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise error
        return json_response({"ok": True})

    sleeps: list[float] = []
    kwargs = {} if idempotent is None else {"idempotent": idempotent}
    if retried:
        assert request_json(mock_client(handler), method, "https://api.test/x", sleep=sleeps.append, **kwargs) == {
            "ok": True
        }
        assert len(calls) == 2
    else:
        with pytest.raises(httpx.TransportError):
            request_json(mock_client(handler), method, "https://api.test/x", sleep=sleeps.append, **kwargs)
        assert len(calls) == 1


def test_request_json_post_only_retries_unprocessed_statuses() -> None:
    def make(status: int):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(status) if len(calls) == 1 else json_response({"ok": True})

        return calls, mock_client(handler)

    calls, client = make(502)  # a gateway error may hide a processed request
    with pytest.raises(ApiError):
        request_json(client, "POST", "https://api.test/token", sleep=lambda _: None)
    assert len(calls) == 1
    calls, client = make(503)
    assert request_json(client, "POST", "https://api.test/token", sleep=lambda _: None) == {"ok": True}
