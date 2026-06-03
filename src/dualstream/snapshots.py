"""Periodic snapshot worker.

On each tick, briefly acquires each camera, captures a frame, and writes a
JPEG to ``{path}/camera{N}/{YYYYMMDD}/{HHMMSS}.jpg``. A retention pass
prunes files older than ``retention_days``.

The worker has two cadences:

  * ``interval_seconds`` (idle) — used when no public viewer is polling.
    Typically minutes; keeps storage / power impact low.
  * ``active_interval_seconds`` (active) — used while a public viewer is
    actively polling ``/api/public/snapshots/latest``. Typically 1–2 s;
    drives the snapshot-streaming UX on the cam page. Activity is
    reported by the server via :meth:`note_viewer_activity`, which both
    records the last-activity timestamp and wakes the worker if it's
    currently sleeping through a long idle interval.

Phase 2 will multiply both intervals by the active power-mode's
snapshot-interval-factor.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from dualstream.cameras import Camera, CameraManager
from dualstream.config import SnapshotsConfig

logger = logging.getLogger("dualstream.snapshots")


class SnapshotWorker:
    # Wall-clock budget for a single ``cam.capture()`` call. picamera2's
    # blocking ``capture_array`` runs inside the per-camera executor;
    # if the underlying libcamera pipeline stalls the await would hang
    # forever. When it fires we mark the camera broken so the next
    # ``acquire()`` rebuilds it from scratch — see
    # ``Camera.mark_broken()``. Healthy captures complete in ~50 ms
    # and a forced recovery (close + reopen) lands in ~20 ms on a Pi 5,
    # so 2 s is generous; shorter than 4 s keeps the cost of each
    # wedge low enough that the slideshow stays responsive.
    CAPTURE_TIMEOUT_SECONDS: float = 2.0

    def __init__(
        self,
        cameras: CameraManager,
        config: SnapshotsConfig,
    ) -> None:
        self._cameras = cameras
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Viewer-activity bookkeeping. _last_activity is a monotonic
        # timestamp set by note_viewer_activity(); the worker uses it to
        # decide between idle and active cadence on each tick. The event
        # is set on every activity ping so a worker currently asleep in a
        # long idle interval wakes up promptly and starts producing
        # frames at the fast cadence.
        self._last_activity: float = 0.0
        self._activity_event = asyncio.Event()
        # Heartbeat counters surfaced for diagnostics. Don't gate any
        # control flow on these — they're purely observational.
        self._tick_count: int = 0
        self._last_tick_finished_at: float = 0.0

    @property
    def base_path(self) -> Path:
        return self._config.path

    def note_viewer_activity(self) -> None:
        """Record a public-viewer poll and wake the worker if idling.

        Called from the aiohttp handler that serves the latest-snapshot
        endpoint. Cheap and safe to call on every request; only the
        timestamp + event update happens, no I/O. Setting the event is
        idempotent — extra calls while it's already set are no-ops."""
        self._last_activity = time.monotonic()
        self._activity_event.set()

    def _viewer_is_active(self) -> bool:
        if self._last_activity == 0.0:
            return False
        return (
            time.monotonic() - self._last_activity
            < self._config.active_window_seconds
        )

    def _next_interval(self) -> float:
        """Seconds to sleep before the next tick, based on activity."""
        if self._viewer_is_active():
            return float(self._config.active_interval_seconds)
        return float(self._config.interval_seconds)

    def start(self) -> None:
        if not self._config.enabled:
            logger.info("Snapshot worker disabled in config")
            return
        if self._task is not None:
            return
        self._stop_event.clear()
        self._activity_event.clear()
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Snapshot worker started: idle=%ds, active=%.2fs, "
            "active_window=%.0fs, path=%s",
            self._config.interval_seconds,
            self._config.active_interval_seconds,
            self._config.active_window_seconds,
            self.base_path,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        # Take one snapshot promptly on startup, then loop. Each iteration
        # sleeps for either the idle or the active interval and can be
        # interrupted by a viewer-activity ping (so we don't make someone
        # wait minutes for the next idle-cadence tick to fire after they
        # open the page). Clean shutdown is delivered as task
        # cancellation by ``stop()``, which propagates CancelledError up
        # through ``wait_for`` and back here.
        #
        # ``_safe_tick`` is the only call into ``_tick`` in this loop: it
        # absorbs any exception ``_tick`` raises so a single bad capture
        # (camera glitch, picamera2 timeout, ENOSPC on the snapshot
        # volume, …) doesn't kill the worker. Without this, the worker
        # would tick once on activity, raise inside the next ``_tick``,
        # propagate out through ``_run``, and silently end the task —
        # which is exactly the failure mode we hit on the Pi the first
        # time the cam page was opened.
        try:
            await self._safe_tick()
            while not self._stop_event.is_set():
                self._activity_event.clear()
                interval = self._next_interval()
                try:
                    await asyncio.wait_for(
                        self._activity_event.wait(), timeout=interval
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                if self._stop_event.is_set():
                    break
                await self._safe_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Snapshot worker crashed")
            raise

    async def _safe_tick(self) -> None:
        """Run one ``_tick``, swallowing (and logging) any exception.

        Cancellation still propagates so ``stop()`` can shut the worker
        down cleanly; everything else is contained so the worker keeps
        looping even after a transient failure."""
        try:
            await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Snapshot tick failed; worker continuing")

    async def _tick(self) -> None:
        timestamp = datetime.now()
        self._tick_count += 1
        tick_num = self._tick_count
        start = time.monotonic()
        active = self._viewer_is_active()
        logger.info(
            "Snapshot tick %d starting (mode=%s, interval=%.2fs)",
            tick_num,
            "active" if active else "idle",
            self._next_interval(),
        )
        for cam in self._cameras.all():
            try:
                await self._capture_one(cam, timestamp)
            except Exception:
                logger.exception("Snapshot failed for camera %d", cam.camera_num)
        try:
            self._prune()
        except Exception:
            logger.exception("Snapshot retention pruning failed")
        elapsed = time.monotonic() - start
        self._last_tick_finished_at = time.monotonic()
        logger.info(
            "Snapshot tick %d done in %.2fs", tick_num, elapsed
        )

    async def _capture_one(self, cam: Camera, timestamp: datetime) -> Path:
        # Wrap the blocking capture in a wall-clock timeout. ``cam.capture()``
        # itself hands work to a thread pool, so a TimeoutError here only
        # frees the awaiting coroutine — the underlying executor thread
        # is still stuck in picamera2, holding the camera's device lock.
        # We mark the camera broken so the next acquire() rebinds the
        # lock and reopens a fresh Picamera2 instance, breaking the
        # would-be deadlock. The leaked thread persists until libcamera
        # finally unblocks (which it may never do), but it no longer
        # blocks the next tick.
        async with cam.session():
            try:
                array = await asyncio.wait_for(
                    cam.capture(), timeout=self.CAPTURE_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, TimeoutError):
                cam.mark_broken()
                raise
        return await asyncio.get_running_loop().run_in_executor(
            None, self._encode_and_write, cam.camera_num, array, timestamp
        )

    def _encode_and_write(self, camera_num: int, array, timestamp: datetime) -> Path:
        # Write to a sibling .tmp file and then atomically rename it
        # into place. Without this, ``list_snapshots`` (called from the
        # public ``latest`` endpoint roughly every active_interval_seconds)
        # can pick up the file mid-write — the JPEG exists on disk but
        # only the header has been flushed — and hand its URL to the
        # browser, which then fails to decode and fires <img onerror>.
        # ``os.replace`` is atomic on POSIX, so a concurrent reader
        # either misses the file entirely or sees the fully-written one.
        directory = self.base_path / f"camera{camera_num}" / timestamp.strftime("%Y%m%d")
        directory.mkdir(parents=True, exist_ok=True)
        filename = timestamp.strftime("%H%M%S") + ".jpg"
        final_path = directory / filename
        tmp_path = directory / (filename + ".tmp")
        image = Image.fromarray(array, mode="RGB")
        image.save(tmp_path, format="JPEG", quality=self._config.jpeg_quality, optimize=True)
        os.replace(tmp_path, final_path)
        logger.debug("Wrote snapshot %s", final_path)
        return final_path

    async def capture_now(self) -> dict[int, Path]:
        """Manual trigger: capture one frame per camera right now."""

        timestamp = datetime.now()
        results: dict[int, Path] = {}
        for cam in self._cameras.all():
            path = await self._capture_one(cam, timestamp)
            results[cam.camera_num] = path
        return results

    def _prune(self) -> None:
        if self._config.retention_days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=self._config.retention_days)
        cutoff_ts = cutoff.timestamp()
        if not self.base_path.exists():
            return
        for jpeg in self.base_path.rglob("*.jpg"):
            try:
                if jpeg.stat().st_mtime < cutoff_ts:
                    jpeg.unlink()
            except FileNotFoundError:
                pass
        # Sweep empty date directories left behind.
        for sub in self.base_path.glob("camera*/*"):
            if sub.is_dir() and not any(sub.iterdir()):
                try:
                    sub.rmdir()
                except OSError:
                    pass

    def list_snapshots(self, camera_num: int | None = None, limit: int = 50) -> list[dict]:
        """List most recent snapshots as dicts with relative_path + timestamp."""

        if not self.base_path.exists():
            return []
        results: list[tuple[float, dict]] = []
        glob_root = (
            self.base_path / f"camera{camera_num}" if camera_num is not None else self.base_path
        )
        for jpeg in glob_root.rglob("*.jpg"):
            try:
                stat = jpeg.stat()
            except FileNotFoundError:
                continue
            results.append(
                (
                    stat.st_mtime,
                    {
                        "camera": int(jpeg.parent.parent.name.removeprefix("camera")),
                        "filename": jpeg.name,
                        "url": f"/snapshots/{jpeg.relative_to(self.base_path).as_posix()}",
                        "timestamp": stat.st_mtime,
                        "size_bytes": stat.st_size,
                    },
                )
            )
        results.sort(key=lambda t: t[0], reverse=True)
        return [item for _, item in results[:limit]]
