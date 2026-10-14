"""Strava bulk export importer ("Download or Delete Your Account" archive).

The archive has ``activities.csv`` (one row per activity) and an
``activities/`` folder with each activity's original file (``.fit.gz``,
``.gpx``, ``.gpx.gz``, ``.tcx.gz`` or a Strava app ``.json``). Activities are
stored as source ``strava`` under their Strava activity ID, the same key the
Strava API uses, and each original file is parsed into ``activity_streams``.

``activities.csv`` repeats some column names: the first ``Distance`` is in km
(or miles), the second in metres; the second ``Elapsed Time`` is the detailed
one. Times in ``Activity Date`` are UTC. The local date comes from the FIT
file's local timestamp when there is one, otherwise from ``timezone``.
"""

from __future__ import annotations

import csv
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..activity_files import parse_file
from ..db import Store
from ..util import to_float, utc_iso

log = logging.getLogger(__name__)

SOURCE = "strava"
STREAM = "export"
BATCH = 50
_DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"
_MAX_START_SKEW_S = 120


def read_activities_csv(path: Path) -> list[dict[str, str]]:
    """Rows keyed by header; a repeated header gets a suffix (``Distance #2``)."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or "Activity ID" not in header or "Activity Date" not in header:
            raise ValueError(f"{path} does not look like a Strava export activities.csv")
        seen: dict[str, int] = {}
        keys = []
        for name in header:
            seen[name] = seen.get(name, 0) + 1
            keys.append(name if seen[name] == 1 else f"{name} #{seen[name]}")
        return [dict(zip(keys, row, strict=False)) for row in reader if row]


def _number(row: Mapping[str, str], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is not None and value.strip():
            number = to_float(value.replace(",", ""))
            if number is not None:
                return number
    return None


def parse_activity_date(value: str) -> datetime:
    return datetime.strptime(value.strip(), _DATE_FORMAT).replace(tzinfo=UTC)


def map_csv_row(row: Mapping[str, str]) -> dict[str, Any] | None:
    """One ``activities.csv`` row -> ``activities`` row with a UTC local date
    (the caller replaces it with the real local date)."""
    activity_id = (row.get("Activity ID") or "").strip()
    if not activity_id:
        return None
    try:
        start = parse_activity_date(row.get("Activity Date") or "")
    except ValueError:
        return None
    distance_m = _number(row, "Distance #2")
    if distance_m is None:
        km = _number(row, "Distance")  # the summary column is km on metric accounts
        distance_m = km * 1000 if km is not None else None
    return {
        "source": SOURCE,
        "source_id": activity_id,
        "start_time": utc_iso(start),
        "local_date": start.date().isoformat(),
        "activity_type": row.get("Activity Type") or None,
        "name": row.get("Activity Name") or None,
        "duration_s": _number(row, "Elapsed Time #2", "Elapsed Time"),
        "moving_s": _number(row, "Moving Time"),
        "distance_m": distance_m,
        "elevation_gain_m": _number(row, "Elevation Gain"),
        "avg_hr": _number(row, "Average Heart Rate"),
        "max_hr": _number(row, "Max Heart Rate #2", "Max Heart Rate"),
        "avg_power_w": _number(row, "Average Watts"),
        "calories": _number(row, "Calories"),
        "raw": dict(row),
    }


def _parse_job(path: str) -> dict[str, Any]:
    """Runs in a worker process: parse one file into plain, picklable data."""
    try:
        parsed = parse_file(path)
    except Exception as exc:  # noqa: BLE001 - a broken file must not stop the import
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "start_time": parsed.start_time,
        "streams": parsed.streams,
        "meta": parsed.meta,
        "local_offset_s": parsed.local_offset_s,
    }


def _local_date(start: datetime, offset_s: int | None, tz: ZoneInfo | None) -> str:
    if offset_s is not None:
        return (start + timedelta(seconds=offset_s)).date().isoformat()
    if tz is not None:
        return start.astimezone(tz).date().isoformat()
    return start.date().isoformat()


def _parsed_in_order(paths: list[str], workers: int) -> Iterator[dict[str, Any]]:
    if workers <= 1 or len(paths) <= 1:
        yield from map(_parse_job, paths)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(_parse_job, paths, chunksize=4)


def import_strava_export(
    store: Store,
    path: Path | str,
    *,
    timezone: str | None = None,
    force: bool = False,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Import an unzipped Strava archive. Idempotent; files whose streams are
    already stored are not parsed again unless ``force`` is set.

    ``timezone`` (IANA name) sets the local date of activities whose file has no
    local timestamp (GPX, Strava JSON, some older FIT files); without it those
    use the UTC date.
    """
    root = Path(path)
    csv_path = root / "activities.csv"
    if not csv_path.is_file():
        raise ValueError(f"{root} has no activities.csv; point this at the unzipped Strava export folder")
    tz = None
    if timezone:
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {timezone!r} (use an IANA name like America/Los_Angeles)") from None

    rows, skipped = [], 0
    files: dict[str, Path] = {}
    for raw in read_activities_csv(csv_path):
        row = map_csv_row(raw)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
        filename = (raw.get("Filename") or "").strip()
        if filename:
            files[row["source_id"]] = root / filename

    # Activities whose streams are already stored keep the local offset found last time.
    known: dict[str, int | None] = {
        r[0]: r[1]
        for r in store.conn.execute(
            "SELECT source_id, json_extract(meta, '$.local_offset_s') FROM activity_streams WHERE source = ?",
            (SOURCE,),
        )
    }
    for row in rows:
        if row["source_id"] in known:
            start = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
            row["local_date"] = _local_date(start, known[row["source_id"]], tz)

    todo = [r for r in rows if r["source_id"] in files and (force or r["source_id"] not in known)]
    missing = [r["source_id"] for r in todo if not files[r["source_id"]].is_file()]
    todo = [r for r in todo if files[r["source_id"]].is_file()]
    todo_ids = {r["source_id"] for r in todo}
    no_file_rows = [r for r in rows if r["source_id"] not in todo_ids]
    imported = store.upsert_activities(no_file_rows)
    for activity_id in missing:
        log.warning("strava export: file for activity %s is missing: %s", activity_id, files[activity_id])

    parsed_count = failed = empty = 0
    workers = workers if workers is not None else min(8, os.cpu_count() or 1)
    batch_rows: list[dict[str, Any]] = []
    batch_streams: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal imported
        imported += store.upsert_activities(batch_rows)
        store.upsert_activity_streams(batch_streams)
        batch_rows.clear()
        batch_streams.clear()

    jobs = [str(files[r["source_id"]]) for r in todo]
    for done, (row, result) in enumerate(zip(todo, _parsed_in_order(jobs, workers), strict=True), start=1):
        start = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
        if "error" in result:
            failed += 1
            log.warning("strava export: could not parse %s: %s", files[row["source_id"]].name, result["error"])
            row["local_date"] = _local_date(start, None, tz)
        else:
            offset = result["local_offset_s"]
            file_start = result["start_time"]
            if file_start is not None and abs((file_start - start).total_seconds()) > _MAX_START_SKEW_S:
                log.warning(
                    "strava export: %s starts at %s but activities.csv says %s",
                    files[row["source_id"]].name, file_start, start,
                )
            row["local_date"] = _local_date(start, offset, tz)
            if result["streams"]:
                meta = {**result["meta"], "file": files[row["source_id"]].name, "local_offset_s": offset}
                batch_streams.append(
                    {"source": SOURCE, "source_id": row["source_id"], "streams": result["streams"], "meta": meta}
                )
                parsed_count += 1
            else:
                empty += 1
        batch_rows.append(row)
        if len(batch_rows) >= BATCH:
            flush()
        if progress:
            progress(done, len(todo))
    flush()

    store.record_sync(SOURCE, STREAM, rows=imported)
    return {
        "activities": imported,
        "streams_imported": parsed_count,
        "streams_already_stored": len(known) if not force else 0,
        "files_without_samples": empty,
        "files_failed": failed,
        "files_missing": len(missing),
        "rows_skipped": skipped,
    }
