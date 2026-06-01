"""Quality profile dataclass.

Phase 1 reads these straight from config. Phase 2 will introduce a
PowerModeQualityClamp that returns an effective quality profile derived
from the configured one plus the current power mode. Putting it in one
place now keeps that wiring trivial later.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityProfile:
    """Per-camera streaming quality knobs."""

    width: int
    height: int
    framerate: int
    bitrate_kbps: int

    @property
    def bitrate_bps(self) -> int:
        return self.bitrate_kbps * 1000

    def with_overrides(
        self,
        width: int | None = None,
        height: int | None = None,
        framerate: int | None = None,
        bitrate_kbps: int | None = None,
    ) -> QualityProfile:
        return QualityProfile(
            width=width if width is not None else self.width,
            height=height if height is not None else self.height,
            framerate=framerate if framerate is not None else self.framerate,
            bitrate_kbps=bitrate_kbps if bitrate_kbps is not None else self.bitrate_kbps,
        )
