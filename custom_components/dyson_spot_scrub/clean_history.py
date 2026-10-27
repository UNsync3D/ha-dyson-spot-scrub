"""Cleaning history: turn Dyson's /clean-maps records into plain dicts.

Pure functions with no Home Assistant imports, so they can be tested offline.
Field meanings come from MyDyson iOS captures (issue #26); anything missing
or malformed becomes None rather than raising, because the format is
undocumented and may change.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

# Per-room cleanStatus values seen in captures.
ROOM_COMPLETE = "CLEAN_COMPLETE"
ROOM_IN_PROGRESS = "CLEAN_IN_PROGRESS"   # also what a clean stopped early keeps
ROOM_CANT_CLEAN = "CANT_CLEAN"
ROOM_NOT_REQUESTED = "CLEAN_NOT_REQUESTED"

# Overall result of one clean (sensor states, translated in strings.json).
RESULT_COMPLETE = "complete"         # every requested room finished
RESULT_PARTIAL = "partial"           # some requested rooms finished
RESULT_STOPPED = "stopped"           # none finished; stopped or interrupted
RESULT_CANT_CLEAN = "cant_clean"     # every requested room was unreachable
RESULTS = [RESULT_COMPLETE, RESULT_PARTIAL, RESULT_STOPPED, RESULT_CANT_CLEAN]

# How many past cleans the sensor's "history" attribute carries.
HISTORY_ATTRIBUTE_LIMIT = 10


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _timestamp(value: Any) -> datetime | None:
    seconds = _number(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _room(zone: dict) -> dict:
    settings = zone.get("settings") if isinstance(zone.get("settings"), dict) else {}
    return {
        "id": None if zone.get("id") is None else str(zone["id"]),
        "name": _text(zone.get("name")),
        "type": _text(zone.get("type")),
        "status": _text(zone.get("cleanStatus")),
        "strategy": _text(settings.get("cleaningStrategy")),
        "clean_type": _text(settings.get("cleanType")),
        "water_level": _text(settings.get("waterLevel")),
        "mop_passes": _number(settings.get("mopPasses")),
    }


def clean_result(rooms: list[dict]) -> str | None:
    """Overall result from the rooms that were asked to be cleaned."""
    statuses = [r["status"] for r in rooms
                if r.get("status") and r["status"] != ROOM_NOT_REQUESTED]
    if not statuses:
        return None
    done = statuses.count(ROOM_COMPLETE)
    if done == len(statuses):
        return RESULT_COMPLETE
    if done:
        return RESULT_PARTIAL
    if all(s == ROOM_CANT_CLEAN for s in statuses):
        return RESULT_CANT_CLEAN
    return RESULT_STOPPED


def parse_record(raw: Any) -> dict | None:
    """One clean, or None when it lacks an ID or a valid start time.

    downloadUrl is deliberately not copied: it is a pre-signed S3 link that
    carries temporary AWS credentials.
    """
    if not isinstance(raw, dict):
        return None
    clean_id = _text(raw.get("cleanId"))
    start = _timestamp(raw.get("startTime"))
    if clean_id is None or start is None:
        return None
    end = _timestamp(raw.get("endTime"))
    if end is not None and end < start:
        end = None
    zones = raw.get("zones") if isinstance(raw.get("zones"), list) else []
    rooms = [_room(z) for z in zones if isinstance(z, dict)]
    battery_start = _number(raw.get("startBattery"))
    battery_end = _number(raw.get("endBattery"))
    battery_used = None
    if battery_start is not None and battery_end is not None and battery_start >= battery_end:
        battery_used = round(battery_start - battery_end, 1)
    duration = _number(raw.get("cleanDuration"))
    area = _number(raw.get("areaCleaned"))
    faults = raw.get("faults") if isinstance(raw.get("faults"), list) else []
    map_id = raw.get("persistentMapId")
    return {
        "clean_id": clean_id,
        "map_id": None if map_id in (None, "") else str(map_id),
        "start": start,
        "end": end,
        "duration_minutes": duration if duration is not None and duration >= 0 else None,
        "area_m2": area if area is not None and area >= 0 else None,
        "battery_start": battery_start,
        "battery_end": battery_end,
        "battery_used": battery_used,
        # Meaning unconfirmed: 0 and 1 both seen on manual cleans, none scheduled.
        "start_method": _number(raw.get("startMethod")),
        "spot_clean": raw.get("isSpotClean") is True,
        "fault_count": len(faults),
        "rooms": rooms,
        "result": clean_result(rooms),
    }


def parse_history(data: Any) -> list[dict]:
    """All valid cleans, newest first, one per cleanId.

    Accepts the list from get_clean_history() or the raw {"data": [...]} body.
    """
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        return []
    seen: set[str] = set()
    records = []
    for raw in data:
        record = parse_record(raw)
        if record is not None and record["clean_id"] not in seen:
            seen.add(record["clean_id"])
            records.append(record)
    records.sort(key=lambda r: r["start"], reverse=True)
    return records


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def requested_rooms(record: dict) -> list[str]:
    return [r["name"] for r in record["rooms"]
            if r.get("name") and r.get("status") not in (None, ROOM_NOT_REQUESTED)]


def record_attributes(record: dict) -> dict:
    """Attributes describing one clean (JSON-safe)."""
    return {
        "clean_id": record["clean_id"],
        "map_id": record["map_id"],
        "start": _iso(record["start"]),
        "end": _iso(record["end"]),
        "duration_minutes": record["duration_minutes"],
        "area_m2": record["area_m2"],
        "battery_start": record["battery_start"],
        "battery_end": record["battery_end"],
        "result": record["result"],
        "spot_clean": record["spot_clean"],
        "start_method": record["start_method"],
        "fault_count": record["fault_count"],
        "rooms": [dict(r) for r in record["rooms"]],
    }


def history_summary(records: list[dict], limit: int = HISTORY_ATTRIBUTE_LIMIT) -> list[dict]:
    """Compact list of recent cleans for a sensor attribute."""
    return [{
        "start": _iso(r["start"]),
        "duration_minutes": r["duration_minutes"],
        "area_m2": r["area_m2"],
        "battery_used": r["battery_used"],
        "result": r["result"],
        "rooms": requested_rooms(r),
    } for r in records[:limit]]
