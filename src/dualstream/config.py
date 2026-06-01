"""Configuration loader.

TOML file -> pydantic settings model. Phase 1 surface is intentionally
narrow; Phase 2 will add [battery], [schedule], and expand [power].
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from dualstream.quality import QualityProfile


DEFAULT_CONFIG_PATHS = (
    Path("/etc/dualstream/dualstream.toml"),
    Path("config/dualstream.toml"),
    Path(__file__).resolve().parent.parent.parent / "config" / "dualstream.toml",
)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    auth_token: str = "change-me"


class CameraConfig(BaseModel):
    resolution: tuple[int, int] = (1280, 720)
    framerate: int = Field(default=15, ge=1, le=60)
    bitrate_kbps: int = Field(default=1000, ge=100, le=10_000)
    controls: dict[str, Any] = Field(default_factory=dict)

    @field_validator("resolution", mode="before")
    @classmethod
    def _coerce_resolution(cls, v: Any) -> tuple[int, int]:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return (int(v[0]), int(v[1]))
        raise ValueError("resolution must be [width, height]")

    def to_quality(self) -> QualityProfile:
        return QualityProfile(
            width=self.resolution[0],
            height=self.resolution[1],
            framerate=self.framerate,
            bitrate_kbps=self.bitrate_kbps,
        )


class SnapshotsConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=10)
    retention_days: int = Field(default=7, ge=1)
    path: Path = Path("/var/lib/dualstream/snapshots")
    jpeg_quality: int = Field(default=75, ge=1, le=95)


class PowerConfig(BaseModel):
    """Phase 1 power settings. Phase 2 expands this block significantly."""

    disable_act_led: bool = True
    idle_grace_seconds: int = Field(default=10, ge=0)


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    camera0: CameraConfig = Field(default_factory=CameraConfig)
    camera1: CameraConfig = Field(default_factory=CameraConfig)
    snapshots: SnapshotsConfig = Field(default_factory=SnapshotsConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)

    @classmethod
    def from_toml(cls, path: Path) -> AppConfig:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.model_validate(data)


def _candidate_paths(explicit: Path | None) -> list[Path]:
    if explicit:
        return [explicit]
    env = os.environ.get("DUALSTREAM_CONFIG")
    paths: list[Path] = [Path(env)] if env else []
    paths.extend(DEFAULT_CONFIG_PATHS)
    return paths


def load_config(explicit_path: Path | None = None) -> AppConfig:
    """Load config from the first matching path on disk, or defaults."""

    for candidate in _candidate_paths(explicit_path):
        if candidate.is_file():
            return AppConfig.from_toml(candidate)
    return AppConfig()
