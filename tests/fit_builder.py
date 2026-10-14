"""A minimal FIT file writer for tests (definition + data messages, CRCs).

Values are raw FIT units: timestamps are seconds since 1989-12-31 UTC, speed
is mm/s, distance cm, altitude (m + 500) * 5, positions semicircles.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

ENUM, SINT8, UINT8, UINT16, SINT32, UINT32 = 0x00, 0x01, 0x02, 0x84, 0x85, 0x86
_FORMATS = {ENUM: "B", SINT8: "b", UINT8: "B", UINT16: "H", SINT32: "i", UINT32: "I"}

# global message number -> {field name: (field number, base type)}
MESSAGES = {
    "file_id": (0, {"type": (0, ENUM), "manufacturer": (1, UINT16), "product": (2, UINT16), "time_created": (4, UINT32)}),
    "record": (
        20,
        {
            "timestamp": (253, UINT32),
            "position_lat": (0, SINT32),
            "position_long": (1, SINT32),
            "altitude": (2, UINT16),
            "heart_rate": (3, UINT8),
            "cadence": (4, UINT8),
            "distance": (5, UINT32),
            "speed": (6, UINT16),
            "power": (7, UINT16),
            "temperature": (13, SINT8),
        },
    ),
    "session": (
        18,
        {
            "timestamp": (253, UINT32),
            "start_time": (2, UINT32),
            "sport": (5, ENUM),
            "total_elapsed_time": (7, UINT32),
            "normalized_power": (34, UINT16),
            "training_stress_score": (35, UINT16),
            "intensity_factor": (36, UINT16),
            "threshold_power": (45, UINT16),
        },
    ),
    "lap": (19, {"timestamp": (253, UINT32), "start_time": (2, UINT32), "avg_power": (19, UINT16)}),
    "activity": (34, {"timestamp": (253, UINT32), "num_sessions": (1, UINT16), "local_timestamp": (5, UINT32)}),
}

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ _CRC_TABLE[nibble]
    return crc


def fit_time(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


def build_fit(messages: list[tuple[str, dict[str, int]]]) -> bytes:
    """``[(message name, {field: raw value}), ...]`` -> FIT file bytes."""
    body = bytearray()
    defined: dict[int, tuple[str, tuple[str, ...]]] = {}
    for name, values in messages:
        global_num, fields = MESSAGES[name]
        names = tuple(sorted(values))
        local = global_num % 16
        if defined.get(local) != (name, names):
            body += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(names))
            for field in names:
                number, base = fields[field]
                body += struct.pack("<BBB", number, struct.calcsize(_FORMATS[base]), base)
            defined[local] = (name, names)
        body += struct.pack("<B", local)
        for field in names:
            body += struct.pack("<" + _FORMATS[fields[field][1]], values[field])
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", crc16(header))
    data = header + bytes(body)
    return data + struct.pack("<H", crc16(data))


def ride(
    start: datetime,
    watts: list[int],
    *,
    heart_rate: int | None = 140,
    local_offset_s: int | None = -7 * 3600,
    threshold_power: int | None = 250,
) -> bytes:
    """A 1 Hz ride with the given power samples, plus session, lap and activity messages."""
    t0 = fit_time(start)
    msgs: list[tuple[str, dict[str, int]]] = [("file_id", {"type": 4, "manufacturer": 32, "product": 31, "time_created": t0})]
    for i, w in enumerate(watts):
        record = {
            "timestamp": t0 + i,
            "power": w,
            "cadence": 90,
            "speed": 8000,
            "distance": i * 800,
            "altitude": (100 + 500) * 5,
            "position_lat": int(37.4 / (180 / 2**31)),
            "position_long": int(-122.0 / (180 / 2**31)),
            "temperature": 20,
        }
        if heart_rate is not None:
            record["heart_rate"] = heart_rate
        msgs.append(("record", record))
    end = t0 + len(watts)
    msgs.append(("lap", {"timestamp": end, "start_time": t0, "avg_power": sum(watts) // max(len(watts), 1)}))
    session = {"timestamp": end, "start_time": t0, "sport": 2, "total_elapsed_time": len(watts) * 1000}
    if threshold_power is not None:
        session["threshold_power"] = threshold_power
    msgs.append(("session", session))
    activity = {"timestamp": end, "num_sessions": 1}
    if local_offset_s is not None:
        activity["local_timestamp"] = end + local_offset_s
    msgs.append(("activity", activity))
    return build_fit(msgs)
