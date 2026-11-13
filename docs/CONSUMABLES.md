# Consumable sensors

This branch adds read-only sensors from the existing MQTT `consumables` list. It is based on `codex/support-fault-code-field`. No robot commands, reset actions or extra polling are introduced. The ioniser cartridge is intentionally excluded.

| MQTT type / `consumable_type` | Entity name | Unique ID suffix | Native value |
|---|---|---|---|
| `brushBar` | Brush bar usage | `_consumable_brush_bar_usage` | Used lifespan, 0–100 % |
| `mopRoller` | Mop roller usage | `_consumable_mop_roller_usage` | Used lifespan, 0–100 % |
| `sideBrushes` | Side brushes usage | `_consumable_side_brushes_usage` | Used lifespan, 0–100 % |
| `robotFilter` | Robot filter usage | `_consumable_robot_filter_usage` | Used lifespan, 0–100 % |
| `dockFilter` | Dock filter usage | `_consumable_dock_filter_usage` | Used lifespan, 0–100 % |
| `cleaningSolution` | Cleaning solution | `_consumable_cleaning_solution` | `ok` or `refill_required` |

Unique IDs start with the robot serial number. All six sensors belong to the same device as the vacuum and are enabled by default. Home Assistant may generate or retain different entity IDs if users rename entities; do not derive IDs from display names.

## Card integration contract

Find sensor entities from the `dyson_spot_scrub` integration on the selected vacuum's device. Use their stable `consumable_type` attribute to identify the material. `value_kind` is `usage_percent` for the five numeric sensors and `refill_status` for cleaning solution.

Numeric values represent **consumption**, not remaining life. Calculate the remaining-life bar as `100 - sensor state` only when the state is a valid number from 0 to 100. For example, usage 4 means 96 % remaining. Do not treat `unknown` or `unavailable` as zero consumption.

Cleaning solution is an enum sensor. Its native states are `ok` and `refill_required`, displayed in English as **OK** and **Refill required**. `needsRefill: false` does not mean the bottle is full; do not draw a percentage bar for it.

Missing records, duplicate types, negative values (including -1), invalid data and out-of-range percentages produce an unknown value while connected. Disconnection makes sensors unavailable. The integration retains the last received MQTT snapshot when unrelated messages arrive; values update when the robot next reports consumables.

## Installation and testing

Install this branch's integration files, then restart Home Assistant to load the changed sensor platform. Check the six new entities on the Dyson device. For the supplied sample, usage values should be 4, 6, 9, 9 and 0 %, and cleaning solution should show OK. Confirm updates and disconnected behavior using actual robot telemetry.

Offline checks: `python -m unittest discover -s tests -v`. New tests exercise entity registration, unique IDs, device association, sample values, invalid input, refill status and listener registration using minimal Home Assistant stubs. A full Home Assistant runtime and physical robot test are still required.

Validation in this development environment: all five new sensor tests pass. The full suite runs 51 tests with 21 failures and 2 errors; the unchanged base runs 46 tests with the same 21 failures and 2 errors. Existing room-preference assertions and map-test environment issues therefore remain outside this change.
