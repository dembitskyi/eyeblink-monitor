"""Configuration loading with sensible defaults and XDG support."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DetectionConfig:
    ear_threshold: float = 0.21
    consecutive_frames: int = 2
    camera_index: int = 0
    fps: int = 30
    # Try the MediaPipe GPU delegate first; fall back to CPU if unavailable.
    prefer_gpu: bool = True


@dataclass
class AlertConfig:
    warning_seconds: int = 5


@dataclass
class DisplayConfig:
    show_preview: bool = False


@dataclass
class DBusConfig:
    bus_name: str = "org.eyeblink.Monitor"
    object_path: str = "/org/eyeblink/Monitor"


@dataclass
class NudgeConfig:
    # "all"     -> fullscreen overlay across every monitor.
    # "focused" -> overlay tracks the currently focused Hyprland window.
    scope: str = "all"
    target_dim: float = 0.35
    fade_ms: int = 800
    # Escalation: list of [seconds, dim_level] pairs. When elapsed time exceeds
    # the threshold, dim ramps to the corresponding level.
    escalation: list = field(default_factory=lambda: [[18, 0.80]])


@dataclass
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    dbus: DBusConfig = field(default_factory=DBusConfig)
    nudge: NudgeConfig = field(default_factory=NudgeConfig)


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "eyeblink-monitor" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    target = path or default_config_path()
    cfg = Config()
    if not target.exists():
        return cfg

    with target.open("rb") as fh:
        data = tomllib.load(fh)

    for section_name, section_obj in (
        ("detection", cfg.detection),
        ("alert", cfg.alert),
        ("display", cfg.display),
        ("dbus", cfg.dbus),
        ("nudge", cfg.nudge),
    ):
        section_data = data.get(section_name, {})
        for key, value in section_data.items():
            if hasattr(section_obj, key):
                setattr(section_obj, key, value)
    return cfg
