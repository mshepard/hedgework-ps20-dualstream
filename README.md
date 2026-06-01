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
- Minimal web UI with start/stop, manual snapshot trigger, and a recent-snapshot strip.
- Bearer-token auth on the API; static UI and snapshot images are unauthenticated so they can be embedded.
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
5. Creates `/opt/dualstream/.venv` with `--system-site-packages` so picamera2 from apt is visible.
6. `pip install`s this project into the venv.
7. Installs and enables the `dualstream.service` systemd unit (does **not** start it — set your token first).

Re-running the installer is safe.

## Configure

Edit `/etc/dualstream/dualstream.toml`. The single thing you **must** change before exposing the service is the bearer token:

```toml
[server]
host = "0.0.0.0"
port = 8080
auth_token = "pick-a-long-random-string"
```

All other Phase 1 settings are commented in the file. Phase 2 will append `[battery]`, `[schedule]`, and an expanded `[power]` block.

## Run

```bash
sudo tailscale up                       # one-time, if Tailscale was installed
sudo systemctl start dualstream
sudo systemctl status dualstream
journalctl -u dualstream -f             # follow logs
```

Browse from another tailnet-connected machine to `http://<pi-tailnet-name>:8080/`. Paste the bearer token from the config into the token field at the top of the page; the value is stored in your browser's `localStorage`. Click **Start** to begin streaming.

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
      index.html
      app.js
      style.css
```

## Troubleshooting

- **Both cameras don't enumerate**: see "Enabling both cameras" above.
- **`ImportError: No module named picamera2`**: the venv was created without `--system-site-packages`. Reinstall with `sudo bash scripts/install.sh` (the script handles this), or recreate the venv with `python3 -m venv --system-site-packages /opt/dualstream/.venv`.
- **WebRTC connects but no video**: check `journalctl -u dualstream -f` for capture errors; verify the camera works standalone with `rpicam-hello`.
- **High CPU**: lower `framerate` or `resolution` in the per-camera config. Phase 2's `REDUCED` mode will do this automatically based on battery state.
- **401 from API**: paste the bearer token (from `/etc/dualstream/dualstream.toml`) into the UI's token field, or set `Authorization: Bearer ...` on your `curl` calls.
