from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from fitness_hub import nutrition, queries
from fitness_hub.analysis import analyze
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.mcp_server import create_server, is_loopback, serve

EXPECTED_TOOLS = {
    "sync_status",
    "list_activities",
    "get_activity",
    "activity_summary",
    "get_daily_metrics",
    "get_sleep",
    "get_body_measurements",
    "query_sql",
    "training_load",
    "power_curve",
    "cardio_index",
    "get_activity_analysis",
    "nutrition_daily",
    "performance_index",
    "workout_fueling",
    "sync_now",
}


def call(settings: Settings, name: str, arguments: dict | None = None) -> tuple[bool, object]:
    """Call one tool in-process; returns (is_error, decoded JSON or error text)."""

    async def run():
        server = create_server(settings)
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await client.call_tool(name, arguments or {})

    result = anyio.run(run)
    text = "".join(c.text for c in result.content if c.type == "text")
    return result.isError, (text if result.isError else json.loads(text))


def test_tools_are_listed_with_annotations(seeded: Settings) -> None:
    async def run():
        server = create_server(seeded)
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await client.list_tools()

    tools = {t.name: t for t in anyio.run(run).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert all(t.annotations.readOnlyHint for n, t in tools.items() if n != "sync_now")
    assert tools["sync_now"].annotations.readOnlyHint is False
    assert tools["activity_summary"].inputSchema["properties"]["group_by"]["enum"] == ["week", "month", "day", "type"]


def test_list_activities_dedupes(seeded: Settings) -> None:
    error, body = call(seeded, "list_activities", {})
    assert not error
    assert body["count"] == 2
    ride = next(a for a in body["activities"] if a["sport"] == "cycling")
    assert ride["source"] == "garmin" and ride["also_recorded_by"] == ["strava"]
    _, strava = call(seeded, "list_activities", {"source": "strava"})
    assert strava["count"] == 2


def test_activity_summary_counts_shared_ride_once(seeded: Settings) -> None:
    error, body = call(
        seeded, "activity_summary", {"start_date": "2026-10-05", "end_date": "2026-10-11", "group_by": "type"}
    )
    assert not error
    assert {g["sport"]: (g["count"], g["distance_km"]) for g in body["groups"]} == {
        "cycling": (1, 40.0),
        "running": (1, 10.0),
    }
    assert body["total"]["count"] == 2


def test_invalid_arguments_are_tool_errors(seeded: Settings) -> None:
    error, text = call(seeded, "activity_summary", {"start_date": "2026-10-01", "end_date": "2026-10-31", "group_by": "fortnight"})
    assert error and "group_by" in text
    error, text = call(seeded, "list_activities", {"start_date": "yesterday"})
    assert error and "start_date" in text


def test_get_activity(seeded: Settings) -> None:
    _, row = call(seeded, "get_activity", {"source": "strava", "source_id": "s1", "include_raw": True})
    assert row["avg_power_w"] == 215 and row["raw"] == {"id": "s1"}
    error, missing = call(seeded, "get_activity", {"source": "strava", "source_id": "nope"})
    assert not error and missing is None


def test_training_tools(seeded: Settings) -> None:
    from fitness_hub.analysis import analyze

    with Store(seeded.db_path) as store:
        n = 1800
        streams = {"time": list(range(n)), "power": [215] * n, "heart_rate": [150] * n}
        store.upsert_activity_streams([{"source": "strava", "source_id": "s1", "streams": streams}])
        analyze(store)
    error, load = call(seeded, "training_load", {"start_date": "2026-10-06", "end_date": "2026-10-08"})
    assert not error and [d["date"] for d in load["days"]] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    # The Garmin copy of the ride has no streams; the scored Strava copy counts, once. The run is unscored.
    assert load["days"][0]["tss"] > 0 and load["days"][0]["unscored"] == 0
    assert load["days"][2]["tss"] == 0 and load["days"][2]["unscored"] == 1
    _, curve = call(seeded, "power_curve", {})
    best_20min = next(r for r in curve if r["duration_s"] == 1200)
    assert best_20min["watts"] == 215.0 and best_20min["source_id"] == "s1"
    _, analysis = call(seeded, "get_activity_analysis", {"source": "strava", "source_id": "s1"})
    assert analysis["tss_method"] == "power" and analysis["ftp_w"] == 204
    assert analysis["power_zones"][4] == {"zone": "Z5 VO2max", "upper": 244, "seconds": n}
    error, missing = call(seeded, "get_activity_analysis", {"source": "strava", "source_id": "s2"})
    assert not error and missing is None


def test_metrics_sleep_body_and_status(seeded: Settings) -> None:
    _, metrics = call(
        seeded, "get_daily_metrics", {"start_date": "2026-10-01", "end_date": "2026-10-14", "metrics": ["resting_hr"]}
    )
    assert {(r["source"], r["value"]) for r in metrics["rows"]} == {("garmin", 49), ("google_health", 51)}
    _, sleep = call(seeded, "get_sleep", {"start_date": "2026-10-01", "end_date": "2026-10-14"})
    assert sleep["sessions"][0]["minutes_asleep"] == 431
    _, body = call(seeded, "get_body_measurements", {"start_date": "2026-10-01", "end_date": "2026-10-14"})
    assert body["rows"] == [{"ts": "2026-10-10T06:00:00Z", "metric": "weight_kg", "source": "garmin", "value": 72.4}]
    _, status = call(seeded, "sync_status")
    assert status["row_counts"]["activities"] == 3
    assert status["streams"][0]["cursor"] == "2026-10-08T17:00:00Z"


def test_default_ranges_are_recent(seeded: Settings) -> None:
    today = date.today()
    with Store(seeded.db_path) as store:
        store.upsert_daily_metrics(
            [
                {"source": "garmin", "date": today.isoformat(), "metric": "steps", "value": 1234},
                {"source": "garmin", "date": (today - timedelta(days=45)).isoformat(), "metric": "steps", "value": 1},
            ]
        )
    _, metrics = call(seeded, "get_daily_metrics", {"metrics": ["steps"]})
    assert [r["value"] for r in metrics["rows"]] == [1234]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM activities",
        "DROP TABLE activities",
        "PRAGMA query_only = OFF",
        "SELECT 1; DELETE FROM activities",
        "WITH x AS (SELECT 1) DELETE FROM activities",
        "ATTACH DATABASE 'x.db' AS x",
    ],
)
def test_query_sql_cannot_modify(seeded: Settings, sql: str) -> None:
    error, _ = call(seeded, "query_sql", {"sql": sql})
    assert error
    with Store(seeded.db_path) as store:
        assert store.row_counts()["activities"] == 3


def test_query_sql_reads(seeded: Settings) -> None:
    error, body = call(
        seeded,
        "query_sql",
        {"sql": "SELECT source, json_extract(raw, '$.id') AS id FROM activities ORDER BY source_id", "limit": 2},
    )
    assert not error
    assert body == {"columns": ["source", "id"], "rows": [["garmin", "g1"], ["strava", "s1"]], "truncated": True}


def test_slow_query_does_not_block_other_tools(seeded: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queries, "SQL_TIMEOUT_S", 3.0)
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"

    async def run() -> dict:
        server = create_server(seeded)
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            started = time.monotonic()
            seen: dict = {}

            async def slow() -> None:
                result = await client.call_tool("query_sql", {"sql": endless})
                seen["slow"] = (time.monotonic() - started, result.isError)

            async def fast() -> None:
                await anyio.sleep(0.3)
                result = await client.call_tool("sync_status", {})
                seen["fast"] = (time.monotonic() - started, result.isError)

            async with anyio.create_task_group() as tg:
                tg.start_soon(slow)
                tg.start_soon(fast)
            return seen

    seen = anyio.run(run)
    assert seen["fast"][0] < 2.0 and not seen["fast"][1]  # answered while the query was still running
    assert seen["slow"][0] >= 2.9 and seen["slow"][1]  # stopped by the time limit, as a tool error


def test_sync_now_with_nothing_authorized(seeded: Settings) -> None:
    error, body = call(seeded, "sync_now", {"sources": ["strava"]})
    assert not error
    assert body["ok"] is True
    assert any("skipped" in line for line in body["log"])
    error, text = call(seeded, "sync_now", {"sources": ["fitbit"]})
    assert error and "unknown source" in text


def test_tools_work_on_a_fresh_home(env_settings: Settings) -> None:
    assert not env_settings.db_path.exists()
    error, body = call(env_settings, "list_activities", {})
    assert not error and body == {"count": 0, "activities": []}
    assert env_settings.db_path.exists()


def test_http_transport_is_loopback_only(env_settings: Settings) -> None:
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1") and is_loopback("[::1]")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5")
    # loopback, but FastMCP would not enable DNS-rebinding protection for it
    assert not is_loopback("127.0.0.2")
    with pytest.raises(ValueError, match="no authentication"):
        serve(env_settings, transport="http", host="0.0.0.0")


# -- real stdio subprocess ------------------------------------------------------------------


def _stdio_roundtrip(home: Path, args: list[str]) -> dict:
    env = {**get_default_environment(), "FITNESS_HUB_HOME": str(home)}
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    params = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(home.parent))

    async def run() -> dict:
        with anyio.fail_after(60):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool(
                        "query_sql", {"sql": "SELECT source, COUNT(*) AS n FROM activities GROUP BY source ORDER BY source"}
                    )
                    bad = await session.call_tool("query_sql", {"sql": "DELETE FROM activities"})
                    return {
                        "server": init.serverInfo.name,
                        "instructions": init.instructions,
                        "tools": {t.name for t in tools.tools},
                        "result": json.loads(result.content[0].text),
                        "result_error": result.isError,
                        "bad_error": bad.isError,
                    }

    return anyio.run(run)


@pytest.mark.parametrize(
    "args",
    [["-m", "fitness_hub.mcp_server"], ["-m", "fitness_hub.cli", "serve-mcp"], ["-m", "fitness_hub", "serve-mcp"]],
    ids=["module", "cli", "package"],
)
def test_stdio_subprocess(seeded: Settings, args: list[str]) -> None:
    out = _stdio_roundtrip(seeded.home, args)
    assert out["server"] == "fitness-hub"
    assert "de-dup" in out["instructions"].lower()
    assert out["tools"] == EXPECTED_TOOLS
    assert not out["result_error"]
    assert out["result"] == {"columns": ["source", "n"], "rows": [["garmin", 1], ["strava", 2]], "truncated": False}
    assert out["bad_error"]


def nutrition_ride(store: Store) -> None:
    store.upsert_activities([{"source": "strava", "source_id": "1", "start_time": "2026-10-12T16:00:00Z",
                              "local_date": "2026-10-12", "activity_type": "Ride", "moving_s": 3600.0, "raw": {}}])  # fmt: skip
    streams = {"time": list(range(3600)), "power": [200] * 3600, "heart_rate": [140] * 3600}
    store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": streams}])


def test_nutrition_tools(settings: Settings) -> None:
    with Store(settings.db_path) as s:
        nutrition_ride(s)
        analyze(s)
        nutrition.save_entry(s.conn, {"local_date": "2026-10-12", "meal": "during_workout", "name": "Gel",
                                      "carbs_g": 25, "activity_source": "strava", "activity_source_id": "1"})  # fmt: skip
    error, days = call(settings, "nutrition_daily", {"start_date": "2026-10-12", "end_date": "2026-10-12"})
    assert not error and days["days"][0]["intake"]["carbs_g"] == 25  # type: ignore[index]
    error, fueling = call(settings, "workout_fueling", {"source": "strava", "source_id": "1"})
    assert not error and fueling["during"]["carbs_g"] == 25  # type: ignore[index]


def test_performance_index_tool(settings: Settings) -> None:
    error, body = call(settings, "performance_index", {"start_date": "2026-10-01"})
    assert not error and body["days"] == [] and body["weights"]["threshold"] == 30
