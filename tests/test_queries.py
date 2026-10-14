from __future__ import annotations

from datetime import date

import pytest

from fitness_hub import queries
from fitness_hub.db import Store, open_readonly


def act(source: str, source_id: str, start: str, activity_type: str, distance_m: float = 30000, **extra) -> dict:
    return {
        "source": source,
        "source_id": source_id,
        "start_time": start,
        "local_date": start[:10],
        "activity_type": activity_type,
        "name": f"{source} {source_id}",
        "distance_m": distance_m,
        "moving_s": 3600,
        "elevation_gain_m": 100,
        "avg_hr": 140,
        "raw": {"id": source_id},
        **extra,
    }


SAME_RIDE = [
    act("garmin", "g1", "2026-10-06T07:00:05Z", "road_biking", calories=800),
    act("strava", "s1", "2026-10-06T07:01:30Z", "Ride", avg_power_w=210),
    act("google_health", "x1", "2026-10-06T06:59:00Z", "OUTDOOR_BIKE"),
]


@pytest.fixture
def seeded(store: Store) -> Store:
    store.upsert_activities(
        SAME_RIDE
        + [
            act("strava", "s2", "2026-10-08T17:00:00Z", "Run", distance_m=10000),
            act("garmin", "g2", "2026-10-13T06:00:00Z", "road_biking", distance_m=50000),
            act("garmin", "g3", "2026-10-13T06:02:00Z", "road_biking", distance_m=5000),  # same source: kept apart
            act("strava", "s3", "2026-09-22T07:00:00Z", "VirtualRide", distance_m=20000),
        ]
    )
    store.upsert_daily_metrics(
        [
            {"source": "garmin", "date": "2026-10-10", "metric": "resting_hr", "value": 50},
            {"source": "google_health", "date": "2026-10-10", "metric": "resting_hr", "value": 52},
            {"source": "garmin", "date": "2026-10-10", "metric": "steps", "value": 9000},
            {"source": "garmin", "date": "2026-08-01", "metric": "steps", "value": 1},
        ]
    )
    store.upsert_sleep_sessions(
        [
            {"source": "garmin", "source_id": "a", "local_date": "2026-10-10", "minutes_asleep": 420, "is_main": 1},
            {"source": "garmin", "source_id": "nap", "local_date": "2026-10-10", "minutes_asleep": 30, "is_main": 0},
        ]
    )
    store.upsert_body_measurements(
        [
            {"source": "garmin", "ts": "2026-10-10T06:00:00Z", "metric": "weight_kg", "value": 72.4},
            {"source": "garmin", "ts": "2026-10-10T06:00:00Z", "metric": "body_fat_pct", "value": 18.0},
            {"source": "garmin", "ts": "2026-01-01T06:00:00Z", "metric": "weight_kg", "value": 75.0},
        ]
    )
    return store


def test_dedupe_prefers_device_source() -> None:
    [kept] = queries.dedupe_activities(SAME_RIDE)
    assert kept["source"] == "garmin" and kept["source_id"] == "g1"
    assert kept["also_recorded_by"] == ["google_health", "strava"]


def test_dedupe_keeps_same_source_and_far_apart_rows() -> None:
    rows = [
        act("garmin", "a", "2026-10-13T06:00:00Z", "cycling"),
        act("garmin", "b", "2026-10-13T06:02:00Z", "cycling"),
        act("strava", "c", "2026-10-13T06:10:00Z", "Ride"),  # 10 min later: different workout
    ]
    assert len(queries.dedupe_activities(rows)) == 3


def test_dedupe_strava_only_and_csv_export() -> None:
    rows = [
        act("strava", "s", "2026-10-13T06:00:00Z", "Ride"),
        act("garmin_export", "e", "2026-10-13T06:01:00", "Cycling"),  # naive -> treated as UTC
    ]
    [kept] = queries.dedupe_activities(rows)
    assert kept["source"] == "strava" and kept["also_recorded_by"] == ["garmin_export"]


def test_list_activities_dedupes_unless_source_given(seeded: Store) -> None:
    rows = queries.list_activities(seeded.conn)
    assert [r["source_id"] for r in rows] == ["g3", "g2", "s2", "g1", "s3"]  # newest first
    assert rows[3]["also_recorded_by"] == ["google_health", "strava"]
    assert rows[3]["sport"] == "cycling"
    strava = queries.list_activities(seeded.conn, source="strava")
    assert {r["source_id"] for r in strava} == {"s1", "s2", "s3"}
    assert queries.list_activities(seeded.conn, limit=2)[1]["source_id"] == "g2"


def test_list_activities_filters(seeded: Store) -> None:
    cycling = queries.list_activities(seeded.conn, activity_type="cycling")
    assert {r["source_id"] for r in cycling} == {"g1", "g2", "g3", "s3"}
    assert {r["source_id"] for r in queries.list_activities(seeded.conn, activity_type="ride")} == {
        "g1",
        "g2",
        "g3",
        "s3",
    }
    runs = queries.list_activities(seeded.conn, activity_type="Run")
    assert [r["source_id"] for r in runs] == ["s2"]
    october = queries.list_activities(seeded.conn, start_date="2026-10-07", end_date="2026-10-12")
    assert [r["source_id"] for r in october] == ["s2"]
    with pytest.raises(ValueError, match="start_date"):
        queries.list_activities(seeded.conn, start_date="10/07/2026")


def test_get_activity(seeded: Store) -> None:
    row = queries.get_activity(seeded.conn, "strava", "s1", include_raw=True)
    assert row["raw"] == {"id": "s1"} and row["avg_power_w"] == 210
    assert "raw" not in queries.get_activity(seeded.conn, "strava", "s1")
    assert queries.get_activity(seeded.conn, "strava", "nope") is None


def test_activity_summary_weekly_does_not_double_count(seeded: Store) -> None:
    summary = queries.activity_summary(seeded.conn, "2026-09-21", "2026-10-14", "week", activity_type="cycling")
    weeks = {g["week"]: g for g in summary["groups"]}
    assert list(weeks) == ["2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12"]  # Mondays, empty weeks kept
    assert weeks["2026-09-28"]["count"] == 0
    assert weeks["2026-10-05"]["count"] == 1  # one ride recorded by three services
    assert weeks["2026-10-05"]["distance_km"] == 30.0
    assert weeks["2026-10-05"]["calories"] == 800
    assert weeks["2026-10-12"]["count"] == 2 and weeks["2026-10-12"]["distance_km"] == 55.0
    assert summary["total"]["count"] == 4 and summary["total"]["distance_km"] == 105.0
    assert summary["total"]["moving_hours"] == 4.0 and summary["total"]["avg_of_avg_hr"] == 140.0


def test_activity_summary_by_type_and_month(seeded: Store) -> None:
    by_type = queries.activity_summary(seeded.conn, "2026-09-01", "2026-10-31", "type")
    sports = {g["sport"]: g["count"] for g in by_type["groups"]}
    assert sports == {"cycling": 4, "running": 1}
    by_month = queries.activity_summary(seeded.conn, "2026-09-01", "2026-10-31", "month")
    assert [(g["month"], g["count"]) for g in by_month["groups"]] == [("2026-09", 1), ("2026-10", 4)]
    with pytest.raises(ValueError, match="group_by"):
        queries.activity_summary(seeded.conn, "2026-09-01", "2026-10-31", "fortnight")


def test_daily_metrics_sleep_body(seeded: Store) -> None:
    today = date(2026, 10, 14)
    rows = queries.daily_metrics(seeded.conn, today=today)
    assert {(r["metric"], r["source"]) for r in rows} == {
        ("resting_hr", "garmin"),
        ("resting_hr", "google_health"),
        ("steps", "garmin"),
    }  # 2026-08-01 is outside the default 30 days; sources are not merged
    only_hr = queries.daily_metrics(seeded.conn, metrics=["resting_hr"], source="garmin", today=today)
    assert only_hr == [{"date": "2026-10-10", "metric": "resting_hr", "source": "garmin", "value": 50.0}]

    assert [s["source_id"] for s in queries.sleep_sessions(seeded.conn, today=today)] == ["a"]
    assert len(queries.sleep_sessions(seeded.conn, main_only=False, today=today)) == 2

    body = queries.body_measurements(seeded.conn, today=today)
    assert {(b["metric"], b["value"]) for b in body} == {("weight_kg", 72.4), ("body_fat_pct", 18.0)}
    year = queries.body_measurements(seeded.conn, "2026-01-01", "2026-12-31", metric="weight_kg")
    assert [b["value"] for b in year] == [75.0, 72.4]


def test_sync_status(seeded: Store) -> None:
    seeded.record_sync("strava", "activities", cursor="2026-10-06T07:01:30Z", rows=3)
    status = queries.sync_status(seeded.conn)
    assert status["row_counts"]["activities"] == 7
    assert status["streams"][0]["source"] == "strava"


# -- run_select safety ------------------------------------------------------------------


def test_run_select_returns_rows(seeded: Store, settings) -> None:
    conn = open_readonly(settings.db_path)
    try:
        out = queries.run_select(conn, "-- weekly\nSELECT source, COUNT(*) AS n FROM activities GROUP BY source ORDER BY source;")
        assert out["columns"] == ["source", "n"]
        assert out["rows"][0] == ["garmin", 3] and out["truncated"] is False
        limited = queries.run_select(conn, "WITH x AS (SELECT * FROM activities) SELECT source_id FROM x", limit=2)
        assert len(limited["rows"]) == 2 and limited["truncated"] is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM activities",
        "UPDATE activities SET name = 'x'",
        "DROP TABLE activities",
        "CREATE TABLE evil (x)",
        "INSERT INTO activities (source) VALUES ('x')",
        "PRAGMA query_only = OFF",
        "PRAGMA table_info(activities)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "SELECT 1; DELETE FROM activities",
        "select 1;drop table activities;",
        "/* hi */ DELETE FROM activities",
        "WITH x AS (SELECT 1) DELETE FROM activities",
        "WITH x AS (SELECT 1) INSERT INTO sync_state (source, stream) VALUES ('a', 'b')",
        "",
    ],
)
def test_run_select_cannot_modify(seeded: Store, sql: str) -> None:
    before = seeded.row_counts()
    # even on a read-write connection the validator + authorizer refuse writes
    with pytest.raises(ValueError):
        queries.run_select(seeded.conn, sql)
    assert seeded.row_counts() == before
    assert seeded.conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0


def test_run_select_time_limit(seeded: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queries, "SQL_TIMEOUT_S", 0.2)
    with pytest.raises(ValueError, match="time limit"):
        queries.run_select(
            seeded.conn, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
        )
    # the connection is usable afterwards and no handler is left behind
    assert seeded.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 7


def test_sport_family() -> None:
    assert queries.sport_family("VirtualRide") == "cycling"
    assert queries.sport_family("OUTDOOR_BIKE") == "cycling"
    assert queries.sport_family("trail_running") == "running"
    assert queries.sport_family("Pilates") == "pilates"
    assert queries.sport_family(None) == "other"
