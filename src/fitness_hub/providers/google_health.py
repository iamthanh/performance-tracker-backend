"""Google Health API v4 (successor to the Fitbit Web API).

URL paths use kebab-case data types (``body-fat``), filters use snake_case
(``body_fat``) and data point JSON keys use camelCase (``bodyFat``). Filters
only use ``>=``, ``<`` and ``AND``. int64 values arrive as JSON strings and
distances are millimetres.

Value field names for daily-oxygen-saturation, daily-respiratory-rate,
daily-vo2-max, weight and floors rollups are UNVERIFIED: they are read from a
hint list with a first-numeric fallback, the raw payload is always stored, and
a warning is logged whenever the expected field is missing.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..auth import AuthError, TokenStore, expires_soon, make_pkce, new_state, run_browser_flow
from ..config import Settings
from ..db import Store
from ..http import ApiError, new_client, request_json
from ..util import civil_date, parse_duration_s, parse_ts, to_float, ts_to_utc_iso
from . import StreamProgress, SyncResult, SyncWindow, run_day_stream, run_stream

log = logging.getLogger(__name__)

SOURCE = "google_health"
API_BASE = "https://health.googleapis.com/v4"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth"
SCOPES = (
    f"{SCOPE_PREFIX}.activity_and_fitness.readonly",
    f"{SCOPE_PREFIX}.health_metrics_and_measurements.readonly",
    f"{SCOPE_PREFIX}.sleep.readonly",
)
SESSION_PAGE_SIZE = 25  # exercise and sleep are capped at 25 per page
MAX_PAGES = 2000
REAUTH = "run `fitness-hub auth google`"
INVALID_GRANT_HELP = (
    "Google rejected the refresh token (invalid_grant). It was revoked or expired: while the OAuth "
    f"consent screen is in 'Testing' status, refresh tokens expire after 7 days. {REAUTH}"
)

# kebab type -> (metric, value-field hints). Hints for the first two are confirmed
# in the v4 reference; the rest are UNVERIFIED best guesses.
DAILY_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "daily-resting-heart-rate": ("resting_hr", ("beatsPerMinute",)),
    "daily-heart-rate-variability": ("hrv_rmssd_ms", ("averageHeartRateVariabilityMilliseconds",)),
    "daily-oxygen-saturation": (
        "spo2_pct",
        ("averagePercentage", "percentage", "averageOxygenSaturationPercentage", "average", "value"),
    ),
    "daily-respiratory-rate": (
        "respiratory_rate_bpm",
        ("breathsPerMinute", "averageBreathsPerMinute", "fullSleepBreathsPerMinute", "value"),
    ),
    "daily-vo2-max": (
        "vo2_max",
        ("vo2Max", "estimatedVo2Max", "vo2MaxMillilitersPerMinutePerKilogram", "value"),
    ),
}
SAMPLE_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "weight": ("weight_kg", ("weightGrams", "grams", "kilograms", "weightKilograms", "weight", "value")),
    "body-fat": ("body_fat_pct", ("percentage",)),
}
# kebab type -> (metric, max days per dailyRollUp request)
ROLLUP_TYPES: dict[str, tuple[str, int]] = {
    "steps": ("steps", 90),
    "distance": ("distance_m", 90),
    "total-calories": ("total_calories_kcal", 14),
    "floors": ("floors", 90),
    "active-zone-minutes": ("active_zone_minutes", 90),
}
_SKIP_KEYS = {"date", "sampleTime", "interval", "name", "dataSource", "metadata", "civilStartTime", "civilEndTime"}
_warned: set[tuple[str, str]] = set()
_logged_samples: set[str] = set()


def snake(data_type: str) -> str:
    return data_type.replace("-", "_")


def camel(data_type: str) -> str:
    head, *rest = data_type.split("-")
    return head + "".join(part.capitalize() for part in rest)


def _warn_once(data_type: str, message: str) -> None:
    if (data_type, message) not in _warned:
        _warned.add((data_type, message))
        log.warning("google_health %s: %s", data_type, message)


def _payload(data_point: Mapping[str, Any], data_type: str) -> Mapping[str, Any]:
    """The type-specific object of a data point (``{"bodyFat": {...}}``)."""
    inner = data_point.get(camel(data_type))
    return inner if isinstance(inner, Mapping) else data_point


def _first_numeric(payload: Mapping[str, Any], depth: int = 1) -> tuple[float | None, str | None]:
    for key, value in payload.items():
        if key in _SKIP_KEYS:
            continue
        number = to_float(value)
        if number is not None:
            return number, key
    if depth > 0:
        for key, value in payload.items():
            if key not in _SKIP_KEYS and isinstance(value, Mapping):
                number, sub = _first_numeric(value, depth - 1)
                if number is not None:
                    return number, f"{key}.{sub}"
    return None, None


def pick_value(
    payload: Mapping[str, Any], hints: tuple[str, ...], data_type: str
) -> tuple[float | None, str | None]:
    """Value from the first hint present, else the first numeric field (logged)."""
    for hint in hints:
        if hint in payload:
            raw = payload[hint]
            if isinstance(raw, Mapping):
                number, sub = _first_numeric(raw, 0)
                if number is not None:
                    return number, f"{hint}.{sub}"
            number = to_float(raw)
            if number is not None:
                return number, hint
    number, field = _first_numeric(payload)
    if number is None:
        _warn_once(data_type, f"no numeric value found (keys: {sorted(payload)}); raw payload stored only")
    else:
        _warn_once(data_type, f"expected one of {list(hints)}; using field {field!r}. Please verify the mapping")
    return number, field


# -- pure mappers -------------------------------------------------------------------


def map_exercise(dp: Mapping[str, Any]) -> dict[str, Any] | None:
    e = _payload(dp, "exercise")
    interval = e.get("interval") or {}
    start = ts_to_utc_iso(interval.get("startTime"))
    if not start:
        return None
    end = ts_to_utc_iso(interval.get("endTime"))
    duration = None
    if end:
        duration = (parse_ts(end) - parse_ts(start)).total_seconds()  # type: ignore[operator]
    summary = e.get("metricsSummary") or {}
    name = dp.get("name") or ""
    source_id = name.rsplit("/", 1)[-1] if name else start
    distance_mm = to_float(summary.get("distanceMillimeters"))
    elevation_mm = to_float(summary.get("elevationGainMillimeters"))
    return {
        "source": SOURCE,
        "source_id": source_id,
        "start_time": start,
        "local_date": civil_date(interval.get("civilStartTime")) or start[:10],
        "activity_type": e.get("exerciseType"),
        "name": e.get("displayName"),
        "duration_s": duration,
        "moving_s": parse_duration_s(e.get("activeDuration")),
        "distance_m": distance_mm / 1000.0 if distance_mm is not None else None,
        "elevation_gain_m": elevation_mm / 1000.0 if elevation_mm is not None else None,
        "avg_hr": to_float(summary.get("averageHeartRateBeatsPerMinute")),
        "max_hr": to_float(summary.get("maxHeartRateBeatsPerMinute")),
        "avg_power_w": to_float(summary.get("averagePowerWatts")),
        "calories": to_float(summary.get("caloriesKcal")),
        "raw": dict(dp),
    }


def map_sleep(dp: Mapping[str, Any]) -> dict[str, Any] | None:
    s = _payload(dp, "sleep")
    interval = s.get("interval") or {}
    start = ts_to_utc_iso(interval.get("startTime"))
    end = ts_to_utc_iso(interval.get("endTime"))
    if not start and not end:
        return None
    summary = s.get("summary") or {}
    stages: dict[str, float] = {}
    for stage in summary.get("stagesSummary") or []:
        kind = str(stage.get("type", "")).upper()
        minutes = to_float(stage.get("minutes"))
        if minutes is not None:
            stages[kind] = stages.get(kind, 0.0) + minutes
    metadata = s.get("metadata") or {}
    main = metadata.get("mainSleep")
    if main is None:
        main = not metadata.get("nap", False)
    name = dp.get("name") or ""
    return {
        "source": SOURCE,
        "source_id": name.rsplit("/", 1)[-1] if name else (start or end),
        "start_time": start,
        "end_time": end,
        "local_date": civil_date(interval.get("civilEndTime")) or (end or start)[:10],
        "minutes_asleep": to_float(summary.get("minutesAsleep")),
        "minutes_awake": to_float(summary.get("minutesAwake")),
        "minutes_deep": stages.get("DEEP"),
        "minutes_light": stages.get("LIGHT"),
        "minutes_rem": stages.get("REM"),
        "is_main": 1 if main else 0,
        "raw": dict(dp),
    }


def map_daily(data_type: str, dp: Mapping[str, Any]) -> dict[str, Any] | None:
    metric, hints = DAILY_TYPES[data_type]
    payload = _payload(dp, data_type)
    day = civil_date(payload.get("date")) or civil_date(dp.get("date"))
    if not day:
        _warn_once(data_type, "data point without a date; skipped")
        return None
    value, _ = pick_value(payload, hints, data_type)
    return {"source": SOURCE, "date": day, "metric": metric, "value": value, "raw": dict(dp)}


def map_sample(data_type: str, dp: Mapping[str, Any]) -> dict[str, Any] | None:
    metric, hints = SAMPLE_TYPES[data_type]
    payload = _payload(dp, data_type)
    sample_time = payload.get("sampleTime") or dp.get("sampleTime") or {}
    ts = ts_to_utc_iso(sample_time.get("physicalTime")) if isinstance(sample_time, Mapping) else None
    if not ts:
        _warn_once(data_type, "data point without sampleTime.physicalTime; skipped")
        return None
    value, field = pick_value(payload, hints, data_type)
    if value is not None and data_type == "weight":
        lowered = (field or "").lower()
        if "gram" in lowered and "kilogram" not in lowered:
            value /= 1000.0
        elif "kilogram" not in lowered and value > 500:
            _warn_once(data_type, f"value {value} in field {field!r} looks like grams; converting to kg")
            value /= 1000.0
    return {"source": SOURCE, "ts": ts, "metric": metric, "value": value, "raw": dict(dp)}


def rollup_value(data_type: str, point: Mapping[str, Any]) -> float | None:
    payload = point.get(camel(data_type))
    if not isinstance(payload, Mapping):
        _warn_once(data_type, f"rollup point without {camel(data_type)!r} key")
        return None
    if data_type == "steps":
        return to_float(payload.get("countSum"))
    if data_type == "distance":
        mm = to_float(payload.get("millimetersSum"))
        return mm * 0.001 if mm is not None else None
    if data_type == "total-calories":
        return to_float(payload.get("kcalSum"))
    if data_type == "active-zone-minutes":
        parts = [
            to_float(payload.get(k))
            for k in ("sumInCardioHeartZone", "sumInPeakHeartZone", "sumInFatBurnHeartZone")
        ]
        present = [p for p in parts if p is not None]
        return sum(present) if present else None
    value, _ = pick_value(payload, ("countSum",), data_type)  # floors: UNVERIFIED
    return value


def map_rollup(data_type: str, point: Mapping[str, Any]) -> dict[str, Any] | None:
    metric, _ = ROLLUP_TYPES[data_type]
    day = civil_date(point.get("civilStartTime"))
    if not day and isinstance(point.get("civilInterval"), Mapping):
        day = civil_date(point["civilInterval"].get("start"))
    if not day:
        _warn_once(data_type, "rollup point without civilStartTime; skipped")
        return None
    return {
        "source": SOURCE,
        "date": day,
        "metric": metric,
        "value": rollup_value(data_type, point),
        "raw": dict(point),
    }


# -- request builders ---------------------------------------------------------------


def daily_filter(data_type: str, start: date, end: date) -> str:
    field = f"{snake(data_type)}.date"
    return f'{field} >= "{start.isoformat()}" AND {field} < "{(end + timedelta(days=1)).isoformat()}"'


def sample_filter(data_type: str, start: date) -> str:
    return f'{snake(data_type)}.sample_time.physical_time >= "{start.isoformat()}T00:00:00Z"'


def exercise_filter(start: date) -> str:
    return f'exercise.interval.civil_start_time >= "{start.isoformat()}"'


def sleep_filter(start: date) -> str:
    return f'sleep.interval.civil_end_time >= "{start.isoformat()}"'


def civil_datetime(day: date) -> dict[str, Any]:
    return {
        "date": {"year": day.year, "month": day.month, "day": day.day},
        "time": {"hours": 0, "minutes": 0, "seconds": 0, "nanos": 0},
    }


def rollup_chunks(start: date, end_inclusive: date, max_days: int) -> list[tuple[date, date]]:
    """Closed-open ``[start, end)`` ranges of at most ``max_days`` days."""
    chunks = []
    stop = end_inclusive + timedelta(days=1)
    cursor = start
    while cursor < stop:
        nxt = min(cursor + timedelta(days=max_days), stop)
        chunks.append((cursor, nxt))
        cursor = nxt
    return chunks


# -- provider -----------------------------------------------------------------------


class GoogleHealthProvider:
    name = SOURCE

    def __init__(
        self,
        settings: Settings,
        tokens: TokenStore,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.tokens = tokens
        self._client = client
        self._owns_client = client is None
        self.sleep = sleep
        self.now = now

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = new_client()
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def configured(self) -> bool:
        token = self.tokens.get(SOURCE)
        return bool(token and token.get("refresh_token"))

    def access_token(self) -> str:
        token = self.tokens.get(SOURCE)
        if not token or not token.get("refresh_token"):
            raise AuthError(f"Google Health is not authorized; {REAUTH}")
        if not expires_soon(token, now=self.now()):
            return token["access_token"]
        if not self.settings.google_client_id or not self.settings.google_client_secret:
            raise AuthError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required to refresh the Google token")
        try:
            payload = request_json(
                self.client,
                "POST",
                TOKEN_URL,
                data={
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "refresh_token": token["refresh_token"],
                    "grant_type": "refresh_token",
                },
                sleep=self.sleep,
            )
        except ApiError as exc:
            if "invalid_grant" in exc.body:
                raise AuthError(INVALID_GRANT_HELP) from None
            if exc.status in (400, 401):
                raise AuthError(f"Google token refresh failed (HTTP {exc.status}); {REAUTH}") from None
            raise
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise AuthError(f"Google token response had no access_token; {REAUTH}")
        token.update(
            access_token=payload["access_token"],
            expires_at=int(self.now() + float(payload.get("expires_in") or 3600)),
        )
        if payload.get("refresh_token"):
            token["refresh_token"] = payload["refresh_token"]
        self.tokens.save(SOURCE, token)
        log.info("refreshed Google access token")
        return token["access_token"]

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        return request_json(self.client, method, API_BASE + path, headers=headers, sleep=self.sleep, **kwargs)

    def list_pages(self, data_type: str, filter_: str, page_size: int | None = None) -> Iterator[list[dict]]:
        """Yield pages of data points for ``data_type`` (newest first)."""
        params: dict[str, Any] = {"filter": filter_}
        if page_size:
            params["pageSize"] = page_size
        seen_tokens: set[str] = set()
        for _ in range(MAX_PAGES):
            body = self._request("GET", f"/users/me/dataTypes/{data_type}/dataPoints", params=params) or {}
            points = body.get("dataPoints") or []
            if points and data_type not in _logged_samples:
                _logged_samples.add(data_type)
                log.debug("google_health %s sample payload: %s", data_type, json.dumps(points[0])[:4000])
            yield points
            token = body.get("nextPageToken")
            if not token or token in seen_tokens:
                return
            seen_tokens.add(token)
            params["pageToken"] = token
        raise ApiError(200, f"more than {MAX_PAGES} pages for {data_type}; aborting", API_BASE)

    def daily_rollup(self, data_type: str, start: date, end_exclusive: date) -> list[dict]:
        body = {"range": {"start": civil_datetime(start), "end": civil_datetime(end_exclusive)}, "windowSizeDays": 1}
        result = (
            self._request(
                "POST", f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", json=body, idempotent=True
            )
            or {}
        )
        return result.get("rollupDataPoints") or []

    # -- streams --------------------------------------------------------------------

    def sync(self, store: Store, window: SyncWindow, full: bool = False) -> list[SyncResult]:
        # Check auth once so an expired token yields one actionable error, not 14.
        def check_auth(progress: StreamProgress) -> None:
            self.access_token()

        auth = run_stream(store, SOURCE, "auth", check_auth)
        if not auth.ok:
            return [auth]
        results = [
            self._run(store, window, full, "exercise", self._list_stream(
                "exercise", lambda s, e: exercise_filter(s), map_exercise, store.upsert_activities,
                SESSION_PAGE_SIZE)),
            self._run(store, window, full, "sleep", self._list_stream(
                "sleep", lambda s, e: sleep_filter(s), map_sleep, store.upsert_sleep_sessions,
                SESSION_PAGE_SIZE)),
        ]
        for data_type in DAILY_TYPES:
            results.append(
                self._run(store, window, full, data_type, self._list_stream(
                    data_type,
                    lambda s, e, t=data_type: daily_filter(t, s, e),
                    lambda dp, t=data_type: map_daily(t, dp),
                    store.upsert_daily_metrics,
                ))
            )
        for data_type in SAMPLE_TYPES:
            results.append(
                self._run(store, window, full, data_type, self._list_stream(
                    data_type,
                    lambda s, e, t=data_type: sample_filter(t, s),
                    lambda dp, t=data_type: map_sample(t, dp),
                    store.upsert_body_measurements,
                ))
            )
        for data_type in ROLLUP_TYPES:
            results.append(self._run(store, window, full, f"rollup:{data_type}", self._rollup_stream(data_type)))
        return results

    def _run(
        self,
        store: Store,
        window: SyncWindow,
        full: bool,
        stream: str,
        body: Callable[[Store, date, date, StreamProgress], None],
    ) -> SyncResult:
        return run_day_stream(
            store, SOURCE, stream, window, self.settings.lookback_days, full, lambda s, e, p: body(store, s, e, p)
        )

    def _list_stream(
        self,
        data_type: str,
        make_filter: Callable[[date, date], str],
        mapper: Callable[[Mapping[str, Any]], dict[str, Any] | None],
        upsert: Callable[[list[dict[str, Any]]], int],
        page_size: int | None = None,
    ) -> Callable[[Store, date, date, StreamProgress], None]:
        def body(store: Store, start: date, end: date, progress: StreamProgress) -> None:
            for page in self.list_pages(data_type, make_filter(start, end), page_size):
                rows = [row for row in (mapper(dp) for dp in page if isinstance(dp, Mapping)) if row]
                progress.rows += upsert(rows)

        return body

    def _rollup_stream(self, data_type: str) -> Callable[[Store, date, date, StreamProgress], None]:
        _, max_days = ROLLUP_TYPES[data_type]

        def body(store: Store, start: date, end: date, progress: StreamProgress) -> None:
            for chunk_start, chunk_end in rollup_chunks(start, end, max_days):
                points = self.daily_rollup(data_type, chunk_start, chunk_end)
                rows = [row for row in (map_rollup(data_type, p) for p in points if isinstance(p, Mapping)) if row]
                progress.rows += store.upsert_daily_metrics(rows)

        return body



# -- authorization --------------------------------------------------------------------


def authorize_url(settings: Settings, state: str, code_challenge: str) -> str:
    query = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urlencode(query)


def fetch_identity(client: httpx.Client, access_token: str) -> dict[str, Any]:
    try:
        body = request_json(
            client, "GET", API_BASE + "/users/me/identity", headers={"Authorization": f"Bearer {access_token}"}
        )
    except ApiError as exc:
        if exc.status == 400 and "ACCOUNT_NOT_LINKED" in exc.body:
            raise AuthError(
                "This Google account is not linked to Google Health data yet. Sign in to the Google Health "
                "(formerly Fitbit) app with this Google account, let your device sync, then run "
                "`fitness-hub auth google` again."
            ) from None
        raise
    return body if isinstance(body, dict) else {}


def authorize(
    settings: Settings,
    tokens: TokenStore,
    *,
    client: httpx.Client | None = None,
    open_browser: bool = True,
    echo: Callable[[str], None] = print,
    flow: Callable[..., dict[str, str]] = run_browser_flow,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Interactive OAuth (Web client + PKCE), token exchange, identity check."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise AuthError(
            "set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (Google Cloud OAuth client of type "
            f"'Web application' with redirect URI {settings.redirect_uri})"
        )
    state = new_state()
    verifier, challenge = make_pkce()
    params = flow(
        authorize_url(settings, state, challenge), settings.oauth_port, state, open_browser=open_browser, echo=echo
    )
    own_client = client is None
    http = client or new_client()
    try:
        try:
            payload = request_json(
                http,
                "POST",
                TOKEN_URL,
                data={
                    "code": params["code"],
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
        except ApiError as exc:
            raise AuthError(f"Google code exchange failed (HTTP {exc.status}): {exc.body[:300]}") from None
        if not isinstance(payload, dict) or not payload.get("refresh_token"):
            raise AuthError(
                "Google did not return a refresh token. Remove fitness-hub's access at "
                "https://myaccount.google.com/permissions and run auth again."
            )
        granted = set(str(payload.get("scope", "")).split())
        missing = [s for s in SCOPES if s not in granted]
        if missing:
            echo("Warning: these scopes were not granted, related streams will fail: " + ", ".join(missing))
        record = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": int(now() + float(payload.get("expires_in") or 3600)),
            "scope": " ".join(sorted(granted)),
        }
        tokens.save(SOURCE, record)
        identity = fetch_identity(http, record["access_token"])
    finally:
        if own_client:
            http.close()
    record["health_user_id"] = identity.get("healthUserId")
    record["legacy_user_id"] = identity.get("legacyUserId")
    tokens.save(SOURCE, record)
    return {"health_user_id": record["health_user_id"], "missing_scopes": missing}
