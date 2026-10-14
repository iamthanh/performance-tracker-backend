"""Strava (official API v3): OAuth2 and athlete activity summaries."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any
from urllib.parse import urlencode

import httpx

from ..auth import AuthError, TokenStore, expires_soon, new_state, run_browser_flow
from ..config import Settings
from ..db import Store
from ..http import ApiError, new_client, request_json
from ..util import parse_date, to_float, ts_to_utc_iso
from . import StreamProgress, SyncResult, SyncWindow, run_stream

log = logging.getLogger(__name__)

SOURCE = "strava"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
SCOPES = "read,activity:read_all,profile:read_all"
PER_PAGE = 200
MAX_PAGES = 1000
# Strava's 429 means a 15-minute (or daily) window is exhausted: retrying within
# seconds is pointless, so only transient gateway errors are retried.
RETRY_STATUSES = frozenset({502, 503, 504})
REAUTH = "run `fitness-hub auth strava`"


def map_activity(a: dict[str, Any]) -> dict[str, Any] | None:
    """Strava summary activity -> ``activities`` row (``None`` if unusable)."""
    start = ts_to_utc_iso(a.get("start_date"))
    if a.get("id") is None or not start:
        return None
    local_raw = a.get("start_date_local")
    try:
        local_date = parse_date(local_raw).isoformat() if local_raw else start[:10]
    except ValueError:
        local_date = start[:10]
    return {
        "source": SOURCE,
        "source_id": str(a["id"]),
        "start_time": start,
        "local_date": local_date,
        "activity_type": a.get("sport_type") or a.get("type"),
        "name": a.get("name"),
        "duration_s": to_float(a.get("elapsed_time")),
        "moving_s": to_float(a.get("moving_time")),
        "distance_m": to_float(a.get("distance")),
        "elevation_gain_m": to_float(a.get("total_elevation_gain")),
        "avg_hr": to_float(a.get("average_heartrate")),
        "max_hr": to_float(a.get("max_heartrate")),
        "avg_power_w": to_float(a.get("average_watts")),
        # Only the detailed endpoint has calories; summaries leave this NULL.
        "calories": to_float(a.get("calories")),
        "raw": a,
    }


def _token_record(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AuthError(f"Strava token response had no access_token; {REAUTH}")
    record = dict(previous or {})
    record.update(
        access_token=payload["access_token"],
        # Refresh tokens rotate: always keep the newest one Strava returns.
        refresh_token=payload.get("refresh_token") or record.get("refresh_token"),
        expires_at=int(payload.get("expires_at") or (time.time() + float(payload.get("expires_in") or 0))),
    )
    athlete = payload.get("athlete")
    if isinstance(athlete, dict) and athlete.get("id") is not None:
        record["athlete_id"] = athlete["id"]
    return record


class StravaProvider:
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
            raise AuthError(f"Strava is not authorized; {REAUTH}")
        if not expires_soon(token, now=self.now()):
            return token["access_token"]
        if not self.settings.strava_client_id or not self.settings.strava_client_secret:
            raise AuthError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET are required to refresh the Strava token")
        try:
            payload = request_json(
                self.client,
                "POST",
                TOKEN_URL,
                data={
                    "client_id": self.settings.strava_client_id,
                    "client_secret": self.settings.strava_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                },
                retry_statuses=RETRY_STATUSES,
                sleep=self.sleep,
            )
        except ApiError as exc:
            if exc.status in (400, 401, 403):
                raise AuthError(f"Strava token refresh was rejected (HTTP {exc.status}); {REAUTH}") from None
            raise
        record = _token_record(payload, token)
        self.tokens.save(SOURCE, record)
        log.info("refreshed Strava access token")
        return record["access_token"]

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        try:
            return request_json(
                self.client,
                "GET",
                API_BASE + path,
                params=params,
                headers=headers,
                retry_statuses=RETRY_STATUSES,
                sleep=self.sleep,
            )
        except ApiError as exc:
            if exc.status == 401:
                raise AuthError(f"Strava rejected the access token (HTTP 401); {REAUTH}") from None
            raise

    def sync(self, store: Store, window: SyncWindow, full: bool = False) -> list[SyncResult]:
        return [
            run_stream(store, SOURCE, "activities", lambda p: self._sync_activities(store, window, full, p))
        ]

    def _sync_activities(
        self, store: Store, window: SyncWindow, full: bool, progress: StreamProgress
    ) -> None:
        previous = store.get_cursor(SOURCE, "activities")
        start = window.start_for(previous, self.settings.lookback_days, full)
        after = int(datetime.combine(start, dtime.min, tzinfo=UTC).timestamp())
        latest = previous
        for page in range(1, MAX_PAGES + 1):
            batch = self._get("/athlete/activities", {"after": after, "per_page": PER_PAGE, "page": page})
            if not isinstance(batch, list):
                raise ApiError(200, f"expected a list of activities, got {type(batch).__name__}", API_BASE)
            rows = []
            for item in batch:
                row = map_activity(item) if isinstance(item, dict) else None
                if row is None:
                    log.warning("skipping Strava activity without id/start_date: %r", item)
                    continue
                rows.append(row)
                latest = max(latest or row["start_time"], row["start_time"])
            progress.rows += store.upsert_activities(rows)
            if len(batch) < PER_PAGE:
                break
        # The cursor only moves once every page is stored (an interrupted or
        # rate-limited run re-fetches from the previous cursor next time).
        progress.cursor = latest


def authorize_url(settings: Settings, state: str) -> str:
    query = {
        "client_id": settings.strava_client_id,
        "response_type": "code",
        "redirect_uri": settings.redirect_uri,
        "approval_prompt": "auto",
        "scope": SCOPES,
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urlencode(query)


def authorize(
    settings: Settings,
    tokens: TokenStore,
    *,
    client: httpx.Client | None = None,
    open_browser: bool = True,
    echo: Callable[[str], None] = print,
    flow: Callable[..., dict[str, str]] = run_browser_flow,
) -> dict[str, Any]:
    """Interactive OAuth: browser consent, code exchange, tokens saved."""
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise AuthError(
            "set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET (from https://www.strava.com/settings/api; "
            "Authorization Callback Domain must be 'localhost')"
        )
    state = new_state()
    params = flow(authorize_url(settings, state), settings.oauth_port, state, open_browser=open_browser, echo=echo)
    granted = params.get("scope", "")
    if "activity:read" not in granted:
        echo(
            "Warning: activity access was not granted (scope=%r); private activities and "
            "activity lists may be unavailable. Re-run auth and keep the boxes checked." % granted
        )
    own_client = client is None
    http = client or new_client()
    try:
        payload = request_json(
            http,
            "POST",
            TOKEN_URL,
            data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "code": params["code"],
                "grant_type": "authorization_code",
            },
            retry_statuses=RETRY_STATUSES,
        )
    except ApiError as exc:
        raise AuthError(f"Strava code exchange failed (HTTP {exc.status}): {exc.body[:200]}") from None
    finally:
        if own_client:
            http.close()
    record = _token_record(payload)
    record["scope"] = granted
    if not record.get("refresh_token"):
        raise AuthError("Strava did not return a refresh token")
    tokens.save(SOURCE, record)
    return {"athlete_id": record.get("athlete_id"), "scope": granted}
