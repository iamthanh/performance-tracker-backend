"""Garmin Connect via the unofficial ``garminconnect`` library, plus the CSV
export importer used as the fallback when login breaks.

Garmin's official APIs are partner-only, so this logs in like the website
does. It is best-effort: the library and Garmin's bot protection change
without notice. The password is never stored; only session tokens are kept
in ``$FITNESS_HUB_HOME/garmin-tokens``.
"""

from __future__ import annotations

import csv
import getpass
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..auth import AuthError
from ..config import Settings
from ..db import Store
from ..util import daterange, epoch_ms_to_utc_iso, parse_date, to_float, utc_iso
from . import StreamProgress, SyncResult, SyncWindow, partial_cursor, run_day_stream, run_stream

log = logging.getLogger(__name__)

SOURCE = "garmin"
EXPORT_SOURCE = "garmin_export"
TOKEN_FILE = "garmin_tokens.json"  # name garminconnect 0.3 uses inside a token directory
DAY_PAUSE_S = 0.4
REAUTH = "run `fitness-hub auth garmin`"
INSTALL_HINT = (
    "the Garmin provider needs the optional dependency: pip install 'fitness-hub[garmin]' "
    "(garminconnect requires Python >= 3.12). The CSV importer works without it."
)

# Garmin daily summary field -> daily_metrics metric. The first four are the
# documented ones; the others are best-effort and simply absent on some devices.
STATS_FIELDS: dict[str, str] = {
    "totalSteps": "steps",
    "restingHeartRate": "resting_hr",
    "averageStressLevel": "avg_stress",
    "bodyBatteryHighestValue": "body_battery_high",
    "bodyBatteryLowestValue": "body_battery_low",
    "totalKilocalories": "total_calories_kcal",
    "activeKilocalories": "active_calories_kcal",
    "totalDistanceMeters": "distance_m",
    "floorsAscended": "floors",
    "averageSpo2": "spo2_pct",
    "avgWakingRespirationValue": "respiratory_rate_bpm",
}


def _naive_utc_to_iso(value: Any) -> str | None:
    """``"YYYY-MM-DD HH:MM:SS"`` (Garmin's GMT fields, no zone) -> UTC ISO."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return utc_iso(datetime.fromisoformat(value.strip().replace(" ", "T")))
    except ValueError:
        return None


# -- pure mappers --------------------------------------------------------------------


def map_activity(a: Mapping[str, Any]) -> dict[str, Any] | None:
    start = _naive_utc_to_iso(a.get("startTimeGMT"))
    if a.get("activityId") is None or not start:
        return None
    local = a.get("startTimeLocal")
    try:
        local_date = parse_date(local).isoformat() if local else start[:10]
    except ValueError:
        local_date = start[:10]
    activity_type = a.get("activityType")
    return {
        "source": SOURCE,
        "source_id": str(a["activityId"]),
        "start_time": start,
        "local_date": local_date,
        "activity_type": activity_type.get("typeKey") if isinstance(activity_type, Mapping) else None,
        "name": a.get("activityName"),
        "duration_s": to_float(a.get("duration")),
        "moving_s": to_float(a.get("movingDuration")),
        "distance_m": to_float(a.get("distance")),
        "elevation_gain_m": to_float(a.get("elevationGain")),
        "avg_hr": to_float(a.get("averageHR")),
        "max_hr": to_float(a.get("maxHR")),
        "avg_power_w": to_float(a.get("avgPower")),
        "calories": to_float(a.get("calories")),
        "raw": dict(a),
    }


def map_stats(day: str, stats: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(stats, Mapping):
        return []
    rows = []
    for field, metric in STATS_FIELDS.items():
        value = to_float(stats.get(field))
        if value is None or (metric == "avg_stress" and value < 0):  # -1/-2 mean "no data"
            continue
        rows.append({"source": SOURCE, "date": day, "metric": metric, "value": value, "raw": dict(stats)})
    return rows


def map_sleep(day: str, data: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Garmin sleep payload -> (sleep session row or None, daily metric rows)."""
    if not isinstance(data, Mapping):
        return None, []
    dto = data.get("dailySleepDTO") or {}
    metrics = []
    score = None
    scores = dto.get("sleepScores")
    if isinstance(scores, Mapping) and isinstance(scores.get("overall"), Mapping):
        score = to_float(scores["overall"].get("value"))
    local_date = dto.get("calendarDate") or day  # Garmin files sleep under the wake-up day
    if score is not None:
        metrics.append({"source": SOURCE, "date": local_date, "metric": "sleep_score", "value": score, "raw": dict(dto)})
    start = epoch_ms_to_utc_iso(dto.get("sleepStartTimestampGMT"))
    end = epoch_ms_to_utc_iso(dto.get("sleepEndTimestampGMT"))
    if not start or not end:
        return None, metrics

    def minutes(field: str) -> float | None:
        seconds = to_float(dto.get(field))
        return seconds / 60.0 if seconds is not None else None

    session = {
        "source": SOURCE,
        "source_id": str(dto.get("id") or start),
        "start_time": start,
        "end_time": end,
        "local_date": local_date,
        "minutes_asleep": minutes("sleepTimeSeconds"),
        "minutes_awake": minutes("awakeSleepSeconds"),
        "minutes_deep": minutes("deepSleepSeconds"),
        "minutes_light": minutes("lightSleepSeconds"),
        "minutes_rem": minutes("remSleepSeconds"),
        "is_main": 1,
        "raw": dict(data),
    }
    return session, metrics


def map_hrv(day: str, data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    summary = data.get("hrvSummary") or {}
    value = to_float(summary.get("lastNightAvg"))
    if value is None:
        return []
    return [
        {
            "source": SOURCE,
            "date": summary.get("calendarDate") or day,
            "metric": "hrv_rmssd_ms",
            "value": value,
            "raw": dict(summary),
        }
    ]


def map_heart_rates(day: str, data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``get_heart_rates`` -> a ``daily_series`` row of all-day HR (epoch seconds, bpm).

    ``heartRateValues`` holds ``[timestamp_ms_gmt, bpm]`` pairs (about one every
    two minutes); ``heartRateValueDescriptors`` names the positions when present.
    Samples without a value are dropped.
    """
    if not isinstance(data, Mapping):
        return None
    values = data.get("heartRateValues")
    if not isinstance(values, list):
        return None
    index = {"timestamp": 0, "heartrate": 1}
    for d in data.get("heartRateValueDescriptors") or []:
        if isinstance(d, Mapping) and d.get("key") in index and isinstance(d.get("index"), int):
            index[d["key"]] = d["index"]
    samples = []
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) <= max(index.values()):
            continue
        ts, bpm = to_float(pair[index["timestamp"]]), to_float(pair[index["heartrate"]])
        if ts is not None and bpm is not None:
            samples.append((int(ts // 1000), bpm))
    if not samples:
        return None
    samples.sort()
    return {
        "source": SOURCE,
        "date": day,
        "series": "heart_rate",
        "streams": {"time": [t for t, _ in samples], "heart_rate": [b for _, b in samples]},
    }


def map_body(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    rows = []
    for entry in data.get("dateWeightList") or []:
        if not isinstance(entry, Mapping):
            continue
        # `timestampGMT` is the real instant; `date` is local wall-clock time
        # encoded as epoch ms, so it is only a fallback.
        ts = epoch_ms_to_utc_iso(entry.get("timestampGMT") or entry.get("date"))
        if not ts:
            continue
        grams = to_float(entry.get("weight"))
        if grams is not None:
            rows.append({"source": SOURCE, "ts": ts, "metric": "weight_kg", "value": grams / 1000.0, "raw": dict(entry)})
        fat = to_float(entry.get("bodyFat"))
        if fat is not None:
            rows.append({"source": SOURCE, "ts": ts, "metric": "body_fat_pct", "value": fat, "raw": dict(entry)})
    return rows


# -- provider ------------------------------------------------------------------------


def _default_client_factory() -> Any:
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise AuthError(INSTALL_HINT) from exc
    return Garmin()


class GarminProvider:
    name = SOURCE

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.client_factory = client_factory or _default_client_factory
        self.sleep = sleep

    def configured(self) -> bool:
        return (self.settings.garmin_token_dir / TOKEN_FILE).is_file()

    def close(self) -> None:
        return None

    def login(self) -> Any:
        client = self.client_factory()
        client.login(str(self.settings.garmin_token_dir))
        return client

    def sync(self, store: Store, window: SyncWindow, full: bool = False) -> list[SyncResult]:
        holder: dict[str, Any] = {}

        def do_login(progress: StreamProgress) -> None:
            try:
                holder["client"] = self.login()
            except AuthError:
                raise
            except Exception as exc:  # noqa: BLE001 - library raises many types
                raise AuthError(f"Garmin login failed ({type(exc).__name__}: {exc}); {REAUTH}") from exc

        login = run_stream(store, SOURCE, "login", do_login)
        if not login.ok:
            return [login]
        client = holder["client"]
        results = []
        # Workouts normally come from Strava; GARMIN_ACTIVITIES=true also syncs Garmin's.
        if self.settings.garmin_activities:
            activities = lambda s, e, p: self._activities(client, store, s, e, p)  # noqa: E731
            results.append(self._run(store, window, full, "activities", activities))
        elif store.get_state(SOURCE, "activities"):
            log.info("garmin activities are no longer synced by default; set GARMIN_ACTIVITIES=true to keep syncing them")
        results.append(self._run(store, window, full, "daily", lambda s, e, p: self._daily(client, store, s, e, p)))
        results.append(self._run(store, window, full, "body", lambda s, e, p: self._body(client, store, s, e, p)))
        return results

    def _run(
        self,
        store: Store,
        window: SyncWindow,
        full: bool,
        stream: str,
        body: Callable[[date, date, StreamProgress], None],
    ) -> SyncResult:
        return run_day_stream(store, SOURCE, stream, window, self.settings.lookback_days, full, body)

    def _activities(self, client: Any, store: Store, start: date, end: date, progress: StreamProgress) -> None:
        items = client.get_activities_by_date(start.isoformat(), end.isoformat()) or []
        rows = []
        for item in items:
            row = map_activity(item) if isinstance(item, Mapping) else None
            if row is None:
                log.warning("skipping Garmin activity without activityId/startTimeGMT")
                continue
            rows.append(row)
        progress.rows += store.upsert_activities(rows)

    def _daily(self, client: Any, store: Store, start: date, end: date, progress: StreamProgress) -> None:
        days = list(daterange(start, end))
        for index, day in enumerate(days):
            iso = day.isoformat()
            stats = _day_call(client.get_stats, iso, stop_on={"throttled", "auth", "error"})
            sleep_data = _day_call(client.get_sleep_data, iso, stop_on={"throttled", "auth", "error"})
            # HRV is absent on many devices, so only throttling or auth problems stop the stream.
            hrv = _day_call(client.get_hrv_data, iso, stop_on={"throttled", "auth"})
            heart_rates = _day_call(client.get_heart_rates, iso, stop_on={"throttled", "auth"})
            session, sleep_metrics = map_sleep(iso, sleep_data)
            progress.rows += store.upsert_daily_metrics(map_stats(iso, stats) + sleep_metrics + map_hrv(iso, hrv))
            hr_series = map_heart_rates(iso, heart_rates)
            if hr_series:
                progress.rows += store.upsert_daily_series([hr_series])
            if session:
                progress.rows += store.upsert_sleep_sessions([session])
            progress.cursor = partial_cursor(day)  # replaced by the window end on success
            if index < len(days) - 1:
                self.sleep(DAY_PAUSE_S)

    def _body(self, client: Any, store: Store, start: date, end: date, progress: StreamProgress) -> None:
        data = client.get_body_composition(start.isoformat(), end.isoformat())
        progress.rows += store.upsert_body_measurements(map_body(data))


def classify_error(exc: BaseException) -> str:
    """Sort a garminconnect error into ``throttled``, ``auth``, ``no_data`` or ``error``.

    Matched by class name so the library stays an optional import. In
    garminconnect 0.3, ``get_stats`` raises ``GarminConnectConnectionError("No
    data received from server")`` for a day without a summary (e.g. before the
    watch was owned) and ``GarminConnectAuthenticationError("Authentication
    error")`` for a privacy-protected day; neither is a reason to stop syncing.
    """
    name, text = type(exc).__name__, str(exc)
    if name == "GarminConnectTooManyRequestsError" or "429" in text:
        return "throttled"
    if name == "GarminConnectNotFoundError":
        return "no_data"
    if name == "GarminConnectConnectionError" and "No data received" in text:
        return "no_data"
    if name == "GarminConnectAuthenticationError":
        return "no_data" if text == "Authentication error" else "auth"
    return "error"


def _day_call(fn: Callable[[str], Any], iso: str, stop_on: set[str]) -> Any:
    """Call a per-day endpoint; ``None`` when that day has no data. Errors whose
    kind is in ``stop_on`` stop the stream at this day (the partial cursor makes
    the next run resume here); other errors only skip this part of the day."""
    try:
        return fn(iso)
    except Exception as exc:  # noqa: BLE001 - classified below
        kind = classify_error(exc)
        if kind in stop_on:
            raise RuntimeError(f"stopped at {iso} ({kind}): {type(exc).__name__}: {exc}") from exc
        if kind == "error":
            log.warning("garmin %s failed for %s, skipping it: %s", getattr(fn, "__name__", "call"), iso, exc)
        else:
            log.debug("garmin %s: no data for %s (%s)", getattr(fn, "__name__", "call"), iso, exc)
        return None


def authorize(
    settings: Settings,
    *,
    client_factory: Callable[..., Any] | None = None,
    prompt: Callable[[str], str] = input,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    echo: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Interactive login (email, password, MFA if asked) that saves session tokens.

    The password is only held in memory for the login call.
    """
    if client_factory is None:
        try:
            from garminconnect import Garmin
        except ImportError as exc:
            raise AuthError(INSTALL_HINT) from exc
        client_factory = Garmin
    email = settings.garmin_email or prompt("Garmin Connect email: ").strip()
    password = settings.garmin_password or prompt_secret("Garmin Connect password: ")
    if not email or not password:
        raise AuthError("Garmin email and password are required for the first login")
    token_dir = settings.garmin_token_dir
    token_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(token_dir, 0o700)
    client = client_factory(email=email, password=password, prompt_mfa=lambda: prompt("Garmin MFA code: ").strip())
    del password
    try:
        client.login(str(token_dir))
    except Exception as exc:  # noqa: BLE001
        raise AuthError(
            f"Garmin login failed ({type(exc).__name__}: {exc}). Garmin's bot protection breaks the "
            "unofficial login from time to time; use `fitness-hub import-garmin-csv` as a fallback."
        ) from exc
    token_file = token_dir / TOKEN_FILE
    if not token_file.is_file():
        # garminconnect suppresses errors when persisting tokens; try once more explicitly.
        inner = getattr(client, "client", None)
        if inner is not None and hasattr(inner, "dump"):
            inner.dump(str(token_dir))
    if not token_file.is_file():
        raise AuthError(f"Garmin login succeeded but no session tokens were written to {token_dir}")
    os.chmod(token_file, 0o600)
    name = None
    try:
        name = client.get_full_name()
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    echo(f"Garmin session saved to {token_dir}")
    return {"token_dir": str(token_dir), "name": name}


# -- CSV export importer ---------------------------------------------------------------

_KM_PER_MI = 1.609344
_M_PER_FT = 0.3048
_M_PER_YD = 0.9144
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse_hms(value: str | None) -> float | None:
    """``"1:02:03"``, ``"02:03"`` or ``"02:03.4"`` -> seconds."""
    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def _csv_number(value: str | None) -> float | None:
    if value is None or value.strip() in ("", "--"):
        return None
    return to_float(value)


def _parse_local(value: str) -> datetime | None:
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def map_csv_row(row: Mapping[str, str], distance_unit: str = "km", tz: ZoneInfo | None = None) -> dict[str, Any] | None:
    local = _parse_local(row.get("Date") or "")
    if local is None:
        return None
    if tz is not None:
        start = utc_iso(local.replace(tzinfo=tz))
    else:
        # Zone unknown: stored without a "Z" and read as UTC, so de-duplication
        # against other sources is unreliable (pass a timezone to fix that).
        start = local.strftime("%Y-%m-%dT%H:%M:%S")
    activity_type = (row.get("Activity Type") or "").strip() or None
    distance = _csv_number(row.get("Distance"))
    ascent = _csv_number(row.get("Total Ascent"))
    swim = "swim" in (activity_type or "").lower()
    if distance is not None:
        # Swims are exported in metres (yards on imperial accounts), e.g. "1,500";
        # everything else in km (mi).
        if distance_unit == "mi":
            distance *= _M_PER_YD if swim else _KM_PER_MI * 1000
        elif not swim:
            distance *= 1000
    if ascent is not None and distance_unit == "mi":
        ascent *= _M_PER_FT
    return {
        "source": EXPORT_SOURCE,
        "source_id": f"{local.strftime('%Y-%m-%dT%H:%M:%S')}|{activity_type or ''}",
        "start_time": start,
        "local_date": local.date().isoformat(),
        "activity_type": activity_type,
        "name": (row.get("Title") or "").strip() or None,
        "duration_s": parse_hms(row.get("Time")),
        "moving_s": parse_hms(row.get("Moving Time")),
        "distance_m": distance,
        "elevation_gain_m": ascent,
        "avg_hr": _csv_number(row.get("Avg HR")),
        "max_hr": _csv_number(row.get("Max HR")),
        "avg_power_w": _csv_number(row.get("Avg Power")),
        "calories": _csv_number(row.get("Calories")),
        "raw": dict(row),
    }


def import_garmin_csv(
    store: Store, path: Path | str, distance_unit: str = "km", timezone: str | None = None
) -> dict[str, int]:
    """Import Garmin Connect's "Export CSV" activity list. Idempotent."""
    if distance_unit not in ("km", "mi"):
        raise ValueError("distance_unit must be 'km' or 'mi'")
    tz = None
    if timezone:
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {timezone!r} (use an IANA name like Europe/Berlin)") from None
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"Activity Type", "Date"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} does not look like a Garmin activities export (missing {sorted(missing)})")
        rows, skipped = [], 0
        for raw in reader:
            row = map_csv_row(raw, distance_unit, tz)
            if row is None:
                skipped += 1
            else:
                rows.append(row)
    imported = store.upsert_activities(rows)
    store.record_sync(EXPORT_SOURCE, "csv", rows=imported)
    return {"imported": imported, "skipped": skipped}
