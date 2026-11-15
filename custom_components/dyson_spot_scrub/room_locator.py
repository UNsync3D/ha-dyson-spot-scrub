"""Which room the robot is in, from its position and the map's room outlines (#53).

The robot doesn't report its room. While it moves, CURRENT-STATE carries
``globalPosition`` (about once a second) in metres, in the same frame as the
persistent map's zones: on the 3 Nov 2026 Spot+Scrub AI log, every position
from a clean of Dining fell inside the map's rooms (57 in Dining, 7 just over
the Kitchen line). Each zone's ``visited`` list is a closed outline of the
floor the robot can reach in that room, so a point-in-polygon test gives the
room. Pure functions: no Home Assistant imports.
"""
from __future__ import annotations

from typing import Any

# Doorways: a point can sit between two outlines, inside both, or a few
# centimetres over the line while the robot edges along it (the 3 Nov log
# crossed the Dining/Kitchen line by up to 4 cm). The robot stays in the room
# it was last seen in until it is this far inside another one: about its radius.
SWITCH_MARGIN = 0.2   # metres


def room_outlines(map_data: Any) -> list[tuple[str, list[tuple[float, float]]]]:
    """(zone id, outline) for each room with a usable outline."""
    out = []
    zones = map_data.get("zones") if isinstance(map_data, dict) else None
    for zone in zones or []:
        if not isinstance(zone, dict) or zone.get("id") is None:
            continue
        poly = _points(zone.get("visited"))
        if len(poly) < 3:
            poly = _presentation_polygon(zone.get("presentation"))
        if len(poly) >= 3:
            out.append((str(zone["id"]), poly))
    return out


def latest_position(state: dict) -> tuple[float, float] | None:
    """The newest (x, y) in a state's globalPosition, if any."""
    positions = state.get("globalPosition")
    if not isinstance(positions, list):
        return None
    for p in reversed(positions):
        if isinstance(p, dict):
            x, y = p.get("x"), p.get("y")
            if _number(x) and _number(y):
                return float(x), float(y)
    return None


def locate(outlines, x: float, y: float, previous: str | None = None) -> str | None:
    """Zone id containing (x, y). Near a doorway the previous room wins."""
    inside = [(zone_id, poly) for zone_id, poly in outlines if _inside(poly, x, y)]
    if previous is None:
        return inside[0][0] if inside else None
    if any(zone_id == previous for zone_id, _ in inside):
        return previous
    for zone_id, poly in inside:
        if _edge_distance(poly, x, y) >= SWITCH_MARGIN:
            return zone_id
    return previous


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _points(raw) -> list[tuple[float, float]]:
    pts = []
    for p in raw or []:
        if isinstance(p, dict) and _number(p.get("x")) and _number(p.get("y")):
            pts.append((float(p["x"]), float(p["y"])))
    return pts


def _presentation_polygon(segments) -> list[tuple[float, float]]:
    """Fallback: chain the room's straight outline segments into one polygon."""
    pairs = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        a, b = _points([seg.get("start")]), _points([seg.get("end")])
        if a and b:
            pairs.append((a[0], b[0]))
    if not pairs:
        return []
    chain = list(pairs[0])
    remaining = pairs[1:]
    while remaining:
        tail = chain[-1]
        best = min(range(len(remaining)), key=lambda i: min(
            _dist(remaining[i][0], tail), _dist(remaining[i][1], tail)))
        a, b = remaining.pop(best)
        chain.append(b if _dist(a, tail) <= _dist(b, tail) else a)
    return chain


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _edge_distance(poly, x: float, y: float) -> float:
    """Distance from (x, y) to the nearest edge of the outline."""
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length))
        best = min(best, ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5)
    return best


def _inside(poly, x: float, y: float) -> bool:
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit
