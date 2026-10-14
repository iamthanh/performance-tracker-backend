"""SQLite schema, idempotent upserts and sync bookkeeping.

Timestamps are UTC ISO-8601 text (``YYYY-MM-DDTHH:MM:SSZ``); dates are
``YYYY-MM-DD`` on the athlete's local clock. Every row keeps the provider
payload in ``raw`` and every write is ``INSERT ... ON CONFLICT DO UPDATE``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .util import now_utc_iso

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
SOURCES = ("strava", "garmin", "google_health", "garmin_export")

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


def _raw(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


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
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def open_readonly(path: Path | str) -> sqlite3.Connection:
    """Open a connection that cannot write: ``mode=ro`` plus ``query_only``.

    The file and schema are created first if missing so read tools work on a
    fresh install.
    """
    path = Path(path)
    if not path.exists():
        connect(path).close()
    conn = _connect_ro(path)
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not present.issuperset((*DATA_TABLES, "sync_state")):
        conn.close()
        connect(path).close()  # apply the idempotent schema, then reopen read-only
        conn = _connect_ro(path)
    return conn


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
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

    # -- sync bookkeeping --------------------------------------------------------

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
