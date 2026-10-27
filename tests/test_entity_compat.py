"""Entity compatibility: unique_ids, entity classes and device associations.

The Lovelace card discovers entities by unique_id and device, so these must not
change. The snapshot in fixtures/entity_registry_snapshot.json was generated
from main at c1f9d5d (before the security review); any intentional change to
an entity's identity must update it deliberately. Entities added since must
be listed in ADDED, so additions are deliberate too.
"""
import asyncio
import enum
import importlib
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
SNAPSHOT = Path(__file__).resolve().parent / "fixtures/entity_registry_snapshot.json"

# Entities added after the snapshot: (platform, class, unique_id suffix).
ADDED = {
    ("sensor", "DysonLastCleanSensor", "last_clean_start"),
    ("sensor", "DysonLastCleanDurationSensor", "last_clean_duration"),
    ("sensor", "DysonLastCleanAreaSensor", "last_clean_area"),
    ("sensor", "DysonLastCleanBatterySensor", "last_clean_battery_used"),
    ("sensor", "DysonLastCleanResultSensor", "last_clean_result"),
}
SERIAL = "7VD-AU-ABC1234A"


def collect(root):
    stubs = {}
    def mod(name, **kw):
        m = types.ModuleType(name); m.__dict__.update(kw); stubs[name] = m; return m
    Flags = enum.IntFlag('F', {n: 1 << i for i, n in enumerate('START STOP PAUSE RETURN_HOME FAN_SPEED STATE'.split())})
    mod('homeassistant'); mod('homeassistant.components')
    mod('homeassistant.components.vacuum', StateVacuumEntity=object, VacuumEntityFeature=Flags, VacuumActivity=NS())
    mod('homeassistant.components.binary_sensor', BinarySensorEntity=object, BinarySensorDeviceClass=NS(OCCUPANCY='o', BATTERY_CHARGING='b', PROBLEM='p'))
    mod('homeassistant.components.sensor', SensorEntity=object, SensorDeviceClass=NS(BATTERY='b', ENUM='e', DURATION='d', TIMESTAMP='t'), SensorStateClass=NS(MEASUREMENT='m'))
    for p, c in [('select','SelectEntity'),('button','ButtonEntity'),('switch','SwitchEntity'),('camera','Camera')]:
        mod(f'homeassistant.components.{p}', **{c: object})
    mod('homeassistant.config_entries', ConfigEntry=object)
    mod('homeassistant.const', PERCENTAGE='%', UnitOfTime=NS(SECONDS='s', MINUTES='min'))
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
        entry = NS(entry_id='e', data={'serial': '7VD-AU-ABC1234A', 'device_name': 'Robot', 'product_type': '804',
                                       'auth_token': 't'}, options={})
        hass = NS(data={'dyson_spot_scrub': {'e': coord}},
                  services=NS(has_service=lambda d, n: True, async_register=None))
        out = []
        for platform in ['vacuum', 'binary_sensor', 'sensor', 'select', 'button', 'switch', 'camera']:
            m = importlib.import_module(f'compat_integration.{platform}')
            ents = []
            asyncio.run(m.async_setup_entry(hass, entry, ents.extend))
            for e in ents:
                di = {k: sorted(map(list, v)) if isinstance(v, set) else v for k, v in e._attr_device_info.items()}
                out.append([platform, type(e).__name__, e._attr_unique_id, di])
        return sorted(out, key=lambda r: r[2])


class EntityCompatibilityTests(unittest.TestCase):
    def test_unique_ids_and_devices_match_snapshot(self):
        current = collect(str(ROOT))
        snapshot = json.loads(SNAPSHOT.read_text())
        # Every existing entity keeps its platform, class, unique_id and device.
        self.assertEqual([r for r in current if r in snapshot], snapshot)
        added = [r for r in current if r not in snapshot]
        self.assertEqual({(r[0], r[1], r[2].removeprefix(SERIAL + "_")) for r in added}, ADDED)
        self.assertEqual(len(added), len(ADDED))
        device = snapshot[0][3]
        for row in added:
            self.assertEqual(row[3], device, "new entities belong to the same device")


if __name__ == "__main__":
    unittest.main()
