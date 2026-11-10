"""Validated, single-rectangle vacuum prototype (robot coordinates in metres)."""
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
