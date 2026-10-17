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


def test_parse_long_dates_in_pounds() -> None:
    parsed = parse_weight_csv(LOG, unit="lb")
    assert parsed["rows"] == [{"date": "2018-06-26", "kg": 79.2}, {"date": "2018-06-28", "kg": 77.93}]
    assert parsed["skipped"] == 1 and parsed["unit"] == "lb"


def test_unit_from_the_header_or_refused() -> None:
    assert parse_weight_csv("date,weight (kg)\n2020-01-02,70.5\n")["rows"] == [{"date": "2020-01-02", "kg": 70.5}]
    assert parse_weight_csv("Day,Weight lbs\n01/02/2020,160\n")["rows"][0] == {"date": "2020-01-02", "kg": 72.57}
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
