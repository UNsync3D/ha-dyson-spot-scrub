"""Cleaning schedules kept on the robot (#67).

The MyDyson app keeps schedules ("orders") on the robot over JDM MQTT:

- ``service.add_order`` with the full order creates a schedule, and re-sending
  it with the same ``id`` changes it; switching one off and on is the same
  order with only ``enable`` changed.
- ``service.del_order`` ``{"id": ...}`` deletes one.
- ``service.get_order`` ``{}`` only returns a summary, ``order_data_lite``
  ``{total, enable, md5, timestamp}``; ``prop.post`` reports
  ``order_total`` ``{total, enable}`` after some changes.

There is no full read, so Home Assistant keeps its own copy, learnt from every
add_order / del_order it sees confirmed on the robot's topics (its own and the
app's). A schedule saved while Home Assistant wasn't watching shows up only as
a difference from the summary ("out of sync"); opening and saving it once in
the app, or switching it off and on, teaches it to Home Assistant.

Captures (FutuRazor, Spot+Scrub AI, 24 Oct and 23 Nov 2026):
- ``day`` is a bitmask, Mon 1, Tue 2, Wed 4, Thu 8, Fri 16, Sat 32, Sun 64
  (1, 5, 21 and 106 = Tue+Thu+Sat+Sun observed).
- ``time_zone`` is the UTC offset in seconds when the schedule was saved
  (7200 in October, 3600 in November, Germany).
- Each schedule carries its own room settings (``room_preference``, the
  same rows as the robot's room settings): ``[8]`` = 1 marks the rooms it
  cleans, listed first, with ``[10]`` their order.
- Switching a schedule off sent every ``uv_switch`` value as 0, and switching
  it back on kept them 0.
- The summary's ``enable`` counts the schedules that are on.

Pure functions and a plain class: no Home Assistant imports.
"""
from __future__ import annotations

from copy import deepcopy
import random
import re
import time
from typing import Any, Callable, Iterable

ADD_ORDER = "service.add_order"
DEL_ORDER = "service.del_order"
GET_ORDER = "service.get_order"
ORDER_METHODS = (ADD_ORDER, DEL_ORDER, GET_ORDER)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_BITS = {day: 1 << index for index, day in enumerate(DAYS)}

# The app's key order for add_order params.
ORDER_KEYS = ("id", "enable", "day", "hour", "minute", "repeat", "map_id", "room_count",
              "time_zone", "prefer_type", "is_global", "areas", "room_preference", "uv_switch")

# Room row fields used here (see DysonMqttClient.room_preferences).
_SELECTED = 8
_ORDER = 10

# A command is matched to its reply by msgId; forget unanswered ones.
PENDING_SECONDS = 60.0

class ScheduleError(Exception):
    """A schedule action that can't be done; the message says why."""


_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::00)?$")


def _int(value) -> bool:
    return type(value) is int


def days_to_mask(days: Iterable[str]) -> int:
    """Bitmask for day names ("mon" ... "sun"; longer names work too)."""
    mask = 0
    for day in days:
        key = str(day).strip().lower()[:3]
        if key not in DAY_BITS:
            raise ValueError(f"Unknown day: {day!r}")
        mask |= DAY_BITS[key]
    if not mask:
        raise ValueError("A schedule needs at least one day")
    return mask


def mask_to_days(mask: int) -> list[str]:
    """Day names for a bitmask, Monday first."""
    return [day for day in DAYS if mask & DAY_BITS[day]]


def parse_time(value) -> tuple[int, int]:
    """(hour, minute) from "HH:MM" (or "HH:MM:00", as Home Assistant's time selector sends)."""
    match = _TIME.match(str(value).strip())
    if not match:
        raise ValueError(f"Time must be HH:MM, not {value!r}")
    return int(match.group(1)), int(match.group(2))


def validate_order(params: Any) -> dict:
    """The order if it has the captured shape; ValueError otherwise."""
    if not isinstance(params, dict):
        raise ValueError("Order is not an object")
    for key, low, high in (("id", 1, 2**63), ("enable", 0, 1), ("day", 1, 127),
                           ("hour", 0, 23), ("minute", 0, 59)):
        value = params.get(key)
        if not _int(value) or not low <= value <= high:
            raise ValueError(f"Unrecognised order field {key}: {value!r}")
    rows = params.get("room_preference")
    if not isinstance(rows, list) or not all(
            isinstance(row, list) and len(row) > _ORDER and _int(row[0]) for row in rows):
        raise ValueError("Unrecognised room plan")
    return params


def selected_rows(order: dict) -> list[list]:
    """The rooms a schedule cleans ([8] = 1), in its order ([10])."""
    rows = [row for row in order.get("room_preference") or []
            if isinstance(row, list) and len(row) > _ORDER and row[_SELECTED] == 1]
    return sorted(rows, key=lambda row: row[_ORDER] if _int(row[_ORDER]) else 0)


def describe_order(order: dict, room_name: Callable[[Any], str] = str) -> dict:
    """A readable summary of one schedule, for attributes and action responses."""
    rows = selected_rows(order)
    return {
        "schedule_id": order["id"],
        "enabled": order["enable"] == 1,
        "days": mask_to_days(order["day"]),
        "time": f"{order['hour']:02d}:{order['minute']:02d}",
        "repeat": order.get("repeat") == 1,
        "map_id": None if order.get("map_id") is None else str(order["map_id"]),
        "room_ids": [str(row[0]) for row in rows],
        "rooms": [room_name(row[1]) for row in rows],
    }


def toggled(order: dict, enabled: bool) -> dict:
    """The order the app sends to switch a schedule on or off."""
    params = deepcopy(order)
    params["enable"] = 1 if enabled else 0
    if not enabled and isinstance(params.get("uv_switch"), list):
        # As the app: switching off sends every room's UV as 0, and switching
        # back on keeps what the robot then has.
        params["uv_switch"] = [[item[0], 0] for item in params["uv_switch"]
                               if isinstance(item, list) and item]
    return ordered_params(params)


def ordered_params(order: dict) -> dict:
    """Params in the app's key order, any other keys after."""
    params = {key: order[key] for key in ORDER_KEYS if key in order}
    params.update({key: value for key, value in order.items() if key not in params})
    return params


def room_plan(rows: list[list], room_ids: list, apply_settings: Callable[[list, dict], bool],
              room_settings: dict | None = None) -> list[list]:
    """A schedule's rows: the chosen rooms first, in order, then the rest, as the app sends.

    ``rows`` is the base (the robot's room settings for a new schedule, the
    schedule's own rows for a change); ``room_settings`` (room ID -> labels,
    as for start_rooms_with_settings) is applied with ``apply_settings``.
    """
    by_id = {str(row[0]): deepcopy(row) for row in rows if isinstance(row, list) and row}
    chosen = [str(room_id) for room_id in room_ids]
    if not chosen:
        raise ValueError("A schedule needs at least one room")
    if len(set(chosen)) != len(chosen):
        raise ValueError("A room is listed twice")
    missing = [room_id for room_id in chosen if room_id not in by_id]
    if missing:
        raise ValueError(f"Unknown room ID: {', '.join(missing)}")
    settings = room_settings or {}
    unknown = [str(room_id) for room_id in settings if str(room_id) not in by_id]
    if unknown:
        raise ValueError(f"Room settings for an unknown room ID: {', '.join(unknown)}")
    for room_id, values in settings.items():
        if not isinstance(values, dict) or not apply_settings(by_id[str(room_id)], values):
            raise ValueError(f"Invalid room settings for room ID {room_id}")
    others = [row for row in rows if isinstance(row, list) and row and str(row[0]) not in chosen]
    plan = [by_id[room_id] for room_id in chosen] + [by_id[str(row[0])] for row in others]
    for position, row in enumerate(plan, start=1):
        row[_SELECTED] = 1 if str(row[0]) in chosen else 0
        row[_ORDER] = position
    return plan


def uv_plan(plan: list[list], uv_rows: Any, default: int = 0) -> list[list]:
    """uv_switch in the plan's room order, from the robot's values where known."""
    known = {str(item[0]): item[1] for item in uv_rows or []
             if isinstance(item, list) and len(item) == 2 and _int(item[1])}
    return [[row[0], known.get(str(row[0]), default)] for row in plan]


def new_order_id(existing: Iterable[int]) -> int:
    """A schedule ID like the app's (nine or ten digits), not already used."""
    taken = set(existing)
    while True:
        order_id = random.randint(100_000_000, 2**31 - 1)
        if order_id not in taken:
            return order_id


def time_conflict(orders: Iterable[dict], day: int, hour: int, minute: int,
                  ignore_id: int | None = None) -> dict | None:
    """An enabled schedule at the same time on a shared day (the app refuses those)."""
    for order in orders:
        if (order.get("id") != ignore_id and order.get("enable") == 1
                and order.get("hour") == hour and order.get("minute") == minute
                and _int(order.get("day")) and order["day"] & day):
            return order
    return None


class ScheduleBook:
    """Home Assistant's copy of one robot's schedules, learnt from its MQTT traffic.

    Feed it every add_order / del_order / get_order message seen on the
    robot's command/jdm and status/jdm topics, and prop.post order_total.
    Each feed returns True when something shown changed.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.orders: dict[int, dict] = {}
        self.robot_total: int | None = None
        self.robot_enabled: int | None = None
        self.in_sync: bool | None = None
        self._synced_md5: str | None = None
        self._expect_baseline = False
        self._pending: dict[str, tuple[str, Any, float]] = {}

    # ── Persistence ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "orders": [self.orders[key] for key in sorted(self.orders)],
            "robot_total": self.robot_total,
            "robot_enabled": self.robot_enabled,
            "in_sync": self.in_sync,
            "synced_md5": self._synced_md5,
            # A change was seen since the last checksum: the next one is the new baseline.
            "expect_baseline": self._expect_baseline,
        }

    @classmethod
    def from_dict(cls, data: Any, clock: Callable[[], float] = time.monotonic) -> "ScheduleBook":
        book = cls(clock)
        if not isinstance(data, dict):
            return book
        for raw in data.get("orders") or []:
            try:
                order = validate_order(deepcopy(raw))
            except ValueError:
                continue
            book.orders[order["id"]] = order
        for key in ("robot_total", "robot_enabled"):
            if _int(data.get(key)):
                setattr(book, key, data[key])
        if isinstance(data.get("in_sync"), bool):
            book.in_sync = data["in_sync"]
        if isinstance(data.get("synced_md5"), str):
            book._synced_md5 = data["synced_md5"]
        book._expect_baseline = data.get("expect_baseline") is True
        return book

    # ── Messages ──────────────────────────────────────────────────────────

    def command_seen(self, data: dict) -> None:
        """An add_order / del_order request on command/jdm (ours or the app's)."""
        now = self._clock()
        for key, (_, _, sent) in list(self._pending.items()):
            if now - sent > PENDING_SECONDS:
                del self._pending[key]
        if data.get("method") in (ADD_ORDER, DEL_ORDER) and data.get("msgId") is not None:
            self._pending[str(data["msgId"])] = (data["method"], deepcopy(data.get("params")), now)

    def reply_seen(self, data: dict) -> bool:
        """A reply on status/jdm."""
        method = data.get("method")
        reply = data.get("data")
        pending = self._pending.pop(str(data.get("msgId")), None)
        if data.get("code") != 0 or not isinstance(reply, dict):
            return False
        if method == GET_ORDER:
            return self.summary_seen(reply.get("order_data_lite"))
        if pending is None or reply.get("result", 0) != 0:
            return False
        sent_method, params, _ = pending
        try:
            if sent_method == ADD_ORDER:
                return self.apply_add(params)
            if isinstance(params, dict) and _int(params.get("id")):
                return self.apply_delete(params["id"])
        except ValueError:
            return False
        return False

    def apply_add(self, params: Any) -> bool:
        """A confirmed add_order: the schedule as the robot now has it."""
        order = validate_order(deepcopy(params))
        if self.orders.get(order["id"]) == order:
            return False
        self.orders[order["id"]] = order
        self._expect_baseline = True
        return True

    def apply_delete(self, order_id: int) -> bool:
        if self.orders.pop(order_id, None) is None:
            return False
        self._expect_baseline = True
        return True

    def summary_seen(self, lite: Any) -> bool:
        """get_order's order_data_lite: compare with what Home Assistant knows."""
        if not isinstance(lite, dict) or not _int(lite.get("total")):
            return False
        before = (self.robot_total, self.robot_enabled, self.in_sync)
        self.robot_total = lite["total"]
        if _int(lite.get("enable")):
            self.robot_enabled = lite["enable"]
        md5 = lite.get("md5") if isinstance(lite.get("md5"), str) else None
        if not self._counts_match():
            self.in_sync = False
        elif self._expect_baseline or self._synced_md5 is None:
            # Matches a change Home Assistant saw (or its first look):
            # remember this checksum as the in-step one.
            self._synced_md5 = md5
            self._expect_baseline = False
            self.in_sync = True
        else:
            self.in_sync = md5 == self._synced_md5
        return (self.robot_total, self.robot_enabled, self.in_sync) != before

    def total_seen(self, total: Any) -> bool:
        """prop.post order_total {total, enable}: counts only, no checksum."""
        if not isinstance(total, dict) or not _int(total.get("total")):
            return False
        before = (self.robot_total, self.robot_enabled, self.in_sync)
        self.robot_total = total["total"]
        if _int(total.get("enable")):
            self.robot_enabled = total["enable"]
        if not self._counts_match():
            self.in_sync = False
        return (self.robot_total, self.robot_enabled, self.in_sync) != before

    def _counts_match(self) -> bool:
        if self.robot_total != len(self.orders):
            return False
        enabled = sum(1 for order in self.orders.values() if order["enable"] == 1)
        return self.robot_enabled is None or self.robot_enabled == enabled

    # ── Views ─────────────────────────────────────────────────────────────

    @property
    def unknown_count(self) -> int:
        """Schedules on the robot that Home Assistant hasn't seen (at least)."""
        if self.robot_total is None:
            return 0
        return max(0, self.robot_total - len(self.orders))

    def sorted_orders(self) -> list[dict]:
        """By first day, then time."""
        def key(order):
            first = next((i for i, day in enumerate(DAYS) if order["day"] & DAY_BITS[day]), 7)
            return (first, order["hour"], order["minute"], order["id"])
        return sorted(self.orders.values(), key=key)


# The calendar shows each scheduled clean as an hour long: the robot doesn't
# say how long a clean will take.
EVENT_MINUTES = 60


def occurrences(order: dict, start, end) -> list:
    """Start times (aware datetimes, in start's time zone) of an enabled schedule in [start - 1 h, end)."""
    from datetime import datetime, timedelta

    if order.get("enable") != 1 or not _int(order.get("day")):
        return []
    tz = start.tzinfo
    out = []
    day = (start - timedelta(minutes=EVENT_MINUTES)).date()
    while day <= end.date():
        if order["day"] & (1 << day.weekday()):
            at = datetime(day.year, day.month, day.day, order["hour"], order["minute"], tzinfo=tz)
            if at + timedelta(minutes=EVENT_MINUTES) > start and at < end:
                out.append(at)
        day += timedelta(days=1)
    return out
