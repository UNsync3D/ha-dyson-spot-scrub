# Drying controls — fork build v1.4.5

## Automated validation

Run `python -m unittest discover -s tests -p test_drying.py -v`.
Seven offline tests pass using minimal Home Assistant and MQTT stubs. They cover
packet parsing, missing/invalid samples, measurement timestamps, zero remaining
time, cycle transitions, disconnect invalidation, entity attributes, target
validation, publish failure, and cancellation without optimistic completion.

The full suite was also compared against the unmodified main used as the base.
Both report the same 21 failed assertions/subtests and 2 errors in existing room
preference and map retention tests in this environment. No additional failures
were introduced (51 tests on main, 58 including the new tests). These are offline
checks, not a live Home Assistant or robot certification.

## Live test

1. Install this branch and restart Home Assistant. Confirm the integration version
   is 1.4.5 and the `dyson_spot_scrub.cancel_drying` action is available.
2. While the roller is drying, inspect the vacuum attributes. After the next
   valid timing packet, check `drying_total_seconds`, `drying_remaining_seconds`,
   and `drying_updated_at`. Values are seconds; the timestamp is UTC.
3. Verify remaining time updates roughly once per minute. Other vacuum updates
   must not change `drying_updated_at`. A missed packet must not reset the values.
4. Call `dyson_spot_scrub.cancel_drying` with that vacuum's `entity_id` in action
   data. Confirm drying stops without starting a clean or changing room settings.
   Attributes should disappear when the robot reports leaving DRYING_MOP.
5. Repeat the action while idle: expect a clear error and no robot command.
6. Start a normal cleaning run during drying: the existing automatic drying
   cancellation should still work as before.
7. On a later drying cycle, confirm no previous remaining value reappears before
   the first valid time packet. Disconnect/reconnect and repeat this check.

The planner card is unchanged. Its existing disabled cancel icon can later call
this action with the configured vacuum entity; detect the service before enabling
it. Countdown/progress rendering belongs to that later card change.
