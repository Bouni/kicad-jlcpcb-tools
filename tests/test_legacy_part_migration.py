"""Legacy recovery plans are conservative, read-only, and independent of stock."""

from collections.abc import Iterator
from dataclasses import replace
import importlib
from pathlib import Path
import sqlite3
from types import ModuleType
from typing import Any, Optional
from unittest.mock import Mock

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def migration() -> Iterator[ModuleType]:
    """Load the planner and its real alias resolver without GUI dependencies."""
    with load_siblings(
        "_legacy_migration_tests", ("legacy_part_migration",), {}
    ) as modules:
        yield modules["legacy_part_migration"]


def seed(database: Path, rows: tuple[tuple[Any, ...], ...]) -> None:
    """Use main's historical table shape, including fields never migrated."""
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE part_info (reference TEXT, value TEXT, footprint TEXT, "
            "lcsc TEXT, stock NUMERIC, exclude_from_bom NUMERIC, "
            "exclude_from_pos NUMERIC, assembly_process TEXT)"
        )
        connection.executemany("INSERT INTO part_info VALUES (?,?,?,?,?,?,?,?)", rows)
        connection.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('generation_count', '19')")


def legacy_row(**changes: Any) -> tuple[Any, ...]:
    """Return a historical assignment with representative cached supplier data."""
    row = {
        "reference": "R1",
        "value": "10k",
        "footprint": "R_0603",
        "lcsc": " c123 ",
        "stock": 900,
        "exclude_from_bom": 0,
        "exclude_from_pos": 0,
        "assembly_process": "SMT",
    }
    row.update(changes)
    return tuple(row.values())


def footprint(migration: ModuleType, **changes: Any) -> Any:
    """Capture immutable native values, with genuinely missing fields by default."""
    assignment, code = schematic(migration)
    values = {
        "component_id": "uuid-r1",
        "reference": "R1",
        "value": "10k",
        "footprint": "R_0603",
        "bom": True,
        "pos": True,
        "assignment": assignment,
        "lcsc": code,
    }
    values.update(changes)
    return migration.MigrationFootprint(**values)


def schematic(migration: ModuleType, fields: Optional[dict[str, str]] = None) -> Any:
    """Use the actual provenance resolver for a linked schematic symbol."""
    resolver = importlib.import_module(f"{migration.__package__}.part_assignments")
    return resolver.resolve_assignment(fields or {}, {}, "")


def test_missing_native_assignment_is_recovered_as_one_normalized_id(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Recovery only plans a UUID assignment and does not write any data."""
    database = tmp_path / "project ? #.db"
    seed(database, (legacy_row(),))
    original = database.read_bytes()
    part = footprint(migration)
    lookup = Mock(return_value=schematic(migration))

    plan = migration.plan_legacy_migration(str(database), (part,), lookup)

    assert plan.assignments == (("uuid-r1", "C123"),)
    assert plan.rows[0].status == "planned"
    assert plan.rows[0].component_ids == ("uuid-r1",)
    assert plan.active_table and plan.retirement_eligible
    assert not plan.unresolved and not plan.blocked_component_ids
    assert part.assignment.status == "missing"
    assert database.read_bytes() == original
    lookup.assert_called_once_with("uuid-r1")


@pytest.mark.parametrize(
    "fields",
    [
        {"LCSC": "C999"},
        {"LCSC": ""},
        {"LCSC": "garbage"},
        {"LCSC": "C123", "JLCPCB": "C999"},
        {"JLCPCB PartNr": "C222"},
    ],
)
def test_existing_native_assignment_is_never_overwritten(
    migration: ModuleType, tmp_path: Path, fields: dict[str, str]
) -> None:
    """Valid, empty, invalid, and conflicting native data retain precedence."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, fields)
    part = footprint(migration, assignment=assignment, lcsc=code)
    lookup = Mock(side_effect=AssertionError("native assignments need no lookup"))

    plan = migration.plan_legacy_migration(str(database), (part,), lookup)

    assert not plan.assignments
    assert plan.retirement_eligible == (assignment.status in {"valid", "empty"})
    assert len(plan.unresolved) == int(assignment.status in {"invalid", "conflict"})
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "R2"},
        {"value": "11k"},
        {"footprint": "R_0805"},
        {"exclude_from_bom": 1},
        {"exclude_from_pos": 1},
    ],
)
def test_historical_mismatch_is_obsolete_for_current_board(
    migration: ModuleType, tmp_path: Path, changes: dict[str, Any]
) -> None:
    """Only the exact old-main identity tuple can seed a missing assignment."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(**changes),))
    lookup = Mock(side_effect=AssertionError("mismatched rows cannot migrate"))

    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lookup
    )

    assert not plan.assignments and plan.retirement_eligible
    assert not plan.unresolved
    assert plan.rows[0].status == "obsolete"
    assert not plan.blocked_component_ids
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"LCSC": "C999"},
        {"LCSC": ""},
        {"LCSC": "invalid"},
        {"LCSC": "C123", "JLCPCB": "C999"},
    ],
)
def test_schematic_disagreement_cannot_be_overwritten_by_stale_database(
    migration: ModuleType, tmp_path: Path, fields: dict[str, str]
) -> None:
    """A matching cached row still needs linked schematic agreement."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))

    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration),),
        lambda _key: schematic(migration, fields),
    )

    assert not plan.assignments and not plan.retirement_eligible
    assert plan.blocked_component_ids == ("uuid-r1",)
    assert "schematic" in plan.unresolved[0].reason


@pytest.mark.parametrize("linked_fields", [None, {"LCSC": " c123 "}])
def test_missing_or_agreeing_schematic_allows_recovery(
    migration: ModuleType, tmp_path: Path, linked_fields: Optional[dict[str, str]]
) -> None:
    """Absence and an equivalent canonical ID are the only importable states."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration),),
        lambda _key: schematic(migration, linked_fields),
    )
    assert plan.assignments == (("uuid-r1", "C123"),)


def test_unresolved_link_preserves_recovery_and_blocks_preference_fallback(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Failure to resolve a link cannot turn the stale cache into board truth."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: None
    )
    assert not plan.assignments and not plan.retirement_eligible
    assert plan.blocked_component_ids == ("uuid-r1",)
    assert "unresolved" in plan.unresolved[0].reason


def test_absent_rows_require_saved_board_scope_check_not_local_blocker(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Local obsolescence leaves directory ownership to saved-board verification."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(), legacy_row(reference="U1", lcsc="C456")))
    before = database.read_bytes()
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: schematic(migration)
    )
    assert plan.assignments == (("uuid-r1", "C123"),)
    assert plan.retirement_eligible
    assert not plan.unresolved
    assert [row.reference for row in plan.rows if row.status == "obsolete"] == ["U1"]
    assert database.read_bytes() == before


def test_duplicate_native_identity_matches_are_ambiguous(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A legacy reference cannot be guessed between two physical footprints."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    first = footprint(migration)
    second = replace(first, component_id="uuid-r1-copy")
    plan = migration.plan_legacy_migration(
        str(database), (first, second), lambda _key: schematic(migration)
    )
    assert not plan.assignments and not plan.retirement_eligible
    assert plan.blocked_component_ids == ("uuid-r1", "uuid-r1-copy")


def test_conflicting_legacy_rows_do_not_choose_an_arbitrary_assignment(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Malformed historical duplicates remain recoverable instead of picking one."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(), legacy_row(lcsc="C456")))
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: schematic(migration)
    )
    assert not plan.assignments and not plan.retirement_eligible
    assert len(plan.unresolved) == 2
    assert all(row.native_value is None for row in plan.unresolved)


@pytest.mark.parametrize("code", [None, "", "garbage", "C123 extra", "C123４"])
def test_nonrecoverable_legacy_text_is_never_imported(
    migration: ModuleType, tmp_path: Path, code: Any
) -> None:
    """Invalid historical values survive in archives without entering PCB fields."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(lcsc=code),))
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: schematic(migration)
    )
    assert not plan.assignments and plan.retirement_eligible
    assert plan.rows[0].status == "ignored"


def test_missing_database_does_not_create_any_storage(
    migration: ModuleType, tmp_path: Path
) -> None:
    """The planner is safe before a project ever creates its jlcpcb directory."""
    database = tmp_path / "missing" / "project.db"
    plan = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert not plan.active_table and not plan.assignments and not plan.rows
    assert plan.retirement_eligible
    assert not database.parent.exists()


def test_archives_are_never_reimported_and_old_plugin_recreation_is_seen(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Only the active table participates across repeated and mixed-version opens."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE part_info RENAME TO part_info_retired")
    original = database.read_bytes()
    lookup = lambda _key: schematic(migration)
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lookup
    )
    assert not plan.active_table and not plan.assignments
    assert database.read_bytes() == original
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE part_info AS SELECT * FROM part_info_retired")
        connection.execute("UPDATE part_info SET lcsc = 'C789'")
    reopened = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lookup
    )
    assert reopened.assignments == (("uuid-r1", "C789"),)


def test_missing_columns_raise_without_upgrading_legacy_database(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Unknown historical schemas must remain available for manual recovery."""
    database = tmp_path / "project.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE part_info (reference TEXT, lcsc TEXT)")
    original = database.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert database.read_bytes() == original


def test_matching_excluded_part_restores_only_assignment(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Historical exclusion bits are identity guards, never migration edits."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(exclude_from_bom=1, exclude_from_pos=1, stock=123456),))
    part = footprint(migration, bom=False, pos=False)
    plan = migration.plan_legacy_migration(
        str(database), (part,), lambda _key: schematic(migration)
    )
    assert plan.assignments == (("uuid-r1", "C123"),)
    assert not part.bom and not part.pos
    assert plan.retirement_eligible


def test_row_read_failure_closes_read_only_connection_and_retains_bytes(
    migration: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed historical read leaves recovery available for a later opening."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    original = database.read_bytes()
    connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
            if sql.startswith("SELECT reference"):
                raise sqlite3.OperationalError("historical row read failed")
            return super().execute(sql, parameters)

    def failing_connection(database_uri: str, **kwargs: Any) -> sqlite3.Connection:
        assert database_uri.endswith("?mode=ro") and kwargs["uri"] is True
        connection = connect(database_uri, factory=FailingConnection, **kwargs)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(migration.sqlite3, "connect", failing_connection)
        with pytest.raises(
            sqlite3.OperationalError, match="historical row read failed"
        ):
            migration.plan_legacy_migration(
                str(database),
                (footprint(migration),),
                lambda _key: schematic(migration),
            )
    assert database.read_bytes() == original
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        sqlite3.Connection.execute(opened[0], "SELECT 1")


@pytest.mark.parametrize(
    "peer_fields", [None, {"LCSC": "C456"}, {"LCSC": ""}, {"LCSC": "bad"}]
)
def test_reused_symbol_candidate_requires_all_native_instances_to_agree(
    migration: ModuleType, tmp_path: Path, peer_fields: Optional[dict[str, str]]
) -> None:
    """One legacy import cannot create disagreement on a reused physical symbol."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, peer_fields)
    peer = footprint(
        migration,
        component_id="uuid-r2",
        reference="R2",
        assignment=assignment,
        lcsc=code,
    )
    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration), peer),
        lambda _key: schematic(migration),
        target_identity=lambda _key: ("sheet.kicad_sch", "symbol-uuid"),
    )
    assert not plan.assignments and not plan.retirement_eligible
    assert "shared schematic" in plan.unresolved[0].reason
    assert set(plan.blocked_component_ids) == {"uuid-r1", "uuid-r2"}


def test_reused_symbol_matching_candidates_can_migrate_together(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Two matching imports use UUIDs while one shared schematic property agrees."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(), legacy_row(reference="R2")))
    parts = (
        footprint(migration),
        footprint(migration, component_id="uuid-r2", reference="R2"),
    )
    plan = migration.plan_legacy_migration(
        str(database),
        parts,
        lambda _key: schematic(migration),
        target_identity=lambda _key: ("sheet.kicad_sch", "symbol-uuid"),
    )
    assert plan.assignments == (("uuid-r1", "C123"), ("uuid-r2", "C123"))
    assert plan.retirement_eligible


def test_reused_symbol_conflicting_candidates_are_all_reported(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Distinct legacy references cannot assign different IDs to one symbol."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(), legacy_row(reference="R2", lcsc="C456")))
    parts = (
        footprint(migration),
        footprint(migration, component_id="uuid-r2", reference="R2"),
    )
    plan = migration.plan_legacy_migration(
        str(database),
        parts,
        lambda _key: schematic(migration),
        target_identity=lambda _key: ("sheet.kicad_sch", "symbol-uuid"),
    )
    assert not plan.assignments and len(plan.unresolved) == 2


def test_reused_symbol_bom_disagreement_blocks_candidate_recovery(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A native peer with the same ID still cannot contradict shared BOM state."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, {"LCSC": "C123"})
    peer = footprint(
        migration,
        component_id="uuid-r2",
        reference="R2",
        assignment=assignment,
        lcsc=code,
        bom=False,
    )
    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration), peer),
        lambda _key: schematic(migration),
        target_identity=lambda _key: ("sheet.kicad_sch", "symbol-uuid"),
    )
    assert not plan.assignments and not plan.retirement_eligible


def test_plan_digest_guards_archive_against_unexamined_historical_updates(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A real plan and archive share one signature covering unknown cache columns."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: schematic(migration)
    )
    storage = importlib.import_module(f"{migration.__package__}.legacy_part_storage")
    assert plan.active_digest is not None
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE part_info SET stock = 55")
    before = database.read_bytes()
    with pytest.raises(storage.LegacyPartInfoChanged):
        storage.retire_legacy_part_info(
            str(database), expected_digest=plan.active_digest
        )
    assert database.read_bytes() == before
    refreshed = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: schematic(migration)
    )
    assert (
        storage.retire_legacy_part_info(
            str(database), expected_digest=refreshed.active_digest
        )
        == "part_info_retired"
    )


@pytest.mark.parametrize("whitespace", ["   ", "\t", "\n"])
def test_schematic_whitespace_alias_cannot_authorize_legacy_import(
    migration: ModuleType, tmp_path: Path, whitespace: str
) -> None:
    """A valid first alias cannot hide a malformed linked schematic assignment."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    linked = schematic(migration, {"LCSC": "C123", "JLCPCB Part #": whitespace})
    assert linked[0].status == "valid"

    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: linked
    )

    assert not plan.assignments and not plan.retirement_eligible
    assert plan.blocked_component_ids == ("uuid-r1",)
    assert "schematic" in plan.unresolved[0].reason


@pytest.mark.parametrize("canonical", ["C123", ""])
def test_native_whitespace_alias_cannot_count_as_completed_recovery(
    migration: ModuleType, tmp_path: Path, canonical: str
) -> None:
    """Neither a valid value nor explicit clear masks another malformed alias."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, {"LCSC": canonical, "JLCPCB Part #": "   "})
    assert assignment.status in {"valid", "empty"}
    part = footprint(migration, assignment=assignment, lcsc=code)

    plan = migration.plan_legacy_migration(
        str(database), (part,), lambda _key: schematic(migration)
    )

    assert not plan.assignments and not plan.retirement_eligible
    assert plan.blocked_component_ids == ("uuid-r1",)
    assert "native" in plan.unresolved[0].reason


def test_shared_native_whitespace_alias_blocks_other_instance_legacy_import(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A reused symbol's unsafe native peer cannot supply assignment consensus."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, {"LCSC": "C123", "JLCPCB Part #": "   "})
    peer = footprint(
        migration,
        component_id="uuid-r2",
        reference="R2",
        assignment=assignment,
        lcsc=code,
    )

    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration), peer),
        lambda _key: schematic(migration),
        target_identity=lambda _key: ("sheet.kicad_sch", "symbol-uuid"),
    )

    assert not plan.assignments and not plan.retirement_eligible
    assert set(plan.blocked_component_ids) == {"uuid-r1", "uuid-r2"}


@pytest.mark.parametrize(
    ("fields", "disposition", "value"),
    [
        ({"LCSC": ""}, "explicit_clear", ""),
        ({"LCSC": "C456"}, "board_override", "C456"),
        ({"LCSC": " c123 "}, "already_matches", "C123"),
    ],
)
def test_native_precedence_keeps_old_and_new_values_for_first_save_audit(
    migration: ModuleType,
    tmp_path: Path,
    fields: dict[str, str],
    disposition: str,
    value: str,
) -> None:
    """Recovery decisions keep enough provenance to audit accepted board choices."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, fields)
    part = footprint(migration, assignment=assignment, lcsc=code)
    plan = migration.plan_legacy_migration(str(database), (part,), lambda _key: None)
    row = plan.rows[0]
    assert row.status == "accounted" and row.disposition == disposition
    assert row.lcsc == "C123" and row.native_value == value
    assert row.identity == ("R1", "10k", "R_0603", False, False)
    assert not plan.assignments


def test_applied_import_provenance_is_not_reclassified_as_existing_board_choice(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A later close can distinguish an imported assignment from a preexisting one."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    assignment, code = schematic(migration, {"LCSC": "C123"})
    part = footprint(migration, assignment=assignment, lcsc=code)
    plan = migration.plan_legacy_migration(
        str(database),
        (part,),
        lambda _key: None,
        imported_assignments={"uuid-r1": "C123"},
    )
    assert plan.rows[0].disposition == "imported"
    changed = replace(
        part, assignment=schematic(migration, {"LCSC": "C456"})[0], lcsc="C456"
    )
    later = migration.plan_legacy_migration(
        str(database),
        (changed,),
        lambda _key: None,
        imported_assignments={"uuid-r1": "C123"},
    )
    assert later.rows[0].disposition == "board_override"


def test_table_generation_survives_rows_metadata_and_unrelated_schema_edits(
    migration: ModuleType, tmp_path: Path
) -> None:
    """Unrelated database activity cannot repeatedly announce the same migration."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    before = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert before.active_generation
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE part_info SET stock = 17")
        connection.execute("CREATE TABLE other_data (payload TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('audit', 'acknowledged')")
    after = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert after.active_digest != before.active_digest
    assert after.active_generation == before.active_generation
    storage = importlib.import_module(f"{migration.__package__}.legacy_part_storage")
    storage.retire_legacy_part_info(str(database), expected_digest=after.active_digest)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE part_info AS SELECT * FROM part_info_retired")
    recreated = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert recreated.active_generation != before.active_generation


@pytest.mark.parametrize("status", ["pcb_only", "no_schematic"])
def test_confirmed_schematic_absence_allows_exact_missing_field_recovery(
    migration: ModuleType, tmp_path: Path, status: str
) -> None:
    """Proven absence is safe to recover without treating broken links as absence."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    link = migration.MigrationLink(status)
    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration),),
        lambda _key: link,
        target_identity=lambda _key: None,
    )
    assert plan.assignments == (("uuid-r1", "C123"),)
    assert plan.rows[0].disposition == "pending"
    assert plan.rows[0].native_value == "C123"
    assert plan.retirement_eligible  # Saved-board coverage is still required.


def test_declared_unresolved_link_preserves_legacy_recovery(
    migration: ModuleType, tmp_path: Path
) -> None:
    """An unreadable root or broken link cannot masquerade as a PCB-only part."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    link = migration.MigrationLink("unresolved", reason="declared schematic is missing")
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: link
    )
    assert not plan.assignments and not plan.retirement_eligible
    assert "declared schematic is missing" in plan.unresolved[0].reason


@pytest.mark.parametrize("second", [{}, {"LCSC": " c123 "}])
def test_every_authenticated_multiunit_member_can_agree_with_recovery(
    migration: ModuleType, tmp_path: Path, second: dict[str, str]
) -> None:
    """A component group checks each member's provenance before one PCB import."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    link = migration.MigrationLink(
        "linked",
        (schematic(migration), schematic(migration, second)),
        (("project.kicad_sch", "unit-a"), ("project.kicad_sch", "unit-b")),
    )
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: link
    )
    assert plan.assignments == (("uuid-r1", "C123"),)


@pytest.mark.parametrize(
    "second",
    [
        {"LCSC": "C456"},
        {"LCSC": ""},
        {"LCSC": "bad"},
        {"LCSC": "C123", "JLCPCB": "   "},
    ],
)
def test_disagreeing_multiunit_member_cannot_be_hidden_by_representative(
    migration: ModuleType, tmp_path: Path, second: dict[str, str]
) -> None:
    """Any disagreeing unit preserves the candidate even if the first agrees."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    link = migration.MigrationLink(
        "linked",
        (schematic(migration), schematic(migration, second)),
        (("project.kicad_sch", "unit-a"), ("project.kicad_sch", "unit-b")),
    )
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: link
    )
    assert not plan.assignments and not plan.retirement_eligible


def test_shared_member_occurrences_require_complete_board_coverage(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A candidate cannot rewrite a shared unit whose other instance is absent."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    key = ("shared.kicad_sch", "unit-a")
    link = migration.MigrationLink(
        "linked",
        (schematic(migration),),
        (key,),
        covered_occurrences=((*key, "/root/sheet-a/unit-a"),),
        expected_occurrences=(
            (*key, "/root/sheet-a/unit-a"),
            (*key, "/root/sheet-b/unit-a"),
        ),
    )
    plan = migration.plan_legacy_migration(
        str(database), (footprint(migration),), lambda _key: link
    )
    assert not plan.assignments and not plan.retirement_eligible
    assert "shared" in plan.unresolved[0].reason


def test_table_generation_survives_vacuum_and_unrelated_archive_prefix_names(
    migration: ModuleType, tmp_path: Path
) -> None:
    """File maintenance and similarly named user tables are not new migrations."""
    database = tmp_path / "project.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE temporary_first (value TEXT)")
        connection.execute("CREATE TABLE temporary_second (value TEXT)")
    seed(database, (legacy_row(),))
    before = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    with sqlite3.connect(database) as connection:
        old_page = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='part_info'"
        ).fetchone()
        connection.execute("DROP TABLE temporary_first")
        connection.execute("DROP TABLE temporary_second")
        connection.commit()
        connection.execute("VACUUM")
        new_page = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='part_info'"
        ).fetchone()
        assert new_page != old_page
    compacted = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert compacted.active_generation == before.active_generation
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE part_info_retiredFoo (unrelated TEXT)")
        connection.execute("CREATE TABLE part_info_retired_01 (unrelated TEXT)")
    unrelated = migration.plan_legacy_migration(str(database), (), lambda _key: None)
    assert unrelated.active_generation == before.active_generation


def test_changed_legacy_candidate_is_not_misreported_as_previously_imported(
    migration: ModuleType, tmp_path: Path
) -> None:
    """A later cache edit cannot rewrite the provenance of an earlier native import."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(lcsc="C456"),))
    assignment, code = schematic(migration, {"LCSC": "C123"})
    plan = migration.plan_legacy_migration(
        str(database),
        (footprint(migration, assignment=assignment, lcsc=code),),
        lambda _key: None,
        imported_assignments={"uuid-r1": "C123"},
    )
    assert plan.rows[0].disposition == "board_override"
    assert (plan.rows[0].lcsc, plan.rows[0].native_value) == ("C456", "C123")


@pytest.mark.parametrize("reverse", [False, True])
def test_incomplete_multiunit_member_blocks_connected_candidate_imports(
    migration: ModuleType, tmp_path: Path, reverse: bool
) -> None:
    """A rejected import cannot supply the shared consensus for another import."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(), legacy_row(reference="R2")))
    shared, private_a, private_b = (
        ("shared.kicad_sch", "shared-unit"),
        ("private.kicad_sch", "unit-a"),
        ("private.kicad_sch", "unit-b"),
    )
    missing = schematic(migration)
    links = {
        "uuid-r1": migration.MigrationLink(
            "linked",
            (missing, missing),
            (shared, private_a),
            covered_occurrences=(
                (*shared, "/sheet-a/shared"),
                (*private_a, "/sheet-a/a"),
            ),
            expected_occurrences=(
                (*shared, "/sheet-a/shared"),
                (*shared, "/sheet-b/shared"),
                (*private_a, "/sheet-a/a"),
                (*private_a, "/sheet-c/a"),
            ),
        ),
        "uuid-r2": migration.MigrationLink(
            "linked",
            (missing, missing),
            (shared, private_b),
            covered_occurrences=(
                (*shared, "/sheet-b/shared"),
                (*private_b, "/sheet-b/b"),
            ),
            expected_occurrences=(
                (*shared, "/sheet-a/shared"),
                (*shared, "/sheet-b/shared"),
                (*private_b, "/sheet-b/b"),
            ),
        ),
    }
    parts = (
        footprint(migration),
        footprint(migration, component_id="uuid-r2", reference="R2"),
    )
    if reverse:
        parts = tuple(reversed(parts))
    plan = migration.plan_legacy_migration(str(database), parts, links.__getitem__)
    assert not plan.assignments
    assert len(plan.unresolved) == 2
    assert all(row.native_value is None for row in plan.unresolved)
    assert set(plan.blocked_component_ids) == {"uuid-r1", "uuid-r2"}


@pytest.mark.parametrize("reverse", [False, True])
def test_all_shared_group_peers_stay_blocked_from_preference_fallback(
    migration: ModuleType, tmp_path: Path, reverse: bool
) -> None:
    """Overlapping physical groups retain every affected native peer identity."""
    database = tmp_path / "project.db"
    seed(database, (legacy_row(),))
    shared_x = ("shared.kicad_sch", "unit-x")
    shared_y = ("shared.kicad_sch", "unit-y")
    missing = schematic(migration)
    links = {
        "uuid-r1": migration.MigrationLink(
            "linked", (missing, missing), (shared_x, shared_y)
        ),
        "uuid-r2": migration.MigrationLink("linked", (missing,), (shared_x,)),
        "uuid-r3": migration.MigrationLink("linked", (missing,), (shared_y,)),
    }
    parts = (
        footprint(migration),
        footprint(migration, component_id="uuid-r2", reference="R2"),
        footprint(migration, component_id="uuid-r3", reference="R3"),
    )
    if reverse:
        parts = tuple(reversed(parts))
    plan = migration.plan_legacy_migration(str(database), parts, links.__getitem__)
    assert not plan.assignments
    assert plan.blocked_component_ids == ("uuid-r1", "uuid-r2", "uuid-r3")
