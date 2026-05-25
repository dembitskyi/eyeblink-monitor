"""Listen for noctalia session Lock/Unlock signals on the session bus.

Noctalia shell emits org.noctalia.Session.Lock and org.noctalia.Session.Unlock
signals on /org/noctalia/Session when the screen is locked/unlocked.
"""

from __future__ import annotations

import logging

log = logging.getLogger("eyeblink")


class SessionLockListener:
    """Monitors org.noctalia.Session Lock/Unlock signals."""

    def __init__(
        self,
        on_lock: callable,
        on_unlock: callable,
    ) -> None:
        self._on_lock = on_lock
        self._on_unlock = on_unlock

    def start(self) -> None:
        """Subscribe to signals. Must be called before the GLib main loop starts."""
        from gi.repository import Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        bus.signal_subscribe(
            None,
            "org.noctalia.Session",
            "Lock",
            "/org/noctalia/Session",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_lock_signal,
            None,
        )

        bus.signal_subscribe(
            None,
            "org.noctalia.Session",
            "Unlock",
            "/org/noctalia/Session",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_unlock_signal,
            None,
        )

        log.info("listening for org.noctalia.Session Lock/Unlock")

    def stop(self) -> None:
        pass

    def _on_lock_signal(self, *_args) -> None:
        log.info("screen locked (noctalia Lock signal)")
        self._on_lock()

    def _on_unlock_signal(self, *_args) -> None:
        log.info("screen unlocked (noctalia Unlock signal)")
        self._on_unlock()
