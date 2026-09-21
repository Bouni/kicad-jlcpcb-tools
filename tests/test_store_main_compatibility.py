"""Keep ordinary project persistence unchanged by native variant support."""

from contextlib import closing
from pathlib import Path
import sqlite3
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .variant_data_support import _store as variant_store
from .wx_harness import load, package_stubs, wx_stubs

_PACKAGE = "_store_main_compatibility_tests"


@pytest.fixture
def store_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Use the real Store and SQLite with only GUI and footprint adapters stubbed."""
    module = load(_PACKAGE, "store", {**package_stubs(_PACKAGE), **wx_stubs()})
    monkeypatch.setattr(
        module, "get_valid_footprints", lambda board: board.GetFootprints()
    )
    monkeypatch.setattr(module, "get_lcsc_value", lambda footprint: "")
    monkeypatch.setattr(module, "get_exclude_from_bom", lambda footprint: False)
    monkeypatch.setattr(module, "get_exclude_from_pos", lambda footprint: False)
    monkeypatch.setattr(
        module, "get_footprint_pad_metadata", lambda footprint: (2, False)
    )
    monkeypatch.setattr(module, "get_assembly_flags", lambda footprint: "[]")
    return module


def _store(module: ModuleType, project: Path, name: str = "board") -> Any:
    """Run the actual constructor and ordinary synchronization."""
    filename = project / f"{name}.kicad_pcb"
    filename.write_text("(kicad_pcb)\n", encoding="utf-8")
    footprint = SimpleNamespace(
        GetReference=lambda: "R1",
        GetValue=lambda: "10k",
        GetFPID=lambda: SimpleNamespace(GetLibItemName=lambda: "R_0603"),
    )
    board = SimpleNamespace(
        GetFileName=lambda: str(filename), GetFootprints=lambda: [footprint]
    )
    return module.Store(SimpleNamespace(settings={}), str(project), board)


def _metadata(store: Any) -> list[tuple[str, str]]:
    """Read the durable key/value format independently of the Store APIs."""
    with closing(sqlite3.connect(store.dbfile)) as connection:
        return connection.execute(
            "SELECT key, value FROM metadata ORDER BY key"
        ).fetchall()


def test_ordinary_schema_has_no_variant_columns_tables_or_metadata(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Opening ordinary mode retains main's exact tables and assignment columns."""
    store = _store(store_module, tmp_path)

    with closing(sqlite3.connect(store.dbfile)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        columns = connection.execute("PRAGMA table_info(part_info)").fetchall()
    assert tables == [("metadata",), ("part_info",)]
    assert [row[1] for row in columns] == [
        "reference",
        "value",
        "footprint",
        "lcsc",
        "stock",
        "exclude_from_bom",
        "exclude_from_pos",
        "pad_count",
        "has_tht",
        "assembly_process",
        "component_product_type",
        "assembly_flags",
    ]
    assert [row[1] for row in columns if row[5]] == ["reference"]
    assert _metadata(store) == []


def test_ordinary_and_variant_generation_share_only_the_existing_counter(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Cross-mode generations preserve the ordinary schema, assignments and metadata."""
    first = _store(store_module, tmp_path, "first")
    assert first.increment_generation_count() == 1
    second = _store(store_module, tmp_path, "second")
    assert second.get_generation_count() == 1
    assert second.increment_generation_count() == 2
    with closing(sqlite3.connect(first.dbfile)) as connection, connection:
        connection.execute("UPDATE part_info SET lcsc = 'C999'")
        connection.execute("INSERT INTO metadata VALUES ('unrelated', 'keep')")
        connection.execute("CREATE TABLE unrelated_plugin (payload BLOB)")
        connection.execute(
            "INSERT INTO unrelated_plugin VALUES (?)", (b"preserve-exactly",)
        )
        schema = connection.execute(
            "SELECT * FROM sqlite_master ORDER BY name"
        ).fetchall()
        parts = connection.execute("SELECT * FROM part_info").fetchall()
    variant = variant_store(tmp_path, "first")
    assert variant.get_generation_count() == 2
    assert variant.increment_generation_count() == 3
    with closing(sqlite3.connect(first.dbfile)) as connection:
        assert (
            connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
            == schema
        )
        assert connection.execute("SELECT * FROM part_info").fetchall() == parts
        assert connection.execute("SELECT * FROM unrelated_plugin").fetchall() == [
            (b"preserve-exactly",)
        ]
    reopened = _store(store_module, tmp_path, "first")
    assert reopened.get_generation_count() == 3
    assert _metadata(reopened) == [("generation_count", "3"), ("unrelated", "keep")]


@pytest.mark.parametrize("lcsc", ["", "C100"])
def test_ordinary_csv_is_ignored_on_reopen_without_backup_or_new_marker(
    tmp_path: Path, store_module: ModuleType, lcsc: str
) -> None:
    """Reopening preserves SQLite assignments, board flags, and obsolete CSV bytes."""
    original = _store(store_module, tmp_path)
    with closing(sqlite3.connect(original.dbfile)) as connection, connection:
        connection.execute("UPDATE part_info SET lcsc = ?", (lcsc,))
    source = tmp_path / "jlcpcb" / "part_assignments.csv"
    contents = b"R1,C123,1,1\r\n"
    source.write_bytes(contents)

    for _ in range(2):
        reopened = _store(store_module, tmp_path)
        part = reopened.get_part("R1")
        assert (part["lcsc"], part["exclude_from_bom"], part["exclude_from_pos"]) == (
            lcsc,
            0,
            0,
        )
        assert source.read_bytes() == contents
        assert not source.with_suffix(".csv.backup").exists()
        assert _metadata(reopened) == []
