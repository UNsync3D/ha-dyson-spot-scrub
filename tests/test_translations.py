"""Translations (#57): English, Dutch and German cover the same text and placeholders."""
import ast
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/dyson_spot_scrub"
LANGS = {p.stem: json.loads(p.read_text()) for p in (ROOT / "translations").glob("*.json")}


def flatten(data, path=""):
    out = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(flatten(value, f"{path}/{key}"))
        else:
            out[f"{path}/{key}"] = value
    return out


def placeholders(text):
    return set(re.findall(r"\{(\w+)\}", text))


class TranslationTests(unittest.TestCase):
    def test_languages(self):
        self.assertEqual(set(LANGS), {"en", "nl", "de"})

    def test_strings_json_matches_english(self):
        self.assertEqual(json.loads((ROOT / "strings.json").read_text()), LANGS["en"])

    def test_every_language_has_every_phrase_and_placeholder(self):
        english = flatten(LANGS["en"])
        for lang, data in LANGS.items():
            with self.subTest(lang=lang):
                phrases = flatten(data)
                self.assertEqual(set(phrases), set(english))
                for key, text in english.items():
                    self.assertEqual(placeholders(phrases[key]), placeholders(text), key)
                    self.assertTrue(phrases[key].strip(), key)

    def test_braces_are_only_placeholders(self):
        """Hassfest rejects any other use of curly braces."""
        for lang, data in LANGS.items():
            for key, text in flatten(data).items():
                for inner in re.findall(r"\{([^}]*)\}", text):
                    self.assertRegex(inner, r"^[a-zA-Z_][a-zA-Z0-9_]*$", f"{lang}{key}")

    def test_every_translation_key_in_the_code_has_a_name(self):
        """Literal translation keys in the code exist for their platform."""
        names = LANGS["en"]["entity"]
        for path in ROOT.glob("*.py"):
            platform = path.stem
            if platform not in names:
                continue
            for key in re.findall(r'_attr_translation_key\s*=\s*"([a-z_]+)"', path.read_text()):
                with self.subTest(platform=platform, key=key):
                    self.assertIn(key, names[platform])

    def test_no_fixed_entity_names_left(self):
        """Every entity takes its name from the translations, except the vacuum (the device name)."""
        for path in ROOT.glob("*.py"):
            fixed = [line.strip() for line in path.read_text().splitlines()
                     if re.search(r"_attr_name\s*=", line) and "None" not in line]
            self.assertEqual(fixed, [], path.name)


if __name__ == "__main__":
    unittest.main()
