"""Walls on the HA map: presentation types 0 and 2 are walls, of any length.

Before this change the renderer dropped every presentation segment longer
than 1.5 m, so most walls never appeared. Type meanings were confirmed by
splitting a room in the MyDyson app (fixtures/map_downstairs_split.json):
the split line appears as type 1 in both rooms. A four-room capture
(fixtures/map_downstairs_4rooms.json) and the MyDyson app's own drawing of it
showed type 2 is a wall between two rooms and type 1 an open boundary, which
the app draws dashed.
Run: python -m unittest discover -s tests -v
"""
import importlib
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
import unittest.mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
FIXTURE = Path(__file__).resolve().parent / "fixtures/map_downstairs_split.json"
FOUR_ROOMS = Path(__file__).resolve().parent / "fixtures/map_downstairs_4rooms.json"
package = ModuleType("walls_integration")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
renderer = importlib.import_module(package.__name__ + ".map_renderer")
constants = importlib.import_module(package.__name__ + ".const")

SCALE, PAD, MARGIN = renderer.SCALE, renderer.PAD, 0.3


def seg(x0, y0, x1, y1, kind):
    return {"start": {"x": x0, "y": y0}, "end": {"x": x1, "y": y1}, "type": kind}


def square_room(extra=()):
    """A 4 x 4 m room: four 4 m walls (type 0), nothing else drawn."""
    return {"zones": [{"id": "10", "name": "", "presentation": [
        seg(0, 0, 4, 0, 0), seg(4, 0, 4, 4, 0), seg(4, 4, 0, 4, 0), seg(0, 4, 0, 0, 0),
        *extra]}]}


def render(map_data, **options):
    png = renderer.render_map(map_data, None, options={"map_show_dock": False,
                                                       "map_show_grid": False,
                                                       "map_floor_fill": False, **options},
                              raise_errors=True)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def pixel(img, x, y, bounds):
    """Pixel at map point (x, y); bounds = (min_x, max_y) of the drawn data."""
    min_x, max_y = bounds
    return img.getpixel((int((x - (min_x - MARGIN)) * SCALE + PAD),
                         int(((max_y + MARGIN) - y) * SCALE + PAD)))


class WallTests(unittest.TestCase):
    def test_long_walls_are_drawn(self):
        img = render(square_room())
        for x, y in [(2, 0), (4, 2), (2, 4), (0, 2)]:      # middle of each 4 m wall
            self.assertEqual(pixel(img, x, y, (0, 4)), renderer.WALL, (x, y))
        self.assertEqual(pixel(img, 2, 2, (0, 4)), renderer.BG, "room interior stays empty")

    def test_dividers_are_dashed_and_lighter(self):
        img = render(square_room([seg(2, 0, 2, 4, 1)]))
        line = [pixel(img, 2, i / 20, (0, 4)) for i in range(10, 70)]
        self.assertIn(renderer.BG, line, "gaps: dashed")
        self.assertTrue(any(p not in (renderer.BG, renderer.WALL) for p in line), "drawn")
        self.assertNotIn(renderer.WALL, line, "not drawn as a wall")

    def test_option_hides_walls_and_dividers(self):
        img = render(square_room([seg(2, 0, 2, 4, 1)]), map_show_perimeter=False)
        self.assertEqual(pixel(img, 2, 0, (0, 4)), renderer.BG)
        self.assertEqual(pixel(img, 2, 1, (0, 4)), renderer.BG)

    def test_malformed_segments_are_skipped(self):
        bad = [{"type": 0}, {"start": {"x": 1}, "end": {"x": 2, "y": 2}, "type": 0},
               seg(1, 1, 1, 1, 0), seg(float("nan"), 0, 1, 1, 0), seg(1, 1, 2, 2, 7), "x"]
        self.assertEqual(renderer._outline_segments({"presentation": bad}), [])
        render(square_room(bad))   # and rendering doesn't fail

    def test_captured_map_walls_and_split(self):
        data = json.loads(FIXTURE.read_text())
        img = render(data, map_show_furniture=False, map_show_restrictions=False,
                     map_show_visited_paths=False, map_show_labels=False)
        bounds = (-0.85, 2.425)   # leftmost floor-outline point, topmost wall
        # Room1's 4.7 m south wall and 3.9 m east wall: previously not drawn.
        self.assertEqual(pixel(img, 3.3, -5.225, bounds), renderer.WALL)
        self.assertEqual(pixel(img, 5.675, -3.0, bounds), renderer.WALL)
        # The split line (type 1 in both rooms) is not drawn as a wall.
        split = [pixel(img, 0.975, y / 20, bounds) for y in range(-6, 44)]
        self.assertNotIn(renderer.WALL, split)
        self.assertTrue(any(p != renderer.BG for p in split))

    def test_wall_types_in_the_capture(self):
        data = json.loads(FIXTURE.read_text())
        types = {z["name"]: sorted({s["type"] for s in z["presentation"]}) for z in data["zones"]}
        self.assertEqual(types, {"Room1": [0, 1, 2], "Room2": [0, 1, 2]})
        shared = [renderer._outline_segments(z) for z in data["zones"]]
        split = [[(a, b) for kind, a, b in segs if kind == 1] for segs in shared]
        self.assertEqual({p[0] for pairs in split for a, b in pairs for p in (a, b)}, {0.975})


    def test_type_2_is_a_wall(self):
        img = render(square_room([seg(2, 0, 2, 4, 2)]))
        self.assertEqual(pixel(img, 2, 2, (0, 4)), renderer.WALL)

    def test_open_boundary_stops_where_a_wall_covers_it(self):
        walls = [((2, 0), (2, 1.5))]
        self.assertEqual(renderer._subtract_segments(((2, 0), (2, 4)), walls),
                         [((2, 1.5), (2, 4))])
        self.assertEqual(renderer._subtract_segments(((0, 0), (1, 1)), walls),
                         [((0, 0), (1, 1))])       # diagonal: left alone


class FloorFillTests(unittest.TestCase):
    """Default look, like the MyDyson app: reachable floor dark, the rest grey."""

    def room_with_floor(self):
        data = square_room()
        data["zones"][0]["visited"] = [{"x": 1, "y": 1}, {"x": 3, "y": 1},
                                       {"x": 3, "y": 3}, {"x": 1, "y": 3}]
        return data

    def test_fill_is_the_default(self):
        self.assertTrue(constants.MAP_APPEARANCE_DEFAULTS["map_floor_fill"])

    def test_floor_dark_rest_grey_walls_app_grey(self):
        img = render(self.room_with_floor(), map_floor_fill=True, map_show_labels=False)
        self.assertEqual(pixel(img, 2, 2, (0, 4)), renderer.FLOOR_FILL)
        self.assertEqual(pixel(img, 0.5, 2, (0, 4)), renderer.ROOM_FILL)
        self.assertEqual(pixel(img, 2, 0, (0, 4)), renderer.APP_WALL)
        self.assertEqual(pixel(img, -0.2, 2, (0, 4)), renderer.BG)

    def test_room_colours_tint_the_floor(self):
        img = render(self.room_with_floor(), map_floor_fill=True, map_room_colors=True,
                     map_show_labels=False)
        floor = pixel(img, 2, 2, (0, 4))
        self.assertNotEqual(floor, renderer.FLOOR_FILL)
        self.assertEqual(floor, renderer._tint(renderer.FLOOR_FILL, renderer._room_color("10"), 0.22))

    def test_room_colour_follows_the_map_and_room_id(self):
        self.assertEqual(renderer._room_color("10", "1792911875"),
                         renderer._room_color(10, 1792911875))
        self.assertNotEqual(renderer._room_color("10", "1"), renderer._room_color("11", "1"))
        self.assertNotEqual(renderer._room_color("10", "1"), renderer._room_color("10", "2"))

    def test_default_is_the_app_look(self):
        """Fill on; outlines and furniture off (the fill already shows both)."""
        visibility = constants.MAP_VISIBILITY_DEFAULTS
        self.assertFalse(visibility["map_show_visited_paths"])
        self.assertFalse(visibility["map_show_furniture"])
        data = self.room_with_floor()
        data["furniture"] = [{"points": [{"x": 1.5, "y": 1.5}, {"x": 2.5, "y": 1.5},
                                         {"x": 2.5, "y": 2.5}, {"x": 1.5, "y": 2.5}]}]
        png = renderer.render_map(data, None, options={"map_show_labels": False},
                                  raise_errors=True)
        img = Image.open(io.BytesIO(png)).convert("RGBA")
        self.assertEqual(pixel(img, 2, 2, (0, 4)), renderer.FLOOR_FILL, "no furniture box")
        self.assertEqual(pixel(img, 2, 1, (0, 4)), renderer.FLOOR_FILL, "no outline")
        self.assertEqual(pixel(img, 2, 0, (0, 4)), renderer.APP_WALL)

    def test_walls_stay_white_with_room_colours(self):
        img = render(self.room_with_floor(), map_floor_fill=True, map_room_colors=True,
                     map_show_labels=False)
        self.assertEqual(pixel(img, 2, 0, (0, 4)), renderer.WALL)

    def test_four_room_capture_like_the_app(self):
        img = render(json.loads(FOUR_ROOMS.read_text()), map_floor_fill=True,
                     map_show_labels=False, map_show_restrictions=False)
        bounds = (-0.85, 2.525)
        # Kitchen | Toilet: type 2 in both rooms, solid in the app.
        wall = [pixel(img, 5.075, y / 20, bounds) for y in range(0, 30)]
        self.assertEqual(set(wall), {renderer.APP_WALL})
        # Kitchen | Dining: type 1, dashed in the app.
        line = [pixel(img, 1.125, y / 20, bounds) for y in range(0, 40)]
        self.assertIn(renderer.APP_WALL, line)
        self.assertIn(renderer.FLOOR_FILL, line, "gaps: dashed")


class WallOptionTests(unittest.TestCase):
    """Walls and floors options: style, thickness in cm, line and region colours."""

    def room(self):
        data = square_room([seg(2, 0, 2, 4, 1)])          # a door line down the middle
        data["zones"][0]["visited"] = [{"x": 0.5, "y": 0.5}, {"x": 1.5, "y": 0.5},
                                       {"x": 1.5, "y": 3.5}, {"x": 0.5, "y": 3.5}]
        data["zones"][0].update(name="Küche", nameLocation={"x": 1, "y": 2})
        return data

    def draw(self, **options):
        return render(self.room(), map_floor_fill=True, map_show_labels=False, **options)

    def wall_pixels(self, img, colour):
        """How many pixels thick the left wall (x = 0) is, sampled at y = 2."""
        min_x, max_y = 0, 4
        y = int(((max_y + MARGIN) - 2) * SCALE + PAD)
        return sum(1 for x in range(0, int(1 * SCALE + PAD + MARGIN * SCALE))
                   if img.getpixel((x, y)) == colour)

    def test_thickness_is_in_real_centimetres(self):
        thin = self.wall_pixels(self.draw(map_wall_thickness_cm=5), renderer.APP_WALL)
        thick = self.wall_pixels(self.draw(map_wall_thickness_cm=20), renderer.APP_WALL)
        self.assertEqual(thin, max(1, round(0.05 * SCALE)))
        self.assertEqual(thick, round(0.20 * SCALE))

    def test_dashed_walls_have_gaps(self):
        line = [pixel(self.draw(map_wall_style="dashed"), 4, i / 20, (0, 4)) for i in range(5, 75)]
        self.assertIn(renderer.APP_WALL, line)
        self.assertIn(renderer.ROOM_FILL, line, "gaps")
        solid = [pixel(self.draw(), 4, i / 20, (0, 4)) for i in range(5, 75)]
        self.assertEqual(set(solid), {renderer.APP_WALL})

    def test_solid_doors(self):
        line = [pixel(self.draw(map_door_style="solid"), 2, i / 20, (0, 4)) for i in range(5, 75)]
        self.assertEqual(set(line), {renderer.APP_WALL})

    def test_line_and_region_colours(self):
        img = self.draw(map_wall_color=[255, 0, 0], map_door_color=[0, 0, 255],
                        map_floor_color=[0, 80, 0], map_room_fill_color=[60, 60, 0])
        self.assertEqual(pixel(img, 0, 2, (0, 4)), (255, 0, 0, 255))
        self.assertIn((0, 0, 255, 255), [pixel(img, 2, i / 20, (0, 4)) for i in range(5, 75)])
        self.assertEqual(pixel(img, 1, 2, (0, 4)), (0, 80, 0, 255))
        self.assertEqual(pixel(img, 3, 2, (0, 4)), (60, 60, 0, 255))

    def test_default_grey_turns_white_but_a_chosen_colour_stays(self):
        self.assertEqual(pixel(self.draw(map_room_colors=True), 0, 2, (0, 4)), renderer.WALL)
        chosen = self.draw(map_room_colors=True, map_wall_color=[255, 0, 0])
        self.assertEqual(pixel(chosen, 0, 2, (0, 4)), (255, 0, 0, 255))

    def test_each_wall_type_has_its_own_options(self):
        """Type 0 outside walls, type 2 walls between rooms, type 1 openings."""
        data = square_room([seg(1, 0, 1, 4, 2), seg(3, 0, 3, 4, 1)])
        img = render(data, map_floor_fill=True, map_show_labels=False,
                     map_wall_color=[255, 0, 0], map_inner_wall_color=[0, 255, 0],
                     map_door_color=[0, 0, 255], map_door_style="solid",
                     map_inner_wall_thickness_cm=15)
        self.assertEqual(pixel(img, 0, 2, (0, 4)), (255, 0, 0, 255))     # type 0
        self.assertEqual(pixel(img, 1, 2, (0, 4)), (0, 255, 0, 255))     # type 2
        self.assertEqual(pixel(img, 3, 2, (0, 4)), (0, 0, 255, 255))     # type 1
        self.assertEqual(pixel(img, 1.06, 2, (0, 4)), (0, 255, 0, 255), "15 cm wide")
        self.assertNotEqual(pixel(img, 0.06, 2, (0, 4)), (255, 0, 0, 255), "still 5 cm")
        dashed = render(data, map_floor_fill=True, map_inner_wall_style="dashed")
        inner = [pixel(dashed, 1, i / 20, (0, 4)) for i in range(5, 75)]
        outer = [pixel(dashed, 4, i / 20, (0, 4)) for i in range(5, 75)]
        self.assertIn(renderer.APP_WALL, inner)
        self.assertGreater(len(set(inner)), 1, "type 2 dashed: gaps")
        self.assertEqual(set(outer), {renderer.APP_WALL}, "type 0 still solid")

    def test_labels_turn_dark_on_a_light_floor(self):
        drawn = []
        original = renderer._draw_centered_text
        def record(draw, x, y, text, font, colour):
            drawn.append((text, colour))
            return original(draw, x, y, text, font, colour)
        with unittest.mock.patch.object(renderer, "_draw_centered_text", record):
            render(self.room(), map_floor_fill=True, map_floor_color=[245, 245, 240])
            render(self.room(), map_floor_fill=True)
        self.assertEqual(drawn[0][1], renderer.DARK_LABEL)
        self.assertEqual(drawn[1][1], renderer.ZONE_LABEL)

    def test_thick_walls_meet_at_square_corners(self):
        img = self.draw(map_wall_thickness_cm=20)
        self.assertEqual(pixel(img, -0.08, -0.08, (0, 4)), renderer.APP_WALL)


class FontTests(unittest.TestCase):
    def test_bundled_font_is_used(self):
        self.assertTrue(renderer.FONT_FILE.exists())
        self.assertTrue((renderer.FONT_FILE.parent / "LICENSE-DejaVu.txt").exists())
        font, _ = renderer._load_fonts()
        self.assertIsInstance(font, renderer.ImageFont.FreeTypeFont)
        self.assertEqual(Path(font.path).name, "DejaVuSans.ttf")


class AreaLabelTests(unittest.TestCase):
    """Pillow's default font has no '²': labels showed 'm' plus a box."""

    def drawn_text(self, **options):
        texts = []
        original = renderer.ImageDraw.ImageDraw.text
        def record(draw, xy, text, *args, **kwargs):
            texts.append(text)
            return original(draw, xy, text, *args, **kwargs)
        renderer.ImageDraw.ImageDraw.text = record
        try:
            render(json.loads(FIXTURE.read_text()), **options)
        finally:
            renderer.ImageDraw.ImageDraw.text = original
        return texts

    def test_area_is_drawn_without_the_squared_glyph(self):
        texts = self.drawn_text()
        self.assertFalse(any("\u00b2" in t for t in texts), texts)
        self.assertIn("4.2 m", texts)          # Room2: 4.1525 m²
        self.assertIn("2", texts)              # the raised 2

    def test_fallback_without_measurable_fonts(self):
        calls = []
        draw = type("Draw", (), {"text": lambda self, xy, text, **kw: calls.append(text)})()
        renderer._draw_area_label(draw, 10, 10, 16.95, object(), (255, 255, 255, 255))
        self.assertEqual(calls, ["16.9 m2"])
        calls.clear()
        renderer._draw_area_label(draw, 10, 10, "not a number", object(), (0, 0, 0, 0))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
