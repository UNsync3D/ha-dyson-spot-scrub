"""Captured station commands, confirmed state, and feedback-loop protection."""
import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_drying
from test_drying import module


class StationTests(unittest.TestCase):
    setUp = test_drying.DryingTests.setUp

    def protocol(self):
        return importlib.import_module('drying_test_integration.station_settings')

    def test_all_captured_payloads(self):
        p = self.protocol()
        for key, (_, field, prop) in p.SWITCHES.items():
            for value in (False, True):
                self.assertEqual(p.setting_payloads(key, value), ({field: value}, {prop: int(value)}))
        for label, minutes in p.FREQUENCIES.items():
            high, low = p.setting_payloads('self_cleaning_frequency', label)
            if minutes is None:
                self.assertEqual(low, {'back_wash_type': 1})
            else:
                self.assertEqual(low, {'back_wash_type': 0, 'back_wash_time': minutes})
            self.assertEqual(p.current_setting(high, 'self_cleaning_frequency'), label)
        for hours in (3, 4, 5):
            self.assertEqual(p.setting_payloads('roller_drying_duration', f'{hours} hours'),
                             ({'airDryFrequency': hours}, {'airdry_frequency': hours}))

    def test_unknown_invalid_and_stale_time(self):
        p = self.protocol()
        for key, value in [('hot_water_mopping', 1), ('roller_drying_duration', '6 hours'),
                           ('self_cleaning_frequency', 'unknown'), ('missing', True)]:
            with self.assertRaises(ValueError):
                p.setting_payloads(key, value)
        self.assertIsNone(p.current_setting({'hotWaterMop': 1}, 'hot_water_mopping'))
        self.assertIsNone(p.current_setting({}, 'roller_drying_duration'))
        self.assertEqual(p.current_setting({'backWashType': 'ROOM', 'backWashTime': 60},
                                          'self_cleaning_frequency'), 'After each room')
        self.assertEqual(p.reported_properties({'hot_water_mop': None, 'airdry_frequency': True}), {})

    def test_send_pair_without_optimistic_update_or_repeats(self):
        self.client.state['hotWaterMop'] = False
        with patch.object(self.client, '_publish_raw', return_value=True) as raw, \
                patch.object(self.mqtt.threading, 'Timer') as timer:
            self.assertTrue(self.client.set_station_setting('hot_water_mopping', True))
            self.assertFalse(self.client.state['hotWaterMop'])
            self.assertEqual(raw.call_count, 1)
            self.assertEqual(raw.call_args.args[1]['params'], {'hot_water_mop': 1})
            # Only the STATE-SET: no status request straight away (#50).
            self.assertEqual(self.sent.call_count, 1)
            self.assertEqual(self.sent.call_args_list[0].args[0]['hotWaterMop'], True)
            self.assertEqual(timer.call_args.args[0], 3.0)
            check = timer.call_args.args[1]
            check(*timer.call_args.kwargs['args'])        # not confirmed after 3 s: ask
            self.assertEqual(self.sent.call_args_list[1].args[0]['msg'], 'REQUEST-CURRENT-STATE')
            self.sent.reset_mock(); raw.reset_mock()
            self.client._handle_status({'msg': 'CURRENT-STATE', 'hotWaterMop': True})
            check(*timer.call_args.kwargs['args'])        # confirmed: nothing to ask
            self.sent.assert_not_called()
            self.assertTrue(self.client.set_station_setting('hot_water_mopping', True))
            self.sent.assert_not_called(); raw.assert_not_called()

    def test_missing_disconnected_and_transport_failure(self):
        self.assertFalse(self.client.set_station_setting('hot_water_mopping', True))
        self.client.state['hotWaterMop'] = False
        self.client.connected = False
        self.assertFalse(self.client.set_station_setting('hot_water_mopping', True))
        self.sent.assert_not_called()
        self.client.connected = True
        self.sent.return_value = False
        with patch.object(self.client, '_publish_raw') as raw:
            self.assertFalse(self.client.set_station_setting('hot_water_mopping', True))
            raw.assert_not_called()
        self.sent.return_value = True
        with patch.object(self.client, '_publish_raw', return_value=False):
            self.assertFalse(self.client.set_station_setting('hot_water_mopping', True))
            self.assertFalse(self.client.state['hotWaterMop'])

    def test_reports_update_without_writes(self):
        self.client._handle_jdm({'method': 'prop.get', 'code': 1,
                                'data': {'hot_water_mop': 1, 'airdry_frequency': 4}})
        self.assertTrue(self.client.state['hotWaterMop'])
        self.assertEqual(self.client.state['airDryFrequency'], 4)
        self.client._handle_jdm({'method': 'prop.post', 'params': {'store_demo_mode': 0}})
        self.assertFalse(self.client.state['washMopBeforeClean'])
        self.client._handle_jdm({'method': 'prop.set', 'code': 0,
                                'data': {'property': [{'prop': 'detergent', 'code': 0}]}})
        self.assertNotIn('detergent', self.client.state)
        self.sent.assert_not_called()

    def test_entity_controls_and_lifecycle(self):
        class Entity:
            async def async_added_to_hass(self): pass
            async def async_will_remove_from_hass(self): pass
        with patch.dict('sys.modules', {
            'homeassistant.components.switch': module('switch', SwitchEntity=Entity),
            'homeassistant.components.select': module('select', SelectEntity=Entity),
        }):
            switches = importlib.import_module('drying_test_integration.switch')
            selects = importlib.import_module('drying_test_integration.select')
            entity = switches.DysonStationSwitch(self.coord, self.entry, 'hot_water_mopping', 'Hot water mopping')
            entity.hass = self.hass
            self.coord.async_add_listener = Mock()
            self.coord.async_remove_listener = Mock()
            self.assertFalse(entity.available)
            with self.assertRaises(RuntimeError): asyncio.run(entity.async_turn_on())
            self.client.state['hotWaterMop'] = False
            self.assertTrue(entity.available)
            self.assertFalse(entity.is_on)
            with patch.object(self.client, 'set_station_setting', return_value=True) as setter:
                asyncio.run(entity.async_turn_on())
                setter.assert_called_once_with('hot_water_mopping', True)
                # Shown as sent until the robot confirms it (#50), never longer.
                self.assertTrue(entity.is_on)
                station_entity = importlib.import_module('drying_test_integration.station_entity')
                later = station_entity.time.monotonic() + station_entity.PENDING_SECONDS + 1
                with patch.object(station_entity.time, 'monotonic', return_value=later):
                    self.assertFalse(entity.is_on)
            asyncio.run(entity.async_added_to_hass())
            asyncio.run(entity.async_will_remove_from_hass())
            self.coord.async_add_listener.assert_called_once_with(entity)
            self.coord.async_remove_listener.assert_called_once_with(entity)
            select = selects.DysonStationSelect(self.coord, self.entry, 'roller_drying_duration', 'Drying', ['3 hours', '4 hours', '5 hours'])
            self.client.state['airDryFrequency'] = 4
            self.assertEqual(select.current_option, '4 hours')
            with self.assertRaises(RuntimeError): asyncio.run(select.async_select_option('6 hours'))
