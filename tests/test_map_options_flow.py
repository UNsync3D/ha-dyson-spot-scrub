"""Options form regression tests: real voluptuous, small HA UI adapters.

The frontend selectors/section serializer still need a Home Assistant smoke test.
"""
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import voluptuous as vol


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class Flow:
    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, **kwargs):
        return {'type': 'form', **kwargs}

    def async_create_entry(self, **kwargs):
        return {'type': 'create_entry', **kwargs}


class Selector:
    def __init__(self, config=None):
        self.config = config

    def __call__(self, value):
        return value


class Section:
    def __init__(self, schema, options):
        self.schema, self.options = schema, options

    def __call__(self, value):
        return self.schema(value)


class MapOptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        package = module('map_flow_integration')
        package.__path__ = [str(Path(__file__).resolve().parents[1] /
                                'custom_components/dyson_spot_scrub')]
        entries = module('homeassistant.config_entries', ConfigFlow=Flow, OptionsFlow=Flow)
        selectors = SimpleNamespace(BooleanSelector=Selector, ColorRGBSelector=Selector,
                                    NumberSelector=Selector, NumberSelectorConfig=dict,
                                    NumberSelectorMode=SimpleNamespace(SLIDER='slider'),
                                    SelectSelector=Selector, SelectSelectorConfig=dict,
                                    SelectSelectorMode=SimpleNamespace(DROPDOWN='dropdown'))
        modules = {
            'map_flow_integration': package,
            'homeassistant': module('homeassistant', config_entries=entries),
            'homeassistant.config_entries': entries,
            'homeassistant.core': module('homeassistant.core', callback=lambda fn: fn),
            'homeassistant.data_entry_flow': module('homeassistant.data_entry_flow',
                                                    FlowResult=dict, section=Section),
            'homeassistant.helpers': module('homeassistant.helpers', selector=selectors),
            'map_flow_integration.dyson_api': module('map_flow_integration.dyson_api',
                authenticate_v1=None, initiate_v3_auth=None, verify_v3_auth=None,
                get_devices=None, DysonAuthError=Exception, DysonApiError=Exception),
        }
        self.enterContext(patch.dict(sys.modules, modules))
        self.flow_module = importlib.import_module('map_flow_integration.config_flow')
        self.flow = self.flow_module.DysonOptionsFlow()
        self.flow.config_entry = SimpleNamespace(options={'log_mqtt': True})

    async def test_collapsed_defaults_and_unchecked_switches(self):
        result = await self.flow.async_step_init()
        schema = result['data_schema']
        values = schema({})
        self.assertEqual(values['map_retention_minutes'], 5)
        self.assertFalse(values['map_keep_last'])
        self.assertEqual(values['line_settings_section']['map_glow_width'], 6)
        self.assertTrue(values['visibility_settings_section']['map_show_dock'])
        for marker, section in schema.schema.items():
            if isinstance(section, Section):
                self.assertTrue(section.options['collapsed'])
        # False booleans must not act like required checkboxes in the frontend.
        for marker in schema.schema:
            if str(marker) in ('map_keep_last', 'map_transparent_background'):
                self.assertIsInstance(marker, vol.Optional)
        values['visibility_settings_section'] = {
            key: False for key in values['visibility_settings_section']}
        values['line_settings_section']['map_glow_width'] = 0
        saved = await self.flow.async_step_init(values)
        self.assertEqual(saved['type'], 'create_entry')
        self.assertFalse(saved['data']['map_show_dock'])
        self.assertEqual(saved['data']['map_glow_width'], 0)
        self.assertTrue(saved['data']['log_mqtt'])
        self.assertNotIn('line_settings_section', saved['data'])

    async def test_partial_submission_preserves_existing_and_reopens(self):
        self.flow.config_entry.options.update(map_path_width=12, map_show_grid=True)
        result = await self.flow.async_step_init({'map_retention_minutes': 0})
        self.assertEqual(result['data']['map_path_width'], 12)
        self.assertTrue(result['data']['map_show_grid'])
        self.flow.config_entry.options = result['data']
        form = await self.flow.async_step_init()
        values = form['data_schema']({})
        self.assertEqual(values['map_retention_minutes'], 0)
        self.assertEqual(values['line_settings_section']['map_path_width'], 12)

    async def test_invalid_values_are_not_silently_saved(self):
        for value in [-1, 26, True, '6']:
            result = await self.flow.async_step_init({'line_settings_section': {'map_glow_width': value}})
            self.assertEqual(result['type'], 'form')
            self.assertEqual(result['errors']['base'], 'invalid_map_option')
        result = await self.flow.async_step_init({'visibility_settings_section': 'invalid'})
        self.assertEqual(result['errors']['base'], 'invalid_map_option')


    async def test_walls_and_floors_section(self):
        result = await self.flow.async_step_init()
        schema = result['data_schema']
        walls = schema({})['walls_settings_section']
        self.assertEqual(walls['map_wall_style'], 'solid')
        self.assertEqual(walls['map_wall_thickness_cm'], 5)
        self.assertEqual(walls['map_door_style'], 'dashed')
        fields = {str(k): v for marker, section in schema.schema.items()
                  if isinstance(section, Section) for k, v in section.schema.schema.items()}
        self.assertEqual(fields['map_wall_style'].config['options'], ['solid', 'dashed'])
        self.assertEqual(fields['map_wall_thickness_cm'].config['unit_of_measurement'], 'cm')
        saved = await self.flow.async_step_init({'walls_settings_section': {
            'map_wall_style': 'dashed', 'map_wall_thickness_cm': 8,
            'map_wall_color': [255, 0, 0], 'map_room_fill_color': [10, 20, 30]}})
        self.assertEqual(saved['type'], 'create_entry')
        self.assertEqual(saved['data']['map_wall_style'], 'dashed')
        self.assertEqual(saved['data']['map_wall_thickness_cm'], 8)
        self.assertEqual(saved['data']['map_room_fill_color'], [10, 20, 30])
        self.assertNotIn('walls_settings_section', saved['data'])
        for bad in ({'map_wall_style': 'dotted'}, {'map_door_thickness_cm': 0},
                    {'map_wall_thickness_cm': 31}, {'map_floor_color': [300, 0, 0]}):
            result = await self.flow.async_step_init({'walls_settings_section': bad})
            self.assertEqual(result['errors']['base'], 'invalid_map_option', bad)


if __name__ == '__main__':
    unittest.main()
