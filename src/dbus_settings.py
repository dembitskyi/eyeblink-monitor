"""D-Bus ``org.os_settings.Configurable1`` interface for eyeblink-monitor.

This is the generic settings contract os-settings (and any future settings UI)
consumes: a ``Describe()`` method returning a JSON schema of every knob, and the
knobs themselves as standard read-write D-Bus properties (so ``busctl``/``gdbus``
and the standard ``org.freedesktop.DBus.Properties`` interface work too).

Writes are validated/clamped and applied live by ``RuntimeSettings``; read-only
properties mirror the live monitor status for display.
"""

# NOTE: no `from __future__ import annotations` — dasbus inspects property/method
# type hints at decoration time and cannot resolve stringified (PEP 563) hints.

import json

from dasbus.server.interface import dbus_interface
from dasbus.typing import Bool, Double, Int32, Str

from settings import RuntimeSettings

CONFIGURABLE_OBJECT_PATH = "/org/os_settings/eyeblink"
MODULE_ID = "eyeblink"
MODULE_TITLE = "Eye Blink Monitor"
MODULE_ICON = "eye"


@dbus_interface("org.os_settings.Configurable1")
class ConfigurableInterface:
    """Self-describing, live-tunable settings for the blink monitor."""

    def __init__(self, runtime: RuntimeSettings, config) -> None:
        self._rt = runtime
        # Kept so the Reset action can restore the original config defaults.
        self._config = config

    # ── self-description ────────────────────────────────────────────────────
    def Describe(self) -> Str:
        """Return the settings schema as a JSON string (see settings.py)."""
        return json.dumps(
            self._rt.schema_dict(module_id=MODULE_ID, title=MODULE_TITLE, icon=MODULE_ICON)
        )

    def Reset(self) -> None:
        """Action: drop user tweaks and restore the config defaults."""
        self._rt.reset(self._config)

    # ── writable tunables (property name == schema key) ─────────────────────
    @property
    def Paused(self) -> Bool:
        return bool(self._rt.get("paused"))

    @Paused.setter
    def Paused(self, value: Bool) -> None:
        self._rt.set("paused", bool(value))

    @property
    def WarningSeconds(self) -> Int32:
        return int(self._rt.get("warning_seconds"))

    @WarningSeconds.setter
    def WarningSeconds(self, value: Int32) -> None:
        self._rt.set("warning_seconds", int(value))

    @property
    def EarThreshold(self) -> Double:
        return float(self._rt.get("ear_threshold"))

    @EarThreshold.setter
    def EarThreshold(self, value: Double) -> None:
        self._rt.set("ear_threshold", float(value))

    @property
    def ConsecutiveFrames(self) -> Int32:
        return int(self._rt.get("consecutive_frames"))

    @ConsecutiveFrames.setter
    def ConsecutiveFrames(self, value: Int32) -> None:
        self._rt.set("consecutive_frames", int(value))

    @property
    def CameraIndex(self) -> Int32:
        return int(self._rt.get("camera_index"))

    @CameraIndex.setter
    def CameraIndex(self, value: Int32) -> None:
        self._rt.set("camera_index", int(value))

    @property
    def DimEnabled(self) -> Bool:
        return bool(self._rt.get("dim_enabled"))

    @DimEnabled.setter
    def DimEnabled(self, value: Bool) -> None:
        self._rt.set("dim_enabled", bool(value))

    @property
    def Scope(self) -> Str:
        return str(self._rt.get("scope"))

    @Scope.setter
    def Scope(self, value: Str) -> None:
        self._rt.set("scope", str(value))

    @property
    def TargetDim(self) -> Double:
        return float(self._rt.get("target_dim"))

    @TargetDim.setter
    def TargetDim(self, value: Double) -> None:
        self._rt.set("target_dim", float(value))

    @property
    def FadeMs(self) -> Int32:
        return int(self._rt.get("fade_ms"))

    @FadeMs.setter
    def FadeMs(self, value: Int32) -> None:
        self._rt.set("fade_ms", int(value))

    @property
    def EscalationSeconds(self) -> Int32:
        return int(self._rt.get("escalation_seconds"))

    @EscalationSeconds.setter
    def EscalationSeconds(self, value: Int32) -> None:
        self._rt.set("escalation_seconds", int(value))

    @property
    def EscalationDim(self) -> Double:
        return float(self._rt.get("escalation_dim"))

    @EscalationDim.setter
    def EscalationDim(self, value: Double) -> None:
        self._rt.set("escalation_dim", float(value))

    @property
    def StatusReporting(self) -> Bool:
        return bool(self._rt.get("status_reporting"))

    @StatusReporting.setter
    def StatusReporting(self, value: Bool) -> None:
        self._rt.set("status_reporting", bool(value))

    # ── read-only live status ───────────────────────────────────────────────
    @property
    def FaceDetected(self) -> Bool:
        return bool(self._rt.get("face_detected"))

    @property
    def CurrentEar(self) -> Double:
        return float(self._rt.get("current_ear"))

    @property
    def BlinkCount(self) -> Int32:
        return int(self._rt.get("blink_count"))

    @property
    def SecondsSinceLastBlink(self) -> Double:
        return float(self._rt.get("seconds_since_last_blink"))

    @property
    def WarningActive(self) -> Bool:
        return bool(self._rt.get("warning_active"))
