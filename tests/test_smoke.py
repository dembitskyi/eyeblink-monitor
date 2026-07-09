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
