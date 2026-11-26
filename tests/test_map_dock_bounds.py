"""Synthetic regressions for a stationless floor with a remote dock point.

The maintainer's screenshots show a tiny floor in the bottom-left corner
and kilometre-scale zone calibration, only on B without a station. Its raw
dock value has not been captured; the remote values here are synthetic.
"""
from copy import deepcopy
import importlib
import io
from pathlib import Path
import sys
from types import ModuleType
import unittest

from PIL import Image

package = ModuleType("dock_bounds_integration")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub")]
sys.modules[package.__name__] = package
renderer = importlib.import_module(package.__name__ + ".map_renderer")


def floor(offset=0):
    points = [{"x": offset + x, "y": offset + y} for x, y in [(0, 0), (4, 0), (4, 3), (0, 3)]]
    return {"id": "456", "zones": [{"id": "10", "name": "Wohnzimmer",
        "nameLocation": {"x": offset + 2, "y": offset + 1.5}, "visited": points,
        "presentation": [{"start": point, "end": points[(i + 1) % 4], "type": 0}
                         for i, point in enumerate(points)]}]}


def render(data, live=None):
    calibration = {}
    png = renderer.render_map(data, [], live, calibration=calibration, raise_errors=True)
    return png, calibration


class DockBoundsTests(unittest.TestCase):
    def test_remote_or_missing_dock_has_the_same_image_and_calibration_as_no_dock(self):
        expected, expected_cal = render(floor())
        for dock in (None, {}, {"x": 10000, "y": 10000}, {"x": -10000, "y": -10000},
                     {"x": 0, "y": 10000}, {"x": 10000, "y": 0}):
            with self.subTest(dock=dock):
                data = floor()
                data["dockLocation"] = dock
                png, cal = render(data)
                self.assertEqual(png, expected)
                self.assertEqual(cal, expected_cal)
                self.assertEqual(cal["pixels_per_metre"], renderer.SCALE)
                self.assertLess(max(Image.open(io.BytesIO(png)).size), 500)

    def test_nonfinite_or_malformed_docks_cannot_break_the_map(self):
        expected, _ = render(floor())
        for dock in ({"x": float("nan"), "y": 1}, {"x": float("inf"), "y": 1},
                     {"x": None, "y": 1}, {"x": True, "y": 1}, {"x": "bad", "y": 1}, []):
            with self.subTest(dock=dock):
                data = floor()
                data["dockLocation"] = dock
                self.assertEqual(render(data)[0], expected)

    def test_valid_docks_at_zero_inside_or_just_outside_the_wall_are_retained(self):
        for x, y in ((0, 0), (2, 1.5), (-0.5, 1.5)):
            data = floor()
            data["dockLocation"] = {"x": x, "y": y, "angle": 0}
            self.assertIsNotNone(renderer._get_dock(data, None))
            _, cal = render(data)
            self.assertEqual(cal["pixels_per_metre"], renderer.SCALE)
        self.assertAlmostEqual(cal["min_x"], -0.8)

    def test_dock_validation_uses_relative_geometry_not_an_absolute_coordinate_limit(self):
        data = floor(offset=10000)
        data["dockLocation"] = {"x": 10002, "y": 10001.5}
        self.assertIsNotNone(renderer._get_dock(data, None))
        _, cal = render(data)
        self.assertEqual(cal["pixels_per_metre"], renderer.SCALE)
        self.assertAlmostEqual(cal["min_x"], 9999.7)

    def test_live_placeholder_cannot_expand_the_floor_and_a_valid_static_dock_survives(self):
        data = floor()
        self.assertEqual(render(data, {"dockLocation": {"x": 10000, "y": 10000}})[0], render(data)[0])
        data["dockLocation"] = {"x": 0, "y": 0}
        live = {"dockLocation": {"x": 10000, "y": 10000}}
        self.assertEqual(render(data, live)[0], render(data)[0])
        self.assertEqual(renderer._get_dock(data, {"dockLocation": {"x": 2, "y": 1.5}})["x"], 2)

    def test_switching_back_to_a_retains_the_original_image_and_does_not_mutate_api_data(self):
        a, b = floor(), floor()
        a["dockLocation"] = {"x": 0, "y": 0}
        b["dockLocation"] = {"x": 10000, "y": 10000}
        before = deepcopy((a, b))
        a_png, a_cal = render(a)
        render(b)
        self.assertEqual(render(a), (a_png, a_cal))
        self.assertEqual((a, b), before)


if __name__ == "__main__":
    unittest.main()
