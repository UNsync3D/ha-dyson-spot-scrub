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

Automated regressions cover empty B, schedules on both maps with equal counts, independent checksums, genuine missing/changed orders, old storage, restart, delayed/reordered replies, unknown map ID, retained switch identities/states and write interlocks. On 26 November 2026, the maintainer confirmed successful live acceptance after installing the update: schedules and availability follow map changes cleanly in both the integration and Lovelace card. Further app edits/missing schedules after reconnect and actual scheduled execution remain separate checks.

## Stationless-map camera bounds

On 26 November the maintainer reported a tiny floor in the camera image's bottom-left corner and a zone badge of approximately 112,433 x 112,378 cm, only on B without a station. The renderer previously included any `dockLocation` in the canvas bounds. A synthetic 4 x 3 m room with a dock at (10,000, 10,000) reproduces the same visual failure: a 4096-square canvas with the floor almost invisible. The actual raw dock value on B has not been captured, so a stationless placeholder is a hypothesis rather than a confirmed protocol sentinel.

The renderer now validates dock points using the existing room locator's geometry and dock reach rule. A remote point outside every room is omitted from both bounds and drawing; a dock at zero, within a room or just outside its wall remains valid. Invalid/nonfinite points are skipped, and an invalid live dock can fall back to a valid static dock on that floor. The check is relative to the floor, so large translated map origins remain supported. In the synthetic reproduction, the corrected image is 450 x 370 pixels at 80 pixels/metre.

The full offline suite passes with 440 tests, including six dock/bounds regressions. A live B-camera retest remains required. If B remains oversized, capture its persistent map data to distinguish dock coordinates from a malformed dimensions/room-coordinate field. Navigation and Home commands are unchanged.

## Fresh room discovery and stationless follow-up (26 November)

The maintainer confirmed that deleting B removes the Lovelace switch button when only A remains, and creating a new B restores the button. They also reported that the new room's entities appeared only after a first clean, with A's list visible in the meantime. The supplied `home-assistant_dyson_spot_scrub_2026-11-26T04-19-58.659Z.log` already starts with new B (`1795665600`, room Essbereich). It includes a B preference reply at 05:08:39.715, before the first START at 05:08:54.345, but not creation itself or an HA registry snapshot; the exact entity-discovery delay cannot be reconstructed from this export.

The integration now refreshes current-map preferences and REST inventory after mapping completion, room rename/map-change events, changed catalogues and catalogue polls while current preferences are still missing. Room names from that map's metadata can create entities before settings arrive; no preference rows/settings are invented. Metadata changes notify discovery listeners. With saved-map selection enabled, neither the coordinator nor the vacuum borrows another map's cached names: an unresolved current room list is published explicitly as `rooms: []`, so the card cannot restore A's last list. These changes do not create, edit or start a map/cleaning job. Live creation-before-first-clean acceptance remains required.

Four stationless runs were annotated by the maintainer (start origin is their observation):

| Run | Start origin | Start | Pause before Home | Home | Reported outcome |
| --- | --- | --- | --- | --- | --- |
| 1 | MyDyson | 05:08:54 | No | 05:09:57 | Parked at placement point |
| 2 | Lovelace | 05:10:40 | No | 05:11:18 | Parked at placement point |
| 3 | Lovelace | 05:11:38 | 05:12:17 | 05:12:26 | Searched/localized without a station |
| 4 | MyDyson | 05:17:15 | 05:18:19 | 05:18:29 | Searched/localized without a station |

All four use legacy `ABORT` followed by `service.start_recharge`; MyDyson includes additional state/mode fields in ABORT while HA uses its existing minimal payload. Runs 1/2 report inactive discharging at 05:10:08 / 05:11:26. Runs 3/4 report JDM map ID `0` at 05:12:36.229 / 05:18:36.865 and later restore B. This correlates the differing behavior with a prior pause, not the start origin, but does not prove the robot's internal navigation rule. Home/Pause/Stop commands remain unchanged.

The same export shows `2510` (`USER_CONTINUE`) persisting off-dock while inactive, followed by a successful app start at 05:17:15.898. `2509` appears at 05:19:04.717 while inactive charging. The app meanings supplied by the maintainer are robot lifted / robot not level. Both now have descriptions; idle off-dock placement retries follow the [readiness rule](START_READINESS.md#retained-placement-faults-on-a-stationless-floor), without hiding warnings or permitting competing active jobs. A sanitized fixture retains the four command sequences and selected fault states. All 449 offline integration tests pass; these discovery/readiness corrections still need live acceptance.

## Entity retention and remaining validation

Normal switches retain existing room entities and use the existing active-room refresh path. Removed map IDs disappear from the map selector. Removing the old room entities and registry entries of a deleted map is a separate follow-up: existing room entities use names, and shared names/room IDs require verified map ownership before cleanup.

Home is unchanged: `vacuum.return_to_base` sends legacy `ABORT`, with Dyson's cloud adding `service.start_recharge`. Stop is a distinct legacy `STOP`. The supplied stationless return trace is Home, not a captured MyDyson cancellation or normal completion; returning to the placement point through those app flows needs its own comparison.

Run `python -X utf8 -m unittest discover -s tests -v` with the CI dependencies installed. Tests use mocked MQTT and Home Assistant, including list ordering, deletion, duplicate names, readiness, confirmation, timeouts, lifecycle and the existing entity-identity snapshot. Live installation still needs a two-map switch in both directions from the select and card, a MyDyson rename/deletion, reconnect, and confirmation that existing room cleaning still works.
