"""Entity compatibility: unique_ids, entity classes and device associations.

The Lovelace card discovers entities by unique_id and device, so these must not
change. The snapshot in fixtures/entity_registry_snapshot.json was generated
from main at c1f9d5d (before the security review); any intentional change to
an entity's identity must update it deliberately.
"""
import asyncio
import enum
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
_spec = importlib.util.spec_from_file_location("room_types_for_tests", ROOT / "room_types.py")
_room_types = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_room_types)
sys.modules["room_types_for_tests"] = _room_types
SNAPSHOT = Path(__file__).resolve().parent / "fixtures/entity_registry_snapshot.json"


def collect(root, room_labels=None):
    stubs = {}
    def mod(name, **kw):
        m = types.ModuleType(name); m.__dict__.update(kw); stubs[name] = m; return m
    Flags = enum.IntFlag('F', {n: 1 << i for i, n in enumerate('START STOP PAUSE RETURN_HOME FAN_SPEED STATE'.split())})
    mod('homeassistant'); mod('homeassistant.components')
    mod('homeassistant.components.vacuum', StateVacuumEntity=object, VacuumEntityFeature=Flags, VacuumActivity=NS())
    mod('homeassistant.components.binary_sensor', BinarySensorEntity=object, BinarySensorDeviceClass=NS(OCCUPANCY='o', BATTERY_CHARGING='b', PROBLEM='p'))
    mod('homeassistant.components.sensor', SensorEntity=object, SensorDeviceClass=NS(BATTERY='b', ENUM='e', DURATION='d'), SensorStateClass=NS(MEASUREMENT='m'))
    for p, c in [('select','SelectEntity'),('button','ButtonEntity'),('switch','SwitchEntity'),('camera','Camera')]:
        mod(f'homeassistant.components.{p}', **{c: object})
    mod('homeassistant.components.number', NumberEntity=object, NumberMode=NS(SLIDER='slider'))
    mod('homeassistant.components.time', TimeEntity=object)
    mod('homeassistant.config_entries', ConfigEntry=object)
    mod('homeassistant.const', PERCENTAGE='%', UnitOfTime=NS(SECONDS='s'), EntityCategory=NS(CONFIG='config'))
    mod('homeassistant.core', HomeAssistant=object, ServiceCall=object, callback=lambda f: f)
    mod('homeassistant.exceptions', HomeAssistantError=RuntimeError)
    h = mod('homeassistant.helpers')
    h.config_validation = mod('homeassistant.helpers.config_validation', string=str, entity_id=str, ensure_list=lambda v: v)
    h.entity_registry = mod('homeassistant.helpers.entity_registry', async_get=lambda hass: None)
    mod('homeassistant.helpers.entity', DeviceInfo=dict)
    mod('homeassistant.helpers.entity_platform', AddEntitiesCallback=object)
    mod('homeassistant.helpers.storage', Store=object)
    mod('homeassistant.helpers.aiohttp_client', async_get_clientsession=lambda hass: None)
    mod('paho'); mod('paho.mqtt'); mod('paho.mqtt.client')
    pkg = mod('compat_integration'); pkg.__path__ = [root]
    with patch.dict(sys.modules, stubs):
        rooms = ['Living Room', 'Kitchen 1']
        mqtt = NS(connected=True, room_names=rooms, room_preferences=[], state={})
        coord = NS(mqtt=mqtt, cached_room_names=rooms, room_names=rooms, state={}, serial='7VD-AU-ABC1234A',
                   current_mode='Vacuum', async_add_listener=lambda l: None)
        if room_labels is not None:
            from room_types_for_tests import translate_room
            coord.room_label = lambda name: translate_room(name, room_labels)
        entry = NS(entry_id='e', data={'serial': '7VD-AU-ABC1234A', 'device_name': 'Robot', 'product_type': '804',
                                       'auth_token': 't'}, options={})
        hass = NS(data={'dyson_spot_scrub': {'e': coord}},
                  services=NS(has_service=lambda d, n: True, async_register=None))
        out = []
        for platform in ['vacuum', 'binary_sensor', 'sensor', 'select', 'button', 'switch', 'camera',
                         'number', 'time']:
            m = importlib.import_module(f'compat_integration.{platform}')
            ents = []
            asyncio.run(m.async_setup_entry(hass, entry, ents.extend))
            for e in ents:
                di = {k: sorted(map(list, v)) if isinstance(v, set) else v for k, v in e._attr_device_info.items()}
                out.append([platform, type(e).__name__, e._attr_unique_id, di])
                NAMES[e._attr_unique_id] = display_name(platform, e)
                PLACEHOLDERS[e._attr_unique_id] = getattr(e, '_attr_translation_placeholders', None)
                CATEGORIES[e._attr_unique_id] = getattr(e, '_attr_entity_category', None)
        return sorted(out, key=lambda r: r[2])


NAMES = {}
PLACEHOLDERS = {}
CATEGORIES = {}
EN = json.loads((Path(ROOT) / "translations/en.json").read_text())


def display_name(platform, entity):
    """The English name Home Assistant shows: a fixed name, or the translated one (#57)."""
    if getattr(entity, '_attr_name', None) is not None or not getattr(entity, '_attr_translation_key', None):
        return getattr(entity, '_attr_name', None)
    name = EN["entity"][platform][entity._attr_translation_key]["name"]
    return name.format(**getattr(entity, '_attr_translation_placeholders', {}) or {})


class EntityCompatibilityTests(unittest.TestCase):
    def test_unique_ids_and_devices_match_snapshot(self):
        self.assertEqual(collect(str(ROOT)), json.loads(SNAPSHOT.read_text()))

    def test_english_names_are_unchanged_apart_from_rooms(self):
        """#57: names are translated now, but English names are the old ones, except that
        room entities no longer start with "Room" (settings moved to Configuration instead)."""
        collect(str(ROOT))
        before = json.loads((SNAPSHOT.parent / "entity_names_en.json").read_text())
        expected = {uid: name[len("Room "):] if "_room_" in uid and name.startswith("Room ") else name
                    for uid, name in before.items()}
        self.assertEqual({uid: NAMES[uid] for uid in before}, expected)
        self.assertEqual(NAMES['7VD-AU-ABC1234A_room_kitchen_1'], 'Kitchen 1')

    def test_settings_are_in_configuration_and_rooms_in_controls(self):
        collect(str(ROOT))
        for uid, category in CATEGORIES.items():
            with self.subTest(uid):
                setting = "_station_" in uid or "_setting_" in uid
                self.assertEqual(category, 'config' if setting else None)
        self.assertTrue(any(c == 'config' for c in CATEGORIES.values()))

    def test_repetitions_select_is_renamed_but_keeps_its_id(self):
        """#35: shown as Clean Repetitions; the card finds it by unique ID."""
        collect(str(ROOT))
        uid = '7VD-AU-ABC1234A_room_living_room_mop_repetitions'
        self.assertEqual(NAMES[uid], 'Living Room Clean Repetitions')
        self.assertFalse(any('Mop Repetitions' in str(n) for n in NAMES.values()))

    def test_standard_room_types_are_translated_in_names_only(self):
        """#57: Dutch names for standard room types; IDs and the card's room names stay English."""
        nl = json.loads((ROOT / "translations/nl.json").read_text())["selector"]["room_type"]["options"]
        english = collect(str(ROOT))
        self.assertEqual(collect(str(ROOT), nl), english)          # same unique IDs and devices
        rooms = {uid: p["room"] for uid, p in PLACEHOLDERS.items() if p and "room" in p}
        self.assertEqual(rooms['7VD-AU-ABC1234A_room_living_room'], 'Woonkamer')
        self.assertEqual(rooms['7VD-AU-ABC1234A_room_kitchen_1'], 'Keuken 1')
        self.assertEqual(rooms['7VD-AU-ABC1234A_room_living_room_mop_repetitions'], 'Woonkamer')


if __name__ == "__main__":
    unittest.main()
