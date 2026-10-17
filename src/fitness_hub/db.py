"""SQLite schema, migrations, idempotent upserts and sync bookkeeping.

Timestamps are UTC ISO-8601 text (``YYYY-MM-DDTHH:MM:SSZ``); dates are
``YYYY-MM-DD`` on the athlete's local clock. Every row keeps the provider
payload in ``raw`` and every write is ``INSERT ... ON CONFLICT DO UPDATE``.

``SCHEMA`` is version 1. Later versions are applied by ``migrate`` from
``MIGRATIONS`` and recorded in ``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import zlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import InputError, now_utc_iso, parse_date, utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    local_date TEXT NOT NULL,
    activity_type TEXT,
    name TEXT,
    duration_s REAL,
    moving_s REAL,
    distance_m REAL,
    elevation_gain_m REAL,
    avg_hr REAL,
    max_hr REAL,
    avg_power_w REAL,
    calories REAL,
    raw TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_activities_local_date ON activities(local_date);

CREATE TABLE IF NOT EXISTS daily_metrics (
    source TEXT NOT NULL,
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    raw TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, date, metric)
);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics(date);

CREATE TABLE IF NOT EXISTS sleep_sessions (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    local_date TEXT,
    minutes_asleep REAL,
    minutes_awake REAL,
    minutes_deep REAL,
    minutes_light REAL,
    minutes_rem REAL,
    is_main INTEGER,
    raw TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_sleep_sessions_local_date ON sleep_sessions(local_date);

CREATE TABLE IF NOT EXISTS body_measurements (
    source TEXT NOT NULL,
    ts TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    raw TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, ts, metric)
);

CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT NOT NULL,
    stream TEXT NOT NULL,
    cursor TEXT,
    last_attempt TEXT,
    last_success TEXT,
    last_error TEXT,
    rows_upserted INTEGER,
    PRIMARY KEY (source, stream)
);
"""

SCHEMA_VERSION = 7

# MIGRATIONS[v] upgrades a database from version v - 1 to v. Each one runs in a
# single transaction together with the ``user_version`` bump.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    # Training analysis: per-second streams, derived per-activity metrics, the
    # mean-maximal power curve and dated athlete thresholds (FTP, ...).
    2: (
        """
        CREATE TABLE activity_streams (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            channels TEXT NOT NULL,          -- JSON array of the channel names in data
            sample_count INTEGER NOT NULL,
            encoding TEXT NOT NULL,          -- how data is encoded, see STREAM_ENCODING
            data BLOB NOT NULL,              -- {channel: [value, ...]}, every channel sample_count long
            meta TEXT,                       -- provider metadata (resolution, series type, ...)
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )
        """,
        """
        CREATE TABLE activity_metrics (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            analysis_version INTEGER NOT NULL,
            ftp_w REAL,                      -- thresholds the metrics were computed with
            threshold_hr REAL,
            normalized_power_w REAL,
            intensity_factor REAL,
            tss REAL,
            tss_method TEXT,                 -- power | hr; NULL when no TSS could be computed
            variability_index REAL,
            efficiency_factor REAL,
            work_kj REAL,
            decoupling_pct REAL,
            power_zone_s TEXT,               -- JSON array: seconds spent in each power zone
            hr_zone_s TEXT,                  -- JSON array: seconds spent in each HR zone
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )
        """,
        """
        CREATE TABLE power_curve (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            duration_s INTEGER NOT NULL,
            watts REAL NOT NULL,             -- best average power over duration_s
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id, duration_s)
        )
        """,
        """
        CREATE TABLE daily_series (
            source TEXT NOT NULL,
            date TEXT NOT NULL,              -- athlete's local date
            series TEXT NOT NULL,            -- e.g. heart_rate
            channels TEXT NOT NULL,          -- JSON array of the channel names in data
            sample_count INTEGER NOT NULL,
            encoding TEXT NOT NULL,
            data BLOB NOT NULL,              -- {"time": [epoch s, ...], <series>: [value, ...]}
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, date, series)
        )
        """,
        """
        CREATE TABLE athlete_settings (
            metric TEXT NOT NULL,
            effective_date TEXT NOT NULL,    -- applies from this local date until the next entry
            origin TEXT NOT NULL,            -- manual | auto
            value REAL NOT NULL,
            note TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (metric, effective_date, origin)
        )
        """,
    ),
    # activity_metrics gains the threshold-independent results (so a new FTP only
    # needs the stored row, not the streams). Nothing wrote the v2 table yet.
    3: (
        "DROP TABLE activity_metrics",
        """
        CREATE TABLE activity_metrics (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            analysis_version INTEGER NOT NULL,
            streams_updated_at TEXT,         -- the activity_streams row this was computed from
            streams_bytes INTEGER,
            timer_s REAL,                    -- seconds analysed (pauses longer than a few seconds left out)
            avg_power_w REAL,
            max_power_w REAL,
            normalized_power_w REAL,
            work_kj REAL,
            avg_hr REAL,
            max_hr REAL,
            hr_20min_bpm REAL,               -- best 20-minute average heart rate
            decoupling_pct REAL,             -- Pw:HR drift, first half vs second half
            power_hist TEXT,                 -- JSON {watts: seconds}
            hr_hist TEXT,                    -- JSON {bpm: seconds}
            ftp_w REAL,                      -- thresholds the values below were computed with
            threshold_hr REAL,
            intensity_factor REAL,
            tss REAL,
            tss_method TEXT,                 -- power | hr; NULL when no TSS could be computed
            variability_index REAL,
            efficiency_factor REAL,
            power_zone_s TEXT,               -- JSON array: seconds spent in each power zone
            hr_zone_s TEXT,                  -- JSON array: seconds spent in each HR zone
            scored_with TEXT,                -- JSON [local_date, sport, thresholds...] the values above used
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )
        """,
    ),
    # Nutrition: what was eaten and drunk (meals and fueling during workouts) and
    # saved foods for quick logging. See nutrition.py.
    4: (
        """
        CREATE TABLE foods (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            brand TEXT,
            serving TEXT,                    -- what one serving is, e.g. "1 gel (40 g)", "750 ml bottle"
            kcal REAL,                       -- nutrients per serving; NULL = unknown
            carbs_g REAL,
            sugars_g REAL,
            protein_g REAL,
            fat_g REAL,
            fiber_g REAL,
            sodium_mg REAL,
            potassium_mg REAL,
            caffeine_mg REAL,
            fluid_ml REAL,
            note TEXT,
            archived INTEGER NOT NULL DEFAULT 0,  -- hidden from pickers, kept for old entries
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE nutrition_entries (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'manual',  -- manual, or the app an import came from
            source_id TEXT,                  -- the importer's id, so a re-import updates instead of duplicating
            local_date TEXT NOT NULL,
            consumed_at TEXT,                -- UTC, when known
            meal TEXT NOT NULL,              -- breakfast, lunch, dinner, snack, pre_workout, during_workout, post_workout
            activity_source TEXT,            -- the workout this fuelled (activities.source/source_id)
            activity_source_id TEXT,
            offset_s REAL,                   -- seconds into that workout
            food_id INTEGER,                 -- the saved food it was logged from (NULL if typed in or deleted)
            name TEXT NOT NULL,
            servings REAL NOT NULL DEFAULT 1,
            kcal REAL,                       -- totals for this entry (servings applied), copied at logging
            carbs_g REAL,                    -- time so editing a saved food never rewrites history
            sugars_g REAL,
            protein_g REAL,
            fat_g REAL,
            fiber_g REAL,
            sodium_mg REAL,
            potassium_mg REAL,
            caffeine_mg REAL,
            fluid_ml REAL,
            note TEXT,
            raw TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source, source_id)
        )
        """,
        "CREATE INDEX nutrition_entries_by_date ON nutrition_entries (local_date)",
        "CREATE INDEX nutrition_entries_by_activity ON nutrition_entries (activity_source, activity_source_id)",
    ),
    # Workout drinks as bottles with a recipe, and body weight before and after a workout.
    5: (
        "ALTER TABLE nutrition_entries ADD COLUMN kind TEXT NOT NULL DEFAULT 'food' CHECK (kind IN ('food', 'drink'))",
        # JSON recipe of ONE bottle (water_ml, salt_g, maltodextrin_g, sucrose_g, other: [...]);
        # servings is the number of bottles and the nutrient columns hold the totals.
        "ALTER TABLE nutrition_entries ADD COLUMN recipe TEXT",
        """
        CREATE TABLE workout_weights (
            source TEXT NOT NULL,            -- the workout (activities.source/source_id)
            source_id TEXT NOT NULL,
            pre_kg REAL,                     -- body weight just before
            post_kg REAL,                    -- and just after
            note TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )
        """,
    ),
    # Performance index: each ride's best efforts after accumulated work, and app-wide settings.
    6: (
        """
        CREATE TABLE durability_bests (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            after_kj INTEGER NOT NULL,       -- the effort starts after this much work in the ride
            duration_s INTEGER NOT NULL,
            watts REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id, after_kj, duration_s)
        )
        """,
        """
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,             -- JSON
            updated_at TEXT NOT NULL
        )
        """,
    ),
    # Cardio index: each workout's steady riding and heart-rate recoveries (steady.py).
    7: (
        """
        CREATE TABLE ride_cardio (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            has_gps INTEGER NOT NULL,        -- 0: no position recorded (indoors)
            blocks TEXT NOT NULL,            -- JSON [[start_s, watts, bpm, temp_c, kj_before, clock_s]]
            recoveries TEXT NOT NULL,        -- JSON [[peak_bpm, drop_bpm, effort_w, recovery_w]]
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )
        """,
    ),
}

STREAM_ENCODING = "zlib+json"
ATHLETE_SETTINGS = ("ftp_w", "threshold_hr", "max_hr", "resting_hr")
SETTING_ORIGINS = ("manual", "auto")

DATA_TABLES = ("activities", "daily_metrics", "sleep_sessions", "body_measurements")

DAILY_METRICS = (
    "steps",
    "distance_m",
    "total_calories_kcal",
    "active_calories_kcal",
    "floors",
    "active_zone_minutes",
    "resting_hr",
    "hrv_rmssd_ms",
    "sleep_score",
    "avg_stress",
    "body_battery_high",
    "body_battery_low",
    "spo2_pct",
    "respiratory_rate_bpm",
    "vo2_max",
)
BODY_METRICS = ("weight_kg", "body_fat_pct")
SOURCES = ("strava", "garmin", "google_health", "garmin_export", "upload", "weight_csv")

_ACTIVITY_COLUMNS = (
    "source",
    "source_id",
    "start_time",
    "local_date",
    "activity_type",
    "name",
    "duration_s",
    "moving_s",
    "distance_m",
    "elevation_gain_m",
    "avg_hr",
    "max_hr",
    "avg_power_w",
    "calories",
    "raw",
)
_SLEEP_COLUMNS = (
    "source",
    "source_id",
    "start_time",
    "end_time",
    "local_date",
    "minutes_asleep",
    "minutes_awake",
    "minutes_deep",
    "minutes_light",
    "minutes_rem",
    "is_main",
    "raw",
)
_DAILY_COLUMNS = ("source", "date", "metric", "value", "raw")
_STREAM_COLUMNS = ("source", "source_id", "channels", "sample_count", "encoding", "data", "meta")
_METRIC_COLUMNS = (
    "source",
    "source_id",
    "analysis_version",
    "streams_updated_at",
    "streams_bytes",
    "timer_s",
    "avg_power_w",
    "max_power_w",
    "normalized_power_w",
    "work_kj",
    "avg_hr",
    "max_hr",
    "hr_20min_bpm",
    "decoupling_pct",
    "power_hist",
    "hr_hist",
    "ftp_w",
    "threshold_hr",
    "intensity_factor",
    "tss",
    "tss_method",
    "variability_index",
    "efficiency_factor",
    "power_zone_s",
    "hr_zone_s",
    "scored_with",
)
_METRIC_JSON_COLUMNS = ("power_hist", "hr_hist", "power_zone_s", "hr_zone_s")
_SERIES_COLUMNS = ("source", "date", "series", "channels", "sample_count", "encoding", "data")
_SETTING_COLUMNS = ("metric", "effective_date", "origin", "value", "note")
_BODY_COLUMNS = ("source", "ts", "metric", "value", "raw")


def _upsert_sql(table: str, columns: tuple[str, ...], key: tuple[str, ...]) -> str:
    cols = (*columns, "updated_at")
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key)
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key)}) DO UPDATE SET {updates}"
    )


_SQL_ACTIVITY = _upsert_sql("activities", _ACTIVITY_COLUMNS, ("source", "source_id"))
_SQL_SLEEP = _upsert_sql("sleep_sessions", _SLEEP_COLUMNS, ("source", "source_id"))
_SQL_DAILY = _upsert_sql("daily_metrics", _DAILY_COLUMNS, ("source", "date", "metric"))
_SQL_BODY = _upsert_sql("body_measurements", _BODY_COLUMNS, ("source", "ts", "metric"))
_SQL_STREAMS = _upsert_sql("activity_streams", _STREAM_COLUMNS, ("source", "source_id"))
_SQL_METRICS = _upsert_sql("activity_metrics", _METRIC_COLUMNS, ("source", "source_id"))
_SQL_SERIES = _upsert_sql("daily_series", _SERIES_COLUMNS, ("source", "date", "series"))
_SQL_SETTING = _upsert_sql("athlete_settings", _SETTING_COLUMNS, ("metric", "effective_date", "origin"))


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_iso(value)
    return str(value)


def _raw(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=_json_default)


def encode_streams(streams: Mapping[str, Sequence[Any]]) -> tuple[list[str], int, bytes]:
    """``{channel: values}`` -> ``(channels, sample_count, blob)``.

    Every channel must have the same number of samples, so index ``i`` is the
    same moment in each channel.
    """
    if not streams:
        raise ValueError("streams must have at least one channel")
    lengths = {name: len(values) for name, values in streams.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError(f"stream channels differ in length: {lengths}")
    channels = sorted(streams)
    payload = json.dumps({c: list(streams[c]) for c in channels}, separators=(",", ":"))
    return channels, next(iter(lengths.values())), zlib.compress(payload.encode())


def decode_streams(encoding: str, data: bytes) -> dict[str, list[Any]]:
    if encoding != STREAM_ENCODING:
        raise ValueError(f"unknown stream encoding {encoding!r}")
    return json.loads(zlib.decompress(data))


def load_streams(conn: sqlite3.Connection, source: str, source_id: str) -> dict[str, list[Any]] | None:
    """The stored channels of one activity, or ``None`` if it has no streams."""
    row = conn.execute(
        "SELECT encoding, data FROM activity_streams WHERE source = ? AND source_id = ?", (source, source_id)
    ).fetchone()
    return decode_streams(row[0], row[1]) if row else None


def load_daily_series(conn: sqlite3.Connection, source: str, day: str, series: str) -> dict[str, list[Any]] | None:
    """One day's intraday series (``{"time": [...], series: [...]}``), or ``None``."""
    row = conn.execute(
        "SELECT encoding, data FROM daily_series WHERE source = ? AND date = ? AND series = ?", (source, day, series)
    ).fetchone()
    return decode_streams(row[0], row[1]) if row else None


def _ensure_private_file(path: Path) -> None:
    """Create the database file owner-only (health data) if it does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def connect(path: Path | str) -> sqlite3.Connection:
    """Read-write connection with WAL and the schema applied."""
    path = Path(path)
    _ensure_private_file(path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    _enable_wal(conn)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50) -> None:
    """Switching a database to WAL needs an exclusive lock and ignores
    ``busy_timeout``, so a new file opened by two processes at once can report
    "database is locked" here. Retry briefly; once in WAL this is a no-op."""
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.1)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _check_not_newer(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than this fitness-hub supports ({SCHEMA_VERSION}); "
            "upgrade fitness-hub"
        )
    return current


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a database that has ``SCHEMA`` applied up to ``SCHEMA_VERSION``.

    A database created before versioning reports ``user_version`` 0 and is
    treated as version 1. Each step re-checks the version once it holds the
    write lock, so two processes opening the database at once (the daemon and
    the MCP server) apply each migration only once.
    """
    current = _check_not_newer(conn)
    for target in range(max(current, 1) + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if schema_version(conn) < target:
                for statement in MIGRATIONS[target]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def open_readonly(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection that cannot write: ``mode=ro`` plus ``query_only``.

    The file and schema are created first if missing so read tools work on a
    fresh install. ``check_same_thread=False`` lets a web framework open the
    connection in one thread and use or close it in another (never concurrently).
    """
    path = Path(path)
    if not path.exists():
        connect(path).close()
    conn = _connect_ro(path, check_same_thread)
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    try:
        current = _check_not_newer(conn)
    except RuntimeError:
        conn.close()
        raise
    if not present.issuperset((*DATA_TABLES, "sync_state")) or current < SCHEMA_VERSION:
        conn.close()
        connect(path).close()  # apply the schema and any migrations, then reopen read-only
        conn = _connect_ro(path, check_same_thread)
    return conn


def _connect_ro(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30, check_same_thread=check_same_thread
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


class Store:
    """Write side of the database. All writes are idempotent upserts."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = connect(self.path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- upserts -----------------------------------------------------------------

    def _write(self, sql: str, columns: tuple[str, ...], rows: Iterable[Mapping[str, Any]]) -> int:
        now = now_utc_iso()
        params = []
        for row in rows:
            values = [row.get(c) for c in columns]
            values[columns.index("raw")] = _raw(row.get("raw"))
            params.append((*values, now))
        if not params:
            return 0
        with self.conn:  # one transaction per batch: all or nothing
            self.conn.executemany(sql, params)
        return len(params)

    def upsert_activities(self, rows: Iterable[Mapping[str, Any]]) -> int:
        rows = list(rows)
        for row in rows:
            for required in ("source", "source_id", "start_time", "local_date"):
                if not row.get(required):
                    raise ValueError(f"activity row missing {required}: {row.get('source_id')!r}")
            if row.get("raw") is None:
                raise ValueError("activity row missing raw payload")
        return self._write(_SQL_ACTIVITY, _ACTIVITY_COLUMNS, rows)

    def upsert_sleep_sessions(self, rows: Iterable[Mapping[str, Any]]) -> int:
        rows = list(rows)
        for row in rows:
            if not row.get("source") or not row.get("source_id"):
                raise ValueError("sleep row missing source/source_id")
        return self._write(_SQL_SLEEP, _SLEEP_COLUMNS, rows)

    def upsert_daily_metrics(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Rows without a value are skipped (nothing to store)."""
        rows = [r for r in rows if r.get("value") is not None]
        for row in rows:
            if not row.get("source") or not row.get("date") or not row.get("metric"):
                raise ValueError(f"daily metric row missing key fields: {row!r}")
        return self._write(_SQL_DAILY, _DAILY_COLUMNS, rows)

    def upsert_body_measurements(self, rows: Iterable[Mapping[str, Any]]) -> int:
        rows = [r for r in rows if r.get("value") is not None]
        for row in rows:
            if not row.get("source") or not row.get("ts") or not row.get("metric"):
                raise ValueError(f"body measurement row missing key fields: {row!r}")
        return self._write(_SQL_BODY, _BODY_COLUMNS, rows)

    # -- training analysis ---------------------------------------------------------

    def upsert_activity_streams(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Rows are ``{source, source_id, streams: {channel: values}, meta?}``."""
        now = now_utc_iso()
        params = []
        for row in rows:
            if not row.get("source") or not row.get("source_id"):
                raise ValueError("stream row missing source/source_id")
            channels, count, blob = encode_streams(row.get("streams") or {})
            channel_list = json.dumps(channels)
            params.append(
                (row["source"], row["source_id"], channel_list, count, STREAM_ENCODING, blob, _raw(row.get("meta")),
                 now)
            )
        if not params:
            return 0
        with self.conn:
            self.conn.executemany(_SQL_STREAMS, params)
        return len(params)

    def upsert_daily_series(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Rows are ``{source, date, series, streams: {"time": [...], series: [...]}}``."""
        now = now_utc_iso()
        params = []
        for row in rows:
            if not row.get("source") or not row.get("date") or not row.get("series"):
                raise ValueError("daily series row missing source/date/series")
            channels, count, blob = encode_streams(row.get("streams") or {})
            params.append((row["source"], row["date"], row["series"], json.dumps(channels), count, STREAM_ENCODING,
                           blob, now))  # fmt: skip
        if not params:
            return 0
        with self.conn:
            self.conn.executemany(_SQL_SERIES, params)
        return len(params)

    def upsert_activity_metrics(self, rows: Iterable[Mapping[str, Any]]) -> int:
        now = now_utc_iso()
        params = []
        for row in rows:
            if not row.get("source") or not row.get("source_id") or row.get("analysis_version") is None:
                raise ValueError("activity metrics row missing source/source_id/analysis_version")
            values = [row.get(c) for c in _METRIC_COLUMNS]
            for column in _METRIC_JSON_COLUMNS:
                i = _METRIC_COLUMNS.index(column)
                values[i] = _raw(values[i])
            params.append((*values, now))
        if not params:
            return 0
        with self.conn:
            self.conn.executemany(_SQL_METRICS, params)
        return len(params)

    def replace_power_curve(self, source: str, source_id: str, curve: Mapping[int, float]) -> int:
        """Store one activity's ``{duration_s: watts}`` curve, replacing the old one."""
        now = now_utc_iso()
        params = [(source, source_id, int(d), float(w), now) for d, w in sorted(curve.items())]
        with self.conn:
            self.conn.execute("DELETE FROM power_curve WHERE source = ? AND source_id = ?", (source, source_id))
            self.conn.executemany(
                "INSERT INTO power_curve (source, source_id, duration_s, watts, updated_at) VALUES (?, ?, ?, ?, ?)",
                params,
            )
        return len(params)

    def replace_durability_bests(self, source: str, source_id: str, bests: Mapping[tuple[int, int], float]) -> int:
        """Store one activity's ``{(after_kj, duration_s): watts}``, replacing the old ones."""
        now = now_utc_iso()
        params = [(source, source_id, int(a), int(d), float(w), now) for (a, d), w in sorted(bests.items())]
        with self.conn:
            self.conn.execute("DELETE FROM durability_bests WHERE source = ? AND source_id = ?", (source, source_id))
            self.conn.executemany(
                "INSERT INTO durability_bests (source, source_id, after_kj, duration_s, watts, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                params,
            )
        return len(params)

    def replace_ride_cardio(self, source: str, source_id: str, cardio: Mapping[str, Any] | None) -> None:
        """Store one workout's steady blocks and HR recoveries (``steady.ride_cardio``), or remove
        them (``None``: no power or no heart rate)."""
        with self.conn:
            if cardio is None:
                self.conn.execute("DELETE FROM ride_cardio WHERE source = ? AND source_id = ?", (source, source_id))
                return
            self.conn.execute(
                "INSERT INTO ride_cardio (source, source_id, has_gps, blocks, recoveries, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, source_id) DO UPDATE SET has_gps = excluded.has_gps, "
                "blocks = excluded.blocks, recoveries = excluded.recoveries, updated_at = excluded.updated_at",
                (source, source_id, int(bool(cardio["has_gps"])), json.dumps(cardio["blocks"]),
                 json.dumps(cardio["recoveries"]), now_utc_iso()),
            )  # fmt: skip

    def set_athlete_setting(
        self, metric: str, effective_date: str, value: float, origin: str = "manual", note: str | None = None
    ) -> None:
        if metric not in ATHLETE_SETTINGS:
            raise InputError(f"unknown athlete setting {metric!r}; expected one of {', '.join(ATHLETE_SETTINGS)}")
        if origin not in SETTING_ORIGINS:
            raise InputError(f"origin must be one of {', '.join(SETTING_ORIGINS)}")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise InputError(f"{metric} must be a positive number, got {value!r}")
        effective_date = parse_date(effective_date, "effective_date").isoformat()
        with self.conn:
            self.conn.execute(_SQL_SETTING, (metric, effective_date, origin, value, note, now_utc_iso()))

    def delete_athlete_setting(self, metric: str, effective_date: str, origin: str = "manual") -> bool:
        effective_date = parse_date(effective_date, "effective_date").isoformat()
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM athlete_settings WHERE metric = ? AND effective_date = ? AND origin = ?",
                (metric, effective_date, origin),
            )
        return cursor.rowcount > 0

    # -- sync bookkeeping --------------------------------------------------------

    def get_state(self, source: str, stream: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE source = ? AND stream = ?", (source, stream)
        ).fetchone()
        return dict(row) if row else None

    def get_cursor(self, source: str, stream: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM sync_state WHERE source = ? AND stream = ?", (source, stream)
        ).fetchone()
        return row["cursor"] if row else None

    def record_sync(
        self,
        source: str,
        stream: str,
        *,
        cursor: str | None = None,
        error: str | None = None,
        rows: int = 0,
    ) -> None:
        """Record one stream attempt.

        Success sets ``last_success`` and clears ``last_error``. An error keeps
        the previous ``last_success``. A ``None`` cursor never erases a stored
        cursor; providers pass a cursor with an error only for verified partial
        progress (e.g. the last fully synced day), so the next run resumes there.
        """
        now = now_utc_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sync_state
                    (source, stream, cursor, last_attempt, last_success, last_error, rows_upserted)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, stream) DO UPDATE SET
                    cursor = COALESCE(excluded.cursor, sync_state.cursor),
                    last_attempt = excluded.last_attempt,
                    last_success = COALESCE(excluded.last_success, sync_state.last_success),
                    last_error = excluded.last_error,
                    rows_upserted = excluded.rows_upserted
                """,
                (source, stream, cursor, now, None if error else now, error, rows),
            )

    def sync_states(self) -> list[dict[str, Any]]:
        return sync_states(self.conn)

    def row_counts(self) -> dict[str, int]:
        return row_counts(self.conn)


def sync_states(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM sync_state ORDER BY source, stream").fetchall()
    return [dict(r) for r in rows]


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DATA_TABLES}
