from __future__ import annotations

import gzip
import warnings
from datetime import UTC, datetime

import pytest
from fit_builder import ride

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's TestClient warns about httpx; it still works
    from fastapi.testclient import TestClient

from fitness_hub import queries, uploads
from fitness_hub.activity_files import parse_bytes
from fitness_hub.api import create_app
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.util import InputError

START = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)  # 20:00 on Aug 31 in the file's local clock (UTC-7)
FIT = ride(START, [200] * 3600)
GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
 <trk><name>Track session</name><type>running</type><trkseg>
  <trkpt lat="48.8500" lon="2.3500"><ele>35</ele><time>2026-09-01T05:30:00Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions></trkpt>
  <trkpt lat="48.8510" lon="2.3500"><ele>36</ele><time>2026-09-01T05:30:30Z</time></trkpt>
  <trkpt lat="48.8520" lon="2.3500"><ele>40</ele><time>2026-09-01T05:31:00Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>160</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions></trkpt>
 </trkseg></trk>
</gpx>"""


@pytest.fixture
def store(settings: Settings):
    with Store(settings.db_path) as s:
        yield s


def activity(store: Store, source_id: str) -> dict:
    return dict(store.conn.execute("SELECT * FROM activities WHERE source = 'upload' AND source_id = ?",
                                   (source_id,)).fetchone())  # fmt: skip


def test_fit_upload_is_stored_and_analysed(store: Store) -> None:
    result = uploads.import_activity_file(store, "Morning_ride.fit", FIT)
    a = result["activity"]
    assert result["created"] is True and a["source"] == "upload" and len(a["source_id"]) == 24
    assert a["local_date"] == "2026-08-31"  # from the file's own local clock
    assert a["activity_type"] == "Ride" and a["sport"] == "cycling" and a["name"] == "Evening Ride"
    assert a["samples"] == 3600 and {"power", "heart_rate", "lat"} <= set(a["channels"])
    assert a["tss"] == pytest.approx(100) and a["tss_method"] == "power"  # an hour at 200 W: FTP 200, TSS 100
    row = activity(store, a["source_id"])
    assert row["start_time"] == "2026-09-01T03:00:00Z" and row["duration_s"] == pytest.approx(3600)
    assert row["avg_hr"] == pytest.approx(140) and row["avg_power_w"] == pytest.approx(200)
    assert row["distance_m"] == pytest.approx(3599 * 8)  # the distance channel's last value
    streams = store.conn.execute("SELECT sample_count FROM activity_streams WHERE source = 'upload'").fetchone()
    assert streams["sample_count"] == 3600
    # The same file again: the same workout, updated, not a second one.
    again = uploads.import_activity_file(store, "copy.fit", FIT, name="Renamed")
    assert again["created"] is False and again["activity"]["source_id"] == a["source_id"]
    assert store.conn.execute("SELECT COUNT(*) FROM activities WHERE source = 'upload'").fetchone()[0] == 1
    assert activity(store, a["source_id"])["name"] == "Renamed"
    listed = queries.activities_with_metrics(store.conn, "2026-08-31", "2026-08-31")
    assert [(r["source"], r["has_streams"], r["analysed"]) for r in listed] == [("upload", True, True)]


def test_gzipped_fit_and_overrides(store: Store) -> None:
    result = uploads.import_activity_file(store, "ride.fit.gz", gzip.compress(FIT), name="  Trainer  ",
                                          activity_type="VirtualRide")  # fmt: skip
    assert result["activity"]["name"] == "Trainer" and result["activity"]["activity_type"] == "VirtualRide"
    assert result["activity"]["sport"] == "cycling"


def test_gpx_upload_works_out_its_totals(store: Store) -> None:
    result = uploads.import_activity_file(store, "run.gpx", GPX, timezone="Europe/Paris")
    a = result["activity"]
    assert a["local_date"] == "2026-09-01" and a["activity_type"] == "Run" and a["name"] == "Track session"
    row = activity(store, a["source_id"])
    assert row["duration_s"] == 60 and row["distance_m"] == pytest.approx(222.4, abs=1)  # 0.002 deg of latitude
    assert row["elevation_gain_m"] == pytest.approx(5)  # 35 -> 40 (the 1 m step alone is noise)
    assert row["avg_hr"] == 155 and row["max_hr"] == 160 and row["avg_power_w"] is None
    # Without a time zone the UTC date is used; a bad zone is the user's to fix.
    assert uploads.import_activity_file(store, "run2.gpx", GPX.replace(b"05:3", b"23:3"))["activity"]["local_date"] == "2026-09-01"
    with pytest.raises(InputError, match="unknown timezone"):
        uploads.import_activity_file(store, "run.gpx", GPX, timezone="Mars/Olympus")


def test_upload_reports_the_same_workout_from_another_service(store: Store) -> None:
    store.upsert_activities([{"source": "strava", "source_id": "123", "start_time": "2026-09-01T03:01:00Z",
                              "local_date": "2026-08-31", "activity_type": "Ride", "name": "From Strava",
                              "raw": {}}])  # fmt: skip
    result = uploads.import_activity_file(store, "ride.fit", FIT)
    assert result["also_stored_as"] == [{"source": "strava", "source_id": "123", "name": "From Strava"}]
    listed = queries.activities_with_metrics(store.conn, "2026-08-31", "2026-08-31")
    assert len(listed) == 1 and listed[0]["source"] == "upload"  # merged; the analysed copy is shown


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("notes.txt", b"hello", "only .fit, .gpx and .tcx"),
        ("export.json", b"{}", "only .fit, .gpx and .tcx"),
        ("", FIT, "needs a name"),
        ("ride.fit", b"", "is empty"),
        ("ride.fit", b"not a fit file at all", "does not look like a valid activity file"),
        ("ride.gpx", b"<gpx", "does not look like a valid activity file"),
        ("route.gpx", b"<gpx><trk><trkseg><trkpt lat='1' lon='2'/></trkseg></trk></gpx>", "no recorded samples"),
    ],
)
def test_upload_errors_are_for_the_user(store: Store, filename: str, data: bytes, message: str) -> None:
    with pytest.raises(InputError, match=message):
        uploads.import_activity_file(store, filename, data)
    assert store.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_size_limits(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploads, "MAX_DECOMPRESSED_BYTES", 1000)
    with pytest.raises(InputError, match="larger than"):
        uploads.import_activity_file(store, "bomb.gpx.gz", gzip.compress(b" " * 100_000))
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(InputError, match="larger than"):
        uploads.import_activity_file(store, "ride.fit", FIT)


def test_gps_distance_ignores_jitter() -> None:
    standing = {"lat": [48.85, 48.850005, 48.85, 48.850005, 48.851], "lng": [2.35] * 5}  # 0.5 m wiggles, then 111 m
    assert uploads._distance_m(standing) == pytest.approx(111.2, abs=0.5)


def test_parse_bytes_reads_the_gpx_track_name_and_type() -> None:
    meta = parse_bytes("run.gpx", GPX).meta
    assert meta == {"format": "gpx", "name": "Track session", "type": "running"}


def test_elevation_gain_ignores_noise() -> None:
    gain = uploads._elevation_gain_m({"altitude": [100, 101, 100, 101.5, 100, 110, 109, 120]})
    assert gain == pytest.approx(20)  # 100 -> 120; the 1-1.5 m wiggles are noise
    assert uploads._elevation_gain_m({"altitude": [100, 120, 100, 120]}) == pytest.approx(40)  # two real climbs


# -- API ------------------------------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8801")


def post(client: TestClient, data: bytes, **params: str):
    headers = {"Content-Type": params.pop("content_type", "application/octet-stream"), **params.pop("headers", {})}
    return client.post("/api/activities/import", content=data, params=params, headers=headers)


def test_api_import(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    created = post(client, FIT, filename="ride.fit", timezone="America/Los_Angeles")
    assert created.status_code == 201 and created.json()["created"] is True
    source_id = created.json()["activity"]["source_id"]
    detail = client.get(f"/api/activities/upload/{source_id}").json()
    assert detail["analysis"]["tss_method"] == "power" and detail["streams"]["sample_count"] == 3600
    assert post(client, FIT, filename="ride.fit").json()["created"] is False
    bad = post(client, b"junk", filename="ride.fit")
    assert bad.status_code == 400 and "valid activity file" in bad.json()["detail"]
    assert post(client, FIT, filename="ride.fit", content_type="text/plain").status_code == 415
    assert post(client, FIT).status_code == 422  # no filename
    evil = post(client, FIT, filename="ride.fit", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 100)
    assert post(client, FIT, filename="ride.fit").status_code == 413
