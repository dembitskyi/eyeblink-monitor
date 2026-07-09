"""Screen dimming via native Hyprland mechanisms (socket IPC).

Two modes:
- "all":     applies a static GLSL dimming shader via decoration:screen_shader.
             Fade is done with ~6 discrete steps over fade_ms (each step is a
             new static shader applied via socket).
- "focused": sets the alpha property on the currently focused window (animated
             via GLib timer since setprop is lightweight over socket).

No subprocesses, no GTK, no layer-shell, no overlay windows.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from gi.repository import GLib

from hypr import HyprEventListener, get_active_address, keyword, setprop

_FADE_STEPS = 6

# Static GLSL shader — no `time` uniform, no damage tracking issues.
_DIM_SHADER_TEMPLATE = """\
#version 300 es

precision highp float;
in vec2 v_texcoord;
uniform sampler2D tex;

layout(location = 0) out vec4 fragColor;

void main() {{
    vec4 color = texture(tex, v_texcoord);
    fragColor = vec4(color.rgb * {factor:.4f}, color.a);
}}
"""


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


class NudgeController:
    """Dims the screen via Hyprland socket IPC when activated."""

    def __init__(self, scope: str, target_dim: float, fade_ms: int) -> None:
        if scope not in ("all", "focused"):
            raise ValueError(f"unknown nudge scope: {scope}")
        self._scope = scope
        self._target = max(0.0, min(1.0, target_dim))
        self._fade_ms = max(1, fade_ms)

        self._shader_path: Path | None = None
        self._shader_active = False
        self._active = False
        self._current_level = 0.0

        # Stepped fade state (all mode).
        self._fade_timer: int | None = None
        self._fade_from = 0.0
        self._fade_to = 0.0
        self._fade_step = 0
        self._fade_generation = 0

        # Animation state (focused mode).
        self._anim_source: int | None = None
        self._anim_start_ms: int = 0
        self._anim_from = 0.0
        self._anim_to = 0.0

        # For focused mode: track which window address we dimmed.
        self._dimmed_address: str | None = None
        self._hypr_listener: HyprEventListener | None = None
        self._lock = threading.Lock()

        if self._scope == "all":
            self._shader_path = Path(tempfile.mkdtemp()) / "eyeblink-dim.frag"
        elif self._scope == "focused":
            self._hypr_listener = HyprEventListener(lambda: GLib.idle_add(self._on_focus_change))
            self._hypr_listener.start()

    def update_params(self, target_dim: float, fade_ms: int) -> None:
        """Apply new dim level / fade duration live (used by the tuning UI)."""
        self._target = max(0.0, min(1.0, target_dim))
        self._fade_ms = max(1, fade_ms)

    def activate(self, level: float | None = None) -> None:
        target = max(0.0, min(1.0, level)) if level is not None else self._target
        if self._scope == "all":
            GLib.idle_add(self._request_stepped_fade, target)
        else:
            GLib.idle_add(self._start_fade, target)

    def deactivate(self) -> None:
        if self._scope == "all":
            GLib.idle_add(self._request_stepped_fade, 0.0)
        else:
            if self._current_level < 0.001 and self._fade_timer is None:
                return
            GLib.idle_add(self._start_fade, 0.0)

    def cleanup(self) -> None:
        if self._hypr_listener is not None:
            self._hypr_listener.stop()
        if self._fade_timer is not None:
            GLib.source_remove(self._fade_timer)
            self._fade_timer = None
        if self._scope == "all":
            self._apply_shader(0.0)
        else:
            self._apply_focused_alpha(0.0)
        if self._shader_path is not None and self._shader_path.exists():
            self._shader_path.unlink(missing_ok=True)

    # -------------------------------------------------- all mode (stepped fade)

    def _request_stepped_fade(self, to: float) -> bool:
        if abs(self._fade_to - to) < 0.001 and self._fade_timer is not None:
            return False
        if (
            to < 0.001
            and self._current_level < 0.001
            and self._fade_timer is None
            and not self._shader_active
        ):
            return False
        self._start_stepped_fade(to)
        return False

    def _start_stepped_fade(self, to: float) -> None:
        if self._fade_timer is not None:
            GLib.source_remove(self._fade_timer)
            self._fade_timer = None

        self._fade_generation += 1
        self._fade_from = self._current_level
        self._fade_to = to
        self._fade_step = 0
        self._active = True
        self._stepped_tick(self._fade_generation)

    def _stepped_tick(self, generation: int) -> None:
        if generation != self._fade_generation:
            return

        self._fade_step += 1
        t = min(1.0, self._fade_step / _FADE_STEPS)
        eased = _smoothstep(t)
        level = self._fade_from + (self._fade_to - self._fade_from) * eased
        self._apply_shader(level)
        self._current_level = level

        if self._fade_step >= _FADE_STEPS:
            self._fade_timer = None
            self._active = level > 0.001
            if level < 0.001:
                self._apply_shader(0.0)
            return

        interval_ms = max(1, self._fade_ms // _FADE_STEPS)
        self._fade_timer = GLib.timeout_add(interval_ms, self._stepped_tick_cb, generation)

    def _stepped_tick_cb(self, generation: int) -> bool:
        self._stepped_tick(generation)
        return False

    def _apply_shader(self, level: float) -> None:
        if level < 0.001:
            if not self._shader_active:
                return
            keyword("decoration:screen_shader", "[[EMPTY]]")
            self._shader_active = False
            return
        factor = 1.0 - level
        shader_src = _DIM_SHADER_TEMPLATE.format(factor=factor)
        self._write_shader_atomic(shader_src)
        keyword("decoration:screen_shader", str(self._shader_path))
        self._shader_active = True

    def _write_shader_atomic(self, shader_src: str) -> None:
        # Only reached in "all" scope, where _shader_path is always set.
        assert self._shader_path is not None
        tmp_path = self._shader_path.with_name(self._shader_path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(shader_src)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(self._shader_path)

    # ----------------------------------------------------- focused mode (animated)

    def _start_fade(self, to: float) -> bool:
        self._anim_from = self._current_level
        self._anim_to = to
        self._anim_start_ms = GLib.get_monotonic_time() // 1000
        if self._anim_source is None:
            self._anim_source = GLib.timeout_add(16, self._tick)
        return False

    def _tick(self) -> bool:
        now_ms = GLib.get_monotonic_time() // 1000
        elapsed = now_ms - self._anim_start_ms
        t = min(1.0, elapsed / self._fade_ms)
        eased = _smoothstep(t)
        value = self._anim_from + (self._anim_to - self._anim_from) * eased
        self._apply_focused_alpha(value)
        if t >= 1.0:
            self._anim_source = None
            return False
        return True

    def _apply_focused_alpha(self, level: float) -> None:
        self._current_level = level
        self._active = level > 0.001

        with self._lock:
            addr = get_active_address()
            if self._dimmed_address and self._dimmed_address != addr:
                setprop(self._dimmed_address, "alpha", "1.0")
                self._dimmed_address = None

            if level < 0.001:
                if self._dimmed_address:
                    setprop(self._dimmed_address, "alpha", "1.0")
                    self._dimmed_address = None
                return

            if addr:
                alpha = 1.0 - level
                setprop(addr, "alpha", f"{alpha:.3f}")
                self._dimmed_address = addr

    def _on_focus_change(self) -> bool:
        if self._active:
            self._apply_focused_alpha(self._current_level)
        return False
