"""Exercise correction persistence and recovery against real SQLite files."""

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.correction_test_support import (
    fresh_library,
    make_library,
    raw_rows,
    seed_raw,
)
from tests.wx_harness import load_correction_modules


@pytest.fixture
def modules() -> Iterator[SimpleNamespace]:
    """Import actual correction modules with isolated GUI dependencies."""
    with load_correction_modules() as loaded:
        yield loaded


@pytest.fixture
def library(modules, tmp_path):
    """Create a correction database confined to this test's directory."""
    return make_library(modules.library, tmp_path)


def execute(path, sql, parameters=()):
    """Execute and commit SQL using an independent connection."""
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute(sql, parameters).fetchall()


def install_abort_trigger(path, *, operation="INSERT", pattern="explode"):
    """Abort a genuine later write so tests prove transaction rollback."""
    assert operation in {"INSERT", "UPDATE", "DELETE"}
    reference = "OLD" if operation == "DELETE" else "NEW"
    with closing(sqlite3.connect(path)) as connection, connection:
        # Trigger syntax cannot bind a WHEN value; quote the fixture value.
        quoted = pattern.replace("'", "''")
        connection.execute(
            f"CREATE TRIGGER fail_write BEFORE {operation} ON correction "
            f"WHEN {reference}.regex = '{quoted}' "
            "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
        )


def seed_legacy(path, rows):
    """Create a historical rotation table, retaining raw invalid values."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE rotation (regex, rotation)")
        connection.executemany("INSERT INTO rotation VALUES (?, ?)", rows)


def migration_rows(library):
    """Inspect committed migration records from a new connection."""
    return execute(
        library.globalcorrectionsdb_file,
        "SELECT migration_key, source FROM correction_migrations ORDER BY migration_key",
    )


def immediate_thread(
    *, target: Callable[..., Any], args: tuple[Any, ...], daemon: bool
) -> SimpleNamespace:
    """Run requested work synchronously without changing the callback or arguments."""
    return SimpleNamespace(start=lambda: target(*args))


def prepare_constructor_storage(library: Any) -> None:
    """Start the real constructor with project settings and no correction database."""
    library.parent.settings = {"library": {"data_path": library.datadir}}
    library.create_mapping_table()
    Path(library.globalcorrectionsdb_file).unlink()


def correction_values(library: Any, **kwargs: Any) -> Any:
    """Compare stable field values while checking the public typed-set contract."""
    corrections = library.get_all_correction_data(**kwargs)
    if corrections is None:
        return None
    assert isinstance(corrections, tuple)
    return [(item.pattern, item.rotation, item.offset) for item in corrections]


def test_ordinary_snapshots_never_probe_legacy_storage(
    library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrelated archives cannot change a previously ready active snapshot."""
    seed_raw(library, [("custom", 270, 1, 2)])
    library.migrate_corrections()
    Path(library.partsdb_file).write_bytes(b"download currently replacing parts")

    def forbidden_probe(source: str) -> None:
        pytest.fail(f"ordinary correction read inspected {source}")

    monkeypatch.setattr(library, "_legacy_rotation_rows", forbidden_probe)
    assert correction_values(library) == [("custom", 270, (1.0, 2.0))]


@pytest.mark.parametrize("kind", ["missing", "zero", "no-table", "empty"])
def test_examined_empty_sources_and_settled_startup_are_read_only(
    library: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """No-data archives are terminal without making every startup a writer."""
    if kind == "zero":
        Path(library.rotationsdb_file).touch()
    elif kind == "no-table":
        execute(library.rotationsdb_file, "CREATE TABLE unrelated (value)")
    elif kind == "empty":
        seed_legacy(library.rotationsdb_file, [])
    seed_raw(library, [("custom", 90, 1, 2)])
    library.create_mapping_table()
    assert library.migrate_corrections() == ()
    reopened = fresh_library(library)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("settled startup acquired a write lock or inspected an archive")

    monkeypatch.setattr(reopened, "_correction_transaction", forbidden)
    monkeypatch.setattr(reopened, "_legacy_rotation_rows", forbidden)
    reopened.check_library()
    assert correction_values(reopened) == [("custom", 90, (1.0, 2.0))]


@pytest.mark.parametrize("rows", [[], [("custom", 90)]])
def test_aliases_of_captured_legacy_sources_complete_once(
    modules: SimpleNamespace, library: Any, rows: list[tuple[str, int]]
) -> None:
    """Changing directory spelling after interrupted startup preserves canonical identity."""
    seed_legacy(library.rotationsdb_file, rows)
    execute(
        library.globalcorrectionsdb_file,
        "INSERT INTO correction_migration_state VALUES (?, ?, 'seed-pending', ?)",
        (
            modules.library._INITIAL_DEFAULTS_KEY,
            library.globalcorrectionsdb_file,
            modules.library.json.dumps(
                [library.rotationsdb_file, library.partsdb_file]
            ),
        ),
    )
    alias = Path(library.datadir).parent / "alias"
    alias.symlink_to(library.datadir, target_is_directory=True)
    library.rotationsdb_file = str(alias / "rotations.db")
    library.partsdb_file = str(alias / "parts.db")
    assert library.migrate_corrections() == ()
    assert len(migration_rows(library)) == 2
    assert correction_values(library) == [
        (pattern, rotation, (0.0, 0.0)) for pattern, rotation in rows
    ]
    assert execute(
        library.globalcorrectionsdb_file,
        "SELECT status FROM correction_migration_state",
    ) == [("seed-pending",)]


@pytest.mark.parametrize("key", [None, b"unknown"])
@pytest.mark.parametrize("table", ["completed", "pending", "deferred"])
def test_unrecognized_metadata_keys_preserve_repair_and_pending_diagnostics(
    modules: SimpleNamespace, library: Any, key: object, table: str
) -> None:
    """SQLite permits non-text keys; unrelated metadata cannot crash repair or hide pending work."""
    seed_raw(library, [("broken", "47u", 0, 0)])
    if table == "completed":
        execute(
            library.correctionsdb_file,
            "INSERT INTO correction_migrations VALUES (?, ?)",
            (key, "old-tool"),
        )
    else:
        execute(
            library.correctionsdb_file,
            "INSERT INTO correction_migration_state VALUES (?, ?, ?, ?)",
            (key, library.rotationsdb_file, table, "restore custom corrections"),
        )
    library.migrate_corrections()
    snapshot = library.read_correction_data()
    assert snapshot.rows[0].rotation == "47u"
    assert snapshot.state is (
        modules.library.CorrectionState.UNAVAILABLE
        if table == "pending"
        else modules.library.CorrectionState.NEEDS_REPAIR
    )
    assert snapshot.csv_migrations == ()
    if table != "completed":
        assert "restore custom corrections" in str(
            snapshot.issues if table == "pending" else snapshot.warnings
        )


def test_old_completed_legacy_and_csv_markers_stay_settled(
    library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding lifecycle state cannot replay archives completed by an earlier version."""
    seed_raw(library, [("repaired", 270, 1, 2)])
    for source in (library.rotationsdb_file, library.partsdb_file):
        seed_legacy(source, [("repaired", "47u"), ("deleted", 90)])
        execute(
            library.globalcorrectionsdb_file,
            "INSERT INTO correction_migrations VALUES (?, ?)",
            (library._legacy_migration_key(source), source),
        )
    execute(
        library.globalcorrectionsdb_file,
        "INSERT INTO correction_migrations VALUES ('csv:preserved', 'legacy.csv')",
    )
    execute(library.globalcorrectionsdb_file, "DROP TABLE correction_migration_state")
    library.create_mapping_table()
    reopened = fresh_library(library)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("completed historical metadata caused a write or legacy probe")

    monkeypatch.setattr(reopened, "_correction_transaction", forbidden)
    monkeypatch.setattr(reopened, "_legacy_rotation_rows", forbidden)
    reopened.check_library()
    assert correction_values(reopened) == [("repaired", 270, (1.0, 2.0))]
    assert reopened.read_correction_data().csv_migrations == (
        ("csv:preserved", "legacy.csv"),
    )


def test_unknown_archive_warning_defers_lower_priority_and_retries(
    modules: SimpleNamespace, library: Any
) -> None:
    """Recovered custom rotations cannot lose to prematurely imported parts values."""
    seed_raw(library, [("existing", 270, 1, 2)])
    Path(library.rotationsdb_file).write_bytes(b"temporarily unreadable")
    seed_legacy(library.partsdb_file, [("priority", 180), ("parts-only", 0)])
    assert library.migrate_corrections() == ()
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.READY
    assert any(issue.source == library.rotationsdb_file for issue in snapshot.warnings)
    assert correction_values(library) == [("existing", 270, (1.0, 2.0))]
    Path(library.rotationsdb_file).unlink()
    seed_legacy(library.rotationsdb_file, [("priority", 90)])
    assert fresh_library(library).retry_correction_migrations() == ()
    assert correction_values(library) == [
        ("existing", 270, (1.0, 2.0)),
        ("parts-only", 0, (0.0, 0.0)),
        ("priority", 90, (0.0, 0.0)),
    ]
    assert fresh_library(library).read_correction_data().warnings == ()


def test_failed_known_transfer_remains_blocking_without_source_probes(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback retains durable knowledge of missing custom corrections."""
    seed_legacy(library.rotationsdb_file, [("explode", 90)])
    install_abort_trigger(library.globalcorrectionsdb_file)
    assert library.migrate_corrections()
    reopened = fresh_library(library)

    def forbidden_probe(source: str) -> None:
        pytest.fail(f"snapshot rediscovered pending source {source}")

    monkeypatch.setattr(reopened, "_legacy_rotation_rows", forbidden_probe)
    snapshot = reopened.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.UNAVAILABLE
    assert snapshot.corrections is None
    assert any(issue.source == library.rotationsdb_file for issue in snapshot.issues)
    assert raw_rows(library) == []


def test_pending_metadata_failure_blocks_captured_target_until_retry(
    modules: SimpleNamespace, library: Any
) -> None:
    """Failure to persist positive knowledge cannot make the same session ready."""
    seed_raw(library, [("existing", 270, 1, 2)])
    seed_legacy(library.rotationsdb_file, [("custom", 90)])
    execute(
        library.globalcorrectionsdb_file,
        "CREATE TRIGGER deny_pending BEFORE INSERT ON correction_migration_state "
        "WHEN NEW.status='pending' BEGIN SELECT RAISE(ABORT, 'pending metadata denied'); END",
    )
    before = raw_rows(library)
    library.create_mapping_table()
    library.parent.settings = {"library": {"data_path": library.datadir}}
    library = modules.library.Library(library.parent)
    assert library.retry_correction_migrations()
    snapshot = library.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.UNAVAILABLE
    assert snapshot.corrections is None
    assert "pending metadata denied" in str(snapshot.issues)
    assert raw_rows(library) == before
    library.create_correction_table(library.localcorrectionsdb_file)
    library.correctionsdb_file = library.localcorrectionsdb_file
    assert library.read_correction_data().corrections == ()
    library.correctionsdb_file = library.globalcorrectionsdb_file
    execute(library.globalcorrectionsdb_file, "DROP TRIGGER deny_pending")
    assert library.retry_correction_migrations() == ()
    assert correction_values(library) == [
        ("custom", 90, (0.0, 0.0)),
        ("existing", 270, (1.0, 2.0)),
    ]


@pytest.mark.parametrize("unreadable", [False, True])
def test_failed_optional_bookkeeping_preserves_ready_corrections(
    modules: SimpleNamespace, library: Any, unreadable: bool
) -> None:
    """A read-only destination does not turn absence or unknown archives into lost data."""
    seed_raw(library, [("existing", 270, 1, 2)])
    if unreadable:
        Path(library.rotationsdb_file).write_bytes(b"unclassified archive")
    execute(
        library.globalcorrectionsdb_file,
        "CREATE TRIGGER deny_state BEFORE INSERT ON "
        + ("correction_migration_state " if unreadable else "correction_migrations ")
        + "BEGIN SELECT RAISE(ABORT, 'bookkeeping denied'); END",
    )
    assert library.retry_correction_migrations() == ()
    snapshot = library.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.READY
    assert snapshot.issues == ()
    assert snapshot.warnings
    if unreadable:
        assert any(
            issue.source == library.rotationsdb_file for issue in snapshot.warnings
        )
    assert correction_values(library) == [("existing", 270, (1.0, 2.0))]


@pytest.mark.parametrize("damage", ["missing", "no-table", "unreadable", "empty"])
def test_known_pending_source_cannot_be_downgraded_after_later_damage(
    modules: SimpleNamespace, library: Any, damage: str
) -> None:
    """Positive knowledge survives later loss of readable source rows."""
    seed_legacy(library.rotationsdb_file, [("explode", 90)])
    install_abort_trigger(library.globalcorrectionsdb_file)
    assert library.migrate_corrections()
    execute(library.globalcorrectionsdb_file, "DROP TRIGGER fail_write")
    Path(library.rotationsdb_file).unlink()
    if damage == "no-table":
        execute(library.rotationsdb_file, "CREATE TABLE unrelated (value)")
    elif damage == "unreadable":
        Path(library.rotationsdb_file).write_bytes(b"unreadable now")
    elif damage == "empty":
        seed_legacy(library.rotationsdb_file, [])
    reopened = fresh_library(library)
    assert reopened.retry_correction_migrations()
    snapshot = reopened.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.UNAVAILABLE
    assert snapshot.corrections is None
    assert any(issue.source == library.rotationsdb_file for issue in snapshot.issues)
    assert raw_rows(library) == []


def test_changing_parts_library_keeps_unfinished_archive_priority(library: Any) -> None:
    """A newly selected parts source cannot outrank an earlier deferred archive."""
    first_parts = Path(library.partsdb_file)
    first_parts.write_bytes(b"pending old library download")
    assert library.migrate_corrections() == ()
    library.partsdb_file = str(first_parts.with_name("other-parts.db"))
    seed_legacy(library.partsdb_file, [("same", 180)])
    assert library.migrate_corrections() == ()
    assert raw_rows(library) == []
    first_parts.unlink()
    seed_legacy(first_parts, [("same", 90)])
    assert fresh_library(library).retry_correction_migrations() == ()
    assert correction_values(library) == [("same", 90, (0.0, 0.0))]


@pytest.mark.parametrize(
    "condition",
    ["empty", "valid", "row", "conflict", "migration", "storage", "metadata"],
)
def test_snapshot_readiness_distinguishes_empty_repair_and_unavailable(
    modules: SimpleNamespace, library: Any, condition: str
) -> None:
    """Only complete ready sets are usable, including the deliberately empty set."""
    if condition != "empty":
        seed_raw(library, [("existing", 90, 1.25, -2.5)])
    if condition == "row":
        seed_raw(library, [("broken", "47u", 0, 0)])
    elif condition == "conflict":
        seed_raw(library, [("existing", 180, 0, 0)])
    elif condition == "migration":
        seed_legacy(library.rotationsdb_file, [("pending", 180)])
        install_abort_trigger(library.globalcorrectionsdb_file, pattern="pending")
        assert library.migrate_corrections()
    elif condition == "storage":
        Path(library.correctionsdb_file).write_bytes(b"broken destination")
    elif condition == "metadata":
        execute(library.correctionsdb_file, "DROP TABLE correction_migrations")
        execute(
            library.correctionsdb_file, "CREATE TABLE correction_migrations (wrong)"
        )

    snapshot = fresh_library(library).read_correction_data()

    if condition in {"empty", "valid"}:
        assert snapshot.state is modules.library.CorrectionState.READY
        assert snapshot.corrections == (
            ()
            if condition == "empty"
            else (modules.data.Correction("existing", 90, (1.25, -2.5)),)
        )
        assert snapshot.issues == ()
    else:
        assert snapshot.corrections is None
        assert snapshot.issues
        expected = (
            modules.library.CorrectionState.NEEDS_REPAIR
            if condition in {"row", "conflict"}
            else modules.library.CorrectionState.UNAVAILABLE
        )
        assert snapshot.state is expected
        if condition != "storage":
            assert any(row.pattern == "existing" for row in snapshot.rows)


@pytest.mark.parametrize("legacy_rotation", [0, "47u"])
@pytest.mark.parametrize("destination_rotation", [270, "47u"])
def test_pending_legacy_sources_preserve_established_destination(
    library: Any, legacy_rotation: Any, destination_rotation: Any
) -> None:
    """A pre-marker installation keeps repaired offsets and any remaining bad data."""
    seed_raw(library, [("established", destination_rotation, 1.25, -2.5)])
    before = raw_rows(library)
    originals = [("established", legacy_rotation)]
    seed_legacy(library.rotationsdb_file, originals)
    seed_legacy(library.partsdb_file, originals)

    assert library.migrate_corrections() == ()

    assert raw_rows(library) == before
    assert len(migration_rows(library)) == 2
    for source in (library.rotationsdb_file, library.partsdb_file):
        assert execute(source, "SELECT * FROM rotation") == originals
    fresh_library(library).check_library()
    assert raw_rows(library) == before


@pytest.mark.parametrize("failure", ["row", "marker"])
def test_pending_legacy_batch_failure_rolls_back_every_source(
    library: Any, failure: str
) -> None:
    """A later source failure cannot commit earlier rows or completion markers."""
    seed_raw(library, [("established", 270, 1.25, -2.5)])
    before = raw_rows(library)
    seed_legacy(library.rotationsdb_file, [("first-source", 90)])
    seed_legacy(library.partsdb_file, [("explode", 180)])
    if failure == "row":
        install_abort_trigger(library.globalcorrectionsdb_file)
    else:
        execute(
            library.globalcorrectionsdb_file,
            "CREATE TRIGGER fail_marker BEFORE INSERT ON correction_migrations "
            "WHEN NEW.source LIKE '%parts.db' "
            "BEGIN SELECT RAISE(ABORT, 'injected marker failure'); END",
        )

    assert library.migrate_corrections()
    assert raw_rows(library) == before
    assert migration_rows(library) == []
    assert fresh_library(library).read_correction_data().issues

    trigger = "fail_write" if failure == "row" else "fail_marker"
    execute(library.globalcorrectionsdb_file, f"DROP TRIGGER {trigger}")
    fresh_library(library).check_library()
    assert [row[1:] for row in raw_rows(library)] == [
        ("established", 270, 1.25, -2.5),
        ("first-source", 90, 0, 0),
        ("explode", 180, 0, 0),
    ]
    assert len(migration_rows(library)) == 2


def test_new_pending_source_preserves_a_repair_after_partial_migration(
    library: Any,
) -> None:
    """A later archive cannot undo a repair after another source was completed."""
    seed_legacy(library.rotationsdb_file, [("repaired", 90)])
    library.migrate_corrections_from_rotation()
    rowid = raw_rows(library)[0][0]
    library.save_correction_data("repaired", 270, (1.25, -2.5), rowid=rowid)
    seed_legacy(library.partsdb_file, [("repaired", "47u"), ("new", 180)])
    assert fresh_library(library).migrate_corrections() == ()
    assert correction_values(fresh_library(library)) == [
        ("new", 180, (0.0, 0.0)),
        ("repaired", 270, (1.25, -2.5)),
    ]
    assert len(migration_rows(library)) == 2


@pytest.mark.parametrize("pattern", [None, 17, b"raw", "", "  "])
def test_unidentifiable_legacy_patterns_are_never_silently_suppressed(
    library: Any, pattern: Any
) -> None:
    """Raw invalid identity values remain repairable in destination and both archives."""
    seed_raw(library, [(pattern, 270, 1, 2)])
    seed_legacy(library.rotationsdb_file, [(pattern, 90)])
    seed_legacy(library.partsdb_file, [(pattern, "47u")])
    assert library.migrate_corrections() == ()
    assert [row[1:] for row in raw_rows(library)] == [
        (pattern, 270, 1, 2),
        (pattern, 90, 0, 0),
        (pattern, "47u", 0, 0),
    ]
    assert fresh_library(library).get_all_correction_data() is None


def test_established_invalid_regex_is_preserved_without_legacy_duplicates(
    library: Any,
) -> None:
    """An identifiable existing row remains the user's repair target even if invalid."""
    seed_raw(library, [("[", "47u", 1.25, -2.5)])
    before = raw_rows(library)
    seed_legacy(library.rotationsdb_file, [("[", 90)])
    assert library.migrate_corrections() == ()
    assert raw_rows(library) == before
    assert fresh_library(library).get_all_correction_data() is None


def test_winning_legacy_source_retains_its_internal_conflicts(
    modules: Any, library: Any
) -> None:
    """Rotations beats parts while contradictions within rotations remain repairable."""
    seed_legacy(library.rotationsdb_file, [("same", 90), ("same", 180)])
    seed_legacy(library.partsdb_file, [("same", "47u")])
    assert library.migrate_corrections() == ()
    snapshot = fresh_library(library).read_correction_data()
    assert [row.rotation for row in snapshot.rows] == [90, 180]
    assert all(
        any("conflicting" in issue.message for issue in row.issues)
        for row in snapshot.rows
    )
    assert snapshot.state is modules.library.CorrectionState.NEEDS_REPAIR
    assert snapshot.corrections is None
    assert len(migration_rows(library)) == 2
    before = raw_rows(library)
    fresh_library(library).check_library()
    assert raw_rows(library) == before


@pytest.mark.parametrize(
    "failure", ["invalid-global", "migration", "migration-write", "drop"]
)
def test_failed_discard_preserves_invalid_local_records_and_selection(
    modules: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A failed destination check or actual SQL drop cannot discard active repairs."""
    instance = make_library(
        modules.library, tmp_path, [("local", "47u", 1, 2)], local=True
    )
    instance.create_correction_table(instance.globalcorrectionsdb_file)
    before = raw_rows(instance)
    if failure == "invalid-global":
        execute(
            instance.globalcorrectionsdb_file,
            "INSERT INTO correction VALUES ('global','47u',0,0)",
        )
    elif failure == "migration":
        seed_legacy(instance.rotationsdb_file, [("explode", 90)])
        install_abort_trigger(instance.globalcorrectionsdb_file)
    elif failure == "migration-write":
        seed_legacy(instance.rotationsdb_file, [("custom", 90)])
        execute(instance.globalcorrectionsdb_file, "DROP TABLE correction_migrations")
        connect = sqlite3.connect

        def deny_metadata(operation: int, table: str, *arguments: Any) -> int:
            return (
                sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_CREATE_TABLE
                and table == "correction_migrations"
                else sqlite3.SQLITE_OK
            )

        def protected_connect(target: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = connect(target, **kwargs)
            if target == instance.globalcorrectionsdb_file:
                connection.set_authorizer(deny_metadata)
            return connection

        monkeypatch.setattr(sqlite3, "connect", protected_connect)
    else:
        transaction = instance._correction_transaction

        def deny_drop(operation: int, *arguments: Any) -> int:
            return (
                sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_DROP_TABLE
                else sqlite3.SQLITE_OK
            )

        @contextmanager
        def protected_transaction(
            target: Any, **kwargs: Any
        ) -> Iterator[sqlite3.Connection]:
            with transaction(target, **kwargs) as connection:
                if target == instance.localcorrectionsdb_file:
                    connection.set_authorizer(deny_drop)
                yield connection

        monkeypatch.setattr(instance, "_correction_transaction", protected_transaction)
    with pytest.raises(modules.data.CorrectionDataError):
        instance.switch_to_global_correction_database(True)
    assert instance.correctionsdb_file == instance.localcorrectionsdb_file
    assert raw_rows(instance) == before
    assert fresh_library(instance).uses_global_correction_database() is False


@pytest.mark.parametrize(
    "legacy_row",
    [
        (17, 90),
        (17.5, 90),
        ("valid-pattern", "-9223372036854775809"),
        ("valid-pattern", "9007199254740993.0"),
    ],
)
def test_legacy_migration_rejects_affinity_changes_to_original_data(
    modules: SimpleNamespace, library: Any, legacy_row: tuple[object, object]
) -> None:
    """A typed destination cannot turn malformed archived values into usable corrections."""
    execute(library.correctionsdb_file, "DROP TABLE correction")
    execute(
        library.correctionsdb_file,
        "CREATE TABLE correction (regex TEXT, rotation INTEGER, offset_x REAL, offset_y REAL)",
    )
    seed_legacy(library.rotationsdb_file, [("first", 90)])
    seed_legacy(library.partsdb_file, [legacy_row])
    assert library.migrate_corrections()
    assert raw_rows(library) == []
    assert migration_rows(library) == []
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.UNAVAILABLE
    assert snapshot.corrections is None
    assert execute(library.partsdb_file, "SELECT * FROM rotation") == [legacy_row]


def test_typed_legacy_destination_allows_safe_normalization_and_raw_repairs(
    modules: SimpleNamespace, library: Any
) -> None:
    """Safe numeric representations normalize while compatible malformed rows stay raw."""
    execute(library.correctionsdb_file, "DROP TABLE correction")
    execute(
        library.correctionsdb_file,
        "CREATE TABLE correction (regex TEXT, rotation INTEGER, offset_x REAL, offset_y REAL)",
    )
    seed_legacy(library.rotationsdb_file, [("integer", 90), ("numeric-text", "90.0")])
    seed_legacy(library.partsdb_file, [("bad", "47u"), (None, 180)])
    assert library.migrate_corrections() == ()
    assert [row[1:] for row in raw_rows(library)] == [
        ("integer", 90, 0.0, 0.0),
        ("numeric-text", 90, 0.0, 0.0),
        ("bad", "47u", 0.0, 0.0),
        (None, 180, 0.0, 0.0),
    ]
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.NEEDS_REPAIR
    assert snapshot.corrections is None
    assert len(migration_rows(library)) == 2


@pytest.mark.parametrize("affinity", ["", "INTEGER", "NUMERIC", "REAL"])
def test_supported_offset_schemas_round_trip_float_precision(
    modules: SimpleNamespace, library: Any, affinity: str
) -> None:
    """All supported offset affinities preserve normalized floating-point values."""
    execute(library.correctionsdb_file, "DROP TABLE correction")
    execute(
        library.correctionsdb_file,
        f"CREATE TABLE correction (regex TEXT, rotation INTEGER, offset_x {affinity}, offset_y {affinity})",
    )
    original = modules.data.Correction(
        "precise", 90, (0.12345678901234567, -0.23456789012345678)
    )
    library.apply_corrections([original])
    assert fresh_library(library).get_all_correction_data() == (original,)


@pytest.mark.parametrize("offset_column", ["offset_x", "offset_y"])
def test_text_offset_schema_cannot_round_validated_values(
    modules: SimpleNamespace, library: Any, offset_column: str
) -> None:
    """SQLite's decimal conversion for TEXT offsets loses significant float digits."""
    execute(library.correctionsdb_file, "DROP TABLE correction")
    declarations = [
        f"{name} {'TEXT' if name == offset_column else 'REAL'}"
        for name in ("offset_x", "offset_y")
    ]
    execute(
        library.correctionsdb_file,
        "CREATE TABLE correction (regex TEXT, rotation INTEGER, "
        + ", ".join(declarations)
        + ")",
    )
    original = modules.data.Correction(
        "precise", 90, (0.12345678901234567, -0.23456789012345678)
    )
    with pytest.raises(modules.data.CorrectionDataError, match="offset"):
        library.apply_corrections([original])
    assert raw_rows(library) == []


@pytest.mark.parametrize("broken_source", [False, True])
def test_constructor_waits_for_legacy_batch_before_remote_defaults(
    modules: SimpleNamespace,
    library: Any,
    monkeypatch: pytest.MonkeyPatch,
    broken_source: bool,
) -> None:
    """Default downloads cannot become established corrections ahead of archived custom values."""
    library.partsdb_file = str(
        Path(library.datadir)
        / modules.library.LIBRARY_CONFIGS[modules.library.DEFAULT_LIBRARY].name
    )
    prepare_constructor_storage(library)
    seed_legacy(library.rotationsdb_file, [("custom", 270)])
    if broken_source:
        Path(library.partsdb_file).write_bytes(b"unreadable pending archive")
    else:
        seed_legacy(library.partsdb_file, [("other", 180)])
    http_get = MagicMock(
        return_value=response("Pattern,Rotation\ncustom,90\nremote-only,0\n")
    )
    monkeypatch.setattr(modules.library.requests, "get", http_get)

    monkeypatch.setattr(modules.library, "Thread", immediate_thread)
    first = modules.library.Library(library.parent)
    if broken_source:
        http_get.assert_not_called()
        assert (
            first.read_correction_data().state is modules.library.CorrectionState.READY
        )
        assert first.read_correction_data().warnings
        assert first.get_correction_data("custom") == ("custom", 270, 0, 0)
        assert len(migration_rows(first)) == 1
        Path(library.partsdb_file).unlink()
        seed_legacy(library.partsdb_file, [("other", 180)])
    else:
        http_get.assert_called_once()
    reopened = modules.library.Library(library.parent)
    assert (
        reopened.read_correction_data().state is modules.library.CorrectionState.READY
    )
    assert reopened.get_correction_data("custom") == ("custom", 270, 0, 0)
    assert reopened.get_correction_data("other") == ("other", 180, 0, 0)
    assert reopened.get_correction_data("remote-only") == ("remote-only", 0, 0, 0)
    http_get.assert_called_once()
    execute(reopened.globalcorrectionsdb_file, "DELETE FROM correction")
    assert raw_rows(modules.library.Library(library.parent)) == []
    http_get.assert_called_once()


def test_interrupted_initialization_resumes_defaults_without_replaying_deletions(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating storage is not a successful seed; a committed seed is never replayed."""
    prepare_constructor_storage(library)

    def interrupted_retry(instance: Any) -> tuple[Any, ...]:
        raise RuntimeError("interrupted after creating destination")

    with monkeypatch.context() as interruption:
        interruption.setattr(
            modules.library.Library, "retry_correction_migrations", interrupted_retry
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            modules.library.Library(library.parent)
    http_get = MagicMock(return_value=response("Pattern,Rotation\nremote-only,90\n"))
    monkeypatch.setattr(modules.library.requests, "get", http_get)

    monkeypatch.setattr(modules.library, "Thread", immediate_thread)
    reopened = modules.library.Library(library.parent)
    assert reopened.get_correction_data("remote-only") == ("remote-only", 90, 0, 0)
    http_get.assert_called_once()
    execute(reopened.globalcorrectionsdb_file, "DELETE FROM correction")
    reopened.fetch_remote_corrections(initial=True)
    assert raw_rows(reopened) == []
    assert raw_rows(modules.library.Library(library.parent)) == []


def test_interrupted_initial_seed_recovers_original_library_before_new_selection(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured archives remain discoverable and retain priority after library selection changes."""
    prepare_constructor_storage(library)
    original_config = modules.library.LIBRARY_CONFIGS[modules.library.DEFAULT_LIBRARY]
    original_path = Path(library.datadir) / original_config.name
    seed_legacy(original_path, [("custom", 270)])

    def interrupted_retry(instance: Any) -> tuple[Any, ...]:
        raise RuntimeError("interrupted before archive examination")

    with monkeypatch.context() as interruption:
        interruption.setattr(
            modules.library.Library, "retry_correction_migrations", interrupted_retry
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            modules.library.Library(library.parent)
    alternate_key, alternate_config = next(
        (key, config)
        for key, config in modules.library.LIBRARY_CONFIGS.items()
        if config.name != original_config.name
    )
    library.parent.settings["library"]["selected_library"] = alternate_key
    alternate_path = Path(library.datadir) / alternate_config.name
    seed_legacy(alternate_path, [("custom", 90), ("new-library", 180)])
    http_get = MagicMock(
        return_value=response("Pattern,Rotation\ncustom,0\nremote-only,0\n")
    )
    monkeypatch.setattr(modules.library.requests, "get", http_get)

    monkeypatch.setattr(modules.library, "Thread", immediate_thread)
    reopened = modules.library.Library(library.parent)
    assert correction_values(reopened) == [
        ("custom", 270, (0.0, 0.0)),
        ("new-library", 180, (0.0, 0.0)),
        ("remote-only", 0, (0.0, 0.0)),
    ]
    http_get.assert_called_once()
    assert execute(original_path, "SELECT * FROM rotation") == [("custom", 270)]


def test_concurrent_constructors_share_initial_download_and_commit_once(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening a manager during an in-flight initial seed cannot launch another one."""
    prepare_constructor_storage(library)
    scheduled = []

    def queued_thread(
        *, target: Callable[..., Any], args: tuple[Any, ...], daemon: bool
    ) -> SimpleNamespace:
        return SimpleNamespace(start=lambda: scheduled.append((target, args)))

    monkeypatch.setattr(modules.library, "Thread", queued_thread)
    http_get = MagicMock(return_value=response("Pattern,Rotation\nremote-only,90\n"))
    monkeypatch.setattr(modules.library.requests, "get", http_get)
    first = modules.library.Library(library.parent)
    second = modules.library.Library(library.parent)
    second.retry_correction_migrations()
    assert len(scheduled) == 1
    target, args = scheduled.pop()
    target(*args)
    assert first.get_correction_data("remote-only") == ("remote-only", 90, 0, 0)
    execute(first.globalcorrectionsdb_file, "DELETE FROM correction")
    first.retry_correction_migrations()
    second.retry_correction_migrations()
    assert scheduled == []
    assert raw_rows(second) == []
    http_get.assert_called_once()


def test_failed_unknown_source_bookkeeping_cannot_authorize_initial_defaults(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing durable classification must block seeding even in another instance."""
    prepare_constructor_storage(library)
    Path(library.rotationsdb_file).write_bytes(b"unclassified custom archive")
    retry = modules.library.Library.retry_correction_migrations

    def denied_first_retry(instance: Any) -> tuple[Any, ...]:
        seed_raw(instance, [("existing", 270, 1, 2)])
        execute(
            instance.globalcorrectionsdb_file,
            "CREATE TRIGGER deny_deferred BEFORE INSERT ON correction_migration_state "
            "WHEN NEW.status='deferred' BEGIN SELECT RAISE(ABORT, 'deferred bookkeeping denied'); END",
        )
        return retry(instance)

    http_get = MagicMock(
        return_value=response("Pattern,Rotation\ncustom,180\nremote-only,0\n")
    )
    monkeypatch.setattr(modules.library.requests, "get", http_get)

    monkeypatch.setattr(modules.library, "Thread", immediate_thread)
    with monkeypatch.context() as first_start:
        first_start.setattr(
            modules.library.Library, "retry_correction_migrations", denied_first_retry
        )
        first = modules.library.Library(library.parent)
    assert first.read_correction_data().state is modules.library.CorrectionState.READY
    assert first.read_correction_data().warnings
    http_get.assert_not_called()
    other = fresh_library(first)
    result = other.apply_corrections(
        [("custom", 180, (0, 0)), ("remote-only", 0, (0, 0))],
        migration_key=modules.library._INITIAL_DEFAULTS_KEY,
        overwrite=False,
    )
    assert result.changed == 0
    assert correction_values(other) == [("existing", 270, (1.0, 2.0))]
    assert not other.has_correction_migration(modules.library._INITIAL_DEFAULTS_KEY)
    Path(library.rotationsdb_file).unlink()
    seed_legacy(library.rotationsdb_file, [("custom", 90)])
    assert first.retry_correction_migrations() == ()
    assert correction_values(first) == [
        ("custom", 90, (0.0, 0.0)),
        ("existing", 270, (1.0, 2.0)),
        ("remote-only", 0, (0.0, 0.0)),
    ]
    http_get.assert_called_once()


def test_initial_seed_marker_failure_rolls_back_and_resumes(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion failure rolls back defaults while preserving prior custom migration."""
    prepare_constructor_storage(library)
    seed_legacy(library.rotationsdb_file, [("custom", 270)])
    retry = modules.library.Library.retry_correction_migrations

    def deny_seed_marker(instance: Any) -> tuple[Any, ...]:
        execute(
            instance.globalcorrectionsdb_file,
            "CREATE TRIGGER deny_seed BEFORE INSERT ON correction_migrations "
            "WHEN NEW.migration_key LIKE 'remote:%' BEGIN SELECT RAISE(ABORT, 'seed marker denied'); END",
        )
        return retry(instance)

    http_get = MagicMock(
        return_value=response("Pattern,Rotation\ncustom,90\nremote-only,0\n")
    )
    monkeypatch.setattr(modules.library.requests, "get", http_get)

    monkeypatch.setattr(modules.library, "Thread", immediate_thread)
    with monkeypatch.context() as first_start:
        first_start.setattr(
            modules.library.Library, "retry_correction_migrations", deny_seed_marker
        )
        first = modules.library.Library(library.parent)
    assert correction_values(first) == [("custom", 270, (0.0, 0.0))]
    assert not first.has_correction_migration(modules.library._INITIAL_DEFAULTS_KEY)
    execute(first.globalcorrectionsdb_file, "DROP TRIGGER deny_seed")
    reopened = modules.library.Library(library.parent)
    assert correction_values(reopened) == [
        ("custom", 270, (0.0, 0.0)),
        ("remote-only", 0, (0.0, 0.0)),
    ]
    assert reopened.has_correction_migration(modules.library._INITIAL_DEFAULTS_KEY)
    assert http_get.call_count == 2


def test_switching_back_to_global_resumes_deferred_initial_defaults(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local board can recover its global archive and resume pending defaults."""
    prepare_constructor_storage(library)
    Path(library.rotationsdb_file).write_bytes(b"temporarily unreadable archive")
    scheduled = []

    def queued_thread(
        *, target: Callable[..., Any], args: tuple[Any, ...], daemon: bool
    ) -> SimpleNamespace:
        return SimpleNamespace(start=lambda: scheduled.append((target, args)))

    monkeypatch.setattr(modules.library, "Thread", queued_thread)
    instance = modules.library.Library(library.parent)
    assert scheduled == []
    instance.switch_to_global_correction_database(False)
    instance.apply_corrections([("local", 180, (1, 2))])
    Path(instance.rotationsdb_file).unlink()
    seed_legacy(instance.rotationsdb_file, [("custom", 270)])
    instance.switch_to_global_correction_database(True)
    assert instance.correctionsdb_file == instance.globalcorrectionsdb_file
    assert len(scheduled) == 1
    assert instance.get_correction_data("custom") == ("custom", 270, 0, 0)
    http_get = MagicMock(
        return_value=response("Pattern,Rotation\ncustom,90\nremote-only,0\n")
    )
    monkeypatch.setattr(modules.library.requests, "get", http_get)
    target, args = scheduled.pop()
    target(*args)
    assert correction_values(instance) == [
        ("custom", 270, (0.0, 0.0)),
        ("remote-only", 0, (0.0, 0.0)),
    ]


@pytest.mark.parametrize("entry", ["retry", "startup", "scope"])
def test_optional_download_start_failure_preserves_healthy_corrections(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Every scheduling entry leaves usable data and retryable initial defaults."""
    seed_raw(library, [("existing", 90, 1, 2)])
    library.create_mapping_table()
    library.migrate_corrections()
    execute(
        library.globalcorrectionsdb_file,
        "INSERT INTO correction_migration_state VALUES (?, ?, 'seed-pending', ?)",
        (
            modules.library._INITIAL_DEFAULTS_KEY,
            library.globalcorrectionsdb_file,
            modules.library.json.dumps(
                [library.rotationsdb_file, library.partsdb_file]
            ),
        ),
    )
    thread = MagicMock()
    thread.return_value.start.side_effect = RuntimeError("thread could not start")
    monkeypatch.setattr(modules.library, "Thread", thread)
    if entry == "scope":
        library.switch_to_global_correction_database(False)
        library.switch_to_global_correction_database(True)
        assert library.correctionsdb_file == library.globalcorrectionsdb_file
        assert (
            execute(
                library.localcorrectionsdb_file,
                "SELECT name FROM sqlite_master WHERE name='correction'",
            )
            == []
        )
    elif entry == "startup":
        library.check_library()
    else:
        assert library.retry_correction_migrations() == ()
    snapshot = library.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.READY
    assert correction_values(library) == [("existing", 90, (1.0, 2.0))]
    assert "thread could not start" in str(snapshot.warnings)
    assert not library.has_correction_migration(modules.library._INITIAL_DEFAULTS_KEY)
    thread.return_value.start.side_effect = None
    assert library.retry_correction_migrations() == ()
    assert thread.return_value.start.call_count == 2
    assert library.read_correction_data().warnings == ()
    modules.library._INITIAL_DOWNLOAD_TARGETS.clear()


def test_late_active_storage_failure_remains_blocking(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A race after migration cannot hide an unreadable active correction set."""
    seed_raw(library, [("existing", 90, 1, 2)])
    migrate = library.migrate_corrections

    def damage_after_migration() -> tuple[Any, ...]:
        result = migrate()
        Path(library.globalcorrectionsdb_file).write_bytes(b"unreadable active storage")
        return result

    monkeypatch.setattr(library, "migrate_corrections", damage_after_migration)
    library.retry_correction_migrations()
    snapshot = library.read_correction_data()
    assert snapshot.state is modules.library.CorrectionState.UNAVAILABLE
    assert snapshot.corrections is None
    assert any(
        issue.source == library.globalcorrectionsdb_file for issue in snapshot.issues
    )


@pytest.mark.parametrize("bad_rotation", ["47u", 90])
def test_discard_invalid_local_corrections_for_healthy_global_and_reopen(
    modules: Any, tmp_path: Path, bad_rotation: Any
) -> None:
    """Global selection drops the local table even when its rows need repair."""
    rows = [("same", bad_rotation, 0, 0), ("same", 180, 0, 0)]
    instance = make_library(modules.library, tmp_path, rows, local=True)
    instance.create_correction_table(instance.globalcorrectionsdb_file)
    instance.apply_corrections(
        [("global", 270, (1.25, -2.5))], db_path=instance.globalcorrectionsdb_file
    )
    execute(instance.localcorrectionsdb_file, "CREATE TABLE unrelated (value)")
    execute(instance.localcorrectionsdb_file, "INSERT INTO unrelated VALUES ('keep')")
    instance.parent.settings = {"library": {"data_path": instance.datadir}}

    instance.switch_to_global_correction_database(True)

    reopened = modules.library.Library(instance.parent)
    assert reopened.correctionsdb_file == instance.globalcorrectionsdb_file
    assert correction_values(reopened) == [("global", 270, (1.25, -2.5))]
    assert execute(instance.localcorrectionsdb_file, "SELECT * FROM unrelated") == [
        ("keep",)
    ]
    assert (
        execute(
            instance.localcorrectionsdb_file,
            "SELECT name FROM sqlite_master WHERE name='correction'",
        )
        == []
    )


@pytest.mark.parametrize("existing", [False, True])
def test_issue_531_invalid_rotation_never_persists(
    modules: SimpleNamespace, library: Any, existing: bool
) -> None:
    """Reject the reported 47u value without destroying an existing rotation."""
    if existing:
        seed_raw(library, [("Capacitor.*", 90, 0.25, -0.5)])
    before = raw_rows(library)

    with pytest.raises(modules.data.CorrectionDataError, match="47u") as error:
        library.apply_corrections([("Capacitor.*", "47u", (0, 0))])

    assert "rotation" in str(error.value)
    assert "Capacitor" in str(error.value)
    assert raw_rows(library) == before
    assert correction_values(fresh_library(library)) == (
        [("Capacitor.*", 90, (0.25, -0.5))] if existing else []
    )
    library.apply_corrections([("Capacitor.*", "-90", (".125", "-.5"))])
    assert correction_values(fresh_library(library)) == [
        ("Capacitor.*", -90, (0.125, -0.5))
    ]


@pytest.mark.parametrize("bad_position", [0, 1, 3])
@pytest.mark.parametrize("overwrite", [False, True])
def test_validate_complete_batch_before_any_write(
    modules: SimpleNamespace, library: Any, bad_position: int, overwrite: bool
) -> None:
    """Every bad-row position preserves all prior inserts and replacements."""
    seed_raw(library, [("existing", 90, 0.25, -0.5), ("untouched", 0, 0, 0)])
    before = raw_rows(library)
    batch = [
        ("insert-before", 180, (0, 0)),
        ("existing", -90, (1, 2)),
        ("insert-after", 270, (3, 4)),
    ]
    batch.insert(bad_position, ("bad", "47u", (0, 0)))
    with pytest.raises(modules.data.CorrectionDataError):
        library.apply_corrections(batch, overwrite=overwrite)
    assert raw_rows(library) == before
    assert correction_values(fresh_library(library)) == [
        ("existing", 90, (0.25, -0.5)),
        ("untouched", 0, (0.0, 0.0)),
    ]


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("bad_first", [False, True])
def test_invalid_duplicate_cannot_hide_behind_resolution(
    modules, library, overwrite, bad_first
):
    """Validate duplicates even when last-wins or insert-only would skip them."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    batch = [("existing", 180, (0, 0)), ("existing", "47u", (0, 0))]
    if bad_first:
        batch.reverse()
    with pytest.raises(modules.data.CorrectionDataError):
        library.apply_corrections(batch, overwrite=overwrite)
    assert raw_rows(library) == before


def test_database_failure_rolls_back_prior_insert_and_update(
    modules: SimpleNamespace, library: Any
) -> None:
    """A real SQLite abort restores all earlier mutations in the batch."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    install_abort_trigger(library.correctionsdb_file)
    with pytest.raises(
        modules.data.CorrectionDataError, match="injected write failure"
    ):
        library.apply_corrections(
            [
                ("first", 0, (0, 0)),
                ("existing", 180, (1, 2)),
                ("explode", 270, (3, 4)),
            ]
        )
    assert raw_rows(library) == before
    assert correction_values(fresh_library(library)) == [("existing", 90, (0.0, 0.0))]


@pytest.mark.parametrize("overwrite", [False, True])
def test_successful_batch_has_explicit_duplicate_policy(
    modules: SimpleNamespace, library: Any, overwrite: bool
) -> None:
    """User import keeps last input; remote insertion preserves first input."""
    seed_raw(library, [("existing", 90, 0, 0)])
    result = library.apply_corrections(
        [
            modules.data.Correction("new", 180, (0.25, -0.5)),
            ("existing", "270.0", ("1.5", "-2")),
            ("new", "-90", ("3", "4")),
        ],
        overwrite=overwrite,
    )
    assert result.changed
    assert result.inserted == 1
    assert result.updated == int(overwrite)
    assert correction_values(fresh_library(library)) == (
        [("existing", 270, (1.5, -2.0)), ("new", -90, (3.0, 4.0))]
        if overwrite
        else [("existing", 90, (0.0, 0.0)), ("new", 180, (0.25, -0.5))]
    )


def test_empty_batch_is_noop(library):
    """A valid empty import leaves records and commit result unchanged."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    result = library.apply_corrections([])
    assert not result.changed
    assert (result.inserted, result.updated, result.skipped) == (0, 0, 0)
    assert raw_rows(library) == before


@pytest.mark.parametrize(
    "method",
    ["insert_correction_data", "update_correction_data", "save_correction_data"],
)
def test_single_record_writers_validate_at_storage_boundary(modules, library, method):
    """Direct callers cannot bypass validation by avoiding the CSV importer."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    with pytest.raises(modules.data.CorrectionDataError, match="47u"):
        getattr(library, method)("existing", "47u", (0, 0))
    assert raw_rows(library) == before


def test_typed_corrections_persist_their_immutable_normalized_values(
    modules: Any, library: Any
) -> None:
    """Caller changes to mutable inputs cannot alter a validated pending write."""
    offsets = [".25", "-.5"]
    record = modules.data.Correction("existing", "90.0", offsets)
    offsets[0] = "47u"
    library.apply_corrections([record])
    saved = modules.data.Correction("saved", "180.0", ["1.25", "-2.5"])
    library.save_correction_data(saved)
    assert correction_values(fresh_library(library)) == [
        ("existing", 90, (0.25, -0.5)),
        ("saved", 180, (1.25, -2.5)),
    ]


def test_batch_target_is_captured_before_consuming_input_iterator(library: Any) -> None:
    """Validation cannot redirect writes when another activity changes active scope."""
    target = library.globalcorrectionsdb_file
    execute(
        library.localcorrectionsdb_file,
        "CREATE TABLE correction (regex, rotation, offset_x, offset_y)",
    )

    def changing_scope():
        yield ("first", 90, (0, 0))
        library.correctionsdb_file = library.localcorrectionsdb_file
        yield ("second", 180, (1, 2))

    library.apply_corrections(changing_scope())
    assert raw_rows(library) == []
    assert correction_values(library, db_path=target) == [
        ("first", 90, (0.0, 0.0)),
        ("second", 180, (1.0, 2.0)),
    ]


def test_parameterized_crud_preserves_quotes_and_unrelated_records(
    library: Any,
) -> None:
    """Apostrophes in valid patterns remain data throughout CRUD operations."""
    pattern = r"O'Brien\:C.*' OR 1=1 --"
    seed_raw(library, [("untouched", 0, 0, 0)])
    library.insert_correction_data(pattern, 90, (0.25, -0.5))
    assert library.get_correction_data(pattern) == (pattern, 90, 0.25, -0.5)
    library.update_correction_data(pattern, -180, (1.5, 2.5))
    assert library.get_correction_data(pattern) == (pattern, -180, 1.5, 2.5)
    library.delete_correction_data(pattern)
    assert library.get_correction_data(pattern) is None
    assert correction_values(fresh_library(library)) == [("untouched", 0, (0.0, 0.0))]


def test_read_snapshot_preserves_original_invalid_values(
    modules: SimpleNamespace, library: Any
) -> None:
    """Recoverable reads expose raw data while strict readers reject it."""
    seed_raw(
        library,
        [("good", "90.0", ".25", "-.5"), ("broken", "47u", "0", "0")],
    )
    before = raw_rows(library)
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.db_path == library.correctionsdb_file
    assert snapshot.scope == "global"
    assert len(snapshot.rows) == 2
    bad = next(row for row in snapshot.rows if row.pattern == "broken")
    assert bad.rotation == "47u"
    assert bad.offset == ("0", "0")
    assert bad.correction is None
    assert bad.issues[0].rowid == bad.rowid
    assert bad.issues[0].source == library.correctionsdb_file
    assert snapshot.corrections is None
    assert snapshot.state is modules.library.CorrectionState.NEEDS_REPAIR
    assert fresh_library(library).get_all_correction_data() is None
    good = next(row for row in snapshot.rows if row.pattern == "good")
    assert good.correction == modules.data.Correction("good", 90, (0.25, -0.5))
    assert raw_rows(library) == before


@pytest.mark.parametrize(
    "row, field",
    [
        ((None, 90, 0, 0), "pattern"),
        (("[", 90, 0, 0), "pattern"),
        (("bad", None, 0, 0), "rotation"),
        (("bad", b"90", 0, 0), "rotation"),
        (("bad", 90.5, 0, 0), "rotation"),
        (("bad", 90, "NaN", 0), "offset_x"),
        (("bad", 90, 0, float("inf")), "offset_y"),
        (("bad", 90, None, 0), "offset_x"),
    ],
)
def test_all_invalid_database_remains_inspectable(
    modules: SimpleNamespace, library: Any, row: tuple[object, ...], field: str
) -> None:
    """Unexpected historical SQLite values produce repairable field errors."""
    seed_raw(library, [row])
    before = raw_rows(library)
    snapshot = fresh_library(library).read_correction_data()
    assert len(snapshot.rows) == 1
    assert snapshot.corrections is None
    assert field in {issue.field for issue in snapshot.issues}
    assert snapshot.corrections is None
    assert raw_rows(library) == before


def test_snapshot_and_values_are_immutable(
    modules: SimpleNamespace, library: Any
) -> None:
    """A validated operation snapshot cannot change through ordinary mutation."""
    library.apply_corrections([("good", 90, (0.25, -0.5))])
    snapshot = library.read_correction_data()
    assert isinstance(snapshot.rows, tuple)
    assert isinstance(snapshot.corrections, tuple)
    assert isinstance(snapshot.issues, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot.db_path = "another.db"
    with pytest.raises(FrozenInstanceError):
        snapshot.corrections[0].rotation = 180
    library.update_correction_data("good", 180, (1, 2))
    assert snapshot.corrections == (modules.data.Correction("good", 90, (0.25, -0.5)),)
    assert correction_values(library) == [("good", 180, (1.0, 2.0))]


def test_snapshot_reads_rows_and_migration_markers_in_same_transaction(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent legacy import cannot make an incomplete snapshot look valid."""
    seed_raw(library, [("existing", 90, 0, 0)])
    seed_legacy(library.rotationsdb_file, [("legacy", 180)])
    install_abort_trigger(library.globalcorrectionsdb_file, pattern="legacy")
    assert library.migrate_corrections()
    execute(library.globalcorrectionsdb_file, "DROP TRIGGER fail_write")
    assert execute(library.globalcorrectionsdb_file, "PRAGMA journal_mode=WAL") == [
        ("wal",)
    ]
    read_metadata = library._correction_metadata
    migrated = False

    def migrate_between_reads(connection: sqlite3.Connection) -> tuple[Any, ...]:
        nonlocal migrated
        if not migrated:
            migrated = True
            fresh_library(library).migrate_corrections_from_rotation()
        return read_metadata(connection)

    monkeypatch.setattr(library, "_correction_metadata", migrate_between_reads)
    snapshot = library.read_correction_data()
    assert migrated
    assert [row.pattern for row in snapshot.rows] == ["existing"]
    assert any(issue.field == "migration" for issue in snapshot.issues)
    assert snapshot.corrections is None
    assert correction_values(fresh_library(library)) == [
        ("existing", 90, (0.0, 0.0)),
        ("legacy", 180, (0.0, 0.0)),
    ]


def test_equal_duplicate_rows_collapse_without_losing_raw_records(library: Any) -> None:
    """Historical numerically equivalent duplicates have one unambiguous value."""
    seed_raw(library, [("same", "90", "0", "0"), ("same", 90.0, 0.0, 0.0)])
    snapshot = library.read_correction_data()
    assert len(snapshot.rows) == 2
    assert snapshot.issues == ()
    assert correction_values(library) == [("same", 90, (0.0, 0.0))]


def test_conflicting_duplicates_are_visible_and_block_strict_reads(
    modules: SimpleNamespace, library: Any
) -> None:
    """Conflicting historical rows require an explicit repair decision."""
    seed_raw(library, [("same", 90, 0, 0), ("same", 180, 0, 0)])
    snapshot = library.read_correction_data()
    assert len(snapshot.rows) == 2
    assert snapshot.issues
    assert snapshot.corrections is None
    assert {issue.rowid for issue in snapshot.issues} == {
        row.rowid for row in snapshot.rows
    }


@pytest.mark.parametrize(
    "storage_kind", ["missing", "no-table", "malformed", "directory"]
)
def test_storage_errors_are_not_presented_as_empty_healthy_data(
    modules: SimpleNamespace, library: Any, tmp_path: Path, storage_kind: str
) -> None:
    """Read failures are recoverable diagnostics and cannot create source files."""
    target = tmp_path / f"{storage_kind}.db"
    if storage_kind == "no-table":
        execute(target, "CREATE TABLE unrelated (value)")
    elif storage_kind == "malformed":
        target.write_bytes(b"not a sqlite database")
    elif storage_kind == "directory":
        target.mkdir()
    snapshot = library.read_correction_data(db_path=str(target))
    assert snapshot.issues
    assert snapshot.corrections is None
    if storage_kind == "missing":
        assert not target.exists()


def test_row_repair_preserves_bad_row_until_validation_succeeds(
    modules: SimpleNamespace, library: Any
) -> None:
    """The original reported value remains repairable after a rejected save."""
    seed_raw(library, [("bad", "47u", "0", "0"), ("other", 90, 1, 2)])
    rowid = next(row[0] for row in raw_rows(library) if row[1] == "bad")
    before = raw_rows(library)
    with pytest.raises(modules.data.CorrectionDataError):
        library.save_correction_data("bad", "still bad", (0, 0), rowid=rowid)
    assert raw_rows(library) == before
    library.save_correction_data("repaired", 180, (0.25, -0.5), rowid=rowid)
    assert correction_values(fresh_library(library)) == [
        ("other", 90, (1.0, 2.0)),
        ("repaired", 180, (0.25, -0.5)),
    ]
    assert next(row[0] for row in raw_rows(library) if row[1] == "repaired") == rowid


def test_repair_and_delete_target_only_the_selected_duplicate(library: Any) -> None:
    """Row identity distinguishes duplicate or NULL patterns during recovery."""
    seed_raw(
        library,
        [
            (None, "47u", 0, 0),
            (None, "bad", 0, 0),
            ("same", 90, 0, 0),
            ("same", 180, 0, 0),
        ],
    )
    rows = raw_rows(library)
    library.save_correction_data("fixed", 270, (0, 0), rowid=rows[0][0])
    library.delete_correction_row(rows[1][0])
    library.delete_correction_row(rows[2][0])
    assert raw_rows(library) == [
        (rows[0][0], "fixed", 270, 0.0, 0.0),
        rows[3],
    ]
    assert correction_values(fresh_library(library)) == [
        ("fixed", 270, (0.0, 0.0)),
        ("same", 180, (0.0, 0.0)),
    ]


def test_row_rename_requires_explicit_collision_replacement(modules, library):
    """A selected-row rename cannot silently overwrite another correction."""
    seed_raw(library, [("old", 90, 0, 0), ("target", 180, 1, 2)])
    before = raw_rows(library)
    with pytest.raises(modules.data.CorrectionDataError):
        library.save_correction_data("target", 270, (3, 4), rowid=before[0][0])
    assert raw_rows(library) == before
    library.save_correction_data(
        "target", 270, (3, 4), rowid=before[0][0], replace=True
    )
    assert raw_rows(library) == [(before[0][0], "target", 270, 3.0, 4.0)]


def test_failed_replacement_restores_deleted_collision_and_selected_row(
    modules, library
):
    """A trigger abort after collision deletion rolls back the complete rename."""
    seed_raw(library, [("old", 90, 0, 0), ("target", 180, 1, 2)])
    before = raw_rows(library)
    install_abort_trigger(
        library.correctionsdb_file, operation="UPDATE", pattern="target"
    )
    with pytest.raises(
        modules.data.CorrectionDataError, match="injected write failure"
    ):
        library.save_correction_data(
            "target", 270, (3, 4), rowid=before[0][0], replace=True
        )
    assert raw_rows(library) == before


@pytest.mark.parametrize("action", ["save", "delete"])
@pytest.mark.parametrize("change", ["missing", "reused", "edited", "type"])
def test_stale_selected_record_cannot_change_storage(
    modules: SimpleNamespace, library: Any, action: str, change: str
) -> None:
    """Raw selected values, including their types, are checked under the write lock."""
    seed_raw(library, [(None, "47u", 0, 0)])
    selected = library.read_correction_data().rows[0]
    if change in {"missing", "reused"}:
        execute(library.correctionsdb_file, "DELETE FROM correction")
        if change == "reused":
            seed_raw(library, [("unrelated", 90, 1, 2)])
            assert raw_rows(library)[0][0] == selected.rowid
    else:
        execute(
            library.correctionsdb_file,
            "UPDATE correction SET offset_x=?",
            (1 if change == "edited" else 0.0,),
        )
    before = raw_rows(library)
    with pytest.raises(
        modules.data.CorrectionDataError, match="changed|no longer exists"
    ):
        if action == "save":
            library.save_correction_data(
                "repaired", 180, (0, 0), rowid=selected.rowid, expected_record=selected
            )
        else:
            library.delete_correction_row(selected.rowid, expected_record=selected)
    assert raw_rows(library) == before


@pytest.mark.parametrize("change", ["edited", "removed", "added", "reused"])
def test_replacement_checks_the_exact_confirmed_records(
    modules: SimpleNamespace, library: Any, change: str
) -> None:
    """Replacement confirmation never grants permission to modify changed collisions."""
    seed_raw(library, [("selected", 90, 0, 0), ("target", "bad", 1, 2)])
    selected, conflict = library.read_correction_data().rows
    if change in {"removed", "reused"}:
        execute(
            library.correctionsdb_file,
            "DELETE FROM correction WHERE rowid=?",
            (conflict.rowid,),
        )
    if change in {"added", "reused"}:
        seed_raw(library, [("target", 270, 0, 0)])
    elif change == "edited":
        execute(
            library.correctionsdb_file,
            "UPDATE correction SET rotation=180 WHERE rowid=?",
            (conflict.rowid,),
        )
    before = raw_rows(library)
    with pytest.raises(modules.data.CorrectionDataError, match="changed"):
        library.save_correction_data(
            "target",
            180,
            (3, 4),
            rowid=selected.rowid,
            replace=True,
            expected_record=selected,
            expected_conflicts=(conflict,),
        )
    assert raw_rows(library) == before


def test_migration_key_is_stable_and_distinguishes_source_and_content(
    library, tmp_path
):
    """Legacy CSV completion identifies both the source path and its contents."""
    first = tmp_path / "legacy.csv"
    second = tmp_path / "other.csv"
    contents = "Pattern,Rotation\npart,90\n"
    key = library.correction_csv_migration_key(first, contents)
    assert key == library.correction_csv_migration_key(first, contents.encode())
    assert key != library.correction_csv_migration_key(second, contents)
    assert key != library.correction_csv_migration_key(first, contents + "next,180\n")


def test_completion_marker_commits_with_data_and_prevents_replay(library: Any) -> None:
    """A completed automatic import cannot overwrite later user repairs."""
    key = "legacy-csv:fixture"
    assert not library.has_correction_migration(key)
    library.apply_corrections([("existing", 90, (0, 0))], migration_key=key)
    assert fresh_library(library).has_correction_migration(key)
    library.update_correction_data("existing", 270, (0.25, -0.5))
    result = fresh_library(library).apply_corrections(
        [("existing", 90, (0, 0))], migration_key=key
    )
    assert not result.changed
    assert correction_values(fresh_library(library)) == [
        ("existing", 270, (0.25, -0.5))
    ]


def test_cloning_global_corrections_preserves_csv_completion_and_user_repairs(
    library: Any, tmp_path: Path
) -> None:
    """A legacy CSV left after archive failure cannot replay after changing scope."""
    key = library.correction_csv_migration_key(
        tmp_path / "legacy.csv", "Pattern,Rotation\nexisting,90\n"
    )
    original = [("existing", 90, (0, 0))]
    library.apply_corrections(original, migration_key=key)
    library.update_correction_data("existing", 270, (0.25, -0.5))
    library.switch_to_global_correction_database(False)
    assert library.has_correction_migration(key, library.localcorrectionsdb_file)
    result = library.apply_corrections(original, migration_key=key)
    assert not result.changed
    assert correction_values(fresh_library(library)) == [
        ("existing", 270, (0.25, -0.5))
    ]


def test_marker_write_failure_rolls_back_all_imported_data(modules, library):
    """Failure in the final bookkeeping write also rolls back successful records."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    execute(
        library.correctionsdb_file,
        "CREATE TRIGGER fail_marker BEFORE INSERT ON correction_migrations "
        "BEGIN SELECT RAISE(ABORT, 'marker write failed'); END",
    )
    with pytest.raises(modules.data.CorrectionDataError, match="marker write failed"):
        library.apply_corrections(
            [("existing", 180, (1, 2)), ("new", 270, (3, 4))],
            migration_key="csv:marker-failure",
        )
    assert raw_rows(library) == before
    assert not fresh_library(library).has_correction_migration("csv:marker-failure")


def test_failed_batch_cannot_commit_completion_marker(modules, library):
    """Migration bookkeeping rolls back with earlier successful writes."""
    key = "legacy-csv:failed"
    install_abort_trigger(library.correctionsdb_file)
    with pytest.raises(
        modules.data.CorrectionDataError, match="injected write failure"
    ):
        library.apply_corrections(
            [("first", 90, (0, 0)), ("explode", 180, (0, 0))], migration_key=key
        )
    assert raw_rows(library) == []
    assert not fresh_library(library).has_correction_migration(key)


@pytest.mark.parametrize("source", ["rotation", "parts"])
def test_legacy_migration_preserves_invalid_data_and_source(
    modules: SimpleNamespace, library: Any, source: str
) -> None:
    """Historical invalid values migrate into recovery without destroying archives."""
    path = library.rotationsdb_file if source == "rotation" else library.partsdb_file
    original = [("good", 90), ("bad", "47u"), (None, 180)]
    seed_legacy(path, original)
    getattr(library, f"migrate_corrections_from_{source}")()
    assert execute(path, "SELECT * FROM rotation ORDER BY rowid") == original
    assert [row[1:] for row in raw_rows(library)] == [
        ("good", 90, 0, 0),
        ("bad", "47u", 0, 0),
        (None, 180, 0, 0),
    ]
    assert len(migration_rows(library)) == 1
    snapshot = fresh_library(library).read_correction_data()
    assert len(snapshot.rows) == 3
    assert snapshot.corrections is None
    before = raw_rows(library)
    getattr(fresh_library(library), f"migrate_corrections_from_{source}")()
    assert raw_rows(library) == before


@pytest.mark.parametrize("source", ["rotation", "parts"])
def test_legacy_write_failure_rolls_back_and_retries_existing_destination(
    modules: SimpleNamespace, library: Any, source: str
) -> None:
    """Failed migration remains visible and check_library retries on a later start."""
    path = library.rotationsdb_file if source == "rotation" else library.partsdb_file
    original = [("first", 90), ("explode", 180)]
    seed_legacy(path, original)
    seed_raw(library, [("existing", 270, 0.25, -0.5)])
    before = raw_rows(library)
    install_abort_trigger(library.globalcorrectionsdb_file)
    with pytest.raises(
        modules.data.CorrectionDataError, match="injected write failure"
    ):
        getattr(library, f"migrate_corrections_from_{source}")()
    assert raw_rows(library) == before
    assert migration_rows(library) == []
    assert execute(path, "SELECT * FROM rotation ORDER BY rowid") == original
    assert fresh_library(library).read_correction_data().issues
    execute(library.globalcorrectionsdb_file, "DROP TRIGGER fail_write")
    fresh_library(library).check_library()
    assert correction_values(fresh_library(library)) == [
        ("existing", 270, (0.25, -0.5)),
        ("explode", 180, (0.0, 0.0)),
        ("first", 90, (0.0, 0.0)),
    ]
    assert len(migration_rows(library)) == 2


def test_two_legacy_sources_migrate_once_without_overwriting_repairs(
    library: Any,
) -> None:
    """Both historical sources are archived and completed independently."""
    seed_legacy(library.rotationsdb_file, [("rotation-source", 90)])
    seed_legacy(library.partsdb_file, [("parts-source", 180)])
    library.migrate_corrections()
    assert len(migration_rows(library)) == 2
    row = next(row for row in raw_rows(library) if row[1] == "rotation-source")
    library.save_correction_data("repaired", 270, (0.25, 0.5), rowid=row[0])
    before = raw_rows(library)
    fresh_library(library).check_library()
    fresh_library(library).check_library()
    assert raw_rows(library) == before
    assert correction_values(fresh_library(library)) == [
        ("parts-source", 180, (0.0, 0.0)),
        ("repaired", 270, (0.25, 0.5)),
    ]


def test_migration_preserves_established_source_and_destination_records(
    library: Any,
) -> None:
    """Existing corrected destination data wins while the legacy archive stays intact."""
    seed_raw(library, [("same", 90, 1, 2)])
    seed_legacy(library.rotationsdb_file, [("same", 180)])
    library.migrate_corrections()
    assert [row[1:] for row in raw_rows(library)] == [
        ("same", 90, 1, 2),
    ]
    assert correction_values(fresh_library(library)) == [("same", 90, (1.0, 2.0))]
    assert execute(library.rotationsdb_file, "SELECT * FROM rotation") == [
        ("same", 180)
    ]


@pytest.mark.parametrize("source", ["rotation", "parts"])
def test_missing_legacy_source_is_not_created_by_migration(library, source):
    """Checking for historical data must not create empty legacy databases."""
    path = Path(
        library.rotationsdb_file if source == "rotation" else library.partsdb_file
    )
    assert not path.exists()
    getattr(library, f"migrate_corrections_from_{source}")()
    assert not path.exists()
    assert not library.read_correction_data().issues


@pytest.mark.parametrize("source", ["rotation", "parts"])
def test_empty_legacy_table_is_completed_and_retained(
    library: Any, source: str
) -> None:
    """Empty archives share completion markers and cannot become pending again."""
    path = library.rotationsdb_file if source == "rotation" else library.partsdb_file
    seed_legacy(path, [])
    library.migrate_corrections()
    assert execute(path, "SELECT * FROM rotation") == []
    assert raw_rows(library) == []
    assert library.has_correction_migration(library._legacy_migration_key(path))
    assert (
        execute(
            library.globalcorrectionsdb_file, "SELECT * FROM correction_migration_state"
        )
        == []
    )
    reopened = fresh_library(library)
    reopened.migrate_corrections()
    assert reopened.read_correction_data().issues == ()
    assert migration_rows(reopened) == migration_rows(library)


def test_modern_parts_database_without_rotation_is_not_a_failed_migration(
    library: Any,
) -> None:
    """Ordinary parts storage lacking a historical rotation table is valid."""
    execute(library.partsdb_file, "CREATE TABLE parts (part_number)")
    execute(library.partsdb_file, "INSERT INTO parts VALUES ('C123')")
    library.migrate_corrections()
    assert fresh_library(library).read_correction_data().issues == ()
    assert execute(library.partsdb_file, "SELECT * FROM parts") == [("C123",)]
    assert len(migration_rows(library)) == 2


def test_dedicated_rotation_database_without_rotation_is_examined(
    library: Any,
) -> None:
    """A successfully inspected no-table archive cannot block valid corrections."""
    execute(library.rotationsdb_file, "CREATE TABLE unrelated (value)")
    library.migrate_corrections_from_rotation()
    assert fresh_library(library).read_correction_data().issues == ()
    assert library.has_correction_migration(
        library._legacy_migration_key(library.rotationsdb_file)
    )
    assert execute(
        library.rotationsdb_file,
        "SELECT name FROM sqlite_master WHERE name='unrelated'",
    ) == [("unrelated",)]


@pytest.mark.parametrize("source", ["rotation", "parts"])
def test_malformed_legacy_source_is_preserved_and_reported(
    library: Any, source: str
) -> None:
    """Unrecoverable legacy storage cannot become a false completed migration."""
    path = Path(
        library.rotationsdb_file if source == "rotation" else library.partsdb_file
    )
    original = b"not a SQLite database; preserve this source"
    path.write_bytes(original)
    getattr(library, f"migrate_corrections_from_{source}")()
    assert path.read_bytes() == original
    assert raw_rows(library) == []
    assert migration_rows(library) == []
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.issues == ()
    assert any(issue.source == str(path) for issue in snapshot.warnings)


def test_legacy_migration_targets_global_while_local_scope_is_active(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Global archives never pollute the active board's correction table."""
    library = make_library(modules.library, tmp_path, [("board", 90, 1, 2)], local=True)
    local_before = raw_rows(library)
    seed_legacy(library.rotationsdb_file, [("legacy-global", 180)])
    library.migrate_corrections()
    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert raw_rows(library) == local_before
    assert correction_values(library, db_path=library.globalcorrectionsdb_file) == [
        ("legacy-global", 180, (0.0, 0.0))
    ]
    fresh_library(library).migrate_corrections()
    assert raw_rows(library) == local_before


def response(text):
    """Construct an HTTP response substitute without bypassing actual parsing."""
    return SimpleNamespace(text=text, raise_for_status=MagicMock())


def test_remote_import_preserves_existing_and_accepts_short_rows(
    modules: SimpleNamespace, library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production remote format keeps stored edits and first new duplicates."""
    seed_raw(library, [("existing", 90, 0.25, -0.5)])
    monkeypatch.setattr(
        modules.library.requests,
        "get",
        MagicMock(
            return_value=response(
                '"Footprint pattern", "Correction","Offset X","Offset Y"\n'
                '"existing",180\n"new",270\n"new",90\n"offset",-90,.125,-.5\n'
            )
        ),
    )
    library.fetch_remote_corrections()
    assert correction_values(fresh_library(library)) == [
        ("existing", 90, (0.25, -0.5)),
        ("new", 270, (0.0, 0.0)),
        ("offset", -90, (0.125, -0.5)),
    ]


@pytest.mark.parametrize("bad_pattern", ["new-bad", "existing"])
def test_remote_invalid_batch_including_skipped_rows_preserves_database(
    modules, library, monkeypatch, caplog, bad_pattern
):
    """Remote rows are all validated, even records insert-only policy would skip."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    monkeypatch.setattr(
        modules.library.requests,
        "get",
        MagicMock(
            return_value=response(
                f"Pattern,Rotation\nfirst,180\n{bad_pattern},47u\nlast,270\n"
            )
        ),
    )
    library.fetch_remote_corrections()
    assert raw_rows(library) == before
    assert "47u" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)
    modules.wx.PostEvent.assert_called()


def test_remote_network_failure_keeps_storage_and_reports_error(
    modules, library, monkeypatch, caplog
):
    """A remote request failure leaves local corrections usable."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    monkeypatch.setattr(
        modules.library.requests,
        "get",
        MagicMock(
            side_effect=modules.library.requests.RequestException("network failed")
        ),
    )
    library.fetch_remote_corrections()
    assert raw_rows(library) == before
    assert "network failed" in caplog.text
    modules.wx.PostEvent.assert_called()


def test_remote_write_failure_rolls_back_earlier_records_and_reports(
    modules, library, monkeypatch, caplog
):
    """The downloaded-record workflow uses the same atomic transaction boundary."""
    seed_raw(library, [("existing", 90, 0, 0)])
    before = raw_rows(library)
    install_abort_trigger(library.correctionsdb_file)
    monkeypatch.setattr(
        modules.library.requests,
        "get",
        MagicMock(return_value=response("Pattern,Rotation\nfirst,90\nexplode,180\n")),
    )
    library.fetch_remote_corrections()
    assert raw_rows(library) == before
    assert "injected write failure" in caplog.text
    modules.wx.PostEvent.assert_called()


@pytest.mark.parametrize("explicit_target", [False, True])
def test_remote_target_is_captured_before_scope_changes(
    modules: SimpleNamespace,
    library: Any,
    monkeypatch: pytest.MonkeyPatch,
    explicit_target: bool,
) -> None:
    """A download finishing after scope changes writes only its original target."""
    global_path = library.globalcorrectionsdb_file
    execute(
        library.localcorrectionsdb_file,
        "CREATE TABLE correction (regex, rotation, offset_x, offset_y)",
    )

    def download(*_args, **_kwargs):
        library.correctionsdb_file = library.localcorrectionsdb_file
        return response("Pattern,Rotation\nremote,90\n")

    monkeypatch.setattr(modules.library.requests, "get", download)
    library.fetch_remote_corrections(
        **({"db_path": global_path} if explicit_target else {})
    )
    assert raw_rows(library) == []
    assert correction_values(library, db_path=global_path) == [
        ("remote", 90, (0.0, 0.0))
    ]
    assert library.correctionsdb_file == library.localcorrectionsdb_file


def test_scope_switch_preserves_unrelated_project_tables_and_correct_data(
    library: Any,
) -> None:
    """Global-local-global switching changes corrections without dropping project data."""
    seed_raw(library, [("global", 90, 0.25, -0.5)])
    execute(library.localcorrectionsdb_file, "CREATE TABLE unrelated (value)")
    execute(library.localcorrectionsdb_file, "INSERT INTO unrelated VALUES ('keep')")
    global_before = raw_rows(library)
    library.switch_to_global_correction_database(False)
    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert correction_values(fresh_library(library)) == [("global", 90, (0.25, -0.5))]
    library.update_correction_data("global", 180, (1, 2))
    library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert raw_rows(library) == global_before
    assert execute(library.localcorrectionsdb_file, "SELECT * FROM unrelated") == [
        ("keep",)
    ]
    assert (
        execute(
            library.localcorrectionsdb_file,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='correction'",
        )
        == []
    )


@pytest.mark.parametrize(
    "invalid_scope,use_global", [("global", False), ("local", False), ("global", True)]
)
def test_scope_switch_rejects_invalid_active_or_destination_without_mutation(
    modules: SimpleNamespace, library: Any, invalid_scope: str, use_global: bool
) -> None:
    """Copying requires a valid source and both directions require a valid destination."""
    seed_raw(library, [("global", 90, 0, 0)])
    execute(
        library.localcorrectionsdb_file,
        "CREATE TABLE correction (regex, rotation, offset_x, offset_y)",
    )
    execute(
        library.localcorrectionsdb_file,
        "INSERT INTO correction VALUES ('local',180,0,0)",
    )
    paths = {
        "global": library.globalcorrectionsdb_file,
        "local": library.localcorrectionsdb_file,
    }
    execute(paths[invalid_scope], "UPDATE correction SET rotation='47u'")
    library.correctionsdb_file = paths["local" if use_global else "global"]
    active_before = library.correctionsdb_file
    before = {
        scope: execute(path, "SELECT rowid, * FROM correction ORDER BY rowid")
        for scope, path in paths.items()
    }
    with pytest.raises(modules.data.CorrectionDataError):
        library.switch_to_global_correction_database(use_global)
    assert library.correctionsdb_file == active_before
    for scope, path in paths.items():
        assert (
            execute(path, "SELECT rowid, * FROM correction ORDER BY rowid")
            == before[scope]
        )


def test_failed_scope_copy_restores_destination_and_active_selection(modules, library):
    """A later copy write failure rolls back destination changes and selection."""
    seed_raw(library, [("first", 90, 0, 0), ("explode", 180, 0, 0)])
    execute(
        library.localcorrectionsdb_file,
        "CREATE TABLE correction (regex, rotation, offset_x, offset_y)",
    )
    execute(
        library.localcorrectionsdb_file,
        "INSERT INTO correction VALUES ('local',270,1,2)",
    )
    before = execute(library.localcorrectionsdb_file, "SELECT rowid, * FROM correction")
    global_before = raw_rows(library)
    install_abort_trigger(library.localcorrectionsdb_file)
    with pytest.raises(
        modules.data.CorrectionDataError, match="injected write failure"
    ):
        library.switch_to_global_correction_database(False)
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert raw_rows(library) == global_before
    assert (
        execute(library.localcorrectionsdb_file, "SELECT rowid, * FROM correction")
        == before
    )


def test_invalid_inactive_database_does_not_block_active_reads(library: Any) -> None:
    """Healthy active corrections remain usable despite a separate invalid scope."""
    seed_raw(library, [("good", 90, 0, 0)])
    execute(
        library.localcorrectionsdb_file,
        "CREATE TABLE correction (regex, rotation, offset_x, offset_y)",
    )
    execute(
        library.localcorrectionsdb_file,
        "INSERT INTO correction VALUES ('bad','47u',0,0)",
    )
    assert correction_values(fresh_library(library)) == [("good", 90, (0.0, 0.0))]


def test_local_csv_provenance_survives_switch_to_global_without_replaying(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """An archived local import cannot overwrite repaired global values later."""
    library = make_library(modules.library, tmp_path, local=True)
    library.create_correction_table(library.globalcorrectionsdb_file)
    library.apply_corrections(
        [("existing", 270, (0.25, -0.5))], db_path=library.globalcorrectionsdb_file
    )
    key = library.correction_csv_migration_key(
        tmp_path / "legacy.csv", "Pattern,Rotation\nexisting,90\n"
    )
    original = [("existing", 90, (0, 0))]
    library.apply_corrections(original, migration_key=key)
    library.update_correction_data("existing", 180, (1, 2))
    library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert fresh_library(library).has_correction_migration(key)
    result = fresh_library(library).apply_corrections(original, migration_key=key)
    assert not result.changed
    assert correction_values(fresh_library(library)) == [
        ("existing", 270, (0.25, -0.5))
    ]
    assert (
        execute(
            library.localcorrectionsdb_file,
            "SELECT name FROM sqlite_master WHERE name='correction'",
        )
        == []
    )
    assert execute(
        library.localcorrectionsdb_file,
        "SELECT migration_key FROM correction_migrations",
    ) == [(key,)]


def test_corrupt_alternate_csv_provenance_blocks_replay_but_not_active_reads(
    modules: SimpleNamespace, library: Any, tmp_path: Path
) -> None:
    """Uncertain archived provenance is surfaced without invalidating active corrections."""
    seed_raw(library, [("existing", 270, 0.25, -0.5)])
    Path(library.localcorrectionsdb_file).write_bytes(b"unreadable archive")
    key = library.correction_csv_migration_key(
        tmp_path / "legacy.csv", "Pattern,Rotation\nexisting,90\n"
    )
    before = raw_rows(library)
    with pytest.raises(modules.data.CorrectionDataError):
        library.has_correction_migration(key)
    with pytest.raises(modules.data.CorrectionDataError):
        library.apply_corrections([("existing", 90, (0, 0))], migration_key=key)
    assert raw_rows(library) == before
    assert correction_values(fresh_library(library)) == [
        ("existing", 270, (0.25, -0.5))
    ]


def test_switching_from_local_initializes_missing_global_before_drop(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """A board with local data can select a newly configured global data directory."""
    library = make_library(modules.library, tmp_path, [("local", 90, 0, 0)], local=True)
    assert not Path(library.globalcorrectionsdb_file).exists()
    execute(library.localcorrectionsdb_file, "CREATE TABLE unrelated (value)")
    execute(library.localcorrectionsdb_file, "INSERT INTO unrelated VALUES ('keep')")
    library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert correction_values(fresh_library(library)) == []
    assert execute(library.localcorrectionsdb_file, "SELECT * FROM unrelated") == [
        ("keep",)
    ]
    assert (
        execute(
            library.localcorrectionsdb_file,
            "SELECT name FROM sqlite_master WHERE name='correction'",
        )
        == []
    )


def test_switching_to_missing_global_migrates_legacy_before_validating(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Selecting new global storage uses its valid archived corrections."""
    library = make_library(modules.library, tmp_path, [("local", 90, 0, 0)], local=True)
    seed_legacy(library.rotationsdb_file, [("legacy-global", 180)])
    library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.globalcorrectionsdb_file
    assert correction_values(fresh_library(library)) == [
        ("legacy-global", 180, (0.0, 0.0))
    ]
    assert execute(library.rotationsdb_file, "SELECT * FROM rotation") == [
        ("legacy-global", 180)
    ]


def test_invalid_legacy_in_new_global_preserves_active_local_scope(
    modules: SimpleNamespace, tmp_path: Path
) -> None:
    """Discovering invalid legacy data during a switch cannot discard local records."""
    library = make_library(modules.library, tmp_path, [("local", 90, 0, 0)], local=True)
    seed_legacy(library.rotationsdb_file, [("legacy-bad", "47u")])
    before = raw_rows(library)
    with pytest.raises(modules.data.CorrectionDataError, match="47u"):
        library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert raw_rows(library) == before
    assert correction_values(fresh_library(library)) == [("local", 90, (0.0, 0.0))]
    invalid_global = library.read_correction_data(library.globalcorrectionsdb_file)
    assert any(row.rotation == "47u" for row in invalid_global.rows)
    assert invalid_global.corrections is None


@pytest.mark.parametrize("source", ["rotation", "parts"])
def test_switching_to_existing_global_retries_failed_migration(
    modules: SimpleNamespace, tmp_path: Path, source: str
) -> None:
    """A resolved transient migration failure cannot trap the board in local scope."""
    library = make_library(modules.library, tmp_path, [("local", 90, 0, 0)], local=True)
    library.create_correction_table(library.globalcorrectionsdb_file)
    library.apply_corrections(
        [("existing-global", 270, (0, 0))], db_path=library.globalcorrectionsdb_file
    )
    path = library.rotationsdb_file if source == "rotation" else library.partsdb_file
    seed_legacy(path, [("first", 90), ("explode", 180)])
    install_abort_trigger(library.globalcorrectionsdb_file)
    local_before = raw_rows(library)
    global_before = execute(
        library.globalcorrectionsdb_file, "SELECT rowid, * FROM correction"
    )

    with pytest.raises(
        modules.data.CorrectionDataError, match="injected write failure"
    ):
        library.switch_to_global_correction_database(True)
    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert raw_rows(library) == local_before
    assert (
        execute(library.globalcorrectionsdb_file, "SELECT rowid, * FROM correction")
        == global_before
    )
    assert not library.has_correction_migration(library._legacy_migration_key(path))

    execute(library.globalcorrectionsdb_file, "DROP TRIGGER fail_write")
    fresh = fresh_library(library)
    fresh.check_library()
    fresh.switch_to_global_correction_database(True)
    assert fresh.correctionsdb_file == fresh.globalcorrectionsdb_file
    assert correction_values(fresh_library(fresh)) == [
        ("existing-global", 270, (0.0, 0.0)),
        ("explode", 180, (0.0, 0.0)),
        ("first", 90, (0.0, 0.0)),
    ]
    assert len(migration_rows(fresh)) == 2
    assert execute(path, "SELECT * FROM rotation") == [("first", 90), ("explode", 180)]


def test_malformed_migration_metadata_preserves_repairable_raw_rows(
    modules: SimpleNamespace, library: Any
) -> None:
    """Metadata failures do not hide the original data required for recovery."""
    seed_raw(library, [("good", 90, 0, 0), ("bad", "47u", 0, 0)])
    execute(library.correctionsdb_file, "DROP TABLE correction_migrations")
    execute(
        library.correctionsdb_file, "CREATE TABLE correction_migrations (wrong_column)"
    )
    before = raw_rows(library)
    snapshot = fresh_library(library).read_correction_data()
    assert {row.pattern for row in snapshot.rows} == {"good", "bad"}
    assert next(row for row in snapshot.rows if row.pattern == "bad").rotation == "47u"
    assert snapshot.corrections is None
    assert raw_rows(library) == before


@pytest.mark.parametrize("mixed_case", [False, True])
@pytest.mark.parametrize("writer", ["batch", "save", "insert", "rotation", "parts"])
def test_reordered_storage_columns_are_written_by_name(
    library: Any, mixed_case: bool, writer: str
) -> None:
    """Every insert path respects schema names instead of corrupting reordered tables."""
    execute(library.correctionsdb_file, "DROP TABLE correction")
    columns = (
        "RoTaTiOn, ReGeX, OfFsEt_Y, OfFsEt_X"
        if mixed_case
        else "rotation, regex, offset_y, offset_x"
    )
    execute(library.correctionsdb_file, f"CREATE TABLE correction ({columns})")
    if writer == "batch":
        library.apply_corrections([("part", "90", (".25", "-.5"))])
    elif writer == "save":
        library.save_correction_data("part", "90", (".25", "-.5"))
    elif writer == "insert":
        library.insert_correction_data("part", "90", (".25", "-.5"))
    else:
        source = (
            library.rotationsdb_file if writer == "rotation" else library.partsdb_file
        )
        seed_legacy(source, [("part", 90)])
        getattr(library, f"migrate_corrections_from_{writer}")()
    offsets = (0.0, 0.0) if writer in {"rotation", "parts"} else (0.25, -0.5)
    stored = execute(
        library.correctionsdb_file,
        "SELECT regex, rotation, offset_x, offset_y FROM correction",
    )
    assert stored == [("part", 90, *offsets)]
    assert isinstance(stored[0][0], str)
    assert isinstance(stored[0][1], int)
    assert correction_values(fresh_library(library)) == [("part", 90, offsets)]


@pytest.mark.parametrize("mixed_case", [False, True])
def test_scope_copy_writes_reordered_destination_columns_by_name(
    library: Any, mixed_case: bool
) -> None:
    """Copying global corrections into an existing local schema preserves meaning."""
    seed_raw(library, [("global", 90, 0.25, -0.5)])
    columns = (
        "RoTaTiOn, ReGeX, OfFsEt_Y, OfFsEt_X"
        if mixed_case
        else "rotation, regex, offset_y, offset_x"
    )
    execute(library.localcorrectionsdb_file, f"CREATE TABLE correction ({columns})")
    execute(
        library.localcorrectionsdb_file,
        "INSERT INTO correction (regex, rotation, offset_x, offset_y) VALUES ('local',180,1,2)",
    )
    library.switch_to_global_correction_database(False)
    assert library.correctionsdb_file == library.localcorrectionsdb_file
    assert execute(
        library.localcorrectionsdb_file,
        "SELECT regex, rotation, offset_x, offset_y FROM correction",
    ) == [("global", 90, 0.25, -0.5)]
    assert correction_values(fresh_library(library)) == [("global", 90, (0.25, -0.5))]


_UNSUPPORTED_SCHEMAS = (
    "shadowed-rowid",
    "view",
    "without-rowid",
    "missing-column",
    "extra-column",
)


def replace_with_unsupported_schema(library, schema):
    """Seed malformed storage without relying on the compromised row identities."""
    target = library.correctionsdb_file
    execute(target, "DROP TABLE correction")
    if schema == "view":
        execute(target, "CREATE TABLE backing (regex, rotation, offset_x, offset_y)")
        execute(
            target, "INSERT INTO backing VALUES ('existing',90,1,2), ('other',180,3,4)"
        )
        execute(target, "CREATE VIEW correction AS SELECT * FROM backing")
        execute(
            target,
            "CREATE TRIGGER insert_view INSTEAD OF INSERT ON correction BEGIN "
            "INSERT INTO backing VALUES (NEW.regex,NEW.rotation,NEW.offset_x,NEW.offset_y); END",
        )
        execute(
            target,
            "CREATE TRIGGER update_view INSTEAD OF UPDATE ON correction BEGIN "
            "UPDATE backing SET regex=NEW.regex,rotation=NEW.rotation,"
            "offset_x=NEW.offset_x,offset_y=NEW.offset_y WHERE regex=OLD.regex; END",
        )
        execute(
            target,
            "CREATE TRIGGER delete_view INSTEAD OF DELETE ON correction BEGIN "
            "DELETE FROM backing WHERE regex=OLD.regex; END",
        )
    elif schema == "shadowed-rowid":
        execute(
            target,
            "CREATE TABLE correction (regex, rotation, offset_x, offset_y, rowid)",
        )
        execute(
            target,
            "INSERT INTO correction VALUES ('existing',90,1,2,7), ('other',180,3,4,7)",
        )
    elif schema == "without-rowid":
        execute(
            target,
            "CREATE TABLE correction (regex TEXT PRIMARY KEY, rotation, offset_x, offset_y) WITHOUT ROWID",
        )
        execute(
            target,
            "INSERT INTO correction VALUES ('existing',90,1,2), ('other',180,3,4)",
        )
    elif schema == "missing-column":
        execute(target, "CREATE TABLE correction (regex, rotation, offset_x)")
        execute(
            target, "INSERT INTO correction VALUES ('existing',90,1), ('other',180,3)"
        )
    else:
        assert schema == "extra-column"
        execute(
            target,
            "CREATE TABLE correction (regex, rotation, offset_x, offset_y, extra)",
        )
        execute(
            target,
            "INSERT INTO correction VALUES ('existing',90,1,2,'keep'), ('other',180,3,4,'also keep')",
        )
    return execute(target, "SELECT * FROM correction ORDER BY regex")


@pytest.mark.parametrize("schema", _UNSUPPORTED_SCHEMAS)
def test_unsupported_schema_has_no_guessed_repair_rowids(
    modules: SimpleNamespace, library: Any, schema: str
) -> None:
    """Unknown row identity cannot be presented as a safe target for user repair."""
    before = replace_with_unsupported_schema(library, schema)
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.rows == ()
    assert snapshot.issues
    assert snapshot.db_path == library.correctionsdb_file
    assert snapshot.corrections is None
    assert correction_values(fresh_library(library)) is None
    assert (
        execute(library.correctionsdb_file, "SELECT * FROM correction ORDER BY regex")
        == before
    )


@pytest.mark.parametrize("schema", _UNSUPPORTED_SCHEMAS)
@pytest.mark.parametrize(
    "operation",
    ["batch", "save", "repair", "delete-row", "delete-pattern", "update", "insert"],
)
def test_unsupported_schema_blocks_all_correction_mutations(
    modules, library, schema, operation
):
    """Unsupported storage is rejected before a trigger or ambiguous ID can change data."""
    before = replace_with_unsupported_schema(library, schema)
    mutation = {
        "batch": lambda: library.apply_corrections([("new", 270, (5, 6))]),
        "save": lambda: library.save_correction_data("new", 270, (5, 6)),
        "repair": lambda: library.save_correction_data(
            "repaired", 270, (5, 6), rowid=7
        ),
        "delete-row": lambda: library.delete_correction_row(7),
        "delete-pattern": lambda: library.delete_correction_data("existing"),
        "update": lambda: library.update_correction_data("existing", 270, (5, 6)),
        "insert": lambda: library.insert_correction_data("new", 270, (5, 6)),
    }[operation]
    with pytest.raises(modules.data.CorrectionDataError):
        mutation()
    assert (
        execute(library.correctionsdb_file, "SELECT * FROM correction ORDER BY regex")
        == before
    )
    if schema == "view":
        assert (
            execute(library.correctionsdb_file, "SELECT * FROM backing ORDER BY regex")
            == before
        )


@pytest.mark.parametrize(
    "pattern_type, rotation_type",
    [
        ("INTEGER", "INTEGER"),
        ("FLOATING POINT", "INTEGER"),
        ("STRING", "INTEGER"),
        ("TEXT", "REAL"),
        ("TEXT", "DOUBLE"),
    ],
)
@pytest.mark.parametrize("operation", ["read", "apply", "save", "update"])
def test_lossy_sqlite_affinity_is_rejected_before_data_changes(
    modules: SimpleNamespace,
    library: Any,
    pattern_type: str,
    rotation_type: str,
    operation: str,
) -> None:
    """Storage affinity cannot coerce a numeric pattern or round exact whole degrees."""
    target = library.correctionsdb_file
    execute(target, "DROP TABLE correction")
    execute(
        target,
        f"CREATE TABLE correction (regex {pattern_type}, rotation {rotation_type}, "
        "offset_x REAL, offset_y REAL)",
    )
    execute(
        target,
        "INSERT INTO correction (regex, rotation, offset_x, offset_y) VALUES ('456',90,.25,-.5)",
    )
    before = execute(target, "SELECT * FROM correction")
    if operation == "read":
        snapshot = fresh_library(library).read_correction_data()
        assert snapshot.rows == ()
        assert snapshot.issues
        assert snapshot.corrections is None
    else:
        exact_rotation = 9007199254740993
        mutation = {
            "apply": lambda: library.apply_corrections(
                [("123", exact_rotation, (0.125, -0.5))]
            ),
            "save": lambda: library.save_correction_data(
                "123", exact_rotation, (0.125, -0.5)
            ),
            "update": lambda: library.update_correction_data(
                "456", exact_rotation, (0.125, -0.5)
            ),
        }[operation]
        with pytest.raises(modules.data.CorrectionDataError):
            mutation()
    assert execute(target, "SELECT * FROM correction") == before


@pytest.mark.parametrize("rotation_type", ["INTEGER", "FLOATING POINT"])
def test_safe_typed_schema_preserves_numeric_patterns_and_large_rotations(
    library: Any, rotation_type: str
) -> None:
    """SQLite integer affinity takes priority over FLOAT in a declared type name."""
    target = library.correctionsdb_file
    execute(target, "DROP TABLE correction")
    execute(
        target,
        f"CREATE TABLE correction (regex TEXT, rotation {rotation_type}, "
        "offset_x REAL, offset_y REAL)",
    )
    exact_rotation = 9007199254740993
    library.apply_corrections([("123", exact_rotation, (0.125, -0.5))])
    assert execute(
        target, "SELECT regex, rotation, offset_x, offset_y FROM correction"
    ) == [("123", exact_rotation, 0.125, -0.5)]
    assert execute(
        target, "SELECT typeof(regex), typeof(rotation) FROM correction"
    ) == [("text", "integer")]
    assert correction_values(fresh_library(library)) == [
        ("123", exact_rotation, (0.125, -0.5))
    ]


def test_delete_all_corrections_empties_the_table_without_dropping_it(library):
    """Emptying the rules must leave the table that marks the storage as present."""
    library.insert_correction_data("R1", 90, (1, 2))
    library.insert_correction_data("R2", 180, (0, 0))

    assert library.delete_all_corrections() == 2

    assert raw_rows(library) == []
    # The table surviving is what keeps a board on its own corrections.
    assert execute(
        library.correctionsdb_file,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='correction'",
    )
    snapshot = fresh_library(library).read_correction_data()
    assert snapshot.corrections == ()
    assert snapshot.issues == ()


def test_delete_all_corrections_on_an_empty_table_reports_nothing_deleted(library):
    """An empty database is already in the requested state, not an error."""
    assert library.delete_all_corrections() == 0
    assert raw_rows(library) == []


def test_delete_all_corrections_only_empties_the_named_database(modules, tmp_path):
    """A board-local wipe must leave the shared global rules untouched."""
    library = make_library(modules.library, tmp_path, local=True)
    library.insert_correction_data("LOCAL", 90, (1, 2))
    global_path = library.globalcorrectionsdb_file
    library.create_correction_table(global_path)
    execute(
        global_path,
        "INSERT INTO correction (regex, rotation, offset_x, offset_y) VALUES ('GLOBAL', 270, 0, 0)",
    )

    assert library.delete_all_corrections(db_path=library.localcorrectionsdb_file) == 1

    assert raw_rows(library) == []
    assert [
        row[1] for row in execute(global_path, "SELECT rowid, regex FROM correction")
    ] == ["GLOBAL"]


def test_delete_all_corrections_rolls_back_when_the_write_is_aborted(modules, library):
    """A refused delete must leave every rule in place rather than a partial wipe."""
    library.insert_correction_data("R1", 90, (1, 2))
    library.insert_correction_data("explode", 180, (0, 0))
    before = raw_rows(library)
    install_abort_trigger(library.correctionsdb_file, operation="DELETE")

    with pytest.raises(modules.data.CorrectionDataError):
        library.delete_all_corrections()

    assert raw_rows(library) == before
