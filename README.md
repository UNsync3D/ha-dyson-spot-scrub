# Dyson Spot+Scrub AI — Home Assistant Integration

A custom Home Assistant integration for the **Dyson Spot+Scrub AI** robot vacuum. Controls the robot locally over MQTT — no cloud polling during normal operation, so commands are fast and reliable.

---

## Features

| Entity | Type | Notes |
|---|---|---|
| Robot Vacuum | `vacuum` | Start, Stop, Return to Dock |
| Vacuum Mode | `select` | Vacuum / Vacuum and Mop / Mop / Vacuum then Mop |
| Floor Map | `camera` | Live map showing room layout and robot track |
| Battery | `sensor` | Live battery percentage |
| Docked | `binary_sensor` | On when robot is sitting in its dock |
| Charging | `binary_sensor` | On while battery is actively charging |
| Fault | `binary_sensor` | On when robot has reported an error |
| Zone switches | `switch` | One per mapped room — enable or disable individual zones |

### New cleaning start readiness

New cleaning jobs are rejected while the robot is cleaning or paused, returning/finishing, mapping, washing its roller or emptying its bin. In particular, `FULL_CLEAN_FINISHED` does not mean ready: the robot may still be returning or preparing station work. Confirmed inactive states with an idle dock allow starting without requiring a preceding station cycle. Confirmed post-clean drying remains interruptible by the existing start sequence.

The vacuum exposes `ready_to_start` (boolean) and `start_block_reason` (reason code, or `null` when ready). Dashboard cards can use these attributes to disable Start and explain why. Enforcement also applies to the integration's start button, room actions and MQTT start path, independently of dashboard support. `vacuum.start` still resumes a paused clean; a different/new room job is blocked while paused. A fan-speed command that would restart an active job is now rejected; selecting preferences without starting is unchanged.

See [start readiness and testing](START_READINESS.md) for the state contract, evidence and limitations.

### Floor Map

The Floor Map camera entity renders a live PNG showing:

- **Room outlines** — reconstructed from the robot's perimeter pass data
- **Cleaned area** — the track the robot has driven so far, updated as it cleans
- **Dock location** — marked on the map
- **Room labels** — zone names pulled from your Dyson app layout

The map refreshes every 5 seconds while the robot is cleaning, and every 60 seconds when idle.

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

- Home Assistant **2024.1** or newer
- The robot already registered in the **Dyson app**
- Your **Dyson account email and password**, with access to the email inbox for a one-time code — or, alternatively, a Dyson **bearer token** (see [Getting a bearer token](#getting-a-bearer-token))
- Your Dyson account **country code** (e.g. `AU`, `GB`, `US`, `DE`); the token method also needs the robot's **serial number**

---

## Installation

### Option A — HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/UNsync3D/ha-dyson-spot-scrub` as an **Integration**
3. Search for *Dyson Spot+Scrub AI* and install
4. Restart Home Assistant

### Option B — Manual

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
3. **MQTT**: the robot is then controlled entirely over a direct MQTT-over-WebSocket connection. No further cloud calls are made during normal operation.
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
The MQTT connection to Dyson's IoT backend dropped. The integration will reconnect automatically. If it stays unavailable, try reloading the integration from **Settings → Devices & Services**.

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

## Licence

MIT

## Consumable monitoring

Five percentage sensors report used lifespan for the brush bar, mop roller, side brushes, robot filter and dock filter. A cleaning-solution sensor reports OK or Refill required. See [Consumable sensors](CONSUMABLES.md) for entity identification, card integration and testing.


### Mop roller drying (fork build v1.4.5)

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
then call it with the selected vacuum's `entity_id`. The planner card's existing
Cancel drying placeholder still needs frontend wiring in a separate card update.
