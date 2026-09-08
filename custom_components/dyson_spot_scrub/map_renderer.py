"""Pillow-based renderer for the Dyson floor map → PNG bytes.

Confirmed API structure (from live HA debug logs, Sep 2026):

  map_data (from get_map / get_current_map):
    id, orientation, dimensions, dockLocation, furniture, restrictions,
    hazardZones
    zones[]: id, name, type, nameLocation {x,y}, area, cleanStatus,
             visited [{x,y}],
             presentation [{start:{x,y}, end:{x,y}, type:int}]
               type 0 = perimeter pass  ← outlines the zone boundary
               type 1 = sweep pass
               type 2 = turn

  live_data (from get_live_map, only while robot is cleaning):
    robotLocation {x, y, angle}
    cleanPath [{x,y} | {x,y,update}]
    dockLocation {x, y, angle}
    zones[]: same as above but current-session status/visited/presentation
    furniture [], restrictions [], dirt [], hazardZones []

Zone boundary polygons are NOT in the REST API as explicit polygons.
We reconstruct them from the perimeter-pass (type=0) presentation
segments, which the robot drives around the edge of each zone.
"""
from __future__ import annotations

import io
import math
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ── Canvas constants ───────────────────────────────────────────────────────────

SCALE = 72   # pixels per metre
PAD   = 70   # border padding in pixels


# ── Colour palette ─────────────────────────────────────────────────────────────

BG             = (15,  23,  42,  255)   # #0f172a
GRID           = (71,  85,  105, 30)    # faint slate-grey
DOCK           = (52,  211, 153, 255)   # #34d399 green
ROBOT          = (250, 204, 21,  255)   # #facc15 yellow
ROBOT_HALO     = (250, 204, 21,  64)
PATH_MAIN      = (34,  211, 238, 255)   # #22d3ee cyan
PATH_GLOW      = (34,  211, 238, 64)
KEEPOUT_FILL   = (239, 68,  68,  51)
KEEPOUT_STROKE = (239, 68,  68,  200)
FURN_U_FILL    = (99,  102, 241, 64)
FURN_U_STROKE  = (129, 140, 248, 220)
FURN_A_FILL    = (245, 158, 11,  64)
FURN_A_STROKE  = (245, 158, 11,  200)
AXIS_COL       = (71,  85,  105, 200)
ORIGIN_COL     = (255, 255, 255, 40)

# Zone colour palette — one per zone, cycling
# Each entry: (stroke_rgb, visited_rgba, perimeter_rgba, label_rgba)
ZONE_COLOURS = [
    # Cyan    — Living Room / zone 10
    ((56,  189, 248), (56,  189, 248, 60),  (56,  189, 248, 160), (56,  189, 248, 200)),
    # Green   — Dining / zone 11
    ((74,  222, 128), (74,  222, 128, 60),  (74,  222, 128, 160), (74,  222, 128, 200)),
    # Amber   — Kitchen / zone 13
    ((251, 191, 36),  (251, 191, 36,  60),  (251, 191, 36,  160), (251, 191, 36,  200)),
    # Purple  — Toilet / zone 12
    ((192, 132, 252), (192, 132, 252, 60),  (192, 132, 252, 160), (192, 132, 252, 200)),
]

# Status highlight: override visited/perimeter colour when zone is cleaning
STATUS_OVERLAY: dict[str, tuple] = {
    "CLEAN_PENDING":       (56,  189, 248, 120),   # bright cyan glow
    "CLEAN_IN_PROGRESS":   (74,  222, 128, 180),   # bright green
    "CLEANING":            (74,  222, 128, 180),
    "CLEAN_COMPLETE":      (134, 239, 172, 100),   # pale green
    "CLEAN_NOT_REQUESTED": (0,   0,   0,   0),     # no overlay
}


# ── Public API ─────────────────────────────────────────────────────────────────


def render_map(
    map_data:  dict[str, Any] | None,
    metadata:  list[dict[str, Any]] | None,
    live_data: dict[str, Any] | None = None,
) -> bytes:
    """Render the Dyson floor map to PNG bytes.

    map_data  : response from get_map() / get_current_map()
    metadata  : response from get_map_metadata() (used for zone name fallback)
    live_data : response from get_live_map() (None when not cleaning)
    """
    if not _PIL_AVAILABLE:
        return _error_image("Pillow not installed — add Pillow to requirements")
    if not map_data:
        return _error_image("No map data available")
    try:
        return _render(map_data, metadata, live_data)
    except Exception as exc:  # pylint: disable=broad-except
        return _error_image(f"Render error: {exc}")


# ── Rendering ──────────────────────────────────────────────────────────────────


def _render(
    map_data:  dict[str, Any],
    metadata:  list[dict[str, Any]] | None,
    live_data: dict[str, Any] | None,
) -> bytes:

    # ── 1. Collect zones ──────────────────────────────────────────────────────
    # During cleaning, live_data zones have up-to-date visited/cleanStatus;
    # use them in preference to the (potentially stale) map_data zones.
    map_zones  = map_data.get("zones", [])
    live_zones = live_data.get("zones", []) if live_data else []

    # Build live zone lookup: zone_id → live zone dict
    live_by_id: dict[str, dict] = {str(z.get("id", "")): z for z in live_zones}

    # Merge: for each map zone, overlay live data if present.
    # Live zones carry up-to-date cleanStatus and visited paths, but their
    # presentation (perimeter) and nameLocation fields often arrive empty in
    # the first MQTT burst and only fill in later — or never, for zones not
    # yet visited this session.  To prevent the boundary outlines from
    # disappearing every time the map refreshes, we preserve the static map's
    # presentation/nameLocation unless the live data has non-empty values.
    zones: list[dict] = []
    for z in map_zones:
        zid = str(z.get("id", ""))
        if zid in live_by_id:
            live_z = live_by_id[zid]
            merged = {**z, **live_z}
            # Keep static perimeter segments unless live has its own
            if not live_z.get("presentation") and z.get("presentation"):
                merged["presentation"] = z["presentation"]
            # Keep static name location unless live has its own
            if not live_z.get("nameLocation") and z.get("nameLocation"):
                merged["nameLocation"] = z["nameLocation"]
            zones.append(merged)
        else:
            zones.append(z)

    # ── 2. Compute canvas bounds ───────────────────────────────────────────────
    # Try dimensions field first (may contain the map extent)
    min_x, max_x, min_y, max_y = _bounds_from_dimensions(map_data)

    # Supplement / fall back with coordinate data from the map
    coord_pts = _collect_all_coords(map_data, live_data, zones)
    if coord_pts:
        xs = [p[0] for p in coord_pts]
        ys = [p[1] for p in coord_pts]
        if min_x is None or min_y is None:
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
        else:
            min_x = min(min_x, min(xs))
            max_x = max(max_x, max(xs))
            min_y = min(min_y, min(ys))
            max_y = max(max_y, max(ys))

    if min_x is None or abs(max_x - min_x) < 0.1 or abs(max_y - min_y) < 0.1:
        return _error_image(
            "Map has no coordinate data — run a full clean first so the "
            "robot builds its floor plan"
        )

    # Add a small margin so map edges are never clipped
    margin = 0.3
    min_x -= margin
    max_x += margin
    min_y -= margin
    max_y += margin

    W = int((max_x - min_x) * SCALE + PAD * 2) + 2
    H = int((max_y - min_y) * SCALE + PAD * 2) + 2

    # ── Coordinate helpers ────────────────────────────────────────────────────
    def tx(x: float) -> float:
        return (x - min_x) * SCALE + PAD

    def ty(y: float) -> float:
        # Dyson Y-axis is inverted (positive = north on the real floor but
        # down in raw coords) — negate to flip for screen.
        return (max_y - y) * SCALE + PAD

    def txy(pt: Any) -> tuple[int, int]:
        x, y = _pt(pt)
        return int(tx(x)), int(ty(y))

    def txy_list(pts: list) -> list[tuple[int, int]]:
        return [txy(p) for p in pts]

    # ── 3. Create base image ──────────────────────────────────────────────────
    img = Image.new("RGBA", (W, H), BG)

    # ── 4. Grid (every 1 m) ───────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    for gx in range(int(math.floor(min_x)), int(math.ceil(max_x)) + 1):
        px = int(tx(gx))
        draw.line([(px, 0), (px, H)], fill=GRID)
    for gy in range(int(math.floor(min_y)), int(math.ceil(max_y)) + 1):
        py = int(ty(gy))
        draw.line([(0, py), (W, py)], fill=GRID)

    # ── 5. Keep-out / restriction zones ───────────────────────────────────────
    for r in _get_restrictions(map_data, live_data):
        bnd = r.get("points") or r.get("boundary") or []
        if len(bnd) >= 3:
            _poly_alpha(img, txy_list(bnd), KEEPOUT_FILL, KEEPOUT_STROKE,
                        stroke_width=1, dashed=True)

    # ── 6. Zone perimeter outlines and visited paths ──────────────────────────
    for i, z in enumerate(zones):
        col = ZONE_COLOURS[i % len(ZONE_COLOURS)]
        stroke_rgb, visited_rgba, perim_rgba, label_rgba = col

        status = z.get("cleanStatus", "CLEAN_NOT_REQUESTED")
        overlay = STATUS_OVERLAY.get(status, STATUS_OVERLAY["CLEAN_NOT_REQUESTED"])

        # Perimeter pass segments (type == 0) → zone boundary outline.
        # Draw each segment as its own individual line (start→end) to avoid
        # artificial diagonals when segments are non-contiguous.
        perim_segs = _perimeter_segments(z)
        draw = ImageDraw.Draw(img)
        for seg_start, seg_end in perim_segs:
            sx, sy = _pt(seg_start)
            ex, ey = _pt(seg_end)
            # Skip long jumps that would cross the room (robot teleport/lift)
            if math.hypot(ex - sx, ey - sy) > 1.5:
                continue
            draw.line([txy(seg_start), txy(seg_end)], fill=perim_rgba, width=2)

        # Status overlay: collect all contiguous perimeter points for a fill polygon
        perim_pts = _perimeter_points(z)
        if overlay[3] > 0 and len(perim_pts) >= 3:
            _poly_alpha(img, txy_list(perim_pts), overlay, (0, 0, 0, 0))

        # Historical visited path — skip segment if points jump > 0.8 m
        # (robot was lifted or teleported; don't draw a diagonal line across the room)
        visited = z.get("visited", [])
        if len(visited) > 1:
            draw = ImageDraw.Draw(img)
            vpts = [_pt(p) for p in visited]
            for j in range(len(vpts) - 1):
                x0, y0 = vpts[j]
                x1, y1 = vpts[j + 1]
                if math.hypot(x1 - x0, y1 - y0) > 0.8:
                    continue  # large jump — new segment, skip line
                draw.line([txy(vpts[j]), txy(vpts[j + 1])], fill=visited_rgba, width=1)

    # ── 7. Furniture ──────────────────────────────────────────────────────────
    for f in _get_furniture(map_data, live_data):
        bnd = f.get("points") or f.get("boundary") or []
        if len(bnd) >= 3:
            pts2 = txy_list(bnd)
            if f.get("userDefined", False):
                _poly_alpha(img, pts2, FURN_U_FILL, FURN_U_STROKE)
            else:
                _poly_alpha(img, pts2, FURN_A_FILL, FURN_A_STROKE)

    # ── 8. Historical visited paths already drawn above in step 6 ─────────────
    # (no separate step needed)

    # ── 9. Live clean path ────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    if live_data:
        cp = live_data.get("cleanPath", [])
        if len(cp) > 1:
            pts2 = [txy(p) for p in cp]
            for j in range(len(pts2) - 1):
                draw.line([pts2[j], pts2[j + 1]], fill=PATH_GLOW, width=6)
            for j in range(len(pts2) - 1):
                draw.line([pts2[j], pts2[j + 1]], fill=PATH_MAIN, width=3)
            for j in range(4, len(pts2), 5):
                x, y = pts2[j]
                draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=PATH_MAIN)

    # ── 10. Dock ──────────────────────────────────────────────────────────────
    dock = _get_dock(map_data, live_data)
    if dock:
        dx, dy = int(tx(dock["x"])), int(ty(dock["y"]))
        draw = ImageDraw.Draw(img)
        draw.ellipse([dx - 9, dy - 9, dx + 9, dy + 9], fill=DOCK)
        # Small "D" label
        font_s, _ = _load_fonts()
        _draw_centered_text(draw, dx, dy, "D", font_s, BG)

    # ── 11. Robot position + heading ──────────────────────────────────────────
    if live_data:
        robot = live_data.get("robotLocation")
        if robot:
            rx, ry = int(tx(robot["x"])), int(ty(robot["y"]))
            angle  = float(robot.get("angle", 0.0))
            _ellipse_alpha(img, rx, ry, 20, ROBOT_HALO)
            draw = ImageDraw.Draw(img)
            draw.ellipse([rx - 9, ry - 9, rx + 9, ry + 9], fill=ROBOT)
            # Heading arrow (Dyson angle: CCW from East, Y-flipped on screen)
            ax = rx + int(16 * math.cos(angle))
            ay = ry - int(16 * math.sin(angle))
            draw.line([(rx, ry), (ax, ay)], fill=BG, width=2)

    # ── 12. Origin marker ─────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    ox, oy = int(tx(0)), int(ty(0))
    draw.ellipse([ox - 4, oy - 4, ox + 4, oy + 4], fill=None, outline=ORIGIN_COL)

    # ── 13. Zone labels ───────────────────────────────────────────────────────
    font_l, font_s = _load_fonts()
    for i, z in enumerate(zones):
        col = ZONE_COLOURS[i % len(ZONE_COLOURS)]
        label_rgba = col[3]
        loc = z.get("nameLocation") or {}
        lx_m = loc.get("x", 0.0)
        ly_m = loc.get("y", 0.0)
        if not lx_m and not ly_m:
            continue
        lx, ly = int(tx(lx_m)), int(ty(ly_m))
        draw = ImageDraw.Draw(img)
        name = (z.get("name") or "").upper()
        area = f"{z.get('area', 0):.1f} m²"
        _draw_centered_text(draw, lx, ly - 12, name, font_l, label_rgba)
        area_col = (label_rgba[0], label_rgba[1], label_rgba[2], 140)
        _draw_centered_text(draw, lx, ly + 2, area, font_s, area_col)

    # ── 14. Axis labels ───────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    for gx in range(int(math.floor(min_x + margin)), int(math.ceil(max_x - margin)) + 1):
        px = int(tx(gx))
        _draw_centered_text(draw, px, H - 10, f"{gx}m", font_s, AXIS_COL)
    for gy in range(int(math.floor(min_y + margin)), int(math.ceil(max_y - margin)) + 1):
        py = int(ty(gy))
        _draw_centered_text(draw, 12, py, f"{gy}", font_s, AXIS_COL)

    # ── 15. Legend strip ──────────────────────────────────────────────────────
    _draw_legend(img, zones, font_s)

    # ── 16. Serialise ─────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ── Data extraction helpers ────────────────────────────────────────────────────


def _perimeter_points(zone: dict) -> list[Any]:
    """Return an ordered polygon of perimeter-pass waypoints for a zone.

    The segments in zone["presentation"] (type==0) may arrive in any order.
    Naively concatenating start/end points produces a self-intersecting
    polygon — the classic cause of a diagonal wedge drawn across the room.

    This function chains segments greedily (each segment's start is matched
    to the nearest unvisited endpoint) so the resulting point list forms a
    proper (or near-proper) closed polygon suitable for Pillow's polygon fill.
    """
    raw = [s for s in zone.get("presentation", []) if s.get("type") == 0
           and s.get("start") is not None and s.get("end") is not None]
    if not raw:
        return []

    # Convert to (start_xy, end_xy) pairs
    pairs: list[tuple[tuple, tuple]] = [
        (_pt(s["start"]), _pt(s["end"])) for s in raw
    ]

    # Greedy chain: pick the next segment whose start or end is closest
    # to the current tail point.
    chain: list[tuple[float, float]] = list(pairs[0])  # [start, end] of first seg
    remaining = list(pairs[1:])

    while remaining:
        tail = chain[-1]
        best_i, best_d, best_flip = 0, float("inf"), False
        for i, (s, e) in enumerate(remaining):
            ds = math.hypot(s[0] - tail[0], s[1] - tail[1])
            de = math.hypot(e[0] - tail[0], e[1] - tail[1])
            if ds < best_d:
                best_d, best_i, best_flip = ds, i, False
            if de < best_d:
                best_d, best_i, best_flip = de, i, True
        s, e = remaining.pop(best_i)
        if best_flip:
            chain.append(s)   # reversed: e was the close end, s is the far end
        else:
            chain.append(e)

    # Deduplicate adjacent identical points
    out: list[Any] = []
    for p in chain:
        if not out or p != out[-1]:
            out.append(p)
    return out


def _perimeter_segments(zone: dict) -> list[tuple[Any, Any]]:
    """Return each perimeter-pass segment as a (start, end) pair.

    Unlike _perimeter_points, this does NOT chain segments together,
    so non-contiguous perimeter runs never produce diagonal connector lines.
    """
    return [
        (seg["start"], seg["end"])
        for seg in zone.get("presentation", [])
        if seg.get("type") == 0
        and seg.get("start") is not None
        and seg.get("end") is not None
    ]


def _bounds_from_dimensions(map_data: dict) -> tuple:
    """Try to extract map extent from map_data['dimensions'].

    Returns (min_x, max_x, min_y, max_y) or (None, None, None, None).
    """
    dims = map_data.get("dimensions")
    if not dims or not isinstance(dims, dict):
        return None, None, None, None

    # Possible structures:
    # {"width": N_cells, "height": M_cells, "resolution": 0.05,
    #  "origin": {"x": min_x_m, "y": min_y_m}}
    res    = dims.get("resolution", 0.05)
    origin = dims.get("origin") or {}
    ox     = origin.get("x")
    oy     = origin.get("y")
    width  = dims.get("width")
    height = dims.get("height")

    if ox is not None and oy is not None and width and height:
        return (
            float(ox),
            float(ox) + float(width) * float(res),
            float(oy),
            float(oy) + float(height) * float(res),
        )
    return None, None, None, None


def _collect_all_coords(
    map_data: dict,
    live_data: dict | None,
    zones: list[dict],
) -> list[tuple[float, float]]:
    """Gather every coordinate in the map for bounds calculation."""
    pts: list[tuple[float, float]] = []

    for z in zones:
        loc = z.get("nameLocation") or {}
        if loc.get("x") or loc.get("y"):
            pts.append((float(loc["x"]), float(loc["y"])))
        for p in z.get("visited", []):
            pts.append(_pt(p))
        for seg in z.get("presentation", []):
            for field in ("start", "end"):
                p = seg.get(field)
                if p:
                    pts.append(_pt(p))

    for f in _get_furniture(map_data, live_data):
        for p in (f.get("points") or f.get("boundary") or []):
            pts.append(_pt(p))

    for r in _get_restrictions(map_data, live_data):
        for p in (r.get("points") or r.get("boundary") or []):
            pts.append(_pt(p))

    dock = _get_dock(map_data, live_data)
    if dock:
        pts.append((float(dock["x"]), float(dock["y"])))

    if live_data:
        for p in live_data.get("cleanPath", []):
            pts.append(_pt(p))
        robot = live_data.get("robotLocation")
        if robot:
            pts.append((float(robot["x"]), float(robot["y"])))

    return pts


def _get_restrictions(map_data: dict, live_data: dict | None) -> list[dict]:
    src = live_data if live_data else map_data
    rests = src.get("restrictions") or map_data.get("restrictions") or []
    return [r for r in rests if r.get("behavior", "keepOut") == "keepOut" or "keepOut" in str(r.get("behavior", ""))]


def _get_furniture(map_data: dict, live_data: dict | None) -> list[dict]:
    if live_data and live_data.get("furniture"):
        return live_data["furniture"]
    return map_data.get("furniture") or []


def _get_dock(map_data: dict, live_data: dict | None) -> dict | None:
    if live_data and live_data.get("dockLocation"):
        return live_data["dockLocation"]
    return map_data.get("dockLocation")


# ── Drawing helpers ────────────────────────────────────────────────────────────


def _poly_alpha(
    img: "Image.Image",
    pts: list[tuple[int, int]],
    fill: tuple,
    stroke: tuple,
    stroke_width: int = 1,
    dashed: bool = False,
) -> None:
    if len(pts) < 3:
        return
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).polygon(pts, fill=fill)
    img.alpha_composite(overlay)

    if dashed:
        _dashed_polygon(img, pts, stroke, stroke_width)
    else:
        draw = ImageDraw.Draw(img)
        closed = pts + [pts[0]]
        for i in range(len(closed) - 1):
            draw.line([closed[i], closed[i + 1]], fill=stroke, width=stroke_width)


def _dashed_polygon(img, pts, colour, width=1, dash=6, gap=4):
    closed = pts + [pts[0]]
    for i in range(len(closed) - 1):
        _dashed_seg(img, closed[i], closed[i + 1], colour, width, dash, gap)


def _dashed_seg(img, p1, p2, colour, width=1, dash=6, gap=4):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    draw = ImageDraw.Draw(img)
    pos, on = 0.0, True
    while pos < length:
        seg = dash if on else gap
        end = min(pos + seg, length)
        if on:
            draw.line(
                [(int(p1[0] + ux * pos), int(p1[1] + uy * pos)),
                 (int(p1[0] + ux * end), int(p1[1] + uy * end))],
                fill=colour, width=width,
            )
        pos, on = end, not on


def _ellipse_alpha(img, cx, cy, r, colour):
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    img.alpha_composite(overlay)


def _draw_legend(img: "Image.Image", zones: list[dict], font: Any) -> None:
    """Small colour legend at the bottom-left corner."""
    if not zones or not font:
        return
    draw = ImageDraw.Draw(img)
    x0, y0 = 8, img.height - 8 - len(zones) * 14
    for i, z in enumerate(zones):
        col = ZONE_COLOURS[i % len(ZONE_COLOURS)]
        stroke_rgb = col[0]
        colour = (stroke_rgb[0], stroke_rgb[1], stroke_rgb[2], 200)
        name = z.get("name", f"Zone {z.get('id', i)}")
        y = y0 + i * 14
        draw.rectangle([x0, y + 2, x0 + 8, y + 10], fill=colour)
        _draw_centered_text(draw, x0 + 34, y + 6, name, font, colour)


# ── Utilities ──────────────────────────────────────────────────────────────────


def _pt(pt: Any) -> tuple[float, float]:
    """Normalise a point to (x, y). Accepts dict or sequence."""
    if isinstance(pt, dict):
        return float(pt["x"]), float(pt["y"])
    return float(pt[0]), float(pt[1])


def _load_fonts() -> tuple[Any, Any]:
    try:
        return ImageFont.load_default(size=11), ImageFont.load_default(size=9)
    except TypeError:
        pass
    try:
        f = ImageFont.load_default()
        return f, f
    except Exception:
        return None, None


def _draw_centered_text(draw, x, y, text, font, colour):
    if not font or not text:
        return
    try:
        draw.text((x, y), text, fill=colour, font=font, anchor="mm")
        return
    except TypeError:
        pass
    try:
        bb = font.getbbox(text)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
    except AttributeError:
        tw, th = len(text) * 6, 10
    draw.text((x - tw // 2, y - th // 2), text, fill=colour, font=font)


# ── Error card ─────────────────────────────────────────────────────────────────


def _error_image(msg: str) -> bytes:
    try:
        img = Image.new("RGB", (500, 80), (15, 23, 42))
        draw = ImageDraw.Draw(img)
        font = None
        try:
            font = ImageFont.load_default()
        except Exception:
            pass
        draw.text((10, 30), f"Dyson map: {msg}", fill=(239, 68, 68), font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return b""
