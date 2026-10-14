"""HTTP read API for the web UI (performance-tracker-ui).

Local by default: it binds to loopback and has no authentication. Started on
a non-loopback address (``serve-api --host 0.0.0.0`` or ``API_HOST``) it also
serves the local network ("LAN mode"), still without authentication.
Protections against other web pages in the browser:

- Requests whose ``Host`` is not an allowed name are refused (421), so a web
  page cannot reach the API through DNS rebinding. Allowed: loopback names; in
  LAN mode also private/link-local IP addresses, this machine's host name
  (bare and ``.local``) and ``API_ALLOWED_HOSTS``.
- Writes (POST/DELETE) carrying an ``Origin`` header must come from an origin
  whose host is allowed as above, or one in ``API_CORS_ORIGINS`` (403); CORS
  lets only ``API_CORS_ORIGINS`` read responses cross-origin; and JSON bodies
  need ``Content-Type: application/json``, which a cross-site form or simple
  request cannot send.

Every read opens its own read-only connection (``mode=ro`` + ``query_only``);
the only writes are threshold overrides, which re-score the affected activities.

Dates are ``YYYY-MM-DD`` on the athlete's local clock; units are those of the
database (metres, seconds, watts, bpm) unless a field name says otherwise.
"""

from __future__ import annotations

import ipaddress
import socket
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Collection, Iterator
from contextlib import closing
from dataclasses import asdict
from typing import Annotated, Any, Literal, get_args

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__, queries
from .analysis import analyze, thresholds_on
from .config import Settings
from .db import ATHLETE_SETTINGS, Store, open_readonly, schema_version
from .mcp_server import LOOPBACK_HOSTS, is_loopback, normalize_host
from .util import InputError, parse_date

DateQuery = Annotated[str | None, Query(description="YYYY-MM-DD (local date), inclusive")]


ThresholdMetric = Literal["ftp_w", "threshold_hr", "max_hr", "resting_hr"]
if set(get_args(ThresholdMetric)) != set(ATHLETE_SETTINGS):  # pragma: no cover - a programming error
    raise RuntimeError("ThresholdMetric is out of sync with db.ATHLETE_SETTINGS")


class ThresholdIn(BaseModel):
    metric: ThresholdMetric
    effective_date: str = Field(description="YYYY-MM-DD: the value applies from this date on")
    value: float = Field(gt=0, description="W for ftp_w, bpm otherwise")
    note: str | None = None


def _read(request: Request) -> Iterator[sqlite3.Connection]:
    # FastAPI may run this dependency's setup, the endpoint and the cleanup on different
    # worker threads; one request still uses the connection one step at a time.
    with closing(open_readonly(request.app.state.settings.db_path, check_same_thread=False)) as conn:
        yield conn


def host_name(value: str) -> str:
    """``"localhost:8801"`` -> ``"localhost"``, ``"[::1]:8801"`` -> ``"::1"``."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def origin_host(origin: str) -> str:
    return host_name(origin.split("://", 1)[-1].split("/", 1)[0])


def _ip(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(name.split("%", 1)[0])  # drop an IPv6 zone ("fe80::1%eth0")
    except ValueError:
        return None


def _local_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7) or link-local."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or (ip.is_private and not ip.is_unspecified)


def machine_host_names() -> set[str]:
    """Names other devices may use for this machine: its host name, bare and as mDNS ``.local``."""
    names = {n.lower().rstrip(".") for n in (socket.gethostname(), socket.getfqdn()) if n}
    names |= {f"{n.split('.', 1)[0]}.local" for n in names}
    return names - {"localhost", "localhost.local"}


def host_allowed(name: str, lan: bool, extra: Collection[str] = ()) -> bool:
    """Whether ``name`` (from Host or Origin) may be served. A DNS-rebinding page sends its own domain."""
    if name in LOOPBACK_HOSTS:
        return True
    if not lan:
        return False
    ip = _ip(name)
    if ip is not None and _local_ip(ip):
        return True
    return name in extra or (ip is None and name.rstrip(".") in machine_host_names())


def check_bind_host(host: str) -> None:
    """Allow loopback, all interfaces (0.0.0.0 / ::) or one private address, never a public one."""
    host = normalize_host(host)
    if host == "localhost":
        return
    ip = _ip(host)
    if ip is None:
        raise ValueError(f"bind address {host!r} must be an IP address, e.g. 127.0.0.1 or 0.0.0.0")
    if not (ip.is_unspecified or _local_ip(ip)):
        raise ValueError(
            f"refusing to bind to the public address {host}: the API has no authentication "
            "and would expose your health data. Use 127.0.0.1, a private address or 0.0.0.0 behind a router."
        )


def lan_urls(port: int) -> list[str]:
    """Best-effort addresses other devices can use (no packets are sent)."""
    urls = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: only picks the outgoing interface
            urls.append(f"http://{s.getsockname()[0]}:{port}")
        except OSError:
            pass
    urls.append(f"http://{socket.gethostname().split('.', 1)[0].lower()}.local:{port}")
    return urls


# Module level: with postponed annotations FastAPI resolves these names from the module's globals.
Conn = Annotated[sqlite3.Connection, Depends(_read)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="fitness-hub",
        version=__version__,
        description="Read API over the local fitness-hub database, for the performance-tracker UI.",
    )
    app.state.settings = settings
    allowed_origins = {o.rstrip("/") for o in settings.api_cors_origins}
    lan = not is_loopback(settings.api_host)
    extra_hosts = frozenset(settings.api_allowed_hosts)

    @app.middleware("http")
    async def allowed_hosts_only(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = host_name(request.headers.get("host", ""))
        if not host_allowed(host, lan, extra_hosts):
            detail = (
                f"Host {host!r} is not served: add it to API_ALLOWED_HOSTS"
                if lan
                else "only loopback Host names are served; start serve-api with --host 0.0.0.0 for the local network"
            )
            return JSONResponse(status_code=421, content={"detail": detail})
        origin = request.headers.get("origin")
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and origin
            and origin.rstrip("/") not in allowed_origins
            and not host_allowed(origin_host(origin), lan, extra_hosts)
        ):
            return JSONResponse(status_code=403, content={"detail": f"writes are not allowed from {origin}"})
        return await call_next(request)

    # Added last so it runs first: preflight requests are answered before the checks above.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(InputError)
    async def bad_request(request: Request, exc: InputError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RuntimeError)
    async def unavailable(request: Request, exc: RuntimeError) -> JSONResponse:
        # e.g. a database written by a newer fitness-hub
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/api/health")
    def health(conn: Conn) -> dict[str, Any]:
        """Liveness plus what the UI needs to know about the data's freshness."""
        latest = conn.execute("SELECT MAX(local_date) FROM activities").fetchone()[0]
        analysed = conn.execute("SELECT MAX(updated_at) FROM activity_metrics").fetchone()[0]
        return {
            "status": "ok",
            "version": __version__,
            "schema_version": schema_version(conn),
            "latest_activity_date": latest,
            "last_analysis_at": analysed,
        }

    @app.get("/api/status")
    def status(conn: Conn) -> dict[str, Any]:
        """Row counts and per-source sync state (last success, last error)."""
        return queries.sync_status(conn)

    @app.get("/api/activities")
    def activities(
        conn: Conn,
        start_date: DateQuery = None,
        end_date: DateQuery = None,
        activity_type: Annotated[str | None, Query(description="Sport family or provider type")] = None,
        limit: Annotated[int, Query(ge=1, le=20000)] = 500,
    ) -> list[dict[str, Any]]:
        """Workouts newest first, one per workout, with TSS, IF, NP and work."""
        return queries.activities_with_metrics(conn, start_date, end_date, activity_type, limit)

    @app.get("/api/activities/{source}/{source_id}")
    def activity(conn: Conn, source: str, source_id: str) -> dict[str, Any]:
        """One workout: summary, analysis (zones, power curve, thresholds used), laps and device."""
        detail = queries.activity_detail(conn, source, source_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="activity not found")
        return detail

    @app.get("/api/activities/{source}/{source_id}/streams")
    def activity_streams(
        conn: Conn,
        source: str,
        source_id: str,
        max_points: Annotated[int, Query(ge=10, le=20000)] = 2000,
    ) -> dict[str, list[Any]]:
        """Per-sample channels (time, power, heart_rate, cadence, speed, distance, altitude,
        lat, lng, temperature; only those recorded), averaged down to ``max_points``."""
        streams = queries.activity_streams(conn, source, source_id, max_points)
        if streams is None:
            raise HTTPException(status_code=404, detail="no streams for this activity")
        return streams

    @app.get("/api/summary")
    def summary(
        conn: Conn,
        start_date: Annotated[str, Query(description="YYYY-MM-DD, inclusive")],
        end_date: Annotated[str, Query(description="YYYY-MM-DD, inclusive")],
        group_by: Literal["day", "week", "month"] = "week",
    ) -> dict[str, Any]:
        """Totals per period (weeks start Monday), overall and per sport, including TSS and work."""
        return queries.period_totals(conn, start_date, end_date, group_by)

    @app.get("/api/training-load")
    def training_load(conn: Conn, start_date: DateQuery = None, end_date: DateQuery = None) -> list[dict[str, Any]]:
        """Daily TSS, fitness (CTL), fatigue (ATL), form (TSB) and unscored workouts; default 90 days."""
        return queries.training_load(conn, start_date, end_date)

    @app.get("/api/power-curve")
    def power_curve(
        conn: Conn,
        start_date: DateQuery = None,
        end_date: DateQuery = None,
        activity_type: Annotated[str | None, Query(description="Sport; default cycling, 'all' for every sport")] = None,
    ) -> list[dict[str, Any]]:
        """Best average power per duration in the range, with the activity that set it."""
        sport = None if activity_type == "all" else (activity_type or "cycling")
        return queries.best_power_curve(conn, start_date, end_date, sport)

    @app.get("/api/daily-metrics")
    def daily_metrics(
        conn: Conn,
        start_date: DateQuery = None,
        end_date: DateQuery = None,
        metric: Annotated[list[str] | None, Query(description="Repeatable; default all")] = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Daily health metrics ``{date, metric, source, value}``; default last 30 days."""
        return queries.daily_metrics(conn, start_date, end_date, metric, source)

    @app.get("/api/sleep")
    def sleep(
        conn: Conn, start_date: DateQuery = None, end_date: DateQuery = None, main_only: bool = True
    ) -> list[dict[str, Any]]:
        """Sleep sessions by the date they ended; default last 30 days."""
        return queries.sleep_sessions(conn, start_date, end_date, main_only)

    @app.get("/api/body")
    def body(conn: Conn, start_date: DateQuery = None, end_date: DateQuery = None) -> list[dict[str, Any]]:
        """Weight (kg) and body fat (%) samples; default last 90 days."""
        return queries.body_measurements(conn, start_date, end_date)

    @app.get("/api/heart-rate/{day}")
    def heart_rate(conn: Conn, day: str) -> dict[str, Any]:
        """All-day heart rate of one date, per source (``time`` in epoch seconds)."""
        return queries.heart_rate_day(conn, day)

    @app.get("/api/thresholds")
    def thresholds(conn: Conn, on: DateQuery = None) -> dict[str, Any]:
        """Every dated FTP / threshold HR / max HR / resting HR value, and those in effect on ``on``."""
        day = parse_date(on, "on").isoformat() if on else None
        in_effect = asdict(thresholds_on(conn, day)) if day else None
        return {"values": queries.thresholds(conn), "on": day, "in_effect": in_effect}

    @app.post("/api/thresholds", status_code=201)
    def set_threshold(body: ThresholdIn) -> dict[str, Any]:
        """Set a manual value from a date on and re-score the affected activities."""
        with Store(settings.db_path) as store:
            store.set_athlete_setting(body.metric, body.effective_date, body.value, note=body.note)
            return {"analysis": analyze(store)}

    @app.delete("/api/thresholds/{metric}/{effective_date}")
    def delete_threshold(metric: ThresholdMetric, effective_date: str) -> dict[str, Any]:
        """Remove a manual value and re-score."""
        with Store(settings.db_path) as store:
            if not store.delete_athlete_setting(metric, effective_date):
                raise HTTPException(status_code=404, detail=f"no manual {metric} value from {effective_date}")
            return {"analysis": analyze(store)}

    return app


def serve(settings: Settings, host: str | None = None, port: int = 8801) -> None:
    import dataclasses

    import uvicorn

    host = normalize_host(host or settings.api_host)
    check_bind_host(host)
    settings = dataclasses.replace(settings, api_host=host)
    print(f"fitness-hub API on http://{host}:{port}/api (docs: /docs). Ctrl-C to stop.", file=sys.stderr)
    if not is_loopback(host):
        print(
            "LAN mode, no authentication: anyone on your network can read your data and change thresholds. "
            f"Try {' or '.join(lan_urls(port))}",
            file=sys.stderr,
        )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
