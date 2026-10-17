"""Import body weight from a CSV of dated weigh-ins (a spreadsheet log, a scale app's export).

The date column is the one headed "Date" (else the first whose header has the word, else the
first column) and the weight column the one headed "Weight" (else the first with the word that is
not a goal, target, change, BMI or body composition, else the second). Dates may be ISO
(2018-06-26, with or without a time), long ("Tuesday, June 26, 2018" or "June 26, 2018") or US
(06/26/2018); day/month/year dates and dates in the future are refused. The unit comes from the
header ("kg", "lb", "lbs", "pounds") or ``unit`` (refused when they disagree); when neither says,
the import is refused rather than guessed. Blank weights are skipped, weights outside
``RANGE_KG`` refused, and several weigh-ins on one date keep the last. Each is stored as
``weight_kg`` in ``body_measurements`` at noon UTC of its date (a day's value, so that it stays on
that date in any time zone), under ``SOURCE``: importing the file again updates those days rather
than adding copies (days removed from the file stay).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from typing import Any

from .db import Store
from .util import InputError

SOURCE = "weight_csv"
LB_KG = 0.45359237
RANGE_KG = (20.0, 300.0)
_DATE_FORMATS = ("%Y-%m-%d", "%A, %B %d, %Y", "%B %d, %Y", "%a, %b %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y")
_UNITS = {"kg": 1.0, "kgs": 1.0, "kilograms": 1.0, "lb": LB_KG, "lbs": LB_KG, "pounds": LB_KG}


def _parse_date(text: str) -> date | None:
    text = " ".join(text.split())
    if re.match(r"\d{4}-\d{2}-\d{2}", text):  # ISO, with or without a time (scale exports)
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
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
    return next((w for w in _words(header) if w in _UNITS), None)


_NOT_WEIGHT = {"goal", "target", "change", "bmi", "lean", "muscle", "bone", "water", "fat"}


def _columns(header: list[str]) -> tuple[int, int]:
    """The date and weight columns: a header that is just the word wins over one that contains it."""
    words = [_words(h) for h in header]
    bare = [[w for w in ws if w not in _UNITS] for ws in words]

    def pick(word: str, exclude: set[str], skip: int | None) -> int | None:
        candidates = [i for i, ws in enumerate(words) if word in ws and not exclude & set(ws) and i != skip]
        exact = [i for i in candidates if bare[i] == [word]]
        return (exact or candidates or [None])[0]

    date_col = pick("date", set(), None)
    date_col = 0 if date_col is None else date_col
    weight_col = pick("weight", _NOT_WEIGHT, date_col)
    if weight_col is None:
        weight_col = 1 if date_col != 1 else 0
    return date_col, weight_col


def parse_weight_csv(text: str, unit: str | None = None, today: date | None = None) -> dict[str, Any]:
    """``{rows: [{date, kg}], skipped, unit, columns}`` from the CSV text, one row per date (the
    last of several), oldest first; raises InputError when the columns, the unit or a date cannot be
    told, or a value is not a weight."""
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
            f"cannot tell the unit of {header[weight_col]!r}: pass --unit kg or --unit lb"
            if not unit
            else f"unknown unit {unit!r}: use kg or lb"
        )
    factor = _UNITS[used]
    low, high = RANGE_KG
    latest = (today or date.today()).isoformat()
    by_date: dict[str, float] = {}
    skipped = 0
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
        day = _parse_date(raw_date)
        if day is None:
            raise InputError(f"line {n}: cannot read the date {raw_date!r}")
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
        by_date[day.isoformat()] = round(kg, 2)
    rows = [{"date": d, "kg": kg} for d, kg in sorted(by_date.items())]
    return {"rows": rows, "skipped": skipped, "unit": used, "columns": (header[date_col], header[weight_col])}


def import_weight_csv(store: Store, text: str, unit: str | None = None) -> dict[str, Any]:
    """Store the CSV's weigh-ins (see the module docstring); ``{imported, skipped, unit, first, last}``."""
    parsed = parse_weight_csv(text, unit)
    rows = parsed["rows"]
    if rows:
        store.upsert_body_measurements(
            {"source": SOURCE, "ts": f"{r['date']}T12:00:00Z", "metric": "weight_kg", "value": r["kg"],
             "raw": {"date": r["date"], "unit": parsed["unit"]}}
            for r in rows
        )  # fmt: skip
    return {
        "imported": len(rows),
        "skipped": parsed["skipped"],
        "unit": parsed["unit"],
        "first": rows[0]["date"] if rows else None,
        "last": rows[-1]["date"] if rows else None,
    }
