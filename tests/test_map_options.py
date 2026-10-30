"""Offline options and pixel-rendering tests (Pillow and voluptuous required)."""
import copy
import importlib
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType('map_options_integration')
package.__path__ = [str(ROOT / 'custom_components/dyson_spot_scrub')]
sys.modules[package.__name__] = package
constants = importlib.import_module(package.__name__ + '.const')
options_module = importlib.import_module(package.__name__ + '.map_options')
renderer = importlib.import_module(package.__name__ + '.map_renderer')
PathHistory = importlib.import_module(package.__name__ + '.map_path').PathHistory


class MapOptionsTests(unittest.TestCase):
    def test_ranges_false_and_zero(self):
        normalize = options_module.map_options
        for key, bounds in constants.MAP_NUMBER_RANGES.items():
            for value in bounds:
                self.assertEqual(normalize({key: value})[key], value)
            for value in [bounds[0]-1, bounds[1]+1, float('nan'), float('inf'), True, '3']:
                self.assertEqual(normalize({key: value})[key], constants.MAP_DEFAULTS[key])
        self.assertFalse(normalize({'map_show_dock': False})['map_show_dock'])
        self.assertEqual(normalize({'map_glow_width': 0})['map_glow_width'], 0)

    def test_independent_colors_and_defaults(self):
        submitted = {'map_vacuum_color': [1, 2, 3]}
        options = options_module.map_options(submitted)
        options['map_vacuum_color'][0] = 255
        self.assertEqual(submitted['map_vacuum_color'], [1, 2, 3])
        for value in [[True, 0, 0], [256, 0, 0], [-1, 0, 0], [0, 0], [0, 1.5, 0]]:
            self.assertEqual(options_module.map_options({'map_mop_color': value})['map_mop_color'],
                             constants.MAP_DEFAULTS['map_mop_color'])

    def test_path_prefix_survives_mode_changes_and_short_snapshots(self):
        history = PathHistory()
        points = [[0, 0], [1, 0], [2, 0], [3, 0]]
        raw = {'cleanPath': points[:2]}
        self.assertEqual(history.annotate(raw, 'VACUUMING')['_pathModes'], ['VACUUMING'])
        self.assertNotIn('_pathModes', raw)
        self.assertEqual(history.annotate({'cleanPath': points}, 'MOPPING')['_pathModes'],
                         ['VACUUMING', 'MOPPING', 'MOPPING'])
        history.annotate(raw, 'VACUUMING_AND_MOPPING')
        self.assertEqual(history.annotate({'cleanPath': points}, None)['_pathModes'],
                         ['VACUUMING', 'MOPPING', 'MOPPING'])
        rewritten = history.annotate({'cleanPath': [[8, 8], [9, 9]]}, 'VACUUMING')
        self.assertEqual(rewritten['_pathModes'], [None])

    def test_translations_preserve_services_and_cover_all_settings(self):
        texts = [json.loads((ROOT / 'custom_components/dyson_spot_scrub' / file).read_text(encoding='utf-8'))
                 for file in ['strings.json', 'translations/en.json']]
        self.assertEqual(texts[0]['options'], texts[1]['options'])
        step = texts[0]['options']['step']['init']
        keys = set(step['data'])
        for section in step['sections'].values():
            keys.update(section['data'])
        self.assertEqual(keys, set(constants.MAP_DEFAULTS))
        self.assertIn('start_rooms_with_settings', texts[0]['services'])


class MapRenderingTests(unittest.TestCase):
    def setUp(self):
        corners = [[0, 0], [1, 0], [1, 1], [0, 1]]
        self.map = {'zones': [{'id': 1, 'name': 'Room', 'nameLocation': {'x': .5, 'y': .5},
                              'area': 1, 'cleanStatus': 'CLEAN_COMPLETE',
                              'presentation': [{'type': 0, 'start': a, 'end': b}
                                               for a, b in zip(corners, corners[1:] + corners[:1])],
                              'visited': [[0, 0], [.5, 0]]}],
                    'furniture': [{'points': [[0, 0], [.3, 0], [.3, .3]]}],
                    'restrictions': [{'points': [[.7, .7], [1, .7], [1, 1]]}],
                    'dockLocation': {'x': 0, 'y': 1}}
        self.hidden = {key: False for key in constants.MAP_VISIBILITY_DEFAULTS}
        self.hidden.update(map_transparent_background=True, map_glow_width=0,
                           map_floor_fill=False)

    def render(self, options=None, live=None):
        result = renderer.render_map(self.map, [], live, options, raise_errors=True)
        return Image.open(io.BytesIO(result)).convert('RGBA')

    def test_visibility_does_not_shift_bounds(self):
        empty = self.render(self.hidden)
        self.assertEqual(empty.getchannel('A').getextrema(), (0, 0))
        for key in constants.MAP_VISIBILITY_DEFAULTS:
            if key == 'map_show_waypoints':
                continue
            visible = self.render({**self.hidden, key: True})
            self.assertEqual(visible.size, empty.size, key)
            self.assertGreater(visible.getchannel('A').getextrema()[1], 0, key)
        self.assertEqual(self.render().getchannel('A').getextrema(), (255, 255))

    def test_mode_colors_and_glow_total_width(self):
        # Transform for this fixture: x=64+80*x, y=144-80*y.
        live = {'cleanPath': [[0, .5], [1, .5]], '_pathModes': ['VACUUMING']}
        before = copy.deepcopy((self.map, live))
        image = self.render({**self.hidden, 'map_vacuum_color': [255, 0, 0],
                             'map_path_width': 3, 'map_glow_width': 9}, live)
        self.assertEqual(image.getpixel((104, 104)), (255, 0, 0, 255))
        self.assertEqual(image.getpixel((104, 108)), (255, 0, 0, 50))
        self.assertEqual(image.getpixel((104, 109))[3], 0)
        no_glow = self.render({**self.hidden, 'map_path_width': 3}, live)
        self.assertEqual(no_glow.getpixel((104, 108))[3], 0)
        smaller = self.render({**self.hidden, 'map_path_width': 15, 'map_glow_width': 1}, live)
        self.assertEqual(smaller.getpixel((104, 112))[3], 0)
        self.assertEqual((self.map, live), before)
        for action, key in constants.MAP_PATH_ACTIONS.items():
            colored = self.render({**self.hidden, key: [12, 34, 56]},
                                  {**live, '_pathModes': [action]})
            self.assertEqual(colored.getpixel((104, 104)), (12, 34, 56, 255))


if __name__ == '__main__':
    unittest.main()
