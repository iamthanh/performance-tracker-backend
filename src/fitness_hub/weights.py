"""Import body weight from a CSV of dated weigh-ins (a spreadsheet log, a scale app's export).

The date column is the one headed "Date" (else the first whose header has the word, else one headed
just "Timestamp" or "Time", else the first column) and the weight column the one headed "Weight"
(else the first with the word that is not a goal, target, change, BMI or body composition, else the
second). Dates may be ISO, long ("Tuesday, June 26, 2018" or "June 26, 2018") or US (06/26/2018);
day/month/year dates and dates in the future are refused. A timestamp with a zone (a scale's export:
2020-12-21T16:55:25Z) counts for its date in the given time zone (this machine's by default): an
evening weigh-in in California is past midnight UTC but belongs to that evening. The unit comes from
the header ("kg", "lb", "lbs", "pounds", "grams") or ``unit`` (refused when they disagree); when
neither says, the import is refused rather than guessed. Blank weights are skipped and weights
outside ``RANGE_KG`` refused.

Each day is stored once, as ``weight_kg`` in ``body_measurements`` at noon UTC of its date (so that
it stays on that date in any time zone) under ``SOURCE``: the mean of its timestamped weigh-ins (their
times and devices in ``raw``), or its date-only value. Importing again, or a file with both kinds of
dates, updates those days rather than adding copies (days removed from a file stay).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, tzinfo
from typing import Any

from .db import Store
from .util import InputError

SOURCE = "weight_csv"
LB_KG = 0.45359237
RANGE_KG = (20.0, 300.0)
_DATE_FORMATS = ("%Y-%m-%d", "%A, %B %d, %Y", "%B %d, %Y", "%a, %b %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y")
_UNITS = {
    "kg": 1.0, "kgs": 1.0, "kilograms": 1.0, "lb": LB_KG, "lbs": LB_KG, "pounds": LB_KG,
    "g": 0.001, "grams": 0.001, "gram": 0.001,
}  # fmt: skip


def _parse_when(text: str, tz: tzinfo | None) -> tuple[date, str | None] | None:
    """The day of a date cell and, for a timestamp with a zone (a scale's export: 2020-12-21T16:55:25Z,
    ...T07:00:00+01:00), its instant in UTC; the day is then the date in ``tz`` (an evening weigh-in in
    California is past midnight UTC). None when it cannot be read. A time without a zone is ignored: it
    says nothing about the instant."""
    text = " ".join(text.split())
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}.*(Z|[+-]\d{2}:?\d{2})", text):
            try:
                instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            # Without a zone, this machine's, with the daylight saving time of that date.
            local = instant.astimezone(tz) if tz else instant.astimezone()
            return local.date(), instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return day, None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None


def _day_first(text: str) -> bool:
    """A d/m/y date (a day over 12 first), which this importer refuses to guess about."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/\d{2,4}", text.strip())
    return bool(m) and int(m.group(1)) > 12 >= int(m.group(2))


def _words(header: str) -> list[str]:
    return re.findall(r"[a-z]+", header.lower())


def _header_unit(header: str) -> str | None:
    """The unit a header names: "(kg)", "weight lbs", "weight grams". A bare "g" counts only as the whole
    bracketed unit or the last word ("weight (g)", "weight g"), not inside other text ("(e.g. morning)")."""
    bracketed = re.findall(r"\(([^)]*)\)", header.lower())
    for inside in bracketed:
        if inside.strip() in _UNITS:
            return inside.strip()
    words = _words(header)
    found = next((w for w in words if w in _UNITS and w != "g"), None)
    return found or ("g" if words and words[-1] == "g" else None)


_NOT_WEIGHT = {"goal", "target", "change", "bmi", "lean", "muscle", "bone", "water", "fat"}


def _columns(header: list[str]) -> tuple[int, int]:
    """The date and weight columns: a header that is just the word wins over one that contains it."""
    words = [_words(h) for h in header]
    bare = [[w for w in ws if w not in _UNITS] for ws in words]

    def pick(word: str, exclude: set[str], skip: int | None) -> int | None:
        candidates = [i for i, ws in enumerate(words) if word in ws and not exclude & set(ws) and i != skip]
        exact = [i for i in candidates if bare[i] == [word]]
        return (exact or candidates or [None])[0]

    exact = [next((i for i, b in enumerate(bare) if b == [w]), None) for w in ("timestamp", "time")]
    # "Date" (or a header with the word), else a column headed just "Timestamp" or "Time" (not "Time of day").
    date_col = next((c for c in (pick("date", set(), None), *exact) if c is not None), 0)
    weight_col = pick("weight", _NOT_WEIGHT, date_col)
    if weight_col is None:
        weight_col = 1 if date_col != 1 else 0
    return date_col, weight_col


def parse_weight_csv(
    text: str, unit: str | None = None, today: date | None = None, tz: tzinfo | None = None
) -> dict[str, Any]:
    """``{rows: [{date, kg, count, times, devices}], skipped, unit, columns}`` from the CSV text, one row per
    day, oldest first: the mean of its timestamped weigh-ins (``times``, UTC), or its date-only value (the
    last of several). Raises InputError when the columns, the unit or a date cannot be told, or a value is
    not a weight."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise InputError("the CSV needs a header row with a date column and a weight column")
    date_col, weight_col = _columns(header)
    stated = _header_unit(header[weight_col])
    if unit and stated and _UNITS[unit.lower()] != _UNITS[stated]:
        raise InputError(f"--unit {unit} contradicts the header {header[weight_col]!r}")
    used = (unit or stated or "").lower()
    if used not in _UNITS:
        raise InputError(
            f"cannot tell the unit of {header[weight_col]!r}: pass --unit kg, lb or g"
            if not unit
            else f"unknown unit {unit!r}: use kg, lb or g"
        )
    factor = _UNITS[used]
    low, high = RANGE_KG
    latest = (today or date.today()).isoformat()
    by_day: dict[str, dict[str, Any]] = {}
    skipped = 0
    source_col = next(
        (i for i, h in enumerate(header)
         if i not in (date_col, weight_col) and {"source", "device"} & set(_words(h))),
        None,
    )  # fmt: skip
    for n, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue
        raw_date = record[date_col].strip() if date_col < len(record) else ""
        raw_weight = record[weight_col].strip() if weight_col < len(record) else ""
        if _day_first(raw_date):
            raise InputError(
                f"line {n}: {raw_date!r} is day/month/year; this importer reads 06/26/2018 as month/day, "
                "so convert the dates to 2018-06-26 first"
            )
        when = _parse_when(raw_date, tz)
        if when is None:
            raise InputError(f"line {n}: cannot read the date {raw_date!r}")
        day, ts = when
        if day.isoformat() > latest:
            raise InputError(f"line {n}: {raw_date!r} is in the future")
        if not raw_weight:
            skipped += 1  # a day without a weigh-in
            continue
        try:
            kg = float(raw_weight) * factor
        except ValueError:
            hint = " (use a dot for decimals)" if "," in raw_weight else ""
            raise InputError(f"line {n}: {raw_weight!r} is not a number{hint}") from None
        if not low <= kg <= high:
            raise InputError(f"line {n}: {raw_weight} {used} is not a body weight (wrong unit?)")
        device = record[source_col].strip() if source_col is not None and source_col < len(record) else ""
        entry = by_day.setdefault(day.isoformat(), {"weights": [], "times": [], "devices": set()})
        if ts is None:
            entry["weights"], entry["times"] = [], []  # a date-only value is the day's: the last one wins
        entry["weights"].append(kg)
        if ts:
            entry["times"].append(ts)
        if device:
            entry["devices"].add(device)
    rows = [
        {"date": d, "kg": round(sum(e["weights"]) / len(e["weights"]), 2), "count": len(e["weights"]),
         "times": e["times"], "devices": sorted(e["devices"])}
        for d, e in sorted(by_day.items())
    ]  # fmt: skip
    return {"rows": rows, "skipped": skipped, "unit": used, "columns": (header[date_col], header[weight_col])}


def import_weight_csv(store: Store, text: str, unit: str | None = None, tz: tzinfo | None = None) -> dict[str, Any]:
    """Store the CSV's days (see the module docstring); ``{imported, weigh_ins, skipped, unit, first, last}``."""
    parsed = parse_weight_csv(text, unit, tz=tz)
    rows = parsed["rows"]
    if rows:
        store.upsert_body_measurements(
            {"source": SOURCE, "ts": f"{r['date']}T12:00:00Z", "metric": "weight_kg", "value": r["kg"],
             "raw": {"date": r["date"], "unit": parsed["unit"], "weigh_ins": r["count"], "times": r["times"],
                     "devices": r["devices"]}}
            for r in rows
        )  # fmt: skip
    return {
        "imported": len(rows),
        "weigh_ins": sum(r["count"] for r in rows),
        "skipped": parsed["skipped"],
        "unit": parsed["unit"],
        "first": rows[0]["date"] if rows else None,
        "last": rows[-1]["date"] if rows else None,
    }
