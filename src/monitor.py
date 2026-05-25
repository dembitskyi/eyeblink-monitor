"""Blink state machine and no-blink timer."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class MonitorState:
    ear: float = 0.0
    face_detected: bool = False
    blink_count: int = 0
    seconds_since_last_blink: float = 0.0
    warning_active: bool = False


class BlinkMonitor:
    """Tracks EAR over time, counts blinks, and tracks time since the last blink."""

    def __init__(
        self,
        ear_threshold: float,
        consecutive_frames: int,
        warning_seconds: int,
    ) -> None:
        self._threshold = ear_threshold
        self._required_frames = max(1, consecutive_frames)
        self._warning_seconds = warning_seconds

        self._closed_frames = 0
        self._last_blink_ts = time.monotonic()
        self._warning_active = False

        self.state = MonitorState()

    def update(self, face_detected: bool, ear: float) -> tuple[bool, bool, bool]:
        """Feed a new frame.

        Returns a tuple of (blinked, warning_started, warning_cleared) edge events.
        """
        now = time.monotonic()
        blinked = False
        warning_started = False
        warning_cleared = False

        if face_detected and ear < self._threshold:
            self._closed_frames += 1
        else:
            if self._closed_frames >= self._required_frames:
                blinked = True
                self._last_blink_ts = now
                if self._warning_active:
                    self._warning_active = False
                    warning_cleared = True
                self.state.blink_count += 1
            self._closed_frames = 0

        elapsed = now - self._last_blink_ts
        if not self._warning_active and elapsed >= self._warning_seconds:
            self._warning_active = True
            warning_started = True

        self.state.ear = ear
        self.state.face_detected = face_detected
        self.state.seconds_since_last_blink = elapsed
        self.state.warning_active = self._warning_active

        return blinked, warning_started, warning_cleared

    def reset_timer(self) -> None:
        self._last_blink_ts = time.monotonic()
        self._warning_active = False
