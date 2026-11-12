# New-job start readiness

This branch adds a shared gate, not a replacement robot state machine. It does
not change command payloads or Home Assistant vacuum activity mapping.

## Frontend contract

Vacuum attributes:

- `ready_to_start`: boolean permission to request a **new** cleaning job.
- `start_block_reason`: `null` when ready, otherwise one of the codes below.

| Code | Meaning |
|---|---|
| `disconnected` | No MQTT connection |
| `robot_fault` | Existing fault classifier reports an actionable fault |
| `washing_mop` | Roller washing |
| `emptying_bin` | Dust collection |
| `mapping_or_navigation` | Mapping-family state; do not start a competing job |
| `returning_or_finishing` | Return/abort or post-clean completion still in progress |
| `cleaning_job_active` | Existing clean, pause or mid-clean recharge |
| `waiting_for_ready_state` | Missing, unknown or insufficient readiness telemetry |

The card should gate **new Start**, not Resume, on this attribute. A future card
update can show a friendly message for each code. This branch changes only the
integration: an older card may still show an enabled Start button, but its
request will be rejected. Readiness is advisory until checked at execution.

## Evidence and rules

Sanitized October 21 vacuum sequence:

1. 20:15:58: `FULL_CLEAN_FINISHED`, `workMode=38`, `jdmStatus=3`, dock `IDLE`.
2. 20:16:28: same finished state, status 4 / charging begins; dock still `IDLE`.
3. 20:16:34: `COLLECTING_DUST`.
4. 20:16:44: `INACTIVE_CHARGING`, work mode 0, still `COLLECTING_DUST`.
5. 20:16:49: `INACTIVE_CHARGING` and dock `IDLE`: ready.

The October 18 mop capture likewise retains `FULL_CLEAN_FINISHED` / work mode
38 through dock arrival and `WASHING_MOP`. At 19:51:38 it changes to work mode
0, `INACTIVE_CHARGING`, `DRYING_MOP`, which permits the existing drying-abort
and new-start sequence. No guessed delay or completed-station-cycle latch is used.

`INACTIVE_CHARGING`, `INACTIVE_CHARGED`, `INACTIVE_DISCHARGING` and legacy
`CHARGING` allow a new job only with dock `IDLE`, `DOCKED` or `DRYING_MOP` and
without a higher-priority block. `FULL_CLEAN_FINISHED` or work mode 38 blocks
even after dock contact. Missing/unknown telemetry stays blocked. Reconnection
invalidates readiness fields and waits for new telemetry, retaining map data.

Mapping-family strings are conservatively blocked, but no mapping run was
captured on the test robot. This branch does not claim validated mapping-state
transitions. No numeric mapping modes were inferred. The generic inactive-away
state remains eligible with an idle dock; the robot need not always be docked.

## Enforcement and preserved behavior

- HA preflight checks cover vacuum new Start, start button, room services and
  batch start. Rejection uses a readable HA error where the action is synchronous.
- The common MQTT start path checks before reading preferences and again after
  the response, before preference writes, drying abort or START. This catches
  readiness changes while waiting. It does not claim atomicity with commands
  sent independently by the Dyson app.
- Sequential room cleaning waits for completion **and** readiness, avoiding an
  early next-room start while dock work is active.
- Pause, resume, Home, stop, drying cancellation and save-only settings retain
  their existing command paths. No robot states are synthesized.
- The existing `vacuum.set_fan_speed` restart-during-clean path is blocked as a
  new job; normal preference selection remains available.

## Validation

Run `python -m unittest discover -s tests -v` with Pillow installed, plus
`python -m compileall -q custom_components/dyson_spot_scrub tests`.

The changed suite has 65 tests: 64 pass. One pre-existing map-retention test
errors because its mocked API exception lacks `retry_after`. The unchanged
main baseline has the same error, plus 21 failing room-preference subtests.
The room transport stub now returns `True` on successful publication, matching
the real transport contract, and supplies an explicit ready state.

Regression checks include both captured completion sequences, attribute/service
agreement, blocked MQTT entry points, state change during preference lookup,
drying start, paused resume, sequential dock-work waiting and reconnect invalidation.

Before merging, test in real HA: vacuum finish and return, gap after docking,
dust collection, roller washing, drying start and paused resume. Confirm
blocked requests send no new cleaning command. No new mapping run is required
for this focused return-to-dock fix. Live acceptance is still pending.
