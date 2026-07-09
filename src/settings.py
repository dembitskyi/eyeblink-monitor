"""Live-tunable runtime settings + the self-describing schema.

This module is the single source of truth for every knob eyeblink-monitor
exposes over D-Bus (via ``org.os_settings.Configurable1``). ``SETTINGS_SCHEMA``
describes each setting richly enough for a generic settings UI to render it
(type, range, step, unit, choices, description), and ``RuntimeSettings`` holds
the current values behind a lock so the capture thread can read them live while
the D-Bus thread mutates them.

User tweaks are layered over the config defaults and persisted to
``$XDG_STATE_HOME/eyeblink-monitor/prefs.json`` so they survive a restart even
when ``config.toml`` is managed read-only by Nix.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger("eyeblink.settings")


@dataclass(frozen=True)
class SettingSpec:
    """Rich description of one tunable, consumed by the settings UI."""

    key: str
    label: str
    type: str  # bool | int | double | enum | string | readonly.
    group: str
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    choices: tuple[str, ...] = ()
    readonly: bool = False

    def coerce(self, value: Any) -> Any:
        """Validate and normalise ``value`` for this setting.

        Raises ValueError on an out-of-domain value (enum / wrong type); numbers
        are clamped to ``[minimum, maximum]``.
        """
        if self.readonly:
            raise ValueError(f"{self.key} is read-only")
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.key} expects a bool")
            return value
        if self.type == "enum":
            text = str(value)
            if text not in self.choices:
                raise ValueError(f"{self.key} must be one of {self.choices}")
            return text
        if self.type == "string":
            return str(value)
        if self.type in ("int", "double"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.key} expects a number")
            num = float(value)
            if self.minimum is not None:
                num = max(self.minimum, num)
            if self.maximum is not None:
                num = min(self.maximum, num)
            return round(num) if self.type == "int" else num
        raise ValueError(f"{self.key} has non-writable type {self.type}")


# ── the schema: the authoritative list of every exposed knob ────────────────
# Order here is the order the UI renders (grouped by ``group``).
SETTINGS_SCHEMA: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="paused",
        label="Pause monitoring",
        type="bool",
        group="Monitoring",
        description="Release the camera and stop blink detection until resumed.",
    ),
    SettingSpec(
        key="warning_seconds",
        label="No-blink period",
        type="int",
        group="Blink detection",
        description="Seconds without a blink before the screen starts dimming.",
        minimum=1,
        maximum=120,
        step=1,
        unit="s",
    ),
    SettingSpec(
        key="ear_threshold",
        label="EAR threshold",
        type="double",
        group="Blink detection",
        description="Eye-aspect-ratio below which an eye counts as closed. "
        "Lower is less sensitive.",
        minimum=0.05,
        maximum=0.5,
        step=0.01,
    ),
    SettingSpec(
        key="consecutive_frames",
        label="Consecutive frames",
        type="int",
        group="Blink detection",
        description="Frames below the threshold required to register a blink "
        "(filters single-frame noise).",
        minimum=1,
        maximum=10,
        step=1,
        unit="frames",
    ),
    SettingSpec(
        key="camera_index",
        label="Camera index",
        type="int",
        group="Blink detection",
        description="Webcam device index. Changing this reopens the camera.",
        minimum=0,
        maximum=8,
        step=1,
    ),
    SettingSpec(
        key="dim_enabled",
        label="Enable dimming",
        type="bool",
        group="Screen dimming",
        description="Master switch for the screen-dimming nudge.",
    ),
    SettingSpec(
        key="scope",
        label="Dim scope",
        type="enum",
        group="Screen dimming",
        description="Dim every monitor or only the currently focused window.",
        choices=("all", "focused"),
    ),
    SettingSpec(
        key="target_dim",
        label="Dim level",
        type="double",
        group="Screen dimming",
        description="Peak opacity of the dimming overlay (0 = none, 1 = black).",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
    ),
    SettingSpec(
        key="fade_ms",
        label="Fade duration",
        type="int",
        group="Screen dimming",
        description="Fade in/out duration for the dimming overlay.",
        minimum=50,
        maximum=3000,
        step=50,
        unit="ms",
    ),
    SettingSpec(
        key="escalation_seconds",
        label="Escalation delay",
        type="int",
        group="Screen dimming",
        description="Seconds without a blink at which the dim ramps up to the escalation level.",
        minimum=1,
        maximum=300,
        step=1,
        unit="s",
    ),
    SettingSpec(
        key="escalation_dim",
        label="Escalation level",
        type="double",
        group="Screen dimming",
        description="Overlay opacity after the escalation delay elapses.",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
    ),
    # ── read-only live status (rendered as indicators, not editable) ────────
    SettingSpec(
        key="status_reporting",
        label="Live status updates",
        type="bool",
        group="Status",
        description="Stream live status (EAR, blink count, …) as change events. "
        "Off by default to avoid frequent D-Bus traffic.",
    ),
    SettingSpec(
        key="face_detected",
        label="Face detected",
        type="bool",
        group="Status",
        readonly=True,
    ),
    SettingSpec(
        key="current_ear",
        label="Current EAR",
        type="double",
        group="Status",
        readonly=True,
    ),
    SettingSpec(
        key="blink_count",
        label="Blinks this session",
        type="int",
        group="Status",
        readonly=True,
    ),
    SettingSpec(
        key="seconds_since_last_blink",
        label="Since last blink",
        type="double",
        group="Status",
        unit="s",
        readonly=True,
    ),
    SettingSpec(
        key="warning_active",
        label="Warning active",
        type="bool",
        group="Status",
        readonly=True,
    ),
)

SCHEMA_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS_SCHEMA}

# Keys that a user can write (everything except the read-only status block).
WRITABLE_KEYS: tuple[str, ...] = tuple(s.key for s in SETTINGS_SCHEMA if not s.readonly)

# Read-only live-status keys (emitted as coalesced PropertiesChanged at ~2 Hz).
STATUS_KEYS: tuple[str, ...] = tuple(s.key for s in SETTINGS_SCHEMA if s.readonly)

# Setting type -> D-Bus signature, used to build PropertiesChanged variants.
DBUS_SIGNATURE: dict[str, str] = {
    "bool": "b",
    "int": "i",
    "double": "d",
    "enum": "s",
    "string": "s",
}


def _camel(snake: str) -> str:
    """snake_case internal key -> CamelCase D-Bus property name.

    dasbus only exports CamelCase member names, so the D-Bus property (and the
    schema ``key`` os-settings uses to read/write it) is the CamelCase form,
    while the daemon keeps readable snake_case keys internally.
    """
    return "".join(part.capitalize() for part in snake.split("_"))


def state_prefs_path() -> Path:
    """Writable overlay for user tweaks (XDG state; survives read-only config)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "eyeblink-monitor" / "prefs.json"


@dataclass
class RuntimeSettings:
    """Thread-safe live settings, seeded from config and persisted on change.

    The capture thread reads values every frame; the D-Bus thread writes them.
    Writers bump ``camera_generation`` / ``nudge_generation`` so the capture
    loop knows when to reopen the camera or rebuild the dimming controller.
    """

    _values: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _prefs_path: Path = field(default_factory=state_prefs_path)
    # Live read-only status, pushed by the capture loop.
    _status: dict[str, Any] = field(default_factory=dict)
    # Called (with the changed keys) after a write, so the D-Bus layer can emit
    # PropertiesChanged. Set via ``set_notifier``.
    _notifier: Any = None

    camera_generation: int = 0
    nudge_generation: int = 0

    def set_notifier(self, callback: Any) -> None:
        """Register a ``callback(changed_keys: list[str])`` fired after writes."""
        self._notifier = callback

    def _notify(self, keys: list[str]) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(keys)
        except Exception:
            log.exception("settings change notifier")

    @classmethod
    def from_config(cls, cfg: Config, prefs_path: Path | None = None) -> RuntimeSettings:
        """Build defaults from ``cfg``, then layer persisted user tweaks on top."""
        rt = cls()
        if prefs_path is not None:
            rt._prefs_path = prefs_path
        rt._values = {
            "paused": False,
            "warning_seconds": int(cfg.alert.warning_seconds),
            "ear_threshold": float(cfg.detection.ear_threshold),
            "consecutive_frames": int(cfg.detection.consecutive_frames),
            "camera_index": int(cfg.detection.camera_index),
            "dim_enabled": True,
            "scope": cfg.nudge.scope,
            "target_dim": float(cfg.nudge.target_dim),
            "fade_ms": int(cfg.nudge.fade_ms),
            "escalation_seconds": _first_escalation_seconds(cfg),
            "escalation_dim": _first_escalation_dim(cfg),
            "status_reporting": False,
        }
        rt._status = {
            "face_detected": False,
            "current_ear": 0.0,
            "blink_count": 0,
            "seconds_since_last_blink": 0.0,
            "warning_active": False,
        }
        rt._load_overlay()
        return rt

    # ── reads (capture thread) ──────────────────────────────────────────────
    def get(self, key: str) -> Any:
        with self._lock:
            if key in self._values:
                return self._values[key]
            return self._status.get(key)

    def snapshot(self) -> dict[str, Any]:
        """All values + live status (what D-Bus GetAll returns)."""
        with self._lock:
            return {**self._values, **self._status}

    # ── writes (D-Bus thread) ───────────────────────────────────────────────
    def set(self, key: str, value: Any) -> Any:
        """Validate, clamp, store and persist ``key``; returns the stored value.

        Bumps generation counters for changes the capture loop must act on.
        Raises ValueError for unknown or read-only keys / bad values.
        """
        spec = SCHEMA_BY_KEY.get(key)
        if spec is None or spec.readonly:
            raise ValueError(f"unknown or read-only setting: {key}")
        coerced = spec.coerce(value)
        with self._lock:
            previous = self._values.get(key)
            self._values[key] = coerced
            if key == "camera_index" and coerced != previous:
                self.camera_generation += 1
            if key == "scope" and coerced != previous:
                self.nudge_generation += 1
        log.info("setting %s = %r", key, coerced)
        self._save_overlay()
        self._notify([key])
        return coerced

    def override(self, key: str, value: Any) -> None:
        """Set a value for this run only, without persisting (CLI flag overrides)."""
        spec = SCHEMA_BY_KEY.get(key)
        if spec is None or spec.readonly:
            return
        with self._lock:
            self._values[key] = spec.coerce(value)
        self._notify([key])

    def reset(self, cfg: Config) -> None:
        """Drop all persisted tweaks, restoring the config defaults."""
        fresh = RuntimeSettings.from_config_defaults_only(cfg)
        with self._lock:
            for key in WRITABLE_KEYS:
                if key in fresh:
                    self._values[key] = fresh[key]
            self.camera_generation += 1
            self.nudge_generation += 1
        try:
            self._prefs_path.unlink(missing_ok=True)
        except OSError:
            log.exception("prefs delete")
        log.info("settings reset to config defaults")
        self._notify(list(WRITABLE_KEYS))

    @staticmethod
    def from_config_defaults_only(cfg: Config) -> dict[str, Any]:
        return {
            "paused": False,
            "warning_seconds": int(cfg.alert.warning_seconds),
            "ear_threshold": float(cfg.detection.ear_threshold),
            "consecutive_frames": int(cfg.detection.consecutive_frames),
            "camera_index": int(cfg.detection.camera_index),
            "dim_enabled": True,
            "scope": cfg.nudge.scope,
            "target_dim": float(cfg.nudge.target_dim),
            "fade_ms": int(cfg.nudge.fade_ms),
            "escalation_seconds": _first_escalation_seconds(cfg),
            "escalation_dim": _first_escalation_dim(cfg),
            "status_reporting": False,
        }

    # ── live status (capture thread pushes; D-Bus thread reads) ─────────────
    def push_status(self, **status: Any) -> None:
        with self._lock:
            self._status.update(status)

    # ── schema for Describe() ───────────────────────────────────────────────
    def schema_dict(self, *, module_id: str, title: str, icon: str) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for spec in SETTINGS_SCHEMA:
            group = index.get(spec.group)
            if group is None:
                group = {"title": spec.group, "settings": []}
                index[spec.group] = group
                groups.append(group)
            entry: dict[str, Any] = {
                "key": _camel(spec.key),
                "label": spec.label,
                "type": spec.type,
                "readonly": spec.readonly,
            }
            if spec.description:
                entry["description"] = spec.description
            if spec.minimum is not None:
                entry["min"] = spec.minimum
            if spec.maximum is not None:
                entry["max"] = spec.maximum
            if spec.step is not None:
                entry["step"] = spec.step
            if spec.unit:
                entry["unit"] = spec.unit
            if spec.choices:
                entry["choices"] = list(spec.choices)
            group["settings"].append(entry)
        return {
            "id": module_id,
            "title": title,
            "icon": icon,
            "groups": groups,
            "actions": [{"key": "Reset", "label": "Reset to defaults"}],
        }

    # ── persistence ─────────────────────────────────────────────────────────
    def _load_overlay(self) -> None:
        try:
            data = json.loads(self._prefs_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            spec = SCHEMA_BY_KEY.get(key)
            if spec is None or spec.readonly:
                continue
            try:
                self._values[key] = spec.coerce(value)
            except ValueError:
                log.warning("ignoring invalid persisted setting %s=%r", key, value)

    def _save_overlay(self) -> None:
        try:
            self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {k: self._values[k] for k in WRITABLE_KEYS if k in self._values}
            self._prefs_path.write_text(json.dumps(data, indent=2))
        except OSError:
            log.exception("prefs save")


def _first_escalation_seconds(cfg: Config) -> int:
    if cfg.nudge.escalation:
        return int(cfg.nudge.escalation[0][0])
    return int(cfg.alert.warning_seconds) + 13


def _first_escalation_dim(cfg: Config) -> float:
    if cfg.nudge.escalation:
        return float(cfg.nudge.escalation[0][1])
    return 0.80
