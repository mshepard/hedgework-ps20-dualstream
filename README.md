# DualStream

Dual Pi Camera 3 WebRTC streamer for Raspberry Pi 5, designed for solar-powered, LTE-connected remote monitoring deployments.

This repository is split into two phases:

- **Phase 1 (this build)** — Core streaming server: two simultaneous WebRTC video streams, periodic snapshots, basic web UI, systemd service, Tailscale-friendly.
- **Phase 2 (planned)** — Power and bandwidth management: INA226 battery monitor, mode state machine (FULL/REDUCED/SNAPSHOT_ONLY), scheduled `HARD_SLEEP` via the onboard RTC + J5 BAT, USB-A router-power gating.

See `.cursor/plans/dualstream_pi5_webrtc_*.plan.md` for the full design plan.

---

## Phase 1: what you get

- Two `picamera2`-backed video streams (default 1280x720 @ 15 fps, software H.264 via libx264 inside aiortc).
- WebRTC delivery to a browser, no STUN/TURN required when connecting over Tailscale.
- Periodic JPEG snapshots written to `/var/lib/dualstream/snapshots/cameraN/YYYYMMDD/HHMMSS.jpg` with a configurable retention window.
- Two web surfaces:
  - **Admin dual-tile UI at `/`** — side-by-side feeds, manual snapshot trigger, recent-snapshot strip, and a live log. Admin bearer token required.
  - **Public per-camera viewers at `/cam0` and `/cam1`** — single-feed kiosk-style page with Start/Stop/Fullscreen and nothing else. Access is gated by a separate `viewer_token` that travels as `?key=…` in the URL, so the URL itself is shareable.
- Auto-generated random tokens on first install (admin + viewer) printed by the installer; config file is `0640 root:dualstream` so other local users can't read the tokens.
- Static UI and snapshot images are unauthenticated so they can be embedded.
- systemd service that runs as a dedicated `dualstream` user.
- Activity LED disabled on startup (the only Phase 1 power hook).

What is intentionally **not** in Phase 1:

- No battery monitoring, mode state machine, scheduled sleep, or EEPROM tweaks. Those land in Phase 2.
- No motion detection, recording, or audio. Pi Camera 3 has no microphone.
- No multi-viewer fan-out. Each additional viewer spawns its own encoder; Phase 1 supports one viewer per camera comfortably.

## Hardware setup

- Raspberry Pi 5 (4 GB or 8 GB).
- Two Raspberry Pi Camera Module 3 units connected to **CSI0 and CSI1** (the two ribbon connectors on the Pi 5).
- (Optional in Phase 1; required for Phase 2) Pi Foundation RTC battery on the J5 BAT header.
- Linovision IOT-R41 LTE router, either powered from the same 12 V bus or — for Phase 2's coordinated `HARD_SLEEP` — from one of the Pi's USB-A ports.

### Enabling both cameras

On Raspberry Pi OS Bookworm, `camera_auto_detect=1` (the default) usually handles a single camera but may not enumerate both Pi Camera 3 modules. If `rpicam-hello --list-cameras` doesn't show both, edit `/boot/firmware/config.txt` and add:

```
camera_auto_detect=0
dtoverlay=imx708,cam0
dtoverlay=imx708,cam1
```

Reboot, then verify with `rpicam-hello --list-cameras` (should list two devices) and `rpicam-hello --camera 0 -t 1000` followed by `rpicam-hello --camera 1 -t 1000`.

## Install

Clone the repo to the Pi (or `scp` it across), then:

```bash
cd DualStream
sudo bash scripts/install.sh                   # core install only
# or, to also install Tailscale:
sudo bash scripts/install.sh --install-tailscale
```

The installer:

1. Installs apt deps (`python3-picamera2`, `libcamera-tools`, `ffmpeg`, etc.).
2. Optionally installs Tailscale.
3. Creates the `dualstream` system user.
4. Creates `/etc/dualstream/dualstream.toml` from the bundled default.
5. **Auto-generates random admin and viewer tokens** and writes them into the config (only when the placeholder values are still present; user-customized tokens are preserved). On upgrades, also auto-adds the `viewer_token` line if a pre-existing config lacks it. Tightens the file to `0640 root:dualstream`.
6. Creates `/opt/dualstream/.venv` with `--system-site-packages` so picamera2 from apt is visible.
7. `pip install`s this project into the venv.
8. Installs and enables the `dualstream.service` systemd unit (does **not** start it).

Re-running the installer is safe.

### Updating after a `git pull`

The installer does a **non-editable** pip install, which copies the source into `/opt/dualstream/.venv`. A bare `git pull` updates the working tree but **does not** update the running copy. To pick up new code:

```bash
cd ~/DualStream                    # wherever you cloned it
git pull
sudo bash scripts/update.sh        # fast: re-installs the package + restarts the service
```

`scripts/update.sh` does only the necessary parts of `install.sh` (rebuild the dualstream package, fix ownership, restart the service) so it takes seconds rather than minutes. The full installer still works for upgrades that need apt or systemd changes.

**At the end of the install, the script prints the freshly generated tokens.** Copy them — they are not displayed again. You can always re-read them from the config file with `sudo cat /etc/dualstream/dualstream.toml`.

## Configure

`/etc/dualstream/dualstream.toml` is created and populated for you. The two tokens you'll interact with most:

```toml
[server]
host = "0.0.0.0"
port = 8080
auth_token = "<random, generated by installer>"      # admin (dual-tile UI)
viewer_token = "<random, generated by installer>"    # public per-camera URLs
site_name = "HEDGEWORK @ PS 20"                      # brand mark shown in the header on both surfaces
```

To give the public per-camera pages human-friendly headers, set a `name` per camera (empty falls back to `Camera N`):

```toml
[camera0]
name = "North View"

[camera1]
name = "South View"
```

Both the public per-camera pages and the admin dual-tile UI carry the umbrella **Hedgework @ PS 20** brand at [ps20.hedgework.net](https://ps20.hedgework.net/): Inter typeface for body, Lexend Tera for the brand mark, dusty pink header, dark forest text, orange accent buttons, off-white background, sky blue borders. The hedgework illustration (by Johanna Kindvall) is anchored bottom-centre of the viewport as a fixed background. Drop a different PNG into `src/dualstream/webui/media/` and update the `--cam-bg-image` custom property (in `style.css`) on either `.cam-page` or `.admin-page` to swap the artwork. The full palette lives in CSS variables at the top of each `body.<page>-page` block — change one variable to retune.

The admin page keeps a dark forest log panel (instead of the cream body background) because logs are a debug surface where monospace-on-dark reads best.

`Hedge-icon.png` in `webui/media/` is wired up as the browser favicon on both pages. Inter and Lexend Tera load from Google Fonts; if the browser can't reach `fonts.googleapis.com` (e.g. an air-gapped tailnet), the CSS falls back to a system sans-serif so both pages still render cleanly.

The shipped artwork PNG has been palette-quantized to 256 colours (1.6 MB → 188 KB) and the favicon to 128 colours (79 KB → 23 KB); quality is visually indistinguishable from the originals at their on-page sizes.

All other Phase 1 settings are commented in the file. Phase 2 will append `[battery]`, `[schedule]`, and an expanded `[power]` block.

## Run

```bash
sudo tailscale up                       # one-time, if Tailscale was installed
sudo systemctl start dualstream
sudo systemctl status dualstream
journalctl -u dualstream -f             # follow logs
```

### Two web surfaces

**Admin dual-tile UI (for testing / troubleshooting):**
```
http://<pi-tailnet-name>:8080/
```
Paste the **admin `auth_token`** from the config into the token field at the top of the page (cached in your browser's `localStorage`). Click **Start** to begin streaming. This page also has a manual snapshot button, a snapshot strip, and a live activity log. Per-tile "↗ single view" links are auto-populated with the viewer key once the page loads.

**Public per-camera viewer pages (for sharing):**
```
http://<pi-tailnet-name>:8080/cam0?key=<viewer_token>
http://<pi-tailnet-name>:8080/cam1?key=<viewer_token>
```
Each is a single-camera page with Start, Stop, and Fullscreen — no log, no token field. The URL is the access credential, so treat it like a password.

While the live stream is not running, the page displays the most recent snapshot as a still preview (auto-refreshed every 30 s) so visitors immediately see what's on camera without needing to start a WebRTC session. The header shows "last snapshot HH:MM:SS" to confirm freshness.

### Exposing the public pages over the internet (Tailscale Funnel)

By default, the URLs above are reachable only by clients on the same tailnet. If you want guests to view the cameras *without* installing the Tailscale client, enable **Tailscale Funnel** — it terminates HTTPS at Tailscale's edge and tunnels traffic to the Pi over the connection it already holds open outbound, so the LTE router doesn't need any inbound port forwarding.

**One-time prep in the admin console:**
1. **DNS → MagicDNS**: enabled.
2. **DNS → HTTPS Certificates**: **Enable HTTPS**.
3. **Access Controls (ACL)** — grant the `funnel` attribute to the node (replace the target with your own tag or user as appropriate):

   ```json
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```

**Turn it on, on the Pi:**

```bash
sudo tailscale funnel --bg 8080
sudo tailscale funnel status     # confirm
```

The whole dualstream surface is now reachable as `https://<hostname>.<tailnet>.ts.net/…` (find the hostname with `tailscale status --self`). All credential checks still apply, so:

| Public path | What it does |
|---|---|
| `/cam0?key=<viewer_token>` | live viewer page for camera 0 |
| `/cam1?key=<viewer_token>` | live viewer page for camera 1 |
| `/` (admin UI) | renders, but the JS / APIs all require the admin `auth_token` |
| `/snapshots/<...>?key=…` | snapshot JPEGs — gated by the viewer or admin token |
| `/api/public/*`, `/api/*` | as documented above (viewer or admin token) |

Snapshot JPEGs and the snapshot-list API both require the viewer token (or admin token), so even when the service is reachable from the public internet, nobody can enumerate `/snapshots/cameraN/YYYYMMDD/HHMMSS.jpg` without holding a valid credential. The snapshot URLs returned by the APIs already include `?key=…` so the `<img>` and `<video poster>` tags load them transparently.

To turn Funnel back off:

```bash
sudo tailscale funnel --bg --https=443 off
sudo tailscale serve reset
```

## Phase 1 validation checklist

These mirror the validation steps in the plan, scoped to Phase 1:

1. `rpicam-hello --camera 0 -t 1000` and `rpicam-hello --camera 1 -t 1000` each show a preview.
2. `curl http://localhost:8080/health` returns `{"status":"ok","version":"..."}`.
3. From a tailnet laptop, open the UI; both video tiles should connect and play within ~3 seconds of clicking **Start**.
4. While streaming, `top` shows the dualstream process using roughly 30–60% total CPU.
5. Click **Stop** (or close the tab). Within `idle_grace_seconds`, both cameras stop (the per-camera `running` flag in `/api/status` flips to `false`).
6. Wait `snapshots.interval_seconds`; a new JPEG appears under `/var/lib/dualstream/snapshots/`. The snapshot strip in the UI also refreshes.
7. Click **Snapshot**; a fresh JPEG appears immediately.
8. `sudo systemctl restart dualstream` succeeds and the service returns to a clean state.

## Project layout

```
DualStream/
  pyproject.toml
  config/
    dualstream.toml           # default config (installed to /etc/dualstream/)
  scripts/
    install.sh                # installer
    dualstream.service        # systemd unit
  src/dualstream/
    __init__.py
    __main__.py               # entrypoint (python -m dualstream)
    config.py                 # pydantic + TOML loader
    quality.py                # QualityProfile dataclass
    cameras.py                # Picamera2 wrapper with refcounted lifecycle
    tracks.py                 # aiortc VideoStreamTrack subclass
    snapshots.py              # periodic snapshot worker + retention
    server.py                 # aiohttp app
    webui/
      index.html                # admin dual-tile UI
      app.js
      cam.html                  # public per-camera viewer (served at /cam0, /cam1)
      cam.js
      style.css
```

## Troubleshooting

- **Both cameras don't enumerate**: see "Enabling both cameras" above.
- **`ImportError: No module named picamera2`**: the venv was created without `--system-site-packages`. Reinstall with `sudo bash scripts/install.sh` (the script handles this), or recreate the venv with `python3 -m venv --system-site-packages /opt/dualstream/.venv`.
- **WebRTC connects but no video**: check `journalctl -u dualstream -f` for capture errors; verify the camera works standalone with `rpicam-hello`.
- **High CPU**: lower `framerate` or `resolution` in the per-camera config. Phase 2's `REDUCED` mode will do this automatically based on battery state.
- **401 from API**: paste the admin `auth_token` (from `/etc/dualstream/dualstream.toml`) into the dual-tile UI's token field, or set `Authorization: Bearer ...` on your `curl` calls.
- **`/cam0` says "Access key required"**: open the URL with `?key=<viewer_token>` appended (the admin UI's per-tile "↗ single view" link does this automatically once you're authenticated). If `viewer_token` is empty in the config, the public endpoints are disabled by design and the page will refuse to negotiate.
- **Snapshot JPEG URL returns 401**: `/snapshots/*` is no longer anonymous — the APIs (`/api/snapshots`, `/api/public/snapshots/latest`, `/api/snapshot`) emit URLs already keyed with `?key=…`. If you copied a snapshot URL from before the gating was added, refresh the snapshot strip / latest-snapshot poll so the page picks up new URLs.
- **Forgot your tokens**: `sudo cat /etc/dualstream/dualstream.toml` shows both. To rotate, edit the file (set either value back to the placeholder `"change-me"` / `"change-me-viewer"` then re-run `sudo bash scripts/install.sh`, or just paste in your own new random string) and `sudo systemctl restart dualstream`.
