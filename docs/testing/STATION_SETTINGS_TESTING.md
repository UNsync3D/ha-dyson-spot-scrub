# Station settings

Based on main `c6b54fab1ef1c29bf6d908bd3cdfd1a7710523a6`. Adds four switches
and two selects to the existing device, independent of room settings:

| Control | JDM property | Values |
| --- | --- | --- |
| Hot water mopping | hot_water_mop | 0 / 1 |
| Cleaning solution | detergent | 0 / 1 |
| Extended roller preparation | store_demo_mode | 0 / 1 |
| Hot water self-cleaning | hot_water_switch | 0 / 1 |
| Self-cleaning frequency | back_wash_type, back_wash_time | ROOM=1; TIME=0 with 15, 30 or 60 |
| Roller drying duration | airdry_frequency | 3, 4, 5 hours |

Mappings were captured on 2026-10-23 from the MyDyson app with successful
property acknowledgements and matching state reports. Despite its name,
`store_demo_mode` maps to `washMopBeforeClean`. The app's "Only as needed"
selection sends TIME/60; its physical behavior is not established by the capture.
After each room takes precedence over any retained time value.

Explicit HA actions send the observed STATE-SET and prop.set pair once, then
request current state. Whether both commands are necessary remains unproven.
Transport acceptance is not device confirmation: controls display reported
values only. Rejection or missing telemetry does not optimistically change them.
There are no automatic write retries. Unknown values and disconnected devices
cannot trigger writes. Reconnect clears station telemetry until fresh reports
arrive. Incoming updates never generate writes. Identical confirmed values are
not resent. No commands start a cleaning or station cycle.

## Offline tests

`python -m unittest discover -s tests -v`

The station tests cover captured mappings, command pairs, invalid values,
missing state, disconnects, transport failures, telemetry updates without writes,
and entity lifecycle. Home Assistant and transport are mocked.

## Live verification (still required)

1. Replace integration files and restart HA once. Six new entities should appear.
2. Change each setting in the Dyson app; check that HA follows without writes.
3. Change each in HA; check the reported HA state and the Dyson app agree.
4. Test all four frequency choices and all three drying durations. Switching
   from Only as needed to After each room must display the latter.
5. Disconnect/reconnect and confirm no unsolicited setting commands occur.
6. Enable debug logging before tests so command acknowledgement and subsequent
   state can be checked. Verify the physical effect on a later cleaning cycle.

No storage migration or removal of existing settings is needed.
