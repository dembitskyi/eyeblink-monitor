"""Blink state machine and no-blink timer."""

from __future__ import annotations

import time
from dataclasses import dataclass

# No face for this long: process one frame in IDLE_SKIP + 1.
IDLE_AFTER_S = 3.0
IDLE_SKIP = 5
# Adaptive rate: every frame once EAR drops below threshold x NEAR_BLINK.
NEAR_BLINK = 1.2


def frames_to_skip(
    face_detected: bool, ear: float, threshold: float, adaptive: bool, faceless_s: float
) -> int:
    """Camera frames to dequeue without processing after this one."""
    if not face_detected:
        return IDLE_SKIP if faceless_s >= IDLE_AFTER_S else 0
    if adaptive and ear >= threshold * NEAR_BLINK:
        return 1
    return 0


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
        self.configure(ear_threshold, consecutive_frames, warning_seconds)

        self._closed_frames = 0
        self._last_blink_ts = time.monotonic()
        self._warning_active = False

        self.state = MonitorState()

    def configure(
        self, ear_threshold: float, consecutive_frames: int, warning_seconds: int
    ) -> None:
        self._threshold = ear_threshold
        self._required_frames = max(1, int(consecutive_frames))
        self._warning_seconds = warning_seconds

    def update(self, face_detected: bool, ear: float) -> tuple[bool, bool, bool]:
        """Feed a new frame.

        Returns a tuple of (blinked, warning_started, warning_cleared) edge events.
        """
        now = time.monotonic()
        blinked = False
        warning_started = False
        warning_cleared = False

        # No face → reset timer so we don't dim when nobody is present.
        if not face_detected:
            self._closed_frames = 0
            self._last_blink_ts = now
            if self._warning_active:
                self._warning_active = False
                warning_cleared = True
            self.state.ear = ear
            self.state.face_detected = False
            self.state.seconds_since_last_blink = 0.0
            self.state.warning_active = False
            return blinked, warning_started, warning_cleared

        if ear < self._threshold:
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
