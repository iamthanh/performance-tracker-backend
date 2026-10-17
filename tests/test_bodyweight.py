from __future__ import annotations

import pytest

from fitness_hub.bodyweight import TREND_ALPHA, daily_weigh_ins, trend, weight_trend
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.util import InputError


def test_weigh_ins_are_kept_and_the_trend_smooths_them() -> None:
    days = trend([("2020-01-01", 70.0), ("2020-01-02", 72.0)])
    first, second = days[0], days[1]
    assert first == {"date": "2020-01-01", "kg": 70.0, "kind": "weigh_in", "weigh_in": 70.0, "weigh_ins": 1,
                     "gap_days": 0, "last_weigh_in": "2020-01-01"}  # fmt: skip
    assert second["weigh_in"] == 72.0 and second["kg"] == pytest.approx(70 + TREND_ALPHA * 2)  # raw kept, trend smooth


def test_gaps_are_labelled_by_length() -> None:
    days = {d["date"]: d for d in trend([("2020-01-01", 70.0), ("2020-01-11", 70.0), ("2020-04-01", 74.0)])}
    assert days["2020-01-05"]["kind"] == "interpolated" and days["2020-01-05"]["gap_days"] == 10
    assert days["2020-01-05"]["weigh_in"] is None and days["2020-01-05"]["last_weigh_in"] == "2020-01-01"
    mid = days["2020-02-20"]
    assert mid["kind"] == "estimated" and mid["gap_days"] == 81
    # After 81 days the trend has all but caught up with the weigh-in: the line ends near 74 kg.
    assert days["2020-04-01"]["kg"] == pytest.approx(74, abs=0.01)
    assert 70 < mid["kg"] < 74
    assert days["2020-05-01"]["kind"] == "carried" and days["2020-05-01"]["gap_days"] == 30
    assert "2020-05-02" not in days and "2019-12-31" not in days


def test_api_and_raw_data_untouched(settings: Settings) -> None:
    with Store(settings.db_path) as store:
        store.upsert_body_measurements(
            {"source": "weight_csv", "ts": f"2020-01-{d:02d}T12:00:00Z", "metric": "weight_kg", "value": 70.0}
            for d in (1, 20)
        )
        result = weight_trend(store.conn, "2020-01-01", "2020-01-31")
        with pytest.raises(InputError):
            weight_trend(store.conn, "2020-02-01", "2020-01-31")
        assert result["counts"] == {"weigh_in": 2, "interpolated": 18, "estimated": 0, "carried": 11}
        assert store.conn.execute("SELECT count(*) FROM body_measurements").fetchone()[0] == 2  # nothing written


def test_a_gap_of_over_a_year_is_left_empty() -> None:
    days = {d["date"] for d in trend([("2016-12-14", 78.0), ("2018-06-26", 79.0)])}
    assert "2017-06-01" not in days and {"2016-12-14", "2018-06-26"} <= days


def test_one_reading_on_two_services_counts_once_and_pre_ride_weights_count(settings: Settings) -> None:
    with Store(settings.db_path) as store:
        store.upsert_body_measurements(
            [{"source": s, "ts": "2020-01-01T15:00:00Z", "metric": "weight_kg", "value": 70.0}
             for s in ("garmin", "google_health")]
            + [{"source": "garmin", "ts": "2020-01-01T20:00:00Z", "metric": "weight_kg", "value": 73.0}]
        )  # fmt: skip
        store.upsert_activities([{"source": "strava", "source_id": "r", "start_time": "2020-01-05T16:00:00Z",
                                  "local_date": "2020-01-05", "activity_type": "Ride", "raw": {}}])  # fmt: skip
        store.conn.execute("INSERT INTO workout_weights (source, source_id, pre_kg, updated_at) VALUES "
                           "('strava', 'r', 71.5, 'x')")  # fmt: skip
        store.conn.commit()
        days = {d[0]: d for d in daily_weigh_ins(store.conn)}
        assert days["2020-01-01"][1:] == (71.5, 2)  # 70 (on two services) and 73
        assert days["2020-01-05"][1:] == (71.5, 1)  # the pre-ride weight


def test_a_weigh_in_counts_for_its_local_day(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    import time

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        with Store(settings.db_path) as store:
            store.upsert_body_measurements([{"source": "garmin", "ts": "2020-01-02T03:00:00Z", "metric": "weight_kg",
                                             "value": 70.0}])  # fmt: skip
            assert daily_weigh_ins(store.conn)[0][0] == "2020-01-01"  # 19:00 in California
    finally:
        monkeypatch.undo()
        time.tzset()
