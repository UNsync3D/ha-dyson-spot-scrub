# Saved-map selection

This development branch adds selection of existing maps on Spot+Scrub AI / RB05. It does not add map creation, deletion, renaming or editing actions; those remain in MyDyson. The integration version is unchanged while the feature is tested.

## Home Assistant interface

- Entity: a select on the robot's device, unique ID `<serial>_active_map`.
- Name: Map (translated in English, German, Dutch and French).
- Options: saved map names. Duplicate names gain ID-based labels; identities remain the persistent map IDs.
- State: the option matching the robot's reported `persistentMapId`, not an optimistic selection.
- Attributes: `maps` (objects with string `map_id`, `name`, `option`), `map_id` (current string ID or null), `can_change` (Boolean) and `switching` (Boolean).
- Action: standard `select.select_option`, targeting this select and an option from its current options list.

The Lovelace Cleaning Planner uses this registry identity on the configured robot, and displays a map menu only when the select exists. It waits for both the select and vacuum's `map_id` to agree. Older integration builds keep their existing card layout. Local room drafts must be discarded before switching from the card.

## Refresh and commands

The integration queries `service.get_map_list` when the select is set up, after reconnect, on reported active-map changes, after observed MyDyson rename/delete/mapping events, and every 60 seconds. Requests are correlated by message ID, including app requests observed on MQTT. A newer applied list supersedes older outstanding replies. A malformed or unsuccessful response keeps the last valid list; a valid empty list is accepted.

Selecting a map sends one `service.set_cur_map` with its numeric `map_id`, waits for a successful acknowledgement/result and the matching robot telemetry, then refreshes the list. Acknowledgement and state each have a 15-second deadline. There are no automatic retries or cleaning-start commands. The existing start lock prevents a new clean from racing this transition; active cleaning, paused/returning/mapping states and blocking dock work reject selection.

The command and list shapes come from RB05 app captures on 25/26 November 2026. The same native room ID can occur on different maps, so room ID alone is not map identity. Deleting selected B in MyDyson was observed to select remaining A automatically; the refreshed list then contains only A.

## Entity retention and remaining validation

Normal switches retain existing room entities and use the existing active-room refresh path. Removed map IDs disappear from the map selector. Removing the old room entities and registry entries of a deleted map is a separate follow-up: existing room entities use names, and shared names/room IDs require verified map ownership before cleanup.

Run `python -X utf8 -m unittest discover -s tests -v` with the CI dependencies installed. Tests use mocked MQTT and Home Assistant, including list ordering, deletion, duplicate names, readiness, confirmation, timeouts, lifecycle and the existing entity-identity snapshot. Live installation still needs a two-map switch in both directions from the select and card, a MyDyson rename/deletion, reconnect, and confirmation that existing room cleaning still works.
