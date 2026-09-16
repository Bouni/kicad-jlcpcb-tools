"""Shared supplier metadata uses existing storage and a local failed-write overlay."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from tests.wx_harness import load_correction_modules


@pytest.fixture
def modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Load real storage while isolating unrelated correction downloads."""
    with load_correction_modules() as loaded:
        monkeypatch.setattr(
            loaded.library.Library, "retry_correction_migrations", lambda self: None
        )
        yield loaded


def make_library(modules: SimpleNamespace, directory: Path) -> Any:
    """Construct or reopen a real Library in an isolated data directory."""
    return modules.library.Library(
        SimpleNamespace(
            settings={"library": {"data_path": str(directory)}},
            project_path=str(directory.parent / "project"),
        )
    )


@pytest.fixture
def library(modules: SimpleNamespace, tmp_path: Path) -> Any:
    """Provide initialized optional storage for cache workflows."""
    return make_library(modules, tmp_path / "global")


def execute(path: str, sql: str) -> list[Any]:
    """Inspect or change durable state through a separate SQLite connection."""
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute(sql).fetchall()


def test_existing_preferences_and_schema(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Only the metadata table is added; installed preferences stay intact."""
    directory = tmp_path / "global"
    directory.mkdir()
    database = str(directory / "mappings.db")
    execute(database, "CREATE TABLE mapping ('footprint', 'value', 'LCSC')")
    execute(database, "INSERT INTO mapping VALUES ('R_0603', '10k', 'C123')")
    library = make_library(modules, directory)
    assert library.get_all_part_preferences() == [["R_0603", "10k", "C123"]]
    assert execute(
        database, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) == [("lcsc_metadata",), ("mapping",)]
    assert [
        (row[1], row[2], row[3], row[5])
        for row in execute(database, "PRAGMA table_info(lcsc_metadata)")
    ] == [
        ("lcsc", "TEXT", 1, 1),
        ("assembly_process", "TEXT", 0, 0),
        ("component_product_type", "INTEGER", 0, 0),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        execute(database, "INSERT INTO lcsc_metadata VALUES ('C123', NULL, 3)")


@pytest.mark.parametrize("recovery", ["first-use", "retry", "reopen"])
def test_preference_initialization_precedes_cache(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    """An interrupted first use cannot make preferences look initialized."""
    library_type = modules.library.Library
    create_preferences = library_type.create_part_preferences_table
    attempts, migrations = [], []

    def create(library: Any) -> None:
        attempts.append(True)
        if recovery != "first-use" and len(attempts) == 1:
            with closing(sqlite3.connect(library.part_preferences_db_file)):
                raise sqlite3.OperationalError("injected first schema failure")
        create_preferences(library)

    def migrate(library: Any) -> None:
        migrations.append(True)
        assert execute(
            library.part_preferences_db_file,
            "SELECT name FROM sqlite_master WHERE type='table'",
        ) == [("mapping",)]

    monkeypatch.setattr(library_type, "create_part_preferences_table", create)
    monkeypatch.setattr(library_type, "migrate_legacy_part_preferences", migrate)
    library = make_library(modules, tmp_path / "global")
    if recovery != "first-use":
        library.merge_lcsc_metadata("C1", {"component_product_type": 2})
        assert library.get_lcsc_metadata(["C1"])["C1"]["component_product_type"] == 2
        assert migrations == []
        if recovery == "reopen":
            library = make_library(modules, tmp_path / "global")
        else:
            library.check_library()
    assert migrations == [True]
    assert execute(
        library.part_preferences_db_file,
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    ) == [("lcsc_metadata",), ("mapping",)]


@pytest.mark.parametrize(
    "identifier,invalid_type",
    [
        (None, None),
        ("", ""),
        ("Z123", "unknown"),
        ("123", True),
        (123, 3),
        ("C123 extra", 1.5),
    ],
)
def test_normalization_partial_merges_and_shared_updates(
    library: Any,
    modules: SimpleNamespace,
    tmp_path: Path,
    identifier: object,
    invalid_type: object,
) -> None:
    """Validated partial results survive reopen and follow other projects' writes."""
    library.merge_lcsc_metadata(" c123 ", {"assembly_process": " SMT "})
    library.merge_lcsc_metadata("C123", {"component_product_type": "0"})
    library.merge_lcsc_metadata(
        "C123", {"assembly_process": " ", "component_product_type": invalid_type}
    )
    library.merge_lcsc_metadata(identifier, {"component_product_type": 2})
    library.merge_lcsc_metadata("C999", {"component_product_type": invalid_type})
    assert library.get_lcsc_metadata([identifier, "C999"]) == {}
    assert execute(library.part_preferences_db_file, "SELECT * FROM lcsc_metadata") == [
        ("C123", "SMT", 0)
    ]
    other = make_library(modules, tmp_path / "global")
    other.parent.project_path = str(tmp_path / "different-project")
    assert other.get_lcsc_metadata(["C123", "c123"]) == {
        "C123": {"assembly_process": "SMT", "component_product_type": 0}
    }
    other.merge_lcsc_metadata("C123", {"component_product_type": 2})
    library.merge_lcsc_metadata("C123", {"assembly_process": "THT"})
    assert other.get_lcsc_metadata(["C123"]) == {
        "C123": {"assembly_process": "THT", "component_product_type": 2}
    }


def test_large_reads_use_bounded_sql_parameters(
    library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Large boards respect SQLite's historical 999-variable limit."""
    library.merge_lcsc_metadata("C2000", {"component_product_type": 0})
    connect = sqlite3.connect

    class LimitedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            assert len(parameters) <= 999
            return super().execute(sql, parameters)

    def limited_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=LimitedConnection)

    monkeypatch.setattr(sqlite3, "connect", limited_connect)
    assert library.get_lcsc_metadata(f"C{number}" for number in range(2001)) == {
        "C2000": {"assembly_process": None, "component_product_type": 0}
    }


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
def test_failed_fields_survive_shared_changes_until_explicit_success(
    library: Any, modules: SimpleNamespace, tmp_path: Path, operation: str
) -> None:
    """Reads never retry writes; committing one field clears only its overlay."""
    database = library.part_preferences_db_file
    if operation == "UPDATE":
        library.merge_lcsc_metadata(
            "C1", {"assembly_process": "old", "component_product_type": 0}
        )
    before = execute(database, "SELECT * FROM lcsc_metadata")
    execute(
        database,
        f"CREATE TRIGGER reject_metadata BEFORE {operation} ON lcsc_metadata "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END",
    )
    library.merge_lcsc_metadata(
        "C1", {"assembly_process": "SMT", "component_product_type": 2}
    )
    assert library.get_lcsc_metadata(["C1"]) == {
        "C1": {"assembly_process": "SMT", "component_product_type": 2}
    }
    assert execute(database, "SELECT * FROM lcsc_metadata") == before
    assert library.save_part_preferences([("R_0603", "10k", "C1")]) == 1
    execute(database, "DROP TRIGGER reject_metadata")
    other = make_library(modules, tmp_path / "global")
    other.merge_lcsc_metadata(
        "C1", {"assembly_process": "THT", "component_product_type": 1}
    )
    library.check_library()
    assert library.get_lcsc_metadata(["C1"]) == {
        "C1": {"assembly_process": "SMT", "component_product_type": 2}
    }
    assert execute(database, "SELECT * FROM lcsc_metadata") == [("C1", "THT", 1)]
    library.merge_lcsc_metadata("C1", {"assembly_process": "new"})
    assert execute(database, "SELECT * FROM lcsc_metadata") == [("C1", "new", 1)]
    other.merge_lcsc_metadata("C1", {"assembly_process": "shared"})
    assert library.get_lcsc_metadata(["C1"]) == {
        "C1": {"assembly_process": "shared", "component_product_type": 2}
    }
    assert (
        make_library(modules, tmp_path / "global").get_lcsc_metadata(["C1"])["C1"][
            "component_product_type"
        ]
        == 1
    )
    library.merge_lcsc_metadata("C1", {"component_product_type": 2})
    other.merge_lcsc_metadata("C1", {"component_product_type": 0})
    assert library.get_lcsc_metadata(["C1"])["C1"]["component_product_type"] == 0


def test_read_failure_recovery_and_path_isolation(
    library: Any, modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Unavailable storage keeps fallback and pending fields in their own directory."""
    library.merge_lcsc_metadata(
        "C1", {"assembly_process": "SMT", "component_product_type": 0}
    )
    database = library.part_preferences_db_file
    execute(database, "DROP TABLE lcsc_metadata")
    assert library.get_lcsc_metadata(["C1"])["C1"]["component_product_type"] == 0
    library.merge_lcsc_metadata("C1", {"component_product_type": 2})
    assert library.get_lcsc_metadata(["C1"])["C1"]["component_product_type"] == 2
    library.parent.settings["library"]["data_path"] = str(tmp_path / "other")
    library.refresh_library_config()
    assert library.get_lcsc_metadata(["C1"]) == {}
    library.parent.settings["library"]["data_path"] = str(tmp_path / "global")
    library.refresh_library_config()
    library.check_library()
    assert execute(database, "SELECT * FROM lcsc_metadata") == []
    library.merge_lcsc_metadata("C1", {"assembly_process": "THT"})
    assert library.get_lcsc_metadata(["C1"]) == {
        "C1": {"assembly_process": "THT", "component_product_type": 2}
    }
    assert execute(database, "SELECT * FROM lcsc_metadata") == [("C1", "THT", None)]
    library.merge_lcsc_metadata("C1", {"component_product_type": 2})
    Path(library.partsdb_file).write_bytes(b"replacement downloaded catalog")
    library.parent.settings["library"]["selected_library"] = next(
        name
        for name in modules.library.LIBRARY_CONFIGS
        if name != library.selected_library
    )
    library.refresh_library_config()
    assert library.get_lcsc_metadata(["C1"]) == {
        "C1": {"assembly_process": "THT", "component_product_type": 2}
    }
    assert make_library(modules, tmp_path / "global").get_lcsc_metadata(["C1"]) == {
        "C1": {"assembly_process": "THT", "component_product_type": 2}
    }
