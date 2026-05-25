"""CLI entry point: capture loop, blink detection, screen dimming, D-Bus.

Threading model:
- Capture + detection runs on a background thread.
- GLib main loop runs on the main thread (services D-Bus + nudge animation
  timers + optional preview window).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from config import Config, load_config
from dbus_service import DBusService
from detector import EyeDetector
from hypr import HyprEventListener
from monitor import BlinkMonitor
from nudge import NudgeController
from session import SessionLockListener

log = logging.getLogger("eyeblink")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eyeblink-monitor",
        description=(
            "Webcam-based blink detector with screen dimming. Tracks your eyes "
            "via MediaPipe and dims the screen when you stop blinking to provoke "
            "a blink reflex."
        ),
    )
    p.add_argument("--config", type=Path, default=None, help="Path to TOML config file.")
    p.add_argument(
        "--show-preview",
        action="store_true",
        help="Show a native Wayland (GTK4) preview window with live EAR and eye landmarks.",
    )
    p.add_argument(
        "--no-dim",
        action="store_true",
        help="Disable screen dimming (D-Bus signals are still emitted).",
    )
    p.add_argument(
        "--scope",
        choices=("all", "focused"),
        default=None,
        help='What to dim: "all" = every monitor (default), "focused" = active window only.',
    )
    p.add_argument(
        "--camera",
        type=int,
        default=None,
        help="Camera device index (default: 0).",
    )
    p.add_argument(
        "--warning-seconds",
        type=int,
        default=None,
        help="Seconds without a blink before dimming starts (default: 5).",
    )
    p.add_argument(
        "--ear-threshold",
        type=float,
        default=None,
        help="EAR below this value counts as eye-closed. Lower = less sensitive (default: 0.21).",
    )
    p.add_argument(
        "--target-dim",
        type=float,
        default=None,
        help="Peak overlay opacity, 0.0–1.0 (default: 0.35).",
    )
    p.add_argument(
        "--fade-ms",
        type=int,
        default=None,
        help="Fade in/out duration in milliseconds (default: 800).",
    )
    return p.parse_args()


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if args.show_preview:
        cfg.display.show_preview = True
    if args.camera is not None:
        cfg.detection.camera_index = args.camera
    if args.warning_seconds is not None:
        cfg.alert.warning_seconds = args.warning_seconds
    if args.ear_threshold is not None:
        cfg.detection.ear_threshold = args.ear_threshold
    if args.scope is not None:
        cfg.nudge.scope = args.scope
    if args.target_dim is not None:
        cfg.nudge.target_dim = args.target_dim
    if args.fade_ms is not None:
        cfg.nudge.fade_ms = args.fade_ms


def _draw_overlay(
    frame_bgr: np.ndarray,
    ear: float,
    seconds_since_blink: float,
    blink_count: int,
    warning_active: bool,
    threshold: float,
    left_pts: np.ndarray | None,
    right_pts: np.ndarray | None,
) -> None:
    color = (0, 0, 255) if warning_active else (0, 255, 0)
    cv2.putText(frame_bgr, f"EAR: {ear:.3f}  (thr {threshold:.2f})", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    cv2.putText(frame_bgr, f"No blink: {seconds_since_blink:5.1f}s", (10, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    cv2.putText(frame_bgr, f"Blinks: {blink_count}", (10, 75),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    if warning_active:
        cv2.putText(frame_bgr, "!! BLINK !!", (10, 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)

    for pts in (left_pts, right_pts):
        if pts is None:
            continue
        for (x, y) in pts.astype(int):
            cv2.circle(frame_bgr, (int(x), int(y)), 2, (255, 255, 0), -1)


def _capture_loop(
    cfg: Config,
    stop_flag: threading.Event,
    locked: threading.Event,
    service: DBusService,
    nudge: NudgeController | None,
    preview=None,
) -> None:
    cap = cv2.VideoCapture(cfg.detection.camera_index)
    if not cap.isOpened():
        log.error("cannot open camera index %d", cfg.detection.camera_index)
        stop_flag.set()
        return
    cap.set(cv2.CAP_PROP_FPS, cfg.detection.fps)
    log.info("camera %d opened", cfg.detection.camera_index)

    detector = EyeDetector()
    monitor = BlinkMonitor(
        ear_threshold=cfg.detection.ear_threshold,
        consecutive_frames=cfg.detection.consecutive_frames,
        warning_seconds=cfg.alert.warning_seconds,
    )

    # Build sorted escalation thresholds: [(seconds, level), ...].
    escalation = sorted(
        [(cfg.alert.warning_seconds, cfg.nudge.target_dim)]
        + [(int(s), float(l)) for s, l in cfg.nudge.escalation],
        key=lambda x: x[0],
    )
    current_escalation_idx = -1
    fail_count = 0
    camera_paused = False

    try:
        while not stop_flag.is_set():
            # Hyprland lock event (if available).
            if locked.is_set():
                if not camera_paused:
                    log.info("screen locked, releasing camera")
                    camera_paused = True
                    if nudge is not None:
                        nudge.deactivate()
                        current_escalation_idx = -1
                    monitor.reset_timer()
                    cap.release()
                time.sleep(0.5)
                continue

            # Resume from lock.
            if camera_paused and not locked.is_set():
                log.info("screen unlocked, reopening camera")
                time.sleep(1.0)
                cap = cv2.VideoCapture(cfg.detection.camera_index)
                cap.set(cv2.CAP_PROP_FPS, cfg.detection.fps)
                monitor.reset_timer()
                camera_paused = False
                fail_count = 0
                if not cap.isOpened():
                    log.warning("camera reopen failed, retrying in 2s")
                    time.sleep(2.0)
                    continue
                log.info("camera reopened")
                continue

            ok, frame_bgr = cap.read()
            if not ok:
                fail_count += 1
                if fail_count == 1:
                    log.warning("camera read failed, waiting for recovery")
                    if nudge is not None:
                        nudge.deactivate()
                        current_escalation_idx = -1
                    monitor.reset_timer()
                # After 5 consecutive failures (~5s of timeouts), reopen.
                if fail_count >= 5:
                    log.info("camera unresponsive (%d failures), reopening", fail_count)
                    cap.release()
                    time.sleep(2.0)
                    cap = cv2.VideoCapture(cfg.detection.camera_index)
                    cap.set(cv2.CAP_PROP_FPS, cfg.detection.fps)
                    fail_count = 0
                    if cap.isOpened():
                        log.info("camera reopened successfully")
                    else:
                        log.warning("camera reopen failed, retrying in 2s")
                continue
            fail_count = 0

            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            result = detector.process(frame_rgb)

            blinked, warning_started, warning_cleared = monitor.update(
                result.face_detected, result.ear
            )

            iface = service.interface
            iface.push_state(
                seconds_since_last_blink=monitor.state.seconds_since_last_blink,
                current_ear=result.ear,
                blink_count=monitor.state.blink_count,
                face_detected=result.face_detected,
                warning_active=monitor.state.warning_active,
            )

            if blinked:
                iface.Blinked()
            if warning_started:
                iface.NoBlinkWarning(cfg.alert.warning_seconds)
            if warning_cleared:
                iface.BlinkResumed()
                if nudge is not None:
                    nudge.deactivate()
                current_escalation_idx = -1

            # Escalation: check which threshold we've crossed and update dim.
            if nudge is not None and monitor.state.warning_active:
                elapsed = monitor.state.seconds_since_last_blink
                new_idx = current_escalation_idx
                for i, (threshold_s, _level) in enumerate(escalation):
                    if elapsed >= threshold_s:
                        new_idx = i
                if new_idx != current_escalation_idx:
                    current_escalation_idx = new_idx
                    nudge.activate(escalation[new_idx][1])

            if preview is not None:
                _draw_overlay(
                    frame_bgr,
                    result.ear,
                    monitor.state.seconds_since_last_blink,
                    monitor.state.blink_count,
                    monitor.state.warning_active,
                    cfg.detection.ear_threshold,
                    result.left_eye_pts,
                    result.right_eye_pts,
                )
                rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                preview.push_frame(rgb.tobytes(), rgb.shape[1], rgb.shape[0])
    finally:
        cap.release()
        detector.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    args = _parse_args()
    cfg = load_config(args.config)
    _apply_overrides(cfg, args)

    service = DBusService(
        bus_name=cfg.dbus.bus_name,
        object_path=cfg.dbus.object_path,
        warning_threshold=cfg.alert.warning_seconds,
    )
    service.publish()

    nudge: NudgeController | None = None
    if not args.no_dim:
        nudge = NudgeController(
            scope=cfg.nudge.scope,
            target_dim=cfg.nudge.target_dim,
            fade_ms=cfg.nudge.fade_ms,
        )

    preview = None
    if cfg.display.show_preview:
        from preview import PreviewWindow
        preview = PreviewWindow(on_close=lambda: stop_flag.set())

    stop_flag = threading.Event()
    locked = threading.Event()

    # Listen for logind session Lock/Unlock (triggered by loginctl lock-session).
    session_listener = SessionLockListener(
        on_lock=locked.set,
        on_unlock=locked.clear,
    )
    session_listener.start()

    # Also listen for Hyprland lock/unlock events (backup for hyprlock users).
    hypr_listener = HyprEventListener(
        on_lock=lambda: (locked.set(), log.info("hyprland lockscreen event")),
        on_unlock=lambda: (locked.clear(), log.info("hyprland unlockscreen event")),
    )
    hypr_listener.start()

    def _handle_signal(_signum, _frame):
        stop_flag.set()
        locked.clear()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "started: threshold=%ds ear<%.2f dim=%s camera=%d",
        cfg.alert.warning_seconds,
        cfg.detection.ear_threshold,
        "off" if args.no_dim else cfg.nudge.scope,
        cfg.detection.camera_index,
    )

    capture_thread = threading.Thread(
        target=_capture_loop,
        args=(cfg, stop_flag, locked, service, nudge, preview),
        name="capture",
        daemon=True,
    )
    capture_thread.start()

    from gi.repository import GLib

    def _check_stop() -> bool:
        if stop_flag.is_set():
            service.quit()
            return False
        return True

    GLib.timeout_add(200, _check_stop)

    if preview is not None:
        try:
            preview.run()
        finally:
            stop_flag.set()
            locked.clear()
            hypr_listener.stop()
            if nudge is not None:
                nudge.cleanup()
            capture_thread.join(timeout=2.0)
    else:
        try:
            service.run()
        finally:
            stop_flag.set()
            locked.clear()
            hypr_listener.stop()
            if nudge is not None:
                nudge.cleanup()
            capture_thread.join(timeout=2.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
