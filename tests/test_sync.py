from __future__ import annotations

from datetime import date

import pytest

from fitness_hub.db import Store
from fitness_hub.providers import SyncResult, run_stream
from fitness_hub.sync import SyncInProgress, make_window, run_sync, sync_lock

TODAY = date(2026, 10, 14)


class FakeProvider:
    def __init__(self, name: str, configured: bool = True, crash: bool = False, fail_stream: bool = False):
        self.name = name
        self._configured = configured
        self.crash = crash
        self.fail_stream = fail_stream
        self.windows = []

    def configured(self) -> bool:
        return self._configured

    def sync(self, store: Store, window, full=False) -> list[SyncResult]:
        self.windows.append((window, full))
        if self.crash:
            raise RuntimeError("provider bug")

        def good(progress):
            progress.rows = store.upsert_activities(
                [
                    {
                        "source": self.name,
                        "source_id": "1",
                        "start_time": "2026-10-10T07:00:00Z",
                        "local_date": "2026-10-10",
                        "raw": {},
                    }
                ]
            )
            progress.cursor = window.end.isoformat()

        def bad(progress):
            raise ConnectionError("HTTP 403")

        results = [run_stream(store, self.name, "activities", good)]
        if self.fail_stream:
            results.append(run_stream(store, self.name, "daily", bad))
        return results


def test_nothing_authorized_is_skipped(settings) -> None:
    lines: list[str] = []
    results = run_sync(settings, log_line=lines.append, today=TODAY)
    assert [r.source for r in results] == ["strava", "garmin", "google_health"]
    assert all(r.skipped and r.ok for r in results)
    assert any("fitness-hub auth google" in line for line in lines)


def test_failures_are_isolated_and_reported(settings) -> None:
    providers = {
        "strava": FakeProvider("strava", crash=True),
        "garmin": FakeProvider("garmin", fail_stream=True),
        "google_health": FakeProvider("google_health"),
    }
    lines: list[str] = []
    results = run_sync(settings, providers=providers, log_line=lines.append, today=TODAY)
    errors = {(r.source, r.stream) for r in results if r.error}
    assert errors == {("strava", "sync"), ("garmin", "daily")}
    assert {(r.source, r.stream) for r in results if r.ok} == {("garmin", "activities"), ("google_health", "activities")}
    with Store(settings.db_path) as store:
        states = {(s["source"], s["stream"]): s for s in store.sync_states()}
    assert "provider bug" in states[("strava", "sync")]["last_error"]
    assert states[("google_health", "activities")]["last_error"] is None
    assert lines[-1] == "done: 2 rows upserted, 2 stream error(s)"


def test_rerun_is_idempotent(settings) -> None:
    providers = {"garmin": FakeProvider("garmin")}
    run_sync(settings, sources=["garmin"], providers=providers, today=TODAY)
    with Store(settings.db_path) as store:
        first = store.row_counts()
    run_sync(settings, sources=["garmin"], providers=providers, today=TODAY)
    with Store(settings.db_path) as store:
        assert store.row_counts() == first


def test_window_and_options(settings) -> None:
    provider = FakeProvider("strava")
    run_sync(settings, sources=["strava"], since="2026-09-01", full=True, providers={"strava": provider}, today=TODAY)
    window, full = provider.windows[0]
    assert (window.start, window.end, window.explicit_start, full) == (date(2026, 9, 1), TODAY, True, True)
    default = make_window(settings, today=TODAY)
    assert default.start == date(2026, 9, 14) and not default.explicit_start  # backfill_days=30
    with pytest.raises(ValueError, match="unknown source"):
        run_sync(settings, sources=["fitbit"], today=TODAY)
    with pytest.raises(ValueError, match="future"):
        make_window(settings, since="2027-01-01", today=TODAY)


def test_concurrent_sync_is_refused(settings) -> None:
    with sync_lock(settings):
        with pytest.raises(SyncInProgress):
            run_sync(settings, providers={"strava": FakeProvider("strava")}, sources=["strava"], today=TODAY)
    # lock released afterwards
    assert run_sync(settings, providers={"strava": FakeProvider("strava")}, sources=["strava"], today=TODAY)[0].ok


def test_corrupt_tokens_file_does_not_stop_other_providers(settings) -> None:
    from fitness_hub.auth import TokenStore
    from fitness_hub.providers.strava import StravaProvider

    settings.ensure_home()
    settings.tokens_path.write_text("{not json")
    providers = {"strava": StravaProvider(settings, TokenStore(settings.tokens_path)), "garmin": FakeProvider("garmin")}
    results = run_sync(settings, sources=["strava", "garmin"], providers=providers, today=TODAY)
    by_source = {r.source: r for r in results}
    assert "corrupt" in by_source["strava"].error
    assert by_source["garmin"].ok and by_source["garmin"].rows == 1
