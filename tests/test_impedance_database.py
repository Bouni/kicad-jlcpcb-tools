"""Exercise impedance persistence without migrating the existing parts store."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib.util
from pathlib import Path
import sqlite3
import sys
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the standard-library database layer without the plugin entrypoint."""
    name = "_test_impedance_database"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "impedance" / "database.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _board(project: Path, name: str = "main") -> Path:
    """Create a minimal saved board path used solely for storage identity."""
    path = project / f"{name}.kicad_pcb"
    path.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    return path


def _legacy_database(project: Path, malformed: bool = False) -> Path:
    """Create legacy tables including opaque third-party data to preserve."""
    directory = project / "jlcpcb"
    directory.mkdir()
    path = directory / "project.db"
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        if malformed:
            connection.execute("CREATE TABLE part_info (value TEXT)")
            connection.execute("INSERT INTO part_info VALUES ('irreplaceable')")
        else:
            connection.execute(
                "CREATE TABLE part_info (reference TEXT PRIMARY KEY, "
                "value TEXT NOT NULL, footprint TEXT NOT NULL, lcsc TEXT, "
                "stock NUMERIC, exclude_from_bom NUMERIC DEFAULT 0, "
                "exclude_from_pos NUMERIC DEFAULT 0, custom_field BLOB)"
            )
            connection.execute(
                "INSERT INTO part_info VALUES "
                "('R1', '10k', 'R_0603', 'C100', 50, 0, 0, ?)",
                (b"opaque value",),
            )
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [("generation_count", "12"), ("unknown-key", "preserve")],
        )
        connection.execute(
            "CREATE TABLE correction_rules (pattern TEXT, rotation REAL)"
        )
        connection.execute("INSERT INTO correction_rules VALUES ('R_*', 90)")
        connection.execute("CREATE TABLE other_plugin (payload BLOB)")
        connection.execute("INSERT INTO other_plugin VALUES (?)", (b"keep me",))
    return path


def _contents(path: Path) -> list[str]:
    """Return SQL-level database content to compare transaction rollback."""
    with contextlib.closing(sqlite3.connect(path)) as connection:
        return list(connection.iterdump())


@pytest.mark.parametrize("existing_directory", [False, True])
def test_deferred_open_and_lookup_do_not_create_storage(
    tmp_path: Path, database_module: ModuleType, existing_directory: bool
) -> None:
    """Opening an unconfigured board leaves its project directory unchanged."""
    board_path = _board(tmp_path)
    path = tmp_path / "jlcpcb" / "project.db"
    if existing_directory:
        path.parent.mkdir()
    before = set(tmp_path.rglob("*"))

    database = database_module.ImpedanceDatabase(path, initialize=False)
    assert database.find_board(board_path) is None

    assert set(tmp_path.rglob("*")) == before
    assert not path.exists()


def test_deferred_constructor_never_opens_sqlite(
    tmp_path: Path, database_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attaching deferred storage does not connect, even to an existing database."""
    path = _legacy_database(tmp_path)

    def reject_connection(*args: Any, **kwargs: Any) -> None:
        """Expose any connection attempted during deferred construction."""
        pytest.fail("Deferred construction opened SQLite")

    monkeypatch.setattr(database_module.sqlite3, "connect", reject_connection)
    database_module.ImpedanceDatabase(path, initialize=False)


@pytest.mark.parametrize("malformed_parts", [False, True])
def test_read_only_lookup_preserves_legacy_database_bytes_and_schema(
    tmp_path: Path, database_module: ModuleType, malformed_parts: bool
) -> None:
    """Looking for impedance settings never upgrades or rewrites legacy tables."""
    board_path = _board(tmp_path)
    path = _legacy_database(tmp_path, malformed=malformed_parts)
    before_bytes, before_schema = path.read_bytes(), _contents(path)
    database = database_module.ImpedanceDatabase(path, initialize=False)

    assert database.find_board(board_path) is None

    assert path.read_bytes() == before_bytes
    assert _contents(path) == before_schema


def test_read_only_lookup_loads_saved_settings_without_registering_other_boards(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Saved intent reloads unchanged, and an unknown PCB gets no identity row."""
    first_path, second_path = _board(tmp_path, "main"), _board(tmp_path, "panel")
    path = tmp_path / "jlcpcb" / "project.db"
    database = database_module.ImpedanceDatabase(path)
    first = database.resolve_board(first_path)
    database.save_config(first, {"label": "retain"}, False, 0)
    before_bytes, before_schema = path.read_bytes(), _contents(path)

    reopened = database_module.ImpedanceDatabase(path, initialize=False)
    assert reopened.find_board(first_path) == first
    assert reopened.load_config(first) == {
        "version": 1,
        "revision": 1,
        "enabled": False,
        "payload": {"label": "retain"},
    }
    assert reopened.find_board(second_path) is None

    assert path.read_bytes() == before_bytes
    assert _contents(path) == before_schema


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE board_feature_config",
        "ALTER TABLE boards RENAME COLUMN relative_path TO missing_path",
        "CREATE VIEW impedance_stackup_catalog AS SELECT 6 AS layer_count",
        "CREATE TABLE BOARDS (damaged TEXT)",
    ],
)
def test_read_only_lookup_rejects_damaged_feature_schema_without_repair(
    tmp_path: Path, database_module: ModuleType, damage: str
) -> None:
    """Partial tables and conflicting schema objects stay intact for recovery."""
    board_path = _board(tmp_path)
    path = tmp_path / "project.db"
    if not damage.startswith("CREATE"):
        database_module.ImpedanceDatabase(path)
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(damage)
    before_bytes, before_schema = path.read_bytes(), _contents(path)

    database = database_module.ImpedanceDatabase(path, initialize=False)
    with pytest.raises(database_module.DatabaseMigrationError):
        database.find_board(board_path)

    assert path.read_bytes() == before_bytes
    assert _contents(path) == before_schema


@pytest.mark.parametrize("existing_database", [False, True])
def test_deferred_catalog_read_preserves_unconfigured_storage(
    tmp_path: Path, database_module: ModuleType, existing_database: bool
) -> None:
    """Viewing catalog settings does not initialize a missing or legacy store."""
    path = (
        _legacy_database(tmp_path)
        if existing_database
        else tmp_path / "jlcpcb" / "project.db"
    )
    before_paths = set(tmp_path.rglob("*"))
    before_bytes = path.read_bytes() if existing_database else None
    database = database_module.ImpedanceDatabase(path, initialize=False)

    assert database.load_stackup_catalog(6) is None

    assert set(tmp_path.rglob("*")) == before_paths
    if existing_database:
        assert path.read_bytes() == before_bytes


def test_deferred_catalog_read_rejects_partial_feature_schema(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """An incomplete impedance database cannot appear to have no saved catalog."""
    path = tmp_path / "project.db"
    database_module.ImpedanceDatabase(path)
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE impedance_stackup_catalog")
    before = path.read_bytes()
    database = database_module.ImpedanceDatabase(path, initialize=False)

    with pytest.raises(database_module.DatabaseMigrationError):
        database.load_stackup_catalog(6)

    assert path.read_bytes() == before


def test_read_only_lookup_rejects_corrupt_database_without_mutation(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Unrecognized existing file contents are an error, not absent settings."""
    board_path = _board(tmp_path)
    path = tmp_path / "project.db"
    original = b"retain unreadable project database"
    path.write_bytes(original)
    database = database_module.ImpedanceDatabase(path, initialize=False)

    with pytest.raises(database_module.DatabaseMigrationError):
        database.find_board(board_path)

    assert path.read_bytes() == original


def test_explicit_initialization_enables_saving_after_deferred_open(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """The first explicit edit initializes storage and remains available on reopen."""
    board_path = _board(tmp_path)
    path = tmp_path / "jlcpcb" / "project.db"
    database = database_module.ImpedanceDatabase(path, initialize=False)
    database.initialize()
    board_id = database.resolve_board(board_path)
    database.save_config(board_id, {"label": "new"}, False, 0)
    database.initialize()

    reopened = database_module.ImpedanceDatabase(path, initialize=False)
    assert reopened.find_board(board_path) == board_id
    assert reopened.load_config(board_id)["payload"] == {"label": "new"}


def test_read_connection_cannot_create_an_absent_database(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """A database removed after initialization must not reappear during a read."""
    path = tmp_path / "project.db"
    database = database_module.ImpedanceDatabase(path)
    path.unlink()

    with pytest.raises(sqlite3.OperationalError), database.connect():
        pass

    assert not path.exists()


def test_read_connection_is_read_only_at_the_sqlite_file_level(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Read connections cannot become writers by disabling SQLite query_only."""
    path = tmp_path / "project #1?" / "project.db"
    database = database_module.ImpedanceDatabase(path)
    before = path.read_bytes()

    with database.connect() as connection:
        connection.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE unexpected (value TEXT)")

    assert path.read_bytes() == before


@pytest.mark.parametrize("filename", ["", "missing.kicad_pcb", "schematic.kicad_sch"])
def test_read_only_lookup_validates_board_before_absent_storage(
    tmp_path: Path, database_module: ModuleType, filename: str
) -> None:
    """An invalid PCB cannot be treated as a valid board without configuration."""
    path = tmp_path / "jlcpcb" / "project.db"
    database = database_module.ImpedanceDatabase(path, initialize=False)
    candidate = str(tmp_path / filename) if filename else ""

    with pytest.raises(database_module.BoardIdentityError):
        database.find_board(candidate)

    assert not path.parent.exists()


@pytest.mark.parametrize("malformed_parts", [False, True])
def test_impedance_initialization_and_save_preserve_existing_parts_and_counters(
    tmp_path: Path, database_module: ModuleType, malformed_parts: bool
) -> None:
    """Impedance storage adds only its own tables without migrating parts data."""
    board_path = _board(tmp_path)
    path = _legacy_database(tmp_path, malformed=malformed_parts)
    with contextlib.closing(sqlite3.connect(path)) as connection:
        original_schema = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        original_rows = {
            name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
            for name, _sql in original_schema
        }

    database = database_module.ImpedanceDatabase(path)
    board_id = database.resolve_board(board_path)
    database.save_config(board_id, {"label": "USB"}, False, 0)
    reopened = database_module.ImpedanceDatabase(path)
    assert reopened.load_config(board_id)["payload"] == {"label": "USB"}
    with reopened.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == set(original_rows) | {
            "boards",
            "board_feature_config",
            "impedance_stackup_catalog",
            "impedance_calculator_config",
        }
        for name, sql in original_schema:
            assert connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
            ).fetchone() == (sql,)
            assert (
                connection.execute(f'SELECT * FROM "{name}"').fetchall()
                == (original_rows[name])
            )


def test_saved_board_identity_is_stable_and_separates_same_directory_boards(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Overlapping board filenames resolve to distinct persistent identities."""
    first_path, second_path = _board(tmp_path, "main"), _board(tmp_path, "panel")
    path = tmp_path / "jlcpcb" / "project.db"
    database = database_module.ImpedanceDatabase(path)
    first = database.resolve_board(first_path)
    second = database.resolve_board(second_path)

    assert first != second
    assert database.resolve_board(first_path) == first
    assert database_module.ImpedanceDatabase(path).resolve_board(second_path) == second
    with database.connect() as connection:
        boards = connection.execute(
            "SELECT relative_path, display_name FROM boards ORDER BY relative_path"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert len(boards) == 2
    assert all(not Path(row[0]).is_absolute() for row in boards)
    assert {Path(row[0]).name for row in boards} == {
        "main.kicad_pcb",
        "panel.kicad_pcb",
    }


def test_multiple_boards_with_existing_parts_need_no_legacy_ownership_choice(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """The existing project-wide assignments do not participate in impedance identity."""
    first_path, second_path = _board(tmp_path, "main"), _board(tmp_path, "panel")
    database = database_module.ImpedanceDatabase(_legacy_database(tmp_path))
    first = database.resolve_board(first_path)
    second = database.resolve_board(second_path)
    database.save_config(first, {"target_ohms": "50"}, False, 0)
    database.save_config(second, {"target_ohms": "90"}, False, 0)
    assert first != second
    assert database.load_config(first)["payload"] == {"target_ohms": "50"}
    assert database.load_config(second)["payload"] == {"target_ohms": "90"}


@pytest.mark.parametrize("change", ["filename", "registry_path", "deleted_identity"])
def test_changed_board_identity_rejects_stale_editor_without_writes(
    tmp_path: Path, database_module: ModuleType, change: str
) -> None:
    """A stale window cannot keep applying edits to an identity it no longer owns."""
    board_path = _board(tmp_path)
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(board_path)
    database.ensure_current_board(board_id, board_path)
    if change == "filename":
        board_path = _board(tmp_path, "save-as")
    else:
        with database.connect(write=True) as connection:
            if change == "registry_path":
                connection.execute(
                    "UPDATE boards SET relative_path = 'renamed.kicad_pcb'"
                )
            else:
                connection.execute("DELETE FROM boards")
    before = _contents(database.path)
    with pytest.raises(database_module.BoardIdentityError):
        database.ensure_current_board(board_id, board_path)
    assert _contents(database.path) == before


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE boards (board_id TEXT PRIMARY KEY, relative_path TEXT UNIQUE)",
        "CREATE TABLE boards (board_id TEXT, relative_path TEXT PRIMARY KEY, display_name TEXT)",
        "CREATE TABLE boards (board_id TEXT PRIMARY KEY, relative_path TEXT, display_name TEXT)",
        "CREATE TABLE board_feature_config (board_id TEXT REFERENCES boards(board_id), feature TEXT, revision INTEGER, enabled INTEGER, payload_json TEXT, PRIMARY KEY(board_id,feature))",
        "CREATE TABLE board_feature_config (board_id TEXT REFERENCES boards(board_id), feature TEXT PRIMARY KEY, version INTEGER, revision INTEGER, enabled INTEGER, payload_json TEXT)",
        "CREATE TABLE board_feature_config (board_id TEXT, feature TEXT, version INTEGER, revision INTEGER, enabled INTEGER, payload_json TEXT, PRIMARY KEY(board_id,feature))",
        "CREATE TABLE boards (board_id TEXT PRIMARY KEY NOT NULL, relative_path TEXT NOT NULL, display_name TEXT NOT NULL)",
        "CREATE TABLE board_feature_config (board_id TEXT NOT NULL, feature TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL, enabled INTEGER NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(board_id,feature))",
        "CREATE TABLE impedance_stackup_catalog (layer_count INTEGER NOT NULL, payload_json TEXT PRIMARY KEY NOT NULL)",
    ],
)
def test_malformed_owned_table_contracts_are_rejected_atomically(
    tmp_path: Path, database_module: ModuleType, schema: str
) -> None:
    """Invalid impedance tables roll back without altering unrelated content."""
    path = tmp_path / "project.db"
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(schema)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('retain')")
    before = _contents(path)
    with pytest.raises(database_module.DatabaseMigrationError):
        database_module.ImpedanceDatabase(path)
    assert _contents(path) == before


def test_feature_configuration_is_atomic_scoped_and_optimistically_versioned(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Concurrent editors cannot overwrite newer configuration or another board."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    first = database.resolve_board(_board(tmp_path, "main"))
    second = database.resolve_board(_board(tmp_path, "panel"))
    payload = {"specs": [{"label": "USB Ω", "target_ohms": "90"}], "review": None}
    assert database.load_config(first) is None
    revision = database.save_config(first, payload, enabled=True, expected_revision=0)
    assert revision == 1
    assert database.load_config(first) == {
        "version": 1,
        "revision": 1,
        "enabled": True,
        "payload": payload,
    }
    assert database.load_config(second) is None

    with pytest.raises(database_module.ConfigConflictError):
        database.save_config(first, {"specs": []}, enabled=False, expected_revision=0)
    assert database.load_config(first)["payload"] == payload
    assert database.save_config(first, payload, enabled=False, expected_revision=1) == 2
    assert database.load_config(first)["enabled"] is False
    assert database.load_config(first)["payload"] == payload


def test_feature_configuration_failure_preserves_prior_revision(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """An unserializable payload leaves the last saved settings available."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    database.save_config(board_id, {"specs": []}, enabled=False, expected_revision=0)
    before = database.load_config(board_id)
    with pytest.raises((TypeError, ValueError)):
        database.save_config(
            board_id, {"unserializable": object()}, enabled=True, expected_revision=1
        )
    assert database.load_config(board_id) == before


def test_calculator_config_cache_is_shared_without_changing_board_intent(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Calculator metadata survives reopen and stays independent of board revisions."""
    path = tmp_path / "jlcpcb" / "project.db"
    database = database_module.ImpedanceDatabase(path)
    board_id = database.resolve_board(_board(tmp_path, "main"))
    database.save_config(board_id, {"target_ohms": "50"}, False, 0)
    before = database.load_config(board_id)
    cache = {
        "schema_version": 1,
        "models": [{"impedanceType": "CoatedMicrostrip1B"}],
        "copper": [{"baseCopperThickness": "1"}],
        "coating": [{"coatingAboveSubstrate": "0.01"}],
        "limits": [{"impedanceName": "W2", "minValue": "2.5", "maxValue": "100"}],
        "checked_at_utc": "2026-09-14T14:00:00.000000Z",
    }
    database.save_calculator_config(cache)
    reopened = database_module.ImpedanceDatabase(path)
    assert reopened.load_calculator_config() == cache
    assert reopened.load_config(board_id) == before


@pytest.mark.parametrize("layer_count", [True, 1, 65, "6", 6.0])
def test_invalid_catalog_layer_count_leaves_cached_data_unchanged(
    tmp_path: Path, database_module: ModuleType, layer_count: Any
) -> None:
    """Invalid catalog keys cannot replace an existing successful fetch."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    database.save_stackup_catalog(6, {"retain": True})
    before = _contents(database.path)
    with pytest.raises(ValueError):
        database.save_stackup_catalog(layer_count, {})
    with pytest.raises(ValueError):
        database.load_stackup_catalog(layer_count)
    assert _contents(database.path) == before


@pytest.mark.parametrize("payload_json", ["not JSON", "[]", "null"])
def test_corrupt_catalog_cache_is_reported_without_rewriting_it(
    tmp_path: Path, database_module: ModuleType, payload_json: str
) -> None:
    """Corrupt cache data remains intact for the catalog caller's recovery policy."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO impedance_stackup_catalog VALUES (6, ?)", (payload_json,)
        )
    before = _contents(database.path)
    with pytest.raises(ValueError):
        database.load_stackup_catalog(6)
    assert _contents(database.path) == before


def test_concurrent_configuration_saves_have_exactly_one_winner(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Two dialogs saving the same revision cannot silently overwrite each other."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(database.save_config, board_id, {"label": label}, False, 0)
            for label in ("first", "second")
        ]
        failures = [future.exception() for future in futures]
    assert failures.count(None) == 1
    assert (
        sum(
            isinstance(error, database_module.ConfigConflictError) for error in failures
        )
        == 1
    )
    assert database.load_config(board_id)["revision"] == 1
    assert database.load_config(board_id)["payload"] in (
        {"label": "first"},
        {"label": "second"},
    )


@pytest.mark.parametrize("payload_json", ["not JSON", "[]", '{"enabled": true}'])
def test_corrupt_configuration_does_not_silently_disable_feature(
    tmp_path: Path, database_module: ModuleType, payload_json: str
) -> None:
    """Malformed saved records surface errors instead of becoming absent settings."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO board_feature_config VALUES (?, 'impedance', 1, 1, 0, ?)",
            (board_id, payload_json),
        )
    with pytest.raises((ValueError, TypeError)):
        database.load_config(board_id)


@pytest.mark.parametrize(
    ("version", "revision", "enabled"),
    [(1, 1, 2), (2, 1, 0), ("invalid", 1, 0), (1, "invalid", 0), (1, 0, 0)],
)
def test_invalid_raw_configuration_metadata_is_rejected(
    tmp_path: Path,
    database_module: ModuleType,
    version: Any,
    revision: Any,
    enabled: Any,
) -> None:
    """Unknown schemas and corrupt record scalars cannot appear as valid settings."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    with database.connect(write=True) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO board_feature_config VALUES (?, 'impedance', ?, ?, ?, '{}')",
            (board_id, version, revision, enabled),
        )
    with pytest.raises((ValueError, TypeError)):
        database.load_config(board_id)


def _empty_config() -> dict[str, Any]:
    """Represent the current complete disabled feature document for reset."""
    return {
        "schema_version": 5,
        "enabled": False,
        "specifications": [],
        "reviewed_digest": "",
        "included_section_ids": [],
        "review_tracking": {"images": [], "layers": []},
        "stackup": None,
        "width_results": [],
    }


@pytest.mark.parametrize(
    ("version", "revision", "enabled", "payload_json", "next_revision"),
    [
        (1, 4, 1, '{"broken": Ω', 5),
        (2, 8, 1, '{"future":true}', 9),
        (1, "broken", 2, "[]", 1),
        (1, 9, 1, b"invalid JSON blob\x00\xff", 10),
        (1, float("inf"), 1, "{}", 1),
    ],
)
def test_configuration_reset_replaces_invalid_record_without_creating_archive(
    tmp_path: Path,
    database_module: ModuleType,
    version: Any,
    revision: Any,
    enabled: Any,
    payload_json: Any,
    next_revision: int,
) -> None:
    """Explicit reset replaces invalid settings with disabled defaults, without history."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    with database.connect(write=True) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO board_feature_config VALUES (?, 'impedance', ?, ?, ?, ?)",
            (board_id, version, revision, enabled, payload_json),
        )
    token = database.config_reset_token(board_id)
    assert len(token) == 64 and all(char in "0123456789abcdef" for char in token)
    assert database.config_reset_token(board_id) == token
    assert database.reset_config(board_id, token, _empty_config()) == next_revision
    assert database.load_config(board_id) == {
        "version": 1,
        "revision": next_revision,
        "enabled": False,
        "payload": _empty_config(),
    }
    with database.connect() as connection:
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {
            "boards",
            "board_feature_config",
            "impedance_stackup_catalog",
            "impedance_calculator_config",
        }
    assert database.config_reset_token(board_id) != token


def test_configuration_reset_rejects_stale_confirmation(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """A reset confirmation cannot replace newer settings from another window."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    database.save_config(board_id, _empty_config(), enabled=False, expected_revision=0)
    token = database.config_reset_token(board_id)
    database.save_config(board_id, _empty_config(), enabled=False, expected_revision=1)
    before = _contents(database.path)
    with pytest.raises(database_module.ConfigConflictError):
        database.reset_config(board_id, token, _empty_config())
    assert _contents(database.path) == before


def test_configuration_reset_failure_preserves_original_settings(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """A failed replacement leaves the original row and every other table untouched."""
    path = tmp_path / "jlcpcb" / "project.db"
    database = database_module.ImpedanceDatabase(path)
    board_id = database.resolve_board(_board(tmp_path))
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO board_feature_config VALUES (?, 'impedance', 1, 3, 1, 'broken')",
            (board_id,),
        )
        connection.execute(
            "CREATE TRIGGER reject_reset BEFORE UPDATE ON board_feature_config "
            "BEGIN SELECT RAISE(ABORT, 'injected reset failure'); END"
        )
    token = database.config_reset_token(board_id)
    before = _contents(path)
    with pytest.raises(sqlite3.DatabaseError, match="injected reset failure"):
        database.reset_config(board_id, token, _empty_config())
    assert _contents(path) == before
    assert database.config_reset_token(board_id) == token


@pytest.mark.parametrize("payload", [{"enabled": True}, {"target_ohms": float("nan")}])
def test_invalid_config_content_is_rejected_without_a_saved_revision(
    tmp_path: Path, database_module: ModuleType, payload: dict[str, Any]
) -> None:
    """Nonfinite JSON numbers and contradictory enable state cannot be stored."""
    database = database_module.ImpedanceDatabase(tmp_path / "jlcpcb" / "project.db")
    board_id = database.resolve_board(_board(tmp_path))
    with pytest.raises(ValueError):
        database.save_config(board_id, payload, enabled=False, expected_revision=0)
    assert database.load_config(board_id) is None


def test_project_directory_move_preserves_board_identity(
    tmp_path: Path, database_module: ModuleType
) -> None:
    """Board paths remain valid when a complete project directory is relocated."""
    original_root = tmp_path / "original"
    original_root.mkdir()
    original_board = _board(original_root)
    database = database_module.ImpedanceDatabase(
        original_root / "jlcpcb" / "project.db"
    )
    board_id = database.resolve_board(original_board)
    database.save_config(
        board_id, {"label": "retain"}, enabled=False, expected_revision=0
    )
    relocated_root = tmp_path / "relocated"
    original_root.rename(relocated_root)
    relocated = database_module.ImpedanceDatabase(
        relocated_root / "jlcpcb" / "project.db"
    )
    assert relocated.resolve_board(relocated_root / original_board.name) == board_id
    assert relocated.load_config(board_id)["payload"] == {"label": "retain"}


@pytest.mark.parametrize("filename", ["", "missing.kicad_pcb", "schematic.kicad_sch"])
def test_invalid_board_path_does_not_create_storage(
    tmp_path: Path, database_module: ModuleType, filename: str
) -> None:
    """An unsaved or non-board document cannot create a database directory."""
    path = tmp_path / "jlcpcb" / "project.db"
    candidate = str(tmp_path / filename) if filename else ""
    with pytest.raises(database_module.BoardIdentityError):
        database_module.ImpedanceDatabase.validate_board_path(candidate)
    assert not path.parent.exists()
