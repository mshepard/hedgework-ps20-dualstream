"""Camera lifecycle manager.

Wraps two Picamera2 instances with reference-counted start/stop. Frames are
captured in worker threads (picamera2 calls are blocking) and exposed to
async consumers as RGB-ordered numpy arrays of shape (H, W, 3), suitable
for both PyAV (format "rgb24") and Pillow (mode "RGB").

Pixel-format note: picamera2 has the historical quirk that its "RGB888"
format produces BGR-ordered data in numpy, and its "BGR888" format produces
RGB-ordered data. We use "BGR888" so callers receive RGB without an
explicit channel swap.

Phase 1: single "main" stream per camera, refcount, configurable idle
grace period. Phase 2 will add a "lores" stream (YUV) for snapshots and
power-mode-driven quality clamps.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from picamera2 import Picamera2  # imported lazily at runtime

from dualstream.config import AppConfig, CameraConfig
from dualstream.quality import QualityProfile

logger = logging.getLogger("dualstream.cameras")


class Camera:
    """Single picamera2 device with refcounted start/stop."""

    def __init__(
        self,
        camera_num: int,
        quality: QualityProfile,
        controls: dict[str, Any] | None = None,
        idle_grace_seconds: int = 10,
    ) -> None:
        self.camera_num = camera_num
        self.quality = quality
        self.controls = dict(controls or {})
        self.idle_grace_seconds = idle_grace_seconds

        self._picam2: Picamera2 | None = None
        self._refcount: int = 0
        self._refcount_lock = asyncio.Lock()
        # Serialise capture calls; picamera2 is not documented as fully
        # thread-safe and we hand work to the default thread pool. We
        # rebind this on recovery so a leaked executor thread that's
        # stuck inside ``picam2.capture_array`` (and therefore still
        # holding the old lock) can't deadlock the new pipeline.
        self._device_lock = threading.Lock()
        self._stop_task: asyncio.Task[None] | None = None
        # Set by ``mark_broken()`` (typically from the snapshot worker
        # after its per-capture timeout fires) and consumed by the next
        # ``acquire()``, which tears the abandoned picamera2 instance
        # down and starts a fresh one.
        self._broken: bool = False

    @property
    def running(self) -> bool:
        return self._picam2 is not None

    @property
    def refcount(self) -> int:
        return self._refcount

    def mark_broken(self) -> None:
        """Signal that this camera's capture pipeline is wedged.

        The next ``acquire()`` will abandon the current ``Picamera2``
        instance (its leaked executor thread, blocked inside
        ``capture_array``, may still hold the old ``_device_lock``),
        rebind ``_device_lock``, best-effort close the old instance on
        a daemon thread, and start a fresh one. Callers can keep
        invoking this idempotently; only the next ``acquire()`` acts on
        it."""
        self._broken = True

    async def acquire(self) -> None:
        async with self._refcount_lock:
            self._refcount += 1
            if self._stop_task is not None and not self._stop_task.done():
                self._stop_task.cancel()
                self._stop_task = None
            try:
                if self._broken:
                    logger.warning(
                        "Camera %d marked broken; abandoning instance and reopening",
                        self.camera_num,
                    )
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._recover_blocking)
                    self._broken = False
                elif self._picam2 is None:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._start_blocking)
            except Exception:
                # Don't leak a refcount if start/recover failed; the
                # next acquire will retry from a clean slate.
                self._refcount -= 1
                raise

    async def release(self) -> None:
        async with self._refcount_lock:
            if self._refcount > 0:
                self._refcount -= 1
            if self._refcount == 0 and self._picam2 is not None:
                self._stop_task = asyncio.create_task(self._delayed_stop())

    async def _delayed_stop(self) -> None:
        try:
            await asyncio.sleep(self.idle_grace_seconds)
        except asyncio.CancelledError:
            return
        async with self._refcount_lock:
            if self._refcount == 0 and self._picam2 is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._stop_blocking)

    def _start_blocking(self) -> None:
        from picamera2 import Picamera2  # noqa: PLC0415  (deferred import)

        logger.info(
            "Starting camera %d at %dx%d @ %d fps",
            self.camera_num,
            self.quality.width,
            self.quality.height,
            self.quality.framerate,
        )
        picam2 = Picamera2(camera_num=self.camera_num)
        frame_duration_us = int(round(1_000_000 / self.quality.framerate))
        # "BGR888" in picamera2 nomenclature returns numpy data in RGB channel
        # order; "RGB888" would return BGR. Pick BGR888 so PyAV "rgb24" and
        # Pillow "RGB" both accept the array directly.
        config = picam2.create_video_configuration(
            main={"size": (self.quality.width, self.quality.height), "format": "BGR888"},
            controls={
                "FrameDurationLimits": (frame_duration_us, frame_duration_us),
                **self.controls,
            },
            buffer_count=4,
        )
        picam2.configure(config)
        picam2.start()
        self._picam2 = picam2

    def _stop_blocking(self) -> None:
        picam2 = self._picam2
        if picam2 is None:
            return
        logger.info("Stopping camera %d", self.camera_num)
        try:
            picam2.stop()
            picam2.close()
        finally:
            self._picam2 = None

    def _recover_blocking(self) -> None:
        """Drop a wedged ``Picamera2`` instance and start a fresh one.

        The leaked executor thread that's stuck inside
        ``picam2.capture_array`` may still be holding ``_device_lock``,
        so we rebind the lock here — new captures use the fresh lock
        and won't deadlock against the stuck thread. We also drop our
        reference to the old picam2 and try to call ``close()`` on it
        from a daemon thread, with a short wait. ``close()`` can itself
        block on the wedged libcamera pipeline; if it does, we
        proceed without it and accept that libcamera may refuse to let
        us reopen the device (in which case ``_start_blocking`` below
        raises and the caller's ``acquire()`` rolls back the
        refcount).
        """
        old_picam2 = self._picam2
        self._picam2 = None
        self._device_lock = threading.Lock()

        if old_picam2 is not None:
            done = threading.Event()

            def _closer() -> None:
                try:
                    old_picam2.close()
                except Exception:
                    logger.exception(
                        "Force-close of camera %d failed", self.camera_num
                    )
                finally:
                    done.set()

            threading.Thread(
                target=_closer,
                daemon=True,
                name=f"cam{self.camera_num}-recover-close",
            ).start()
            if not done.wait(timeout=3.0):
                logger.error(
                    "Camera %d close() hung during recovery; "
                    "abandoning old picamera2 instance",
                    self.camera_num,
                )

        self._start_blocking()

    async def capture(self) -> np.ndarray:
        """Capture one frame from the main stream as an RGB (H, W, 3) array."""

        if self._picam2 is None:
            raise RuntimeError(f"Camera {self.camera_num} is not running")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._capture_blocking)

    def _capture_blocking(self) -> np.ndarray:
        with self._device_lock:
            picam2 = self._picam2
            if picam2 is None:
                raise RuntimeError(f"Camera {self.camera_num} is not running")
            # Defensive copy: picamera2.capture_array() may, on some
            # libcamera versions / multi-camera setups, hand back a view
            # into a shared buffer pool. Copying decouples the two
            # cameras' frame streams.
            return picam2.capture_array("main").copy()

    @asynccontextmanager
    async def session(self):
        await self.acquire()
        try:
            yield self
        finally:
            await self.release()


class CameraManager:
    """Holds the configured cameras keyed by camera_num."""

    def __init__(self, cameras: dict[int, Camera]) -> None:
        self._cameras = cameras

    def get(self, camera_num: int) -> Camera:
        return self._cameras[camera_num]

    def all(self) -> list[Camera]:
        return list(self._cameras.values())

    def numbers(self) -> list[int]:
        return sorted(self._cameras.keys())

    async def shutdown(self) -> None:
        loop = asyncio.get_running_loop()
        for cam in self._cameras.values():
            if cam._stop_task is not None and not cam._stop_task.done():
                cam._stop_task.cancel()
            if cam._picam2 is not None:
                await loop.run_in_executor(None, cam._stop_blocking)


def _make_camera(num: int, cfg: CameraConfig, idle_grace_seconds: int) -> Camera:
    return Camera(
        camera_num=num,
        quality=cfg.to_quality(),
        controls=cfg.controls,
        idle_grace_seconds=idle_grace_seconds,
    )


def build_manager(config: AppConfig) -> CameraManager:
    cameras = {
        0: _make_camera(0, config.camera0, config.power.idle_grace_seconds),
        1: _make_camera(1, config.camera1, config.power.idle_grace_seconds),
    }
    return CameraManager(cameras)
