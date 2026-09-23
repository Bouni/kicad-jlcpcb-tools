"""Ordinary and variant modes preserve unrelated project persistence."""

from contextlib import closing
from pathlib import Path
import sqlite3
from types import ModuleType

from .store_board_support import (
    Board,
    Footprint,
    make_store,
    store_module as _store_module,
)
from .test_store_board_data import seed_legacy
from .variant_data_support import _store as variant_store

store_module = _store_module


def test_ordinary_reads_and_counter_lookup_create_no_database(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Opening a PCB and inspecting a missing counter need no filesystem writes."""
    store = make_store(store_module, tmp_path, Board(Footprint()))
    assert store.read_all()[0]["lcsc"] == "C100"
    assert store.get_generation_count() == 0
    assert not (tmp_path / "jlcpcb").exists()
    assert store.increment_generation_count() == 1
    with closing(sqlite3.connect(store.dbfile)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == [("metadata",)]
        assert connection.execute("SELECT * FROM metadata").fetchall() == [
            ("generation_count", "1")
        ]


def test_ordinary_and_variant_generation_share_only_the_existing_counter(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Counters cross modes while obsolete assignments and corrections stay intact."""
    path = seed_legacy(tmp_path)
    first = make_store(store_module, tmp_path, Board(Footprint()))
    assert first.get_generation_count() == 0
    assert first.increment_generation_count() == 1
    second = make_store(store_module, tmp_path, Board(Footprint("R9", "C900")))
    assert second.get_generation_count() == 1
    assert second.increment_generation_count() == 2
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO metadata VALUES ('unrelated', 'keep')")
        connection.execute("CREATE TABLE correction (payload BLOB)")
        connection.execute("INSERT INTO correction VALUES (?)", (b"keep-exactly",))
        schema = connection.execute(
            "SELECT * FROM sqlite_master ORDER BY name"
        ).fetchall()
        parts = connection.execute("SELECT * FROM part_info").fetchall()
    variant = variant_store(tmp_path, "first")
    assert variant.get_generation_count() == 2
    assert variant.increment_generation_count() == 3
    reopened = make_store(store_module, tmp_path, Board(Footprint(lcsc="")))
    assert reopened.get_generation_count() == 3
    assert reopened.get_part("R1")["lcsc"] == ""
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
            == schema
        )
        assert connection.execute("SELECT * FROM part_info").fetchall() == parts
        assert connection.execute("SELECT * FROM correction").fetchall() == [
            (b"keep-exactly",)
        ]
        assert connection.execute("SELECT * FROM metadata ORDER BY key").fetchall() == [
            ("generation_count", "3"),
            ("unrelated", "keep"),
        ]
