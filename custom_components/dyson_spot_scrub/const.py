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
