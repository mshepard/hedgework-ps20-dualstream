"""Periodic snapshot worker.

On each tick, briefly acquires each camera, captures a frame, and writes a
JPEG to ``{path}/camera{N}/{YYYYMMDD}/{HHMMSS}.jpg``. A retention pass
prunes files older than ``retention_days``.

Phase 1: fixed interval. Phase 2 will multiply this interval by the active
power-mode's snapshot-interval-factor.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from dualstream.cameras import Camera, CameraManager
from dualstream.config import SnapshotsConfig

logger = logging.getLogger("dualstream.snapshots")


class SnapshotWorker:
    def __init__(
        self,
        cameras: CameraManager,
        config: SnapshotsConfig,
    ) -> None:
        self._cameras = cameras
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def base_path(self) -> Path:
        return self._config.path

    def start(self) -> None:
        if not self._config.enabled:
            logger.info("Snapshot worker disabled in config")
            return
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Snapshot worker started: interval=%ds, path=%s",
            self._config.interval_seconds,
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
        # Take one snapshot promptly on startup, then loop at the interval.
        try:
            await self._tick()
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Snapshot worker crashed")
            raise

    async def _tick(self) -> None:
        timestamp = datetime.now()
        for cam in self._cameras.all():
            try:
                await self._capture_one(cam, timestamp)
            except Exception:
                logger.exception("Snapshot failed for camera %d", cam.camera_num)
        try:
            self._prune()
        except Exception:
            logger.exception("Snapshot retention pruning failed")

    async def _capture_one(self, cam: Camera, timestamp: datetime) -> Path:
        async with cam.session():
            array = await cam.capture()
        return await asyncio.get_running_loop().run_in_executor(
            None, self._encode_and_write, cam.camera_num, array, timestamp
        )

    def _encode_and_write(self, camera_num: int, array, timestamp: datetime) -> Path:
        directory = self.base_path / f"camera{camera_num}" / timestamp.strftime("%Y%m%d")
        directory.mkdir(parents=True, exist_ok=True)
        filename = timestamp.strftime("%H%M%S") + ".jpg"
        out_path = directory / filename
        image = Image.fromarray(array, mode="RGB")
        image.save(out_path, format="JPEG", quality=self._config.jpeg_quality, optimize=True)
        logger.debug("Wrote snapshot %s", out_path)
        return out_path

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
