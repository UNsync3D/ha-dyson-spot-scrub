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

---

## Requirements

- Home Assistant **2024.1** or newer
- The robot already registered in the **Dyson app**
- A Dyson **bearer token** for your account (see [Getting a bearer token](#getting-a-bearer-token) below)
- Your robot's **serial number** and **country code** (e.g. `AU`, `GB`, `US`, `DE`)

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
3. Choose **Paste existing token**
4. Enter your bearer token, robot serial number, and country code
5. The integration will find your robot and create its entities

> **Country code must match your Dyson account region.** Common codes: `AU`, `GB`, `US`, `DE`, `FR`, `JP`, `NL`.

---

## Getting a bearer token

The easiest way to get your Dyson bearer token is to intercept the Dyson app's network traffic using **Proxyman** on a Mac with your iPhone on the same Wi-Fi network. This takes about 5 minutes.

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
   Bearer 02107737E7CD56482FC6E974039257F99987AA5D337520D6C36FF24F1813F682-1
   ```

5. Copy everything **after** the word `Bearer ` (including the long hex string and the `-1` suffix)

### Step 5 — Paste into Home Assistant

Use this token in the **Paste existing token** step of the integration setup. Tokens are long-lived — you should only need to do this once.

> **After setup:** Remove the proxy from your iPhone's Wi-Fi settings so normal traffic isn't routed through your Mac.

---

## How it works

1. **Authenticate**: your bearer token is stored in HA's config entry — your Dyson password is never stored.
2. **IoT credentials**: on each HA start the integration fetches short-lived MQTT credentials from Dyson's cloud for your specific device.
3. **MQTT**: the robot is then controlled entirely over a direct MQTT-over-WebSocket connection. No further cloud calls are made during normal operation.
4. **Auto-recovery**: if the MQTT connection drops, the integration automatically fetches fresh credentials and reconnects with exponential backoff (15 s → 30 s → 60 s → 120 s).

---

## Troubleshooting

### Integration not found after install
Make sure the folder is named exactly `dyson_spot_scrub` inside `custom_components/`, then restart HA.

### Bearer token is rejected
- Make sure you copied the full token value — it should be 65+ characters ending in `-1` or similar
- Tokens can expire if Dyson forces a re-login. Repeat the Proxyman steps to capture a fresh token, then re-enter it in the integration options

### Floor map is blank or shows an error
- The map is only populated after the robot has completed at least one cleaning session with room mapping enabled in the Dyson app
- Check the HA logs for `dyson_spot_scrub` errors — the map entity logs rendering failures at `WARNING` level

### Robot shows as Unavailable
The MQTT connection to Dyson's IoT backend dropped. The integration will reconnect automatically. If it stays unavailable, try reloading the integration from **Settings → Devices & Services**.

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
