from __future__ import annotations

import dataclasses
import json
import socket
import threading
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's TestClient warns about httpx; it still works
    from fastapi.testclient import TestClient

from fitness_hub.analysis import analyze
from fitness_hub.api import (
    check_bind_host,
    create_app,
    host_allowed,
    host_name,
    is_lan_bind,
    lan_urls,
    machine_host_names,
    origin_host,
    serve,
)
from fitness_hub.config import Settings
from fitness_hub.db import Store
from fitness_hub.queries import downsample


def ride(store: Store, source_id: str, day: str, watts: float, seconds: int = 3600, source: str = "strava",
         start: str = "T16:00:00Z", activity_type: str = "Ride") -> None:  # fmt: skip
    store.upsert_activities(
        [{"source": source, "source_id": source_id, "start_time": day + start, "local_date": day,
          "activity_type": activity_type, "distance_m": 30000.0, "moving_s": float(seconds), "raw": {}}]  # fmt: skip
    )
    if watts:
        streams = {"time": list(range(seconds)), "power": [watts] * seconds, "heart_rate": [150] * seconds}
        meta = {"device": "ELEMNT", "laps": [{"avg_power": watts}]}
        store.upsert_activity_streams([{"source": source, "source_id": source_id, "streams": streams, "meta": meta}])


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with Store(settings.db_path) as store:
        ride(store, "1", "2026-09-01", 250)
        ride(store, "2", "2026-09-03", 200, seconds=1800)
        ride(store, "g2", "2026-09-03", 0, source="garmin", start="T16:00:40Z")  # the same ride, no streams
        ride(store, "3", "2026-09-08", 0, activity_type="Run")  # no streams at all
        store.upsert_daily_metrics([{"source": "garmin", "date": "2026-09-02", "metric": "resting_hr", "value": 48}])
        store.upsert_daily_series(
            [{"source": "garmin", "date": "2026-09-02", "series": "heart_rate",
              "streams": {"time": [1788307200, 1788307320], "heart_rate": [52, 50]}}]  # fmt: skip
        )
        analyze(store)
        # Streams imported after the last analysis: charts exist, metrics do not yet.
        ride(store, "4", "2026-09-10", 180)
    return TestClient(create_app(settings), base_url=BASE)


BASE = "http://127.0.0.1:8801"


def test_health_and_status(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["schema_version"] == 6
    assert health["latest_activity_date"] == "2026-09-10" and health["last_analysis_at"]
    assert client.get("/api/status").json()["row_counts"]["activities"] == 5


def test_activities_prefer_the_analysed_copy(client: TestClient) -> None:
    rows = client.get("/api/activities", params={"start_date": "2026-09-01", "end_date": "2026-09-08"}).json()
    assert [(r["source"], r["source_id"]) for r in rows] == [("strava", "3"), ("strava", "2"), ("strava", "1")]
    second = rows[1]
    assert second["also_recorded_by"] == ["garmin"] and second["has_streams"] is True
    assert second["tss"] > 0 and second["tss_method"] == "power" and second["sport"] == "cycling"
    assert rows[0]["has_streams"] is False and rows[0]["tss"] is None and rows[0]["analysed"] is False
    assert second["kcal"] == pytest.approx(200 * 1.8) and rows[0]["kcal"] is None  # a ride's kJ; a run with nothing
    week = client.get("/api/summary", params={"start_date": "2026-09-01", "end_date": "2026-09-08"}).json()
    assert week["periods"][0]["total"]["kcal"] == pytest.approx(250 * 3.6 + 200 * 1.8)
    newest = client.get("/api/activities", params={"limit": 1}).json()
    assert len(newest) == 1 and newest[0]["source_id"] == "4"
    assert newest[0]["has_streams"] is True and newest[0]["analysed"] is False  # streams, not analysed yet
    assert [r["source_id"] for r in client.get("/api/activities", params={"activity_type": "running"}).json()] == ["3"]


def test_activity_detail_and_streams(client: TestClient) -> None:
    detail = client.get("/api/activities/strava/1").json()
    assert detail["device"] == "ELEMNT" and detail["laps"] == [{"avg_power": 250}]
    assert detail["analysis"]["tss_method"] == "power" and detail["streams"]["sample_count"] == 3600
    streams = client.get("/api/activities/strava/1/streams", params={"max_points": 100}).json()
    assert len(streams["time"]) == 100 and streams["power"][0] == 250
    assert client.get("/api/activities/strava/3").json()["analysis"] is None
    assert client.get("/api/activities/strava/3/streams").status_code == 404
    assert client.get("/api/activities/strava/missing").status_code == 404
    assert client.get("/api/activities/strava/1/streams", params={"max_points": 1}).status_code == 422


def test_summary_and_training_load(client: TestClient) -> None:
    summary = client.get(
        "/api/summary", params={"start_date": "2026-08-31", "end_date": "2026-09-13", "group_by": "week"}
    ).json()
    weeks = {p["period"]: p for p in summary["periods"]}
    assert list(weeks) == ["2026-08-31", "2026-09-07"]
    first = weeks["2026-08-31"]["total"]
    assert first["count"] == 2 and first["distance_km"] == 60 and first["moving_hours"] == 1.5  # duplicate counted once
    assert first["tss"] > 0 and first["work_kj"] == pytest.approx(250 * 3.6 + 200 * 1.8)
    assert weeks["2026-09-07"]["by_sport"]["running"]["count"] == 1
    load = client.get("/api/training-load", params={"start_date": "2026-09-01", "end_date": "2026-09-08"}).json()
    assert len(load) == 8 and load[0]["tss"] > 0 and load[-1]["unscored"] == 1
    assert weeks["2026-09-07"]["total"]["unscored"] == 2 and weeks["2026-08-31"]["total"]["unscored"] == 0
    assert client.get("/api/summary", params={"start_date": "2026-09-01"}).status_code == 422
    bad = client.get("/api/summary", params={"start_date": "x", "end_date": "2026-09-01"})
    assert bad.status_code == 400 and "YYYY-MM-DD" in bad.json()["detail"]
    for path, params in (
        ("/api/summary", {"start_date": "2026-01-01", "end_date": "9999-12-31"}),  # was an OverflowError
        ("/api/summary", {"start_date": "0001-01-01", "end_date": "2026-01-01", "group_by": "day"}),
        ("/api/training-load", {"end_date": "0001-01-05"}),
        ("/api/training-load", {"start_date": "2026-01-01", "end_date": "9999-01-01"}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 400, (path, params, response.text)


def test_power_curve(client: TestClient) -> None:
    curve = {r["duration_s"]: r for r in client.get("/api/power-curve").json()}
    assert curve[60]["watts"] == 250 and curve[60]["source_id"] == "1"
    recent = {r["duration_s"]: r for r in client.get("/api/power-curve", params={"start_date": "2026-09-02"}).json()}
    assert recent[60]["watts"] == 200 and 3600 not in recent
    assert client.get("/api/power-curve", params={"activity_type": "all"}).status_code == 200


def test_health_data(client: TestClient) -> None:
    metrics = client.get("/api/daily-metrics", params={"start_date": "2026-09-01", "end_date": "2026-09-30"}).json()
    assert metrics == [{"date": "2026-09-02", "metric": "resting_hr", "source": "garmin", "value": 48.0}]
    hr = client.get("/api/heart-rate/2026-09-02").json()
    assert hr["sources"]["garmin"]["heart_rate"] == [52, 50]
    assert client.get("/api/heart-rate/2026-09-03").json()["sources"] == {}
    assert client.get("/api/sleep", params={"start_date": "2026-09-01", "end_date": "2026-09-30"}).json() == []
    assert client.get("/api/body", params={"start_date": "2026-09-01", "end_date": "2026-09-30"}).json() == []


def test_thresholds_read_set_delete(client: TestClient) -> None:
    current = client.get("/api/thresholds", params={"on": "2026-09-05"}).json()
    # Auto FTP: an hour at 250 W (ride 1) proves 250 W.
    assert current["in_effect"]["ftp_w"] == 250 and any(v["origin"] == "auto" for v in current["values"])
    created = client.post(
        "/api/thresholds", json={"metric": "ftp_w", "effective_date": "2026-09-02", "value": 210, "note": "test"}
    )
    # Ride 2 is re-scored with the new FTP, and ride 4 (pending) is analysed and scored with it too.
    assert created.status_code == 201 and created.json()["analysis"]["rescored"] == 2
    assert client.get("/api/thresholds", params={"on": "2026-09-05"}).json()["in_effect"]["ftp_w"] == 210
    detail = client.get("/api/activities/strava/2").json()
    assert detail["analysis"]["ftp_w"] == 210
    assert client.get("/api/activities/strava/4").json()["analysis"]["ftp_w"] == 210
    assert client.delete("/api/thresholds/ftp_w/2026-09-02").json()["analysis"]["rescored"] == 2
    assert client.delete("/api/thresholds/ftp_w/2026-09-02").status_code == 404
    for invalid in ({"metric": "ftp_w", "value": 0}, {"metric": "vo2", "value": 5}):
        assert client.post("/api/thresholds", json={**invalid, "effective_date": "2026-09-02"}).status_code == 422
    bad_date = client.post("/api/thresholds", json={"metric": "ftp_w", "effective_date": "soon", "value": 200})
    assert bad_date.status_code == 400


def test_non_loopback_host_is_refused(client: TestClient) -> None:
    """A page that rebinds its own name to 127.0.0.1 still sends its name as Host."""
    assert client.get("/api/health", headers={"Host": "attacker.example:8801"}).status_code == 421
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 421
    for host in ("localhost:8801", "127.0.0.1", "[::1]:8801"):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200
    assert [host_name(h) for h in ("LOCALHOST:1", "[::1]:8", "::1", "127.0.0.1")] == ["localhost", "::1", "::1",
                                                                                     "127.0.0.1"]  # fmt: skip


def test_writes_from_other_origins_are_refused(client: TestClient) -> None:
    body = {"metric": "ftp_w", "effective_date": "2026-09-02", "value": 123}
    blocked = client.post("/api/thresholds", json=body, headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403
    evil = {"Origin": "https://evil.example"}
    assert client.delete("/api/thresholds/ftp_w/2026-09-02", headers=evil).status_code == 403
    # A cross-site "simple" request cannot set Content-Type: application/json, and is rejected without it.
    plain = client.post("/api/thresholds", content=json.dumps(body), headers={"Content-Type": "text/plain"})
    assert plain.status_code == 422
    assert client.get("/api/thresholds", params={"on": "2026-09-05"}).json()["in_effect"]["ftp_w"] == 250
    for origin in ("http://localhost:5173", "http://127.0.0.1:8801"):  # the UI, and the API's own /docs page
        ok = client.post("/api/thresholds", json=body, headers={"Origin": origin})
        assert ok.status_code == 201
    assert client.get("/api/thresholds", params={"on": "2026-09-05"}).json()["in_effect"]["ftp_w"] == 123


def test_cors_allows_only_the_configured_origins(settings: Settings) -> None:
    app = create_app(dataclasses.replace(settings, api_cors_origins=("http://localhost:5173/",)))
    client = TestClient(app, base_url=BASE)
    ok = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
    preflight = client.options(
        "/api/thresholds",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type"},
    )  # fmt: skip
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_parallel_requests_on_a_real_server(client: TestClient, settings: Settings) -> None:
    """Connections are opened and closed on different worker threads under load."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        paths = ["/api/health", "/api/activities", "/api/status", "/api/training-load", "/api/power-curve",
                 "/api/thresholds"] * 20  # fmt: skip

        def fetch(path: str) -> int:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:  # noqa: S310
                return response.status

        with ThreadPoolExecutor(max_workers=24) as pool:
            assert set(pool.map(fetch, paths)) == {200}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_api_cors_origins_setting(tmp_path) -> None:
    base = {"FITNESS_HUB_HOME": str(tmp_path)}
    assert Settings.from_env(base, cwd=tmp_path).api_cors_origins == ("http://localhost:5173", "http://127.0.0.1:5173")
    custom = Settings.from_env({**base, "API_CORS_ORIGINS": "http://a:1, http://b:2"}, cwd=tmp_path)
    assert custom.api_cors_origins == ("http://a:1", "http://b:2")


def test_serve_refuses_public_bind_addresses(settings: Settings) -> None:
    for host in ("8.8.8.8", "2001:4860:4860::8888", "example.com", "198.18.0.1", "240.0.0.1", "0.0.0.1",
                 "2001:0:1234::1", "fe80::1"):  # benchmarking, reserved, "this network", Teredo, link-local
        with pytest.raises(ValueError):
            serve(settings, host=host)
    with pytest.raises(ValueError, match="public"):
        serve(dataclasses.replace(settings, api_host="8.8.8.8"))  # API_HOST, not only --host
    for host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", "192.168.1.124", "10.0.0.2", "fd00::2"):
        check_bind_host(host)


def test_lan_mode_is_any_bind_beyond_loopback() -> None:
    assert [is_lan_bind(h) for h in ("127.0.0.1", "127.0.0.2", "localhost", "[::1]", "0.0.0.0", "::", "192.168.1.5")] \
        == [False, False, False, False, True, True, True]  # fmt: skip


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(socket, "gethostname", lambda: "Box.home.arpa")
    return "box"


def lan_client(settings: Settings, peer: str = "192.168.1.50", **changes: object) -> TestClient:
    app = create_app(dataclasses.replace(settings, api_host="0.0.0.0", **changes))  # type: ignore[arg-type]
    return TestClient(app, base_url=BASE, client=(peer, 50000))


def test_lan_mode_serves_local_network_hosts(settings: Settings, machine: str) -> None:
    lan = lan_client(settings, api_allowed_hosts=("nas.home", "pc.lan:8801", "[fd00::9]"))
    for host in ("192.168.1.124:8801", "10.1.2.3", "172.16.0.9:5173", "[fd9b::1]:8801", "[fe80::1%25eth0]",
                 "[::ffff:192.168.0.2]:8801", "localhost:5173", "LOCALHOST.", "box:5173", "Box.local", "box.local.",
                 "nas.home", "pc.lan", "[fd00::9]:1"):  # fmt: skip
        assert lan.get("/api/health", headers={"Host": host}).status_code == 200, host
    # DNS rebinding still sends the attacker's name; public and odd addresses are not the local network.
    for host in ("attacker.example", "8.8.8.8:8801", "0.0.0.0:8801", "0.0.0.1", "198.18.0.1", "100.64.0.1",
                 "evil.local", "box.home.arpa", ""):  # fmt: skip
        refused = lan.get("/api/health", headers={"Host": host})
        assert refused.status_code == 421 and "API_ALLOWED_HOSTS" in refused.json()["detail"], host


def test_lan_mode_writes(settings: Settings, machine: str) -> None:
    lan = lan_client(settings)
    body = {"metric": "ftp_w", "effective_date": "2026-09-02", "value": 123}
    for origin in ("http://192.168.1.124:5173", "http://box.local:5173", "http://[fd00::1]:5173"):
        assert lan.post("/api/thresholds", json=body, headers={"Origin": origin}).status_code == 201, origin
    for origin in ("https://evil.example", "null", "http://8.8.8.8", "http://evil.local"):
        assert lan.post("/api/thresholds", json=body, headers={"Origin": origin}).status_code == 403, origin


def test_lan_mode_refuses_clients_outside_the_local_network(settings: Settings) -> None:
    """A Host header can say anything; the peer address cannot."""
    for peer in ("8.8.8.8", "2001:4860::1", "100.64.0.1", "testclient"):
        response = lan_client(settings, peer=peer).get("/api/health", headers={"Host": "192.168.1.124:8801"})
        assert response.status_code == 403, peer
    for peer in ("127.0.0.1", "::1", "192.168.1.50", "fd00::5", "::ffff:10.0.0.7"):
        assert lan_client(settings, peer=peer).get("/api/health").status_code == 200, peer


def test_loopback_mode_refuses_lan_names(client: TestClient, settings: Settings, machine: str) -> None:
    refused = client.get("/api/health", headers={"Host": "192.168.1.124:8801"})
    assert refused.status_code == 421 and "--host 0.0.0.0" in refused.json()["detail"]
    allowed_names = TestClient(create_app(dataclasses.replace(settings, api_allowed_hosts=("nas.home",))), base_url=BASE)
    for host in ("nas.home", "box.local"):
        assert allowed_names.get("/api/health", headers={"Host": host}).status_code == 421, host
    body = {"metric": "ftp_w", "effective_date": "2026-09-02", "value": 123}
    assert client.post("/api/thresholds", json=body, headers={"Origin": "http://192.168.1.124:5173"}).status_code == 403


def test_host_helpers(machine: str) -> None:
    assert host_allowed("localhost", lan=False) and not host_allowed("192.168.0.2", lan=False)
    assert host_allowed("::ffff:192.168.0.2", lan=True) and not host_allowed("::ffff:8.8.8.8", lan=True)
    assert machine_host_names() == {"box", "box.local"}
    assert [origin_host(o) for o in ("http://[::1]:5173", "HTTP://Box.Local:1/", "null", "http://a.b:80@10.0.0.1")] \
        == ["::1", "box.local", "", "10.0.0.1"]  # fmt: skip
    assert lan_urls("192.168.1.9", 8801) == ["http://192.168.1.9:8801"]
    assert lan_urls("fd00::2", 8801) == ["http://[fd00::2]:8801"]
    assert lan_urls("0.0.0.0", 8801)[-1] == "http://box.local:8801"


def test_api_host_settings(tmp_path) -> None:
    base = {"FITNESS_HUB_HOME": str(tmp_path)}
    default = Settings.from_env(base, cwd=tmp_path)
    assert (default.api_host, default.api_allowed_hosts) == ("127.0.0.1", ())
    lan = Settings.from_env({**base, "API_HOST": "0.0.0.0", "API_ALLOWED_HOSTS": "NAS.home, pc.lan."}, cwd=tmp_path)
    assert (lan.api_host, lan.api_allowed_hosts) == ("0.0.0.0", ("nas.home", "pc.lan"))


def test_downsample() -> None:
    streams = {"time": [0, 1, 2, 3], "power": [100, None, 300, 300], "lat": [1.0, 1.0, 2.0, 2.0]}
    assert downsample(streams, 2) == {"time": [0.5, 2.5], "power": [100.0, 300.0], "lat": [1.0, 2.0]}
    assert downsample(streams, 10) == streams
    uneven = downsample({"time": list(range(10))}, 3)["time"]  # buckets of 3, 3 and 4 samples
    assert uneven == [1.0, 4.0, 7.5]
