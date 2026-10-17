"""Import body weight from a CSV of dated weigh-ins (a spreadsheet log, a scale app's export).

The date column is the first whose header mentions "date" (else the first column) and the weight
column the first other one that mentions "weight" (else the second). Dates may be ISO
(2018-06-26), long ("Tuesday, June 26, 2018" or "June 26, 2018") or US (06/26/2018). The unit
comes from the header ("kg", "lb", "lbs", "pounds") or ``unit``; when neither says, the import
is refused rather than guessed. Blank weights are skipped; weights outside ``RANGE_KG`` are
refused. Each weigh-in is stored as ``weight_kg`` in ``body_measurements`` at noon UTC of its
date (a day's value, so that it stays on that date in any time zone), under ``SOURCE``:
importing the file again updates those days rather than adding copies.
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
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _header_unit(header: str) -> str | None:
    words = re.findall(r"[a-z]+", header.lower())
    return next((w for w in words if w in _UNITS), None)


def parse_weight_csv(text: str, unit: str | None = None) -> dict[str, Any]:
    """``{rows: [{date, kg}], skipped, unit, columns}`` from the CSV text; raises InputError when
    the columns or the unit cannot be told, or a value is not a weight."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise InputError("the CSV needs a header row with a date column and a weight column")
    lowered = [h.lower() for h in header]
    date_col = next((i for i, h in enumerate(lowered) if "date" in h), 0)
    weight_col = next((i for i, h in enumerate(lowered) if "weight" in h and i != date_col), 1 if date_col != 1 else 0)
    used = (unit or _header_unit(header[weight_col]) or "").lower()
    if used not in _UNITS:
        raise InputError(
            f"cannot tell the unit of {header[weight_col]!r}: pass --unit kg or --unit lb"
            if not unit
            else f"unknown unit {unit!r}: use kg or lb"
        )
    factor = _UNITS[used]
    low, high = RANGE_KG
    rows, skipped = [], 0
    for n, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue
        raw_date = record[date_col].strip() if date_col < len(record) else ""
        raw_weight = record[weight_col].strip() if weight_col < len(record) else ""
        day = _parse_date(raw_date)
        if day is None:
            raise InputError(f"line {n}: cannot read the date {raw_date!r}")
        if not raw_weight:
            skipped += 1  # a day without a weigh-in
            continue
        try:
            kg = float(raw_weight.replace(",", "")) * factor
        except ValueError:
            raise InputError(f"line {n}: {raw_weight!r} is not a number") from None
        if not low <= kg <= high:
            raise InputError(f"line {n}: {raw_weight} {used} is not a body weight (wrong unit?)")
        rows.append({"date": day.isoformat(), "kg": round(kg, 2)})
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
