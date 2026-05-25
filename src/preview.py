"""Native Wayland (GTK4) preview window for debug/tuning.

Frames produced by the capture thread are pushed into a thread-safe slot;
the GTK side polls the slot via a GLib timer at ~30 Hz and redraws.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402


@dataclass
class PreviewFrame:
    rgb: bytes
    width: int
    height: int
    stride: int


class PreviewWindow:
    """A minimal GTK4 window that displays the latest webcam frame."""

    def __init__(self, on_close=None) -> None:
        self._on_close = on_close
        self._latest: PreviewFrame | None = None
        self._lock = threading.Lock()

        self.app = Gtk.Application(application_id="org.eyeblink.MonitorPreview")
        self.app.connect("activate", self._on_activate)

    def push_frame(self, rgb_bytes: bytes, width: int, height: int) -> None:
        # Called from the capture thread.
        with self._lock:
            self._latest = PreviewFrame(rgb_bytes, width, height, width * 3)

    def _on_activate(self, app: Gtk.Application) -> None:
        window = Gtk.ApplicationWindow(application=app, title="eyeblink-monitor")
        window.set_default_size(640, 480)

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        window.set_child(self._picture)

        window.connect("close-request", self._on_close_request)
        window.present()

        # Drive UI refresh at ~30 Hz. Capture thread updates _latest independently.
        GLib.timeout_add(33, self._tick)

    def _on_close_request(self, _window) -> bool:
        if self._on_close is not None:
            self._on_close()
        return False

    def _tick(self) -> bool:
        with self._lock:
            frame = self._latest
            self._latest = None
        if frame is not None:
            pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(frame.rgb),
                GdkPixbuf.Colorspace.RGB,
                False,
                8,
                frame.width,
                frame.height,
                frame.stride,
            )
            self._picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        return True  # Keep the timer alive.

    def run(self) -> int:
        return self.app.run(None)

    def quit(self) -> None:
        GLib.idle_add(self.app.quit)
