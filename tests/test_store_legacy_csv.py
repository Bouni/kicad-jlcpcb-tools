"""Obsolete assignment files remain inert through live reads and reopening."""

import builtins
from contextlib import closing
from pathlib import Path
import sqlite3
from types import ModuleType
from typing import Any

import pytest

from .store_board_support import (
    Board,
    Footprint,
    make_store,
    store_module as _store_module,
)

store_module = _store_module


def legacy_files(project: Path, contents: bytes) -> dict[Path, bytes]:
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


def assert_files_untouched(files: dict[Path, bytes]) -> None:
    """Require original names and contents to remain available for recovery."""
    for path, content in files.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("lcsc", ["", "C100"])
@pytest.mark.parametrize("attributes", [0, 4, 8, 12])
def test_fresh_project_and_reopen_use_board_instead_of_assignment_csv(
    store_module: ModuleType, tmp_path: Path, lcsc: str, attributes: int
) -> None:
    """Obsolete IDs and exclusion flags cannot replace board values at startup."""
    files = legacy_files(tmp_path, b"R1,C999,1,1\r\n")
    footprint = Footprint(lcsc=lcsc)
    footprint.attributes = attributes
    board = Board(footprint)
    for _ in range(2):
        store = make_store(store_module, tmp_path, board)
        part = store.get_part("R1")
        assert (part["lcsc"], part["exclude_from_bom"], part["exclude_from_pos"]) == (
            lcsc,
            bool(attributes & 8),
            bool(attributes & 4),
        )
        assert [row["refs"] for row in store.read_bom_parts()] == (
            [] if attributes & 8 else ["R1"]
        )
        assert_files_untouched(files)
        assert not Path(store.dbfile).exists()


def test_csv_appearing_after_startup_never_overrides_native_edits(
    store_module: ModuleType, tmp_path: Path
) -> None:
    """New obsolete files cannot hide changed flags, added parts or removals."""
    footprint = Footprint()
    board = Board(footprint, Footprint("R9"))
    store = make_store(store_module, tmp_path, board)
    files = legacy_files(tmp_path, b"R1,C999,0,0\nR2,C999,1,1\nR9,C999,1,1\n")
    footprint.attributes, footprint.lcsc = 12, "C200"
    board.footprints = [footprint, Footprint("R2", "C300")]
    assert store.get_part("R1")["exclude_from_bom"]
    assert store.get_part("R1")["exclude_from_pos"]
    assert store.get_part("R1")["lcsc"] == "C200"
    assert store.get_part("R9") is None
    assert [row["refs"] for row in store.read_bom_parts()] == ["R2"]
    assert_files_untouched(files)


@pytest.mark.parametrize("contents", [b"R1,C999\n", b"R1,C999,broken,0\n", b"\xff"])
@pytest.mark.parametrize("database_state", ["corrupt", "malformed-table", "missing"])
def test_malformed_legacy_files_cannot_block_open_or_native_reads(
    store_module: ModuleType, tmp_path: Path, contents: bytes, database_state: str
) -> None:
    """Unknown assignment schemas and invalid bytes never trigger reads or repairs."""
    files = legacy_files(tmp_path, contents)
    path = tmp_path / "jlcpcb" / "project.db"
    if database_state == "corrupt":
        path.write_bytes(b"not a sqlite database")
    elif database_state == "malformed-table":
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE part_info (unrelated BLOB)")
    if path.exists():
        files[path] = path.read_bytes()
    board = Board(Footprint(), Footprint("R9"))
    for _ in range(2):
        store = make_store(store_module, tmp_path, board)
        assert store.get_part("R1")["lcsc"] == "C100"
        board.footprints = board.footprints[:1]
        assert store.get_part("R9") is None
        assert_files_untouched(files)


def test_unreadable_assignment_csv_without_backup_is_never_opened_or_archived(
    store_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable obsolete files cannot interrupt opening or create backup files."""
    directory = tmp_path / "jlcpcb"
    directory.mkdir()
    source = directory / "part_assignments.csv"
    source.write_bytes(b"R1,C999,1,1\r\n")
    real_open = builtins.open

    def deny_legacy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        """Reject attempts to import a legacy CSV."""
        if str(file) == str(source):
            raise PermissionError("Obsolete assignment CSV is unreadable")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_legacy_open)
    for _ in range(2):
        assert (
            make_store(store_module, tmp_path, Board(Footprint())).get_part("R1")[
                "lcsc"
            ]
            == "C100"
        )
        assert source.read_bytes() == b"R1,C999,1,1\r\n"
        assert list(directory.iterdir()) == [source]
