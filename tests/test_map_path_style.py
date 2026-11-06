"""Clean path drawn like the MyDyson app: no travel, smooth, no nodes."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
pkg = ModuleType("pathstyle_pkg")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("pathstyle_pkg", pkg)
spec = importlib.util.spec_from_file_location("pathstyle_pkg.map_renderer", ROOT / "map_renderer.py")
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)


def pts(n):
    return [(float(i), 0.0) for i in range(n)]


class PathRunsTests(unittest.TestCase):
    def test_travel_points_are_left_out(self):
        cp = [{"x": i, "y": 0, "update": u} for i, u in enumerate([0, 0, 1, 1, 1, 0, 0])]
        runs = renderer._path_runs(cp, pts(7), [None] * 6)
        self.assertEqual([(m, len(r)) for m, r in runs], [("VACUUMING", 4)])

    def test_per_point_codes_decide_the_colour(self):
        cp = [{"x": i, "y": 0, "update": u} for i, u in enumerate([1, 1, 1, 2, 2, 3, 3])]
        runs = renderer._path_runs(cp, pts(7), ["VACUUMING"] * 6)
        self.assertEqual([m for m, _ in runs],
                         ["VACUUMING", "MOPPING", "VACUUMING_AND_MOPPING"])

    def test_without_codes_the_observed_activity_is_used(self):
        cp = [{"x": i, "y": 0} for i in range(4)]
        runs = renderer._path_runs(cp, pts(4), ["MOPPING"] * 3)
        self.assertEqual([(m, len(r)) for m, r in runs], [("MOPPING", 4)])

    def test_runs_join_without_gaps(self):
        cp = [{"x": i, "y": 0, "update": u} for i, u in enumerate([1, 1, 2, 2])]
        runs = renderer._path_runs(cp, pts(4), [None] * 3)
        self.assertEqual(runs[0][1][-1], runs[1][1][0], "colour change shares a point")


class SmoothTests(unittest.TestCase):
    def test_corners_are_rounded_and_ends_kept(self):
        square = [(0, 0), (10, 0), (10, 10)]
        out = renderer._smooth(square)
        self.assertEqual(out[0], (0, 0))
        self.assertEqual(out[-1], (10, 10))
        self.assertNotIn((10, 0), out, "the sharp corner is cut")
        self.assertGreater(len(out), len(square))



class PathCodeCountTests(unittest.TestCase):
    def test_counts_by_code_without_coordinates(self):
        path = [{"x": 1, "y": 2, "update": 0}, {"x": 1, "y": 3, "update": 0},
                {"x": 2, "y": 3, "update": 1}, {"x": 2, "y": 4, "update": 2},
                {"x": 3, "y": 4}, {"x": 3, "y": 5, "update": True}, [4, 5]]
        counts = renderer.path_code_counts(path)
        self.assertEqual(counts, {0: 2, 1: 1, 2: 1, "none": 2})
        text = renderer.format_path_codes(counts)
        self.assertEqual(text, "6 points (code 0: 2, code 1: 1, code 2: 1, code none: 2)")
        self.assertNotIn("x", text.replace("none", ""))

if __name__ == "__main__":
    unittest.main()
