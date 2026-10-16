"""HTTP read API for the web UI (performance-tracker-ui).

Local by default: it binds to loopback and has no authentication. Started on
a non-loopback address (``serve-api --host 0.0.0.0`` or ``API_HOST``) it also
serves the local network ("LAN mode"), still without authentication, and
answers only clients whose address is loopback, private or link-local (403).
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
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__, nutrition, queries
from .analysis import analyze, thresholds_on
from .config import Settings
from .db import ATHLETE_SETTINGS, Store, open_readonly, schema_version
from .mcp_server import LOOPBACK_HOSTS, normalize_host
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


Meal = Literal["breakfast", "lunch", "dinner", "snack", "pre_workout", "during_workout", "post_workout"]
if set(get_args(Meal)) != set(nutrition.MEALS):  # pragma: no cover - a programming error
    raise RuntimeError("Meal is out of sync with nutrition.MEALS")
Amount = Annotated[float | None, Field(ge=0)]


class Nutrients(BaseModel):
    """Amounts; None = unknown. Per serving for a food, totals for an entry."""

    kcal: Amount = None
    carbs_g: Amount = None
    sugars_g: Amount = None
    protein_g: Amount = None
    fat_g: Amount = None
    fiber_g: Amount = None
    sodium_mg: Amount = None
    potassium_mg: Amount = None
    caffeine_mg: Amount = None
    fluid_ml: Amount = None


class FoodIn(Nutrients):
    name: str = Field(min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=200)
    serving: str | None = Field(default=None, max_length=200, description='e.g. "1 gel (40 g)", "750 ml bottle"')
    note: str | None = Field(default=None, max_length=1000)
    archived: bool = False


class EntryIn(Nutrients):
    local_date: str = Field(description="YYYY-MM-DD, the athlete's local date")
    meal: Meal
    name: str | None = Field(default=None, max_length=200, description="Defaults to the food's name")
    food_id: int | None = Field(default=None, description="Nutrients not given are the food's per serving x servings")
    servings: float = Field(default=1.0, gt=0)
    consumed_at: str | None = Field(default=None, description="ISO date-time with a time zone")
    activity_source: str | None = None
    activity_source_id: str | None = None
    offset_s: float | None = Field(default=None, ge=0, description="Seconds into the workout")
    note: str | None = Field(default=None, max_length=1000)


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
    """``"http://192.168.1.5:5173"`` -> ``"192.168.1.5"``; ``""`` for an opaque origin (``"null"``)."""
    try:
        return (urlsplit(origin.strip()).hostname or "").rstrip(".")
    except ValueError:
        return ""


IP = ipaddress.IPv4Address | ipaddress.IPv6Address

# The local network, listed explicitly: ipaddress's is_private also covers documentation,
# benchmarking, reserved and Teredo ranges, some of which are routable.
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
              "::1/128", "fc00::/7", "fe80::/10")
)  # fmt: skip


def _ip(name: str) -> IP | None:
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])  # drop an IPv6 zone ("fe80::1%eth0")
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip


def _local_ip(ip: IP) -> bool:
    """Loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7) or link-local."""
    return any(ip in net for net in LOCAL_NETWORKS)


def machine_host_names() -> frozenset[str]:
    """Names other devices may use for this machine: its host name, bare and as mDNS ``.local``."""
    name = socket.gethostname().lower().split(".", 1)[0]
    return frozenset() if name in ("", "localhost") else frozenset({name, f"{name}.local"})


def host_allowed(name: str, lan: bool, names: Collection[str] = ()) -> bool:
    """Whether ``name`` (from Host or Origin) may be served. A DNS-rebinding page sends its own domain.

    ``names``: in LAN mode, the host names allowed besides loopback and local-network IP addresses.
    """
    name = name.rstrip(".")
    if name in LOOPBACK_HOSTS:
        return True
    if not lan:
        return False
    ip = _ip(name)
    return (ip is not None and _local_ip(ip)) or name in names


def is_lan_bind(host: str) -> bool:
    """True when ``host`` listens beyond loopback (0.0.0.0, ::, a private address)."""
    host = normalize_host(host)
    ip = _ip(host)
    return host != "localhost" and not (ip is not None and ip.is_loopback)


def check_bind_host(host: str) -> None:
    """Allow loopback, all interfaces (0.0.0.0 / ::) or one private address, never a public one."""
    host = normalize_host(host)
    if host == "localhost":
        return
    ip = _ip(host)
    if ip is None:
        raise ValueError(f"bind address {host!r} must be an IP address, e.g. 127.0.0.1 or 0.0.0.0")
    if ip.is_link_local:
        raise ValueError(f"cannot bind to the link-local address {host}; use 0.0.0.0")
    if not (ip.is_unspecified or _local_ip(ip)):
        raise ValueError(
            f"refusing to bind to the public address {host}: the API has no authentication "
            "and would expose your health data. Use 127.0.0.1, a private address or 0.0.0.0."
        )


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def lan_urls(host: str, port: int) -> list[str]:
    """Best-effort addresses other devices can use (no packets are sent)."""
    ip = _ip(normalize_host(host))
    if ip is not None and not ip.is_unspecified:
        return [_url(str(ip), port)]
    urls = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: only picks the outgoing interface
            urls.append(_url(s.getsockname()[0], port))
        except OSError:
            pass
    urls += [_url(n, port) for n in sorted(machine_host_names()) if n.endswith(".local")]
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
    lan = is_lan_bind(settings.api_host)
    # Computed once: host names are looked up on every request.
    lan_names = machine_host_names() | {host_name(h).rstrip(".") for h in settings.api_allowed_hosts}

    @app.middleware("http")
    async def allowed_hosts_only(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if lan:
            # The Host header is the client's word; the peer address is not. Through the UI's
            # proxy the peer is 127.0.0.1, so the UI server must keep outsiders out itself.
            peer = _ip(request.client.host) if request.client else None
            if peer is None or not _local_ip(peer):
                return JSONResponse(status_code=403, content={"detail": "only the local network is served"})
        host = host_name(request.headers.get("host", ""))
        if not host_allowed(host, lan, lan_names):
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
            and not host_allowed(origin_host(origin), lan, lan_names)
        ):
            return JSONResponse(status_code=403, content={"detail": f"writes are not allowed from {origin}"})
        return await call_next(request)

    # Added last so it runs first: preflight requests are answered before the checks above.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
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

    # -- nutrition ------------------------------------------------------------------------

    @app.get("/api/nutrition/foods")
    def foods(conn: Conn, include_archived: bool = False) -> list[dict[str, Any]]:
        """Saved foods (nutrients per serving), by name."""
        return nutrition.list_foods(conn, include_archived)

    @app.post("/api/nutrition/foods", status_code=201)
    def create_food(body: FoodIn) -> dict[str, Any]:
        with Store(settings.db_path) as store:
            return nutrition.save_food(store.conn, body.model_dump()) or {}

    @app.put("/api/nutrition/foods/{food_id}")
    def update_food(food_id: int, body: FoodIn) -> dict[str, Any]:
        """Replace a saved food. Entries already logged from it keep their own nutrients."""
        with Store(settings.db_path) as store:
            food = nutrition.save_food(store.conn, body.model_dump(), food_id)
        if food is None:
            raise HTTPException(status_code=404, detail=f"no saved food {food_id}")
        return food

    @app.delete("/api/nutrition/foods/{food_id}", status_code=204)
    def delete_food(food_id: int) -> Response:
        with Store(settings.db_path) as store:
            if not nutrition.delete_food(store.conn, food_id):
                raise HTTPException(status_code=404, detail=f"no saved food {food_id}")
        return Response(status_code=204)

    @app.get("/api/nutrition/entries")
    def entries(
        conn: Conn,
        start_date: DateQuery = None,
        end_date: DateQuery = None,
        activity_source: str | None = None,
        activity_source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """What was eaten and drunk, in order; filter by dates and/or one workout."""
        if not (start_date or end_date or activity_source):
            raise InputError("give start_date/end_date or a workout (activity_source, activity_source_id)")
        if (activity_source is None) != (activity_source_id is None):
            raise InputError("activity_source and activity_source_id go together")
        workout = (activity_source, activity_source_id) if activity_source and activity_source_id else None
        return nutrition.list_entries(conn, start_date, end_date, workout)

    @app.post("/api/nutrition/entries", status_code=201)
    def create_entry(body: EntryIn) -> dict[str, Any]:
        with Store(settings.db_path) as store:
            return nutrition.save_entry(store.conn, body.model_dump(exclude_unset=True)) or {}

    @app.put("/api/nutrition/entries/{entry_id}")
    def update_entry(entry_id: int, body: EntryIn) -> dict[str, Any]:
        """Replace an entry (send its nutrients to keep them; missing ones are refilled from its food)."""
        with Store(settings.db_path) as store:
            entry = nutrition.save_entry(store.conn, body.model_dump(exclude_unset=True), entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no nutrition entry {entry_id}")
        return entry

    @app.delete("/api/nutrition/entries/{entry_id}", status_code=204)
    def delete_entry(entry_id: int) -> Response:
        with Store(settings.db_path) as store:
            if not nutrition.delete_entry(store.conn, entry_id):
                raise HTTPException(status_code=404, detail=f"no nutrition entry {entry_id}")
        return Response(status_code=204)

    @app.get("/api/nutrition/daily")
    def nutrition_daily(conn: Conn, start_date: DateQuery = None, end_date: DateQuery = None) -> list[dict[str, Any]]:
        """Per day: intake totals, workout kcal, the watch's total and active kcal, and the balance."""
        return nutrition.daily_nutrition(conn, start_date or "", end_date or "")

    @app.get("/api/activities/{source}/{source_id}/fueling")
    def fueling(conn: Conn, source: str, source_id: str) -> dict[str, Any]:
        """What a workout was fuelled with: totals and per-hour rates, against the energy and
        (roughly estimated) carbohydrate it burned."""
        result = nutrition.workout_fueling(conn, source, source_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"no activity {source}/{source_id}")
        return result

    return app


def serve(settings: Settings, host: str | None = None, port: int = 8801) -> None:
    import dataclasses

    import uvicorn

    host = normalize_host(host or settings.api_host)
    check_bind_host(host)
    settings = dataclasses.replace(settings, api_host=host)
    print(f"fitness-hub API on {_url(host, port)}/api (docs: /docs). Ctrl-C to stop.", file=sys.stderr)
    if is_lan_bind(host):
        print(
            "LAN mode, no authentication: anyone on your network can read your data and change thresholds. "
            f"Try {' or '.join(lan_urls(host, port))}",
            file=sys.stderr,
        )
    # proxy_headers=False: the peer-address check must not trust X-Forwarded-For.
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning", proxy_headers=False)
