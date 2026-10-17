"""Constants for the Dyson Spot+Scrub AI integration."""

DOMAIN = "dyson_spot_scrub"
PLATFORMS = ["vacuum", "binary_sensor", "sensor", "select", "button", "switch", "camera"]

# Config entry keys
CONF_COUNTRY    = "country"
CONF_AUTH_TOKEN = "auth_token"
CONF_SERIAL     = "serial"
CONF_DEVICE_NAME    = "device_name"
CONF_PRODUCT_TYPE   = "product_type"
CONF_MQTT_PREFIX    = "mqtt_prefix"

# Local MQTT connection (replaces cloud IoT credentials)
CONF_ROBOT_HOST          = "robot_host"
CONF_LOCAL_MQTT_USERNAME = "local_mqtt_username"
CONF_LOCAL_MQTT_PASSWORD = "local_mqtt_password"

# Persisted room names — cached so switch entities survive HA restarts even
# when MQTT hasn't connected yet (e.g. robot is drying or briefly offline).
CONF_CACHED_ROOMS   = "cached_rooms"

# Cleaning mode names (displayed in HA)
MODE_VACUUM          = "Vacuum"
MODE_VACUUM_AND_MOP  = "Vacuum and Mop"
MODE_MOP             = "Mop"
MODE_VACUUM_THEN_MOP = "Vacuum then Mop"

CLEANING_MODES = [MODE_VACUUM, MODE_VACUUM_AND_MOP, MODE_MOP, MODE_VACUUM_THEN_MOP]

MODE_TO_INT: dict[str, int] = {
    MODE_VACUUM:          0,
    MODE_VACUUM_AND_MOP:  1,
    MODE_MOP:             2,
    MODE_VACUUM_THEN_MOP: 3,
}

# Default fan speed (cleaning mode) when none has been chosen yet
DEFAULT_MODE = MODE_VACUUM

# Dyson robot product type prefixes / exact codes.
# "804" = Spot+Scrub AI (confirmed from live manifest, Aug 2026)
# "RB0" = other robot vacuum family
# "276", "277" = older robot models
ROBOT_PRODUCT_PREFIXES = ("RB0", "276", "277", "804")

# How long (seconds) to suppress snap-back on optimistic OFF
OPTIMISTIC_OFF_TIMEOUT = 15


# ── Fault code mapping (hardware-verified, FutuRazor Oct 2026) ─────────────────────
#
# Codes appear in activeFaults[*].faultCode on current firmware. Older captures
# and firmware may use activeFaults[*].faultId instead.
# main /status topic.  Ghost-data codes (2108/2109) that leaked from app
# polling cycles on /status/jdm are intentionally excluded.
#
# 2105 is the normal dock-idle baseline (LOG_ONLY) — excluded from has_fault()
# and active_fault_code() so it never triggers VacuumActivity.ERROR.
# 2103 is informational (dock maintenance log) — not a robot-stopping error.

FAULT_CODE_INFORMATIONAL: frozenset[int] = frozenset({2103, 2105})

# Full verified dictionary from FutuRazor MQTT captures, Oct 2026.
FAULT_CODE_MESSAGES: dict[int, str] = {
    # ── 3-digit codes ─────────────────────────────────────────────────────────────────
    500: "LiDAR sensor obstructed",
    501: "Wheels lifted",
    502: "Battery is low",
    503: "Robot's bin not detected",
    504: "Gyroscopic sensor error",
    507: "Unable to determine position",
    508: "Unable to climb slope",
    509: "Drop sensor obstructed",
    510: "Collision sensor obstructed",
    511: "Unable to return to dock",
    513: "Robot stuck",
    514: "Robot stuck",
    516: "Battery temperature high",
    518: "Battery is low",
    521: "Dock's clean water tank not detected",
    522: "Wet roller not detected",
    560: "Side sweeper stuck",
    561: "Camera obstructed",
    562: "Wall follow sensor obstructed",
    563: "Robot's bin not detected",
    566: "Robot's dirty water tank not detected",
    567: "Brush bar error",
    568: "Left wheel stuck",
    569: "Right wheel stuck",
    570: "Brush bar error",
    572: "Robot stuck",
    581: "Dock's clean water tank empty",
    582: "Dock's dirty water tank full",
    583: "Dock's clean water tank not detected",
    584: "Dock's dirty water tank not detected",
    586: "Robot's dirty water tank full",
    587: "Communication failure",
    591: "Dock's bin full",
    592: "Dock's filter error",
    594: "Unable to empty robot's bin",
    595: "Communication failure",
    596: "Unable to empty robot's bin",
    597: "Unable to empty robot's bin",
    611: "Mapping failed",
    612: "Mapping failed",
    620: "Dock's cleaning solution empty",
    627: "Something went wrong",
    629: "Wet roller not detected",
    630: "Wet roller stuck",
    634: "Unable to return to dock",
    636: "Robot stuck",
    637: "Dock's clean water tank not detected",
    639: "Wet roller not detected",
    645: "Wet roller not detected",
    646: "Wet roller error",
    650: "Robot's dirty water tank not detected",
    # ── 4-digit codes ─────────────────────────────────────────────────────────────────
    2000: "Dock's bin full",
    2003: "Unable to start scheduled clean",
    2007: "Mapping failed",
    2012: "Unable to reach area",
    2103: "Self-cleaning finished — check dock",   # informational; see FAULT_CODE_INFORMATIONAL
    2119: "Unable to start scheduled clean",
    2123: "Dock's clean water pump error",
    2124: "Dock's dirty water pump error",
    2125: "Robot not charging",
    2126: "Robot not charging",
    2131: "Battery temperature too low",
    2132: "Battery temperature too high",
    2133: "Battery temperature too low",
}
