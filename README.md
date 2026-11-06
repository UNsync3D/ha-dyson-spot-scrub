# Dyson Spot+Scrub AI — Home Assistant Integration

A custom Home Assistant integration for the **Dyson Spot+Scrub AI** robot vacuum, with support for the **Dyson R2 Nurovi Wash+Dry**. It connects through Dyson's cloud, the same way the MyDyson app does. The robot pushes status updates to Home Assistant over MQTT as they happen, so there's no constant polling. The robot doesn't accept local connections, so an internet connection is required.

### Supported robots

| Robot | Status |
|---|---|
| **Dyson Spot+Scrub AI** | Supported and tested on hardware |
| **Dyson R2 Nurovi Wash+Dry** | Supported, built from real R2 logs; owner testing welcome ([#32](https://github.com/UNsync3D/ha-dyson-spot-scrub/issues/32)) |
| **Dyson R1 Nurovi Dry**, **Dyson R3 Nurovi Spot+Scrub UV** | Not yet supported: owners wanted to help test ([#30](https://github.com/UNsync3D/ha-dyson-spot-scrub/issues/30)) |

---

## Dashboard card: Dyson Cleaning Planner Card

This integration and **[FutuRazor's Dyson Cleaning Planner Card](https://github.com/FutuRazor/dyson-cleaning-planner-card)** are designed to work together. The integration connects Home Assistant to your robot and provides its entities and actions; the card turns them into one dashboard view built for the Spot+Scrub AI:

- **Plan a clean:** pick rooms, choose each room's settings and start, all in one place.
- **Follow the session:** see the cleaning status, the selected rooms and the live map.
- **No YAML needed:** select your Dyson vacuum in the card editor, and its rooms and supported controls are detected automatically.

The card needs **v1.5.0 or newer** of this integration. On Home Assistant 2026.11 or newer, install it from **Settings → Marketplace → Add from link** with `https://github.com/FutuRazor/dyson-cleaning-planner-card`; on older versions, add it in HACS as a custom repository (type **Dashboard**). The [card's README](https://github.com/FutuRazor/dyson-cleaning-planner-card#installation) has the details. You can also [try the interactive preview](https://futurazor.github.io/dyson-cleaning-planner-card/preview/index.html), which uses simulated data.

The card is developed and maintained by **[@FutuRazor](https://github.com/FutuRazor)**, who has also contributed much of this integration: the station controls, consumable sensors, mop drying controls, start readiness checks, map display options and card actions, along with extensive hardware testing, MQTT captures and code reviews. Thank you, FutuRazor! Please report card issues in the [card's repository](https://github.com/FutuRazor/dyson-cleaning-planner-card/issues).

---

## Features

**Robot**

| Entity | Type | Notes |
|---|---|---|
| Robot vacuum | `vacuum` | Start, pause, resume, stop, return to dock, suction power |
| Vacuum Mode | `select` | Vacuum / Vacuum and Mop / Mop / Vacuum then Mop |
| Vacuum Start | `button` | Starts the rooms selected with the room switches |
| Floor Map | `camera` | Live map of rooms, the cleaned path and the robot |
| Battery | `sensor` | Battery percentage |
| Docked, Charging, Fault | `binary_sensor` | Robot on the dock, battery charging, error reported |

**Rooms** (one set per room on the current map)

| Entity | Type | Notes |
|---|---|---|
| Room *name* | `switch` | Chooses whether the next **Start** cleans this room. Nothing is sent to the robot until you start |
| Room *name* Cleaning Mode, Cleaning Strategy, Water Level, Clean Repetitions | `select` | The room's saved settings, as in the MyDyson app |

**Dock and station**

| Entity | Type | Notes |
|---|---|---|
| Empty Bin, Wash Mop, Dry Mop | `button` | Dock actions. Available only while the robot is on the dock between cleans and the dock is idle |
| Hot water mopping, Hot water self-cleaning, Extended roller preparation, Cleaning solution | `switch` | Station settings (no cleaning solution on the R2) |
| Self-cleaning frequency, Roller drying duration | `select` | Station settings (drying: 3–5 hours on the Spot+Scrub AI, 2–4 hours on the R2) |
| Mop drying time remaining | `sensor` | Countdown while the mop is drying |
| Consumable usage, Cleaning solution | `sensor` | See [Consumable monitoring](#consumable-monitoring) |

### New cleaning start readiness

New cleaning jobs are rejected while the robot is cleaning or paused, returning/finishing, mapping, washing its roller or emptying its bin. In particular, `FULL_CLEAN_FINISHED` does not mean ready: the robot may still be returning or preparing station work. Confirmed inactive states with an idle dock allow starting without requiring a preceding station cycle. Confirmed post-clean drying remains interruptible by the existing start sequence.

The vacuum exposes `ready_to_start` (boolean) and `start_block_reason` (reason code, or `null` when ready). Dashboard cards can use these attributes to disable Start and explain why. Enforcement also applies to the integration's start button, room actions and MQTT start path, independently of dashboard support. `vacuum.start` still resumes a paused clean; a different/new room job is blocked while paused. A fan-speed command that would restart an active job is now rejected; selecting preferences without starting is unchanged.

See [start readiness and testing](START_READINESS.md) for the state contract, evidence and limitations.

### Floor Map

The Floor Map camera entity draws a live PNG in the style of the MyDyson app:

- **Rooms:** walls, doors and room dividers from your Dyson map, with the floor the robot can reach shaded dark and the rest of each room grey.
- **Cleaned path:** smooth lines, purple for vacuuming and blue with a soft glow for mopping. Travel between areas isn't drawn.
- **Robot:** seen from above, with a red front bumper and a green beam showing which way it's heading.
- **Dock and room names:** names are centred in each room by default.

The map refreshes about every 5 seconds while the robot is cleaning, and every 60 seconds otherwise. After a clean, the last map is kept for 5 minutes by default.

**Map options** (the integration's **Configure** dialog):

- **Retention:** how long the finished map stays (0–60 minutes), or keep the last map until the next clean.
- **Transparent background:** for laying the map over your own floor plan.
- **Appearance:** path colours for vacuum, mop and vacuum-and-mop, path and glow width, and per-room colours.
- **Visible elements:** furniture, room names (and **Center room names**, or Dyson's positions), room areas, legend, cleaning status, walls, floor outlines, grid, dock, keep-out zones, and **Show floor shading**.

### Cleaning modes

Selected via the **Vacuum Mode** entity, or from any automation:

| Mode | What the robot does |
|---|---|
| Vacuum | Suction only |
| Vacuum and Mop | Both simultaneously |
| Mop | Wet mopping only |
| Vacuum then Mop | Full vacuum pass, then full mop pass |

### Batched room settings for Lovelace cards

Per-room select entities continue to send each changed option to the robot immediately. A custom card can keep edits locally and call `dyson_spot_scrub.save_room_settings` once when the user saves. The payload uses stable room IDs and the same option labels shown by the select entities:

```yaml
action: dyson_spot_scrub.save_room_settings
data:
  entity_id: vacuum.dyson_spot_scrub
  room_settings:
    "12":
      cleaning_mode: Vacuum then Mop
      cleaning_strategy: Auto
      water_level: Medium
      mop_repetitions: Two Passes
```

`mop_repetitions` sets the room's **Clean Repetitions** select (one or two passes). Despite its field name, the second pass also applies to vacuum-only cleans.

To start with a local draft, call `dyson_spot_scrub.start_rooms_with_settings`. It applies the supplied room settings and sends the selected room IDs to the robot in the supplied order. The current Dyson protocol calls the field `unorderedZones`, so the robot may choose its own execution order until ordered cleaning is supported by the integration. Existing entities and actions keep their current behavior.

```yaml
action: dyson_spot_scrub.start_rooms_with_settings
data:
  entity_id: vacuum.dyson_spot_scrub
  room_ids: ["12", "10"]
  room_settings:
    "12":
      cleaning_mode: Vacuum then Mop
```

---

## Requirements

- Home Assistant **2025.1** or newer
- The robot already registered in the **Dyson app**
- Your **Dyson account email and password**, with access to the email inbox for a one-time code — or, alternatively, a Dyson **bearer token** (see [Getting a bearer token](#getting-a-bearer-token))
- Your Dyson account **country code** (e.g. `AU`, `GB`, `US`, `DE`); the token method also needs the robot's **serial number**

---

## Installation

### Option A — Marketplace (Home Assistant 2026.11 or newer, recommended)

1. Go to **Settings → Marketplace**.
2. Select **Add from link** and enter `https://github.com/UNsync3D/ha-dyson-spot-scrub`. The first time, you're asked to connect GitHub.
3. Open **Dyson Spot+Scrub AI** and select **Install**.
4. Continue with [Setup](#setup).

### Option B — HACS (Home Assistant 2026.10 or older)

1. Open **HACS**, select **⋮ → Custom repositories**.
2. Add `https://github.com/UNsync3D/ha-dyson-spot-scrub` with the type **Integration**.
3. Search for *Dyson Spot+Scrub AI* and select **Download**.
4. Restart Home Assistant.

### Option C — Manual

Copy the `custom_components/dyson_spot_scrub/` folder into your HA config directory:

```
<config>/custom_components/dyson_spot_scrub/
```

Restart Home Assistant.

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Dyson Spot+Scrub AI**
3. Choose how to sign in:
   - **Log in with Dyson account (email + OTP)** — see [Email login](#email-login) below
   - **Enter a bearer token directly** — see [Getting a bearer token](#getting-a-bearer-token)
4. The integration finds the robot(s) on your account and creates their entities

> **Country code must match your Dyson account region.** Common codes: `AU`, `GB`, `US`, `DE`, `FR`, `JP`, `NL`.

### Email login

The integration signs in the same way the MyDyson iOS app does:

1. Enter your Dyson account **email**, **password** and **country code**.
2. The integration checks that your account is active and uses email codes, then Dyson emails you a **6-digit code**. If the account isn't reported as active, it stops here and no code is requested.
3. Enter the code. Dyson checks your **password and the code together** at this step, then the integration fetches your robots and sets them up.

Your password is only used for this sign-in and is not stored; Home Assistant keeps only the resulting token, as with the token method.

**If the code is rejected:**

- **Check your password first.** Dyson only checks the password at the last step, after the code has been emailed, and it gives the same rejection for a wrong password, a wrong code and a locked account.
- The code screen then shows a **password** field. Correct the password there and resubmit the **same code** — no new email is needed. Leave the field blank to keep the password you entered before.
- **After two rejections in a row, stop.** The integration pauses sign-in attempts for that email for 15 minutes, because repeated attempts can get a Dyson account temporarily locked. If codes keep being rejected with the right password, wait several hours or contact Dyson support rather than retrying.

Logging in here doesn't sign you out of the MyDyson app: each login gets its own token. Questions and reports: [issue #25](https://github.com/UNsync3D/ha-dyson-spot-scrub/issues/25).

---

## Getting a bearer token

Only needed if you choose the token method instead of [Email login](#email-login). The easiest way to get your Dyson bearer token is to intercept the Dyson app's network traffic using **Proxyman** on a Mac with your iPhone on the same Wi-Fi network. This takes about 5 minutes.

### Step 1 — Install Proxyman on your Mac

Download and install [Proxyman](https://proxyman.io) (free tier is sufficient). Launch it.

### Step 2 — Install the Proxyman certificate on your iPhone

1. In Proxyman, go to **Certificate → Install Certificate on iOS → Physical Device**
2. Follow the on-screen instructions — this involves visiting a URL on your iPhone and installing a profile
3. On your iPhone, go to **Settings → General → VPN & Device Management** and trust the Proxyman certificate
4. Then go to **Settings → General → About → Certificate Trust Settings** and toggle the Proxyman root CA to fully trusted

### Step 3 — Configure your iPhone's proxy

1. On your iPhone, go to **Settings → Wi-Fi** and tap the ℹ️ next to your current network
2. Scroll down to **HTTP Proxy** → **Configure Proxy → Manual**
3. Set **Server** to your Mac's IP address (shown in Proxyman's title bar) and **Port** to `9090`

### Step 4 — Capture the token

1. Open the **Dyson app** on your iPhone and log in (or pull down to refresh if already logged in)
2. In Proxyman on your Mac, look for requests to `appapi.cp.dyson.com`
3. Click on any of those requests and open the **Request** tab
4. Find the **Authorization** header — its value will look like:

   ```
   Bearer XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX-1
   ```

5. Copy everything **after** the word `Bearer ` (including the long hex string and the `-1` suffix)

### Step 5 — Paste into Home Assistant

Use this token in the **Enter a bearer token directly** step of the integration setup. Tokens are long-lived — you should only need to do this once.

> **After setup:** Remove the proxy from your iPhone's Wi-Fi settings so normal traffic isn't routed through your Mac.

---

## How it works

1. **Authenticate**: email login or a pasted token gives a bearer token, which is stored in HA's config entry — your Dyson password is never stored.
2. **IoT credentials**: on each HA start the integration fetches short-lived MQTT credentials from Dyson's cloud for your specific device.
3. **MQTT**: the integration then connects to Dyson's cloud MQTT broker over a WebSocket, as the MyDyson app does. Commands go out and status updates arrive over this connection as they happen. The floor map comes from Dyson's cloud separately: about every 5 seconds while cleaning, and every 60 seconds otherwise.
4. **Auto-recovery**: if the MQTT connection drops, the integration automatically fetches fresh credentials and reconnects with exponential backoff (15 s → 30 s → 60 s → 120 s).

---

## Troubleshooting

### Integration not found after install
Make sure the folder is named exactly `dyson_spot_scrub` inside `custom_components/`, then restart HA.

### Bearer token is rejected
- Make sure you copied the full token value — it should be 65+ characters ending in `-1` or similar
- If a token stops working, sign in again with [Email login](#email-login), or capture a fresh token with Proxyman

### Floor map is blank or shows an error
- The map is only populated after the robot has completed at least one cleaning session with room mapping enabled in the Dyson app
- Check the HA logs for `dyson_spot_scrub` errors — the map entity logs rendering failures at `WARNING` level

### Robot shows as Unavailable
Either the connection to Dyson's cloud dropped, or Dyson reports the robot itself as offline (for example, it's off Wi-Fi or switched off). The integration recovers from both on its own: it reconnects automatically, and while the robot is offline it asks for its status once a minute. If the robot shows as online in the MyDyson app but stays unavailable here, reload the integration from **Settings → Devices & Services**.

### Email login code is rejected
See [Email login](#email-login): check the password first, correct it on the code screen and resubmit the same code, and stop after two rejections.

### "Cannot connect" during setup
Dyson's cloud API may be temporarily unavailable. Wait a few minutes and try again.

---

## Development

```bash
# Clone
git clone https://github.com/UNsync3D/ha-dyson-spot-scrub
cd ha-dyson-spot-scrub

# Symlink into your local HA dev instance
ln -s $PWD/custom_components/dyson_spot_scrub \
      ~/.homeassistant/custom_components/dyson_spot_scrub
```

Pull requests welcome. Please open an issue before starting significant work so we can coordinate.

---

## Consumable monitoring

Five percentage sensors report the used lifespan of each consumable (the MyDyson app shows the remaining life instead, so 2% used here is 98% in the app):

- **Spot+Scrub AI:** brush bar, mop roller, side brushes, robot filter and dock filter, plus a **Cleaning solution** sensor (OK or Refill required).
- **R2 Nurovi Wash+Dry:** brush bar, **mop pad**, side brushes, robot filter and dock filter. The R2 has no cleaning solution. See [Consumable sensors](CONSUMABLES.md) for entity identification, card integration and testing.


### Mop roller drying

While `dock_state` is `DRYING_MOP`, the vacuum exposes `drying_total_seconds`,
`drying_remaining_seconds`, and `drying_updated_at` (UTC reception time of the
last valid timing packet). These are robot-reported values, not a locally
estimated countdown. Timing packets arrive roughly once per minute; missing or
invalid packets leave the previous sample unchanged until the next valid one.
The timestamp is not refreshed by unrelated status messages.

Timing requires a confirmed drying state and a valid `work_time` type 3 packet.
It may therefore take about a minute to appear after startup. Times are removed
when the dock leaves drying or MQTT disconnects. No old cycle values are restored.
A zero remaining time alone does not change the dock state. Frontends may derive
progress as `(total - remaining) / total` and interpolate between samples using
the timestamp, but the live dock state determines whether drying is active.

To cancel drying independently of starting a clean:

```yaml
action: dyson_spot_scrub.cancel_drying
data:
  entity_id: vacuum.your_dyson
```

The action resolves exactly one Dyson vacuum, requires a live connection and
active drying, and reuses the existing `ABORT-DOCK-ACTION` / `DRY_MOP` command.
It sends no cleaning, stop or home command and does not modify room settings.
Successful submission is not confirmation of completion: wait for `dock_state`
to change. The existing pre-clean drying cancellation remains unchanged.

Card integration contract: detect `hass.services.dyson_spot_scrub.cancel_drying`,
then call it with the selected vacuum's `entity_id`. The planner card's Cancel
drying button uses this action.

---

## Licence

MIT
