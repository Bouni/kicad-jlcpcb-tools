"""Check validated preference batches, installed storage and CSV round trips."""

from collections.abc import Iterator
from contextlib import closing
import csv
import importlib
import logging
from pathlib import Path
import sqlite3
import sys
import types
from typing import Any
from unittest.mock import Mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "_part_preferences_storage_tests"


@pytest.fixture
def part_preferences_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """Isolate GUI/network imports while using the real SQLite preference methods."""
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    monkeypatch.setitem(sys.modules, _PACKAGE, package)
    wx = types.ModuleType("wx")
    wx.__path__ = []
    wx.Dialog = type("Dialog", (), {})
    wx.dataview = types.ModuleType("wx.dataview")
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "wx.dataview", wx.dataview)
    monkeypatch.setitem(sys.modules, "requests", types.ModuleType("requests"))
    try:
        module = importlib.import_module(f"{_PACKAGE}.library")
        library = module.Library.__new__(module.Library)
        library.logger = logging.getLogger(_PACKAGE)
        library.part_preferences_db_file = str(tmp_path / "mappings.db")
        library.create_part_preferences_table()
        yield library
    finally:
        for name in tuple(sys.modules):
            if name.startswith(f"{_PACKAGE}."):
                sys.modules.pop(name)


def _seed(library: Any, rows: list[tuple[str, str, object]]) -> None:
    """Write installed rows directly, including duplicates and invalid old values."""
    with closing(sqlite3.connect(library.part_preferences_db_file)) as con, con:
        con.executemany("INSERT INTO mapping VALUES (?, ?, ?)", rows)


def _rows(library: Any) -> list[tuple[object, ...]]:
    """Reopen the database to inspect durable values independently of the reader."""
    with closing(sqlite3.connect(library.part_preferences_db_file)) as con:
        return con.execute("SELECT rowid, * FROM mapping").fetchall()


@pytest.fixture
def preference_dialog(part_preferences_library: Any) -> Any:
    """Use real CSV handlers with only the GUI refresh replaced."""
    module = importlib.import_module(f"{_PACKAGE}.part_preferences")
    dialog = module.PartPreferencesDialog.__new__(module.PartPreferencesDialog)
    dialog.parent = types.SimpleNamespace(library=part_preferences_library)
    dialog.logger = logging.getLogger(_PACKAGE)
    dialog.populate_part_preferences_list = Mock()
    return dialog


@pytest.mark.parametrize("identifier", ["C123", "c123", " \tc123\n"])
def test_preferences_normalize_and_survive_reopening(
    part_preferences_library: Any, identifier: str
) -> None:
    """Store canonical C-numbers so another window can reuse them."""
    library = part_preferences_library
    assert library.save_part_preferences([("R_0603", "10k", identifier)]) == 1
    assert _rows(library) == [(1, "R_0603", "10k", "C123")]
    reopened = type(library).__new__(type(library))
    reopened.part_preferences_db_file = library.part_preferences_db_file
    reopened.logger = library.logger
    assert reopened.get_part_preference("R_0603", "10k") == "C123"


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        " \t\n",
        None,
        123,
        b"C123",
        "Z123",
        "C",
        "C-1",
        "C123junk",
        "see C123",
        "C１２３",
    ],
)
def test_invalid_identifiers_never_replace_or_create_preferences(
    part_preferences_library: Any, identifier: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Reject complete invalid inputs without erasing an existing choice."""
    library = part_preferences_library
    _seed(library, [("R_0603", "10k", "C1")])
    assert (
        library.save_part_preferences(
            [("R_0603", "10k", identifier), ("R_0603", "20k", identifier)]
        )
        == 0
    )
    assert _rows(library) == [(1, "R_0603", "10k", "C1")]
    assert "invalid" in caplog.text.lower()


@pytest.mark.parametrize(
    "identifier, expected",
    [(" c123 ", "C123"), ("Z123", None), ("C123junk", None), (None, None)],
)
def test_installed_rows_are_validated_for_use_without_rewriting(
    part_preferences_library: Any,
    identifier: object,
    expected: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep invalid installed data visible while preventing its application."""
    library = part_preferences_library
    _seed(library, [("R_0603", "10k", identifier)])
    before = _rows(library)
    assert library.get_part_preference("R_0603", "10k") == expected
    assert library.get_part_preference("missing", "10k") is None
    assert _rows(library) == before
    assert library.get_all_part_preferences() == [["R_0603", "10k", identifier]]
    assert ("invalid" in caplog.text.lower()) is (expected is None)


@pytest.mark.parametrize(
    "identifiers, expected", [(["Z123", "C2"], None), ([" c2 ", "Z123"], "C2")]
)
def test_mixed_legacy_duplicates_use_first_row_until_explicit_replacement(
    part_preferences_library: Any,
    identifiers: list[str],
    expected: object,
) -> None:
    """Do not silently choose a different duplicate when the first stored row is invalid."""
    library = part_preferences_library
    _seed(library, [("R", "10k", identifier) for identifier in identifiers])
    before = _rows(library)
    assert library.get_part_preference("R", "10k") == expected
    assert _rows(library) == before
    assert library.save_part_preferences([("R", "10k", "C3")]) == 1
    assert library.get_all_part_preferences() == [
        ["R", "10k", "C3"],
        ["R", "10k", "C3"],
    ]


@pytest.mark.parametrize("value", ["", " 10k ", "10Ω ±1%", "10k's tolerance"])
def test_exact_keys_and_literal_quotes_survive_save_lookup_delete(
    part_preferences_library: Any, value: str
) -> None:
    """Preserve exact keys and parameterize every retained CRUD operation."""
    library = part_preferences_library
    footprint = "Custom:Bob's_0603' OR 1=1 --"
    _seed(library, [("Other", "10k", "C2")])
    assert library.save_part_preferences([(footprint, value, "C1")]) == 1
    assert library.get_part_preference(footprint, value) == "C1"
    assert library.save_part_preferences([(footprint, value, "C3")]) == 1
    assert library.get_part_preference(footprint, value) == "C3"
    library.delete_part_preference(footprint, value)
    assert library.get_all_part_preferences() == [["Other", "10k", "C2"]]


def test_one_transaction_counts_keys_and_updates_legacy_duplicates_without_migration(
    part_preferences_library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deduplicate accepted choices and update old duplicate rows in one transaction."""
    library = part_preferences_library
    _seed(library, [("R", "10k", "C1"), ("R", "10k", "C2"), ("R", "20k", "C3")])
    with closing(sqlite3.connect(library.part_preferences_db_file)) as con:
        schema = con.execute("SELECT name, sql FROM sqlite_master").fetchall()
    statements: list[str] = []
    connections: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def traced_connect(database: str) -> sqlite3.Connection:
        connection = connect(database)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    assert (
        library.save_part_preferences(
            iter(
                [
                    ("R", "10k", "C4"),
                    ("R", "10k", "c5"),
                    ("R", "10k", "Z123"),
                    ("R", "20k", "C3"),
                    ("R", " 20k ", "C6"),
                ]
            )
        )
        == 2
    )
    assert len(connections) == 1
    assert statements.count("BEGIN IMMEDIATE") == statements.count("COMMIT") == 1
    assert sum(s.startswith("UPDATE mapping") for s in statements) == 1
    assert sum(s.startswith("INSERT INTO mapping") for s in statements) == 1
    with closing(connect(library.part_preferences_db_file)) as con:
        assert con.execute("SELECT name, sql FROM sqlite_master").fetchall() == schema
        assert con.execute("SELECT * FROM mapping").fetchall() == [
            ("R", "10k", "C5"),
            ("R", "10k", "C5"),
            ("R", "20k", "C3"),
            ("R", " 20k ", "C6"),
        ]


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
def test_later_write_failure_rolls_back_every_key_after_reopening(
    part_preferences_library: Any, operation: str
) -> None:
    """Observe whole-batch rollback through a fresh SQLite connection."""
    library = part_preferences_library
    _seed(library, [("R", "untouched", "C0")])
    if operation == "UPDATE":
        _seed(library, [("R", "good", "C1"), ("R", "bad", "C2")])
    before = _rows(library)
    with closing(sqlite3.connect(library.part_preferences_db_file)) as con, con:
        con.execute(
            f"CREATE TRIGGER reject_second_write BEFORE {operation} ON mapping "
            "WHEN NEW.value = 'bad' BEGIN SELECT RAISE(ABORT, 'cannot save batch'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot save batch"):
        library.save_part_preferences([("R", "good", "C3"), ("R", "bad", "C4")])
    assert _rows(library) == before


def test_unchanged_and_empty_batches_do_not_write(
    part_preferences_library: Any,
) -> None:
    """Do not write when the last valid choice already matches installed data."""
    library = part_preferences_library
    _seed(library, [("R", "10k", "C1")])
    with closing(sqlite3.connect(library.part_preferences_db_file)) as con, con:
        con.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE ON mapping "
            "BEGIN SELECT RAISE(ABORT, 'unexpected update'); END"
        )
    assert (
        library.save_part_preferences(
            [("R", "10k", "C2"), ("R", "10k", "c1"), ("R", "10k", "")]
        )
        == 0
    )
    assert library.save_part_preferences([]) == 0
    assert _rows(library) == [(1, "R", "10k", "C1")]


def test_csv_import_batches_valid_rows_and_reports_actual_changes(
    preference_dialog: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one validated save and report only distinct committed changes."""
    library = preference_dialog.parent.library
    _seed(library, [("R", "10k", "C1"), ("R", "unchanged", "C3")])
    path = tmp_path / "preferences.csv"
    path.write_text(
        "Footprint,Part Value,LCSC Part\nR,10k,c2\nR,10k,\nR,other, C4 \nR,invalid,see C123\nR,unchanged,C3\n",
        encoding="utf-8",
    )
    save = Mock(wraps=library.save_part_preferences)
    monkeypatch.setattr(library, "save_part_preferences", save)
    with caplog.at_level(logging.INFO):
        preference_dialog._import_part_preferences(str(path))
    assert save.call_count == 1
    assert library.get_all_part_preferences() == [
        ["R", "10k", "C2"],
        ["R", "unchanged", "C3"],
        ["R", "other", "C4"],
    ]
    assert "invalid" in caplog.text.lower()
    assert "Imported 2 part preference(s)" in caplog.text
    preference_dialog.populate_part_preferences_list.assert_called_once()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        preference_dialog._import_part_preferences(str(path))
    assert "Imported" not in caplog.text


@pytest.mark.parametrize(
    "bad_row",
    [b"R,bad\n", b"R,bad,C3,extra\n", b'R,bad,"unterminated\n', b"R,bad,\xff\n"],
)
def test_csv_bad_structure_or_read_failure_never_commits_earlier_rows(
    preference_dialog: Any,
    tmp_path: Path,
    bad_row: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Finish parsing before any earlier valid row can change storage."""
    library = preference_dialog.parent.library
    _seed(library, [("R", "10k", "C1")])
    before = _rows(library)
    path = tmp_path / "broken.csv"
    path.write_bytes(b"Footprint,Part Value,LCSC Part\nR,10k,C2\n" + bad_row)
    with caplog.at_level(logging.INFO):
        preference_dialog._import_part_preferences(str(path))
    assert _rows(library) == before
    assert "Unable to import" in caplog.text
    assert "Imported" not in caplog.text
    preference_dialog.populate_part_preferences_list.assert_not_called()


def test_csv_later_database_failure_rolls_back_and_does_not_log_success(
    preference_dialog: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep the whole import unchanged when its later insert fails."""
    library = preference_dialog.parent.library
    _seed(library, [("R", "10k", "C1")])
    with closing(sqlite3.connect(library.part_preferences_db_file)) as con, con:
        con.execute(
            "CREATE TRIGGER reject_import BEFORE INSERT ON mapping "
            "BEGIN SELECT RAISE(ABORT, 'cannot import'); END"
        )
    path = tmp_path / "preferences.csv"
    path.write_text(
        "Footprint,Part Value,LCSC Part\nR,10k,C2\nR,new,C3\n", encoding="utf-8"
    )
    with caplog.at_level(logging.INFO):
        preference_dialog._import_part_preferences(str(path))
    assert _rows(library) == [(1, "R", "10k", "C1")]
    assert "Unable to import" in caplog.text
    assert "Imported" not in caplog.text
    preference_dialog.populate_part_preferences_list.assert_not_called()


@pytest.mark.parametrize("header", ["", "Footprint,Value,Other\n"])
def test_csv_missing_or_wrong_header_aborts_before_writes(
    preference_dialog: Any,
    tmp_path: Path,
    header: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Require the existing exported header instead of discarding the first data row."""
    library = preference_dialog.parent.library
    path = tmp_path / "preferences.csv"
    path.write_text(header + "R,10k,C2\nR,20k,C3\n", encoding="utf-8")
    preference_dialog._import_part_preferences(str(path))
    assert _rows(library) == []
    assert "Unable to import" in caplog.text
    preference_dialog.populate_part_preferences_list.assert_not_called()


def test_csv_export_preserves_invalid_installed_rows_and_header(
    preference_dialog: Any, tmp_path: Path
) -> None:
    """Retain the interchange format and export invalid installed data verbatim."""
    library = preference_dialog.parent.library
    _seed(library, [("Custom:Bob's part", " 10Ω ±1% ", "Z123")])
    path = tmp_path / "preferences.csv"
    preference_dialog._export_part_preferences(str(path))
    with path.open(newline="", encoding="utf-8") as exported:
        assert list(csv.reader(exported)) == [
            ["Footprint", "Part Value", "LCSC Part"],
            ["Custom:Bob's part", " 10Ω ±1% ", "Z123"],
        ]


@pytest.mark.parametrize("storage_state", ["blocked", "malformed", "missing_table"])
def test_library_constructor_initializes_or_recovers_optional_preference_storage(
    part_preferences_library: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    storage_state: str,
) -> None:
    """Keep catalog construction usable and retry optional schema setup after repair."""
    data_path = tmp_path / "bootstrap"
    data_path.mkdir()
    database = data_path / "mappings.db"
    if storage_state == "blocked":
        database.mkdir()
    elif storage_state == "malformed":
        database.write_text("not a database", encoding="utf-8")
    else:
        with closing(sqlite3.connect(database)) as con, con:
            con.execute("CREATE TABLE unrelated (value)")
    library_type = type(part_preferences_library)
    monkeypatch.setattr(library_type, "retry_correction_migrations", lambda self: None)
    parent = types.SimpleNamespace(
        settings={"library": {"data_path": str(data_path)}},
        project_path=str(tmp_path / "project"),
    )
    library = library_type(parent)
    assert library.datadir == str(data_path)
    if storage_state != "missing_table":
        assert "Part preference storage is unavailable" in caplog.text
        database.rmdir() if database.is_dir() else database.unlink()
        library.check_library()
    assert library.get_all_part_preferences() == []
