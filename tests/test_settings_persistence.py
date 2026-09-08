"""Settings writes must leave a complete, reloadable document after failures."""

import json
import os
from pathlib import Path
from typing import Any, Optional, TextIO

import pytest

from .wx_harness import load_mainwindow, wx_stubs

mainwindow = load_mainwindow(
    "settings_persistence_tests",
    wx=wx_stubs(Dialog=type("Dialog", (), {}), NewIdRef=lambda: 1),
)
JLCPCBTools = mainwindow.JLCPCBTools


@pytest.fixture
def saved_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[JLCPCBTools, Path, bytes]:
    """Seed a valid document whose formatting exposes accidental rewrites."""
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    window = object.__new__(JLCPCBTools)
    window.settings = {
        "gerber": {"subtract_mask_from_silk": True},
        "highlighting": {"matches": True},
        "partselector": {"size": [1200, 700]},
        "part_preferences": {
            "remember_lcsc_assignments": False,
            "fill_empty_lcsc_assignments_on_open": False,
        },
        "custom": "retained setting",
    }
    path = tmp_path / "settings.json"
    original = json.dumps(window.settings, indent=2).encode("utf-8")
    path.write_bytes(original)
    return window, path, original


def _assert_previous_settings_reload(
    window: JLCPCBTools, path: Path, original: bytes
) -> None:
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]
    window.settings = {}
    window.load_settings()
    assert window.settings == json.loads(original)


def test_save_replaces_complete_settings_after_closing_temporary_file(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace only once complete JSON has been written and its handle closed."""
    window, path, original = saved_settings
    window.settings["partselector"]["size"] = [1300, 800]
    window.settings["custom"] = "résistance Ω"
    expected = window.settings.copy()
    streams: list[TextIO] = []
    replacements: list[Path] = []
    real_dump, real_replace = json.dump, os.replace

    def dump(settings: dict[str, Any], stream: TextIO) -> None:
        streams.append(stream)
        real_dump(settings, stream)

    def replace(source: str, destination: str) -> None:
        temporary = Path(source)
        assert temporary.parent == path.parent
        assert temporary != path
        assert Path(destination) == path
        assert path.read_bytes() == original
        assert streams and streams[-1].closed
        assert json.loads(temporary.read_text(encoding="utf-8")) == expected
        replacements.append(temporary)
        real_replace(source, destination)

    monkeypatch.setattr(mainwindow.json, "dump", dump)
    monkeypatch.setattr(mainwindow.os, "replace", replace)
    window.save_settings()

    assert len(replacements) == 1
    assert list(path.parent.iterdir()) == [path]
    window.settings = {}
    window.load_settings()
    assert window.settings == expected


def test_partial_json_write_preserves_previous_settings(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted real write must not truncate the last saved document."""
    window, path, original = saved_settings

    def interrupted_dump(_settings: dict[str, Any], stream: TextIO) -> None:
        stream.write('{"partselector":')
        stream.flush()
        raise OSError("disk full during settings write")

    monkeypatch.setattr(mainwindow.json, "dump", interrupted_dump)
    with pytest.raises(OSError, match="disk full"):
        window.save_settings()

    _assert_previous_settings_reload(window, path, original)


@pytest.mark.parametrize("circular", [False, True], ids=["unsupported", "circular"])
def test_serialization_failure_preserves_previous_settings(
    saved_settings: tuple[JLCPCBTools, Path, bytes], circular: bool
) -> None:
    """Real JSON encoder failures leave saved settings readable on reopening."""
    window, path, original = saved_settings
    window.settings["invalid"] = window.settings if circular else object()
    with pytest.raises(ValueError if circular else TypeError):
        window.save_settings()

    _assert_previous_settings_reload(window, path, original)


def test_replace_failure_preserves_previous_settings_and_removes_temporary_file(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed final replacement retains the old file without orphaned data."""
    window, path, original = saved_settings
    window.settings["partselector"]["size"] = [1300, 800]

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("settings replacement denied")

    monkeypatch.setattr(mainwindow.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement denied"):
        window.save_settings()

    _assert_previous_settings_reload(window, path, original)


@pytest.mark.parametrize(
    "existing",
    [
        None,
        {},
        {"remember_lcsc_assignments": False},
        {"fill_empty_lcsc_assignments_on_open": False},
        {
            "remember_lcsc_assignments": False,
            "fill_empty_lcsc_assignments_on_open": False,
            "custom_preference": "retained",
        },
    ],
    ids=["missing-section", "empty-section", "remember-off", "fill-off", "settled"],
)
def test_part_preference_defaults_migrate_atomically_and_settle(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
    existing: Optional[dict[str, Any]],
) -> None:
    """Old files gain defaults once; explicit false and unrelated values survive."""
    window, path, original = saved_settings
    previous = json.loads(original)
    previous.pop("part_preferences")
    if existing is not None:
        previous["part_preferences"] = existing
    original = json.dumps(previous, indent=2).encode("utf-8")
    path.write_bytes(original)
    expected = {
        **previous,
        "part_preferences": {
            "remember_lcsc_assignments": True,
            "fill_empty_lcsc_assignments_on_open": True,
            **(existing or {}),
        },
    }
    replacements: list[Path] = []
    real_replace = os.replace

    def replace(source: str, destination: str) -> None:
        temporary = Path(source)
        assert path.read_bytes() == original
        assert json.loads(temporary.read_text(encoding="utf-8")) == expected
        replacements.append(temporary)
        real_replace(source, destination)

    monkeypatch.setattr(mainwindow.os, "replace", replace)
    window.load_settings()

    assert window.settings == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert list(path.parent.iterdir()) == [path]
    assert len(replacements) == (0 if previous == expected else 1)
    settled = path.read_bytes()
    replacement_count = len(replacements)

    window.settings = {}
    window.load_settings()

    assert window.settings == expected
    assert path.read_bytes() == settled
    assert len(replacements) == replacement_count


@pytest.mark.parametrize("failure", ["partial-write", "replace"])
def test_failed_part_preference_migration_preserves_file_and_allows_retry(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Failed upgrades leave the old complete bytes and can succeed on reopening."""
    _window, path, original = saved_settings
    previous = json.loads(original)
    previous["part_preferences"] = {"remember_lcsc_assignments": False}
    original = json.dumps(previous, indent=2).encode("utf-8")
    path.write_bytes(original)

    def interrupted_dump(_settings: dict[str, Any], stream: TextIO) -> None:
        stream.write('{"part_preferences":')
        stream.flush()
        raise OSError("migration write interrupted")

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("migration replacement denied")

    with monkeypatch.context() as failure_patch:
        if failure == "partial-write":
            failure_patch.setattr(mainwindow.json, "dump", interrupted_dump)
        else:
            failure_patch.setattr(mainwindow.os, "replace", fail_replace)
        window = object.__new__(JLCPCBTools)
        with pytest.raises(OSError, match="migration"):
            window.load_settings()

        assert path.read_bytes() == original
        assert list(path.parent.iterdir()) == [path]

    reopened = object.__new__(JLCPCBTools)
    reopened.load_settings()

    expected = {
        **previous,
        "part_preferences": {
            "remember_lcsc_assignments": False,
            "fill_empty_lcsc_assignments_on_open": True,
        },
    }
    assert reopened.settings == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert list(path.parent.iterdir()) == [path]
