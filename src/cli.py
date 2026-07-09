"""CLI entry point: capture loop, blink detection, screen dimming, D-Bus.

Threading model:
- Capture + detection runs on a background thread.
- GLib main loop runs on the main thread (services D-Bus + nudge animation
  timers + optional preview window).
"""

from __future__ import annotations

import os

# Cap auxiliary threadpools BEFORE numpy / cv2 / mediapipe import — those
# libraries snapshot the env at first import and create ncpu-sized pools by
# default, which on a 16+ core box drowns the per-frame work in context
# switches. setdefault lets the user override via the shell.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import argparse
import logging
import signal
import threading
import time
from pathlib import Path

import cv2
import numpy as np

cv2.setNumThreads(1)

from config import Config, load_config
from dbus_service import DBusService
from detector import EyeDetector
from hypr import HyprEventListener
from monitor import BlinkMonitor
from nudge import NudgeController
from session import SessionLockListener
from settings import RuntimeSettings

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
        help="Peak overlay opacity, 0.0-1.0 (default: 0.35).",
    )
    p.add_argument(
        "--fade-ms",
        type=int,
        default=None,
        help="Fade in/out duration in milliseconds (default: 800).",
    )
    p.add_argument(
        "--no-gpu",
        action="store_true",
        help="Disable the MediaPipe GPU delegate and run inference on CPU only.",
    )
    return p.parse_args()


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    """Fold CLI flags that are not live-tunable runtime settings into ``cfg``."""
    if args.show_preview:
        cfg.display.show_preview = True
    if args.no_gpu:
        cfg.detection.prefer_gpu = False


def _apply_cli_overrides(rt: RuntimeSettings, args: argparse.Namespace) -> None:
    """Apply CLI flags as one-shot overrides (win over config + persisted prefs)."""
    if args.camera is not None:
        rt.override("camera_index", args.camera)
    if args.warning_seconds is not None:
        rt.override("warning_seconds", args.warning_seconds)
    if args.ear_threshold is not None:
        rt.override("ear_threshold", args.ear_threshold)
    if args.scope is not None:
        rt.override("scope", args.scope)
    if args.target_dim is not None:
        rt.override("target_dim", args.target_dim)
    if args.fade_ms is not None:
        rt.override("fade_ms", args.fade_ms)
    if args.no_dim:
        rt.override("dim_enabled", False)


class NudgeHolder:
    """Shared, swappable reference to the active dimming controller.

    The capture thread reads ``controller``; scope changes rebuild it on the
    GLib main thread (where its timers/sources live) and swap the reference.
    """

    def __init__(self, controller: NudgeController) -> None:
        self.controller = controller


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
    cv2.putText(
        frame_bgr,
        f"EAR: {ear:.3f}  (thr {threshold:.2f})",
        (10, 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2,
    )
    cv2.putText(
        frame_bgr,
        f"No blink: {seconds_since_blink:5.1f}s",
        (10, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2,
    )
    cv2.putText(
        frame_bgr, f"Blinks: {blink_count}", (10, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2
    )
    if warning_active:
        cv2.putText(
            frame_bgr, "!! BLINK !!", (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3
        )

    for pts in (left_pts, right_pts):
        if pts is None:
            continue
        for x, y in pts.astype(int):
            cv2.circle(frame_bgr, (int(x), int(y)), 2, (255, 255, 0), -1)


def _capture_loop(
    cfg: Config,
    rt: RuntimeSettings,
    stop_flag: threading.Event,
    locked: threading.Event,
    service: DBusService,
    nudge_holder: NudgeHolder,
    preview=None,
) -> None:
    from gi.repository import GLib

    s = rt.snapshot()
    cap = cv2.VideoCapture(s["camera_index"])
    if not cap.isOpened():
        log.error("cannot open camera index %d", s["camera_index"])
        stop_flag.set()
        return
    cap.set(cv2.CAP_PROP_FPS, cfg.detection.fps)
    log.info("camera %d opened", s["camera_index"])

    detector = EyeDetector(prefer_gpu=cfg.detection.prefer_gpu)
    monitor = BlinkMonitor(
        ear_threshold=s["ear_threshold"],
        consecutive_frames=s["consecutive_frames"],
        warning_seconds=s["warning_seconds"],
    )

    current_escalation_idx = -1
    fail_count = 0
    camera_paused = False
    camera_gen = rt.camera_generation
    nudge_gen = rt.nudge_generation

    def _rebuild_nudge(scope: str, target_dim: float, fade_ms: int) -> bool:
        # Runs on the GLib main thread so timer/source teardown is safe.
        old = nudge_holder.controller
        old.deactivate()
        old.cleanup()
        nudge_holder.controller = NudgeController(
            scope=scope, target_dim=target_dim, fade_ms=fade_ms
        )
        log.info("nudge rebuilt with scope=%s", scope)
        return False

    try:
        while not stop_flag.is_set():
            s = rt.snapshot()
            paused_now = locked.is_set() or bool(s["paused"])
            nudge = nudge_holder.controller

            # Paused (screen lock or user pause): release the camera, idle.
            if paused_now:
                if not camera_paused:
                    log.info("monitoring paused, releasing camera")
                    camera_paused = True
                    nudge.deactivate()
                    current_escalation_idx = -1
                    monitor.reset_timer()
                    cap.release()
                time.sleep(0.5)
                continue

            # Resume from pause.
            if camera_paused and not paused_now:
                log.info("monitoring resumed, reopening camera")
                time.sleep(1.0)
                cap = cv2.VideoCapture(s["camera_index"])
                cap.set(cv2.CAP_PROP_FPS, cfg.detection.fps)
                monitor.reset_timer()
                camera_paused = False
                fail_count = 0
                camera_gen = rt.camera_generation
                if not cap.isOpened():
                    log.warning("camera reopen failed, retrying in 2s")
                    time.sleep(2.0)
                    continue
                log.info("camera reopened")
                continue

            # Live scope change: rebuild the dimming controller on the GLib thread.
            if rt.nudge_generation != nudge_gen:
                nudge_gen = rt.nudge_generation
                current_escalation_idx = -1
                GLib.idle_add(_rebuild_nudge, s["scope"], s["target_dim"], s["fade_ms"])

            # Live camera-index change: reopen the capture device.
            if rt.camera_generation != camera_gen:
                camera_gen = rt.camera_generation
                log.info("camera index changed to %d, reopening", s["camera_index"])
                nudge.deactivate()
                current_escalation_idx = -1
                cap.release()
                time.sleep(0.3)
                cap = cv2.VideoCapture(s["camera_index"])
                cap.set(cv2.CAP_PROP_FPS, cfg.detection.fps)
                monitor.reset_timer()
                if not cap.isOpened():
                    log.warning("camera %d reopen failed, retrying", s["camera_index"])
                    time.sleep(1.0)
                continue

            # Apply live detection tunables to the monitor.
            monitor._threshold = s["ear_threshold"]
            monitor._required_frames = max(1, int(s["consecutive_frames"]))
            monitor._warning_seconds = s["warning_seconds"]
            nudge.update_params(s["target_dim"], s["fade_ms"])

            ok, frame_bgr = cap.read()
            if not ok:
                fail_count += 1
                if fail_count == 1:
                    log.warning("camera read failed, waiting for recovery")
                    nudge.deactivate()
                    current_escalation_idx = -1
                    monitor.reset_timer()
                # After 5 consecutive failures (~5s of timeouts), reopen.
                if fail_count >= 5:
                    log.info("camera unresponsive (%d failures), reopening", fail_count)
                    cap.release()
                    time.sleep(2.0)
                    cap = cv2.VideoCapture(s["camera_index"])
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
            # Mirror live status into the settings interface for the tuning UI.
            rt.push_status(
                seconds_since_last_blink=monitor.state.seconds_since_last_blink,
                current_ear=result.ear,
                blink_count=monitor.state.blink_count,
                face_detected=result.face_detected,
                warning_active=monitor.state.warning_active,
            )

            if blinked:
                iface.Blinked()
            if warning_started:
                iface.NoBlinkWarning(s["warning_seconds"])
            if warning_cleared:
                iface.BlinkResumed()
                nudge.deactivate()
                current_escalation_idx = -1

            dim_enabled = bool(s["dim_enabled"])
            if not dim_enabled and current_escalation_idx != -1:
                nudge.deactivate()
                current_escalation_idx = -1

            # Escalation: two thresholds (no-blink period, escalation). Pick the
            # highest crossed and update the dim level to match.
            if dim_enabled and monitor.state.warning_active:
                escalation = sorted(
                    [
                        (s["warning_seconds"], s["target_dim"]),
                        (s["escalation_seconds"], s["escalation_dim"]),
                    ],
                    key=lambda x: x[0],
                )
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
                    s["ear_threshold"],
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

    # Live-tunable settings: config defaults + persisted user tweaks, then CLI
    # flags win for this run (without being persisted).
    rt = RuntimeSettings.from_config(cfg)
    _apply_cli_overrides(rt, args)

    service = DBusService(
        bus_name=cfg.dbus.bus_name,
        object_path=cfg.dbus.object_path,
        warning_threshold=cfg.alert.warning_seconds,
        runtime=rt,
        config=cfg,
    )
    service.publish()

    # The dimming controller is always constructed so it can be toggled live via
    # the `dim_enabled` setting; `--no-dim` just starts with dimming off.
    initial = rt.snapshot()
    nudge_holder = NudgeHolder(
        NudgeController(
            scope=initial["scope"],
            target_dim=initial["target_dim"],
            fade_ms=initial["fade_ms"],
        )
    )

    stop_flag = threading.Event()
    locked = threading.Event()

    preview = None
    if cfg.display.show_preview:
        from preview import PreviewWindow

        preview = PreviewWindow(on_close=stop_flag.set)

    # Listen for logind session Lock/Unlock (triggered by loginctl lock-session).
    session_listener = SessionLockListener(
        on_lock=locked.set,
        on_unlock=locked.clear,
    )
    session_listener.start()

    def _on_hypr_lock() -> None:
        locked.set()
        log.info("hyprland lockscreen event")

    def _on_hypr_unlock() -> None:
        locked.clear()
        log.info("hyprland unlockscreen event")

    # Also listen for Hyprland lock/unlock events (backup for hyprlock users).
    hypr_listener = HyprEventListener(
        on_lock=_on_hypr_lock,
        on_unlock=_on_hypr_unlock,
    )
    hypr_listener.start()

    def _handle_signal(_signum, _frame):
        stop_flag.set()
        locked.clear()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "started: threshold=%ds ear<%.2f dim=%s camera=%d",
        initial["warning_seconds"],
        initial["ear_threshold"],
        initial["scope"] if initial["dim_enabled"] else "off",
        initial["camera_index"],
    )

    capture_thread = threading.Thread(
        target=_capture_loop,
        args=(cfg, rt, stop_flag, locked, service, nudge_holder, preview),
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
            nudge_holder.controller.cleanup()
            capture_thread.join(timeout=2.0)
    else:
        try:
            service.run()
        finally:
            stop_flag.set()
            locked.clear()
            hypr_listener.stop()
            nudge_holder.controller.cleanup()
            capture_thread.join(timeout=2.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
