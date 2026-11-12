"""Smoke tests for the pure settings logic (no camera / D-Bus required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Config
from settings import SETTINGS_SCHEMA, RuntimeSettings, _camel


def _runtime(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings.from_config(Config(), prefs_path=tmp_path / "prefs.json")


def test_camel() -> None:
    assert _camel("warning_seconds") == "WarningSeconds"
    assert _camel("current_ear") == "CurrentEar"
    assert _camel("paused") == "Paused"


def test_defaults_from_config(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    assert rt.get("warning_seconds") == 5
    assert rt.get("scope") == "all"
    assert rt.get("paused") is False
    # Live status streaming is opt-in.
    assert rt.get("status_reporting") is False
    assert rt.set("status_reporting", True) is True


def test_set_clamps_and_validates(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    assert rt.set("ear_threshold", 0.99) == 0.5  # clamped to max.
    assert rt.set("warning_seconds", 3.7) == 4  # coerced to int.
    with pytest.raises(ValueError, match="one of"):
        rt.set("scope", "bogus")
    with pytest.raises(ValueError):
        rt.set("current_ear", 0.5)  # read-only.


def test_camera_and_scope_generations(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    cam, nudge = rt.camera_generation, rt.nudge_generation
    rt.set("camera_index", 2)
    rt.set("scope", "focused")
    assert rt.camera_generation == cam + 1
    assert rt.nudge_generation == nudge + 1


def test_persistence_roundtrip(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt.set("warning_seconds", 20)
    # A fresh instance pointed at the same prefs file picks up the override.
    fresh = RuntimeSettings.from_config(Config(), prefs_path=rt._prefs_path)
    assert fresh.get("warning_seconds") == 20


def test_schema_dict_is_camelcase(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    schema = rt.schema_dict(module_id="eyeblink", title="Eye Blink Monitor", icon="eye")
    keys = [s["key"] for g in schema["groups"] for s in g["settings"]]
    assert "WarningSeconds" in keys
    assert schema["actions"][0]["key"] == "Reset"
    # Every schema key is the CamelCase form of a known internal setting.
    assert set(keys) == {_camel(spec.key) for spec in SETTINGS_SCHEMA}
    # Serialisable (Describe returns JSON).
    json.dumps(schema)


def test_adaptive_rate_setting(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    assert rt.get("adaptive_rate") is True
    assert rt.set("adaptive_rate", False) is False


def test_frames_to_skip() -> None:
    from monitor import IDLE_AFTER_S, IDLE_SKIP, frames_to_skip

    # Eyes clearly open: every other frame only with the adaptive rate.
    assert frames_to_skip(True, 0.30, 0.21, True, 0.0) == 1
    assert frames_to_skip(True, 0.30, 0.21, False, 0.0) == 0
    # Near or below the threshold: every frame.
    assert frames_to_skip(True, 0.24, 0.21, True, 0.0) == 0
    assert frames_to_skip(True, 0.15, 0.21, True, 0.0) == 0
    # No face: every frame at first, then the idle rate.
    assert frames_to_skip(False, 0.0, 0.21, True, 1.0) == 0
    assert frames_to_skip(False, 0.0, 0.21, False, IDLE_AFTER_S) == IDLE_SKIP


def test_blink_needs_consecutive_frames() -> None:
    from monitor import BlinkMonitor

    mon = BlinkMonitor(0.21, 2, 10)
    assert not mon.update(True, 0.15)[0]
    assert not mon.update(True, 0.30)[0]  # One closed frame: noise.
    mon.update(True, 0.15)
    mon.update(True, 0.15)
    assert mon.update(True, 0.30)[0]
    mon.configure(0.21, 1, 10)
    mon.update(True, 0.15)
    assert mon.update(True, 0.30)[0]
