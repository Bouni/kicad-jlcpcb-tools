"""Tests for seeding runtime settings from the shipped defaults."""

import json
from pathlib import Path
import re

import pytest

from core.settings_defaults import (
    DEFAULTS_FILENAME,
    SETTINGS_FILENAME,
    resolve_settings,
)

ROOT = Path(__file__).parent.parent

# The idiom the plugin uses to read a setting, e.g.
# ``self.parent.settings.get("gerber", {}).get("tented_vias", True)``.  The
# formatter wraps that call once the setting name makes it long enough, so the
# pattern has to tolerate newlines between the two halves -- otherwise it is
# blind to exactly the long names most likely to be newly added.
SETTING_READ = re.compile(r'settings\.get\(\s*"(\w+)"\s*,\s*\{\}\s*\)\.get\(\s*"(\w+)"')

# Directories that hold no plugin runtime code: vendored dependencies, the
# database build action, and the tests themselves.
NON_PLUGIN_DIRS = {"lib", "db_build", "tests", "__pycache__"}

# Settings read through a section the plugin has already pulled out of the
# dict, which the pattern above cannot see.  generate_hooks.run_configured_hook
# is handed the hooks section and builds the script key from the stage name.
SETTINGS_READ_VIA_SECTION = {
    "general": [
        "bom_estimator_boards",
        "bom_estimator_force_standard",
        "bom_estimator_show",
    ],
    "hooks": [
        "post_script",
        "pre_script",
        "timeout_seconds",
    ],
    "partselector": [
        "column_widths",
        "size",
    ],
}


def shipped_defaults():
    """Return the defaults the plugin ships and packages into the PCM archive."""
    with open(ROOT / DEFAULTS_FILENAME, encoding="utf-8") as handle:
        return json.load(handle)


def plugin_dir(tmp_path, defaults, stored=None):
    """Lay out a plugin directory holding ``defaults`` and optional ``stored``."""
    (tmp_path / DEFAULTS_FILENAME).write_text(json.dumps(defaults), encoding="utf-8")
    if stored is not None:
        (tmp_path / SETTINGS_FILENAME).write_text(json.dumps(stored), encoding="utf-8")
    return tmp_path


def test_first_run_starts_from_the_shipped_defaults(tmp_path):
    """With no settings file yet, the defaults are the settings, and get written."""
    defaults = {"gerber": {"tented_vias": True}}

    settings, needs_write = resolve_settings(plugin_dir(tmp_path, defaults))

    assert settings == defaults
    assert needs_write


def test_stored_choices_win_over_the_defaults(tmp_path):
    """A setting the user changed survives; the ones they never touched do not move."""
    defaults = {"gerber": {"tented_vias": True, "plot_values": True}}
    stored = {"gerber": {"tented_vias": False, "plot_values": True}}

    settings, needs_write = resolve_settings(plugin_dir(tmp_path, defaults, stored))

    assert settings == {"gerber": {"tented_vias": False, "plot_values": True}}
    assert not needs_write


def test_a_newly_added_setting_arrives_with_its_default(tmp_path):
    """Upgrading to a version with a new setting fills it in rather than dropping it."""
    defaults = {"gerber": {"tented_vias": True}, "hooks": {"timeout_seconds": 30}}
    stored = {"gerber": {"tented_vias": False}}

    settings, needs_write = resolve_settings(plugin_dir(tmp_path, defaults, stored))

    assert settings == {
        "gerber": {"tented_vias": False},
        "hooks": {"timeout_seconds": 30},
    }
    assert needs_write, "the completed settings should be written back to disk"


def test_settings_the_defaults_no_longer_mention_are_kept(tmp_path):
    """A section or key the defaults dropped is left in place, not silently deleted."""
    defaults = {"gerber": {"tented_vias": True}}
    stored = {"gerber": {"tented_vias": True}, "experiment": {"enabled": True}}

    settings, _ = resolve_settings(plugin_dir(tmp_path, defaults, stored))

    assert settings["experiment"] == {"enabled": True}


def test_a_damaged_settings_file_falls_back_to_the_defaults(tmp_path):
    """Corrupt JSON must not stop the plugin from opening."""
    plugin_path = plugin_dir(tmp_path, {"gerber": {"tented_vias": True}})
    (plugin_path / SETTINGS_FILENAME).write_text("{not json", encoding="utf-8")

    settings, needs_write = resolve_settings(plugin_path)

    assert settings == {"gerber": {"tented_vias": True}}
    assert needs_write


def test_a_damaged_settings_file_is_kept_rather_than_overwritten(tmp_path):
    """The unreadable file moves aside so the user can still recover from it."""
    plugin_path = plugin_dir(tmp_path, {"gerber": {"tented_vias": True}})
    (plugin_path / SETTINGS_FILENAME).write_text(
        '{"gerber": {"tented', encoding="utf-8"
    )

    resolve_settings(plugin_path)

    assert not (plugin_path / SETTINGS_FILENAME).exists()
    damaged = plugin_path / (SETTINGS_FILENAME + ".damaged")
    assert damaged.read_text(encoding="utf-8") == '{"gerber": {"tented'


def test_a_damaged_file_that_cannot_be_moved_aside_is_not_overwritten(tmp_path):
    """If the rename fails, loading stops rather than lose the only copy."""
    plugin_path = plugin_dir(tmp_path, {"gerber": {"tented_vias": True}})
    (plugin_path / SETTINGS_FILENAME).write_text("{not json", encoding="utf-8")
    # A directory in the way makes the rename fail.
    (plugin_path / (SETTINGS_FILENAME + ".damaged")).mkdir()

    with pytest.raises(OSError):
        resolve_settings(plugin_path)

    assert (plugin_path / SETTINGS_FILENAME).read_text(encoding="utf-8") == "{not json"


def test_a_settings_file_that_cannot_be_read_is_left_alone(tmp_path):
    """An I/O error is not damage: moving the file aside would reset good settings."""
    plugin_path = plugin_dir(tmp_path, {"gerber": {"tented_vias": True}})
    # A directory stands in for a file the OS will not let us read right now.
    (plugin_path / SETTINGS_FILENAME).mkdir()

    with pytest.raises(OSError):
        resolve_settings(plugin_path)

    assert (plugin_path / SETTINGS_FILENAME).is_dir()
    assert not (plugin_path / (SETTINGS_FILENAME + ".damaged")).exists()


def test_missing_defaults_file_leaves_stored_settings_usable(tmp_path):
    """An install without the defaults file still honours what the user stored."""
    (tmp_path / SETTINGS_FILENAME).write_text(
        json.dumps({"gerber": {"tented_vias": False}}), encoding="utf-8"
    )

    settings, needs_write = resolve_settings(tmp_path)

    assert settings == {"gerber": {"tented_vias": False}}
    assert not needs_write


def test_legacy_highlight_setting_moves_under_highlighting(tmp_path):
    """The pre-rename key keeps its value instead of reverting to the default."""
    defaults = {"highlighting": {"matches": True}, "partselector": {"basic": True}}
    stored = {"partselector": {"basic": True, "highlight_matches": False}}

    settings, needs_write = resolve_settings(plugin_dir(tmp_path, defaults, stored))

    assert settings == {
        "highlighting": {"matches": False},
        "partselector": {"basic": True},
    }
    assert needs_write


def test_forced_drc_keeps_zone_filling_on(tmp_path):
    """Forced DRC fills zones anyway, so the two settings cannot disagree."""
    defaults = {"gerber": {"force_drc": True, "fill_zones": True}}
    stored = {"gerber": {"force_drc": True, "fill_zones": False}}

    settings, needs_write = resolve_settings(plugin_dir(tmp_path, defaults, stored))

    assert settings["gerber"]["fill_zones"]
    assert needs_write


def test_every_setting_the_plugin_reads_has_a_shipped_default():
    """A setting added without a default would silently depend on its call site."""
    defaults = shipped_defaults()
    missing = set()

    for source in ROOT.rglob("*.py"):
        if NON_PLUGIN_DIRS.intersection(source.relative_to(ROOT).parts):
            continue
        for section, key in SETTING_READ.findall(source.read_text(encoding="utf-8")):
            if key not in defaults.get(section, {}):
                missing.add(f"{section}.{key}")

    for section, keys in SETTINGS_READ_VIA_SECTION.items():
        missing.update(
            f"{section}.{key}" for key in keys if key not in defaults.get(section, {})
        )

    assert not missing, f"no default shipped for: {sorted(missing)}"


def test_part_search_includes_every_library_type_by_default():
    """Basic, Preferred and Extended are exclusive labels, so all three must be on.

    Leaving any of them off hides a whole class of parts from a fresh install's
    search results with no hint as to why.
    """
    partselector = shipped_defaults()["partselector"]

    assert partselector["basic"]
    assert partselector["preferred"]
    assert partselector["extended"]
