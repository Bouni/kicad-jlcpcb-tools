"""Cover consolidating corrections.db and mappings.db into global.db."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import types
from typing import Any

import pytest

from tests.wx_harness import load_correction_modules


@pytest.fixture
def library_modules() -> Iterator[Any]:
    """Import production library code with GUI dependencies stubbed."""
    with load_correction_modules(names=("global_db",)) as loaded:
        yield loaded


def _seed_corrections(path: Path, rows: list[tuple[Any, ...]]) -> None:
    """Create a legacy corrections.db with the production table set."""
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE correction (regex, rotation, offset_x, offset_y)")
        con.execute("CREATE TABLE lcsc_correction (lcsc, rotation, offset_x, offset_y)")
        con.execute(
            "CREATE TABLE correction_migrations "
            "(migration_key TEXT PRIMARY KEY, source TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE correction_migration_state "
            "(migration_key TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "status TEXT NOT NULL, message TEXT NOT NULL)"
        )
        con.executemany(
            "INSERT INTO correction VALUES (?, ?, ?, ?)",
            rows,
        )


def _seed_mappings(path: Path, rows: list[tuple[Any, ...]]) -> None:
    """Create a legacy mappings.db with preference rows."""
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE mapping (footprint, value, LCSC)")
        con.executemany("INSERT INTO mapping VALUES (?, ?, ?)", rows)


def _table_rows(path: Path, table: str) -> list[tuple[Any, ...]]:
    """Read all rows from a table through an independent connection."""
    with closing(sqlite3.connect(path)) as con:
        return con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def _table_names(path: Path) -> set[str]:
    """List ordinary tables in a SQLite file."""
    with closing(sqlite3.connect(path)) as con:
        return {
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def _imported_filenames(path: Path) -> set[str]:
    """Return legacy filenames recorded in the import bookkeeping table."""
    with closing(sqlite3.connect(path)) as con:
        return {
            name
            for (name,) in con.execute(
                "SELECT filename FROM legacy_global_import ORDER BY filename"
            )
        }


def _make_parent(tmp_path: Path, data_path: Path) -> types.SimpleNamespace:
    """Build a Library parent with an isolated data directory."""
    project = tmp_path / "project"
    (project / "jlcpcb").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        settings={"library": {"data_path": str(data_path)}},
        project_path=str(project),
    )


def test_migrate_both_legacy_databases_into_global_db(
    library_modules: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Copy corrections and preferences into global.db and leave both sources."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    corrections = data_path / "corrections.db"
    mappings = data_path / "mappings.db"
    _seed_corrections(corrections, [("^R", 90, 0.1, -0.2)])
    _seed_mappings(mappings, [("R_0603", "10k", "C200")])

    with caplog.at_level(logging.INFO):
        library = library_modules.library.Library(_make_parent(tmp_path, data_path))

    global_db = data_path / "global.db"
    assert library.globalcorrectionsdb_file == str(global_db)
    assert library.part_preferences_db_file == str(global_db)
    assert corrections.exists()
    assert mappings.exists()
    assert _imported_filenames(global_db) == {"corrections.db", "mappings.db"}
    assert _table_rows(global_db, "correction") == [("^R", 90, 0.1, -0.2)]
    assert _table_rows(global_db, "mapping") == [("R_0603", "10k", "C200")]
    assert "Found legacy corrections database" in caplog.text
    assert "Found legacy part preferences database" in caplog.text
    assert "leaving the legacy file" in caplog.text.lower()
    assert library.get_part_preference("R_0603", "10k") == "C200"
    assert library.get_all_correction_data() is not None


def test_fresh_install_uses_global_db_without_legacy_files(
    library_modules: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Create schemas in global.db when no legacy databases exist."""
    data_path = tmp_path / "data"
    data_path.mkdir()

    with caplog.at_level(logging.DEBUG):
        library = library_modules.library.Library(_make_parent(tmp_path, data_path))

    global_db = data_path / "global.db"
    assert library.globalcorrectionsdb_file == str(global_db)
    assert library.part_preferences_db_file == str(global_db)
    assert not (data_path / "corrections.db").exists()
    assert not (data_path / "mappings.db").exists()
    assert global_db.exists()
    assert "No legacy global databases found" in caplog.text
    assert "mapping" in _table_names(global_db)
    assert "correction" in _table_names(global_db)


def test_migrate_only_corrections_when_mappings_absent(
    library_modules: Any, tmp_path: Path
) -> None:
    """Migrate a single legacy file without requiring the other."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    corrections = data_path / "corrections.db"
    _seed_corrections(corrections, [("^C", 180, 0, 0)])

    library = library_modules.library.Library(_make_parent(tmp_path, data_path))
    global_db = data_path / "global.db"

    assert corrections.exists()
    assert library.globalcorrectionsdb_file == str(global_db)
    assert library.part_preferences_db_file == str(global_db)
    assert _imported_filenames(global_db) == {"corrections.db"}
    assert _table_rows(global_db, "correction") == [("^C", 180, 0, 0)]
    assert "mapping" in _table_names(global_db)


def test_migrate_only_mappings_when_corrections_absent(
    library_modules: Any, tmp_path: Path
) -> None:
    """Leave corrections on global.db when only mappings.db needs migration."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    mappings = data_path / "mappings.db"
    _seed_mappings(mappings, [("R_0402", "1k", "C1")])

    library = library_modules.library.Library(_make_parent(tmp_path, data_path))
    global_db = data_path / "global.db"

    assert mappings.exists()
    assert library.globalcorrectionsdb_file == str(global_db)
    assert library.part_preferences_db_file == str(global_db)
    assert _imported_filenames(global_db) == {"mappings.db"}
    assert _table_rows(global_db, "mapping") == [("R_0402", "1k", "C1")]
    assert "correction" in _table_names(global_db)


def test_reappearing_legacy_file_does_not_overwrite_global_db(
    library_modules: Any, tmp_path: Path
) -> None:
    """Skip a second copy when a recorded legacy file reappears with new data."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    corrections = data_path / "corrections.db"
    global_db = data_path / "global.db"
    _seed_corrections(corrections, [("^orig", 90, 1, 2)])

    library_modules.library.Library(_make_parent(tmp_path, data_path))
    assert _table_rows(global_db, "correction") == [("^orig", 90, 1, 2)]
    assert _imported_filenames(global_db) == {"corrections.db"}

    with closing(sqlite3.connect(global_db)) as con, con:
        con.execute(
            "INSERT INTO correction VALUES (?, ?, ?, ?)",
            ("^new", 180, 0, 0),
        )

    corrections.unlink()
    _seed_corrections(corrections, [("^old", 0, 0, 0)])

    library = library_modules.library.Library(_make_parent(tmp_path, data_path))

    assert corrections.exists()
    assert library.globalcorrectionsdb_file == str(global_db)
    assert _table_rows(global_db, "correction") == [
        ("^orig", 90, 1, 2),
        ("^new", 180, 0, 0),
    ]
    assert ("^old", 0, 0, 0) not in _table_rows(global_db, "correction")


def test_mid_transaction_failure_leaves_legacy_and_rolls_back_destination(
    library_modules: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the legacy file and destination contents when the copy aborts."""
    global_db_module = library_modules.global_db

    data_path = tmp_path / "data"
    data_path.mkdir()
    corrections = data_path / "corrections.db"
    mappings = data_path / "mappings.db"
    global_db = data_path / "global.db"
    _seed_corrections(corrections, [("^keep", 90, 0, 0)])
    _seed_mappings(mappings, [("R_0603", "10k", "C200")])
    _seed_mappings(global_db, [("R_0603", "10k", "C999")])

    original_copy = global_db_module._copy_legacy_database

    def fail_corrections(
        source: Path,
        destination: Path,
        filename: str,
        log: logging.Logger,
    ) -> None:
        if source.name == "corrections.db":
            raise sqlite3.DatabaseError("simulated copy failure")
        original_copy(source, destination, filename, log)

    monkeypatch.setattr(global_db_module, "_copy_legacy_database", fail_corrections)

    library = library_modules.library.Library(_make_parent(tmp_path, data_path))

    assert corrections.exists()
    assert mappings.exists()
    assert library.globalcorrectionsdb_file == str(corrections)
    assert library.part_preferences_db_file == str(global_db)
    assert _table_rows(corrections, "correction") == [("^keep", 90, 0, 0)]
    assert _table_rows(global_db, "mapping") == [("R_0603", "10k", "C200")]
    assert _imported_filenames(global_db) == {"mappings.db"}
    assert library.get_all_correction_data() is not None
    assert library.get_part_preference("R_0603", "10k") == "C200"


def test_corrupt_legacy_file_keeps_path_and_allows_other_migration(
    library_modules: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed for an unreadable legacy DB without blocking the other domain."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    corrections = data_path / "corrections.db"
    mappings = data_path / "mappings.db"
    corrections.write_text("not a database", encoding="utf-8")
    _seed_mappings(mappings, [("R_0603", "10k", "C200")])

    with caplog.at_level(logging.ERROR):
        library = library_modules.library.Library(_make_parent(tmp_path, data_path))

    global_db = data_path / "global.db"
    assert corrections.exists()
    assert mappings.exists()
    assert library.globalcorrectionsdb_file == str(corrections)
    assert library.part_preferences_db_file == str(global_db)
    assert "Failed to migrate legacy corrections database" in caplog.text
    assert library.get_part_preference("R_0603", "10k") == "C200"


def test_empty_legacy_file_is_recorded_and_paths_use_global_db(
    library_modules: Any, tmp_path: Path
) -> None:
    """Treat an empty legacy stub as migrated so startup uses global.db."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    corrections = data_path / "corrections.db"
    mappings = data_path / "mappings.db"
    corrections.touch()
    mappings.touch()

    library = library_modules.library.Library(_make_parent(tmp_path, data_path))
    global_db = data_path / "global.db"

    assert corrections.exists()
    assert mappings.exists()
    assert library.globalcorrectionsdb_file == str(global_db)
    assert library.part_preferences_db_file == str(global_db)
    assert _imported_filenames(global_db) == {"corrections.db", "mappings.db"}
    assert "correction" in _table_names(global_db)
    assert "mapping" in _table_names(global_db)


def test_migration_uses_configured_data_path(
    library_modules: Any, tmp_path: Path
) -> None:
    """Run migration in Settings library.data_path, not only the plugin tree."""
    data_path = tmp_path / "custom-data"
    data_path.mkdir()
    _seed_corrections(data_path / "corrections.db", [("^X", 270, 0, 0)])
    _seed_mappings(data_path / "mappings.db", [("L_0805", "10uH", "C9")])

    library = library_modules.library.Library(_make_parent(tmp_path, data_path))

    assert library.datadir == str(data_path)
    assert Path(library.globalcorrectionsdb_file) == data_path / "global.db"
    assert Path(library.part_preferences_db_file) == data_path / "global.db"
    assert _table_rows(data_path / "global.db", "correction") == [("^X", 270, 0, 0)]
    assert _table_rows(data_path / "global.db", "mapping") == [("L_0805", "10uH", "C9")]
    assert (data_path / "corrections.db").exists()
    assert (data_path / "mappings.db").exists()


def test_migrator_result_reports_per_database_status(
    library_modules: Any, tmp_path: Path
) -> None:
    """Expose absent, migrated, and failed outcomes for path wiring."""
    global_db = library_modules.global_db

    data_path = tmp_path / "data"
    data_path.mkdir()
    _seed_mappings(data_path / "mappings.db", [("R", "1k", "C1")])
    (data_path / "corrections.db").write_bytes(b"corrupt")

    result = global_db.migrate_legacy_global_databases(
        str(data_path), logging.getLogger("test.global_db")
    )
    by_name = {item.filename: item for item in result.outcomes}
    assert by_name["corrections.db"].status == "failed"
    assert by_name["mappings.db"].status == "migrated"
    assert result.uses_global("corrections.db") is False
    assert result.uses_global("mappings.db") is True
    assert (data_path / "mappings.db").exists()
    assert (data_path / "corrections.db").exists()
    assert _imported_filenames(data_path / "global.db") == {"mappings.db"}


def test_switch_to_global_creates_correction_tables_when_only_mapping_exists(
    library_modules: Any, tmp_path: Path
) -> None:
    """Create correction tables when switching and global.db already has mapping."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    _seed_mappings(data_path / "mappings.db", [("R_0603", "10k", "C200")])

    parent = _make_parent(tmp_path, data_path)
    library_modules.library.Library(parent)
    global_db = data_path / "global.db"

    # Drop correction tables while leaving preferences, matching a user who only
    # ever used project-local corrections on a mapping-only global.db.
    with closing(sqlite3.connect(global_db)) as con, con:
        con.execute("DROP TABLE correction")
        con.execute("DROP TABLE IF EXISTS lcsc_correction")
        con.execute("DROP TABLE IF EXISTS correction_migrations")
        con.execute("DROP TABLE IF EXISTS correction_migration_state")

    project_db = Path(parent.project_path) / "jlcpcb" / "project.db"
    _seed_corrections(project_db, [("^local", 90, 0, 0)])

    local = library_modules.library.Library(parent)
    assert local.correctionsdb_file == str(project_db)
    assert "mapping" in _table_names(global_db)
    assert "correction" not in _table_names(global_db)

    local.switch_to_global_correction_database(True)

    assert local.correctionsdb_file == str(global_db)
    assert "correction" in _table_names(global_db)
    assert local.get_all_correction_data() is not None
    assert local.get_part_preference("R_0603", "10k") == "C200"
