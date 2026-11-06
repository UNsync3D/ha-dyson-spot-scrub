# Map rendering options

Built on main commit `595f661332a7ed4205ae67c0f8302a0cfaa2ecfd` (v1.4.5).

The integration's Configure dialog contains retention (0–60 minutes, default 5),
Keep last cleaning map, Transparent background, and collapsed Appearance and
Visible elements sections. Settings apply on the next camera refresh, including
to a retained image, without reloading the integration or fetching another map.

Path width is 1–15 source-image pixels. Glow width is its total width, 0–25
pixels; 0 disables it. Only mop and vacuum-and-mop paths glow, like the MyDyson
app. A glow no wider than the path is covered by the path.
Vacuum, mop, and simultaneous cleaning have separate RGB colors. Other existing
rendering defaults are preserved. The optional grid, legend, and cleaning status
fills are disabled by default. Hiding elements does not change image bounds.

Retention remains in RAM. Keep last cleaning map overrides the timer until a new
cleaning, map change, or integration restart. Turning it off evaluates the timer
from the original cleaning finish time. Pauses and vacuum-to-mop transitions do
not start the timer. The presentation storage format remains version 1.

## Path color limitation

Colors use observed MQTT cleaning actions for newly received path edges. Known
prefix colors are preserved during sequential cleaning. Unknown or rewritten
history is gray. Dyson does not provide a verified cleaning action for every
point: delayed batches spanning an unseen action change, or attaching midway
through cleaning, cannot be reconstructed precisely. This does not distinguish
every travel/turning segment from active cleaning.

## Automated checks

Run `python -m unittest discover -s tests -v` with Pillow, aiohttp, and voluptuous
installed. Tests cover retention, option validation and form submission, rendering
pixels and stable bounds, mode history, and existing integration behavior.
Options-flow tests use Home Assistant adapters; they do not replace a live
Home Assistant frontend test.

## Live verification

1. Install the integration files and restart Home Assistant once. Open Configure.
2. Save with switches off and glow 0; reopen and check persisted values.
3. During cleaning, change colors and widths. Check the next camera refresh.
4. Pause/resume, then finish or cancel. Check retention at 0 and a short interval.
5. Enable Keep last cleaning map; start a new cleaning and verify old paths reset.
6. Change appearance while a map is retained, including transparency and layers.
7. Test vacuum then mop and simultaneous vacuum/mop. Check historical path colors.
8. Verify overlay alignment is unchanged when individual layers are hidden.

Robot behavior and the real Home Assistant dialog still require these live tests.
