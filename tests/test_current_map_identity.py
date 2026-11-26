"""Never show an arbitrary saved floor when no map is reported as current."""
import unittest
from unittest.mock import AsyncMock, patch
import test_retry_after as retry_tests


class CurrentMapIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        retry_tests.RetryAfterTests.setUp(self)

    async def test_unknown_or_ambiguous_map_does_not_fetch_the_first_floor(self):
        cases=[[],[{"id":"A","isCurrentMap":False},{"id":"B","isCurrentMap":False}],
               [{"id":"A","isCurrentMap":True},{"id":"B","isCurrentMap":True}],
               [{"id":"A","isCurrentMap":False}],[{"id":"A"}]]
        for maps in cases:
            with self.subTest(maps=maps), patch.object(self.api,"get_map_metadata",AsyncMock(return_value=maps)), \
                    patch.object(self.api,"get_map",AsyncMock()) as fetch:
                with self.assertRaises(self.api.DysonApiError):
                    await self.api.get_current_map("token","robot")
                fetch.assert_not_awaited()

    async def test_one_confirmed_current_map_is_the_only_map_fetched(self):
        maps=[{"id":"A","isCurrentMap":False},{"id":"B","isCurrentMap":True}]
        with patch.object(self.api,"get_map_metadata",AsyncMock(return_value=maps)), \
                patch.object(self.api,"get_map",AsyncMock(return_value={"zones":[]})) as fetch:
            self.assertEqual(await self.api.get_current_map("token","robot"),("B",{"zones":[]}))
            fetch.assert_awaited_once_with("token","robot","B",session=None)

    async def test_reported_map_identity_wins_over_delayed_metadata_flags(self):
        maps=[{"id":"A","isCurrentMap":True},{"id":"B","isCurrentMap":False}]
        with patch.object(self.api,"get_map_metadata",AsyncMock(return_value=maps)), \
                patch.object(self.api,"get_map",AsyncMock(return_value={"zones":[]})) as fetch:
            self.assertEqual(await self.api.get_current_map("token","robot",expected_map_id="B"),
                             ("B",{"zones":[]}))
            fetch.assert_awaited_once_with("token","robot","B",session=None)

    async def test_zero_or_missing_reported_map_cannot_fall_back_to_floor_a(self):
        maps=[{"id":"A","isCurrentMap":True}]
        for identity in ["0","missing",""]:
            with self.subTest(identity=identity), patch.object(self.api,"get_map_metadata",AsyncMock(return_value=maps)), \
                    patch.object(self.api,"get_map",AsyncMock()) as fetch:
                with self.assertRaises(self.api.DysonApiError):
                    await self.api.get_current_map("token","robot",expected_map_id=identity)
                fetch.assert_not_awaited()
