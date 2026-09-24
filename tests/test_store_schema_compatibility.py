"""Native part reads preserve legacy databases used by impedance projects."""

from pathlib import Path
import sqlite3
from types import ModuleType
from typing import Any

import pytest

from .store_board_support import Board, make_store, store_module as _store_module

store_module = _store_module


def _open_store(module: ModuleType, project: Path) -> Any:
    """Open the real board-backed adapter without initializing project storage."""
    return make_store(module, project, Board())


@pytest.mark.parametrize("include_estimator_columns", [False, True])
def test_opening_keeps_scoped_legacy_parts_untouched(
    tmp_path: Path, store_module: ModuleType, include_estimator_columns: bool
) -> None:
    """Old scoped assignments are ignored and remain intact on successful startup."""
    dbfile = tmp_path / "jlcpcb" / "project.db"
    dbfile.parent.mkdir()
    with sqlite3.connect(dbfile) as connection:
        connection.execute(
            "CREATE TABLE part_info (board_id TEXT NOT NULL, reference TEXT NOT NULL, "
            "value TEXT NOT NULL, footprint TEXT NOT NULL, lcsc TEXT, stock NUMERIC, "
            "exclude_from_bom NUMERIC DEFAULT 0, exclude_from_pos NUMERIC DEFAULT 0, "
            "PRIMARY KEY(board_id, reference))"
        )
        if include_estimator_columns:
            for column, kind in {
                "pad_count": "INTEGER",
                "has_tht": "NUMERIC",
                "assembly_process": "TEXT",
                "component_product_type": "INTEGER",
                "assembly_flags": "TEXT",
            }.items():
                connection.execute(f"ALTER TABLE part_info ADD COLUMN {column} {kind}")
        connection.executemany(
            "INSERT INTO part_info (board_id, reference, value, footprint, lcsc) "
            "VALUES (?, 'R1', '10k', 'R_0603', ?)",
            [("board-a", "C100"), ("board-b", "C200")],
        )
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('generation_count', '7')")
    before = dbfile.read_bytes()

    store = _open_store(store_module, tmp_path)
    assert store.read_all() == []
    assert store.get_generation_count() == 7

    assert dbfile.read_bytes() == before


def test_normal_store_keeps_project_counter_without_creating_parts_table(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Only explicit counter updates create storage; assignments stay on the PCB."""
    store = _open_store(store_module, tmp_path)
    assert store.read_all() == []
    assert store.get_generation_count() == 0
    assert not Path(store.dbfile).exists()
    assert not Path(store.datadir).exists()
    assert store.increment_generation_count() == 1
    reopened = _open_store(store_module, tmp_path)
    assert reopened.get_generation_count() == 1
    with sqlite3.connect(store.dbfile) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == {"metadata"}
