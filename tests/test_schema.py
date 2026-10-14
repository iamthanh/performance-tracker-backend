from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from fitness_hub.db import (
    SCHEMA,
    SCHEMA_VERSION,
    Store,
    connect,
    load_streams,
    open_readonly,
    schema_version,
)
from fitness_hub.queries import run_select

V2_TABLES = {"activity_streams", "activity_metrics", "power_curve", "daily_series", "athlete_settings"}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _v1_database(path: Path) -> None:
    """A database as created before schema versioning, with one activity in it."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO activities (source, source_id, start_time, local_date, raw, updated_at) "
        "VALUES ('strava', '1', '2026-10-01T07:00:00Z', '2026-10-01', '{}', '2026-10-01T08:00:00Z')"
    )
    conn.commit()
    conn.close()


# -- migrations ----------------------------------------------------------------------


def test_new_database_is_at_current_version(store: Store) -> None:
    assert schema_version(store.conn) == SCHEMA_VERSION
    assert V2_TABLES <= _tables(store.conn)


def test_unversioned_database_is_migrated_and_keeps_its_data(tmp_path: Path) -> None:
    path = tmp_path / "fitness.db"
    _v1_database(path)
    with Store(path) as store:
        assert schema_version(store.conn) == SCHEMA_VERSION
        assert V2_TABLES <= _tables(store.conn)
        assert store.row_counts()["activities"] == 1


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "fitness.db"
    for _ in range(3):
        connect(path).close()
    with Store(path) as store:
        assert schema_version(store.conn) == SCHEMA_VERSION


def test_open_readonly_migrates_an_old_database(tmp_path: Path) -> None:
    path = tmp_path / "fitness.db"
    _v1_database(path)
    conn = open_readonly(path)
    try:
        assert schema_version(conn) == SCHEMA_VERSION
        assert V2_TABLES <= _tables(conn)
    finally:
        conn.close()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fitness.db"
    connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this fitness-hub"):
        connect(path)
    with pytest.raises(RuntimeError, match="newer than this fitness-hub"):
        open_readonly(path)


@pytest.mark.parametrize("existing", [True, False])
def test_concurrent_opens(tmp_path: Path, existing: bool) -> None:
    """The daemon and the MCP server may open the database at the same moment."""
    path = tmp_path / "fitness.db"
    if existing:
        _v1_database(path)
    errors: list[BaseException] = []

    def open_db() -> None:
        try:
            connect(path).close()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=open_db) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Store(path) as store:
        assert schema_version(store.conn) == SCHEMA_VERSION


# -- streams ---------------------------------------------------------------------------

STREAMS = {
    "time": [0, 1, 2, 3],
    "watts": [150, 210, None, 260],
    "heartrate": [120, 124, 127, 131],
    "latlng": [[52.5, 13.4], [52.5, 13.41], [52.51, 13.41], [52.51, 13.42]],
}


def test_streams_round_trip(store: Store) -> None:
    store.upsert_activity_streams(
        [{"source": "strava", "source_id": "1", "streams": STREAMS, "meta": {"watts": {"resolution": "high"}}}]
    )
    assert load_streams(store.conn, "strava", "1") == STREAMS
    row = store.conn.execute("SELECT channels, sample_count, encoding, meta FROM activity_streams").fetchone()
    assert row["channels"] == '["heartrate", "latlng", "time", "watts"]'
    assert row["sample_count"] == 4
    assert row["encoding"] == "zlib+json"
    assert '"resolution":"high"' in row["meta"]


def test_streams_upsert_replaces(store: Store) -> None:
    for watts in ([100, 100, 100, 100], [200, 200, 200, 200]):
        store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": {**STREAMS, "watts": watts}}])
    assert store.conn.execute("SELECT COUNT(*) FROM activity_streams").fetchone()[0] == 1
    assert load_streams(store.conn, "strava", "1")["watts"] == [200, 200, 200, 200]


def test_streams_are_compressed(store: Store) -> None:
    n = 7200  # a two-hour ride at 1 Hz
    streams = {"time": list(range(n)), "watts": [200 + i % 40 for i in range(n)], "cadence": [90] * n}
    store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": streams}])
    size = store.conn.execute("SELECT length(data) FROM activity_streams").fetchone()[0]
    assert size < 20_000


def test_streams_reject_ragged_channels(store: Store) -> None:
    with pytest.raises(ValueError, match="differ in length"):
        ragged = {"time": [0, 1], "watts": [1]}
        store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": ragged}])
    with pytest.raises(ValueError, match="at least one channel"):
        store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": {}}])


def test_missing_streams_load_as_none(store: Store) -> None:
    assert load_streams(store.conn, "strava", "nope") is None


# -- derived metrics -----------------------------------------------------------------


def test_activity_metrics_upsert(store: Store) -> None:
    row = {
        "source": "strava",
        "source_id": "1",
        "analysis_version": 1,
        "ftp_w": 250.0,
        "normalized_power_w": 230.0,
        "intensity_factor": 0.92,
        "tss": 84.6,
        "tss_method": "power",
        "power_zone_s": [600, 1200, 900, 300, 0, 0, 0],
    }
    store.upsert_activity_metrics([row])
    store.upsert_activity_metrics([{**row, "tss": 90.0}])
    got = store.conn.execute("SELECT tss, power_zone_s, hr_zone_s FROM activity_metrics").fetchall()
    assert len(got) == 1
    assert got[0]["tss"] == 90.0
    assert got[0]["power_zone_s"] == "[600,1200,900,300,0,0,0]"
    assert got[0]["hr_zone_s"] is None
    with pytest.raises(ValueError, match="analysis_version"):
        store.upsert_activity_metrics([{"source": "strava", "source_id": "2"}])


def test_power_curve_is_replaced(store: Store) -> None:
    store.replace_power_curve("strava", "1", {1: 900, 5: 700, 60: 400, 1200: 260})
    store.replace_power_curve("strava", "1", {1: 950, 5: 720})
    rows = store.conn.execute("SELECT duration_s, watts FROM power_curve ORDER BY duration_s").fetchall()
    assert [tuple(r) for r in rows] == [(1, 950.0), (5, 720.0)]


def test_athlete_settings(store: Store) -> None:
    store.set_athlete_setting("ftp_w", "2026-09-01", 250, origin="auto")
    store.set_athlete_setting("ftp_w", "2026-09-01", 262, note="ramp test")
    store.set_athlete_setting("ftp_w", "2026-09-01", 265)  # same key: updated, not duplicated
    rows = store.conn.execute("SELECT origin, value, note FROM athlete_settings ORDER BY origin").fetchall()
    assert [tuple(r) for r in rows] == [("auto", 250.0, None), ("manual", 265.0, None)]
    assert store.delete_athlete_setting("ftp_w", "2026-09-01T00:00:00") is True  # normalized like set
    assert store.delete_athlete_setting("ftp_w", "2026-09-01") is False
    with pytest.raises(ValueError, match="unknown athlete setting"):
        store.set_athlete_setting("vo2", "2026-09-01", 55)
    with pytest.raises(ValueError, match="origin"):
        store.set_athlete_setting("ftp_w", "2026-09-01", 250, origin="garmin")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        store.set_athlete_setting("ftp_w", "Sept 1", 250)


def test_query_sql_reads_new_tables(store: Store) -> None:
    store.replace_power_curve("strava", "1", {60: 400})
    result = run_select(store.conn, "SELECT duration_s, watts FROM power_curve")
    assert result["rows"] == [[60, 400.0]]
    store.upsert_activity_streams([{"source": "strava", "source_id": "1", "streams": STREAMS}])
    result = run_select(store.conn, "SELECT sample_count, data FROM activity_streams")
    count, data = result["rows"][0]
    assert count == 4
    assert data.startswith("<blob ") and data.endswith(" bytes>")  # never the blob itself
    with pytest.raises(ValueError, match="only a single SELECT"):
        run_select(store.conn, "DELETE FROM power_curve")
