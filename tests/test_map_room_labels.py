"""Center room names: the centre of the room's walls, or Dyson's nameLocation."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
pkg = ModuleType("labels_pkg")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("labels_pkg", pkg)
spec = importlib.util.spec_from_file_location("labels_pkg.map_renderer", ROOT / "map_renderer.py")
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)
FIXTURE = Path(__file__).parent / "fixtures/map_downstairs_4rooms.json"


def room(points):
    segs = [{"start": {"x": a[0], "y": a[1]}, "end": {"x": b[0], "y": b[1]}, "type": 0}
            for a, b in zip(points, points[1:] + points[:1])]
    return {"id": "1", "name": "Room", "presentation": segs}


class RoomLabelTests(unittest.TestCase):
    def test_rectangle_label_sits_at_the_centre_of_its_walls(self):
        x, y = renderer._room_label_point(room([(0, 0), (4, 0), (4, 2), (0, 2)]))
        self.assertAlmostEqual(x, 2.0)
        self.assertAlmostEqual(y, 1.0)

    def test_l_shaped_room_keeps_the_label_inside(self):
        # The area centre of this L lies in the missing corner.
        shape = [(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)]
        x, y = renderer._room_label_point(room(shape))
        self.assertTrue(renderer._inside(shape, x, y))

    def test_real_map_centres(self):
        data = json.loads(FIXTURE.read_text())
        got = {z["name"]: tuple(round(v, 2) for v in renderer._room_label_point(z))
               for z in data["zones"]}
        self.assertEqual(got["Living Room"], (3.1, -2.8))
        self.assertEqual(got["Dining"], (0.15, 1.1))

    def test_on_by_default_and_off_uses_dysons_positions(self):
        data = json.loads(FIXTURE.read_text())
        def render(centre):
            png = renderer.render_map(data, None, None, {"map_center_room_names": centre},
                                      raise_errors=True)
            return Image.open(io.BytesIO(png)).convert("RGB")
        self.assertNotEqual(render(False).tobytes(), render(True).tobytes())
        self.assertEqual(render(True).tobytes(),
                         Image.open(io.BytesIO(renderer.render_map(data, None, None, raise_errors=True))).convert("RGB").tobytes())


if __name__ == "__main__":
    unittest.main()
