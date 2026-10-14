"""MCP server exposing the local fitness database to Claude.

Every read tool opens its own read-only connection (``mode=ro`` +
``PRAGMA query_only``). ``sync_now`` is the only tool that changes anything,
and it only runs the normal provider sync.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import closing
from typing import Annotated, Any, Literal, TypeVar

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import queries
from .config import Settings
from .db import BODY_METRICS, DAILY_METRICS, SOURCES, open_readonly
from .sync import PROVIDER_NAMES, run_sync

log = logging.getLogger(__name__)
T = TypeVar("T")

INSTRUCTIONS = f"""\
fitness-hub: the user's own workouts and health metrics from Strava, Garmin Connect and Google Health \
(Fitbit/Pixel), synced into a local SQLite database. All tools except sync_now are read-only.

Units: distance in metres (summaries: km), durations in seconds (summaries: hours), elevation in metres, \
heart rate in bpm, power in watts, energy in kcal, weight in kg. Timestamps are UTC ISO-8601; \
`local_date`/`date` are YYYY-MM-DD on the athlete's local clock. Sleep is filed under the date it ended.

Sources: {", ".join(SOURCES)} (garmin_export = Garmin CSV import).
De-duplication: the same workout recorded by several services (start within 180 s) is returned once by \
list_activities (unless `source` is given) and always by activity_summary, preferring \
garmin > google_health > strava > garmin_export; `also_recorded_by` lists the other sources. \
`sport` is a normalized family (cycling, running, swimming, ...); activity_type filters accept either \
a family or a provider type (Ride, road_biking, OUTDOOR_BIKE).
Daily metrics are NOT merged across sources: each row has its `source`.

daily_metrics.metric names: {", ".join(DAILY_METRICS)}.
body_measurements.metric names: {", ".join(BODY_METRICS)}.
Tables for query_sql: activities(source, source_id, start_time, local_date, activity_type, name, duration_s, \
moving_s, distance_m, elevation_gain_m, avg_hr, max_hr, avg_power_w, calories, raw, updated_at), \
daily_metrics(source, date, metric, value, raw, updated_at), sleep_sessions(source, source_id, start_time, \
end_time, local_date, minutes_asleep, minutes_awake, minutes_deep, minutes_light, minutes_rem, is_main, raw, \
updated_at), body_measurements(source, ts, metric, value, raw, updated_at), sync_state(source, stream, cursor, \
last_attempt, last_success, last_error, rows_upserted). `raw` holds the provider JSON (use json_extract). \
query_sql does NOT de-duplicate activities.

If data looks stale, check sync_status; sync_now fetches new data (it can take minutes and uses \
provider rate limits, so avoid full=true unless asked).
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
SYNC = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

DateArg = Annotated[str | None, Field(description="YYYY-MM-DD (athlete's local date), inclusive")]
SourceArg = Annotated[str | None, Field(description=f"Only this source: {', '.join(SOURCES)}")]


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


# FastMCP enables DNS-rebinding protection only for these exact hosts.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def normalize_host(host: str) -> str:
    return host.strip().strip("[]")


def is_loopback(host: str) -> bool:
    """True only for the loopback hosts FastMCP protects against DNS rebinding."""
    return normalize_host(host) in LOOPBACK_HOSTS


def check_http_host(host: str) -> None:
    if not is_loopback(host):
        raise ValueError(
            f"refusing to serve over HTTP on {host!r}: the HTTP transport has no authentication and "
            f"would expose your health data. Use one of {', '.join(LOOPBACK_HOSTS)} "
            "(remote access is future work)."
        )


def create_server(
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = 8800,
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING",
) -> FastMCP:
    settings = settings or Settings.from_env()
    mcp = FastMCP("fitness-hub", instructions=INSTRUCTIONS, host=host, port=port, log_level=log_level)

    def read_sync(fn: Callable[[Any], T]) -> T:
        with closing(open_readonly(settings.db_path)) as conn:
            return fn(conn)

    async def read(fn: Callable[[Any], T]) -> T:
        # A worker thread keeps the event loop (and every other tool) responsive
        # while a slow query runs; the connection lives entirely in that thread.
        return await anyio.to_thread.run_sync(read_sync, fn)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def sync_status() -> str:
        """Row counts per table and, per source/stream, the last attempt, last success, cursor and last error."""
        return _dump(await read(queries.sync_status))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def list_activities(
        start_date: DateArg = None,
        end_date: DateArg = None,
        activity_type: Annotated[
            str | None, Field(description="Sport family (cycling, running, ...) or provider type, case-insensitive")
        ] = None,
        source: SourceArg = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Max activities (newest first)")] = 50,
    ) -> str:
        """List workouts newest first. Duplicates across services are merged unless `source` is set."""
        rows = await read(lambda c: queries.list_activities(c, start_date, end_date, activity_type, source, limit))
        return _dump({"count": len(rows), "activities": rows})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_activity(
        source: Annotated[str, Field(description="Source of the activity, e.g. strava")],
        source_id: Annotated[str, Field(description="The activity id within that source")],
        include_raw: Annotated[bool, Field(description="Include the full provider JSON payload")] = False,
    ) -> str:
        """One activity by (source, source_id), or null if it does not exist."""
        return _dump(await read(lambda c: queries.get_activity(c, source, source_id, include_raw)))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def activity_summary(
        start_date: Annotated[str, Field(description="YYYY-MM-DD, inclusive")],
        end_date: Annotated[str, Field(description="YYYY-MM-DD, inclusive")],
        group_by: Annotated[
            Literal["week", "month", "day", "type"],
            Field(description="week (Monday start), month, day, or type (sport family)"),
        ] = "week",
        activity_type: Annotated[str | None, Field(description="Optional sport family or provider type filter")] = None,
    ) -> str:
        """Totals per period or sport: count, distance_km, moving_hours, elevation_gain_m, calories,
        avg_of_avg_hr. De-duplicated across services, so a workout is never counted twice."""
        return _dump(await read(lambda c: queries.activity_summary(c, start_date, end_date, group_by, activity_type)))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_daily_metrics(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default 30 days before end_date")] = None,
        end_date: DateArg = None,
        metrics: Annotated[
            list[str] | None, Field(description=f"Subset of: {', '.join(DAILY_METRICS)}")
        ] = None,
        source: SourceArg = None,
    ) -> str:
        """Daily health metrics as rows {date, metric, source, value}. Sources are not merged."""
        rows = await read(lambda c: queries.daily_metrics(c, start_date, end_date, metrics, source))
        return _dump({"count": len(rows), "rows": rows})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_sleep(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default 30 days before end_date")] = None,
        end_date: DateArg = None,
        main_only: Annotated[bool, Field(description="Only the main sleep of each night (exclude naps)")] = True,
        source: SourceArg = None,
    ) -> str:
        """Sleep sessions (minutes asleep/awake/deep/light/REM) by the local date the sleep ended."""
        rows = await read(lambda c: queries.sleep_sessions(c, start_date, end_date, main_only, source))
        return _dump({"count": len(rows), "sessions": rows})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_body_measurements(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default 90 days before end_date")] = None,
        end_date: DateArg = None,
        metric: Annotated[str | None, Field(description=f"One of: {', '.join(BODY_METRICS)}")] = None,
    ) -> str:
        """Weight (kg) and body fat (%) samples with UTC timestamps."""
        rows = await read(lambda c: queries.body_measurements(c, start_date, end_date, metric))
        return _dump({"count": len(rows), "rows": rows})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def query_sql(
        sql: Annotated[str, Field(description="A single SELECT or WITH query (SQLite dialect)")],
        limit: Annotated[int, Field(ge=1, le=1000, description="Max rows returned")] = 200,
    ) -> str:
        """Run one read-only SQL query against the local database. Returns {columns, rows, truncated}.
        Writes, PRAGMA, ATTACH and multiple statements are rejected. Not de-duplicated."""
        return _dump(await read(lambda c: queries.run_select(c, sql, limit)))

    @mcp.tool(annotations=SYNC, structured_output=False)
    async def sync_now(
        sources: Annotated[
            list[str] | None, Field(description=f"Subset of: {', '.join(PROVIDER_NAMES)}; default all")
        ] = None,
        full: Annotated[
            bool, Field(description="Re-fetch the whole backfill window (slow, uses rate limits)")
        ] = False,
    ) -> str:
        """Fetch new data from the authorized services now and return the sync log. Blocking: may take
        minutes; full=true re-downloads the whole history window and can hit rate limits."""
        lines: list[str] = []
        results = await anyio.to_thread.run_sync(
            lambda: run_sync(settings, sources=sources, full=full, log_line=lines.append)
        )
        return _dump({"ok": all(r.ok for r in results), "log": lines})

    return mcp


def serve(settings: Settings, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8800) -> None:
    """Run the server until stopped. HTTP is unauthenticated, so it only binds to loopback."""
    if transport == "stdio":
        create_server(settings).run("stdio")
        return
    if transport != "http":
        raise ValueError(f"unknown transport {transport!r} (stdio or http)")
    check_http_host(host)
    log.warning(
        "MCP over HTTP at http://%s:%s/mcp has NO authentication; any local process can read your data",
        host,
        port,
    )
    create_server(settings, host=normalize_host(host), port=port).run("streamable-http")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")  # stderr
    serve(Settings.from_env())
