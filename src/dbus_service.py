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

from dasbus.connection import SessionMessageBus
from dasbus.loop import EventLoop
from dasbus.server.interface import dbus_interface, dbus_signal
from dasbus.typing import Bool, Double, UInt32


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
    """Owns the GLib event loop on a background thread and exposes the interface."""

    def __init__(self, bus_name: str, object_path: str, warning_threshold: int) -> None:
        self._bus_name = bus_name
        self._object_path = object_path
        self._bus = SessionMessageBus()
        self.interface = MonitorInterface(warning_threshold)
        self._loop = EventLoop()

    def publish(self) -> None:
        self._bus.publish_object(self._object_path, self.interface)
        self._bus.register_service(self._bus_name)

    def run(self) -> None:
        self._loop.run()

    def quit(self) -> None:
        self._loop.quit()
        try:
            self._bus.disconnect()
        except Exception:
            pass
