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
from datetime import date, timedelta
from typing import Annotated, Any, Literal, TypeVar

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import nutrition, performance, queries
from .config import Settings
from .db import BODY_METRICS, DAILY_METRICS, SOURCES, open_readonly
from .sync import PROVIDER_NAMES, run_sync
from .util import parse_date

log = logging.getLogger(__name__)
T = TypeVar("T")

INSTRUCTIONS = f"""\
fitness-hub: the user's own workouts and health metrics from Strava, Garmin Connect and Google Health \
(Fitbit/Pixel), synced into a local SQLite database. All tools except sync_now are read-only.

Units: distance in metres (summaries: km), durations in seconds (summaries: hours), elevation in metres, \
heart rate in bpm, power in watts, energy in kcal, weight in kg. Timestamps are UTC ISO-8601; \
`local_date`/`date` are YYYY-MM-DD on the athlete's local clock. Sleep is filed under the date it ended.

Sources: {", ".join(SOURCES)} (garmin_export = Garmin CSV import; weight_csv = imported weigh-ins).
De-duplication: the same workout recorded by several services (starting within 180 s of the kept one) is returned once by \
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
last_attempt, last_success, last_error, rows_upserted), activity_metrics(source, source_id, analysis_version, \
streams_updated_at, streams_bytes, timer_s, avg_power_w, max_power_w, normalized_power_w, work_kj, avg_hr, max_hr, \
hr_20min_bpm, decoupling_pct, power_hist, hr_hist, ftp_w, threshold_hr, intensity_factor, tss, tss_method, \
variability_index, efficiency_factor, power_zone_s, hr_zone_s, scored_with, updated_at; tss_method is power or hr, \
and for hr the intensity_factor is the one giving the same TSS), power_curve(source, source_id, duration_s, watts, \
updated_at), athlete_settings(metric, effective_date, origin, value, note, updated_at; metric ftp_w, threshold_hr, \
max_hr or resting_hr), activity_streams(source, \
source_id, channels, sample_count, encoding, data, meta, updated_at) and daily_series(source, date, series, channels, \
sample_count, encoding, data, updated_at; all-day heart rate); their `data` is a compressed blob, not queryable. \
foods(id, name, brand, serving, nutrients per serving: kcal, carbs_g, sugars_g, protein_g, fat_g, fiber_g, \
sodium_mg, potassium_mg, caffeine_mg, fluid_ml; note, archived) and nutrition_entries(id, source, source_id, \
local_date, consumed_at, meal, activity_source, activity_source_id, offset_s, food_id, name, servings, the same \
nutrients as totals for the entry, note, kind food|drink, recipe: one bottle's JSON) hold what the user logged \
eating and drinking; workout_weights(source, source_id, pre_kg, post_kg, note) their weight around a workout. \
activity_metrics, power_curve, durability_bests(source, source_id, after_kj, duration_s, watts: best \
efforts started after that much work) and athlete_settings are filled by the training analysis and may be empty. \
`raw` holds the provider JSON (use json_extract). \
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
        """Totals per period or sport: count, distance_km, moving_hours, elevation_gain_m, calories
        (as recorded by the kept copy: Strava summaries have none; nutrition_daily's workout_kcal also
        uses a duplicate's calories or a ride's kJ), avg_of_avg_hr. De-duplicated across services, so a
        workout is never counted twice."""
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
    async def training_load(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default 90 days before end_date")] = None,
        end_date: DateArg = None,
    ) -> str:
        """Daily TSS with fitness (CTL, 42-day), fatigue (ATL, 7-day) and form (TSB = yesterday's CTL - ATL),
        like TrainingPeaks' Performance Management Chart. De-duplicated across services. Only activities
        with imported streams have a TSS; `unscored` counts the day's workouts without one, so a run of
        unscored days means the load is understated (e.g. no export imported since)."""
        rows = await read(lambda c: queries.training_load(c, start_date, end_date))
        return _dump({"count": len(rows), "days": rows})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def nutrition_daily(
        start_date: Annotated[str, Field(description="YYYY-MM-DD")],
        end_date: Annotated[str, Field(description="YYYY-MM-DD")],
    ) -> str:
        """Per day: calories and nutrients eaten (logged meals and workout fueling; None = nothing logged),
        workout kcal (recorded calories, else kJ of work for rides), the watch's total and active kcal, and
        the balance (intake minus total, when both are known)."""
        rows = await read(lambda c: nutrition.daily_nutrition(c, start_date, end_date))
        return _dump({"count": len(rows), "days": rows})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def workout_fueling(
        source: Annotated[str, Field(description="Activity source, e.g. strava")],
        source_id: Annotated[str, Field(description="Activity id within the source")],
    ) -> str:
        """What a workout was fuelled with: the logged entries, totals and per-hour rates (carbs g/h, fluid
        ml/h, sodium mg/h, caffeine) taken during it, the kcal it burned and a rough estimate of the
        carbohydrate burned from its intensity factor."""
        return _dump(await read(lambda c: nutrition.workout_fueling(c, source, source_id)))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def performance_index(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default the last 90 days")] = None,
        end_date: DateArg = None,
    ) -> str:
        """How good the rider is, day by day (unlike CTL, which is how much they trained): a score where
        100 = their baseline, from six components (sprint 5 s, anaerobic 1 min, VO2max 5 min, threshold,
        endurance 60 min, durability = power kept after 1000 kJ), each an index with the date it was last
        tested near-maximally; plus aerobic efficiency (EF of steady rides) and the rides that raised it."""
        end = parse_date(end_date, "end_date") if end_date else date.today()
        start = start_date or (end - timedelta(days=89)).isoformat()
        return _dump(await read(lambda c: performance.performance(c, start, end_date)))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def power_curve(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default all time")] = None,
        end_date: DateArg = None,
    ) -> str:
        """Best average power (W) for each duration from 1 s to 5 h in the date range, with the activity
        that set it (mean-maximal power curve)."""
        return _dump(await read(lambda c: queries.best_power_curve(c, start_date, end_date)))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_activity_analysis(
        source: Annotated[str, Field(description="Source of the activity, e.g. strava")],
        source_id: Annotated[str, Field(description="The activity id within that source")],
    ) -> str:
        """Computed metrics of one activity: NP, IF, TSS (tss_method power, or hr: TRIMP-based, with the IF
        that gives the same TSS), VI, EF, Pw:HR decoupling, work, time in Coggan power and Friel HR zones
        (with each zone's highest W / bpm), the FTP / threshold HR used, and its power curve. null if the
        activity has no analysed streams."""
        return _dump(await read(lambda c: queries.activity_analysis(c, source, source_id)))

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
