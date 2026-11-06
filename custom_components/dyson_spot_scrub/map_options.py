"""Map display options; validation never mutates the config entry."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math

from .const import (
    MAP_CHOICES,
    MAP_DEFAULTS,
    MAP_NUMBER_RANGES,
    MAP_STYLING_ENABLED,
    MAP_WALL_DEFAULTS,
)


# v1.5.0 defaults. Saving the options form stores every field, so most v1.5.0
# users have these saved even though they never chose them. A saved value
# equal to its old default is read as "not set", so v2.0.0's MyDyson-style
# look applies; a colour the user picked themselves is kept.
V150_DEFAULTS = {
    "map_vacuum_color": [250, 204, 21],
    "map_mop_color": [34, 211, 238],
    "map_combined_color": [192, 132, 252],
    "map_glow_width": 6,
}


def map_options(options: Mapping | None = None, *, upgrade_old_defaults: bool = True) -> dict:
    """Return validated, independent values with defaults for invalid options.

    upgrade_old_defaults=False is for validating what a user just submitted,
    so an explicit choice is never rewritten.
    """
    result = deepcopy(MAP_DEFAULTS)
    for key, value in (options or {}).items():
        if key not in result:
            continue
        if (upgrade_old_defaults and key in V150_DEFAULTS
                and isinstance(value, (list, tuple, int)) and not isinstance(value, bool)
                and (list(value) if isinstance(value, (list, tuple)) else value)
                == V150_DEFAULTS[key]):
            continue        # an untouched v1.5.0 default: use today's default
        if key in MAP_WALL_DEFAULTS and not MAP_STYLING_ENABLED:
            continue        # styling switched off: always the default look
        if key in MAP_CHOICES:
            if isinstance(value, str) and value in MAP_CHOICES[key]:
                result[key] = value
        elif isinstance(result[key], bool):
            if isinstance(value, bool):
                result[key] = value
        elif key in MAP_NUMBER_RANGES:
            low, high = MAP_NUMBER_RANGES[key]
            if (type(value) in (int, float) and math.isfinite(value)
                    and low <= value <= high and int(value) == value):
                result[key] = int(value)
        elif isinstance(value, (list, tuple)) and len(value) == 3:
            if all(type(c) in (int, float) and math.isfinite(c)
                   and 0 <= c <= 255 and int(c) == c for c in value):
                result[key] = [int(c) for c in value]
    return result
