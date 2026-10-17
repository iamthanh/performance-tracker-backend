from __future__ import annotations

import pytest

from fitness_hub.bodyweight import TREND_ALPHA, trend, weight_trend
from fitness_hub.config import Settings
from fitness_hub.db import Store


def test_weigh_ins_are_kept_and_the_trend_smooths_them() -> None:
    days = trend([("2020-01-01", 70.0), ("2020-01-02", 72.0)])
    first, second = days[0], days[1]
    assert first == {"date": "2020-01-01", "kg": 70.0, "kind": "weigh_in", "weigh_in": 70.0, "gap_days": 0,
                     "last_weigh_in": "2020-01-01"}  # fmt: skip
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
        assert result["counts"] == {"weigh_in": 2, "interpolated": 18, "estimated": 0, "carried": 11}
        assert store.conn.execute("SELECT count(*) FROM body_measurements").fetchone()[0] == 2  # nothing written
