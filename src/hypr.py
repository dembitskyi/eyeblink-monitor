"""Hyprland IPC helpers: query active window geometry and subscribe to events.

Uses the two sockets exposed by Hyprland under
$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/:
- .socket.sock  : request/response (e.g. `dispatch`, `j/activewindow`).
- .socket2.sock : newline-delimited stream of events.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


def _hypr_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not runtime or not sig:
        raise RuntimeError(
            "HYPRLAND_INSTANCE_SIGNATURE / XDG_RUNTIME_DIR not set — "
            "are you running under Hyprland?"
        )
    return Path(runtime) / "hypr" / sig


def _request(payload: str) -> bytes:
    sock_path = _hypr_dir() / ".socket.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall(payload.encode())
        chunks: list[bytes] = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def keyword(key: str, value: str) -> None:
    """Set a Hyprland config keyword at runtime."""
    _request(f"/keyword {key} {value}")


def setprop(address: str, prop: str, value: str) -> None:
    """Set a property on a window by address."""
    _request(f"/setprop address:{address} {prop} {value}")


def get_active_address() -> str | None:
    """Return the address of the currently focused window."""
    raw = _request("j/activewindow")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data.get("address")


@dataclass
class WindowGeometry:
    x: int
    y: int
    width: int
    height: int


def get_active_window() -> WindowGeometry | None:
    raw = _request("j/activewindow")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    at = data.get("at")
    size = data.get("size")
    if not at or not size:
        return None
    return WindowGeometry(x=at[0], y=at[1], width=size[0], height=size[1])


class HyprEventListener:
    """Subscribes to Hyprland's .socket2.sock and dispatches events."""

    # Events that may change the focused window's geometry/identity.
    _FOCUS_EVENTS: ClassVar[set[str]] = {
        "activewindow",
        "activewindowv2",
        "movewindow",
        "movewindowv2",
        "resizewindow",
        "openwindow",
        "closewindow",
        "workspace",
        "focusedmon",
        "monitoraddedv2",
        "monitorremoved",
        "configreloaded",
    }

    def __init__(
        self,
        on_change: Callable[[], None] | None = None,
        on_lock: Callable[[], None] | None = None,
        on_unlock: Callable[[], None] | None = None,
    ) -> None:
        self._on_change = on_change
        self._on_lock = on_lock
        self._on_unlock = on_unlock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hypr-events", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        sock_path = _hypr_dir() / ".socket2.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(str(sock_path))
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = s.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        name = line.split(b">>", 1)[0].decode(errors="ignore")
                        self._dispatch(name)
        except Exception:
            pass

    def _dispatch(self, event: str) -> None:
        try:
            if event in self._FOCUS_EVENTS and self._on_change:
                self._on_change()
            elif event == "lockscreen" and self._on_lock:
                self._on_lock()
            elif event == "unlockscreen" and self._on_unlock:
                self._on_unlock()
        except Exception:
            pass
