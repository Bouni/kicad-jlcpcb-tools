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
    expected["part_info_retired"] = expected.pop("part_info")

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
    expected["part_info_retired"] = expected.pop("part_info")
    assert saved_data(database) == expected


@pytest.mark.parametrize("failure", ["rename", "commit"])
def test_transaction_failure_rolls_back_and_closes_connection(
    storage: ModuleType,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A failure after ALTER TABLE executes leaves the active table intact."""
    expected = saved_data(database)
    connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if sql.startswith("ALTER TABLE"):
                if failure == "rename":
                    raise OSError("disk failed after renaming the table")

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
        error = OSError if failure == "rename" else sqlite3.DatabaseError
        with pytest.raises(error):
            storage.retire_legacy_part_info(str(database))

    assert saved_data(database) == expected
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_archive_retains_exact_columns_constraints_indexes_and_data(
    storage: ModuleType, tmp_path: Path
) -> None:
    """Unknown historical extension columns and constraints survive unchanged."""
    database = tmp_path / "project.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE part_info (reference TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "lcsc TEXT CHECK(lcsc LIKE 'C%'), unknown BLOB, stock NUMERIC DEFAULT 0);"
            "CREATE UNIQUE INDEX part_value ON part_info(value);"
            "CREATE TABLE metadata (key TEXT, value TEXT);"
            "INSERT INTO metadata VALUES ('generation_count', '23');"
            "INSERT INTO part_info VALUES ('R1','10k','C123',x'FF00',97);"
        )
        columns = connection.execute("PRAGMA table_info(part_info)").fetchall()
        index = connection.execute("PRAGMA index_info(part_value)").fetchall()
        rows = connection.execute("SELECT * FROM part_info").fetchall()
        metadata = connection.execute("SELECT * FROM metadata").fetchall()

    assert storage.retire_legacy_part_info(str(database)) == "part_info_retired"

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("PRAGMA table_info(part_info_retired)").fetchall()
            == columns
        )
        assert connection.execute("PRAGMA index_info(part_value)").fetchall() == index
        assert connection.execute("SELECT * FROM part_info_retired").fetchall() == rows
        assert connection.execute("SELECT * FROM metadata").fetchall() == metadata
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO part_info_retired VALUES ('R2', '22k', 'invalid', NULL, 0)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            connection.execute(
                "INSERT INTO part_info_retired VALUES ('R2', NULL, 'C456', NULL, 0)"
            )


def test_recreated_old_plugin_table_gets_separate_collision_safe_archive(
    storage: ModuleType, database: Path
) -> None:
    """Mixed-version reopening preserves all old archives and the recreated rows."""
    first_rows = saved_data(database)["part_info"]
    assert storage.retire_legacy_part_info(str(database)) == "part_info_retired"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE VIEW PART_INFO_RETIRED_2 AS SELECT 1;"
            "CREATE TABLE part_info (reference TEXT, lcsc TEXT);"
            "INSERT INTO part_info VALUES ('U7','C789');"
        )
    assert storage.retire_legacy_part_info(str(database)) == "part_info_retired_3"
    contents = saved_data(database)
    assert contents["part_info_retired"] == first_rows
    assert contents["part_info_retired_3"] == [("U7", "C789")]
    assert "part_info" not in contents
    assert storage.retire_legacy_part_info(str(database)) is None


def test_case_insensitive_historical_table_is_archived(
    storage: ModuleType, database: Path
) -> None:
    """Historical casing cannot bypass preservation or collision detection."""
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE part_info RENAME TO temporary_parts")
        connection.execute("ALTER TABLE temporary_parts RENAME TO PART_INFO")
    assert storage.retire_legacy_part_info(str(database)) == "part_info_retired"
    assert saved_data(database)["part_info_retired"] == [("R1", "C123")]


def test_schema_read_failure_closes_connection_and_preserves_active_table(
    storage: ModuleType, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed schema lookup cannot begin a partial archive operation."""
    expected = saved_data(database)
    connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
            if sql.startswith("SELECT"):
                raise OSError("schema read failed")
            return super().execute(sql, parameters)

    def failing_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, factory=FailingConnection, **kwargs)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(storage.sqlite3, "connect", failing_connection)
        with pytest.raises(OSError, match="schema read failed"):
            storage.retire_legacy_part_info(str(database))
    assert saved_data(database) == expected
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        sqlite3.Connection.execute(opened[0], "SELECT 1")


def test_changed_active_rows_cannot_be_archived_using_stale_coverage(
    storage: ModuleType, database: Path
) -> None:
    """Another board's new recovery data stays active until it is examined."""
    with sqlite3.connect(database) as connection:
        digest = storage.legacy_part_info_digest(connection)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO part_info VALUES ('U9', 'C999')")
    expected = saved_data(database)

    with pytest.raises(storage.LegacyPartInfoChanged, match="changed"):
        storage.retire_legacy_part_info(str(database), expected_digest=digest)

    assert saved_data(database) == expected
    with sqlite3.connect(database) as connection:
        new_digest = storage.legacy_part_info_digest(connection)
    assert (
        storage.retire_legacy_part_info(str(database), expected_digest=new_digest)
        == "part_info_retired"
    )


def test_guard_read_failure_after_write_lock_rolls_back_and_closes(
    storage: ModuleType, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure rechecking protected data rolls back the retirement transaction."""
    connect = sqlite3.connect
    with connect(database) as connection:
        digest = storage.legacy_part_info_digest(connection)
    expected = saved_data(database)
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
            if sql.startswith("SELECT") and self.in_transaction:
                raise OSError("locked read failed")
            return super().execute(sql, parameters)

    def failing_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, factory=FailingConnection, **kwargs)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(storage.sqlite3, "connect", failing_connection)
        with pytest.raises(OSError, match="locked read failed"):
            storage.retire_legacy_part_info(str(database), expected_digest=digest)
    assert saved_data(database) == expected
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        sqlite3.Connection.execute(opened[0], "SELECT 1")


def test_rename_preserves_dependent_triggers_and_views(
    storage: ModuleType, database: Path
) -> None:
    """SQLite keeps historical view and trigger behavior attached to the archive."""
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE assignment_audit (reference TEXT, lcsc TEXT);"
            "CREATE TRIGGER audit_part AFTER INSERT ON part_info BEGIN "
            "INSERT INTO assignment_audit VALUES (NEW.reference, NEW.lcsc); END;"
            "CREATE VIEW saved_assignments AS SELECT reference, lcsc FROM part_info;"
        )
    assert storage.retire_legacy_part_info(str(database)) == "part_info_retired"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM saved_assignments").fetchall() == [
            ("R1", "C123")
        ]
        connection.execute("INSERT INTO part_info_retired VALUES ('R2','C456')")
        assert connection.execute("SELECT * FROM assignment_audit").fetchall() == [
            ("R2", "C456")
        ]
        connection.execute("CREATE TABLE part_info (reference TEXT, lcsc TEXT)")
        connection.execute("INSERT INTO part_info VALUES ('U1','C999')")
        assert connection.execute(
            "SELECT * FROM saved_assignments ORDER BY reference"
        ).fetchall() == [("R1", "C123"), ("R2", "C456")]


def test_saved_board_source_guard_runs_under_lock_before_archival(
    storage: ModuleType, database: Path
) -> None:
    """Changed persisted-board evidence cannot authorize renaming active recovery."""
    expected = saved_data(database)
    calls = []

    def sources_unchanged() -> bool:
        with (
            sqlite3.connect(database, timeout=0) as other,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            other.execute("BEGIN IMMEDIATE")
        calls.append(True)
        return False

    with pytest.raises(storage.LegacyPartInfoChanged, match="PCB"):
        storage.retire_legacy_part_info(str(database), source_check=sources_unchanged)
    assert calls == [True]
    assert saved_data(database) == expected
    assert (
        storage.retire_legacy_part_info(str(database), source_check=lambda: True)
        == "part_info_retired"
    )


def test_saved_board_source_check_failure_rolls_back_retirement(
    storage: ModuleType, database: Path
) -> None:
    """An unreadable last-minute persisted source leaves the active table intact."""
    expected = saved_data(database)

    def failed_read() -> bool:
        raise OSError("persisted PCB changed")

    with pytest.raises(OSError, match="persisted PCB changed"):
        storage.retire_legacy_part_info(str(database), source_check=failed_read)
    assert saved_data(database) == expected
