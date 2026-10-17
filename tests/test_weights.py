from __future__ import annotations

from pathlib import Path

import pytest

from fitness_hub.cli import main
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.insights import ride_weight
from fitness_hub.util import InputError
from fitness_hub.weights import SOURCE, import_weight_csv, parse_weight_csv

LOG = """Date,Daily Weight in
"Tuesday, June 26, 2018",174.6
"Wednesday, June 27, 2018",
"Thursday, June 28, 2018",171.8
"""


def days(parsed: dict) -> list[dict]:
    return [{"date": r["date"], "kg": r["kg"]} for r in parsed["rows"]]


def test_parse_long_dates_in_pounds() -> None:
    parsed = parse_weight_csv(LOG, unit="lb")
    assert days(parsed) == [{"date": "2018-06-26", "kg": 79.2}, {"date": "2018-06-28", "kg": 77.93}]
    assert parsed["skipped"] == 1 and parsed["unit"] == "lb"


def test_unit_from_the_header_or_refused() -> None:
    assert days(parse_weight_csv("date,weight (kg)\n2020-01-02,70.5\n")) == [{"date": "2020-01-02", "kg": 70.5}]
    assert days(parse_weight_csv("Day,Weight lbs\n01/02/2020,160\n"))[0] == {"date": "2020-01-02", "kg": 72.57}
    with pytest.raises(InputError, match="--unit"):
        parse_weight_csv(LOG)  # "Daily Weight in" does not say
    with pytest.raises(InputError, match="wrong unit"):
        parse_weight_csv("date,weight\n2020-01-01,700\n", unit="kg")
    with pytest.raises(InputError, match="date"):
        parse_weight_csv("date,weight kg\nyesterday,70\n")


def test_import_and_ride_weight_average(settings: Settings) -> None:
    rows = "\n".join(f"2020-03-{d:02d},{70 + d / 10}" for d in range(1, 11))
    with Store(settings.db_path) as store:
        result = import_weight_csv(store, "date,weight kg\n" + rows)
        assert result == {"imported": 10, "skipped": 0, "unit": "kg", "first": "2020-03-01", "last": "2020-03-10"}
        assert import_weight_csv(store, "date,weight kg\n" + rows)["imported"] == 10  # again: no copies
        count = store.conn.execute("SELECT count(*) FROM body_measurements WHERE source = ?", (SOURCE,)).fetchone()[0]
        assert count == 10
        store.upsert_activities([{"source": "strava", "source_id": "r", "start_time": "2020-03-10T08:00:00Z",
                                  "local_date": "2020-03-10", "activity_type": "Ride", "raw": {}}])  # fmt: skip
        weight = ride_weight(store.conn, "strava", "r", "2020-03-10")
        # The week up to the ride: Mar 4-10, 70.4 ... 71.0 kg.
        assert weight == {"kg": 70.7, "origin": "body", "date": "2020-03-10", "count": 7}
        assert ride_weight(store.conn, "strava", "r", "2020-04-09")["date"] == "2020-03-10"  # 30 days on
        assert ride_weight(store.conn, "strava", "r", "2020-04-10") is None  # too old to say
        assert ride_weight(store.conn, "strava", "r", "2020-02-28") is None  # before any


def test_cli(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "weights.csv"
    path.write_text(LOG)
    assert main(["import-weight-csv", str(path)]) == 1
    assert "--unit" in capsys.readouterr().err
    assert main(["import-weight-csv", str(path), "--unit", "lb"]) == 0
    assert "Imported 2 weigh-ins (lb) from 2018-06-26 to 2018-06-28" in capsys.readouterr().out


def test_review_edge_cases() -> None:
    with pytest.raises(InputError, match="contradicts"):
        parse_weight_csv("date,weight (kg)\n2020-01-02,70\n", unit="lb")
    picked = parse_weight_csv("Last Updated,Date,Goal Weight (kg),Weight (kg)\nx,2020-01-02,65,70\n")
    assert picked["columns"] == ("Date", "Weight (kg)") and days(picked) == [{"date": "2020-01-02", "kg": 70.0}]
    with pytest.raises(InputError, match="day/month"):
        parse_weight_csv("date,weight kg\n01/02/2020,70\n26/06/2020,70\n")
    with pytest.raises(InputError, match="future"):
        parse_weight_csv("date,weight kg\n2068-06-26,70\n")
    newest_first = parse_weight_csv("date,weight kg\n2023-03-05 07:12:30,71\n2023-03-04,70\n2023-03-05,72\n")
    assert days(newest_first) == [{"date": "2023-03-04", "kg": 70.0}, {"date": "2023-03-05", "kg": 72.0}]
    with pytest.raises(InputError, match="dot for decimals"):
        parse_weight_csv('date,weight kg\n2020-01-02,"70,5"\n')


def test_a_scale_export_in_grams_keeps_its_times(settings: Settings) -> None:
    export = (
        "timestamp,weight grams,data source\n"
        "2020-12-21T16:55:25Z,74027,Aria Air\n"
        "2020-12-21T19:00:00+01:00,74500,Google Health App\n"  # 18:00 UTC: a second weigh-in that day
        "2026-07-28T15:51:06.659Z,71758,Google Health App\n"
    )
    parsed = parse_weight_csv(export)
    assert parsed["unit"] == "grams" and parsed["columns"] == ("timestamp", "weight grams")
    assert [(r["ts"], r["kg"], r["device"]) for r in parsed["rows"]] == [
        ("2020-12-21T16:55:25Z", 74.03, "Aria Air"),
        ("2020-12-21T18:00:00Z", 74.5, "Google Health App"),
        ("2026-07-28T15:51:06Z", 71.76, "Google Health App"),
    ]
    with Store(settings.db_path) as store:
        assert import_weight_csv(store, export)["imported"] == 3
        stored = store.conn.execute("SELECT ts, value FROM body_measurements ORDER BY ts").fetchall()
        assert [tuple(r) for r in stored][:2] == [("2020-12-21T16:55:25Z", 74.03), ("2020-12-21T18:00:00Z", 74.5)]
        store.upsert_activities([{"source": "strava", "source_id": "r", "start_time": "2020-12-21T20:00:00Z",
                                  "local_date": "2020-12-21", "activity_type": "Ride", "raw": {}}])  # fmt: skip
        day_mean = ride_weight(store.conn, "strava", "r", "2020-12-21")["kg"]
        assert day_mean == pytest.approx(74.27, abs=0.01)  # two weigh-ins that day: their mean
    with pytest.raises(InputError, match="contradicts"):
        parse_weight_csv(export, unit="kg")


def test_a_weigh_in_from_several_sources_counts_once(settings: Settings) -> None:
    with Store(settings.db_path) as store:
        rows = []
        for source in ("garmin", "google_health", SOURCE):
            for day, kg in (("2020-03-01", 70.0), ("2020-03-02", 80.0)):
                rows.append({"source": source, "ts": f"{day}T07:00:00Z", "metric": "weight_kg", "value": kg})
        store.upsert_body_measurements(rows)
        store.upsert_activities([{"source": "strava", "source_id": "r", "start_time": "2020-03-02T08:00:00Z",
                                  "local_date": "2020-03-02", "activity_type": "Ride", "raw": {}}])  # fmt: skip
        assert ride_weight(store.conn, "strava", "r", "2020-03-02") == {
            "kg": 75.0, "origin": "body", "date": "2020-03-02", "count": 2
        }  # fmt: skip


def test_cli_reads_spreadsheet_encodings(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = "Date,Weight (kg) – matin\n2020-01-02,70\n"
    for name, data in (("utf16.csv", text.encode("utf-16")), ("cp1252.csv", text.encode("cp1252"))):
        path = tmp_path / name
        path.write_bytes(data)
        assert main(["import-weight-csv", str(path)]) == 0, capsys.readouterr().err
        assert "Imported 1 weigh-ins (kg)" in capsys.readouterr().out
