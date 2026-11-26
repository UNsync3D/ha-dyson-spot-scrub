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

Live follow-up on 26 November showed that RB05 reports `prop.post.current_map_id` within a second, while the next legacy `CURRENT-STATE` map ID can lag by 20-24 seconds. Valid JDM map properties (including partial `prop.get` replies) now update the same canonical map identity, confirmation event and room-refresh path. An acknowledgement alone still does not confirm the switch. A sanitized fixture records this ordering.

The camera also binds static-map fetches to the reported identity when map selection is enabled. ID `0` means no selected saved map in this capture; the API never substitutes the first saved floor, and reported B cannot load A because of delayed REST current-map flags. The legacy fallback has been removed.

The command and list shapes come from RB05 app captures on 25/26 November 2026. The same native room ID can occur on different maps, so room ID alone is not map identity. Deleting selected B in MyDyson was observed to select remaining A automatically; the refreshed list then contains only A.

## Schedules on multiple maps

The 26 November capture shows five schedules on A and zero on B. The robot's `order_total` / `get_order` summary describes the selected map. Comparing that summary with every schedule retained for the robot incorrectly marked empty B as out of sync.

With saved-map selection enabled, Home Assistant now retains all known orders but keeps counts, enabled counts, synchronization status and checksum baselines separately for each map. The Schedules sensor and calendar show the reported active map's entries. Sensor attributes `map_id`, `known`, `on`, `unknown` and `in_sync` describe that map; `known_all_maps` counts all retained orders. Each schedule and individual switch exposes its own `map_id`.

An individual schedule switch is unavailable on another map, during an integration map switch, or when no saved map is reported (ID `0`). This does not turn the stored schedule off. Returning to its map restores the same entity and saved enabled state. Create/edit/toggle/delete actions check map ownership in the coordinator and immediately before MQTT dispatch. Schedule writes share the existing map/start lock. Time conflicts are checked within the selected map.

Every reported map change requests a fresh schedule summary. Observed app queries and integration queries are correlated by message ID and map revision. Replies from a previous selection, before a confirmed schedule edit, after expiry, or older than an applied query are ignored. A combined map/count property report applies its map first. Unsolicited `order_total` messages carry no map ID and use the map reported at receipt; they cannot confirm a checksum. Genuine count/checksum mismatches still require learning the missing or changed schedules in MyDyson.

Existing stored orders are preserved on upgrade. An old global checksum is not assigned to any map; the first fresh map-specific summary establishes its baseline. Per-map baselines and observed pending-baseline changes survive restart. Models without saved-map selection retain the legacy schedule interface.

The Lovelace card filters by persistent map ID, including list rows, enabled badge and countdown, and uses only a matching sensor summary. It closes obsolete editors and never interprets a row disappearing on map change as successful deletion. Confirmed empty maps show an empty-map message instead of an instruction to synchronize schedules.

Automated regressions cover empty B, schedules on both maps with equal counts, independent checksums, genuine missing/changed orders, old storage, restart, delayed/reordered replies, unknown map ID, retained switch identities/states and write interlocks. Live follow-up should switch A/B repeatedly with schedules on both maps, compare the HA sensor/switches and MyDyson, and check genuine edits/missing schedules after reconnect. Actual scheduled execution is not established by these offline tests.

## Entity retention and remaining validation

Normal switches retain existing room entities and use the existing active-room refresh path. Removed map IDs disappear from the map selector. Removing the old room entities and registry entries of a deleted map is a separate follow-up: existing room entities use names, and shared names/room IDs require verified map ownership before cleanup.

Home is unchanged: `vacuum.return_to_base` sends legacy `ABORT`, with Dyson's cloud adding `service.start_recharge`. Stop is a distinct legacy `STOP`. The supplied stationless return trace is Home, not a captured MyDyson cancellation or normal completion; returning to the placement point through those app flows needs its own comparison.

Run `python -X utf8 -m unittest discover -s tests -v` with the CI dependencies installed. Tests use mocked MQTT and Home Assistant, including list ordering, deletion, duplicate names, readiness, confirmation, timeouts, lifecycle and the existing entity-identity snapshot. Live installation still needs a two-map switch in both directions from the select and card, a MyDyson rename/deletion, reconnect, and confirmation that existing room cleaning still works.
