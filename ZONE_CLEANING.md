# Experimental zone cleaning

This test branch adds `dyson_spot_scrub.clean_zone` and camera calibration for
one draggable rectangle in the matching Lovelace card test branch.

All four cleaning modes, four power levels and four water levels follow
Spot+Scrub AI app captures from 8–11 November 2026. Other models still need
hardware testing. Two mop passes are captured for Mop and Vacuum and Mop.
Two passes for Vacuum then Mop and vacuum-only zone cleaning are experimental;
a valid outgoing payload does not prove that the robot executes both passes.
Existing room actions and saved room preferences are unchanged.

## Action contract

All three fields are required:

| Field | Format | Meaning |
| --- | --- | --- |
| `entity_id` | One vacuum entity ID | Exactly one robot belonging to this integration |
| `map_id` | Positive numeric string | Must match the robot's current `persistentMapId` |
| `rectangle` | `[x_min, y_min, x_max, y_max]` | Finite numeric robot coordinates in metres; width and height must be positive |

Optional settings (omitting them preserves the original vacuum-only defaults):

| Field | Accepted values | Default |
| --- | --- | --- |
| `clean_type` | `vacuum`, `vacuumAndMop`, `mop`, `vacuumThenMop` | `vacuum` |
| `cleaning_strategy` | `auto`, `boost`, `quiet`, `quick` | `auto` |
| `water_level` | `veryLow`, `low`, `medium`, `high` | `low` |
| `mop_passes` | Integer 1 or 2 | 1 |
| `dry_passes` | Integer 1 or 2 | 1 |

The counts are independent. The card's repetitions control changes `dry_passes`
for vacuum-only mode, otherwise `mop_passes`, keeping the other count at 1.
In particular, the experimental two-pass Vacuum then Mop request means one
vacuum pass followed by two requested mop passes.


The vacuum exposes its current map as the `map_id` attribute. A map change
rejects an old selection; this action does not switch maps or relocate the robot.
Normal start-readiness checks apply, including offline, returning, washing and
emptying. Drying is cancelled before starting, using the existing command.
Concurrent start preparation is rejected using the existing start lock.

The integration sends **one** legacy `START` with
`cleaningMode: spotZoneConfigured`. Its `cleaningProgramme` contains the map ID,
one `spotZones` entry, and `defaultSpotZoneSettings` containing the chosen values.
`isUvScanOn: true` remains the captured constant; no UV control is inferred.
The cloud generated `service.set_areas_start` in the capture, so the integration
does not send a second JDM start or write room preferences.

Action completion means the local MQTT client accepted the command. It does not
prove the robot accepted the zone or started. There is no automatic retry and no
optimistic change to the vacuum state.

## Camera calibration for the card

The floor-map camera exposes `map_calibration` after successfully rendering a
georeferenced PNG whose map ID matches the robot's active map. No calibration is
exposed for an error image, a map without an ID, or a mismatched map.

| Attribute | Meaning |
| --- | --- |
| `version` | `1` |
| `map_id` | Map ID belonging to the rendered image |
| `units` | `m` |
| `image_width`, `image_height` | Original PNG dimensions in pixels |
| `pixels_per_metre` | Actual render scale, including the large-image size cap |
| `padding` | Pixel margin inside the PNG |
| `min_x`, `max_y` | Bounds used by that render, including the map margin |
| `image_sha256` | SHA-256 of the exact original PNG bytes |

For an unrotated original image pixel `(px, py)`:

```text
x = min_x + (px - padding) / pixels_per_metre
y = max_y - (py - padding) / pixels_per_metre
```

Before converting pointer coordinates, the card must undo its own rotation,
scaling, cropping and letterboxing. Coordinates refer to the image, not the
camera element's surrounding container. Convert the rectangle corners and take
the minimum/maximum coordinates for the action. Do not use the whole-apartment
test rectangle as the image extent: padding and live-path bounds can differ.

Camera state and image requests are separate. The card must match the downloaded
original PNG's SHA-256 to the calibration before allowing selection/start, and
freeze that image/calibration pair while editing. Fetch again if they do not
match. Do not use a resized or transcoded proxy image for this comparison.
Calibration follows retained images and rendering failures; a map change or
image reset clears it. This is geometry, not obstacle detection: it does not
certify a selected region is reachable or belongs to a room.

## First hardware test

1. Install this branch and restart Home Assistant. Open the floor-map camera so
   it renders a frame. Check `map_calibration` on the camera and `map_id` on the
   vacuum in Developer tools → States.
2. Select a small known area from that calibrated image. The matching card
   test branch performs the conversion.
3. In Developer tools → Actions, run `dyson_spot_scrub.clean_zone`, targeting
   your vacuum and supplying that map ID and rectangle. Do not copy arbitrary
   example coordinates from another home.
4. Confirm one start announcement, the requested mode and passes in the selected area,
   and unchanged room settings. Capture debug messages around the action and
   subsequent robot telemetry for comparison with the app.
5. Check that the existing room clean, return-to-dock and drying controls still
   work. The normal readiness guards must also reject a new zone while busy.

Offline regression command: `python -m unittest discover -s tests -v`.
Test data uses invented identifiers; no raw personal capture is included.

## Capture evidence

The 11 November capture contains eight Vacuum and Mop START requests, each
followed by its cloud-generated JDM command. `cleanType: vacuumAndMop` maps to
mode 1; power auto/quick/quiet/boost maps to wind 0/3/2/1; water
veryLow/low/medium/high maps to 99/0/1/2. Mop passes 1/2 map to clean_count 0/1.
A second capture confirms mop (mode 2, two passes) and vacuumThenMop (mode 3,
one pass). Errors deliberately prevented execution: these captures verify the
command encoding, not completed cleaning runs. No personal identifiers or raw
capture files are included here.
