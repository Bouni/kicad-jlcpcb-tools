"""Retiring the obsolete mapping table must preserve the rest of project data."""

from collections.abc import Iterator
from pathlib import Path
import sqlite3
from types import ModuleType
from typing import Any

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def storage() -> Iterator[ModuleType]:
    """Import the helper without starting the KiCad plugin."""
    with load_siblings(
        "_legacy_storage_tests", ("legacy_part_storage",), {}
    ) as modules:
        yield modules["legacy_part_storage"]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """Seed real historical rows and independent project data."""
    path = tmp_path / "project ? #.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE part_info (reference TEXT, lcsc TEXT);"
            "INSERT INTO part_info VALUES ('R1', 'C123');"
            "CREATE TABLE metadata (key TEXT, value TEXT);"
            "INSERT INTO metadata VALUES ('generation', '17');"
            "CREATE TABLE corrections (pattern TEXT, rotation INTEGER);"
            "INSERT INTO corrections VALUES ('SOT-23', 90);"
            "CREATE TABLE unrelated (value BLOB);"
            "INSERT INTO unrelated VALUES (x'010203');"
        )
    return path


def saved_data(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Read committed contents through a separate connection."""
    with sqlite3.connect(path) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        return {
            name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
            for name in names
        }


def test_only_legacy_mapping_table_is_retired(
    storage: ModuleType, database: Path
) -> None:
    """Reopening retains counters, corrections, unrelated data, and CSV bytes."""
    csv = database.with_suffix(".csv")
    csv.write_bytes(b"Reference,LCSC\r\nR1,C123\r\n")
    original_csv = csv.read_bytes()
    expected = saved_data(database)
    del expected["part_info"]

    storage.retire_legacy_part_info(str(database))

    assert saved_data(database) == expected
    assert csv.read_bytes() == original_csv
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_missing_database_does_not_create_project_storage(
    storage: ModuleType, tmp_path: Path
) -> None:
    """Projects without historical storage need no new cache directory."""
    database = tmp_path / "missing-directory" / "project.sqlite"

    storage.retire_legacy_part_info(str(database))

    assert not database.parent.exists()


def test_repeated_cleanup_does_not_rewrite_database_without_legacy_table(
    storage: ModuleType, database: Path
) -> None:
    """Later closes leave an already migrated file byte-for-byte unchanged."""
    storage.retire_legacy_part_info(str(database))
    original_bytes = database.read_bytes()
    original_stat = database.stat()
    original_names = sorted(path.name for path in database.parent.iterdir())

    storage.retire_legacy_part_info(str(database))

    assert database.read_bytes() == original_bytes
    assert database.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert sorted(path.name for path in database.parent.iterdir()) == original_names


def test_database_without_legacy_table_needs_no_write_lock(
    storage: ModuleType, database: Path
) -> None:
    """A concurrent counter transaction cannot block a no-op migration."""
    storage.retire_legacy_part_info(str(database))
    blocker = sqlite3.connect(database)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        storage.retire_legacy_part_info(str(database))
    finally:
        blocker.rollback()
        blocker.close()


def test_disappearing_database_is_not_recreated(
    storage: ModuleType, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existence check must not permit recreating a deleted database."""
    connect = sqlite3.connect

    def remove_before_open(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database.unlink()
        return connect(*args, **kwargs)

    monkeypatch.setattr(storage.sqlite3, "connect", remove_before_open)

    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        storage.retire_legacy_part_info(str(database))

    assert not database.exists()


def test_malformed_database_is_retained(storage: ModuleType, tmp_path: Path) -> None:
    """Unreadable historical data raises without replacing the file."""
    database = tmp_path / "project.sqlite"
    contents = b"a damaged historical database, not an empty new database"
    database.write_bytes(contents)

    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        storage.retire_legacy_part_info(str(database))

    assert database.read_bytes() == contents


def test_read_only_database_retains_all_rows(
    storage: ModuleType, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite refusing writes preserves every row for a later retry."""
    expected = saved_data(database)
    connect = sqlite3.connect

    def read_only_connection(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        return connect(database.as_uri() + "?mode=ro", uri=True)

    # Exercise SQLite's actual read-only behavior independently of test-user
    # privileges, which can make chmod-based checks unexpectedly writable.
    with monkeypatch.context() as patch:
        patch.setattr(storage.sqlite3, "connect", read_only_connection)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            storage.retire_legacy_part_info(str(database))

    assert saved_data(database) == expected


def test_locked_database_retains_rows_and_can_be_retried(
    storage: ModuleType, database: Path
) -> None:
    """A concurrent writer prevents retirement without damaging historical data."""
    expected = saved_data(database)
    blocker = sqlite3.connect(database)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            storage.retire_legacy_part_info(str(database))
        assert saved_data(database) == expected
    finally:
        blocker.rollback()
        blocker.close()

    storage.retire_legacy_part_info(str(database))
    del expected["part_info"]
    assert saved_data(database) == expected


@pytest.mark.parametrize("failure", ["drop", "commit"])
def test_transaction_failure_rolls_back_and_closes_connection(
    storage: ModuleType,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Even a failure after DROP executes must not commit the deleted table."""
    expected = saved_data(database)
    connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if sql.startswith("DROP TABLE"):
                if failure == "drop":
                    raise OSError("disk failed after dropping the table")

                def deny_commit(action: int, operation: str, *_args: Any) -> int:
                    if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                self.set_authorizer(deny_commit)
            return cursor

    def failing_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, factory=FailingConnection, **kwargs)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(storage.sqlite3, "connect", failing_connection)
        error = OSError if failure == "drop" else sqlite3.DatabaseError
        with pytest.raises(error):
            storage.retire_legacy_part_info(str(database))

    assert saved_data(database) == expected
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
