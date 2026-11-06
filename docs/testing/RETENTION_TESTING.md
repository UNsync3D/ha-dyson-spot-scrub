# Cleaning map retention (step 1)

Base: upstream commit `1f55e8e5f6a3014c3ef6ce208290228b8881dcfd`.

## Scope

- Hold the last rendered cleaning image during pauses, vacuum-to-mop transitions,
  and temporary disconnections.
- Keep it for 300 seconds after observing `FULL_CLEAN_FINISHED` or `ABORTED`.
  The timer starts on the coordinator notification, even without an open viewer.
- Clear the previous image when a new cleaning starts, including discovery, or
  when the map identity changes. Discard late responses from the previous session.
- Use the existing coordinator fetch, deduplication and HTTP 429 backoff. Cache
  invalidation does not clear a backoff deadline. Poll intervals are unchanged.
- Keep the cleaning image in RAM only. Reloading the integration or restarting
  Home Assistant clears it. Existing room-boundary storage remains separate.

Only `camera.py` and `coordinator.py` change in the integration. There are no new
options, colours or renderer changes. `_retention_seconds` is the single policy
access point for future configuration. Existing prototype options are ignored.

No extra final cloud fetch is made. The retained image is the most recent frame
already rendered, which may predate completion if the API was unavailable.
Expiry is applied on the next camera request (normally the next 5-second refresh
while retention is active), not by an independent background timer.
The camera does not collect frames without a viewer. If no cleaning frame was
rendered, there is no cleaning image to retain.

## Local tests

Run from the repository root:

```shell
python -m unittest discover -s tests -v
```

Requires Pillow, already a runtime dependency of the integration. The tests
import the actual camera, coordinator and MQTT classifiers. Home Assistant's
lifecycle/storage and network APIs are mocked; one test uses the real PNG
renderer. This is not a Home Assistant or physical-robot integration test.

## Installation for testing

Keep a Home Assistant backup and a copy of your existing integration first.
Use the complete upstream integration at the base commit and overlay BOTH changed
Python files, then restart Home Assistant. Alternatively use the complete test
integration archive. Keep your configured integration entry and entity registry.
Do not mix the step-1 camera with the old prototype coordinator or renderer.

### Switching from the private v0.7 prototype

The v0.7 prototype used **version 2** of the room-presentation storage file;
upstream uses **version 1**. If that prototype has written its cache, upstream
cannot load it. This concerns room outlines, not the RAM-only cleaning image.

With Home Assistant stopped, preserve the matching file
`/config/.storage/dyson_spot_scrub.presentations.<robot serial>` in a backup
location outside `.storage`, removing only that file from the active storage
location before starting this version. The room outlines will be learned again
during cleaning. Do not remove other `.storage` files or the integration entry.
Keep the original file for rollback to the prototype. A normal upstream v1 cache
does not need this step. No private cache migration is included in the PR.

## Physical-robot checks (pending)

1. Complete a vacuum run: note the completion announcement time. Confirm that the
   last cleaning frame remains during the return to dock and clears around five
   minutes later.
2. Pause for longer than five minutes, then resume: keep the map throughout the
   pause and resume drawing without starting a new session.
3. Pause and send home: start the five-minute timer on the reported abort, not
   when the robot eventually docks.
4. Vacuum then mop: preserve the vacuum path and extend the cumulative Dyson path
   during mopping. Do not start retention at the intermediate transition. Test
   the actual MQTT state sequence; the tests use the previously observed states.
5. Start another cleaning during retention: discard the previous cleaning image
   during discovery. Show only the new session's returned live data.
6. View the camera on two devices and, if a 429 occurs, check that the last frame
   remains and that the existing backoff is respected.
7. Restart/reload during retention: the previous cleaning image should disappear.

Enable integration debug logging before starting. Record approximate times for
pause, resume, completion, abort, transition and any unexpected image change.

PR 1 should be submitted after these checks; appearance and options UI will be
separate later contributions.
