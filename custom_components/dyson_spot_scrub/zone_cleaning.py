"""Validated, single-rectangle zone cleaning (robot coordinates in metres)."""
import math


def rectangle_points(rectangle):
    """Convert [xmin, ymin, xmax, ymax] to the app's ordered corner objects."""
    if not isinstance(rectangle, (list, tuple)) or len(rectangle) != 4:
        raise ValueError("Rectangle must contain exactly four coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in rectangle):
        raise ValueError("Coordinates must be finite numbers in metres")
    xmin, ymin, xmax, ymax = rectangle
    if xmin >= xmax or ymin >= ymax:
        raise ValueError("Rectangle must have positive width and height")
    return [{"x": x, "y": y} for x, y in
            ((xmin, ymin), (xmin, ymax), (xmax, ymax), (xmax, ymin))]


def validate_map_id(value):
    """Require an explicit map; never silently target a different floor."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Map ID must be a positive integer or numeric string")
    value = str(value)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Map ID must be a positive integer or numeric string")
    return str(int(value))


def validate_zone_strategy(value):
    """Accept only power strings verified in app zone-start captures."""
    if not isinstance(value, str) or value not in ("auto", "boost", "quiet", "quick"):
        raise ValueError("Unsupported zone cleaning strategy")
    return value


def validate_zone_mode(value):
    """Accept the four cleanType strings captured from app zone starts."""
    if not isinstance(value, str) or value not in ("vacuum", "vacuumAndMop", "mop", "vacuumThenMop"):
        raise ValueError("Unsupported zone cleaning mode")
    return value


def validate_zone_water(value):
    """Accept the four captured waterLevel strings."""
    if not isinstance(value, str) or value not in ("veryLow", "low", "medium", "high"):
        raise ValueError("Unsupported zone water level")
    return value


def validate_zone_passes(value):
    """Keep independent dry/mop counts bounded, including experimental repeats."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in (1, 2):
        raise ValueError("Zone passes must be 1 or 2")
    return int(value)
