"""Board snapshots replace project part rows without changing BOM semantics."""

from collections.abc import Callable
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import types
from typing import Any, Optional
from unittest.mock import Mock

import pytest

from .part_preferences_test_support import Board, Footprint
from .wx_harness import load, package_stubs, wx_stubs


@pytest.fixture
def make_store(tmp_path: Path) -> Callable[..., Any]:
    """Construct real Store readers over stateful board fields and cache results."""
    package = "_store_board_data_tests"
    module = load(package, "store", {**package_stubs(package), **wx_stubs()})

    def make(
        footprints: list[Footprint],
        *,
        metadata: Optional[dict[str, dict[str, Any]]] = None,
        project_path: Optional[Path] = None,
    ) -> Any:
        cached = metadata if metadata is not None else {}
        library = types.SimpleNamespace(
            get_lcsc_metadata=lambda codes: {
                code: dict(cached[code]) for code in codes if code in cached
            }
        )
        return module.Store(
            types.SimpleNamespace(library=library, settings={}),
            str(project_path if project_path is not None else tmp_path),
            Board(footprints),
        )

    return make


def test_reads_track_external_edits_rename_and_deletion(
    make_store: Callable[..., Any],
) -> None:
    """Each read derives every board field from the current footprint state."""
    fp = Footprint("R1", lcsc="C100")
    pads = [types.SimpleNamespace(HasHole=lambda: False)]
    fp.Pads = lambda: pads
    store = make_store([fp])
    initial = store.read_all()[0]
    assert initial["lcsc"] == "C100"
    assert initial["pad_count"] == 1 and initial["has_tht"] is False
    assert json.loads(initial["assembly_flags"])["is_dnp"] is False

    fp.SetField("LCSC", "C200")
    fp.value = "22k"
    fp.footprint = "R_0805"
    fp.attributes = (1 << 3) | (1 << 2)
    fp.dnp = True
    pads.extend(
        [
            types.SimpleNamespace(HasHole=lambda: True),
            types.SimpleNamespace(IsNPTH=lambda: True, HasHole=lambda: True),
        ]
    )
    row = store.read_all()[0]
    assert (row["lcsc"], row["value"], row["footprint"]) == ("C200", "22k", "R_0805")
    assert row["exclude_from_bom"] and row["exclude_from_pos"]
    assert row["is_dnp"] is True
    assert row["pad_count"] == 2 and row["has_tht"] is True
    assert json.loads(row["assembly_flags"])["is_dnp"] is True

    fp.SetField("LCSC", "")
    fp.reference = "R12"
    (row,) = store.read_all()
    assert (row["reference"], row["lcsc"]) == ("R12", "")
    store.board.footprints.clear()
    assert store.read_all() == []


def test_reads_need_no_project_directory_and_counter_initializes_only_metadata(
    make_store: Callable[..., Any],
    tmp_path: Path,
) -> None:
    """Board editing is independent of SQLite; generation data remains durable."""
    store = make_store([Footprint()])
    assert store.read_all()[0]["lcsc"] == "C100"
    assert not (tmp_path / "jlcpcb").exists()
    assert store.get_generation_count() == 0
    assert store.increment_generation_count() == 1
    assert make_store([]).get_generation_count() == 1
    with closing(sqlite3.connect(store.dbfile)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == [("metadata",)]


def test_failed_storage_and_missing_library_do_not_block_board_reads(
    make_store: Callable[..., Any],
    tmp_path: Path,
) -> None:
    """Board data survives unavailable project persistence and supplier cache."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    store = make_store([Footprint()], project_path=blocked)
    store.parent.library = None
    row = store.read_all()[0]
    assert row["reference"] == "R1" and row["assembly_process"] == ""
    assert row["component_product_type"] is None
    assert store.get_assembly_enrichment_targets() == {"C100": ["R1"]}
    with pytest.raises(OSError):
        store.increment_generation_count()


def test_reference_order_is_natural_and_invalid_references_are_filtered(
    make_store: Callable[..., Any],
) -> None:
    """Preserve the board reader's valid-reference rule and natural initial sort."""
    store = make_store(
        [Footprint(ref) for ref in ("R10", "R2", "R1", "", "?1", "U1'A", "R1', 'C9")]
    )
    expected = ["R1", "R1', 'C9", "R2", "R10", "U1'A"]
    assert [row["reference"] for row in store.read_all()] == expected
    del store.board.footprints["U1'A"]
    assert [row["reference"] for row in store.read_all()] == expected[:-1]


def test_bom_groups_like_existing_exports_and_accepts_a_snapshot(
    make_store: Callable[..., Any],
) -> None:
    """Group assigned value/code pairs, preserve unassigned rows and frozen inputs."""
    footprints = [
        Footprint("R2", lcsc="C100", footprint="R_0805"),
        Footprint("R10", lcsc="C100"),
        Footprint("R1", lcsc="C100"),
        Footprint("R3", lcsc="C100", value="22k"),
        Footprint("R4", lcsc=""),
        Footprint("R5", lcsc="", bom=True),
        Footprint("R6", lcsc="C100", bom=True),
        Footprint("R7", lcsc="", dnp=True),
    ]
    store = make_store(footprints)
    snapshot = store.read_all()
    expected = [
        {"value": "10k", "refs": "R1,R10,R2", "footprint": "R_0603", "lcsc": "C100"},
        {"value": "22k", "refs": "R3", "footprint": "R_0603", "lcsc": "C100"},
        {"value": "10k", "refs": "R4", "footprint": "R_0603", "lcsc": ""},
    ]
    assert store.read_bom_parts() == expected
    assert store.read_bom_parts(include_unassigned=False) == expected[:-1]
    footprints[0].SetField("LCSC", "C200")
    assert store.read_bom_parts(parts=snapshot) == expected
    assert store.read_all()[1]["lcsc"] == "C200"
    assert store.read_bom_parts(parts=[]) == []


def test_legacy_database_and_csv_are_ignored_and_untouched(
    make_store: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflicting legacy assignments cannot override KiCad or trigger migrations."""
    datadir = tmp_path / "jlcpcb"
    datadir.mkdir()
    dbfile = datadir / "project.db"
    with closing(sqlite3.connect(dbfile)) as connection, connection:
        connection.execute(
            "CREATE TABLE part_info (reference PRIMARY KEY, value, footprint, lcsc, stock, exclude_from_bom, exclude_from_pos)"
        )
        connection.executemany(
            "INSERT INTO part_info VALUES (?, '10k', 'R_0603', 'C999', 100, 1, 1)",
            [("R1",), ("C9",), ("U1'A",)],
        )
        connection.execute("CREATE TABLE correction (reference, rotation)")
        connection.execute("INSERT INTO correction VALUES ('R1', 90)")
        connection.execute("CREATE TABLE metadata (key PRIMARY KEY, value)")
        connection.execute("INSERT INTO metadata VALUES ('generation_count', '7')")
    csv = datadir / "part_assignments.csv"
    csv.write_text("R1,C888,1,1\nC9,C777,1,1\n", encoding="utf-8")
    backup = datadir / "part_assignments.csv.backup"
    backup.write_bytes(b"previous backup\r\n")
    before = {path: path.read_bytes() for path in (dbfile, csv, backup)}
    footprint = Footprint("R1", lcsc="C100")
    file_io = Mock(
        side_effect=AssertionError("Board reads must not open project files")
    )
    with monkeypatch.context() as guard:
        for target in ("builtins.open", "io.open", "sqlite3.connect"):
            guard.setattr(target, file_io)
        store = make_store([footprint])
        (row,) = store.read_all()
        assert (row["reference"], row["lcsc"]) == ("R1", "C100")
        assert not row["exclude_from_bom"] and not row["exclude_from_pos"]
        assert row["stock"] is None
        assert store.read_bom_parts()[0]["lcsc"] == "C100"
        footprint.SetField("LCSC", "")
        reopened = make_store([footprint])
        assert reopened.read_all()[0]["lcsc"] == ""
        assert reopened.read_bom_parts()[0]["lcsc"] == ""
    file_io.assert_not_called()
    assert {path: path.read_bytes() for path in before} == before
    assert sorted(path.name for path in datadir.iterdir()) == [
        "part_assignments.csv",
        "part_assignments.csv.backup",
        "project.db",
    ]
    assert store.get_generation_count() == 7
    assert store.increment_generation_count() == 8
    with closing(sqlite3.connect(dbfile)) as connection:
        assert connection.execute(
            "SELECT reference FROM part_info ORDER BY reference"
        ).fetchall() == [("C9",), ("R1",), ("U1'A",)]
        assert connection.execute("SELECT * FROM correction").fetchall() == [("R1", 90)]


def test_assignments_join_shared_metadata_by_current_lcsc(
    make_store: Callable[..., Any], tmp_path: Path
) -> None:
    """Current codes join shared data without inheriting the former assignment's class."""
    metadata: dict[str, dict[str, Any]] = {}
    first, second = Footprint("R1"), Footprint("R2")
    store = make_store([first, second], metadata=metadata)
    other_project = make_store(
        [Footprint("U1")], metadata=metadata, project_path=tmp_path / "other"
    )
    assert store.get_assembly_enrichment_targets() == {"C100": ["R1", "R2"]}
    metadata.update(
        {
            "C100": {"assembly_process": "SMT", "component_product_type": 0},
            "C200": {"assembly_process": "THT", "component_product_type": 2},
        }
    )
    assert store.get_assembly_enrichment_targets() == {}
    assert other_project.read_all()[0]["component_product_type"] == 0
    for code, process, classification in (
        ("C200", "THT", 2),
        ("", "", None),
        ("C100", "SMT", 0),
    ):
        first.SetField("LCSC", code)
        changed, unchanged = store.read_all()
        assert (changed["assembly_process"], changed["component_product_type"]) == (
            process,
            classification,
        )
        assert unchanged["assembly_process"] == "SMT"
        assert unchanged["component_product_type"] == 0


def test_enrichment_requires_both_fields_and_respects_reference_selection(
    make_store: Callable[..., Any],
) -> None:
    """Missing fields, invalid classes and shared codes produce current targets."""
    metadata = {
        "C1": {"assembly_process": "", "component_product_type": None},
        "C2": {"assembly_process": "SMT", "component_product_type": None},
        "C3": {"assembly_process": "", "component_product_type": 0},
        "C4": {"assembly_process": "SMT", "component_product_type": 0},
        "C5": {"assembly_process": "SMT", "component_product_type": 3},
        "C6": {"assembly_process": "SMT", "component_product_type": "bad"},
        "C7": {"assembly_process": "SMT", "component_product_type": 2},
        "C8": {"assembly_process": "SMT", "component_product_type": 1},
    }
    footprints = [
        Footprint(f"R{number}", lcsc=code) for number, code in enumerate(metadata, 1)
    ]
    footprints.extend([Footprint("R9", lcsc="C1"), Footprint("R10", lcsc="")])
    store = make_store(footprints, metadata=metadata)
    assert store.get_assembly_enrichment_targets() == {
        "C1": ["R1", "R9"],
        "C2": ["R2"],
        "C3": ["R3"],
        "C5": ["R5"],
        "C6": ["R6"],
    }
    assert store.get_assembly_enrichment_targets(["R9", "R4"]) == {"C1": ["R9"]}
    assert store.get_assembly_enrichment_targets([]) == {}
