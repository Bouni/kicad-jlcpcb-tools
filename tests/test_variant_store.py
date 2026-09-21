"""Captured assembly rows and existing plugin-owned persistence contracts."""
# ruff: noqa: D103

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from typing import Any

import pytest

from .variant_data_support import (
    _component,
    _snapshot,
    _store,
    store_module_impl as store_module,
)
from .variant_native_support import native


def test_assembly_rows_preserve_capture_flags_facts_and_natural_reference_order(
    tmp_path: Path,
) -> None:
    """Captured A data cannot silently become current B data or change after refresh."""
    store = _store(tmp_path)
    parts = tuple(
        _component(
            name,
            lcsc,
            reference=ref,
            component_id=ref,
            bom=ref != "R2",
            pos=False,
            pop=ref != "R10",
        )
        for name, lcsc in (("", "C100"), ("A", "C200"))
        for ref in ("R10", "R2", "R1")
    )
    snapshot = _snapshot(store, parts, variants=("", "A"))
    store.set_assembly_metadata("C200", "SMT", 0)
    captured = store.assembly_rows(snapshot, "A")
    assert [p["reference"] for p in captured] == ["R1", "R2", "R10"]
    assert all(p["lcsc"] == "C200" and p["exclude_from_pos"] for p in captured)
    assert [p["exclude_from_bom"] for p in captured] == [False, True, False]
    assert [p["is_dnp"] for p in captured] == [False, False, True]
    assert all(p["assembly_process"] == "SMT" for p in captured)
    assert all(p["component_product_type"] == 0 for p in captured)
    changed = replace(
        snapshot, components=tuple(replace(p, lcsc="C300") for p in parts)
    )
    assert store.assembly_rows(changed, "A")[0]["lcsc"] == "C300"
    store.set_assembly_metadata("C200", "THT", 2)
    assert captured[0]["lcsc"] == "C200" and captured[0]["assembly_process"] == "SMT"
    assert store.assembly_rows(snapshot, "A")[0]["assembly_process"] == "THT"
    assert store.assembly_rows(snapshot, "")[0]["lcsc"] == "C100"
    assert not hasattr(store, "snapshot")
    assert not Path(store.dbfile).exists()


def test_rows_reject_foreign_snapshots_and_removed_variants(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = _snapshot(store)
    other = _store(tmp_path, "panel")
    with pytest.raises(store_module.VariantSnapshotError, match="another PCB"):
        other.assembly_rows(snapshot, "A")
    with pytest.raises(native.StaleVariantTarget, match="unavailable"):
        store.assembly_rows(snapshot, "removed")


@pytest.mark.parametrize("operation", ["rows", "output", "display", "counter"])
def test_save_as_invalidates_old_board_paths(tmp_path: Path, operation: str) -> None:
    store = _store(tmp_path)
    snapshot = _snapshot(store)
    store.board.filename = tmp_path / "renamed.kicad_pcb"
    actions = {
        "rows": lambda: store.assembly_rows(snapshot, "A"),
        "output": lambda: store.set_output_variant("A"),
        "display": store.get_display_preferences,
        "counter": store.increment_generation_count,
    }
    with pytest.raises(store_module.VariantSnapshotError, match="reopen"):
        actions[operation]()
    assert not Path(store.dbfile).exists()


def test_output_and_layout_persist_per_board_and_return_independent_copies(
    tmp_path: Path,
) -> None:
    first = _store(tmp_path)
    second = _store(tmp_path, "panel")
    assert first.get_output_variant() is None
    first.set_output_variant("")
    first.set_display_preferences({"order": ["B", "", "A"]})
    second.set_output_variant("A")
    reopened = _store(tmp_path)
    assert reopened.get_output_variant() == ""
    copied = reopened.get_display_preferences()
    copied["order"].clear()
    assert reopened.get_display_preferences() == {"order": ["B", "", "A"]}
    assert _store(tmp_path, "panel").get_output_variant() == "A"
    # A removed canonical name is retained for the session to display unavailable.
    reopened.set_output_variant("removed")
    assert _store(tmp_path).get_output_variant() == "removed"
    old_identity = first.board_id
    first.board.filename = tmp_path / "saved-as.kicad_pcb"
    with pytest.raises(store_module.VariantSnapshotError, match="reopen"):
        first.get_output_variant()
    saved_as = _store(tmp_path, "saved-as")
    assert saved_as.board_id != old_identity
    assert saved_as.get_output_variant() is None
    assert saved_as.get_display_preferences() == {}
    assert _store(tmp_path).get_display_preferences() == {"order": ["B", "", "A"]}
    assert _store(tmp_path).get_output_variant() == "removed"
    assert not Path(first.dbfile).exists()


def test_same_board_display_edits_merge_from_effective_startup_states(
    tmp_path: Path,
) -> None:
    """Closing stale controls saves only the settings actually changed there."""
    before = {
        "differences_only": False,
        "variant_order": ["", "A", "B"],
        "widths": {"value": 100, "lcsc": 100},
    }
    _store(tmp_path).set_display_preferences(before)
    first, second = _store(tmp_path), _store(tmp_path)
    first.set_display_preferences(
        {**before, "differences_only": True, "widths": {"value": 140, "lcsc": 100}},
        before=before,
    )
    second.set_display_preferences(
        {
            **before,
            "variant_order": ["", "B", "A"],
            "widths": {"value": 100, "lcsc": 170},
        },
        before=before,
    )
    first.set_output_variant("B")
    second.parent.save_settings()
    reopened = _store(tmp_path)
    assert reopened.get_output_variant() == "B"
    assert reopened.get_display_preferences() == {
        "differences_only": True,
        "variant_order": ["", "B", "A"],
        "widths": {"value": 140, "lcsc": 170},
    }


def test_unchanged_implicit_display_defaults_preserve_newer_saved_preferences(
    tmp_path: Path,
) -> None:
    """Defaults absent from JSON are not user edits when a stale window closes."""
    first, second = _store(tmp_path), _store(tmp_path)
    implicit = {"variant_order": ["", "A", "B"], "differences_only": False}
    first.set_display_preferences(
        {"variant_order": ["", "B", "A"], "differences_only": True},
        before=implicit,
    )
    second.set_display_preferences(implicit, before=implicit)
    assert _store(tmp_path).get_display_preferences() == {
        "variant_order": ["", "B", "A"],
        "differences_only": True,
    }


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("kind", ["output", "display"])
def test_failed_preference_save_restores_settings(
    tmp_path: Path,
    existing: bool,
    kind: str,
) -> None:
    store = _store(tmp_path)
    if existing:
        store.set_output_variant("B")
    before = json.loads(json.dumps(store.parent.settings))

    def fail(variant_patch: Any = None) -> None:
        raise OSError("settings unavailable")

    store.parent.save_settings = fail
    with pytest.raises(OSError, match="settings unavailable"):
        if kind == "output":
            store.set_output_variant("A")
        else:
            store.set_display_preferences({"differences_only": True})
    assert store.parent.settings == before


@pytest.mark.parametrize(
    "value",
    [{"variants": []}, {"variants": {"boards": []}}],
)
def test_invalid_preferences_fail_without_overwriting_data(
    tmp_path: Path,
    value: dict[str, Any],
) -> None:
    store = _store(tmp_path)
    store.parent.settings = value
    before = json.loads(json.dumps(value))
    with pytest.raises(store_module.VariantSnapshotError, match="invalid"):
        store.get_output_variant()
    assert value == before


def test_failed_generation_counter_update_rolls_back_without_new_metadata(
    tmp_path: Path,
) -> None:
    """A persistence error cannot mark a failed counter write as completed."""
    store = _store(tmp_path)
    Path(store.datadir).mkdir()
    with closing(sqlite3.connect(store.dbfile)) as connection, connection:
        connection.execute(
            "CREATE TABLE part_info (reference TEXT PRIMARY KEY, lcsc TEXT)"
        )
    assert store.get_generation_count() == 0
    with closing(sqlite3.connect(store.dbfile)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("part_info",)]
    assert store.increment_generation_count() == 1
    with closing(sqlite3.connect(store.dbfile)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_counter BEFORE UPDATE ON metadata "
            "BEGIN SELECT RAISE(ABORT, 'counter unavailable'); END"
        )
        before = tuple(connection.iterdump())
    with pytest.raises(sqlite3.DatabaseError, match="counter unavailable"):
        store.increment_generation_count()
    with closing(sqlite3.connect(store.dbfile)) as connection:
        assert tuple(connection.iterdump()) == before
    assert store.get_generation_count() == 1


def test_concurrent_counter_updates_share_existing_directory_scope(
    tmp_path: Path,
) -> None:
    """Two open boards cannot lose a successful generation increment."""
    first = _store(tmp_path)
    second = _store(tmp_path, "panel")
    assert first.get_generation_count() == second.get_generation_count() == 0
    assert not Path(first.dbfile).exists()
    ready = Barrier(2)

    def generate(store: Any) -> int:
        ready.wait(timeout=5)
        return store.increment_generation_count()

    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(generate, first)
        right = executor.submit(generate, second)
        assert sorted((left.result(timeout=5), right.result(timeout=5))) == [1, 2]
    assert first.get_generation_count() == second.get_generation_count() == 2
    with closing(sqlite3.connect(first.dbfile)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("metadata",)]
        assert connection.execute("SELECT * FROM metadata").fetchall() == [
            ("generation_count", "2")
        ]
