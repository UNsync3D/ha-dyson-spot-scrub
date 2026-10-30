"""Pillow-based renderer for the Dyson floor map → PNG bytes.

Confirmed API structure (from live HA debug logs, Sep 2026):

  map_data (from get_map / get_current_map):
    id, orientation, dimensions, dockLocation, furniture, restrictions,
    hazardZones
    zones[]: id, name, type, nameLocation {x,y}, area, cleanStatus,
             visited [{x,y}]  ← despite the name, a closed outline (points
               every 5 cm) of the floor the robot can reach in the room: it
               follows open walls but goes around furniture
             presentation [{start:{x,y}, end:{x,y}, type:int}]  ← the room's
               simplified, straight-line outline:
               type 0 = wall edge
               type 1 = boundary shared with another room - a real interior
                        wall OR a virtual split; the data doesn't say which
               type 2 = short join between a wall and a shared boundary
             (Types from a room split in the MyDyson app, 28 Oct 2026, where
             the split line appeared as type 1 in both rooms; FutuRazor's map
             showed type 1 on real interior walls too.)

  live_data (from get_live_map, only while robot is cleaning):
    robotLocation {x, y, angle}
    cleanPath [{x,y} | {x,y,update}]
    dockLocation {x, y, angle}
    zones[]: same as above but current-session status/visited/presentation
    furniture [], restrictions [], dirt [], hazardZones []

Zone boundary polygons are NOT in the REST API as explicit polygons.
We reconstruct them from the wall (type=0) presentation segments.
"""
from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from .const import MAP_PATH_ACTIONS
from .map_options import map_options

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ── Canvas constants ───────────────────────────────────────────────────────────

SCALE = 80   # pixels per metre
PAD   = 40   # border padding in pixels

# Upper bound for either canvas side. Map coordinates come from Dyson's API;
# without a cap a single bogus point (e.g. x = 1e6 m) would make Pillow
# allocate a multi-gigabyte image and could take Home Assistant down. Larger
# maps are scaled down to fit instead (4096 px ≈ 50 m at the default scale).
MAX_CANVAS = 4096


# ── Colour palette ─────────────────────────────────────────────────────────────

BG             = (6,   6,   8,   255)   # near-black
DOCK           = (52,  211, 153, 255)   # #34d399 emerald green
ROBOT          = (245, 158, 11,  255)   # #f59e0b amber
ROBOT_HALO     = (245, 158, 11,  50)
PATH_MAIN      = (34,  211, 238, 255)   # #22d3ee cyan
PATH_UNKNOWN   = (160, 160, 168, 255)
KEEPOUT_FILL   = (245, 158, 11,  35)    # amber tint
KEEPOUT_STROKE = (245, 158, 11,  220)   # amber
FURN_FILL      = (55,  60,  70,  255)   # dark grey
FURN_STROKE    = (90,  95,  110, 200)   # slightly lighter grey

# Zone colours — single style for all zones (Dyson app aesthetic)
ZONE_BORDER    = (220, 220, 225, 180)   # soft white outline
WALL           = (232, 232, 238, 255)   # solid wall (presentation types 0 and 2)
DIVIDER        = (200, 200, 208, 170)   # open boundary between rooms (type 1), dashed
# Option map_floor_fill: colours measured from a MyDyson app screenshot
ROOM_FILL      = (37,  37,  40,  255)   # room area the robot can't reach
FLOOR_FILL     = (18,  18,  20,  255)   # floor the robot reaches
APP_WALL       = (147, 147, 147, 255)   # walls in the app's grey
APP_DIVIDER    = (147, 147, 147, 210)
WALL_WIDTH_M   = 0.05                   # drawn wall thickness in metres
DIVIDER_WIDTH_M = 0.025                 # shared boundaries: half a wall
MERGE_TOLERANCE_M = 0.08                # same boundary from two rooms: merge

# Room colours (option map_room_colors), chosen to stay distinct from the
# default path colours (yellow / cyan / purple). Assigned by room ID, so a
# room keeps its colour when rooms are renamed or reordered.
ROOM_PALETTE = [
    (96, 165, 250),    # blue
    (74, 222, 128),    # green
    (251, 146, 60),    # orange
    (244, 114, 182),   # pink
    (45, 212, 191),    # teal
    (163, 230, 53),    # lime
    (248, 113, 113),   # red
    (129, 140, 248),   # indigo
    (232, 190, 120),   # sand
    (148, 163, 184),   # slate
]
FONT_FILE = Path(__file__).with_name("fonts") / "DejaVuSans.ttf"
ZONE_VISITED   = (160, 160, 168, 60)    # subtle grey visited path
ZONE_LABEL     = (220, 220, 225, 220)   # white label text


# ── Public API ─────────────────────────────────────────────────────────────────


def render_map(
    map_data:  dict[str, Any] | None,
    metadata:  list[dict[str, Any]] | None,
    live_data: dict[str, Any] | None = None,
    options: dict | None = None,
    *,
    raise_errors: bool = False,
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
        return _render(map_data, metadata, live_data, map_options(options))
    except Exception as exc:  # pylint: disable=broad-except
        if raise_errors:
            raise
        return _error_image(f"Render error: {exc}")


# ── Rendering ──────────────────────────────────────────────────────────────────


def _render(
    map_data:  dict[str, Any],
    metadata:  list[dict[str, Any]] | None,
    live_data: dict[str, Any] | None,
    options: dict,
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

    span = max(max_x - min_x, max_y - min_y)
    if not math.isfinite(span):
        return _error_image("Map coordinates are invalid")
    scale = min(SCALE, (MAX_CANVAS - PAD * 2 - 2) / span)

    W = int((max_x - min_x) * scale + PAD * 2) + 2
    H = int((max_y - min_y) * scale + PAD * 2) + 2

    # ── Coordinate helpers ────────────────────────────────────────────────────
    def tx(x: float) -> float:
        return (x - min_x) * scale + PAD

    def ty(y: float) -> float:
        # Dyson Y-axis is inverted (positive = north on the real floor but
        # down in raw coords) — negate to flip for screen.
        return (max_y - y) * scale + PAD

    def txy(pt: Any) -> tuple[int, int]:
        x, y = _pt(pt)
        return int(tx(x)), int(ty(y))

    def txy_list(pts: list) -> list[tuple[int, int]]:
        return [txy(p) for p in pts]

    # ── 3. Create base image ──────────────────────────────────────────────────
    img = Image.new("RGBA", (W, H),
                    (0, 0, 0, 0) if options["map_transparent_background"] else BG)
    if options["map_show_grid"]:
        _draw_grid(img, tx, ty, min_x, max_x, min_y, max_y)
    if options["map_show_status_areas"]:
        for zone in zones:
            points = _perimeter_points(zone)
            if len(points) >= 3:
                fill = STATUS_COLORS.get(zone.get("cleanStatus"))
                if fill:
                    _poly_alpha(img, txy_list(points), fill, (0, 0, 0, 0))

    # ── 4. Floor fill (option map_floor_fill) ─────────────────────────────────
    room_colors = options["map_room_colors"]
    map_id = map_data.get("id")
    if options["map_floor_fill"]:
        # Like the MyDyson app: the floor the robot can reach dark, the rest
        # of the room lighter, so furniture shows as grey blocks.
        for z in zones:
            room = _room_polygon(z)
            if len(room) >= 3:
                _poly_alpha(img, txy_list(room), ROOM_FILL, (0, 0, 0, 0))
        for z in zones:
            visited = [_pt(p) for p in z.get("visited") or []]
            if len(visited) >= 3:
                fill = (_tint(FLOOR_FILL, _room_color(z.get("id"), map_id), 0.22)
                        if room_colors else FLOOR_FILL)
                _poly_alpha(img, [txy(p) for p in visited], fill, (0, 0, 0, 0))

    # ── 5. Keep-out zones, floor outlines, then walls and room boundaries ──
    draw = ImageDraw.Draw(img)
    for r in (_get_restrictions(map_data, live_data) if options["map_show_restrictions"] else []):
        bnd = r.get("points") or r.get("boundary") or []
        if len(bnd) >= 3:
            _poly_alpha(img, txy_list(bnd), KEEPOUT_FILL, KEEPOUT_STROKE,
                        stroke_width=1, dashed=True)
    for z in zones:
        # Accessible-floor outline ('visited') - skip jumps between loops
        visited = z.get("visited", [])
        if options["map_show_visited_paths"] and len(visited) > 1:
            draw = ImageDraw.Draw(img)
            if room_colors:
                outline_col, outline_w = _room_color(z.get("id"), map_id) + (220,), 2
            else:
                outline_col, outline_w = ZONE_VISITED, 1
            vpts = [_pt(p) for p in visited]
            for j in range(len(vpts) - 1):
                x0, y0 = vpts[j]
                x1, y1 = vpts[j + 1]
                if math.hypot(x1 - x0, y1 - y0) > 0.8:
                    continue
                draw.line([txy(vpts[j]), txy(vpts[j + 1])], fill=outline_col,
                          width=outline_w)

    # Walls solid: type 0 (outer) and type 2 (a wall between two rooms).
    # Type 1 is where two rooms' floors meet (open plan, doorway or a split
    # made in the app): thinner, lighter and dashed, as the MyDyson app
    # draws it. Each boundary is drawn ONCE: both
    # neighbouring rooms list it, often a few centimetres apart, and drawing
    # both looked doubled. Walls of any length are drawn (an earlier 1.5 m
    # cut-off dropped most real walls).
    if options["map_show_perimeter"]:
        wall_width = max(2, round(WALL_WIDTH_M * scale))
        divider_width = max(1, round(DIVIDER_WIDTH_M * scale))
        walls, shared = [], []
        for z in zones:
            for kind, seg_start, seg_end in _outline_segments(z):
                (shared if kind == 1 else walls).append((seg_start, seg_end))
        # App grey with the floor fill; white otherwise, and with room
        # colours on (white walls keep the coloured rooms easy to read).
        app_grey = options["map_floor_fill"] and not room_colors
        wall_col, divider_col = (APP_WALL, APP_DIVIDER) if app_grey else (WALL, DIVIDER)
        merged_walls = _merge_segments(walls)
        for seg_start, seg_end in _merge_segments(shared):
            for part in _subtract_segments((seg_start, seg_end), merged_walls):
                _dashed_seg(img, txy(part[0]), txy(part[1]), divider_col,
                            divider_width, dash=7, gap=5)
        draw = ImageDraw.Draw(img)
        for seg_start, seg_end in merged_walls:
            draw.line([txy(seg_start), txy(seg_end)], fill=wall_col, width=wall_width)

    # ── 6. Furniture ──────────────────────────────────────────────────────────
    for f in (_get_furniture(map_data, live_data) if options["map_show_furniture"] else []):
        bnd = f.get("points") or f.get("boundary") or []
        if len(bnd) >= 3:
            _poly_alpha(img, txy_list(bnd), FURN_FILL, FURN_STROKE)

    # ── 7. Live clean path ────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    if live_data:
        cp = live_data.get("cleanPath", [])
        if len(cp) > 1:
            pts2 = [txy(p) for p in cp]
            modes = live_data.get("_pathModes", [None] * (len(pts2) - 1))
            colors = [tuple(options[MAP_PATH_ACTIONS[mode]]) + (255,)
                      if mode in MAP_PATH_ACTIONS else PATH_UNKNOWN for mode in modes]
            if len(colors) != len(pts2) - 1:
                colors = [PATH_UNKNOWN] * (len(pts2) - 1)
            if options["map_glow_width"] > 0:
                glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
                glow_draw = ImageDraw.Draw(glow)
                for j, color in enumerate(colors):
                    glow_draw.line([pts2[j], pts2[j + 1]], fill=color[:3] + (50,),
                                   width=options["map_glow_width"])
                img.alpha_composite(glow)
            draw = ImageDraw.Draw(img)
            for j, color in enumerate(colors):
                draw.line([pts2[j], pts2[j + 1]], fill=color,
                          width=options["map_path_width"])
            if options["map_show_waypoints"]:
                for j in range(4, len(pts2), 5):
                    x, y = pts2[j]
                    draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=colors[j - 1])

    # ── 8. Dock ───────────────────────────────────────────────────────────────
    dock = _get_dock(map_data, live_data)
    if dock and options["map_show_dock"]:
        dx, dy = int(tx(dock["x"])), int(ty(dock["y"]))
        draw = ImageDraw.Draw(img)
        draw.ellipse([dx - 9, dy - 9, dx + 9, dy + 9], fill=DOCK)
        font_s, _ = _load_fonts()
        _draw_centered_text(draw, dx, dy, "D", font_s, BG)

    # ── 9. Robot position + heading ───────────────────────────────────────────
    if live_data:
        robot = live_data.get("robotLocation")
        if robot:
            rx, ry = int(tx(robot["x"])), int(ty(robot["y"]))
            angle  = float(robot.get("angle", 0.0))
            _ellipse_alpha(img, rx, ry, 20, ROBOT_HALO)
            draw = ImageDraw.Draw(img)
            draw.ellipse([rx - 9, ry - 9, rx + 9, ry + 9], fill=ROBOT)
            # Heading arrow
            ax = rx + int(16 * math.cos(angle))
            ay = ry - int(16 * math.sin(angle))
            draw.line([(rx, ry), (ax, ay)], fill=BG, width=2)

    # ── 10. Zone labels ───────────────────────────────────────────────────────
    font_l, font_s = _load_fonts()
    draw = ImageDraw.Draw(img)
    area_label_col = (ZONE_LABEL[0], ZONE_LABEL[1], ZONE_LABEL[2], 130)
    for z in zones:
        loc = z.get("nameLocation") or {}
        lx_m = loc.get("x", 0.0)
        ly_m = loc.get("y", 0.0)
        if not lx_m and not ly_m:
            continue
        lx, ly = int(tx(lx_m)), int(ty(ly_m))
        draw = ImageDraw.Draw(img)
        name = (z.get("name") or "").upper()
        label_col, area_col = ZONE_LABEL, area_label_col
        if options["map_room_colors"]:
            label_col = _room_color(z.get("id"), map_id) + (255,)
            area_col = _room_color(z.get("id"), map_id) + (190,)
        if options["map_show_room_names"]:
            _draw_centered_text(draw, lx, ly - 12, name, font_l, label_col)
        if options["map_show_room_areas"]:
            _draw_area_label(draw, lx, ly + 2, z.get("area", 0), font_s, area_col)
    if options["map_show_legend"]:
        _draw_legend(img, zones, font_s, options["map_room_colors"], map_id)

    # ── 11. Serialise ─────────────────────────────────────────────────────────
    buf = io.BytesIO()
    output = img if options["map_transparent_background"] else img.convert("RGB")
    output.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# Optional layers do not change the canvas dimensions or coordinate transform.
STATUS_COLORS = {
    "CLEAN_COMPLETE": (134, 239, 172, 100),
    "CLEAN_IN_PROGRESS": (74, 222, 128, 100),
    "CLEANING": (74, 222, 128, 100),
    "CLEAN_PENDING": (56, 189, 248, 60),
    "CLEAN_NOT_REQUESTED": (0, 0, 0, 0),
}


def _draw_grid(img, tx, ty, min_x, max_x, min_y, max_y):
    """Draw metre grid, distance labels and origin without resizing the map."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _, font = _load_fonts()
    # One line per metre, thinned out on very large (or corrupt) maps so the
    # loop stays bounded at roughly 100 lines per axis.
    step = max(1, math.ceil(max(max_x - min_x, max_y - min_y) / 100))
    for x in range(math.ceil(min_x), math.floor(max_x) + 1, step):
        px = int(tx(x))
        draw.line([(px, int(ty(max_y))), (px, int(ty(min_y)))],
                  fill=(148, 163, 184, 45), width=1)
        _draw_centered_text(draw, px, img.height - 16, f"{x} m", font, ZONE_LABEL)
    for y in range(math.ceil(min_y), math.floor(max_y) + 1, step):
        py = int(ty(y))
        draw.line([(int(tx(min_x)), py), (int(tx(max_x)), py)],
                  fill=(148, 163, 184, 45), width=1)
        _draw_centered_text(draw, 22, py, f"{y} m", font, ZONE_LABEL)
    if min_x <= 0 <= max_x and min_y <= 0 <= max_y:
        ox, oy = int(tx(0)), int(ty(0))
        draw.ellipse((ox - 4, oy - 4, ox + 4, oy + 4), outline=ZONE_LABEL)
    img.alpha_composite(overlay)


def _draw_legend(img, zones, font, room_colors=False, map_id=None):
    """Place labels inside the existing image; never alter overlay alignment."""
    draw = ImageDraw.Draw(img)
    y = 14
    for zone in zones:
        if y + 18 > img.height:
            break
        if room_colors:
            color = _room_color(zone.get("id"), map_id)
        else:
            color = STATUS_COLORS.get(zone.get("cleanStatus"), ZONE_VISITED)
        draw.rectangle((12, y, 20, y + 8), fill=color[:3] + (255,))
        name = str(zone.get("name") or zone.get("id") or "Room")
        # Long labels stay within the canvas, rather than changing its extent.
        while len(name) > 1 and draw.textbbox((0, 0), name, font=font)[2] > img.width - 42:
            name = name[:-2] + "…"
        draw.text((28, y - 2), name, fill=ZONE_LABEL, font=font)
        y += 18


def _room_color(room_id: Any, map_id: Any = None) -> tuple[int, int, int]:
    """A room's colour, fixed by its map ID and room ID.

    Dyson numbers rooms 10, 11, 12 ... on every map, so the map ID shifts the
    palette: rooms on one map keep different colours, and two maps don't
    share the same colour scheme. Non-numeric IDs are hashed.
    """
    def number(value: Any) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return sum(ord(c) * 31 ** i for i, c in enumerate(str(value)))
    offset = number(map_id) if map_id is not None else 0
    return ROOM_PALETTE[(number(room_id) + offset) % len(ROOM_PALETTE)]


def _merge_segments(segments: list) -> list:
    """Draw each boundary once: merge overlapping, near-identical segments.

    Horizontal and vertical segments (all Dyson outlines so far) whose lines
    lie within MERGE_TOLERANCE_M of each other are grouped, averaged and
    their overlapping or touching ranges joined. Anything else is returned
    unchanged.
    """
    eps = 1e-6
    lines: dict[str, list] = {"h": [], "v": []}
    others = []
    for (x0, y0), (x1, y1) in segments:
        if abs(y1 - y0) < eps:
            lines["h"].append((y0, min(x0, x1), max(x0, x1)))
        elif abs(x1 - x0) < eps:
            lines["v"].append((x0, min(y0, y1), max(y0, y1)))
        else:
            others.append(((x0, y0), (x1, y1)))
    out = list(others)
    for axis, items in lines.items():
        items.sort()
        groups: list[list] = []
        for item in items:
            if groups and item[0] - groups[-1][-1][0] <= MERGE_TOLERANCE_M:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            pos = sum(g[0] for g in group) / len(group)
            spans = sorted((lo, hi) for _, lo, hi in group)
            merged = [list(spans[0])]
            for lo, hi in spans[1:]:
                if lo <= merged[-1][1] + MERGE_TOLERANCE_M:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            for lo, hi in merged:
                out.append(((lo, pos), (hi, pos)) if axis == "h" else ((pos, lo), (pos, hi)))
    return out


# ── Data extraction helpers ────────────────────────────────────────────────────


def _perimeter_points(zone: dict) -> list[Any]:
    """Extract an ordered, non-self-intersecting perimeter polygon.

    Uses greedy nearest-neighbour chaining to sort the perimeter-pass
    (type=0) segments into a proper polygon without diagonal artefacts.
    """
    raw = [
        s for s in zone.get("presentation", [])
        if s.get("type") == 0
        and s.get("start") is not None
        and s.get("end") is not None
    ]
    if not raw:
        return []

    pairs = [(_pt(s["start"]), _pt(s["end"])) for s in raw]
    chain = list(pairs[0])
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
            chain.append(s)
        else:
            chain.append(e)

    # Deduplicate adjacent identical points
    out: list[Any] = []
    for p in chain:
        if not out or p != out[-1]:
            out.append(p)
    return out


def _room_polygon(zone: dict) -> list[Any]:
    """The room's outline as one polygon, from all its presentation segments."""
    pairs = [(a, b) for _, a, b in _outline_segments(zone)]
    if not pairs:
        return []
    chain = list(pairs[0])
    remaining = pairs[1:]
    while remaining:
        tail = chain[-1]
        best = min(range(len(remaining)), key=lambda i: min(
            math.dist(remaining[i][0], tail), math.dist(remaining[i][1], tail)))
        a, b = remaining.pop(best)
        chain.append(b if math.dist(a, tail) <= math.dist(b, tail) else a)
    out: list[Any] = []
    for p in chain:
        if not out or p != out[-1]:
            out.append(p)
    return out


def _subtract_segments(seg, walls) -> list:
    """Parts of an axis-aligned segment not already covered by a wall."""
    (x0, y0), (x1, y1) = seg
    if min(abs(x1 - x0), abs(y1 - y0)) > 0.01:
        return [seg]                      # not axis-aligned: leave as is
    horizontal = abs(y1 - y0) < abs(x1 - x0)
    lo, hi = sorted((x0, x1) if horizontal else (y0, y1))
    pos = (y0 + y1) / 2 if horizontal else (x0 + x1) / 2
    spans = [(lo, hi)]
    for (a0, b0), (a1, b1) in walls:
        if horizontal != (abs(b1 - b0) < abs(a1 - a0)):
            continue
        wpos = (b0 + b1) / 2 if horizontal else (a0 + a1) / 2
        if abs(wpos - pos) > MERGE_TOLERANCE_M:
            continue
        wlo, whi = sorted((a0, a1) if horizontal else (b0, b1))
        spans = [piece for s0, s1 in spans
                 for piece in ((s0, min(s1, wlo)), (max(s0, whi), s1)) if piece[1] - piece[0] > 0.01]
    return [((s0, pos), (s1, pos)) if horizontal else ((pos, s0), (pos, s1)) for s0, s1 in spans]


def _tint(base, colour, amount):
    """Mix a room colour into a base RGBA colour."""
    return tuple(round(b + (c - b) * amount) for b, c in zip(base[:3], colour)) + (base[3],)


def _perimeter_segments(zone: dict) -> list[tuple[Any, Any]]:
    """Return each wall (type 0) segment as a (start, end) pair."""
    return [(start, end) for kind, start, end in _outline_segments(zone) if kind == 0]


def _outline_segments(zone: dict) -> list[tuple[int, Any, Any]]:
    """Every usable presentation segment as (type, start, end).

    Types 0 (wall), 1 (shared boundary) and 2 (join) only; malformed,
    non-finite and zero-length segments are dropped.
    """
    out = []
    for seg in zone.get("presentation") or []:
        if not isinstance(seg, dict) or seg.get("type") not in (0, 1, 2):
            continue
        try:
            start, end = _pt(seg["start"]), _pt(seg["end"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not all(math.isfinite(v) for v in (*start, *end)) or start == end:
            continue
        out.append((seg["type"], start, end))
    return out


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
        # Same filtering as drawing, so a malformed segment can't break bounds.
        for _kind, start, end in _outline_segments(z):
            pts += [start, end]

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


def _overlay_box(img, xs, ys, pad=1):
    """Clamp a shape's bounding box to the image; None if fully off-canvas.

    Translucent shapes are drawn on an overlay and alpha-composited. Sizing
    the overlay to the shape's bounding box instead of the whole canvas gives
    the same pixels at a fraction of the memory and compositing cost — this
    runs once per furniture item / keep-out zone on every frame.
    """
    x0, y0 = max(0, min(xs) - pad), max(0, min(ys) - pad)
    x1, y1 = min(img.width, max(xs) + pad + 1), min(img.height, max(ys) + pad + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


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
    box = _overlay_box(img, [p[0] for p in pts], [p[1] for p in pts])
    if box is not None:
        x0, y0, x1, y1 = box
        overlay = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon([(x - x0, y - y0) for x, y in pts], fill=fill)
        img.alpha_composite(overlay, dest=(x0, y0))

    if stroke[3] if len(stroke) == 4 else True:
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
    box = _overlay_box(img, [cx - r, cx + r], [cy - r, cy + r])
    if box is None:
        return
    x0, y0, x1, y1 = box
    overlay = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse(
        [cx - r - x0, cy - r - y0, cx + r - x0, cy + r - y0], fill=colour)
    img.alpha_composite(overlay, dest=(x0, y0))


# ── Utilities ──────────────────────────────────────────────────────────────────


def _pt(pt: Any) -> tuple[float, float]:
    """Normalise a point to (x, y). Accepts dict or sequence."""
    if isinstance(pt, dict):
        return float(pt["x"]), float(pt["y"])
    return float(pt[0]), float(pt[1])


@lru_cache(maxsize=1)
def _load_fonts() -> tuple[Any, Any]:
    """Return (large, small) fonts; cached because it's called several times per frame."""
    # Bundled DejaVu Sans first: Pillow's built-in font has no accented
    # letters, so room names like KÜCHE showed boxes.
    try:
        return ImageFont.truetype(str(FONT_FILE), 11), ImageFont.truetype(str(FONT_FILE), 9)
    except (OSError, ImportError):
        pass
    try:
        return ImageFont.load_default(size=11), ImageFont.load_default(size=9)
    except TypeError:
        pass
    try:
        f = ImageFont.load_default()
        return f, f
    except Exception:
        return None, None


@lru_cache(maxsize=1)
def _load_superscript_font() -> Any:
    """A smaller font for the '2' of m², or None if sizes aren't supported."""
    try:
        return ImageFont.truetype(str(FONT_FILE), 7)
    except (OSError, ImportError):
        pass
    try:
        return ImageFont.load_default(size=7)
    except (TypeError, OSError):
        return None


def _draw_area_label(draw, x, y, area, font, colour):
    """Draw '16.9 m²' centred on (x, y) without needing a '²' glyph.

    Pillow's default font has no '²' and drew a box in its place, so the 2 is
    drawn as a small raised digit instead. Falls back to 'm2' when the fonts
    can't be measured.
    """
    try:
        value = f"{float(area):.1f}"
    except (TypeError, ValueError):
        return
    base = f"{value} m"
    sup_font = _load_superscript_font()
    try:
        base_w = font.getlength(base)
        sup_w = sup_font.getlength("2")
    except AttributeError:
        _draw_centered_text(draw, x, y, f"{value} m2", font, colour)
        return
    left = x - (base_w + sup_w) / 2
    draw.text((left, y), base, fill=colour, font=font, anchor="lm")
    draw.text((left + base_w - 1, y - 3), "2", fill=colour, font=sup_font, anchor="lm")


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
        img = Image.new("RGB", (500, 80), (6, 6, 8))
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
