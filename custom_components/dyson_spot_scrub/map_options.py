"""Map display options; validation never mutates the config entry."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math

from .const import MAP_CHOICES, MAP_DEFAULTS, MAP_NUMBER_RANGES


def map_options(options: Mapping | None = None) -> dict:
    """Return validated, independent values with defaults for invalid options."""
    result = deepcopy(MAP_DEFAULTS)
    for key, value in (options or {}).items():
        if key not in result:
            continue
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
