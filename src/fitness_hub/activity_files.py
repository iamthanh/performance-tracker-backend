"""Parse recorded activity files (FIT, GPX, TCX, Strava app JSON) into per-sample streams.

Each parser returns a ``ParsedActivity``: aligned channels (index ``i`` is the
same moment in every channel), a ``meta`` dict of file-level details (device,
session totals, laps, device thresholds) and, when the file says so, the
offset of the athlete's local clock from UTC.

Channels (missing ones are left out): ``time`` (seconds since the first
sample), ``power`` (W), ``heart_rate`` (bpm), ``cadence`` (rpm), ``speed``
(m/s), ``distance`` (m), ``altitude`` (m), ``lat``/``lng`` (degrees) and
``temperature`` (°C).
"""

from __future__ import annotations

import gzip
import io
import json
import math
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .util import parse_ts

CHANNELS = ("time", "power", "heart_rate", "cadence", "speed", "distance", "altitude", "lat", "lng", "temperature")
_ROUND = {"speed": 3, "distance": 1, "altitude": 1, "lat": 6, "lng": 6}
_SEMICIRCLE_DEG = 180.0 / 2**31


@dataclass
class ParsedActivity:
    start_time: datetime | None
    streams: dict[str, list[Any]]
    meta: dict[str, Any] = field(default_factory=dict)
    local_offset_s: int | None = None

    @property
    def sample_count(self) -> int:
        return len(self.streams.get("time", ()))


class UnsupportedFile(ValueError):
    """The file type is not one this module can parse."""


SUPPORTED_SUFFIXES = (".fit", ".gpx", ".tcx", ".json")


class FileTooLarge(ValueError):
    """A (decompressed) file over the size limit."""


def parse_file(path: Path | str) -> ParsedActivity:
    """Parse ``.fit``, ``.gpx``, ``.tcx`` or Strava ``.json``, optionally gzipped."""
    path = Path(path)
    if not supported(path.name):
        raise UnsupportedFile(f"unsupported activity file type: {path.name}")
    return parse_bytes(path.name, path.read_bytes())


def supported(filename: str) -> bool:
    name = filename.lower()
    return (name[:-3] if name.endswith(".gz") else name).endswith(SUPPORTED_SUFFIXES)


def gunzip(data: bytes, max_bytes: int | None) -> bytes:
    """Decompress gzip data, refusing to inflate past ``max_bytes`` (a "zip bomb") and never
    allocating more than the output so far."""
    if max_bytes is None:
        return gzip.decompress(data)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip framing
    out = bytearray()
    chunk = data
    while chunk:
        out += inflater.decompress(chunk, 1 << 20)
        if len(out) > max_bytes:
            raise FileTooLarge(f"the file is larger than {max_bytes // 1_000_000} MB once decompressed")
        chunk = inflater.unconsumed_tail
    out += inflater.flush()
    if len(out) > max_bytes:
        raise FileTooLarge(f"the file is larger than {max_bytes // 1_000_000} MB once decompressed")
    if not inflater.eof:
        raise ValueError("the gzip data is truncated")
    return bytes(out)


def parse_bytes(filename: str, data: bytes, max_bytes: int | None = None) -> ParsedActivity:
    """Parse a file's contents by its name's type (see ``parse_file``). ``max_bytes`` caps the
    decompressed size of a gzipped file."""
    name = filename.lower()
    gz = name.endswith(".gz")
    stem = name[:-3] if gz else name
    if not stem.endswith(SUPPORTED_SUFFIXES):
        raise UnsupportedFile(f"unsupported activity file type: {filename}")
    if gz:
        data = gunzip(data, max_bytes)
    if stem.endswith(".fit"):
        return parse_fit(io.BytesIO(data))
    if stem.endswith(".gpx"):
        return parse_gpx(data)
    if stem.endswith(".tcx"):
        return parse_tcx(data)
    return parse_strava_json(json.loads(data))


def _build(samples: list[dict[str, Any]], start: datetime) -> dict[str, list[Any]]:
    """Rows of ``{channel: value}`` with a ``ts`` datetime -> aligned channels."""
    streams: dict[str, list[Any]] = {c: [] for c in CHANNELS}
    for s in samples:
        streams["time"].append(round((s["ts"] - start).total_seconds(), 3))
        for c in CHANNELS[1:]:
            value = s.get(c)
            if isinstance(value, float):
                if not math.isfinite(value):
                    value = None
                elif c in _ROUND:
                    value = round(value, _ROUND[c])
            streams[c].append(value)
    return {c: v for c, v in streams.items() if c == "time" or any(x is not None for x in v)}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


# -- FIT ------------------------------------------------------------------------------

_SESSION_FIELDS = (
    "sport",
    "sub_sport",
    "start_time",
    "total_elapsed_time",
    "total_timer_time",
    "total_distance",
    "total_ascent",
    "avg_power",
    "max_power",
    "normalized_power",
    "training_stress_score",
    "intensity_factor",
    "threshold_power",
    "total_work",
    "avg_heart_rate",
    "max_heart_rate",
    "avg_cadence",
    "total_calories",
    "time_in_hr_zone",
    "time_in_power_zone",
)
_LAP_FIELDS = (
    "start_time",
    "total_elapsed_time",
    "total_timer_time",
    "total_distance",
    "total_ascent",
    "avg_power",
    "max_power",
    "normalized_power",
    "avg_heart_rate",
    "max_heart_rate",
    "avg_cadence",
    "avg_speed",
    "lap_trigger",
)
_FIT_MESSAGES = {"record", "session", "lap", "activity", "file_id", "device_info", "zones_target", "user_profile"}
_THRESHOLD_FIELDS = ("functional_threshold_power", "threshold_heart_rate", "max_heart_rate")


def _combined_session(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """A multisport file (e.g. a triathlon): the legs' totals added up, sport "multisport"."""
    combined: dict[str, Any] = {"sport": "multisport", "start_time": sessions[0].get("start_time"),
                                "legs": [s.get("sport") for s in sessions]}  # fmt: skip
    for key in ("total_elapsed_time", "total_timer_time", "total_distance", "total_ascent", "total_calories"):
        values = [s[key] for s in sessions if isinstance(s.get(key), (int, float))]
        if values:
            combined[key] = sum(values)
    hr = [s["max_heart_rate"] for s in sessions if isinstance(s.get("max_heart_rate"), (int, float))]
    if hr:
        combined["max_heart_rate"] = max(hr)
    return combined


def _pick(values: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {n: values[n] for n in names if values.get(n) is not None}


def _fit_value(values: dict[str, Any], *names: str) -> Any:
    for n in names:
        if values.get(n) is not None:
            return values[n]
    return None


def _plausible_offset(seconds: float) -> int | None:
    """A real UTC offset (UTC-12..UTC+14, whole quarter hours), else ``None``.
    Some apps (Zwift) write a ``local_timestamp`` that is not a local time at all."""
    offset = round(seconds / 900) * 900
    if abs(seconds - offset) > 60 or not -12 * 3600 <= offset <= 14 * 3600:
        return None
    return offset


def parse_fit(fileobj: IO[bytes]) -> ParsedActivity:
    import fitdecode

    samples: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"format": "fit", "laps": []}
    local_offset = None
    with fitdecode.FitReader(fileobj, check_crc=fitdecode.CrcCheck.WARN) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            name = frame.name
            if name not in _FIT_MESSAGES:
                continue
            values = {f.name: f.value for f in frame.fields if f.value is not None}
            if name == "record":
                ts = _utc(values.get("timestamp"))
                if ts is None:
                    continue
                lat, lng = values.get("position_lat"), values.get("position_long")
                samples.append(
                    {
                        "ts": ts,
                        "power": values.get("power"),
                        "heart_rate": values.get("heart_rate"),
                        "cadence": values.get("cadence"),
                        "speed": _fit_value(values, "enhanced_speed", "speed"),
                        "distance": values.get("distance"),
                        "altitude": _fit_value(values, "enhanced_altitude", "altitude"),
                        "lat": lat * _SEMICIRCLE_DEG if isinstance(lat, int) else None,
                        "lng": lng * _SEMICIRCLE_DEG if isinstance(lng, int) else None,
                        "temperature": values.get("temperature"),
                    }
                )
            elif name == "session":
                meta.setdefault("sessions", []).append(_pick(values, _SESSION_FIELDS))
            elif name == "lap":
                meta["laps"].append(_pick(values, _LAP_FIELDS))
            elif name == "activity":
                ts, local = _utc(values.get("timestamp")), values.get("local_timestamp")
                if ts is not None and isinstance(local, datetime):
                    delta = local.replace(tzinfo=None) - ts.replace(tzinfo=None)
                    local_offset = _plausible_offset(delta.total_seconds())
            elif name == "file_id":
                meta["manufacturer"] = values.get("manufacturer")
                meta["product"] = values.get("product_name") or values.get("garmin_product") or values.get("product")
            elif name == "device_info" and values.get("device_index") == "creator" and values.get("product_name"):
                meta["device"] = values["product_name"]
            elif name in ("zones_target", "user_profile"):
                meta.setdefault("thresholds", {}).update(_pick(values, _THRESHOLD_FIELDS))
    if meta.get("sessions"):
        meta["session"] = meta["sessions"][0]  # most files have exactly one
        if len(meta["sessions"]) > 1:
            meta["session"] = _combined_session(meta["sessions"])
    samples.sort(key=lambda s: s["ts"])
    if not samples:
        return ParsedActivity(start_time=None, streams={}, meta=meta, local_offset_s=local_offset)
    start = samples[0]["ts"]
    return ParsedActivity(start_time=start, streams=_build(samples, start), meta=meta, local_offset_s=local_offset)


# -- GPX ------------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text is not None and text.strip() else None
    except ValueError:
        return None


def parse_gpx(data: bytes) -> ParsedActivity:
    """GPX track points, including Garmin/Strava ``hr``, ``cad``, ``power`` and ``atemp`` extensions."""
    root = ET.fromstring(data)
    samples = []
    for point in root.iter():
        if _local(point.tag) != "trkpt":
            continue
        sample: dict[str, Any] = {"lat": _num(point.get("lat")), "lng": _num(point.get("lon"))}
        for child in point.iter():
            tag = _local(child.tag)
            if tag == "time":
                sample["ts"] = parse_ts(child.text)
            elif tag == "ele":
                sample["altitude"] = _num(child.text)
            elif tag == "hr":
                sample["heart_rate"] = _num(child.text)
            elif tag == "cad":
                sample["cadence"] = _num(child.text)
            elif tag in ("power", "PowerInWatts"):
                sample["power"] = _num(child.text)
            elif tag == "atemp":
                sample["temperature"] = _num(child.text)
        if sample.get("ts") is not None:
            samples.append(sample)
    samples.sort(key=lambda s: s["ts"])
    meta: dict[str, Any] = {"format": "gpx"}
    track = next((e for e in root.iter() if _local(e.tag) == "trk"), None)
    if track is not None:
        for tag in ("name", "type"):
            text = _child_text(track, tag)
            if text and text.strip():
                meta[tag] = text.strip()[:200]
    if not samples:
        return ParsedActivity(start_time=None, streams={}, meta=meta)
    start = samples[0]["ts"]
    return ParsedActivity(start_time=start, streams=_build(samples, start), meta=meta)


# -- TCX ------------------------------------------------------------------------------


def _child_text(element: ET.Element, *path: str) -> str | None:
    """Text of the first descendant chain matching ``path`` (local names, any namespace)."""
    current: ET.Element | None = element
    for tag in path:
        current = next((c for c in current if _local(c.tag) == tag), None) if current is not None else None
    return current.text if current is not None else None


def parse_tcx(data: bytes) -> ParsedActivity:
    """Garmin Training Center XML trackpoints, including the ``TPX`` speed/watts extension."""
    root = ET.fromstring(data)
    samples = []
    for point in root.iter():
        if _local(point.tag) != "Trackpoint":
            continue
        ts = parse_ts(_child_text(point, "Time"))
        if ts is None:
            continue
        sample: dict[str, Any] = {
            "ts": ts,
            "lat": _num(_child_text(point, "Position", "LatitudeDegrees")),
            "lng": _num(_child_text(point, "Position", "LongitudeDegrees")),
            "altitude": _num(_child_text(point, "AltitudeMeters")),
            "distance": _num(_child_text(point, "DistanceMeters")),
            "heart_rate": _num(_child_text(point, "HeartRateBpm", "Value")),
            "cadence": _num(_child_text(point, "Cadence")),
        }
        for child in point.iter():
            tag = _local(child.tag)
            if tag == "Watts":
                sample["power"] = _num(child.text)
            elif tag == "Speed":
                sample["speed"] = _num(child.text)
        samples.append(sample)
    samples.sort(key=lambda s: s["ts"])
    meta: dict[str, Any] = {"format": "tcx"}
    activity = next((e for e in root.iter() if _local(e.tag) == "Activity"), None)
    if activity is not None and activity.get("Sport"):
        meta["type"] = activity.get("Sport")
    if not samples:
        return ParsedActivity(start_time=None, streams={}, meta=meta)
    start = samples[0]["ts"]
    return ParsedActivity(start_time=start, streams=_build(samples, start), meta=meta)


# -- Strava app JSON ------------------------------------------------------------------


def parse_strava_json(doc: dict[str, Any]) -> ParsedActivity:
    """Strava phone-app recordings: ``data[].fields`` / ``data[].values`` with an epoch ``time``."""
    meta = {"format": "strava_json"}
    samples = []
    for block in doc.get("data") or []:
        fields = block.get("fields") or []
        index = {name: i for i, name in enumerate(fields)}
        if "time" not in index:
            continue
        for row in block.get("values") or []:
            def get(name: str) -> Any:
                i = index.get(name)
                return row[i] if i is not None and i < len(row) else None

            try:
                ts = datetime.fromtimestamp(float(get("time")), UTC)
            except (TypeError, ValueError):
                continue
            latlng = get("latlng")
            speed = get("speed")
            samples.append(
                {
                    "ts": ts,
                    "lat": latlng[0] if isinstance(latlng, list) and len(latlng) == 2 else None,
                    "lng": latlng[1] if isinstance(latlng, list) and len(latlng) == 2 else None,
                    "altitude": get("elevation"),
                    "speed": speed if isinstance(speed, (int, float)) and speed >= 0 else None,
                    "distance": get("distance"),
                    "heart_rate": get("heartrate"),
                    "cadence": get("cadence"),
                    "power": get("watts"),
                }
            )
    samples.sort(key=lambda s: s["ts"])
    if not samples:
        return ParsedActivity(start_time=None, streams={}, meta=meta)
    start = samples[0]["ts"]
    return ParsedActivity(start_time=start, streams=_build(samples, start), meta=meta)
