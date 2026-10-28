"""Station preferences observed in MyDyson MQTT captures (October 2026)."""

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
# Label → minutes between self-cleans (None = after each room, i.e. ROOM mode).
FREQUENCIES = {"After each room": None, "Every 15 minutes": 15,
               "Every 30 minutes": 30, "Only as needed": 60}
SELECTS = {
    "self_cleaning_frequency": ("Self-cleaning frequency", list(FREQUENCIES)),
    "roller_drying_duration": ("Roller drying duration", ["3 hours", "4 hours", "5 hours"]),
}
# CURRENT-STATE fields cleared on reconnect so a stale value is never shown.
STATE_FIELDS = [item[1] for item in SWITCHES.values()] + [
    "backWashType", "backWashTime", "airDryFrequency"]


def current_setting(state, key):
    """Read confirmed state only; unknown encodings never become defaults."""
    if key in SWITCHES:
        value = state.get(SWITCHES[key][1])
        return value if type(value) is bool else None
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
        if type(hours) is int and hours in (3, 4, 5):
            return f"{hours} hours"
    return None


def setting_payloads(key, value):
    """Build only the requested setting, never a full state replacement."""
    if key in SWITCHES and type(value) is bool:
        _, field, prop = SWITCHES[key]
        return {field: value}, {prop: int(value)}
    if key == "self_cleaning_frequency" and isinstance(value, str) and value in FREQUENCIES:
        minutes = FREQUENCIES[value]
        if minutes is None:
            return {"backWashType": "ROOM"}, {"back_wash_type": 1}
        return ({"backWashType": "TIME", "backWashTime": minutes},
                {"back_wash_type": 0, "back_wash_time": minutes})
    if key == "roller_drying_duration" and value in SELECTS[key][1]:
        hours = int(value[0])
        return {"airDryFrequency": hours}, {"airdry_frequency": hours}
    raise ValueError("Unsupported station setting or value")


def reported_properties(props):
    """Translate individually valid JDM properties, including partial replies."""
    updates = {}
    for _, field, prop in SWITCHES.values():
        value = props.get(prop)
        if type(value) is int and value in (0, 1):
            updates[field] = bool(value)
    value = props.get("back_wash_type")
    if type(value) is int and value in (0, 1):
        updates["backWashType"] = "ROOM" if value else "TIME"
    for prop, field, allowed in [("back_wash_time", "backWashTime", (15, 30, 60)),
                                 ("airdry_frequency", "airDryFrequency", (3, 4, 5))]:
        value = props.get(prop)
        if type(value) is int and value in allowed:
            updates[field] = value
    return updates
