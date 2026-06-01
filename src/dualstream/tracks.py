"""aiortc VideoStreamTrack backed by a Camera.

Reads RGB frames from a Camera, wraps them as PyAV VideoFrames, and lets
aiortc/libx264 encode them for the WebRTC peer connection. Frame pacing is
provided by picamera2's FrameDurationLimits (configured in Camera) so we
just stamp wall-clock PTS values at the standard 90 kHz WebRTC video clock.
"""

from __future__ import annotations

import hashlib
import logging
import time
from fractions import Fraction

import av
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError

from dualstream.cameras import Camera

logger = logging.getLogger("dualstream.tracks")


VIDEO_CLOCK_RATE = 90000
VIDEO_TIME_BASE = Fraction(1, VIDEO_CLOCK_RATE)

# Log a frame-content fingerprint every N frames per track so we can tell
# from journalctl whether the two cameras are producing distinct pixels.
DIAG_LOG_EVERY = 30


class CameraTrack(VideoStreamTrack):
    """A VideoStreamTrack that pulls frames from a single Camera.

    The Camera refcount is *not* managed here; the server holds the
    acquire()/release() lifecycle alongside the RTCPeerConnection so that
    closing the PC reliably releases the camera.
    """

    kind = "video"

    def __init__(self, camera: Camera, label: str | None = None) -> None:
        super().__init__()
        self.camera = camera
        self.label = label or f"camera{camera.camera_num}"
        self._start_monotonic: float | None = None
        self._frame_count = 0

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError

        try:
            array = await self.camera.capture()
        except RuntimeError:
            # Camera was stopped underneath us (e.g. forced shutdown).
            self.stop()
            raise MediaStreamError

        # Frame-content fingerprint diagnostic. Logged at DEBUG, so it only
        # appears when the service is started with --verbose. Useful for
        # confirming both cameras are producing distinct frames if the dual
        # video tiles ever start showing the same content again.
        if (
            self._frame_count % DIAG_LOG_EVERY == 0
            and logger.isEnabledFor(logging.DEBUG)
        ):
            digest = hashlib.md5(array.tobytes()).hexdigest()[:10]
            first_px = array[0, 0].tolist() if array.size else []
            logger.debug(
                "cam%d frame#%d shape=%s dtype=%s hash=%s first_px=%s",
                self.camera.camera_num,
                self._frame_count,
                tuple(array.shape),
                array.dtype,
                digest,
                first_px,
            )
        self._frame_count += 1

        if self._start_monotonic is None:
            self._start_monotonic = time.monotonic()
            pts = 0
        else:
            elapsed = time.monotonic() - self._start_monotonic
            pts = int(elapsed * VIDEO_CLOCK_RATE)

        frame = av.VideoFrame.from_ndarray(array, format="rgb24")
        frame.pts = pts
        frame.time_base = VIDEO_TIME_BASE
        return frame
