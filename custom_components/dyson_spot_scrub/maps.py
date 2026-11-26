"""Saved-map identities and labels; no mapping, deletion or editing commands."""
from __future__ import annotations

from collections import Counter


def map_id(value) -> str | None:
    """Canonical positive ID, losslessly representable by the HA frontend."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdecimal():
        return None
    number = int(text)
    return str(number) if 0 < number <= 9007199254740991 else None


def map_catalogue(raw) -> list[dict[str, str]] | None:
    """Validate a full get_map_list reply; invalid data never empties the list."""
    if not isinstance(raw, list):
        return None
    result, ids = [], set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        identity = map_id(item.get("id"))
        name = item.get("name")
        if (identity is None or identity in ids or not isinstance(name, str)
                or not name.strip() or type(item.get("cur")) is not bool):
            return None
        ids.add(identity)
        result.append({"map_id": identity, "name": name.strip()})
    if sum(item["cur"] for item in raw) > 1:
        return None
    counts = Counter(item["name"] for item in result)
    for item in result:
        item["option"] = (item["name"] if counts[item["name"]] == 1
                          else f'{item["name"]} [{item["map_id"]}]')
    if len({item["option"] for item in result}) != len(result):
        for item in result:
            item["option"] = f'{item["map_id"]}: {item["name"]}'
    return result
