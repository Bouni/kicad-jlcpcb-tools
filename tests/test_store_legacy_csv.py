"""Ignore obsolete assignment CSVs throughout real project synchronization."""

import builtins
from contextlib import closing
from pathlib import Path
import sqlite3
import types
from typing import Any

import pytest

from .wx_harness import load, package_stubs, wx_stubs

_PACKAGE = "_store_legacy_csv_tests"


class _Footprint:
    """Retain the board fields read by the real footprint and metadata helpers."""

    def __init__(
        self, reference: str = "R1", lcsc: str = "C100", attributes: int = 0
    ) -> None:
        self.reference = reference
        self.lcsc = lcsc
        self.attributes = attributes

    def GetReference(self) -> str:
        """Return the current board reference."""
        return self.reference

    def GetValue(self) -> str:
        """Return the component value."""
        return "10k"

    def GetFPID(self) -> Any:
        """Expose the footprint's library item name."""
        return types.SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def GetFields(self) -> list[Any]:
        """Read the current board assignment on every synchronization."""
        return [
            types.SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: self.lcsc)
        ]

    def GetAttributes(self) -> int:
        """Return the current BOM and position-file exclusion bits."""
        return self.attributes

    def Pads(self) -> list[object]:
        """Provide two ordinary surface-mount solder pads."""
        return [object(), object()]


class _Board:
    """Keep footprint additions and removals visible across refreshes."""

    def __init__(self, footprints: list[_Footprint]) -> None:
        self.footprints = footprints

    def GetFootprints(self) -> list[_Footprint]:
        """Return the current footprint collection."""
        return self.footprints


@pytest.fixture
def store_module() -> types.ModuleType:
    """Load real persistence and footprint helpers with unused GUI imports stubbed."""
    return load(_PACKAGE, "store", {**package_stubs(_PACKAGE), **wx_stubs()})


def _legacy_files(project: Path, contents: bytes) -> dict[Path, bytes]:
    """Create conflicting obsolete input and an independently preserved backup."""
    directory = project / "jlcpcb"
    directory.mkdir(exist_ok=True)
    files = {
        directory / "part_assignments.csv": contents,
        directory / "part_assignments.csv.backup": b"previous backup\r\n",
    }
    for path, content in files.items():
        path.write_bytes(content)
    return files


def _assert_files_untouched(files: dict[Path, bytes]) -> None:
    """Check both original names and bytes remain available."""
    for path, content in files.items():
        assert path.is_file(), f"Legacy file was moved or removed: {path.name}"
        assert path.read_bytes() == content


@pytest.mark.parametrize("lcsc", ["", "C100"])
@pytest.mark.parametrize("attributes", [0, 4, 8, 12])
def test_fresh_project_and_reopen_use_board_instead_of_assignment_csv(
    store_module: types.ModuleType, tmp_path: Path, lcsc: str, attributes: int
) -> None:
    """Obsolete IDs and exclusion flags cannot replace board values at startup."""
    bom, pos = bool(attributes & 8), bool(attributes & 4)
    files = _legacy_files(
        tmp_path, f"R1,C999,{int(not bom)},{int(not pos)}\r\n".encode()
    )
    board = _Board([_Footprint(lcsc=lcsc, attributes=attributes)])
    parent = types.SimpleNamespace(settings={})

    for _ in range(2):
        store = store_module.Store(parent, str(tmp_path), board)
        part = store.get_part("R1")
        assert (part["lcsc"], part["exclude_from_bom"], part["exclude_from_pos"]) == (
            lcsc,
            bom,
            pos,
        )
        assert part["pad_count"] == 2
        assert part["has_tht"] == 0
        assert [row["refs"] for row in store.read_bom_parts()] == (
            [] if bom else ["R1"]
        )
        _assert_files_untouched(files)


def test_assignment_csv_without_backup_stays_in_place_on_reopen(
    store_module: types.ModuleType, tmp_path: Path
) -> None:
    """Opening a CSV-only project leaves the source and creates no archive."""
    directory = tmp_path / "jlcpcb"
    directory.mkdir()
    source = directory / "part_assignments.csv"
    source.write_bytes(b"R1,C999,1,1\r\n")
    for _ in range(2):
        store_module.Store(
            types.SimpleNamespace(settings={}), str(tmp_path), _Board([_Footprint()])
        )
        _assert_files_untouched({source: b"R1,C999,1,1\r\n"})
        assert not (directory / "part_assignments.csv.backup").exists()


@pytest.mark.parametrize("board_priority", [False, True])
@pytest.mark.parametrize("board_lcsc", ["", "C100", "C200"])
def test_existing_sqlite_assignments_keep_normal_priority_and_enrichment(
    store_module: types.ModuleType,
    tmp_path: Path,
    board_priority: bool,
    board_lcsc: str,
) -> None:
    """Reopening applies LCSC precedence and invalidates enrichment only on changes."""
    footprint = _Footprint()
    board = _Board([footprint, _Footprint("R9")])
    parent = types.SimpleNamespace(
        settings={"general": {"lcsc_priority": board_priority}}
    )
    original = store_module.Store(parent, str(tmp_path), board)
    with closing(sqlite3.connect(original.dbfile)) as connection, connection:
        connection.execute(
            "UPDATE part_info SET stock = 57, assembly_process = 'SMT', "
            "component_product_type = 1 WHERE reference = 'R1'"
        )
    files = _legacy_files(tmp_path, b"R1,C999,1,1\nR9,C999,1,1\n")
    footprint.lcsc = board_lcsc
    board.footprints.remove(board.footprints[1])
    expected_lcsc = board_lcsc if board_priority and board_lcsc else "C100"
    changed = expected_lcsc != "C100"

    for _ in range(2):
        store = store_module.Store(parent, str(tmp_path), board)
        part = store.get_part("R1")
        assert part["lcsc"] == expected_lcsc
        assert part["stock"] == 57
        assert part["assembly_process"] == ("" if changed else "SMT")
        assert part["component_product_type"] == (None if changed else 1)
        assert (part["exclude_from_bom"], part["exclude_from_pos"]) == (0, 0)
        assert store.get_part("R9") is None
        _assert_files_untouched(files)


def test_csv_appearing_after_startup_does_not_override_board_refresh(
    store_module: types.ModuleType, tmp_path: Path
) -> None:
    """Refresh still updates flags, adds board parts, and removes stale DB rows."""
    footprint = _Footprint()
    board = _Board([footprint, _Footprint("R9")])
    store = store_module.Store(types.SimpleNamespace(settings={}), str(tmp_path), board)
    files = _legacy_files(tmp_path, b"R1,C999,0,0\nR2,C999,1,1\nR9,C999,1,1\n")
    footprint.attributes = 12
    footprint.lcsc = "C200"
    board.footprints = [footprint, _Footprint("R2", "C300")]

    for _ in range(2):
        store.update_from_board()
        part = store.get_part("R1")
        assert (part["lcsc"], part["exclude_from_bom"], part["exclude_from_pos"]) == (
            "C200",
            1,
            1,
        )
        assert store.get_part("R2")["lcsc"] == "C300"
        assert store.get_part("R9") is None
        assert [row["refs"] for row in store.read_bom_parts()] == ["R2"]
        _assert_files_untouched(files)


@pytest.mark.parametrize("contents", [b"R1,C999\n", b"R1,C999,broken,0\n", b"\xff"])
def test_invalid_assignment_csv_does_not_block_startup_or_cleanup(
    store_module: types.ModuleType, tmp_path: Path, contents: bytes
) -> None:
    """Malformed and undecodable files remain inert through reopen and cleanup."""
    board = _Board([_Footprint(), _Footprint("R9")])
    parent = types.SimpleNamespace(settings={})
    store_module.Store(parent, str(tmp_path), board)
    files = _legacy_files(tmp_path, contents)
    board.footprints.pop()

    for _ in range(2):
        store = store_module.Store(parent, str(tmp_path), board)
        assert store.get_part("R1")["lcsc"] == "C100"
        assert store.get_part("R9") is None
        _assert_files_untouched(files)


def test_unreadable_assignment_csv_is_never_opened(
    store_module: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission failure in an obsolete file cannot interrupt project loading."""
    files = _legacy_files(tmp_path, b"R1,C999,1,1\n")
    csv_file = tmp_path / "jlcpcb" / "part_assignments.csv"
    real_open = builtins.open

    def deny_legacy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if file == str(csv_file) or file == csv_file:
            raise PermissionError("Obsolete assignment CSV is unreadable")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_legacy_open)
    for _ in range(2):
        store = store_module.Store(
            types.SimpleNamespace(settings={}), str(tmp_path), _Board([_Footprint()])
        )
        assert store.get_part("R1")["lcsc"] == "C100"
        _assert_files_untouched(files)


def test_old_sqlite_schema_still_upgrades_while_legacy_csv_is_ignored(
    store_module: types.ModuleType, tmp_path: Path
) -> None:
    """CSV retirement keeps the existing SQLite metadata upgrade on reopening."""
    files = _legacy_files(tmp_path, b"R1,C999,1,1\n")
    with closing(sqlite3.connect(tmp_path / "jlcpcb" / "project.db")) as con, con:
        con.execute(
            "CREATE TABLE part_info (reference PRIMARY KEY, value TEXT, footprint TEXT, "
            "lcsc TEXT, stock NUMERIC, exclude_from_bom NUMERIC, exclude_from_pos NUMERIC)"
        )
        con.execute("INSERT INTO part_info VALUES ('R1','10k','R_0603','C100',57,0,0)")

    for _ in range(2):
        store = store_module.Store(
            types.SimpleNamespace(settings={}), str(tmp_path), _Board([_Footprint()])
        )
        part = store.get_part("R1")
        assert (part["lcsc"], part["stock"], part["pad_count"]) == ("C100", 57, 2)
        assert store.get_generation_count() == 0
        assert part["has_tht"] == 0
        _assert_files_untouched(files)
