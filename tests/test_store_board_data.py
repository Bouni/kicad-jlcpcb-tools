"""Ordinary mappings follow native PCB state without project assignment storage."""

from contextlib import closing
from pathlib import Path
import sqlite3
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from .store_board_support import (
    Board,
    Footprint,
    make_store,
    store_module as _store_module,
)

store_module = _store_module


def seed_legacy(path: Path) -> Path:
    """Create a conflicting old assignment using its historical on-disk schema."""
    directory = path / "jlcpcb"
    directory.mkdir(exist_ok=True)
    database = directory / "project.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE part_info (reference PRIMARY KEY, value TEXT, footprint TEXT, "
            "lcsc TEXT, stock NUMERIC, exclude_from_bom NUMERIC, exclude_from_pos NUMERIC)"
        )
        connection.execute(
            "INSERT INTO part_info VALUES ('R1','10k','R_0603','C999',57,0,0)"
        )
    return database


@pytest.mark.parametrize("lcsc", ["C100", ""])
@pytest.mark.parametrize("priority", [False, True])
def test_legacy_assignment_never_replaces_native_mapping(
    store_module: ModuleType, tmp_path: Path, lcsc: str, priority: bool
) -> None:
    """Populated and intentionally blank PCB fields win on every reopening."""
    path = seed_legacy(tmp_path)
    before = path.read_bytes()
    board = Board(Footprint(lcsc=lcsc))
    for _ in range(2):
        store = make_store(
            store_module, tmp_path, board, general={"lcsc_priority": priority}
        )
        assert store.get_part("R1")["lcsc"] == lcsc
        assert path.read_bytes() == before


def test_part_reads_do_not_open_or_create_project_storage(
    store_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing or inaccessible project DB cannot block native part reads."""
    connect = Mock(side_effect=AssertionError("part reads must not use SQLite"))
    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    store = make_store(store_module, tmp_path, Board(Footprint()))
    assert store.read_all()[0]["lcsc"] == "C100"
    assert store.get_part("R1")["lcsc"] == "C100"
    assert store.read_bom_parts()[0]["lcsc"] == "C100"
    assert not (tmp_path / "jlcpcb").exists()
    connect.assert_not_called()


def test_reads_follow_live_edits_rename_and_deletion(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """Every read follows PCB edits without an importer or synchronization call."""
    footprint = Footprint()
    board = Board(footprint)
    store = make_store(store_module, tmp_path, board)
    original = store.read_all()
    footprint.lcsc = ""
    footprint.reference = "R2"
    footprint.value = "22k"
    footprint.footprint = "R_0805"
    footprint.attributes = 12
    footprint.dnp = True
    assert store.get_part("R1") is None
    row = store.get_part("R2")
    assert (row["lcsc"], row["value"], row["footprint"]) == ("", "22k", "R_0805")
    assert row["exclude_from_bom"] and row["exclude_from_pos"] and row["is_dnp"]
    assert original[0]["reference"] == "R1" and original[0]["lcsc"] == "C100"
    board.footprints.clear()
    assert store.read_all() == []
    assert store.get_part("R2") is None


def test_boards_in_one_directory_keep_distinct_assignments(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """Matching references and an empty second board cannot affect the first PCB."""
    first_board = Board(Footprint("R1", "C100"), Footprint("R2", "C200"))
    first = make_store(store_module, tmp_path, first_board)
    second_board = Board(Footprint("R1", "C300"))
    second = make_store(store_module, tmp_path, second_board)
    assert [row["lcsc"] for row in first.read_all()] == ["C100", "C200"]
    assert second.get_part("R1")["lcsc"] == "C300"
    second_board.footprints.clear()
    assert second.read_all() == []
    assert first.get_part("R2")["lcsc"] == "C200"


def test_parent_board_accessor_is_rechecked_for_every_read(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """A validated live-board accessor takes precedence over the original wrapper."""
    store = make_store(store_module, tmp_path, Board(Footprint()))
    current = Board(Footprint("R9", "C900"))
    store.parent._get_current_board = lambda: current
    assert store.get_part("R1") is None
    assert store.get_part("R9")["lcsc"] == "C900"
    store.parent._get_current_board = Mock(side_effect=RuntimeError("board changed"))
    with pytest.raises(RuntimeError, match="board changed"):
        store.read_all()


def test_bom_groups_one_captured_native_snapshot_and_filters_population(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """BOM preparation preserves captured IDs, flags and values after later edits."""
    footprints = [Footprint(ref) for ref in ("R10", "R2", "R1", "R3", "R4", "R5")]
    footprints[2].value = "22k"
    footprints[3].lcsc = ""
    footprints[4].attributes = 8
    footprints[5].dnp = True
    store = make_store(store_module, tmp_path, Board(*footprints))
    snapshot = store.read_all()
    assert [row["reference"] for row in snapshot] == [
        "R1",
        "R2",
        "R3",
        "R4",
        "R5",
        "R10",
    ]
    expected = [
        {"value": "10k", "footprint": "R_0603", "lcsc": "C100", "refs": "R10,R2"},
        {"value": "22k", "footprint": "R_0603", "lcsc": "C100", "refs": "R1"},
        {"value": "10k", "footprint": "R_0603", "lcsc": "", "refs": "R3"},
    ]
    assert store.read_bom_parts() == expected
    footprints[0].lcsc = "C200"
    footprints[1].dnp = True
    assert store.read_bom_parts(parts=snapshot) == expected
    assert (
        store.read_bom_parts(parts=snapshot, include_unassigned=False) == expected[:-1]
    )
    assert store.read_bom_parts(parts=[]) == []
    assert store.read_bom_parts() != expected


def test_duplicate_native_references_cannot_silently_collapse_bom_rows(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """The old unique SQLite key cannot hide ambiguous native references anymore."""
    store = make_store(store_module, tmp_path, Board(Footprint(), Footprint()))
    with pytest.raises(ValueError, match="Duplicate footprint reference R1"):
        store.read_bom_parts()


@pytest.mark.parametrize("native_finder", [True, False])
def test_single_part_lookup_does_not_extract_unrelated_native_parts(
    store_module: ModuleType, tmp_path: Path, native_finder: bool
) -> None:
    """Reading one live row never scans the pad geometry of unrelated footprints."""
    footprints = [Footprint(f"R{index}") for index in range(1, 49)]
    board = Board(*footprints)
    source = (
        board if native_finder else SimpleNamespace(GetFootprints=board.GetFootprints)
    )
    store = make_store(store_module, tmp_path, source)
    target = footprints[23]
    assert store.get_part("R24")["lcsc"] == "C100"
    assert board.inventory_reads == (0 if native_finder else 1)
    assert target.pad_reads == 1
    assert sum(fp.pad_reads for fp in footprints) == 1
    assert board.reference_lookups == (["R24"] if native_finder else [])

    target.lcsc = "C200"
    assert store.get_part("R24")["lcsc"] == "C200"
    assert store.get_part("R99") is None
    assert target.pad_reads == 2
    assert sum(fp.pad_reads for fp in footprints) == 2
