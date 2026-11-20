"""Standard room types in the user's language (#57).

The list is the MyDyson app's "Choose a preset" list (the maintainer's
screenshots, Nov 2026); Kitchen, Dining and Toilet come from robot data.
"""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
_spec = importlib.util.spec_from_file_location("room_types", ROOT / "room_types.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


def options(path):
    return json.loads(path.read_text())["selector"]["room_type"]["options"]


NL = options(ROOT / "translations/nl.json")
DE = options(ROOT / "translations/de.json")


class RoomTypeTests(unittest.TestCase):
    def test_every_language_has_every_type(self):
        for path in [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]:
            with self.subTest(path.name):
                self.assertEqual(set(options(path)), set(rt.ROOM_TYPES))

    def test_english_is_the_name_the_robot_stores(self):
        self.assertEqual(options(ROOT / "translations/en.json"), rt.ROOM_TYPES)
        self.assertEqual(options(ROOT / "strings.json"), rt.ROOM_TYPES)

    def test_robot_names(self):
        # As _room_display_name produces them: title case, a space before a number.
        self.assertEqual(rt.translate_room("Kitchen", NL), "Keuken")
        self.assertEqual(rt.translate_room("Living Room", DE), "Wohnzimmer")
        self.assertEqual(rt.translate_room("Kitchen 2", NL), "Keuken 2")
        self.assertEqual(rt.translate_room("living room", NL), "Woonkamer")
        self.assertEqual(rt.translate_room("Dining Room", DE), "Esszimmer")

    def test_custom_names_are_unchanged(self):
        for name in ("Downstairs", "Kitchen Island", "Kevin's Room", "Zone 3", "", "2"):
            with self.subTest(name):
                self.assertEqual(rt.translate_room(name, NL), name)

    def test_english_or_missing_translations(self):
        self.assertEqual(rt.translate_room("Kitchen", {}), "Kitchen")
        self.assertEqual(rt.translate_room("Kitchen", {"bedroom": "Slaapkamer"}), "Kitchen")
        self.assertIsNone(rt.translate_room(None, NL))

    def test_translated_room_entities_start_with_the_room(self):
        """Wesley (#57): "Woonkamer", not "Kamer Woonkamer". English keeps "Room ..."."""
        for lang in ("nl", "de", "fr"):
            entity = json.loads((ROOT / f"translations/{lang}.json").read_text())["entity"]
            for platform, key in [("switch", "room"), ("select", "room_cleaning_mode"),
                                  ("select", "room_cleaning_strategy"), ("select", "room_water_level"),
                                  ("select", "room_mop_repetitions")]:
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(entity[platform][key]["name"].startswith("{room}"))


if __name__ == "__main__":
    unittest.main()
