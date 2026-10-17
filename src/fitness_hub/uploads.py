"""Import one recorded activity file (FIT, GPX, TCX; optionally gzipped) uploaded by the user.

The file is parsed like the Strava export's originals (``activity_files``) and stored as
source ``upload``, keyed by the file's SHA-256, so uploading the same file again updates
the same workout instead of adding another. The ``activities`` row is built from the
file itself: the FIT session's totals where there are some, otherwise values worked out
from the samples (duration, distance, elevation gain, heart rate, power). The training
analysis then runs, so the workout has its NP, TSS and zones straight away.

A workout already stored from another service (e.g. the same ride from a Strava export)
is not duplicated: the activity lists merge copies that start within a few minutes of
each other, and the import reports which ones it matched.
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import queries
from .activity_files import FileTooLarge, ParsedActivity, UnsupportedFile, gunzip, parse_bytes, supported
from .analysis import analyze
from .db import Store
from .util import InputError, parse_ts, utc_iso

log = logging.getLogger(__name__)

SOURCE = "upload"
MAX_UPLOAD_BYTES = 50_000_000  # a long ride's FIT file is a few MB; a GPX with every extension maybe 20
MAX_DECOMPRESSED_BYTES = 60_000_000
MAX_SPAN_S = 48 * 3600  # longer: a device clock that jumped (and a 1 Hz analysis of years)
ELEVATION_HYSTERESIS_M = 2.0  # GPS/barometric noise below this is not climbing (closest to Strava's totals)
GPS_MIN_STEP_M = 3.0  # jitter while slow or standing: a point under this from the last counted one is noise
_EARTH_RADIUS_M = 6_371_000.0

# FIT session sport / GPX <type> / TCX Sport -> the activity type stored (Strava's vocabulary,
# which sport_family and the UI already understand).
_TYPES = {
    "cycling": "Ride",
    "biking": "Ride",
    "e_biking": "EBikeRide",
    "running": "Run",
    "walking": "Walk",
    "hiking": "Hike",
    "swimming": "Swim",
    "rowing": "Rowing",
    "training": "Workout",
    "multisport": "Multisport",
    "generic": "Workout",
}
_LABELS = {"cycling": "Ride", "running": "Run", "walking": "Walk", "hiking": "Hike", "swimming": "Swim"}


def _activity_type(parsed: ParsedActivity) -> str:
    session = parsed.meta.get("session") or {}
    sport = str(session.get("sport") or parsed.meta.get("type") or "").strip().lower()
    if session.get("sub_sport") in ("virtual_activity", "indoor_cycling") and sport == "cycling":
        return "VirtualRide"
    if sport in _TYPES:
        return _TYPES[sport]
    family = queries.sport_family(sport) if sport and not sport.isdigit() else "other"
    return _LABELS.get(family, "Workout")


def _default_name(local_start: datetime | None, activity_type: str) -> str:
    """Strava-style "Morning Ride", "Evening Run"."""
    label = _LABELS.get(queries.sport_family(activity_type), "Workout")
    if activity_type == "VirtualRide":
        label = "Virtual Ride"
    if local_start is None:
        return label
    hour = local_start.hour
    part = "Morning" if 5 <= hour < 12 else "Afternoon" if 12 <= hour < 17 else "Evening" if 17 <= hour < 21 else "Night"
    return f"{part} {label}"


def _values(streams: dict[str, list[Any]], channel: str) -> list[float]:
    return [v for v in streams.get(channel, ()) if isinstance(v, (int, float)) and math.isfinite(v)]


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _distance_m(streams: dict[str, list[Any]]) -> float | None:
    recorded = _values(streams, "distance")
    if recorded:
        return max(recorded)
    lat, lng = streams.get("lat", []), streams.get("lng", [])
    total, last = 0.0, None
    for y, x in zip(lat, lng, strict=False):
        if not isinstance(y, (int, float)) or not isinstance(x, (int, float)) or (x == 0 and y == 0):
            continue
        if last is None:
            last = (y, x)
            continue
        step = _haversine_m(last[0], last[1], y, x)
        if step >= GPS_MIN_STEP_M:
            total += step
            last = (y, x)
    return total if last is not None and total > 0 else None


def _elevation_gain_m(streams: dict[str, list[Any]]) -> float | None:
    altitude = _values(streams, "altitude")
    if len(altitude) < 2:
        return None
    gain, reference = 0.0, altitude[0]
    for value in altitude[1:]:
        if value - reference >= ELEVATION_HYSTERESIS_M:
            gain += value - reference
            reference = value
        elif reference - value >= ELEVATION_HYSTERESIS_M:
            reference = value  # a real descent: climbing is measured from its bottom
    return round(gain, 1)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def summarize(parsed: ParsedActivity) -> dict[str, Any]:
    """Activity-level totals: the FIT session's when present, else from the samples."""
    session = parsed.meta.get("session") or {}
    streams = parsed.streams
    times = _values(streams, "time")

    def pick(key: str, fallback: Any) -> Any:
        value = session.get(key)
        return float(value) if isinstance(value, (int, float)) and value > 0 else fallback

    hr = [v for v in _values(streams, "heart_rate") if 30 <= v <= 220]
    power = _values(streams, "power")
    return {
        "duration_s": pick("total_elapsed_time", times[-1] if times else None),
        "moving_s": pick("total_timer_time", None),
        "distance_m": pick("total_distance", _distance_m(streams)),
        "elevation_gain_m": pick("total_ascent", _elevation_gain_m(streams)),
        "avg_hr": pick("avg_heart_rate", _mean(hr)),
        "max_hr": pick("max_heart_rate", max(hr) if hr else None),
        # From the samples, as Strava and the analysis compute it (head units' own averages differ).
        "avg_power_w": _mean(power) if any(p > 0 for p in power) else pick("avg_power", None),
        "calories": pick("total_calories", None),
    }


def _local_date(start: datetime, offset_s: int | None, timezone: str | None) -> tuple[str, datetime]:
    if offset_s is not None:
        local = start + timedelta(seconds=offset_s)
    elif timezone:
        try:
            local = start.astimezone(ZoneInfo(timezone))
        except (ZoneInfoNotFoundError, ValueError):
            raise InputError(f"unknown timezone {timezone!r} (use an IANA name like America/Los_Angeles)") from None
    else:
        local = start
    return local.date().isoformat(), local


def import_activity_file(
    store: Store,
    filename: str,
    data: bytes,
    *,
    timezone: str | None = None,
    name: str | None = None,
    activity_type: str | None = None,
) -> dict[str, Any]:
    """Parse, store and analyse one uploaded file. Raises ``InputError`` for anything the user
    should fix (wrong type, unreadable or empty file, too large).

    ``timezone`` (IANA) sets the local date when the file has no local clock (GPX, TCX, many
    FIT files from apps); ``name`` and ``activity_type`` override what the file says."""
    filename = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not filename:
        raise InputError("the file needs a name ending in .fit, .gpx or .tcx (optionally .gz)")
    if not supported(filename) or filename.lower().removesuffix(".gz").endswith(".json"):
        raise InputError(f"{filename}: only .fit, .gpx and .tcx files (optionally .gz) can be imported")
    if not data:
        raise InputError(f"{filename} is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise InputError(f"{filename} is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB")
    try:
        # Unzipped first, so the same recording identifies the same way gzipped or not.
        contents = gunzip(data, MAX_DECOMPRESSED_BYTES) if filename.lower().endswith(".gz") else data
        parsed = parse_bytes(filename.removesuffix(".gz").removesuffix(".GZ"), contents)
    except (UnsupportedFile, FileTooLarge) as exc:
        raise InputError(f"{filename}: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - a corrupt or foreign file: tell the user, not a 500
        raise InputError(f"could not read {filename}: it does not look like a valid activity file") from exc
    if parsed.start_time is None or parsed.sample_count < 2:
        raise InputError(f"{filename} has no recorded samples (a course or route file rather than a workout?)")
    times = [t for t in parsed.streams.get("time", ()) if isinstance(t, (int, float))]
    if times and times[-1] - times[0] > MAX_SPAN_S:
        raise InputError(
            f"{filename} spans {round((times[-1] - times[0]) / 86400)} days: the device's clock probably jumped "
            f"(workouts up to {MAX_SPAN_S // 3600} h can be imported)"
        )

    digest = hashlib.sha256(contents).hexdigest()
    start_iso = utc_iso(parsed.start_time)
    # The same workout uploaded before in another form (a GPX of the same ride, the file gzipped):
    # replace that one rather than add a second copy that every total would count again.
    previous = _upload_of_the_same_workout(store, start_iso)
    source_id = previous["source_id"] if previous else digest[:24]
    local_date, local_start = _local_date(parsed.start_time, parsed.local_offset_s, timezone)
    stored_type = (activity_type or "").strip() or (previous["activity_type"] if previous and previous["kept_type"] else None)
    stored_type = stored_type or _activity_type(parsed)
    summary = summarize(parsed)
    chosen_name = (name or "").strip()[:200] or (previous["name"] if previous and previous["kept_name"] else None)
    row = {
        "source": SOURCE,
        "source_id": source_id,
        "start_time": start_iso,
        "local_date": local_date,
        "activity_type": stored_type,
        "name": chosen_name or parsed.meta.get("name") or _default_name(local_start, stored_type),
        **summary,
        "raw": {"file": filename, "bytes": len(data), "sha256": digest, "format": parsed.meta.get("format"),
                "session": parsed.meta.get("session"), "local_offset_s": parsed.local_offset_s, "timezone": timezone,
                # A name or sport set by the user (kept when the workout is imported again without one).
                "name_given": bool(chosen_name), "type_given": bool((activity_type or "").strip())
                or bool(previous and previous["kept_type"])},  # fmt: skip
    }
    meta = {**parsed.meta, "file": filename, "local_offset_s": parsed.local_offset_s}
    store.upsert_activities([row])
    try:
        store.upsert_activity_streams([{"source": SOURCE, "source_id": source_id, "streams": parsed.streams, "meta": meta}])
    except Exception:
        # Never leave a workout without its samples behind (a new one; a re-import keeps the old samples).
        if previous is None:
            with store.conn:
                store.conn.execute("DELETE FROM activities WHERE source = ? AND source_id = ?", (SOURCE, source_id))
        raise
    try:
        analysis: dict[str, Any] | None = analyze(store)
        analysis_error = None
    except Exception as exc:  # noqa: BLE001 - stored; the next analysis (any import or sync) retries
        log.exception("import: analysing %s failed", filename)
        analysis, analysis_error = None, f"stored, but the analysis failed ({type(exc).__name__}); it is retried next time"
    metrics = store.conn.execute(
        "SELECT timer_s FROM activity_metrics WHERE source = ? AND source_id = ?", (SOURCE, source_id)
    ).fetchone()
    if summary["moving_s"] is None and metrics and metrics["timer_s"]:
        # GPX and TCX carry no timer: moving time is what the analysis kept (pauses left out),
        # so every screen shows the same hours.
        with store.conn:
            store.conn.execute("UPDATE activities SET moving_s = ? WHERE source = ? AND source_id = ?",
                               (metrics["timer_s"], SOURCE, source_id))  # fmt: skip
        summary["moving_s"] = metrics["timer_s"]
    existed = previous is not None

    duplicates = _same_workout_elsewhere(store, start_iso)
    metrics = store.conn.execute(
        "SELECT tss, tss_method, intensity_factor, normalized_power_w FROM activity_metrics "
        "WHERE source = ? AND source_id = ?",
        (SOURCE, source_id),
    ).fetchone()
    return {
        "created": not existed,
        "activity": {
            "source": SOURCE,
            "source_id": source_id,
            "name": row["name"],
            "local_date": local_date,
            "start_time": start_iso,
            "activity_type": stored_type,
            "sport": queries.sport_family(stored_type),
            "duration_s": summary["duration_s"],
            "moving_s": summary["moving_s"],
            "distance_m": summary["distance_m"],
            "samples": parsed.sample_count,
            "channels": sorted(parsed.streams),
            "tss": metrics["tss"] if metrics else None,
            "tss_method": metrics["tss_method"] if metrics else None,
            "intensity_factor": metrics["intensity_factor"] if metrics else None,
            "normalized_power_w": metrics["normalized_power_w"] if metrics else None,
        },
        # The same workout already stored from another service; the lists show one of them.
        "also_stored_as": duplicates,
        # The earlier upload of this workout (another file of it) that this one replaced.
        "replaced_file": previous["file"] if previous and previous["file"] != filename else None,
        "analysis": analysis,
        "analysis_error": analysis_error,
    }


def _upload_of_the_same_workout(store: Store, start_iso: str) -> dict[str, Any] | None:
    """An earlier upload starting within the de-duplication window of this one, if any."""
    start = parse_ts(start_iso)
    assert start is not None
    window = timedelta(seconds=queries.DEDUPE_WINDOW_S)
    best = None
    for r in store.conn.execute(
        "SELECT source_id, start_time, name, activity_type, json_extract(raw, '$.file') AS file, "
        "json_extract(raw, '$.name_given') AS name_given, json_extract(raw, '$.type_given') AS type_given "
        "FROM activities WHERE source = ? AND local_date BETWEEN ? AND ?",
        (SOURCE, (start - timedelta(days=1)).date().isoformat(), (start + timedelta(days=1)).date().isoformat()),
    ):
        ts = parse_ts(r["start_time"])
        if ts is not None and abs(ts - start) <= window:
            gap = abs(ts - start)
            if best is None or gap < best[0]:
                best = (gap, r)
    if best is None:
        return None
    r = best[1]
    return {"source_id": r["source_id"], "name": r["name"], "activity_type": r["activity_type"], "file": r["file"],
            "kept_name": bool(r["name_given"]), "kept_type": bool(r["type_given"])}  # fmt: skip


def _same_workout_elsewhere(store: Store, start_iso: str) -> list[dict[str, Any]]:
    start = parse_ts(start_iso)
    assert start is not None
    window = timedelta(seconds=queries.DEDUPE_WINDOW_S)
    found = []
    for r in store.conn.execute(
        "SELECT source, source_id, start_time, name FROM activities WHERE source != ? AND local_date BETWEEN ? AND ?",
        (SOURCE, (start - timedelta(days=1)).date().isoformat(), (start + timedelta(days=1)).date().isoformat()),
    ):
        ts = parse_ts(r["start_time"])
        if ts is not None and abs(ts - start) <= window:
            found.append({"source": r["source"], "source_id": r["source_id"], "name": r["name"]})
    return found
