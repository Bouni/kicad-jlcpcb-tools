"""Removing named variants cannot restore obsolete ordinary-mode assignments."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
import sqlite3
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .variant_data_support import SavedNativeBoard, _native_session
from .variant_native_support import Footprint, native
from .wx_harness import load_siblings, package_stubs, wx_stubs


class TransitionFootprint(Footprint):
    """Expose both ordinary and variant readers over the same native state."""

    def GetFPID(self) -> SimpleNamespace:
        """Return the library identifier used by ordinary-mode board reads."""
        return SimpleNamespace(GetLibItemName=lambda: "R0603")


@pytest.fixture
def ordinary_store_module() -> Iterator[ModuleType]:
    """Load the actual Store and footprint readers with only wx imports isolated."""
    package = "_board_mapping_transition_tests"
    with load_siblings(
        package, ("store",), {**package_stubs(package), **wx_stubs()}
    ) as modules:
        yield modules["store"]


def _seed_legacy_assignments(project: Path) -> Path:
    """Represent a pre-upgrade project whose assignments initially match its PCB."""
    path = project / "jlcpcb" / "project.db"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE part_info (reference TEXT PRIMARY KEY, value TEXT, "
            "footprint TEXT, lcsc TEXT, stock NUMERIC, exclude_from_bom NUMERIC, "
            "exclude_from_pos NUMERIC, pad_count INTEGER, has_tht NUMERIC, "
            "assembly_process TEXT, component_product_type INTEGER, assembly_flags TEXT)"
        )
        connection.executemany(
            "INSERT INTO part_info VALUES "
            "(?, '10k', 'R0603', ?, 100, 0, 0, 2, 0, 'SMT', 0, ?)",
            [
                (
                    reference,
                    lcsc,
                    '{"exclude_from_bom": false, "exclude_from_pos": false, "is_dnp": false}',
                )
                for reference, lcsc in (("R1", "C100"), ("R2", "C300"))
            ],
        )
    return path


def _legacy_rows(path: Path) -> list[tuple[Any, ...]]:
    """Read retained legacy state independently of either assignment store."""
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT * FROM part_info ORDER BY reference"
        ).fetchall()


def _assignments(store: Any) -> dict[str, str]:
    """Read the assignments exposed to ordinary-mode consumers."""
    return {part["reference"]: part["lcsc"] for part in store.read_all()}


@pytest.mark.parametrize("edited", ["", "C200"], ids=["clear", "replace"])
@pytest.mark.parametrize("legacy_priority", [False, True], ids=["old-db", "old-pcb"])
def test_default_assignment_survives_removing_all_named_variants(
    tmp_path: Path,
    ordinary_store_module: ModuleType,
    edited: str,
    legacy_priority: bool,
) -> None:
    """Ordinary → native variants → ordinary retains Default, including a blank."""
    board = SavedNativeBoard(tmp_path)
    board.names.clear()
    board.current = ""
    board.parts = [
        TransitionFootprint(board, "component-1", "R1"),
        TransitionFootprint(board, "component-2", "R2"),
    ]
    board.parts[0].SetField("LCSC", "C100")
    board.parts[1].SetField("LCSC", "C300")
    database = _seed_legacy_assignments(tmp_path)
    legacy = _legacy_rows(database)
    parent = SimpleNamespace(settings={"general": {"lcsc_priority": legacy_priority}})
    ordinary = ordinary_store_module.Store(parent, str(tmp_path), board)
    assert _assignments(ordinary) == {"R1": "C100", "R2": "C300"}

    # KiCad adds a named assembly; a fresh variant session edits the same PCB.
    board.names.append("A")
    _, session, cache, _, _ = _native_session(tmp_path, board=board)
    session.apply(
        (
            native.VariantEdit(
                session.snapshot.target("component-1", "A"), (("lcsc", "C900"),)
            ),
        )
    )
    session.apply(
        (
            native.VariantEdit(
                session.snapshot.target("component-1", ""), (("lcsc", edited),)
            ),
        )
    )
    assert session.snapshot.get("component-1", "").lcsc == edited
    assert session.snapshot.get("component-1", "A").lcsc == "C900"
    assert cache.assembly_rows(session.snapshot, "")[0]["lcsc"] == edited
    assert _legacy_rows(database) == legacy

    # Removing every named variant selects ordinary mode on the next opening.
    board.names.clear()
    for footprint in board.parts:
        footprint.DeleteVariant("A")
    for _ in range(2):
        reopened = ordinary_store_module.Store(parent, str(tmp_path), board)
        assert _assignments(reopened) == {"R1": edited, "R2": "C300"}
        assert board.parts[0].GetField("LCSC").GetText() == edited
        assert _legacy_rows(database) == legacy


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"LCSC": "", "JLCPCB": "C100"}, ""),
        ({"LCSC": "C100", "JLCPCB": "C200"}, ""),
        ({"JLCPCB Customer ID": "C100"}, ""),
        ({"LCSC": " c100 ", "JLCPCB Part Number": "C100"}, "C100"),
        ({"LCSC": "", "JLCPCB": ""}, ""),
    ],
    ids=[
        "canonical-blank-with-stale-alias",
        "conflicting-assignments",
        "unrelated-customer-id",
        "matching-normalized-aliases",
        "explicit-blank-aliases",
    ],
)
def test_default_alias_resolution_is_unchanged_by_adding_or_removing_variants(
    tmp_path: Path,
    ordinary_store_module: ModuleType,
    fields: dict[str, str],
    expected: str,
) -> None:
    """Changing UI modes never chooses a different part from unchanged PCB fields."""
    board = SavedNativeBoard(tmp_path)
    board.names.clear()
    board.current = ""
    footprint = TransitionFootprint(board, "component-1", "R1")
    footprint.fields.pop("LCSC")
    footprint.fields.update(fields)
    footprint.fields["JLCPCB Rotation"] = "90"
    footprint.fields["JLC Description"] = "Retain supplier description"
    board.parts = [footprint]
    original_fields = dict(footprint.fields)
    parent = SimpleNamespace(settings={})

    ordinary = ordinary_store_module.Store(parent, str(tmp_path), board)
    resolved = [_assignments(ordinary)["R1"]]
    assert footprint.fields == original_fields

    board.names.append("A")
    _, session, cache, _, _ = _native_session(tmp_path, board=board)
    resolved.append(session.snapshot.get("component-1", "").lcsc)
    assert session.snapshot.get("component-1", "A").lcsc == expected
    assert cache.assembly_rows(session.snapshot, "")[0]["lcsc"] == expected
    assert footprint.fields == original_fields
    assert not footprint.variants

    board.names.clear()
    reopened = ordinary_store_module.Store(parent, str(tmp_path), board)
    resolved.append(_assignments(reopened)["R1"])

    assert footprint.fields == original_fields
    assert resolved == [expected, expected, expected]
    assert not Path(reopened.dbfile).exists()
