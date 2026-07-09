"""MediaPipe FaceLandmarker (Tasks API) wrapper + EAR computation."""

from __future__ import annotations

import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

log = logging.getLogger("eyeblink.detector")

# 6-point EAR scheme (Soukupová & Čech, 2016).
# Index ordering: [outer corner, top-1, top-2, inner corner, bottom-2, bottom-1].
LEFT_EYE_EAR_IDX = (33, 160, 158, 133, 153, 144)
RIGHT_EYE_EAR_IDX = (362, 385, 387, 263, 373, 380)

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)


def _model_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "eyeblink-monitor" / "face_landmarker.task"


def _ensure_model() -> Path:
    path = _model_cache_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"eyeblink-monitor: downloading FaceLandmarker model to {path} ...", flush=True)
    urllib.request.urlretrieve(MODEL_URL, path)
    return path


@dataclass
class DetectionResult:
    face_detected: bool
    ear: float
    left_eye_pts: np.ndarray | None
    right_eye_pts: np.ndarray | None


def _make_options(
    model_path: Path,
    delegate: mp_python.BaseOptions.Delegate,
) -> mp_vision.FaceLandmarkerOptions:
    # output_face_blendshapes / output_facial_transformation_matrixes are
    # False by default, but setting them explicitly avoids instantiating
    # the FaceBlendshapesGraph (which hardcodes XNNPACK CPU and spawns an
    # ncpu-sized threadpool) when MediaPipe's graph builder honours the flag.
    return mp_vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(
            model_asset_path=str(model_path),
            delegate=delegate,
        ),
        running_mode=mp_vision.RunningMode.VIDEO,
        num_faces=1,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
    )


def _eye_aspect_ratio(pts: np.ndarray) -> float:
    v1 = np.linalg.norm(pts[1] - pts[5])
    v2 = np.linalg.norm(pts[2] - pts[4])
    h = np.linalg.norm(pts[0] - pts[3])
    if h < 1e-6:
        return 0.0
    return float((v1 + v2) / (2.0 * h))


class EyeDetector:
    def __init__(
        self,
        model_path: Path | None = None,
        *,
        prefer_gpu: bool = True,
    ) -> None:
        path = model_path or _ensure_model()
        self._landmarker = self._build_landmarker(path, prefer_gpu=prefer_gpu)
        self._start = time.monotonic()

    @staticmethod
    def _build_landmarker(model_path: Path, *, prefer_gpu: bool) -> mp_vision.FaceLandmarker:
        # MediaPipe's GPU delegate uses TFLite over OpenGL ES via EGL, which
        # fails to initialise on headless boxes, in containers without EGL
        # libs, and on platforms MediaPipe doesn't ship GPU support for
        # (Windows, aarch64 Linux). Try GPU first; fall back to CPU on any
        # construction error so the detector still starts up.
        if prefer_gpu:
            try:
                landmarker = mp_vision.FaceLandmarker.create_from_options(
                    _make_options(model_path, mp_python.BaseOptions.Delegate.GPU)
                )
            except Exception as exc:
                log.warning(
                    "GPU delegate unavailable (%s: %s); falling back to CPU",
                    type(exc).__name__,
                    exc,
                )
            else:
                log.info("FaceLandmarker initialised with GPU delegate")
                return landmarker

        landmarker = mp_vision.FaceLandmarker.create_from_options(
            _make_options(model_path, mp_python.BaseOptions.Delegate.CPU)
        )
        log.info("FaceLandmarker initialised with CPU delegate")
        return landmarker

    def close(self) -> None:
        self._landmarker.close()

    def process(self, frame_rgb: np.ndarray) -> DetectionResult:
        h, w, _ = frame_rgb.shape
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
        # VIDEO mode requires monotonically increasing timestamps in ms.
        ts_ms = int((time.monotonic() - self._start) * 1000)
        result = self._landmarker.detect_for_video(mp_image, ts_ms)

        if not result.face_landmarks:
            return DetectionResult(False, 0.0, None, None)

        landmarks = result.face_landmarks[0]

        def gather(indices: tuple[int, ...]) -> np.ndarray:
            return np.array(
                [(landmarks[i].x * w, landmarks[i].y * h) for i in indices],
                dtype=np.float32,
            )

        left = gather(LEFT_EYE_EAR_IDX)
        right = gather(RIGHT_EYE_EAR_IDX)
        ear = (_eye_aspect_ratio(left) + _eye_aspect_ratio(right)) / 2.0
        return DetectionResult(True, ear, left, right)
