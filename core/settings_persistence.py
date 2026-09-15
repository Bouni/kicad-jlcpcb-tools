"""Serialize settings updates while preserving other windows' variant choices.

Ordinary settings retain their existing snapshot-save behavior. Variant choices
are written only through explicit changed paths, merged with the latest file
under the same lock used by startup migration and damaged-file recovery.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional, Union

from .file_lock import file_lock
from .settings_defaults import (
    SETTINGS_FILENAME,
    apply_invariants,
    load_defaults,
    merge_settings,
    migrate_settings,
    read_settings_file,
    resolve_settings,
)

PreferencePath = tuple[str, ...]
PluginPath = Union[str, os.PathLike[str]]
_LOCK_TIMEOUT_SECONDS = 5.0


class SettingsPersistenceError(ValueError):
    """Stored settings or an explicit preference update cannot be merged safely."""


@dataclass(frozen=True)
class VariantPreferencePatch:
    """Explicit updates/deletions at paths relative to the variants object."""

    updates: Mapping[PreferencePath, Any] = field(default_factory=dict)
    removals: tuple[PreferencePath, ...] = ()


def changed_variant_preferences(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    prefix: PreferencePath,
) -> VariantPreferencePatch:
    """Compare effective control states, with dictionaries merged per leaf.

    JSON normalization makes tuple/list representations equivalent. Ordered
    lists remain one preference, while independently resized columns can merge.
    """
    previous = json.loads(json.dumps(dict(before), allow_nan=False))
    current = json.loads(json.dumps(dict(after), allow_nan=False))
    updates: dict[PreferencePath, Any] = {}
    removals: list[PreferencePath] = []

    def compare(old: dict[str, Any], new: dict[str, Any], path: PreferencePath) -> None:
        for key in sorted(old.keys() - new.keys()):
            removals.append((*path, key))
        for key, value in new.items():
            child = (*path, key)
            if isinstance(value, dict) and isinstance(old.get(key, {}), dict):
                compare(old.get(key, {}), value, child)
            elif key not in old or old[key] != value:
                updates[child] = value

    compare(previous, current, prefix)
    return VariantPreferencePatch(updates, tuple(removals))


@contextmanager
def _settings_lock(directory: Path) -> Iterator[None]:
    """Keep one stable lock file across replacements and separate processes."""
    with file_lock(
        directory / (SETTINGS_FILENAME + ".lock"),
        timeout=_LOCK_TIMEOUT_SECONDS,
        timeout_message="Another KiCad window or process is saving settings. Try again.",
    ):
        yield


def _read_current_settings(path: Path) -> dict[str, Any]:
    """Treat damage during a save as an error, never as permission to reset."""
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise SettingsPersistenceError(
            "Stored settings are invalid. Reopen the plugin to recover them."
        ) from error
    if not isinstance(document, dict):
        raise SettingsPersistenceError(
            "Stored settings are invalid. Reopen the plugin to recover them."
        )
    return document


def _apply_variant_patch(
    document: dict[str, Any], patch: VariantPreferencePatch
) -> None:
    """Apply only requested board preferences, preserving opaque sibling data."""
    paths = [*patch.updates, *patch.removals]
    if not paths:
        return
    for path in paths:
        if (
            len(path) < 3
            or path[0] != "boards"
            or any(not isinstance(key, str) or not key for key in path)
        ):
            raise SettingsPersistenceError("Invalid variant preference path.")
    ordered = sorted(paths)
    if any(left == right[: len(left)] for left, right in zip(ordered, ordered[1:])):
        raise SettingsPersistenceError("Overlapping variant preference paths.")
    variants = document.setdefault("variants", {})
    if not isinstance(variants, dict):
        raise SettingsPersistenceError("Stored variant settings are invalid.")

    def parent_for(path: PreferencePath, create: bool) -> Optional[dict[str, Any]]:
        parent = variants
        for key in path[:-1]:
            if key not in parent:
                if not create:
                    return None
                parent[key] = {}
            child = parent[key]
            if not isinstance(child, dict):
                raise SettingsPersistenceError(
                    "Stored variant preferences are invalid."
                )
            parent = child
        return parent

    for path in patch.removals:
        parent = parent_for(path, create=False)
        if parent is not None:
            parent.pop(path[-1], None)
    for path, value in patch.updates.items():
        parent = parent_for(path, create=True)
        assert parent is not None
        parent[path[-1]] = deepcopy(value)


def _write_settings_document(directory: Path, settings: dict[str, Any]) -> None:
    """Replace only a complete document, cleaning up every failed temporary file."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as handle:
            temporary_path = handle.name
            json.dump(settings, handle)
        os.replace(temporary_path, directory / SETTINGS_FILENAME)
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                os.unlink(temporary_path)


def load_settings_document(plugin_path: PluginPath) -> dict[str, Any]:
    """Read settled settings without writes; lock every recovery/migration step."""
    directory = Path(plugin_path).resolve()
    stored = read_settings_file(str(directory / SETTINGS_FILENAME))
    if stored is not None:
        user = deepcopy(stored)
        migrate_settings(user)
        resolved = merge_settings(load_defaults(directory), user)
        apply_invariants(resolved)
        if resolved == stored:
            # Atomic replacement makes this a complete before/after snapshot.
            # Settled installed settings remain usable in a readonly directory.
            return resolved
    # A concurrent writer may have changed the file since the initial read.
    # Resolve again while locked, including any quarantine or migration write.
    with _settings_lock(directory):
        settings, needs_write = resolve_settings(directory)
        if needs_write:
            _write_settings_document(directory, settings)
        return settings


def save_settings_document(
    plugin_path: PluginPath,
    ordinary_settings: Mapping[str, Any],
    variant_patch: Optional[VariantPreferencePatch] = None,
) -> dict[str, Any]:
    """Merge explicit variant edits without replaying a stale window snapshot.

    The input object is never mutated. Callers publish the returned document
    only after this function succeeds, preserving their state on failed saves.
    """
    directory = Path(plugin_path).resolve()
    with _settings_lock(directory):
        latest = _read_current_settings(directory / SETTINGS_FILENAME)
        document = deepcopy(dict(ordinary_settings))
        if "variants" in latest:
            document["variants"] = deepcopy(latest["variants"])
        else:
            document.pop("variants", None)
        if variant_patch is not None:
            _apply_variant_patch(document, variant_patch)
        _write_settings_document(directory, document)
        return document
