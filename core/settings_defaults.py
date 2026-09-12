"""Resolve the plugin's runtime settings from its shipped defaults.

The plugin ships ``default_settings.json`` and keeps the user's own choices
next to it in ``settings.json``.  Only the defaults file is tracked in git and
packaged into the PCM archive, so toggling a checkbox while developing no
longer dirties the repository, and an installed archive carries no settings
file of its own to write over the choices the user made.

The two files are merged rather than swapped: the defaults supply every key,
the stored settings override the ones the user has actually changed.  A
setting introduced by a newer plugin version therefore arrives with its
default instead of being absent, without the reader having to repeat that
default at every call site.

Nothing here imports wx or pcbnew, so the whole resolution path can be
exercised by the test suite outside KiCad.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Optional

DEFAULTS_FILENAME = "default_settings.json"
SETTINGS_FILENAME = "settings.json"

logger = logging.getLogger(__name__)


def defaults_path(plugin_path) -> str:
    """Return the path of the shipped defaults file."""
    return os.path.join(plugin_path, DEFAULTS_FILENAME)


def settings_path(plugin_path) -> str:
    """Return the path of the user's settings file."""
    return os.path.join(plugin_path, SETTINGS_FILENAME)


def read_settings_file(path: str) -> Optional[dict[str, Any]]:  # noqa: UP045
    """Return the settings object stored at ``path``.

    ``None`` means "nothing usable is there": the file is missing, is not valid
    JSON, or does not hold a JSON object.  A damaged settings file must not stop
    the plugin from opening, so the caller falls back to the defaults instead.

    Any other ``OSError`` propagates.  A file that cannot be read at the moment
    -- locked by another program, say -- may hold perfectly good settings, and
    treating it as damaged would reset them.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return None
    except ValueError:
        logger.warning(
            "Ignoring settings file %s, it is not valid JSON", path, exc_info=True
        )
        return None

    if not isinstance(loaded, dict):
        logger.warning("Ignoring settings file %s, it holds no JSON object", path)
        return None

    return loaded


def load_defaults(plugin_path) -> dict[str, Any]:
    """Return the settings the plugin ships as its defaults."""
    defaults = read_settings_file(defaults_path(plugin_path))
    if defaults is None:
        logger.warning(
            "No usable %s found, starting from empty settings", DEFAULTS_FILENAME
        )
        return {}
    return defaults


def merge_settings(defaults: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Overlay ``stored`` settings onto ``defaults``, section by section."""
    merged = copy.deepcopy(defaults)
    for section, values in stored.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(copy.deepcopy(values))
        else:
            merged[section] = copy.deepcopy(values)
    return merged


def migrate_settings(stored: dict[str, Any]) -> None:
    """Move settings that older plugin versions stored under another name."""
    partselector = stored.get("partselector")
    if isinstance(partselector, dict) and "highlight_matches" in partselector:
        moved = partselector.pop("highlight_matches")
        highlighting = stored.setdefault("highlighting", {})
        if isinstance(highlighting, dict):
            highlighting.setdefault("matches", moved)


def apply_invariants(settings: dict[str, Any]) -> None:
    """Repair setting combinations the plugin cannot honour."""
    gerber = settings.get("gerber")
    if not isinstance(gerber, dict):
        return
    # Forcing DRC fills the zones on the way through, so the two settings
    # cannot disagree about whether zones get filled.
    if gerber.get("force_drc", False) and not gerber.get("fill_zones", True):
        gerber["fill_zones"] = True


def quarantine_settings_file(path: str) -> None:
    """Move a settings file we could not parse out of the way.

    The plugin carries on with the defaults, and writes them back over the
    path we were handed.  Renaming first means a file broken by, say, a hand
    edit is still there to be recovered from; an older ``.damaged`` file is
    replaced.  If the rename fails the ``OSError`` propagates, because writing
    the defaults back would then destroy the only copy.
    """
    damaged = path + ".damaged"
    os.replace(path, damaged)
    logger.warning("Moved damaged settings to %s, continuing with defaults", damaged)


def resolve_settings(plugin_path) -> tuple[dict[str, Any], bool]:
    """Return the settings to run with and whether they must be written back.

    The second element is ``True`` whenever the resolved settings differ from
    what is on disk -- a first run with no settings file, a newly added default,
    a migrated key, or a repaired combination -- so the caller can persist them
    and leave the file complete for the next launch.
    """
    path = settings_path(plugin_path)
    stored = read_settings_file(path)
    if stored is None and os.path.exists(path):
        quarantine_settings_file(path)
    user = copy.deepcopy(stored) if stored is not None else {}
    migrate_settings(user)
    settings = merge_settings(load_defaults(plugin_path), user)
    apply_invariants(settings)
    return settings, settings != stored
