"""Pillow-based renderer for the Dyson floor map → PNG bytes.

Mirrors the rendering logic from floormap.html: dark navy background,
1 m grid, zone boundary polygons coloured by clean status, furniture
outlines, keep-out zones, dock, robot position, and live clean path.
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


# ── Pixels per metre and canvas padding ───────────────────────────────────────

SCALE = 72   # pixels per metre (matches floormap.html scale: 72)
PAD   = 70   # blank border around the map, in pixels


# ── Colour palette (RGBA tuples, 0-255) ───────────────────────────────────────

BG               = (15,  23,  42,  255)   # #0f172a  dark navy
GRID             = (255, 255, 255, 13)    # 5 % white
ROOM_FILL        = (56,  189, 248, 20)    # #38bdf8  8 % fill
ROOM_STROKE      = (56,  189, 248, 255)   # #38bdf8
DIVIDER          = (56,  189, 248, 89)    # #38bdf8  35 %
FURN_U_FILL      = (99,  102, 241, 64)    # #6366f1  user-taught  25 %
FURN_U_STROKE    = (129, 140, 248, 255)   # #818cf8
FURN_A_FILL      = (245, 158, 11,  64)    # #f59e0b  auto-detect  25 %
FURN_A_STROKE    = (245, 158, 11,  255)
KEEPOUT_FILL     = (239, 68,  68,  51)    # #ef4444  20 %
KEEPOUT_STROKE   = (239, 68,  68,  255)
DOCK             = (52,  211, 153, 255)   # #34d399  green
ROBOT            = (250, 204, 21,  255)   # #facc15  yellow
ROBOT_HALO       = (250, 204, 21,  64)    # 25 %
PATH_MAIN        = (34,  211, 238, 255)   # #22d3ee  cyan
PATH_GLOW        = (34,  211, 238, 64)    # 25 %
VISITED          = (56,  189, 248, 89)    # dashed blue
AXIS_COL         = (71,  85,  105, 255)   # #475569  slate
TEXT_COL         = (226, 232, 240, 255)   # #e2e8f0
MUTED_COL        = (148, 163, 184, 255)   # #94a3b8
ORIGIN_COL       = (255, 255, 255, 51)    # 20 %

# Per-status fill and stroke applied to zone boundary polygons
_STATUS_FILL: dict[str, tuple] = {
    "CLEAN_NOT_REQUESTED": (56,  189, 248, 20),
    "CLEAN_PENDING":       (56,  189, 248, 89),
    "CLEAN_IN_PROGRESS":   (74,  222, 128, 102),
    "CLEANING":            (74,  222, 128, 102),
    "CLEAN_COMPLETE":      (134, 239, 172, 77),
}
_STATUS_STROKE: dict[str, tuple] = {
    "CLEAN_NOT_REQUESTED": (56,  189, 248, 115),
    "CLEAN_PENDING":       (56,  189, 248, 204),
    "CLEAN_IN_PROGRESS":   (74,  222, 128, 230),
    "CLEANING":            (74,  222, 128, 230),
    "CLEAN_COMPLETE":      (134, 239, 172, 178),
}
_STATUS_LABEL: dict[str, tuple] = {
    "CLEAN_NOT_REQUESTED": (56,  189, 248, 115),
    "CLEAN_PENDING":       (56,  189, 248, 204),
    "CLEAN_IN_PROGRESS":   (74,  222, 128, 230),
    "CLEANING":            (74,  222, 128, 230),
    "CLEAN_COMPLETE":      (134, 239, 172, 178),
}


# ── Public API ────────────────────────────────────────────────────────────────


def render_map(
    map_data:  dict[str, Any] | None,
    metadata:  list[dict[str, Any]] | None,
    live_data: dict[str, Any] | None = None,
) -> bytes:
    """Render a Dyson floor map to PNG bytes.

    Parameters
    ----------
    map_data:
        Response from ``get_map()`` — zone boundaries, furniture, restrictions,
        dockLocation.
    metadata:
        Response from ``get_map_metadata()`` — zone names, areas, label
        coordinates (nameLocation).
    live_data:
        Response from ``get_live_map()`` — optional; robot position, clean path,
        per-zone clean status. Pass ``None`` when the robot is not cleaning.

    Returns
    -------
    Raw PNG bytes.  Never raises — returns an error-card image on failure.
    """
    if not _PIL_AVAILABLE:
        return _error_image("Pillow not installed — add 'Pillow' to requirements")

    try:
        return _render(map_data, metadata, live_data)
    except Exception as exc:  # pylint: disable=broad-except
        return _error_image(f"Map render error: {exc}")


# ── Internal rendering ────────────────────────────────────────────────────────


def _render(
    map_data:  dict[str, Any] | None,
    metadata:  list[dict[str, Any]] | None,
    live_data: dict[str, Any] | None,
) -> bytes:
    # ── 1. Collect all boundary points to compute canvas bounds ───────────────
    zones_from_map: list[dict] = []
    all_pts: list[tuple[float, float]] = []

    if map_data:
        for z in map_data.get("zones", []):
            zones_from_map.append(z)
            for pt in z.get("boundary", []):
                all_pts.append(_pt(pt))
        # Fallback: top-level boundary field
        if not all_pts:
            for pt in map_data.get("boundary", []):
                all_pts.append(_pt(pt))

    if not all_pts:
        return _error_image("No boundary data — ensure the map has been built")

    min_x = min(p[0] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_y = max(p[1] for p in all_pts)

    # Canvas size in pixels
    W = int((max_x - min_x) * SCALE + PAD * 2) + 2
    H = int((max_y - min_y) * SCALE + PAD * 2) + 2

    # ── Coordinate helpers ────────────────────────────────────────────────────
    def tx(x: float) -> float:
        return (x - min_x) * SCALE + PAD

    def ty(y: float) -> float:
        # Y-axis is inverted in Dyson's coordinate system
        return (max_y - y) * SCALE + PAD

    def txy(pt: Any) -> tuple[int, int]:
        x, y = _pt(pt)
        return int(tx(x)), int(ty(y))

    def txy_list(pts: list) -> list[tuple[int, int]]:
        return [txy(p) for p in pts]

    # ── 2. Build lookup tables ────────────────────────────────────────────────

    # zone_id -> {name, type, area, x (label), y (label)}
    zone_info: dict[str, dict] = {}
    if metadata:
        for m in metadata:
            for z in m.get("zones", []):
                zid = str(z.get("id", ""))
                if not zid:
                    continue
                loc = z.get("nameLocation") or {}
                zone_info[zid] = {
                    "name": z.get("name", ""),
                    "type": z.get("type", ""),
                    "area": float(z.get("area", 0)),
                    "lx":   float(loc.get("x", 0)),
                    "ly":   float(loc.get("y", 0)),
                }

    # zone_id -> clean status string
    zone_status: dict[str, str] = {}
    if live_data:
        for z in live_data.get("zones", []):
            zid = str(z.get("id") or z.get("zoneId") or "")
            if zid:
                zone_status[zid] = z.get("cleanStatus", "CLEAN_NOT_REQUESTED")

    # ── 3. Create RGBA base image ─────────────────────────────────────────────
    img = Image.new("RGBA", (W, H), BG)

    # ── 4. Grid lines (every 1 m) ─────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    for gx in range(int(math.floor(min_x)), int(math.ceil(max_x)) + 1):
        px = int(tx(gx))
        draw.line([(px, 0), (px, H)], fill=GRID)
    for gy in range(int(math.floor(min_y)), int(math.ceil(max_y)) + 1):
        py = int(ty(gy))
        draw.line([(0, py), (W, py)], fill=GRID)

    # ── 5. Zone boundary polygons ─────────────────────────────────────────────
    for z in zones_from_map:
        bnd = z.get("boundary", [])
        if len(bnd) < 3:
            continue
        zid = str(z.get("id", ""))
        status = zone_status.get(zid, "CLEAN_NOT_REQUESTED")
        fill   = _STATUS_FILL.get(status, ROOM_FILL)
        stroke = _STATUS_STROKE.get(status, ROOM_STROKE)
        pts = txy_list(bnd)
        _poly_alpha(img, pts, fill, stroke, stroke_width=2)

    # ── 6. Keep-out zones ─────────────────────────────────────────────────────
    # From persistent map
    for z in zones_from_map:
        for r in z.get("restrictions", []):
            bnd = r.get("boundary") or r.get("points") or []
            if len(bnd) >= 3:
                _poly_alpha(img, txy_list(bnd), KEEPOUT_FILL, KEEPOUT_STROKE,
                            stroke_width=1, dashed=True)

    # From live map
    if live_data:
        for r in live_data.get("restrictions", []):
            if r.get("behavior") == "keepOut":
                bnd = r.get("points") or r.get("boundary") or []
                if len(bnd) >= 3:
                    _poly_alpha(img, txy_list(bnd), KEEPOUT_FILL, KEEPOUT_STROKE,
                                stroke_width=1, dashed=True)

    # ── 7. Furniture ──────────────────────────────────────────────────────────
    furniture: list[dict] = []
    if live_data:
        furniture = live_data.get("furniture", [])
    if not furniture:
        # Fall back to persistent map furniture
        for z in zones_from_map:
            furniture.extend(z.get("furnitureItems", []))

    for f in furniture:
        bnd = f.get("points") or f.get("boundary") or []
        if len(bnd) < 3:
            continue
        pts = txy_list(bnd)
        if f.get("userDefined", False):
            _poly_alpha(img, pts, FURN_U_FILL, FURN_U_STROKE)
        else:
            _poly_alpha(img, pts, FURN_A_FILL, FURN_A_STROKE)

    # ── 8. Historical visited paths (per zone, dashed) ────────────────────────
    if live_data:
        for z in live_data.get("zones", []):
            visited = z.get("visited", [])
            if len(visited) > 1:
                _dashed_polyline(img, txy_list(visited), VISITED, width=1)

    # ── 9. Live clean path ────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    if live_data:
        cp = live_data.get("cleanPath", [])
        if len(cp) > 1:
            pts = [txy(p) for p in cp]
            # Glow layer
            for i in range(len(pts) - 1):
                draw.line([pts[i], pts[i + 1]], fill=PATH_GLOW, width=6)
            # Main trail
            for i in range(len(pts) - 1):
                draw.line([pts[i], pts[i + 1]], fill=PATH_MAIN, width=3)
            # Direction dots every 5 points
            for i in range(4, len(pts), 5):
                x, y = pts[i]
                draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=PATH_MAIN)

    # ── 10. Dock position ─────────────────────────────────────────────────────
    dock = None
    if live_data:
        dock = live_data.get("dockLocation")
    if dock is None and map_data:
        dock = map_data.get("dockLocation")

    if dock:
        dx, dy = int(tx(dock["x"])), int(ty(dock["y"]))
        draw.ellipse([dx - 8, dy - 8, dx + 8, dy + 8], fill=DOCK)

    # ── 11. Robot position + heading arrow ────────────────────────────────────
    if live_data:
        robot = live_data.get("robotLocation")
        if robot:
            rx, ry = int(tx(robot["x"])), int(ty(robot["y"]))
            angle  = float(robot.get("angle", 0.0))
            # Halo (semi-transparent)
            _ellipse_alpha(img, rx, ry, 20, ROBOT_HALO)
            draw = ImageDraw.Draw(img)
            # Robot circle
            draw.ellipse([rx - 8, ry - 8, rx + 8, ry + 8], fill=ROBOT)
            # Heading arrow — angle is CCW from East in Dyson coords,
            # but on-screen Y is flipped so negate the Y component
            arrow_len = 14
            ax = rx + int(arrow_len * math.cos(angle))
            ay = ry - int(arrow_len * math.sin(angle))
            draw.line([(rx, ry), (ax, ay)], fill=BG, width=2)

    # ── 12. Origin marker ─────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    ox, oy = int(tx(0)), int(ty(0))
    draw.ellipse([ox - 4, oy - 4, ox + 4, oy + 4],
                 fill=None, outline=ORIGIN_COL, width=1)

    # ── 13. Zone labels ───────────────────────────────────────────────────────
    font_label, font_area = _load_fonts()

    for zid, info in zone_info.items():
        if not info["lx"] and not info["ly"]:
            continue
        lx = int(tx(info["lx"]))
        ly = int(ty(info["ly"]))
        status = zone_status.get(zid, "CLEAN_NOT_REQUESTED")
        label_col = _STATUS_LABEL.get(status, ROOM_STROKE)
        area_col  = (label_col[0], label_col[1], label_col[2], 140)

        name = info["name"].upper()
        area = f"{info['area']:.1f} m²"

        _draw_centered_text(draw, lx, ly - 14, name, font_label, label_col)
        _draw_centered_text(draw, lx, ly,       area, font_area,  area_col)

    # ── 14. Axis labels ───────────────────────────────────────────────────────
    font_axis = font_area
    for gx in range(int(math.floor(min_x)), int(math.ceil(max_x)) + 1):
        px = int(tx(gx))
        _draw_centered_text(draw, px, H - 10, f"{gx}m", font_axis, AXIS_COL)
    for gy in range(int(math.floor(min_y)), int(math.ceil(max_y)) + 1):
        py = int(ty(gy))
        _draw_centered_text(draw, 12, py, f"{gy}", font_axis, AXIS_COL)

    # ── 15. Serialise to PNG ──────────────────────────────────────────────────
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ── Drawing helpers ───────────────────────────────────────────────────────────


def _poly_alpha(
    img: "Image.Image",
    pts: list[tuple[int, int]],
    fill: tuple,
    stroke: tuple,
    stroke_width: int = 1,
    dashed: bool = False,
) -> None:
    """Draw a filled + outlined polygon, compositing the transparent fill."""
    # Filled layer on a blank overlay so alpha compositing works
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).polygon(pts, fill=fill)
    img.alpha_composite(overlay)

    # Stroke
    if dashed:
        _dashed_polygon(img, pts, stroke, stroke_width)
    else:
        draw = ImageDraw.Draw(img)
        closed = pts + [pts[0]]
        for i in range(len(closed) - 1):
            draw.line([closed[i], closed[i + 1]], fill=stroke, width=stroke_width)


def _dashed_polygon(
    img: "Image.Image",
    pts: list[tuple[int, int]],
    colour: tuple,
    width: int = 1,
    dash: int = 6,
    gap: int = 4,
) -> None:
    closed = pts + [pts[0]]
    for i in range(len(closed) - 1):
        _dashed_seg(img, closed[i], closed[i + 1], colour, width, dash, gap)


def _dashed_polyline(
    img: "Image.Image",
    pts: list[tuple[int, int]],
    colour: tuple,
    width: int = 1,
    dash: int = 6,
    gap: int = 4,
) -> None:
    for i in range(len(pts) - 1):
        _dashed_seg(img, pts[i], pts[i + 1], colour, width, dash, gap)


def _dashed_seg(
    img: "Image.Image",
    p1: tuple[int, int],
    p2: tuple[int, int],
    colour: tuple,
    width: int = 1,
    dash: int = 6,
    gap: int = 4,
) -> None:
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    draw = ImageDraw.Draw(img)
    pos = 0.0
    on  = True
    while pos < length:
        seg = dash if on else gap
        end = min(pos + seg, length)
        if on:
            x1 = int(p1[0] + ux * pos)
            y1 = int(p1[1] + uy * pos)
            x2 = int(p1[0] + ux * end)
            y2 = int(p1[1] + uy * end)
            draw.line([(x1, y1), (x2, y2)], fill=colour, width=width)
        pos = end
        on  = not on


def _ellipse_alpha(
    img: "Image.Image",
    cx: int,
    cy: int,
    r: int,
    colour: tuple,
) -> None:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    img.alpha_composite(overlay)


def _pt(pt: Any) -> tuple[float, float]:
    """Normalise a point to (x, y).  Accepts dict or [x, y] sequence."""
    if isinstance(pt, dict):
        return float(pt["x"]), float(pt["y"])
    return float(pt[0]), float(pt[1])


# ── Font loading ──────────────────────────────────────────────────────────────


def _load_fonts() -> tuple[Any, Any]:
    """Return (label_font, area_font).  Falls back to PIL default if needed."""
    try:
        big   = ImageFont.load_default(size=11)
        small = ImageFont.load_default(size=9)
        return big, small
    except TypeError:
        # Pillow < 9.2.0 — load_default() takes no arguments
        pass
    try:
        font = ImageFont.load_default()
        return font, font
    except Exception:
        return None, None


def _draw_centered_text(
    draw: "ImageDraw.ImageDraw",
    x: int,
    y: int,
    text: str,
    font: Any,
    colour: tuple,
) -> None:
    """Draw text centred on (x, y), with graceful fallback for older Pillow."""
    if font is None or not text:
        return
    try:
        draw.text((x, y), text, fill=colour, font=font, anchor="mm")
        return
    except TypeError:
        pass  # anchor not supported in this version of Pillow
    # Fallback: estimate width and offset manually
    try:
        bbox = font.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except AttributeError:
        tw, th = len(text) * 6, 10
    draw.text((x - tw // 2, y - th // 2), text, fill=colour, font=font)


# ── Error card ────────────────────────────────────────────────────────────────


def _error_image(msg: str) -> bytes:
    """Return a minimal 400×80 error PNG."""
    try:
        img = Image.new("RGB", (400, 80), (15, 23, 42))
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
