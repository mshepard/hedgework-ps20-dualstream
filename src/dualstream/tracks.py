"""aiortc VideoStreamTrack backed by a Camera.

Reads RGB frames from a Camera, wraps them as PyAV VideoFrames, and lets
aiortc/libx264 encode them for the WebRTC peer connection. Frame pacing is
provided by picamera2's FrameDurationLimits (configured in Camera) so we
just stamp wall-clock PTS values at the standard 90 kHz WebRTC video clock.
"""

from __future__ import annotations

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

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError

        try:
            array = await self.camera.capture()
        except RuntimeError:
            # Camera was stopped underneath us (e.g. forced shutdown).
            self.stop()
            raise MediaStreamError

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
