# Dyson Spot+Scrub AI — Home Assistant Integration

A custom Home Assistant integration for the **Dyson Spot+Scrub AI** robot vacuum. Controls the robot locally over MQTT — no cloud polling during normal operation, so commands are fast and reliable.

---

## Features

| Entity | Type | Notes |
|---|---|---|
| Robot Vacuum | `vacuum` | Start, Stop, Return to Dock, cleaning mode selection |
| Battery | `sensor` | Live battery percentage |
| Docked | `binary_sensor` | On when robot is sitting in its dock |
| Charging | `binary_sensor` | On while battery is actively charging |
| Fault | `binary_sensor` | On when robot has reported an error |

### Cleaning modes

Selected via the **Fan Speed** control on the vacuum entity, or from any automation:

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
- Your Dyson account **email**, **password**, and **country code** (e.g. `AU`, `GB`, `US`, `DE`)

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
3. Choose **Sign in with Dyson account**
4. Enter your email, password, and country code
5. Check your email — Dyson will send a 6-digit one-time code
6. Enter the code; the integration will find your robot and create its entities

> **Country code must match your Dyson account region.** Common codes: `AU`, `GB`, `US`, `DE`, `FR`, `JP`, `NL`.

### Alternative: paste an existing bearer token

If you already have a Dyson bearer token (e.g. captured via a proxy tool, or copied from another integration), choose **Paste existing token** on the first screen. You will also need your robot's serial number and country code. The integration will attempt to look up the robot from Dyson's manifest automatically.

---

## How it works

1. **Authenticate**: your Dyson credentials are used once to obtain a bearer token via Dyson's cloud API. The token is stored in HA's config entry — your password is never stored.
2. **IoT credentials**: on each HA start the integration fetches short-lived MQTT credentials from Dyson's cloud for your specific device.
3. **MQTT**: the robot is then controlled entirely over a direct MQTT-over-WebSocket connection. No further cloud calls are made during normal operation.
4. **Auto-recovery**: if the MQTT connection drops, the integration automatically fetches fresh credentials and reconnects with exponential backoff (15 s → 30 s → 60 s → 120 s).

---

## Troubleshooting

### Integration not found after install
Make sure the folder is named exactly `dyson_spot_scrub` inside `custom_components/`, then restart HA.

### OTP email never arrives
- Check your spam folder
- Confirm the country code matches your Dyson account region
- Wait 60 seconds and try again — Dyson rate-limits OTP requests

### Authentication fails with "invalid credentials"
- Verify your email and password in the Dyson app
- If your Dyson account was created via **Sign in with Google** or **Sign in with Apple**, the email+password OTP flow is not available. Use the **Paste existing token** path instead — see [Getting a bearer token manually](#getting-a-bearer-token-manually) below.

### Robot shows as Unavailable
The MQTT connection to Dyson's IoT backend dropped. The integration will reconnect automatically. If it stays unavailable, try reloading the integration from **Settings → Devices & Services**.

### "Cannot connect" during setup
Dyson's cloud API may be temporarily unavailable. Wait a few minutes and try again.

---

## Getting a bearer token manually

If the OTP setup flow doesn't work for your account, you can obtain a bearer token using one of the methods below. Once you have it, use the **Paste existing token** path in the HA integration setup.

### Option 1 — test script (recommended)

Run the included script on any machine with Python 3 and internet access (including your Home Assistant host). It walks you through the full OTP flow interactively and prints your token at the end.

> ⚠️ **Important:** Dyson rate-limits authentication attempts. If the script returns `"Unable to authenticate user"`, **stop and wait 24 hours** before trying again — repeated attempts will extend the lockout.

```bash
# Install the dependency (once)
pip3 install aiohttp --break-system-packages

# Download the script
curl -O https://raw.githubusercontent.com/UNsync3D/ha-dyson-spot-scrub/main/test_auth_flow.py

# Run it
python3 test_auth_flow.py
```

### Option 2 — proxy capture

Route your phone through a proxy tool (Proxyman, mitmproxy, Charles) while logging in to the Dyson app. The `Authorization: Bearer …` header in any request to `appapi.cp.dyson.com` contains your token.

---

## Development

```bash
# Clone
git clone https://github.com/UNsync3D/ha-dyson-spot-scrub
cd ha-dyson-spot-scrub

# Symlink into your local HA dev instance
ln -s $PWD/custom_components/dyson_spot_scrub \
      ~/.homeassistant/custom_components/dyson_spot_scrub

# Test the auth flow standalone (no HA needed)
# ⚠️ Only attempt once — repeated failures trigger a 24h lockout on your Dyson account
pip3 install aiohttp --break-system-packages
python3 test_auth_flow.py
```

Pull requests welcome. Please open an issue before starting significant work so we can coordinate.

---

## Licence

MIT
