"""The MyDyson app's standard room types, for translated room names (#57).

When a room is given a standard type in the app, the robot stores the type
with an English name (`{"type":"kitchen","name":"Kitchen"}`) and the app
shows it in the phone's language. Home Assistant only receives the English
name, so entity names translate it the same way. The English name stays
everywhere else (unique IDs, the vacuum's `rooms` list, actions), so the
dashboard card and automations are unaffected.

The list is the app's "Choose a preset" list (Nov 2026), which hides types
already on the map; Kitchen, Dining and Toilet come from robot data.
Pure functions: no Home Assistant imports.
"""
from __future__ import annotations

import re

# Translation key → the English name the robot stores.
ROOM_TYPES: dict[str, str] = {
    "balcony": "Balcony",
    "bathroom": "Bathroom",
    "bedroom": "Bedroom",
    "boxroom": "Boxroom",
    "cloakroom": "Cloakroom",
    "closet": "Closet",
    "conservatory": "Conservatory",
    "dining": "Dining",
    "ensuite": "Ensuite",
    "entrance": "Entrance",
    "family_room": "Family Room",
    "guest_bathroom": "Guest Bathroom",
    "guest_bedroom": "Guest Bedroom",
    "guest_room": "Guest Room",
    "hallway": "Hallway",
    "kids_bedroom": "Kids Bedroom",
    "kitchen": "Kitchen",
    "laundry_room": "Laundry Room",
    "living_room": "Living Room",
    "nursery": "Nursery",
    "office": "Office",
    "pantry": "Pantry",
    "play_room": "Play Room",
    "primary_bathroom": "Primary Bathroom",
    "primary_bedroom": "Primary Bedroom",
    "recreation_room": "Recreation Room",
    "storage_room": "Storage Room",
    "study": "Study",
    "toilet": "Toilet",
    "utility_room": "Utility Room",
}
# Other spellings of the same types.
_ALIASES = {"dining room": "dining"}
_BY_NAME = {name.casefold(): key for key, name in ROOM_TYPES.items()}
_BY_NAME.update(_ALIASES)
# Dyson numbers rooms that share a name: "Kitchen 1", "Kitchen 2".
_SUFFIX = re.compile(r"^(.*?)(\s+\d+)?$")


def room_type(name: str) -> tuple[str | None, str]:
    """(type key, number suffix) for a room's display name; key None if not standard."""
    match = _SUFFIX.match(name.strip())
    base, suffix = match.group(1), match.group(2) or ""
    return _BY_NAME.get(base.casefold()), suffix


def translate_room(name: str, translations: dict[str, str]) -> str:
    """The room's name in the user's language, if it's a standard type; else unchanged."""
    if not isinstance(name, str) or not translations:
        return name
    key, suffix = room_type(name)
    translated = translations.get(key) if key else None
    return f"{translated}{suffix}" if translated else name
