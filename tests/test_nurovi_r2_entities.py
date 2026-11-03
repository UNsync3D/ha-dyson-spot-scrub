"""Dyson R2 Nurovi Wash+Dry entities, from its real CURRENT-STATE (20 Oct 2026).

The R2 reports a mop pad (not a mop roller), no cleaning solution, and a
2-hour roller drying time.
"""
import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_drying
from test_drying import module

R2_STATE = {
    "hotWaterMop": True, "hotWaterSwitch": True, "washMopBeforeClean": True,
    "backWashType": "TIME", "backWashTime": 15, "airDryFrequency": 2,
    "consumables": [{"type": "brushBar", "usage": 0}, {"type": "mopPad", "usage": 1},
                    {"type": "sideBrushes", "usage": 1}, {"type": "robotFilter", "usage": 0},
                    {"type": "dockFilter", "usage": 0}],
}


class Entity:
    async def async_added_to_hass(self): pass
    async def async_will_remove_from_hass(self): pass


class R2EntityTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def platforms(self):
        return patch.dict('sys.modules', {
            'homeassistant.components.switch': module('switch', SwitchEntity=Entity),
            'homeassistant.components.select': module('select', SelectEntity=Entity),
        })

    def setup_platform(self, name, entry):
        self.coord.room_names = []
        self.coord.enabled_rooms = set()
        self.coord.current_mode = None
        self.coord.room_preferences = []
        entities = []
        platform = importlib.import_module(f'drying_test_integration.{name}')
        asyncio.run(platform.async_setup_entry(self.hass, entry, entities.extend))
        return entities

    def entries(self):
        r2 = SimpleNamespace(entry_id='one', data={'serial': 'test', 'device_name': 'R2',
                                                   'product_type': '914', 'mqtt_prefix': 'RB07'})
        spot = SimpleNamespace(entry_id='one', data={'serial': 'test', 'device_name': 'Spot',
                                                     'product_type': '804', 'mqtt_prefix': 'RB05'})
        return r2, spot

    def test_model_detection(self):
        helpers = importlib.import_module('drying_test_integration.entity_helpers')
        r2, spot = self.entries()
        self.assertTrue(helpers.is_nurovi_r2(r2))
        self.assertFalse(helpers.is_nurovi_r2(spot))
        # A synthetic entry is corrected to the real prefix later; that is enough.
        self.assertTrue(helpers.is_nurovi_r2(SimpleNamespace(data={'mqtt_prefix': 'RB07'})))

    def test_r2_has_no_cleaning_solution_switch(self):
        r2, spot = self.entries()
        with self.platforms():
            self.coord.async_add_listener = Mock()
            r2_keys = {e._setting_key for e in self.setup_platform('switch', r2) if hasattr(e, '_setting_key')}
            spot_keys = {e._setting_key for e in self.setup_platform('switch', spot) if hasattr(e, '_setting_key')}
        self.assertNotIn('cleaning_solution', r2_keys)
        self.assertIn('cleaning_solution', spot_keys)
        self.assertEqual(spot_keys - r2_keys, {'cleaning_solution'})

    def test_r2_drying_shows_two_hours_and_spot_scrub_keeps_its_choices(self):
        r2, spot = self.entries()
        with self.platforms():
            self.coord.async_add_listener = Mock()
            r2_drying = next(e for e in self.setup_platform('select', r2)
                             if getattr(e, '_setting_key', None) == 'roller_drying_duration')
            spot_drying = next(e for e in self.setup_platform('select', spot)
                               if getattr(e, '_setting_key', None) == 'roller_drying_duration')
        self.client.state.update(R2_STATE)
        self.assertEqual(r2_drying.current_option, '2 hours')
        self.assertEqual(r2_drying.options, ['2 hours', '3 hours', '4 hours'])
        with self.assertRaises(Exception):
            asyncio.run(r2_drying.async_select_option('5 hours'))   # not offered on the R2
        # A reported value is always listed, so it never shows as invalid.
        self.assertEqual(spot_drying.options, ['3 hours', '4 hours', '5 hours', '2 hours'])
        self.client.state['airDryFrequency'] = 4
        self.assertEqual(spot_drying.options, ['3 hours', '4 hours', '5 hours'])

    def test_r2_water_level_has_no_very_low(self):
        r2, spot = self.entries()
        with self.platforms():
            self.coord.async_add_listener = Mock()
            r2_entities, spot_entities = [], []
            platform = importlib.import_module('drying_test_integration.select')
            self.coord.serial = 'test'
            for entry, out in ((r2, r2_entities), (spot, spot_entities)):
                self.coord.room_names = ['Room 1']
                self.coord.enabled_rooms = set()
                self.coord.current_mode = None
                asyncio.run(platform.async_setup_entry(self.hass, entry, out.extend))
        r2_water = next(e for e in r2_entities if getattr(e, '_select_type', None) == 'water_level')
        spot_water = next(e for e in spot_entities if getattr(e, '_select_type', None) == 'water_level')
        self.assertEqual(r2_water._attr_options, ['Low', 'Medium', 'High'])
        self.assertEqual(spot_water._attr_options, ['Very Low', 'Low', 'Medium', 'High'])

    def test_r2_reported_properties_include_two_hours(self):
        p = importlib.import_module('drying_test_integration.station_settings')
        self.assertEqual(p.reported_properties({'airdry_frequency': 2}), {'airDryFrequency': 2})
        self.assertEqual(p.setting_payloads('roller_drying_duration', '2 hours'),
                         ({'airDryFrequency': 2}, {'airdry_frequency': 2}))
        self.assertEqual(p.reported_properties({'airdry_frequency': 6}), {})


if __name__ == '__main__':
    unittest.main()
