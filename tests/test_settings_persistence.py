"""Settings writes must leave a complete, reloadable document after failures."""

import json
import os
from pathlib import Path
from typing import Any, Optional, TextIO

import pytest

from core import settings_persistence

from .test_settings_defaults import plugin_dir, shipped_defaults
from .variant_data_support import SavedNativeBoard, store_module_impl
from .wx_harness import load_mainwindow, wx_stubs

mainwindow = load_mainwindow(
    "settings_persistence_tests",
    wx=wx_stubs(Frame=type("Frame", (), {}), NewIdRef=lambda: 1),
)
JLCPCBTools = mainwindow.JLCPCBTools

# The plugin directory holds the shipped defaults beside the user's settings;
# a stable lock file coordinates windows and separate KiCad processes.
PLUGIN_SETTINGS_FILES = ["default_settings.json", "settings.json", "settings.json.lock"]


@pytest.mark.parametrize("second_action", ["variant", "ordinary"])
def test_independent_windows_preserve_other_boards_variant_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_action: str
) -> None:
    """A stale window must not erase another board's saved output or layout."""
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    plugin_dir(tmp_path, shipped_defaults(), shipped_defaults())
    first, second = (object.__new__(JLCPCBTools) for _ in range(2))
    first.load_settings()
    second.load_settings()
    assert first.settings is not second.settings
    stores = [
        store_module_impl.VariantStore(
            window, str(tmp_path), SavedNativeBoard(tmp_path, name)
        )
        for window, name in ((first, "first"), (second, "second"))
    ]
    stores[0].set_output_variant("B")
    stores[0].set_display_preferences(
        {"variant_order": ["", "B", "A"], "differences_only": True}
    )
    if second_action == "variant":
        stores[1].set_output_variant("A")
    else:
        second.settings["general"]["simplify_stock"] = False
        second.save_settings()

    reopened = object.__new__(JLCPCBTools)
    reopened.load_settings()
    reopened_store = store_module_impl.VariantStore(
        reopened, str(tmp_path), SavedNativeBoard(tmp_path, "first")
    )
    assert reopened_store.get_output_variant() == "B"
    assert reopened_store.get_display_preferences() == {
        "variant_order": ["", "B", "A"],
        "differences_only": True,
    }


@pytest.mark.parametrize("failure", ["partial-write", "replace"])
def test_failed_variant_preference_save_preserves_window_and_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Actual window/store saves publish neither memory nor bytes on failure."""
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    plugin_dir(tmp_path, shipped_defaults(), shipped_defaults())
    window = object.__new__(JLCPCBTools)
    window.load_settings()
    store = store_module_impl.VariantStore(
        window, str(tmp_path), SavedNativeBoard(tmp_path)
    )
    store.set_output_variant("A")
    settings_identity = window.settings
    before = json.loads(json.dumps(window.settings))
    path = tmp_path / "settings.json"
    original = path.read_bytes()

    def fail_dump(_settings: dict[str, Any], stream: TextIO) -> None:
        stream.write('{"variants":')
        stream.flush()
        raise OSError("settings write interrupted")

    def fail_replace(_source: Any, _destination: Any) -> None:
        raise OSError("settings replacement denied")

    if failure == "partial-write":
        monkeypatch.setattr(settings_persistence.json, "dump", fail_dump)
    else:
        monkeypatch.setattr(settings_persistence.os, "replace", fail_replace)
    with pytest.raises(OSError, match="settings"):
        store.set_output_variant("B")
    assert window.settings is settings_identity
    assert window.settings == before
    assert path.read_bytes() == original
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "default_settings.json",
        "main.kicad_pcb",
        "settings.json",
        "settings.json.lock",
    ]


@pytest.fixture
def saved_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[JLCPCBTools, Path, bytes]:
    """Seed a valid document whose formatting exposes accidental rewrites.

    The shipped defaults sit beside it as they do in an installed plugin, and the
    document already holds every setting they supply, so loading it adds nothing.
    """
    monkeypatch.setattr(mainwindow, "PLUGIN_PATH", str(tmp_path))
    defaults = shipped_defaults()
    plugin_dir(tmp_path, defaults)
    window = object.__new__(JLCPCBTools)
    window.settings = {
        **defaults,
        "gerber": {**defaults["gerber"], "subtract_mask_from_silk": True},
        "general": {**defaults["general"], "simplify_stock": True},
        "highlighting": {
            **defaults["highlighting"],
            "matches": True,
            "stock_concern": True,
        },
        "partselector": {**defaults["partselector"], "size": [1200, 700]},
        "part_preferences": {
            **defaults["part_preferences"],
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
    assert sorted(f.name for f in path.parent.iterdir()) == PLUGIN_SETTINGS_FILES
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

    monkeypatch.setattr(settings_persistence.json, "dump", dump)
    monkeypatch.setattr(settings_persistence.os, "replace", replace)
    window.save_settings()

    assert len(replacements) == 1
    assert sorted(f.name for f in path.parent.iterdir()) == PLUGIN_SETTINGS_FILES
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

    monkeypatch.setattr(settings_persistence.json, "dump", interrupted_dump)
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

    monkeypatch.setattr(settings_persistence.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement denied"):
        window.save_settings()

    _assert_previous_settings_reload(window, path, original)


_MIGRATION_DEFAULTS = {
    "part_preferences": {
        "remember_lcsc_assignments": True,
        "fill_empty_lcsc_assignments_on_open": True,
    },
    "general": {"simplify_stock": True},
}


@pytest.mark.parametrize(
    "section, existing",
    [
        ("part_preferences", None),
        ("part_preferences", {}),
        ("part_preferences", {"remember_lcsc_assignments": False}),
        ("part_preferences", {"fill_empty_lcsc_assignments_on_open": False}),
        (
            "part_preferences",
            {
                "remember_lcsc_assignments": False,
                "fill_empty_lcsc_assignments_on_open": False,
                "custom_preference": "retained",
            },
        ),
        ("general", None),
        ("general", {"custom_general": "retained"}),
        ("general", {"simplify_stock": False, "custom_general": "retained"}),
        ("general", {"simplify_stock": True, "custom_general": "retained"}),
    ],
)
def test_missing_settings_migrate_atomically_and_settle(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    existing: Optional[dict[str, Any]],
) -> None:
    """Defaults arrive once without losing explicit false or unrelated settings."""
    window, path, original = saved_settings
    previous = json.loads(original)
    previous.pop(section)
    if existing is not None:
        previous[section] = existing
    original = json.dumps(previous, indent=2).encode("utf-8")
    path.write_bytes(original)
    expected = {
        **previous,
        section: {
            **shipped_defaults()[section],
            **_MIGRATION_DEFAULTS[section],
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

    monkeypatch.setattr(settings_persistence.os, "replace", replace)
    window.load_settings()
    assert window.settings == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    expected_files = (
        PLUGIN_SETTINGS_FILES if previous != expected else PLUGIN_SETTINGS_FILES[:2]
    )
    assert sorted(f.name for f in path.parent.iterdir()) == expected_files
    assert len(replacements) == (0 if previous == expected else 1)
    settled, replacement_count = path.read_bytes(), len(replacements)
    reopened = object.__new__(JLCPCBTools)
    reopened.load_settings()
    assert reopened.settings == expected
    assert path.read_bytes() == settled
    assert len(replacements) == replacement_count


@pytest.mark.parametrize(
    "section, missing, failure",
    [
        ("part_preferences", "fill_empty_lcsc_assignments_on_open", "partial-write"),
        ("part_preferences", "fill_empty_lcsc_assignments_on_open", "replace"),
        ("general", "simplify_stock", "partial-write"),
        ("general", "simplify_stock", "replace"),
        ("highlighting", "stock_concern", "replace"),
    ],
)
def test_failed_settings_migration_preserves_file_and_retries(
    saved_settings: tuple[JLCPCBTools, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    missing: str,
    failure: str,
) -> None:
    """Failed upgrades preserve complete bytes and retry on the next real load."""
    _window, path, original = saved_settings
    previous = json.loads(original)
    previous[section].pop(missing)
    original = json.dumps(previous, indent=2).encode("utf-8")
    path.write_bytes(original)

    def interrupted_dump(_settings: dict[str, Any], stream: TextIO) -> None:
        stream.write('{"' + section + '":')
        stream.flush()
        raise OSError("migration write interrupted")

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("migration replacement denied")

    with monkeypatch.context() as failure_patch:
        if failure == "partial-write":
            failure_patch.setattr(settings_persistence.json, "dump", interrupted_dump)
        else:
            failure_patch.setattr(settings_persistence.os, "replace", fail_replace)
        with pytest.raises(OSError, match="migration"):
            object.__new__(JLCPCBTools).load_settings()
        assert path.read_bytes() == original
        assert sorted(f.name for f in path.parent.iterdir()) == PLUGIN_SETTINGS_FILES

    expected = {**previous, section: {**previous[section], missing: True}}
    reopened = object.__new__(JLCPCBTools)
    reopened.load_settings()
    assert reopened.settings == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert sorted(f.name for f in path.parent.iterdir()) == PLUGIN_SETTINGS_FILES
