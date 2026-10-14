from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fitness_hub import cli
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.providers import SyncResult


def run_cli(capsys: pytest.CaptureFixture, *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_console_script_help() -> None:
    script = Path(sys.executable).with_name("fitness-hub")
    command = [str(script)] if script.exists() else [sys.executable, "-m", "fitness_hub"]
    proc = subprocess.run([*command, "--help"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    for word in ("auth", "sync", "status", "import-garmin-csv", "serve-mcp", "run"):
        assert word in proc.stdout


def test_no_command_prints_help(capsys) -> None:
    code, out, _ = run_cli(capsys)
    assert code == 0 and "usage: fitness-hub" in out


def test_status_on_fresh_home(home: Path, capsys) -> None:
    code, out, _ = run_cli(capsys, "status")
    assert code == 0
    assert "strava         not authorized" in out
    assert "activities=0" in out and "No syncs recorded yet" in out


def test_status_survives_corrupt_tokens_file(home: Path, capsys) -> None:
    home.mkdir(parents=True)
    (home / "tokens.json").write_text("{not json")
    code, out, _ = run_cli(capsys, "status")
    assert code == 0
    assert "strava         error:" in out and "corrupt" in out
    assert "garmin         not authorized" in out


def test_sync_with_nothing_authorized_exits_zero(home: Path, capsys) -> None:
    code, out, _ = run_cli(capsys, "sync")
    assert code == 0
    assert out.count("skipped (not authorized") == 3
    assert "fitness-hub auth google" in out


def test_sync_exit_code_on_stream_error(home: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_run_sync(settings, sources=None, since=None, full=False, log_line=print):
        seen.update(sources=sources, since=since, full=full)
        return [SyncResult("strava", "activities", rows=3), SyncResult("garmin", "daily", error="boom")]

    monkeypatch.setattr(cli, "run_sync", fake_run_sync)
    code, _, _ = run_cli(capsys, "sync", "--source", "strava", "--source", "garmin", "--since", "2026-09-01", "--full")
    assert code == 1
    assert seen == {"sources": ["strava", "garmin"], "since": "2026-09-01", "full": True}


def test_sync_bad_since(home: Path, capsys) -> None:
    code, _, err = run_cli(capsys, "sync", "--since", "last week")
    assert code == 1 and "since" in err


def test_status_shows_errors(seeded: Settings, capsys) -> None:
    with Store(seeded.db_path) as store:
        store.record_sync("garmin", "daily", error="stopped at 2026-10-02: 429")
    code, out, _ = run_cli(capsys, "status")
    assert code == 0
    assert "activities=3" in out
    assert "strava" in out and "2026-10-08T17:00:00Z" in out
    assert "! last error" in out and "stopped at 2026-10-02" in out


def test_import_garmin_csv(home: Path, tmp_path: Path, capsys) -> None:
    path = tmp_path / "Activities.csv"
    path.write_text(
        "Activity Type,Date,Title,Distance,Calories,Time,Avg HR,Max HR,Total Ascent,Avg Power\n"
        "Cycling,2026-10-02 12:00:05,Ride,30.0,800,1:00:00,140,170,300,200\n",
        encoding="utf-8",
    )
    code, out, _ = run_cli(capsys, "import-garmin-csv", str(path), "--timezone", "Europe/Berlin")
    assert code == 0 and "Imported 1 activities" in out
    with Store(home / "fitness.db") as store:
        row = store.conn.execute("SELECT start_time, distance_m FROM activities").fetchone()
    assert tuple(row) == ("2026-10-02T10:00:05Z", 30000.0)
    code, _, err = run_cli(capsys, "import-garmin-csv", str(path), "--timezone", "Mars/Olympus")
    assert code == 1 and "timezone" in err
    code, _, err = run_cli(capsys, "import-garmin-csv", str(tmp_path / "missing.csv"))
    assert code == 1


def test_auth_without_credentials_is_actionable(home: Path, capsys) -> None:
    code, _, err = run_cli(capsys, "auth", "strava", "--no-browser")
    assert code == 1 and "STRAVA_CLIENT_ID" in err
    code, _, err = run_cli(capsys, "auth", "google", "--no-browser")
    assert code == 1 and "GOOGLE_CLIENT_ID" in err


def test_config_error_exit_code(home: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOKBACK_DAYS", "a week")
    code, _, err = run_cli(capsys, "status")
    assert code == 2 and "LOOKBACK_DAYS" in err


def test_http_refuses_non_loopback(home: Path, capsys) -> None:
    code, _, err = run_cli(capsys, "serve-mcp", "--transport", "http", "--host", "0.0.0.0")
    assert code == 1 and "no authentication" in err
    code, _, err = run_cli(capsys, "run", "--http", "--host", "192.168.1.10")
    assert code == 1 and "no authentication" in err
    assert not [t for t in threading.enumerate() if t.name == "sync"]  # no sync thread was started


def test_daemon_survives_errors(env_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    calls = []

    def flaky_run_sync(_settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("network down")
        if len(calls) == 2:
            return [SyncResult("strava", "activities", error="rate limited")]
        stop.set()
        return []

    monkeypatch.setattr(cli, "run_sync", flaky_run_sync)
    cli._sync_forever(env_settings, 0.01, stop)
    assert len(calls) == 3
