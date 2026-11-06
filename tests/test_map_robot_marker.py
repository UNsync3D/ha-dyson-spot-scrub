"""Robot marker: body from above, red bumper, fading green heading beam."""
import importlib.util
import math
from pathlib import Path
import sys
from types import ModuleType
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
pkg = ModuleType("marker_pkg")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("marker_pkg", pkg)
spec = importlib.util.spec_from_file_location("marker_pkg.map_renderer", ROOT / "map_renderer.py")
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)

BG = (6, 6, 8, 255)


def canvas():
    return Image.new("RGBA", (120, 120), BG)


def greenish(px):
    r, g, b, _ = px
    return g > r + 25 and g > b + 25


def reddish(px):
    r, g, b, _ = px
    return r > 150 and g < 90 and b < 90


class RobotMarkerTests(unittest.TestCase):
    def test_beam_points_the_way_the_robot_faces(self):
        for angle, ahead, behind in ((0.0, (78, 60), (42, 60)),
                                     (math.pi / 2, (60, 42), (60, 78))):
            img = canvas()
            renderer._draw_robot(img, 60, 60, angle)
            self.assertTrue(greenish(img.getpixel(ahead)), angle)
            self.assertEqual(img.getpixel(behind)[:3], (6, 6, 8), angle)

    def test_beam_fades_with_distance(self):
        img = canvas()
        renderer._draw_robot(img, 60, 60, 0.0)
        near, far = img.getpixel((74, 60)), img.getpixel((82, 60))
        self.assertGreater(near[1], far[1])
        self.assertEqual(img.getpixel((90, 60))[:3], (6, 6, 8))   # ends before 25 px

    def test_bumper_is_red_at_the_front_and_body_is_dark(self):
        img = canvas()
        renderer._draw_robot(img, 60, 60, 0.0)
        self.assertTrue(reddish(img.getpixel((71, 60))))
        self.assertFalse(reddish(img.getpixel((49, 60))))
        r, g, b, _ = img.getpixel((56, 60))
        self.assertTrue(40 < r < 80 and abs(r - g) < 10)

    def test_green_sensor_dot_sits_towards_the_front(self):
        img = canvas()
        renderer._draw_robot(img, 60, 60, 0.0)
        self.assertTrue(greenish(img.getpixel((64, 60))))

    def test_robot_at_or_past_the_edge_does_not_fail(self):
        for x, y in ((0, 0), (119, 60), (-5, 60), (500, 500)):
            img = canvas()
            renderer._draw_robot(img, x, y, 1.0)
            self.assertEqual(img.size, (120, 120))


if __name__ == "__main__":
    unittest.main()
