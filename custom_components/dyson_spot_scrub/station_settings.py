"""Station and robot preferences observed in MyDyson MQTT captures (Oct-Nov 2026)."""
from datetime import time

# key: (English label, reported CURRENT-STATE field, JDM property)
# NOTE: "extended_roller_preparation" maps to the JDM property
# "store_demo_mode". The mapping comes from observed MyDyson app behaviour
# (Oct 2026); because the name is unusual it is due a controlled on/off
# comparison capture. Do not change it until that has been done.
SWITCHES = {
    "hot_water_mopping": ("Hot water mopping", "hotWaterMop", "hot_water_mop"),
    "cleaning_solution": ("Cleaning solution", "detergent", "detergent"),
    "extended_roller_preparation": ("Extended roller preparation", "washMopBeforeClean", "store_demo_mode"),
    "hot_water_self_cleaning": ("Hot water self-cleaning", "hotWaterSwitch", "hot_water_switch"),
}
# Robot settings every model reports, mop or not (FutuRazor's Spot+Scrub AI
# capture on #50, 13 Nov 2026; also in the R1 and R2 logs). Same shape as
# SWITCHES. "alarm" is the app's voice prompts toggle, not a locate sound.
DEVICE_SWITCHES = {
    "voice_prompts": ("Voice prompts", "alarm", "alarm"),
    "child_lock": ("Child lock", "childLock", "child_lock"),
}
ALL_SWITCHES = {**SWITCHES, **DEVICE_SWITCHES}
# Volume of voice prompts and sounds: STATE-SET {"volume": n} and
# prop.set {"volume": n}. The app has been seen sending 50, 60 and 70.
VOLUME_RANGE = (0, 100)
VOLUME_STEP = 10
# Do Not Disturb: STATE-SET doNotDisturbMode {isOn, startTime, endTime} with
# times like "22:00" and "8:00"; the JDM side is service.set_quiet_time
# {is_open, quiet_begin_time, quiet_end_time} in minutes after midnight.
DND_KEYS = ("do_not_disturb", "do_not_disturb_start", "do_not_disturb_end")
DND_FIELD = "doNotDisturbMode"
# Settings sent with something other than prop.set.
JDM_METHODS = {key: "service.set_quiet_time" for key in DND_KEYS}
# Label → minutes between self-cleans (None = after each room, i.e. ROOM mode).
FREQUENCIES = {"After each room": None, "Every 15 minutes": 15,
               "Every 30 minutes": 30, "Only as needed": 60}
# Roller drying hours the robot may report. The Spot+Scrub AI app offers 3-5 h;
# the R2 Nurovi app offers 2-4 h (app screenshot via FutuRazor, #32; both R2
# captures, 18 and 20 Oct 2026, report 2 h).
DRYING_HOURS = (2, 3, 4, 5)
SELECTS = {
    "self_cleaning_frequency": ("Self-cleaning frequency", list(FREQUENCIES)),
    "roller_drying_duration": ("Roller drying duration", ["3 hours", "4 hours", "5 hours"]),
}
R2_SELECT_OPTIONS = {"roller_drying_duration": ["2 hours", "3 hours", "4 hours"]}
# Keys of the mop station settings, which a robot without a mop never gets.
MOP_SETTING_KEYS = frozenset({*SWITCHES, *SELECTS})
# Station switches the R2 Nurovi never reports (no cleaning solution).
R2_MISSING_SWITCHES = {"cleaning_solution"}
# CURRENT-STATE fields cleared on reconnect so a stale value is never shown.
STATE_FIELDS = [item[1] for item in ALL_SWITCHES.values()] + [
    "backWashType", "backWashTime", "airDryFrequency", "volume", DND_FIELD]


def _parse_time(text):
    """ "22:00" or "8:00" → time; anything else → None."""
    if not isinstance(text, str):
        return None
    hours, sep, minutes = text.partition(":")
    if not sep or not hours.isdigit() or not minutes.isdigit() or len(minutes) != 2:
        return None
    h, m = int(hours), int(minutes)
    return time(h, m) if h < 24 and m < 60 else None


def _format_time(value):
    """time → "8:00" / "22:30", the app's format (no leading zero on the hour)."""
    return f"{value.hour}:{value.minute:02d}"


def _minutes(value):
    return value.hour * 60 + value.minute


def _from_minutes(value):
    if type(value) is int and 0 <= value < 24 * 60:
        return time(value // 60, value % 60)
    return None


def do_not_disturb(state):
    """(is_on, start, end) from confirmed state, or None if any part is unknown."""
    mode = state.get(DND_FIELD)
    if not isinstance(mode, dict) or type(mode.get("isOn")) is not bool:
        return None
    start, end = _parse_time(mode.get("startTime")), _parse_time(mode.get("endTime"))
    if start is None or end is None:
        return None
    return mode["isOn"], start, end


def current_setting(state, key):
    """Read confirmed state only; unknown encodings never become defaults."""
    if key in ALL_SWITCHES:
        value = state.get(ALL_SWITCHES[key][1])
        return value if type(value) is bool else None
    if key == "volume":
        value = state.get("volume")
        low, high = VOLUME_RANGE
        return value if type(value) is int and low <= value <= high else None
    if key in DND_KEYS:
        dnd = do_not_disturb(state)
        return None if dnd is None else dnd[DND_KEYS.index(key)]
    if key == "self_cleaning_frequency":
        kind = state.get("backWashType")
        if kind == "ROOM":
            return "After each room"
        minutes = state.get("backWashTime")
        if kind == "TIME" and type(minutes) is int:
            return next((label for label, value in FREQUENCIES.items()
                         if value == minutes), None)
    if key == "roller_drying_duration":
        hours = state.get("airDryFrequency")
        if type(hours) is int and hours in DRYING_HOURS:
            return f"{hours} hours"
    return None


def setting_payloads(key, value, state=None):
    """Build only the requested setting, never a full state replacement.

    Do Not Disturb is the exception: the app always sends all three parts, so
    the other two come from ``state`` (the robot's confirmed values).
    """
    if key in ALL_SWITCHES and type(value) is bool:
        _, field, prop = ALL_SWITCHES[key]
        return {field: value}, {prop: int(value)}
    if key == "volume" and type(value) is int and VOLUME_RANGE[0] <= value <= VOLUME_RANGE[1]:
        return {"volume": value}, {"volume": value}
    if key in DND_KEYS:
        dnd = do_not_disturb(state or {})
        expected = bool if key == "do_not_disturb" else time
        if dnd is None or type(value) is not expected:
            raise ValueError("Do Not Disturb isn't known yet")
        parts = list(dnd)
        parts[DND_KEYS.index(key)] = value
        is_on, start, end = parts
        return ({DND_FIELD: {"isOn": is_on, "startTime": _format_time(start),
                             "endTime": _format_time(end)}},
                {"is_open": int(is_on), "quiet_begin_time": _minutes(start),
                 "quiet_end_time": _minutes(end)})
    if key == "self_cleaning_frequency" and isinstance(value, str) and value in FREQUENCIES:
        minutes = FREQUENCIES[value]
        if minutes is None:
            return {"backWashType": "ROOM"}, {"back_wash_type": 1}
        return ({"backWashType": "TIME", "backWashTime": minutes},
                {"back_wash_type": 0, "back_wash_time": minutes})
    if (key == "roller_drying_duration" and isinstance(value, str)
            and value in [f"{h} hours" for h in DRYING_HOURS]):
        hours = int(value.split()[0])
        return {"airDryFrequency": hours}, {"airdry_frequency": hours}
    raise ValueError("Unsupported station setting or value")


def reported_properties(props):
    """Translate individually valid JDM properties, including partial replies."""
    updates = {}
    for _, field, prop in ALL_SWITCHES.values():
        value = props.get(prop)
        if type(value) is int and value in (0, 1):
            updates[field] = bool(value)
    value = props.get("back_wash_type")
    if type(value) is int and value in (0, 1):
        updates["backWashType"] = "ROOM" if value else "TIME"
    for prop, field, allowed in [("back_wash_time", "backWashTime", (15, 30, 60)),
                                 ("airdry_frequency", "airDryFrequency", DRYING_HOURS)]:
        value = props.get(prop)
        if type(value) is int and value in allowed:
            updates[field] = value
    value = props.get("volume")
    if type(value) is int and VOLUME_RANGE[0] <= value <= VOLUME_RANGE[1]:
        updates["volume"] = value
    # prop.get: quiet_is_open / quiet_begin_time / quiet_end_time.
    # prop.post: quiet_status {quieting, begin_time, end_time}.
    status = props.get("quiet_status")
    if isinstance(status, dict):
        parts = (status.get("quieting"), status.get("begin_time"), status.get("end_time"))
    else:
        parts = (props.get("quiet_is_open"), props.get("quiet_begin_time"),
                 props.get("quiet_end_time"))
    is_open, start, end = parts[0], _from_minutes(parts[1]), _from_minutes(parts[2])
    if type(is_open) is int and is_open in (0, 1) and start and end:
        updates[DND_FIELD] = {"isOn": bool(is_open), "startTime": _format_time(start),
                              "endTime": _format_time(end)}
    return updates
