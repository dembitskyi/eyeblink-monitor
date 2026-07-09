"""D-Bus service exposing blink monitor state and signals.

Properties:
    SecondsSinceLastBlink (d): time since last detected blink.
    CurrentEAR (d): live eye aspect ratio (0 when no face).
    BlinkCount (u): total blinks counted this session.
    FaceDetected (b): whether a face is currently detected.
    WarningThreshold (u): configured warning_seconds.
    WarningActive (b): whether the no-blink warning is currently active.

Signals:
    Blinked(): emitted on each detected blink.
    NoBlinkWarning(seconds: u): emitted once when threshold is crossed.
    BlinkResumed(): emitted on the first blink after a warning was active.
"""

# NOTE: do NOT use `from __future__ import annotations` here — dasbus inspects
# property/signal type hints at decoration time and cannot resolve them when
# they are stringified by PEP 563.

import contextlib
import logging

from dasbus.connection import SessionMessageBus
from dasbus.loop import EventLoop
from dasbus.server.interface import dbus_interface, dbus_signal
from dasbus.typing import Bool, Double, UInt32

log = logging.getLogger("eyeblink")


@dbus_interface("org.eyeblink.Monitor1")
class MonitorInterface:
    def __init__(self, warning_threshold: int) -> None:
        self._seconds_since_last_blink: float = 0.0
        self._current_ear: float = 0.0
        self._blink_count: int = 0
        self._face_detected: bool = False
        self._warning_threshold: int = warning_threshold
        self._warning_active: bool = False

    @property
    def SecondsSinceLastBlink(self) -> Double:
        return self._seconds_since_last_blink

    @property
    def CurrentEAR(self) -> Double:
        return self._current_ear

    @property
    def BlinkCount(self) -> UInt32:
        return self._blink_count

    @property
    def FaceDetected(self) -> Bool:
        return self._face_detected

    @property
    def WarningThreshold(self) -> UInt32:
        return self._warning_threshold

    @property
    def WarningActive(self) -> Bool:
        return self._warning_active

    @dbus_signal
    def Blinked(self) -> None:
        pass

    @dbus_signal
    def NoBlinkWarning(self, seconds: UInt32) -> None:
        pass

    @dbus_signal
    def BlinkResumed(self) -> None:
        pass

    def push_state(
        self,
        seconds_since_last_blink: float,
        current_ear: float,
        blink_count: int,
        face_detected: bool,
        warning_active: bool,
    ) -> None:
        self._seconds_since_last_blink = seconds_since_last_blink
        self._current_ear = current_ear
        self._blink_count = blink_count
        self._face_detected = face_detected
        self._warning_active = warning_active


class DBusService:
    """Owns the GLib event loop on a background thread and exposes the interface.

    Publishes two objects under the same bus name: the legacy read-only monitor
    interface (``org.eyeblink.Monitor1``) and, when a ``RuntimeSettings`` is
    supplied, the generic ``org.os_settings.Configurable1`` tuning interface.
    """

    def __init__(
        self,
        bus_name: str,
        object_path: str,
        warning_threshold: int,
        runtime=None,
        config=None,
    ) -> None:
        self._bus_name = bus_name
        self._object_path = object_path
        self._bus = SessionMessageBus()
        self.interface = MonitorInterface(warning_threshold)
        self.configurable = None
        self._runtime = runtime
        self._configurable_iface = "org.os_settings.Configurable1"
        self._last_status: dict = {}
        if runtime is not None:
            # Imported lazily so a build without the settings module still runs.
            from dbus_settings import CONFIGURABLE_OBJECT_PATH, ConfigurableInterface

            self.configurable = ConfigurableInterface(runtime, config)
            self._configurable_path = CONFIGURABLE_OBJECT_PATH
        self._loop = EventLoop()

    def publish(self) -> None:
        self._bus.publish_object(self._object_path, self.interface)
        if self.configurable is not None:
            self._bus.publish_object(self._configurable_path, self.configurable)
        self._bus.register_service(self._bus_name)
        if self._runtime is not None:
            # Emit a PropertiesChanged as soon as a tunable is written...
            self._runtime.set_notifier(self._on_settings_changed)
            # ...and a coalesced one for live status at ~2 Hz (once a loop runs).
            from gi.repository import GLib

            GLib.timeout_add(500, self._emit_status_tick)

    # ── PropertiesChanged emission ──────────────────────────────────────────
    def _on_settings_changed(self, keys: list) -> None:
        """Notifier target: emit the standard signal for changed tunables.

        Runs on the GLib loop thread (D-Bus dispatches Set there), so it is safe
        to emit on the bus connection directly.
        """
        self._emit_changed(keys)

    def _emit_status_tick(self) -> bool:
        from settings import STATUS_KEYS

        # Live status streaming is opt-in (it changes ~30x/s); the settings
        # PropertiesChanged for tunables is always emitted regardless.
        if not self._runtime.get("status_reporting"):
            return True  # keep polling the flag so it resumes when enabled.
        changed = [
            key for key in STATUS_KEYS if self._last_status.get(key) != self._runtime.get(key)
        ]
        if changed:
            for key in changed:
                self._last_status[key] = self._runtime.get(key)
            self._emit_changed(changed)
        return True  # keep the timer running.

    def _emit_changed(self, keys: list) -> None:
        from gi.repository import GLib

        from settings import DBUS_SIGNATURE, SCHEMA_BY_KEY, _camel

        changed: dict = {}
        for key in keys:
            spec = SCHEMA_BY_KEY.get(key)
            if spec is None:
                continue
            signature = DBUS_SIGNATURE.get(spec.type)
            if signature is None:
                continue
            value = _coerce_for_signature(signature, self._runtime.get(key))
            changed[_camel(key)] = GLib.Variant(signature, value)
        if not changed:
            return
        body = GLib.Variant("(sa{sv}as)", (self._configurable_iface, changed, []))
        try:
            self._bus.connection.emit_signal(
                None,
                self._configurable_path,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                body,
            )
        except Exception:
            log.exception("emit PropertiesChanged")

    def run(self) -> None:
        self._loop.run()

    def quit(self) -> None:
        self._loop.quit()
        with contextlib.suppress(Exception):
            self._bus.disconnect()


def _coerce_for_signature(signature: str, value) -> object:
    if signature == "b":
        return bool(value)
    if signature == "i":
        return int(value)
    if signature == "d":
        return float(value)
    return str(value)
