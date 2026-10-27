"""Offline regression tests. HA lifecycle/storage and network APIs are mocked.

Run: python -m unittest discover -s tests -v
The camera, coordinator and MQTT classifiers are imported from this checkout.
These tests do not replace testing with Home Assistant and a real robot.
"""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


def module(name, **attributes):
    value = ModuleType(name)
    value.__dict__.update(attributes)
    sys.modules[name] = value
    return value


class Camera:
    def __init__(self):
        self.remove_callbacks = []

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, fn):
        self.remove_callbacks.append(fn)

    def async_write_ha_state(self):
        pass

    async def async_will_remove_from_hass(self):
        for fn in self.remove_callbacks:
            fn()


class Store:
    def __init__(self, *args):
        self.async_load = AsyncMock(return_value={})
        self.async_save = AsyncMock()


class ApiError(Exception):
    pass


class RateLimitError(ApiError):
    def __init__(self, retry_after):
        super().__init__('HTTP 429')
        self.retry_after = retry_after


module('homeassistant')
module('homeassistant.components')
module('homeassistant.components.camera', Camera=Camera)
module('homeassistant.config_entries', ConfigEntry=object)
module('homeassistant.core', HomeAssistant=object, callback=lambda fn: fn)
module('homeassistant.helpers')
module('homeassistant.helpers.entity', DeviceInfo=dict)
module('homeassistant.helpers.entity_platform', AddEntitiesCallback=object)
module('homeassistant.helpers.storage', Store=Store)
module('homeassistant.helpers.aiohttp_client', async_get_clientsession=lambda hass: None)
module('paho')
module('paho.mqtt')
module('paho.mqtt.client')
package = module('retention_integration')
package.__path__ = [str(Path(__file__).resolve().parents[1] /
                        'custom_components/dyson_spot_scrub')]
module('retention_integration.dyson_api', DysonApiError=ApiError,
       DysonRateLimitError=RateLimitError,
       get_current_map=AsyncMock(), get_map_metadata=AsyncMock(),
       get_live_map=AsyncMock(), get_iot_credentials=AsyncMock(), get_clean_history=AsyncMock())
camera_module = importlib.import_module('retention_integration.camera')
coordinator_module = importlib.import_module('retention_integration.coordinator')
real_render_map = camera_module.render_map


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1000.0
        self.enterContext(patch.object(camera_module, 'time',
                                      SimpleNamespace(monotonic=lambda: self.now)))
        self.enterContext(patch.object(coordinator_module, 'time',
                                      SimpleNamespace(monotonic=lambda: self.now)))
        self.path = {'cleanPath': [[1, 1], [2, 2]]}
        self.fetch = self.enterContext(patch.object(
            coordinator_module, 'get_live_map', AsyncMock(side_effect=lambda *args, **kwargs: self.path)))
        self.rendered = []

        def render(map_data, metadata, live, options=None, **kwargs):
            # Original retention assertions focus on API data, not path metadata.
            original = {k: v for k, v in live.items() if k != '_pathModes'} if live else live
            self.rendered.append(original)
            return repr(original).encode() if original else b'static'

        self.enterContext(patch.object(camera_module, 'render_map', render))
        async def executor(fn, *args):
            return fn(*args)
        self.hass = SimpleNamespace(async_add_executor_job=executor)
        self.entry = SimpleNamespace(data={
            'auth_token': 'test', 'serial': 'test', 'device_name': 'Dyson'}, options={},
            add_update_listener=lambda fn: lambda: None)
        self.coordinator = coordinator_module.DysonCoordinator(
            self.hass, 'test', 'test', 'NROB', self.entry)
        self.coordinator.mqtt = SimpleNamespace(connected=True, state={})
        self.camera = camera_module.DysonMapCamera(self.coordinator, self.entry)
        self.camera.hass = self.hass
        self.camera._map_data = {'zones': []}
        self.camera._metadata = []
        self.camera._cache_ts = self.now
        await self.camera.async_added_to_hass()

    def state(self, status, action='NONE', map_id=1, **extra):
        self.coordinator.mqtt.state = dict(
            state=status, fullCleanAction=action, persistentMapId=map_id, **extra)
        self.coordinator._async_notify_listeners()

    async def start(self, action='VACUUMING', **extra):
        self.state('FULL_CLEAN_RUNNING', action, **extra)
        result = await self.camera.async_camera_image()
        self.assertNotEqual(result, b'static')
        return result

    async def test_finish_timer_starts_without_camera_viewer(self):
        image = await self.start()
        self.state('FULL_CLEAN_FINISHED')
        self.now += 299
        self.state('FULL_CLEAN_FINISHED')  # Must not extend the deadline.
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.assertEqual(self.fetch.await_count, 1)
        self.now += 1
        self.assertEqual(await self.camera.async_camera_image(), b'static')
        self.assertEqual(self.camera.frame_interval, 60)
        self.assertIsNone(self.camera._last_cleaning_image)

    async def test_pause_resume_and_abort(self):
        image = await self.start()
        self.state('FULL_CLEAN_PAUSED', 'VACUUMING')
        self.now += 900
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.assertIsNone(self.camera._cleaning_finished_at)
        self.assertEqual(self.fetch.await_count, 1)
        self.state('FULL_CLEAN_RUNNING', 'VACUUMING')
        self.path = {'cleanPath': [[1, 1], [2, 2], [3, 3]]}
        image = await self.camera.async_camera_image()
        self.assertEqual(self.rendered[-1], self.path)
        self.state('FULL_CLEAN_PAUSED')
        self.state('ABORTED')
        self.state('CHARGING')
        self.now += 299
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.now += 1
        self.assertEqual(await self.camera.async_camera_image(), b'static')

    async def test_vacuum_then_mop_is_one_session(self):
        image = await self.start(sweepType=7)
        generation = self.camera._cleaning_generation
        self.state('FULL_CLEAN_RUNNING', sweepType=7)
        self.now += 600
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.assertEqual(self.fetch.await_count, 1)
        self.state('FULL_CLEAN_CHARGING', dockState='WASHING_MOP', sweepType=7)
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.state('FULL_CLEAN_DISCOVERING', 'MOPPING', sweepType=7)
        self.path = {'cleanPath': []}
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.now += 5
        self.path = {'cleanPath': [[1, 1], [2, 2], [3, 3], [4, 4]]}
        image = await self.camera.async_camera_image()
        self.assertEqual(self.rendered[-1]['cleanPath'], self.path['cleanPath'])
        self.assertEqual(generation, self.camera._cleaning_generation)
        self.assertIsNone(self.camera._cleaning_finished_at)
        self.state('FULL_CLEAN_FINISHED')
        self.now += 299
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.now += 1
        self.assertEqual(await self.camera.async_camera_image(), b'static')

    async def test_new_cleaning_clears_old_image_and_shared_cache(self):
        await self.start()
        self.state('FULL_CLEAN_FINISHED')
        self.state('FULL_CLEAN_DISCOVERING')
        self.assertIsNone(self.coordinator._live_map_cache)
        self.assertEqual(await self.camera.async_camera_image(), b'static')
        self.state('FULL_CLEAN_RUNNING', 'MOPPING')
        self.path = {'cleanPath': [[10, 10], [11, 11]]}
        image = await self.camera.async_camera_image()
        self.assertEqual(image, repr(self.path).encode())
        self.assertEqual(self.fetch.await_count, 2)

    async def test_429_preserved_and_no_requests_during_retention(self):
        image = await self.start()
        self.now += 5
        self.fetch.side_effect = RateLimitError(30)
        self.assertEqual(await self.camera.async_camera_image(), image)
        deadline = self.coordinator._live_map_backoff_until
        self.state('FULL_CLEAN_FINISHED')
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.state('FULL_CLEAN_DISCOVERING')
        self.state('FULL_CLEAN_RUNNING', 'VACUUMING')
        self.assertEqual(await self.camera.async_camera_image(), b'static')
        self.assertEqual(self.coordinator._live_map_backoff_until, deadline)
        self.assertEqual(self.fetch.await_count, 2)

    async def test_disconnect_does_not_finish_cleaning(self):
        image = await self.start()
        self.coordinator.mqtt.connected = False
        self.coordinator._async_notify_listeners()
        self.now += 600
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.assertIsNone(self.camera._cleaning_finished_at)
        self.coordinator.mqtt.connected = True
        self.state('FULL_CLEAN_FINISHED')
        self.assertEqual(self.camera._cleaning_finished_at, self.now)

    async def test_map_switch_drops_old_static_and_cleaning_maps(self):
        await self.start()
        self.state('FULL_CLEAN_FINISHED', map_id=2)
        self.assertIsNone(self.camera._last_image)
        self.assertIsNone(self.camera._map_data)
        self.assertIsNone(self.coordinator._live_map_cache)

    async def test_late_fetch_cannot_restore_previous_session(self):
        await self.start()
        self.now += 5
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.path
        self.fetch.side_effect = delayed
        pending = asyncio.create_task(self.camera.async_camera_image())
        await entered.wait()
        self.state('FULL_CLEAN_FINISHED')
        self.state('FULL_CLEAN_DISCOVERING')
        release.set()
        self.assertIsNone(await pending)
        self.assertIsNone(self.coordinator._live_map_cache)
        self.assertIsNone(self.camera._last_cleaning_image)

    async def test_late_render_cannot_restore_previous_session(self):
        await self.start()
        self.now += 5
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(fn, *args):
            entered.set()
            await release.wait()
            return fn(*args)
        self.hass.async_add_executor_job = delayed
        pending = asyncio.create_task(self.camera.async_camera_image())
        await entered.wait()
        self.state('ABORTED')
        self.state('FULL_CLEAN_DISCOVERING')
        release.set()
        self.assertIsNone(await pending)
        self.assertIsNone(self.camera._last_cleaning_image)

    async def test_multiple_viewers_share_fetch(self):
        self.state('FULL_CLEAN_RUNNING', 'VACUUMING_AND_MOPPING')
        images = await asyncio.gather(*(self.camera.async_camera_image() for _ in range(4)))
        self.assertTrue(all(image == images[0] for image in images))
        self.assertEqual(self.fetch.await_count, 1)

    async def test_unload_clears_ram_and_removes_listener(self):
        await self.start()
        await self.camera.async_will_remove_from_hass()
        self.assertEqual(self.coordinator._listeners, [])
        self.assertIsNone(self.camera._last_cleaning_image)
        self.camera._store.async_save.assert_not_awaited()

    async def test_no_path_means_no_retained_cleaning_image(self):
        self.path = {'cleanPath': []}
        await self.start()
        self.assertIsNone(self.camera._last_cleaning_image)
        self.state('FULL_CLEAN_FINISHED')
        self.assertEqual(await self.camera.async_camera_image(), b'static')

    async def test_renderer_failure_keeps_previous_frame(self):
        image = await self.start()
        self.now += 5
        with patch.object(camera_module, 'render_map', side_effect=RuntimeError('test')):
            self.assertEqual(await self.camera.async_camera_image(), image)
        self.state('FULL_CLEAN_FINISHED')
        self.assertEqual(await self.camera.async_camera_image(), image)

    async def test_old_static_fetch_is_discarded_after_map_change(self):
        self.camera._map_data = None
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return 1, {'zones': [{'id': 'old'}]}
        self.state('CHARGING')
        with patch.object(camera_module, 'get_current_map', side_effect=delayed), \
             patch.object(camera_module, 'get_map_metadata', AsyncMock(return_value=[])):
            pending = asyncio.create_task(self.camera.async_camera_image())
            await entered.wait()
            self.state('CHARGING', map_id=2)
            release.set()
            self.assertIsNone(await pending)
        self.assertIsNone(self.camera._map_data)

    async def test_existing_room_presentation_storage_is_preserved(self):
        segments = [{'type': 0, 'start': [1, 1], 'end': [2, 2]}]
        self.path = {**self.path, 'zones': [{'id': 1, 'presentation': segments}]}
        await self.start()
        self.camera._store.async_save.assert_awaited_once_with({'1': segments})
        self.state('FULL_CLEAN_FINISHED')
        self.now += 300
        await self.camera.async_camera_image()
        self.assertEqual(self.camera._presentation_cache, {'1': segments})

    async def options(self, **values):
        self.entry.options.update(values)
        await self.camera._async_options_updated(self.hass, self.entry)

    async def test_zero_retention_still_holds_pause_but_not_finished(self):
        await self.options(map_retention_minutes=0)
        image = await self.start()
        self.state('FULL_CLEAN_PAUSED')
        self.now += 500
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.state('FULL_CLEAN_FINISHED')
        self.assertEqual(await self.camera.async_camera_image(), b'static')

    async def test_keep_last_uses_original_end_time_when_disabled(self):
        await self.options(map_keep_last=True)
        image = await self.start()
        self.state('FULL_CLEAN_FINISHED')
        self.now += 4000
        self.assertEqual(await self.camera.async_camera_image(), image)
        await self.options(map_keep_last=False)
        self.assertEqual(await self.camera.async_camera_image(), b'static')
        self.assertEqual(self.fetch.await_count, 1)

    async def test_configurable_sixty_minutes_and_new_clean_reset(self):
        await self.options(map_retention_minutes=60)
        image = await self.start()
        self.state('FULL_CLEAN_FINISHED')
        self.now += 3599
        self.assertEqual(await self.camera.async_camera_image(), image)
        self.now += 1
        self.assertEqual(await self.camera.async_camera_image(), b'static')
        await self.options(map_keep_last=True)
        await self.start()
        self.state('FULL_CLEAN_FINISHED')
        self.state('FULL_CLEAN_DISCOVERING')
        self.assertIsNone(self.camera._last_source)
        self.assertEqual(self.camera._path_history.coords, [])

    async def test_repaint_retained_data_without_fetch_and_retry_changed_options(self):
        def render(map_data, metadata, live, options=None, **kwargs):
            return str(options['map_path_width']).encode()
        with patch.object(camera_module, 'render_map', render):
            self.assertEqual(await self.start(), b'3')
            self.state('FULL_CLEAN_FINISHED')
            await self.options(map_path_width=8)
            self.assertEqual(await self.camera.async_camera_image(), b'8')
            self.assertEqual(self.fetch.await_count, 1)
            self.assertEqual(self.camera._last_cleaning_image, b'8')
            # Options change while the executor is working: only newest wins.
            await self.options(map_path_width=9)
            changed = False
            async def executor(fn, *args):
                nonlocal changed
                result = fn(*args)
                if not changed:
                    changed = True
                    await self.options(map_path_width=10)
                return result
            self.hass.async_add_executor_job = executor
            self.assertEqual(await self.camera.async_camera_image(), b'10')
            self.assertEqual(self.fetch.await_count, 1)

    async def test_repaint_during_api_error_uses_cached_source(self):
        with patch.object(camera_module, 'render_map',
                          side_effect=lambda m, md, live, opts, **kw: str(opts['map_glow_width']).encode()):
            self.assertEqual(await self.start(), b'6')
            self.now += 5
            self.fetch.side_effect = RateLimitError(30)
            await self.options(map_glow_width=0)
            self.assertEqual(await self.camera.async_camera_image(), b'0')
            self.assertEqual(self.camera._last_cleaning_image, b'0')

    async def test_late_repaint_cannot_restore_previous_session(self):
        await self.start()
        self.state('FULL_CLEAN_FINISHED')
        await self.options(map_path_width=8)
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(fn, *args):
            entered.set()
            await release.wait()
            return fn(*args)
        self.hass.async_add_executor_job = delayed
        task = asyncio.create_task(self.camera.async_camera_image())
        await entered.wait()
        self.state('FULL_CLEAN_DISCOVERING')
        release.set()
        await task
        self.assertIsNone(self.camera._last_cleaning_image)

    async def test_path_colors_preserve_vacuum_prefix_without_mutating_api_cache(self):
        await self.start()
        self.state('FULL_CLEAN_RUNNING', 'MOPPING')
        self.now += 5
        self.path = {'cleanPath': [[1, 1], [2, 2], [3, 3]]}
        await self.camera.async_camera_image()
        self.assertEqual(self.camera._last_source[2]['_pathModes'], ['VACUUMING', 'MOPPING'])
        self.assertNotIn('_pathModes', self.coordinator._live_map_cache)

    async def test_real_png_renderer_and_retention(self):
        import io
        from PIL import Image
        with patch.object(camera_module, 'render_map', real_render_map):
            image = await self.start()
        png = Image.open(io.BytesIO(image))
        self.assertEqual(png.format, 'PNG')
        self.assertNotEqual(png.size, (500, 80), 'Renderer returned its error card')
        self.state('FULL_CLEAN_FINISHED')
        self.assertEqual(await self.camera.async_camera_image(), image)


if __name__ == '__main__':
    unittest.main()
