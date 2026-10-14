from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fit_builder import ride
from fitness_hub.activity_files import parse_file, parse_fit, parse_gpx, parse_strava_json
from fitness_hub.cli import main
from fitness_hub.config import Settings
from fitness_hub.db import Store, load_streams
from fitness_hub.providers.strava_export import import_strava_export, map_csv_row, read_activities_csv
from fitness_hub.queries import list_activities

# The real export repeats column names; this subset keeps the repeats that matter.
HEADER = [
    "Activity ID", "Activity Date", "Activity Name", "Activity Type", "Elapsed Time", "Distance",
    "Max Heart Rate", "Filename", "Elapsed Time", "Moving Time", "Distance", "Elevation Gain",
    "Max Heart Rate", "Average Heart Rate", "Average Watts", "Calories",
]  # fmt: skip

GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
 <trk><trkseg>
  <trkpt lat="37.1" lon="-122.1"><ele>10.5</ele><time>2026-09-01T16:00:00Z</time>
   <extensions><power>180</power><gpxtpx:TrackPointExtension><gpxtpx:hr>120</gpxtpx:hr><gpxtpx:cad>85</gpxtpx:cad>
   </gpxtpx:TrackPointExtension></extensions></trkpt>
  <trkpt lat="37.2" lon="-122.2"><ele>11</ele><time>2026-09-01T16:00:02Z</time></trkpt>
 </trkseg></trk>
</gpx>"""


def csv_row(activity_id: str, date: str, filename: str, *, name: str = "Ride", watts: str = "200") -> list[str]:
    return [activity_id, date, name, "Ride", "3600", "30.00", "170.0", filename, "3600.0", "3500.0", "30000.5",
            "300.0", "171.0", "140.0", watts, "800.0"]  # fmt: skip


def write_export(root: Path, rows: list[list[str]], files: dict[str, bytes]) -> Path:
    (root / "activities").mkdir(parents=True)
    with (root / "activities.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)
    for name, data in files.items():
        (root / name).write_bytes(data)
    return root


@pytest.fixture
def export(tmp_path: Path) -> Path:
    evening_ride = ride(datetime(2026, 10, 2, 1, 30, tzinfo=UTC), [200] * 60 + [300] * 60)
    zwift = ride(datetime(2026, 10, 3, 2, 0, tzinfo=UTC), [250] * 30, local_offset_s=-1_000_000_000)
    return write_export(
        tmp_path / "export",
        [
            csv_row("101", "Oct 2, 2026, 1:30:00 AM", "activities/9001.fit.gz"),
            csv_row("102", "Oct 3, 2026, 2:00:00 AM", "activities/9002.fit.gz", name="Zwift - Watopia"),
            csv_row("103", "Sep 1, 2026, 4:00:00 PM", "activities/9003.gpx"),
            csv_row("104", "Aug 1, 2026, 4:00:00 PM", ""),  # manual entry, no file
            csv_row("105", "Jul 1, 2026, 4:00:00 PM", "activities/9005.fit.gz"),  # file missing from the archive
        ],
        {
            "activities/9001.fit.gz": gzip.compress(evening_ride),
            "activities/9002.fit.gz": gzip.compress(zwift),
            "activities/9003.gpx": GPX,
        },
    )


# -- file parsers ----------------------------------------------------------------------


def test_parse_fit_streams_and_meta() -> None:
    parsed = parse_fit(io.BytesIO(ride(datetime(2026, 10, 1, 1, 30, tzinfo=UTC), [200, 210, 220])))
    assert parsed.start_time == datetime(2026, 10, 1, 1, 30, tzinfo=UTC)
    assert parsed.local_offset_s == -7 * 3600
    s = parsed.streams
    assert s["time"] == [0, 1, 2]
    assert s["power"] == [200, 210, 220]
    assert s["heart_rate"] == [140] * 3
    assert s["speed"] == [8.0] * 3
    assert s["distance"] == [0, 8, 16]
    assert s["altitude"] == [100.0] * 3
    assert s["lat"][0] == pytest.approx(37.4) and s["lng"][0] == pytest.approx(-122.0)
    assert parsed.meta["session"]["threshold_power"] == 250
    assert parsed.meta["laps"][0]["avg_power"] == 210


def test_parse_fit_drops_empty_channels_and_bad_offsets() -> None:
    data = ride(datetime(2026, 10, 1, tzinfo=UTC), [100, 100], heart_rate=None, local_offset_s=-1_000_000_000)
    parsed = parse_fit(io.BytesIO(data))
    assert "heart_rate" not in parsed.streams
    assert parsed.local_offset_s is None


def test_parse_gpx_with_extensions() -> None:
    parsed = parse_gpx(GPX)
    assert parsed.streams["time"] == [0, 2]
    assert parsed.streams["power"] == [180, None]
    assert parsed.streams["heart_rate"] == [120, None]
    assert parsed.streams["cadence"] == [85, None]
    assert parsed.streams["altitude"] == [10.5, 11.0]
    assert parsed.local_offset_s is None


def test_parse_strava_json() -> None:
    doc = {
        "metadata": {"activity_type": "ride"},
        "data": [
            {
                "fields": ["time", "latlng", "elevation", "speed", "distance"],
                "values": [[1756742400.0, [37.1, -122.1], 5.0, -1, 0], [1756742401.0, [37.2, -122.2], 6.0, 4.2, 4.2]],
            }
        ],
    }
    parsed = parse_strava_json(doc)
    assert parsed.streams["time"] == [0, 1]
    assert parsed.streams["speed"] == [None, 4.2]  # -1 means "no speed"
    assert parsed.streams["lat"] == [37.1, 37.2]


def test_parse_file_handles_gzip_and_rejects_unknown(tmp_path: Path) -> None:
    path = tmp_path / "a.gpx.gz"
    path.write_bytes(gzip.compress(GPX))
    assert parse_file(path).sample_count == 2
    other = tmp_path / "a.tcx.gz"
    other.write_bytes(gzip.compress(b"<x/>"))
    with pytest.raises(ValueError, match="unsupported"):
        parse_file(other)


# -- activities.csv -------------------------------------------------------------------


def test_repeated_headers_are_kept_apart(export: Path) -> None:
    rows = read_activities_csv(export / "activities.csv")
    assert rows[0]["Distance"] == "30.00"
    assert rows[0]["Distance #2"] == "30000.5"
    row = map_csv_row(rows[0])
    assert row["distance_m"] == 30000.5
    assert row["duration_s"] == 3600.0
    assert row["max_hr"] == 171.0
    assert row["start_time"] == "2026-10-02T01:30:00Z"


def test_summary_distance_is_used_when_detail_is_empty(export: Path) -> None:
    raw = read_activities_csv(export / "activities.csv")[0]
    assert map_csv_row({**raw, "Distance #2": ""})["distance_m"] == 30000.0


def test_not_an_export(tmp_path: Path) -> None:
    (tmp_path / "activities.csv").write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="Strava export"):
        read_activities_csv(tmp_path / "activities.csv")
    with pytest.raises(ValueError, match="no activities.csv"):
        import_strava_export(Store(tmp_path / "db.sqlite"), tmp_path / "nowhere")


# -- importer -------------------------------------------------------------------------


def test_import(store: Store, export: Path) -> None:
    result = import_strava_export(store, export, timezone="America/Los_Angeles", workers=1)
    assert result["activities"] == 5
    assert result["streams_imported"] == 3
    assert result["files_missing"] == 1

    dates = dict(store.conn.execute("SELECT source_id, local_date FROM activities"))
    assert dates["101"] == "2026-10-01"  # 01:30 UTC is the evening before in UTC-7 (from the FIT file)
    assert dates["102"] == "2026-10-02"  # Zwift's bogus offset is ignored; --timezone is used instead
    assert dates["103"] == "2026-09-01"  # GPX: --timezone

    streams = load_streams(store.conn, "strava", "101")
    assert streams["power"] == [200] * 60 + [300] * 60
    meta = json.loads(store.conn.execute("SELECT meta FROM activity_streams WHERE source_id = '101'").fetchone()[0])
    assert meta["file"] == "9001.fit.gz"
    assert meta["local_offset_s"] == -25200
    assert meta["session"]["threshold_power"] == 250
    assert store.get_state("strava", "export")["rows_upserted"] == 5


def test_reimport_skips_parsed_files_and_keeps_local_dates(store: Store, export: Path) -> None:
    sql = (
        "SELECT a.source_id, a.local_date, s.updated_at FROM activity_streams s "
        "JOIN activities a USING (source, source_id) ORDER BY a.source_id"
    )
    import_strava_export(store, export, timezone="America/Los_Angeles", workers=1)
    before = [tuple(r) for r in store.conn.execute(sql)]
    result = import_strava_export(store, export, workers=1)  # no --timezone this time
    assert result["streams_imported"] == 0
    assert result["streams_already_stored"] == 3
    after = [tuple(r) for r in store.conn.execute(sql)]
    # The FIT ride keeps the local date from its stored offset, and no stream row is rewritten.
    assert after[0][:2] == before[0][:2] == ("101", "2026-10-01")
    assert [r[2] for r in after] == [r[2] for r in before]
    assert store.row_counts()["activities"] == 5


def test_force_reparses(store: Store, export: Path) -> None:
    import_strava_export(store, export, workers=1)
    assert import_strava_export(store, export, force=True, workers=1)["streams_imported"] == 3


def test_parallel_import_matches_serial(tmp_path: Path, export: Path) -> None:
    results = []
    for workers in (1, 2):
        with Store(tmp_path / f"w{workers}.db") as store:
            import_strava_export(store, export, timezone="America/Los_Angeles", workers=workers)
            results.append(
                [tuple(r) for r in store.conn.execute(
                    "SELECT a.source_id, a.local_date, s.sample_count FROM activities a "
                    "LEFT JOIN activity_streams s USING (source, source_id) ORDER BY a.source_id")]
            )  # fmt: skip
    assert results[0] == results[1]


def test_broken_file_does_not_stop_import(store: Store, export: Path) -> None:
    (export / "activities/9001.fit.gz").write_bytes(gzip.compress(b"not a fit file"))
    result = import_strava_export(store, export, workers=1)
    assert result["files_failed"] == 1
    assert result["streams_imported"] == 2
    assert store.row_counts()["activities"] == 5  # the summary row is still stored


def test_imported_rides_dedupe_against_garmin(store: Store, export: Path) -> None:
    import_strava_export(store, export, workers=1)
    store.upsert_activities(
        [{"source": "garmin", "source_id": "g1", "start_time": "2026-10-02T01:30:40Z", "local_date": "2026-10-01",
          "activity_type": "road_biking", "raw": {}}]  # fmt: skip
    )
    rides = list_activities(store.conn, start_date="2026-10-01", end_date="2026-10-01")
    assert len(rides) == 1
    assert rides[0]["also_recorded_by"] == ["strava"]


def test_bad_timezone(store: Store, export: Path) -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        import_strava_export(store, export, timezone="Mars/Olympus")


def test_cli(settings: Settings, export: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("FITNESS_HUB_HOME", str(settings.home))
    assert main(["import-strava-export", str(export), "--workers", "1", "--timezone", "America/Los_Angeles"]) == 0
    out = capsys.readouterr().out
    assert "Imported 5 Strava activities; streams parsed for 3" in out
    assert "files missing: 1" in out
