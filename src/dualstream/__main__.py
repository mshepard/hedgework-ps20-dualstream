"""Entrypoint: `python -m dualstream` or the installed `dualstream` script."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aiohttp import web

from dualstream import __version__
from dualstream.cameras import build_manager
from dualstream.config import load_config
from dualstream.server import DualStreamServer
from dualstream.snapshots import SnapshotWorker


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # aiortc is chatty at INFO; bump it to WARNING unless --verbose.
    if not verbose:
        logging.getLogger("aioice").setLevel(logging.WARNING)
        logging.getLogger("aiortc").setLevel(logging.WARNING)


def _apply_phase1_power_hooks(disable_act_led: bool) -> None:
    """One-shot OS tweaks for Phase 1.

    The activity LED trigger lives in /sys and requires root to write. The
    service runs as the unprivileged 'dualstream' user, so this best-effort
    attempt will normally log a warning rather than succeed. Phase 2 wires
    up the privileged power.py path (sudoers / capabilities) that can drive
    LEDs, the CPU governor, HDMI, and HARD_SLEEP halt.
    """

    if not disable_act_led:
        return
    led_trigger = Path("/sys/class/leds/ACT/trigger")
    if not led_trigger.exists():
        return
    log = logging.getLogger("dualstream.power")
    try:
        led_trigger.write_text("none\n")
        log.info("Activity LED disabled")
    except OSError as ex:
        log.debug("Could not disable ACT LED (expected as non-root in Phase 1): %s", ex)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dualstream")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to dualstream.toml (default: /etc/dualstream/dualstream.toml or ./config/dualstream.toml)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    log = logging.getLogger("dualstream.main")
    log.info("DualStream %s starting up", __version__)

    config = load_config(args.config)
    log.info(
        "Config: server=%s:%d, cam0=%dx%d@%dfps, cam1=%dx%d@%dfps, snapshots=%s",
        config.server.host,
        config.server.port,
        config.camera0.resolution[0],
        config.camera0.resolution[1],
        config.camera0.framerate,
        config.camera1.resolution[0],
        config.camera1.resolution[1],
        config.camera1.framerate,
        "on" if config.snapshots.enabled else "off",
    )
    if config.server.auth_token in ("", "change-me"):
        log.warning(
            "Server auth_token is the default/empty value; set a real token in config "
            "before any non-bench deployment"
        )
    if config.server.viewer_token in ("", "change-me-viewer"):
        log.warning(
            "Server viewer_token is the default/empty value; public per-camera pages "
            "(/cam0, /cam1) will reject requests until a real value is set"
        )

    _apply_phase1_power_hooks(config.power.disable_act_led)

    cameras = build_manager(config)
    snapshots = SnapshotWorker(cameras, config.snapshots)
    server = DualStreamServer(config, cameras, snapshots)
    app = server.build()

    async def _on_startup(_app: web.Application) -> None:
        snapshots.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await snapshots.stop()
        await cameras.shutdown()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        access_log=None,  # we log signaling separately
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
