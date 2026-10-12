"""Startup migration provenance and one-time notices survive close and archival."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
import json
import multiprocessing
from pathlib import Path
import time
from types import ModuleType
from typing import Any, Optional

import pytest

from .wx_harness import load_siblings


@dataclass(frozen=True)
class AuditRow:
    """Supply the planner's public row contract without GUI or database fixtures."""

    reference: str = "R1"
    lcsc: str = "C123"
    status: str = "accounted"
    component_ids: tuple[str, ...] = ("uuid-r1",)
    reason: str = "explicit native assignment takes precedence"
    disposition: str = "board_override"
    native_value: Optional[str] = "C456"
    identity: tuple[Any, ...] = ("R1", "10k", "R_0603", False, False)


@dataclass(frozen=True)
class AuditPlan:
    """Carry the immutable generation captured alongside the historical rows."""

    active_table: bool = True
    rows: tuple[AuditRow, ...] = (AuditRow(),)
    active_digest: Optional[str] = "content-before-import"
    active_generation: Optional[str] = "table-generation-1"


@pytest.fixture
def reports() -> Iterator[ModuleType]:
    """Load the durable helper independently of the KiCad plugin."""
    with load_siblings(
        "_legacy_migration_report_tests", ("legacy_migration_report",), {}
    ) as modules:
        yield modules["legacy_migration_report"]


def record(reports: ModuleType, project: Path, plan: AuditPlan, **kwargs: Any) -> Any:
    """Persist one real report using portable project-local paths."""
    return reports.record_legacy_migration_report(
        str(project), str(project / "jlcpcb" / "parts.db"), plan, **kwargs
    )


def test_native_override_and_explicit_clear_show_old_new_and_reason(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Valid previous values remain visible when native intent wins on upgrade."""
    result = record(
        reports,
        tmp_path,
        AuditPlan(
            rows=(
                AuditRow(),
                AuditRow(
                    reference="R2",
                    component_ids=("uuid-r2",),
                    disposition="explicit_clear",
                    native_value="",
                    identity=("R2", "4.7k", "R_0603", False, False),
                ),
            )
        ),
    )

    assert result.needs_acknowledgment
    assert len(result.override_messages) == 2
    assert "R1" in result.override_messages[0]
    assert "C123" in result.override_messages[0]
    assert "C456" in result.override_messages[0]
    assert "explicit native assignment takes precedence" in result.override_messages[0]
    assert "R2" in result.override_messages[1]
    assert "C123" in result.override_messages[1]
    assert "explicitly empty" in result.override_messages[1]
    text = result.path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "jlcpcb/parts.db" in text
    assert "uuid-r1" in text
    assert "C456" in text


def test_all_startup_dispositions_remain_in_durable_report(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Routine and unresolved rows remain auditable without generating overrides."""
    dispositions = (
        "imported",
        "already_matches",
        "obsolete",
        "invalid_legacy",
        "unresolved",
        "pending",
    )
    rows = tuple(
        AuditRow(
            reference=f"R{index}",
            component_ids=(f"uuid-{index}",),
            disposition=disposition,
            identity=(f"R{index}", "10k", "R_0603", False, False),
        )
        for index, disposition in enumerate(dispositions)
    )
    result = record(reports, tmp_path, AuditPlan(rows=rows))

    document = json.loads(result.path.read_text(encoding="utf-8"))
    observed = document["generations"][0]["observations"][0]["rows"]
    assert tuple(row["disposition"] for row in observed) == dispositions
    assert observed[0]["legacy_value"] == "C123"
    assert observed[0]["native_value"] == "C456"
    assert observed[0]["component_ids"] == ["uuid-0"]
    assert observed[0]["identity"] == ["R0", "10k", "R_0603", False, False]
    assert result.override_messages == ()


def test_imports_never_become_preexisting_overrides_after_reopen(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A committed native import retains its startup origin in later observations."""
    initial = AuditRow(status="planned", disposition="pending", native_value="C123")
    first = record(
        reports,
        tmp_path,
        AuditPlan(rows=(initial,)),
        imported_assignments=(("uuid-r1", "C123"),),
    )
    later = record(reports, tmp_path, AuditPlan())

    assert first.override_messages == later.override_messages == ()
    document = json.loads(later.path.read_text(encoding="utf-8"))
    observations = document["generations"][0]["observations"]
    assert observations[0]["rows"][0]["disposition"] == "imported"
    assert observations[0]["rows"][0]["native_value"] == "C123"
    assert observations[1]["rows"][0]["disposition"] == "board_override"


def test_pending_notice_survives_archive_and_transient_report_cleanup(
    reports: ModuleType, tmp_path: Path
) -> None:
    """No active table is needed to discover an unacknowledged startup decision."""
    result = record(reports, tmp_path, AuditPlan())
    transient = tmp_path / "jlcpcb" / "schematic-save-report.txt"
    transient.write_text("old save report", encoding="utf-8")
    transient.unlink()
    assert record(reports, tmp_path, AuditPlan(active_table=False)) is None

    pending = reports.load_pending_legacy_migration_reports(str(tmp_path))

    assert len(pending) == 1
    assert pending[0].generation_id == result.generation_id
    assert pending[0].override_messages == result.override_messages
    assert pending[0].path.exists()
    assert pending[0].needs_acknowledgment


def test_acknowledgment_is_explicit_separate_and_persistent(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Writing or loading a notice does not acknowledge it on a forced close."""
    first = record(reports, tmp_path, AuditPlan())
    original_report = first.path.read_bytes()
    assert reports.load_pending_legacy_migration_reports(str(tmp_path))
    assert reports.load_pending_legacy_migration_reports(str(tmp_path))

    reports.acknowledge_legacy_migration_report(str(tmp_path), first.generation_id)

    assert reports.load_pending_legacy_migration_reports(str(tmp_path)) == ()
    assert first.path.read_bytes() == original_report
    repeated = record(reports, tmp_path, AuditPlan())
    assert not repeated.needs_acknowledgment
    assert repeated.path.read_bytes() == original_report
    acknowledgment = tmp_path / "jlcpcb" / "legacy-migration-acknowledgments.json"
    assert acknowledgment.exists()
    assert first.generation_id in acknowledgment.read_text(encoding="utf-8")


def test_recreated_identical_table_gets_its_own_notice(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A planner generation token distinguishes recreation with identical rows."""
    first = record(reports, tmp_path, AuditPlan())
    reports.acknowledge_legacy_migration_report(str(tmp_path), first.generation_id)

    second = record(
        reports, tmp_path, AuditPlan(active_generation="recreated-table-generation")
    )

    assert second.generation_id != first.generation_id
    assert second.needs_acknowledgment
    assert second.override_messages == first.override_messages
    pending = reports.load_pending_legacy_migration_reports(str(tmp_path))
    assert tuple(report.generation_id for report in pending) == (second.generation_id,)
    document = json.loads(second.path.read_text(encoding="utf-8"))
    assert len(document["generations"]) == 2


def test_digest_changes_within_generation_do_not_repeat_notice(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Stock or metadata row changes do not turn one active table into two audits."""
    first = record(reports, tmp_path, AuditPlan())
    reports.acknowledge_legacy_migration_report(str(tmp_path), first.generation_id)

    second = record(
        reports, tmp_path, AuditPlan(active_digest="content-after-stock-update")
    )

    assert second.generation_id == first.generation_id
    assert not second.needs_acknowledgment
    document = json.loads(second.path.read_text(encoding="utf-8"))
    assert len(document["generations"]) == 1
    assert document["generations"][0]["source_digests"] == [
        "content-before-import",
        "content-after-stock-update",
    ]


def test_normalized_equal_values_do_not_create_override_messages(
    reports: ModuleType, tmp_path: Path
) -> None:
    """An audit never warns that equivalent normalized IDs changed the BOM."""
    result = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(lcsc=" c123 ", native_value="C123"),)),
    )

    assert result.override_messages == ()


def test_two_different_values_naming_no_part_still_create_an_override_message(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Values are compared as text, so two different non-part values stay different."""
    result = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(lcsc="old note", native_value="new note"),)),
    )

    assert result.override_messages == (
        "R1 [uuid-r1]: legacy old note was superseded by native value new note; "
        "explicit native assignment takes precedence.",
    )


def test_report_write_failure_is_retryable_and_never_acknowledges(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed atomic replace leaves any previous audit complete and pending."""
    first = record(reports, tmp_path, AuditPlan())
    before = first.path.read_bytes()
    replace = reports.os.replace

    def fail_replace(source: Any, destination: Any) -> None:
        raise PermissionError("report is locked")

    monkeypatch.setattr(reports.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="report is locked"):
        record(reports, tmp_path, AuditPlan(active_generation="next-generation"))
    assert first.path.read_bytes() == before
    assert not list(first.path.parent.glob("*.tmp"))
    assert len(reports.load_pending_legacy_migration_reports(str(tmp_path))) == 1

    monkeypatch.setattr(reports.os, "replace", replace)
    second = record(reports, tmp_path, AuditPlan(active_generation="next-generation"))
    assert second.needs_acknowledgment
    assert len(reports.load_pending_legacy_migration_reports(str(tmp_path))) == 2


def test_failed_acknowledgment_remains_pending(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dialog response is only persisted after the acknowledgment write works."""
    first = record(reports, tmp_path, AuditPlan())

    def fail_replace(source: Any, destination: Any) -> None:
        raise PermissionError("acknowledgment is locked")

    monkeypatch.setattr(reports.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="acknowledgment is locked"):
        reports.acknowledge_legacy_migration_report(str(tmp_path), first.generation_id)
    pending = reports.load_pending_legacy_migration_reports(str(tmp_path))
    assert len(pending) == 1
    assert pending[0].needs_acknowledgment


@pytest.mark.parametrize(
    "filename",
    ["legacy-migration-report.json", "legacy-migration-acknowledgments.json"],
)
def test_corrupt_durable_data_is_preserved_for_retry(
    reports: ModuleType, tmp_path: Path, filename: str
) -> None:
    """Corrupt history must not be silently overwritten or marked delivered."""
    directory = tmp_path / "jlcpcb"
    directory.mkdir()
    damaged = directory / filename
    damaged.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError):
        record(reports, tmp_path, AuditPlan())

    assert damaged.read_text(encoding="utf-8") == "{broken"


def test_inactive_project_does_not_create_report_storage(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Ordinary projects with no legacy history stay free of audit files."""
    assert record(reports, tmp_path, AuditPlan(active_table=False)) is None
    assert reports.load_pending_legacy_migration_reports(str(tmp_path)) == ()
    assert not (tmp_path / "jlcpcb").exists()


def test_generation_is_required_before_any_report_is_written(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Without a generation snapshot a report cannot authorize safe retirement."""
    with pytest.raises(ValueError, match="generation"):
        record(reports, tmp_path, AuditPlan(active_generation=None))
    assert not (tmp_path / "jlcpcb").exists()


def test_unknown_generation_cannot_be_acknowledged(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Acknowledgments cannot preempt a future report that was never persisted."""
    with pytest.raises(ValueError, match="persisted"):
        reports.acknowledge_legacy_migration_report(str(tmp_path), "unknown")
    assert not (tmp_path / "jlcpcb" / "legacy-migration-acknowledgments.json").exists()


def test_shared_legacy_row_retains_each_boards_startup_provenance(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Importing one board cannot suppress another board's preexisting override."""
    imported = AuditRow(status="planned", disposition="pending", native_value="C123")
    first = record(
        reports,
        tmp_path,
        AuditPlan(rows=(imported,)),
        imported_assignments=(("uuid-r1", "C123"),),
    )
    second = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(component_ids=("other-board-uuid-r1",)),)),
    )

    assert first.override_messages == ()
    assert len(second.override_messages) == 1
    assert "other-board-uuid-r1" in second.override_messages[0]
    assert "C123" in second.override_messages[0]
    assert "C456" in second.override_messages[0]


def test_concurrent_update_keeps_history_retryable_without_stale_locks(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competing window retains existing generations and retries after release."""
    first = record(reports, tmp_path, AuditPlan())
    before = first.path.read_bytes()
    monkeypatch.setattr(reports, "_LOCK_TIMEOUT_SECONDS", 0.01)
    with reports._report_lock(first.path.parent):
        with pytest.raises(TimeoutError, match="Try again"):
            record(reports, tmp_path, AuditPlan(active_generation="other-window"))
        assert first.path.read_bytes() == before

    result = record(reports, tmp_path, AuditPlan(active_generation="other-window"))

    assert result.needs_acknowledgment
    assert len(reports.load_pending_legacy_migration_reports(str(tmp_path))) == 2


def test_failed_import_is_not_reported_as_success(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A native batch must prove the exact value of every target before import credit."""
    pending = AuditRow(
        status="planned",
        disposition="pending",
        native_value="C123",
        component_ids=("uuid-r1", "uuid-r2"),
    )
    result = record(
        reports,
        tmp_path,
        AuditPlan(rows=(pending,)),
        imported_assignments=(("uuid-r1", "C123"),),
    )

    document = json.loads(result.path.read_text(encoding="utf-8"))
    observed = document["generations"][0]["observations"][0]["rows"]
    assert observed[0]["disposition"] == "pending"
    assert result.override_messages == ()


def test_post_replace_sync_failure_reports_committed_acknowledgment(
    reports: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A post-commit directory sync error must not falsely claim the notice is pending."""
    if reports.os.name == "nt":
        pytest.skip("Windows does not expose directory fsync")
    first = record(reports, tmp_path, AuditPlan())
    fsync = reports.os.fsync
    calls = 0

    def fail_directory_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync unavailable")
        fsync(descriptor)

    monkeypatch.setattr(reports.os, "fsync", fail_directory_sync)
    reports.acknowledge_legacy_migration_report(str(tmp_path), first.generation_id)

    assert reports.load_pending_legacy_migration_reports(str(tmp_path)) == ()
    assert "committed" in caplog.text
    assert "directory sync unavailable" in caplog.text


def test_audit_directory_sync_failure_blocks_retirement_until_retry(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archival requires confirmed report durability, even after data was replaced."""
    if reports.os.name == "nt":
        pytest.skip("Windows does not expose directory fsync")
    original_fsync = reports.os.fsync
    calls = 0

    def fail_first_directory_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync unavailable")
        original_fsync(descriptor)

    monkeypatch.setattr(reports.os, "fsync", fail_first_directory_sync)
    with pytest.raises(OSError, match="directory sync unavailable"):
        record(reports, tmp_path, AuditPlan())

    retry = record(reports, tmp_path, AuditPlan())

    assert calls == 3  # Identical bytes still need the directory durability check.
    assert retry.needs_acknowledgment
    assert len(reports.load_pending_legacy_migration_reports(str(tmp_path))) == 1


def test_first_report_failure_can_retry_original_successful_import_provenance(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller's retained startup plan records the native import after a write retry."""
    startup = AuditPlan(
        rows=(AuditRow(status="planned", disposition="pending", native_value="C123"),)
    )
    replace = reports.os.replace

    def fail_replace(source: Any, destination: Any) -> None:
        raise PermissionError("first audit write unavailable")

    monkeypatch.setattr(reports.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="first audit write unavailable"):
        record(
            reports,
            tmp_path,
            startup,
            imported_assignments=(("uuid-r1", "C123"),),
        )
    assert reports.load_pending_legacy_migration_reports(str(tmp_path)) == ()
    monkeypatch.setattr(reports.os, "replace", replace)

    result = record(
        reports, tmp_path, startup, imported_assignments=(("uuid-r1", "C123"),)
    )

    assert result.needs_acknowledgment
    assert result.override_messages == ()
    document = json.loads(result.path.read_text(encoding="utf-8"))
    assert (
        document["generations"][0]["observations"][0]["rows"][0]["disposition"]
        == "imported"
    )


def test_obsolete_blob_identity_remains_auditable(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Unusual SQLite metadata does not prevent preserving an obsolete row."""
    result = record(
        reports,
        tmp_path,
        AuditPlan(
            rows=(
                AuditRow(
                    status="obsolete",
                    disposition="obsolete",
                    identity=("R1", b"\xff\x00", "R_0603", False, False),
                ),
            )
        ),
    )
    document = json.loads(result.path.read_text(encoding="utf-8"))
    identity = document["generations"][0]["observations"][0]["rows"][0]["identity"]
    assert identity[1] == {"bytes_hex": "ff00"}
    assert reports.load_pending_legacy_migration_reports(str(tmp_path))


def test_real_planner_generation_and_dispositions_round_trip_into_audit(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Real SQLite, native provenance and the planner share the report contract."""
    import importlib
    import sqlite3

    migration = importlib.import_module(f"{reports.__package__}.legacy_part_migration")
    assignments = importlib.import_module(f"{reports.__package__}.part_assignments")
    database = tmp_path / "parts.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE part_info (reference TEXT, value TEXT, footprint TEXT, "
            "lcsc TEXT, exclude_from_bom NUMERIC, exclude_from_pos NUMERIC)"
        )
        connection.execute(
            "INSERT INTO part_info VALUES ('R1', '10k', 'R_0603', 'C123', 0, 0)"
        )
    before = database.read_bytes()
    assignment, code = assignments.resolve_assignment({"LCSC": "C456"}, {}, "")
    footprint = migration.MigrationFootprint(
        "uuid-r1", "R1", "10k", "R_0603", True, True, assignment, code
    )
    plan = migration.plan_legacy_migration(
        str(database), (footprint,), lambda _key: None
    )

    result = reports.record_legacy_migration_report(str(tmp_path), str(database), plan)

    assert result.generation_id == plan.active_generation
    assert len(result.override_messages) == 1
    assert "C123" in result.override_messages[0]
    assert "C456" in result.override_messages[0]
    assert database.read_bytes() == before


def test_copied_boards_with_shared_uuids_keep_separate_audit_provenance(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A copied PCB's identical UUIDs cannot hide its distinct preexisting value."""
    imported = AuditRow(status="planned", disposition="pending", native_value="C123")
    first = record(
        reports,
        tmp_path,
        AuditPlan(rows=(imported,)),
        imported_assignments=(("uuid-r1", "C123"),),
        board_name="board-a.kicad_pcb",
    )
    second = record(reports, tmp_path, AuditPlan(), board_name="board-b.kicad_pcb")

    assert first.override_messages == ()
    assert len(second.override_messages) == 1
    assert "board-b.kicad_pcb" in second.override_messages[0]
    assert "C123" in second.override_messages[0]
    assert "C456" in second.override_messages[0]


def test_later_board_override_shows_only_previously_unseen_rows(
    reports: ModuleType, tmp_path: Path
) -> None:
    """One table generation can disclose later board decisions without nagging twice."""
    first = record(reports, tmp_path, AuditPlan(), board_name="board-a.kicad_pcb")
    reports.acknowledge_legacy_migration_report(
        str(tmp_path), first.generation_id, messages=first.override_messages
    )
    assert reports.load_pending_legacy_migration_reports(str(tmp_path)) == ()

    second = record(reports, tmp_path, AuditPlan(), board_name="board-b.kicad_pcb")

    assert second.generation_id == first.generation_id
    assert second.needs_acknowledgment
    assert len(second.override_messages) == 1
    assert "board-b.kicad_pcb" in second.override_messages[0]
    assert "board-a.kicad_pcb" not in second.override_messages[0]
    pending = reports.load_pending_legacy_migration_reports(str(tmp_path))
    assert pending[0].override_messages == second.override_messages


def test_modal_acknowledgment_cannot_cover_concurrent_unseen_messages(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A second session writing during the first modal retains its own pending notice."""
    shown = record(reports, tmp_path, AuditPlan(), board_name="shown.kicad_pcb")
    record(reports, tmp_path, AuditPlan(), board_name="not-yet-shown.kicad_pcb")

    reports.acknowledge_legacy_migration_report(
        str(tmp_path), shown.generation_id, messages=shown.override_messages
    )

    pending = reports.load_pending_legacy_migration_reports(str(tmp_path))
    assert len(pending) == 1
    assert len(pending[0].override_messages) == 1
    assert "not-yet-shown.kicad_pcb" in pending[0].override_messages[0]
    reports.acknowledge_legacy_migration_report(
        str(tmp_path), pending[0].generation_id, messages=pending[0].override_messages
    )
    assert reports.load_pending_legacy_migration_reports(str(tmp_path)) == ()


def test_acknowledgment_rejects_messages_never_persisted(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Fabricated receipt contents cannot silently acknowledge unrelated history."""
    first = record(reports, tmp_path, AuditPlan())

    with pytest.raises(ValueError, match="persisted"):
        reports.acknowledge_legacy_migration_report(
            str(tmp_path), first.generation_id, messages=("never shown or recorded",)
        )

    pending = reports.load_pending_legacy_migration_reports(str(tmp_path))
    assert pending[0].override_messages == first.override_messages


def test_board_context_stays_portable_in_history_and_messages(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Board identity uses the filename rather than embedding machine-local paths."""
    result = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name=str(tmp_path / "board.kicad_pcb"),
    )

    document = json.loads(result.path.read_text(encoding="utf-8"))
    assert (
        document["generations"][0]["observations"][0]["board_name"] == "board.kicad_pcb"
    )
    assert "board.kicad_pcb" in result.override_messages[0]
    assert str(tmp_path) not in result.path.read_text(encoding="utf-8")


def test_repeated_sessions_keep_first_provenance_and_latest_snapshot_bounded(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Changing native values cannot duplicate the entire recovery table forever."""
    rows = tuple(
        AuditRow(
            reference=f"R{index}",
            component_ids=(f"uuid-{index}",),
            identity=(f"R{index}", "10k", "R_0603", False, False),
        )
        for index in range(150)
    )
    first = record(
        reports, tmp_path, AuditPlan(rows=rows), board_name="board.kicad_pcb"
    )
    reports.acknowledge_legacy_migration_report(
        tmp_path, first.generation_id, messages=first.override_messages
    )
    for session in range(100):
        latest = tuple(replace(row, native_value=f"C{1000 + session}") for row in rows)
        result = record(
            reports,
            tmp_path,
            AuditPlan(rows=latest),
            board_name="board.kicad_pcb",
        )

    generation = json.loads(result.path.read_text(encoding="utf-8"))["generations"][0]
    observations = generation["observations"]
    assert len(observations) <= 2
    assert sum(len(item["rows"]) for item in observations) <= 300
    assert {row["native_value"] for row in observations[0]["rows"]} == {"C456"}
    assert {row["native_value"] for row in observations[-1]["rows"]} == {"C1099"}
    assert result.override_messages == ()
    assert not result.needs_acknowledgment


def test_version_one_history_compacts_without_repeating_acknowledged_messages(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Upgrade preserves first-row attribution, unnamed boards and exact receipts."""
    imported = AuditRow(status="planned", disposition="pending", native_value="C123")
    first = record(
        reports,
        tmp_path,
        AuditPlan(rows=(imported,)),
        imported_assignments=(("uuid-r1", "C123"),),
        board_name="a.kicad_pcb",
    )
    second = record(reports, tmp_path, AuditPlan(), board_name="b.kicad_pcb")
    reports.acknowledge_legacy_migration_report(
        tmp_path, second.generation_id, messages=second.override_messages
    )
    data = json.loads(first.path.read_text(encoding="utf-8"))
    generation = data["generations"][0]
    initial_a = generation["observations"][0]
    initial_b = next(
        item
        for item in generation["observations"]
        if item["board_name"] == "b.kicad_pcb"
    )
    unnamed = {"rows": [dict(initial_b["rows"][0], component_ids=["unnamed-uuid"])]}
    later_a = {
        "board_name": "a.kicad_pcb",
        "rows": [dict(initial_b["rows"][0], native_value="C789")],
    }
    generation["observations"] = [
        initial_a,
        initial_b,
        later_a,
        unnamed,
        dict(later_a, rows=[dict(later_a["rows"][0], native_value="C999")]),
    ]
    first.path.write_text(json.dumps(data), encoding="utf-8")
    original = first.path.read_bytes()
    pending = reports.load_pending_legacy_migration_reports(tmp_path)
    assert first.path.read_bytes() == original
    assert len(pending[0].override_messages) == 1
    assert "unnamed-uuid" in pending[0].override_messages[0]

    result = record(
        reports,
        tmp_path,
        AuditPlan(
            rows=(replace(imported, disposition="board_override", native_value="C999"),)
        ),
        board_name="a.kicad_pcb",
    )

    compacted = json.loads(result.path.read_text(encoding="utf-8"))["generations"][0]
    counts = {}
    for observation in compacted["observations"]:
        board = observation.get("board_name", "")
        counts[board] = counts.get(board, 0) + 1
    assert all(count <= 2 for count in counts.values())
    assert result.override_messages == pending[0].override_messages
    assert compacted["generation_id"] == first.generation_id
    assert compacted["source_digests"] == ["content-before-import"]


def test_provenance_retry_cannot_replace_latest_rows_or_retention_state(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A delayed startup retry cannot erase a newer board's current blockers."""
    initial = AuditPlan(
        rows=(AuditRow(status="planned", disposition="pending", native_value="C123"),)
    )
    record(
        reports,
        tmp_path,
        initial,
        imported_assignments=(("uuid-r1", "C123"),),
        board_name="board.kicad_pcb",
        provenance_only=True,
    )
    latest = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(native_value="C789"),)),
        board_name="board.kicad_pcb",
        retention_messages=("Save board.kicad_pcb before retiring recovery.",),
    )
    before = latest.path.read_bytes()

    retry = record(
        reports,
        tmp_path,
        initial,
        imported_assignments=(("uuid-r1", "C123"),),
        board_name="board.kicad_pcb",
        provenance_only=True,
        retention_messages=(),
    )

    assert retry.path.read_bytes() == before
    assert retry.override_messages == ()
    assert retry.retention_messages == (
        "Save board.kicad_pcb before retiring recovery.",
    )
    generation = json.loads(retry.path.read_text(encoding="utf-8"))["generations"][0]
    assert generation["observations"][-1]["rows"][0]["native_value"] == "C789"


def test_delayed_provenance_adds_new_rows_without_replacing_current_snapshot(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Recoverable startup history may arrive after the current board dropped a row."""
    current = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="board.kicad_pcb",
        retention_messages=("Save board.kicad_pcb before retiring recovery.",),
    )
    imported = AuditRow(
        reference="R2",
        component_ids=("uuid-r2",),
        identity=("R2", "4.7k", "R_0603", False, False),
        status="planned",
        disposition="pending",
        native_value="C123",
    )

    result = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(), imported)),
        board_name="board.kicad_pcb",
        imported_assignments=(("uuid-r2", "C123"),),
        provenance_only=True,
    )

    assert result.messages == current.messages
    generation = json.loads(result.path.read_text(encoding="utf-8"))["generations"][0]
    origin, latest = generation["observations"]
    assert [row["reference"] for row in origin["rows"]] == ["R1", "R2"]
    assert origin["rows"][1]["disposition"] == "imported"
    assert [row["reference"] for row in latest["rows"]] == ["R1"]


@pytest.mark.parametrize("later_disposition", ["already_matches", "board_override"])
def test_earlier_failed_startup_import_replaces_later_ordered_provenance_only(
    reports: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_disposition: str,
) -> None:
    """An older import retry retains its origin despite a later window writing first."""
    earlier_import = AuditPlan(
        rows=(AuditRow(status="planned", disposition="pending", native_value="C123"),)
    )
    replace_report = reports.os.replace

    def fail_first_audit(source: Any, destination: Any) -> None:
        raise PermissionError("startup audit volume unavailable")

    monkeypatch.setattr(reports.os, "replace", fail_first_audit)
    with pytest.raises(PermissionError, match="startup audit"):
        record(
            reports,
            tmp_path,
            earlier_import,
            board_name="board.kicad_pcb",
            imported_assignments=(("uuid-r1", "C123"),),
            provenance_only=True,
            provenance_order=100,
        )
    assert reports.load_pending_legacy_migration_reports(tmp_path) == ()
    monkeypatch.setattr(reports.os, "replace", replace_report)
    later_startup = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(disposition=later_disposition),)),
        board_name="board.kicad_pcb",
        provenance_only=True,
        provenance_order=200,
    )
    current = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(native_value="C789"),)),
        board_name="board.kicad_pcb",
        retention_messages=("Save board.kicad_pcb before retiring recovery.",),
    )
    retry = record(
        reports,
        tmp_path,
        earlier_import,
        board_name="board.kicad_pcb",
        imported_assignments=(("uuid-r1", "C123"),),
        provenance_only=True,
        provenance_order=100,
    )

    assert retry.override_messages == ()
    assert retry.retention_messages == current.retention_messages
    origin, latest = json.loads(retry.path.read_text(encoding="utf-8"))["generations"][
        0
    ]["observations"]
    assert origin["rows"][0]["disposition"] == "imported"
    assert latest["rows"][0]["native_value"] == "C789"
    reports.acknowledge_legacy_migration_report(
        tmp_path, later_startup.generation_id, messages=later_startup.messages
    )
    assert (
        reports.load_pending_legacy_migration_reports(tmp_path)[0].messages
        == current.retention_messages
    )


@pytest.mark.parametrize("known_order", [False, True])
def test_later_import_cannot_rewrite_earlier_override_or_unordered_legacy_origin(
    reports: ModuleType, tmp_path: Path, known_order: bool
) -> None:
    """Undo and later recovery cannot invent an older origin than established history."""
    kwargs = {"provenance_only": True, "provenance_order": 100} if known_order else {}
    first = record(
        reports, tmp_path, AuditPlan(), board_name="board.kicad_pcb", **kwargs
    )
    imported = AuditPlan(
        rows=(AuditRow(status="planned", disposition="pending", native_value="C123"),)
    )

    later = record(
        reports,
        tmp_path,
        imported,
        board_name="board.kicad_pcb",
        imported_assignments=(("uuid-r1", "C123"),),
        provenance_only=True,
        provenance_order=200,
    )
    final = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(disposition="already_matches", native_value="C123"),)),
        board_name="board.kicad_pcb",
    )

    assert later.override_messages == final.override_messages == first.override_messages
    origin, latest = json.loads(final.path.read_text(encoding="utf-8"))["generations"][
        0
    ]["observations"]
    assert origin["rows"][0]["disposition"] == "board_override"
    assert latest["rows"][0]["native_value"] == "C123"


def test_exact_generation_retirement_resolves_notices_and_preserves_open_receipts(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Archival after another board recovers cannot leave a stale active-recovery notice."""
    no_override = AuditPlan(rows=(AuditRow(disposition="already_matches"),))
    first = record(
        reports,
        tmp_path,
        no_override,
        board_name="a.kicad_pcb",
        retention_messages=("b.kicad_pcb still needs active recovery.",),
    )
    record(
        reports,
        tmp_path,
        no_override,
        board_name="b.kicad_pcb",
        retention_messages=("Save b.kicad_pcb before retiring recovery.",),
    )
    observations = json.loads(first.path.read_text(encoding="utf-8"))["generations"][0][
        "observations"
    ]
    reports.mark_legacy_migration_reports_retired(
        tmp_path,
        str(tmp_path / "jlcpcb" / "parts.db"),
        generation_id=first.generation_id,
    )

    pending = reports.load_pending_legacy_migration_reports(tmp_path)
    assert len(pending) == 1
    assert pending[0].messages == ()
    retired = json.loads(first.path.read_text(encoding="utf-8"))["generations"][0]
    assert retired["retired"] is True
    assert retired["observations"] == observations
    reports.acknowledge_legacy_migration_report(
        tmp_path, first.generation_id, messages=first.messages
    )
    assert reports.load_pending_legacy_migration_reports(tmp_path) == ()


def test_delayed_provenance_cannot_clear_other_boards_retention_notices(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A startup retry cannot resolve another board's active-recovery notices."""
    first = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="a.kicad_pcb",
        retention_messages=("b.kicad_pcb still needs active recovery.",),
    )
    retry = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="b.kicad_pcb",
        provenance_only=True,
        retention_messages=(),
    )

    assert retry.retention_messages == first.retention_messages


@pytest.mark.parametrize("order", [True, -1, "100"])
def test_invalid_provenance_order_cannot_create_or_replace_history(
    reports: ModuleType, tmp_path: Path, order: Any
) -> None:
    """Invalid chronology cannot silently reorder established startup evidence."""
    with pytest.raises(ValueError, match="provenance order"):
        record(
            reports, tmp_path, AuditPlan(), provenance_only=True, provenance_order=order
        )
    assert not (tmp_path / "jlcpcb").exists()


def test_retention_notices_are_once_only_and_replace_only_the_current_board(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Durable notices suppress repeat disclosure without changing recovery proof."""
    no_override = AuditPlan(rows=(AuditRow(disposition="already_matches"),))
    first_message = "Save a.kicad_pcb before retiring recovery."
    second_message = "b.kicad_pcb still needs active recovery."
    first = record(
        reports,
        tmp_path,
        no_override,
        board_name="a.kicad_pcb",
        retention_messages=(first_message, first_message),
    )
    assert first.messages == first.retention_messages == (first_message,)
    assert first.override_messages == ()
    assert (
        reports.load_pending_legacy_migration_reports(tmp_path)[0].messages
        == first.messages
    )
    reports.acknowledge_legacy_migration_report(
        tmp_path, first.generation_id, messages=first.messages
    )
    repeated = record(
        reports,
        tmp_path,
        no_override,
        board_name="a.kicad_pcb",
        retention_messages=(first_message,),
    )
    assert repeated.messages == ()
    assert not repeated.needs_acknowledgment
    second = record(
        reports,
        tmp_path,
        no_override,
        board_name="b.kicad_pcb",
        retention_messages=(second_message,),
    )
    assert second.messages == (second_message,)
    unchanged = record(reports, tmp_path, no_override, board_name="b.kicad_pcb")
    assert unchanged.messages == (second_message,)
    resolved_first = record(
        reports,
        tmp_path,
        no_override,
        board_name="a.kicad_pcb",
        retention_messages=(),
    )
    assert resolved_first.messages == (second_message,)
    resolved_second = record(
        reports,
        tmp_path,
        no_override,
        board_name="b.kicad_pcb",
        retention_messages=(),
    )
    assert resolved_second.messages == ()


def test_retention_modal_acknowledges_resolved_messages_but_not_concurrent_unseen_ones(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Replacing current blockers during a modal cannot reject or expand its receipt."""
    old_message = "Save board.kicad_pcb before retiring recovery."
    new_message = "sibling.kicad_pcb still needs active recovery."
    first = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="board.kicad_pcb",
        retention_messages=(old_message,),
    )
    second = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="board.kicad_pcb",
        retention_messages=(new_message,),
    )
    assert second.retention_messages == (new_message,)

    reports.acknowledge_legacy_migration_report(
        tmp_path, first.generation_id, messages=first.messages
    )

    pending = reports.load_pending_legacy_migration_reports(tmp_path)
    assert pending[0].messages == (new_message,)


@pytest.mark.parametrize(
    "notice",
    [
        [],
        {"current_messages": [], "known_messages": "damaged"},
        {"current_messages": ["not previously recorded"], "known_messages": []},
        {"current_messages": [], "known_messages": ["duplicate", "duplicate"]},
    ],
)
def test_damaged_retention_history_is_never_replaced_or_acknowledged(
    reports: ModuleType, tmp_path: Path, notice: Any
) -> None:
    """Unreadable notice state cannot be erased by a fresh audit or dialog response."""
    first = record(reports, tmp_path, AuditPlan())
    data = json.loads(first.path.read_text(encoding="utf-8"))
    data["generations"][0]["retention_notices"] = {"board.kicad_pcb": notice}
    first.path.write_text(json.dumps(data), encoding="utf-8")
    before = first.path.read_bytes()

    with pytest.raises(ValueError, match="retention"):
        record(reports, tmp_path, AuditPlan())
    with pytest.raises(ValueError, match="retention"):
        reports.acknowledge_legacy_migration_report(
            tmp_path, first.generation_id, messages=first.override_messages
        )

    assert first.path.read_bytes() == before
    assert not (first.path.parent / "legacy-migration-acknowledgments.json").exists()


def _report_process(
    directory: str,
    connection: Any,
    operation: str,
    shown_messages: tuple[str, ...] = (),
) -> None:
    """Exercise the production read/merge/write lock from separate interpreters."""
    try:
        with load_siblings(
            "_legacy_migration_report_process", ("legacy_migration_report",), {}
        ) as modules:
            reports = modules["legacy_migration_report"]
            if operation == "first":
                original_read = reports._read_report

                def paused_read(path: Path) -> dict[str, Any]:
                    result = original_read(path)
                    connection.send("locked")
                    if not connection.poll(10) or connection.recv() != "release":
                        raise RuntimeError(
                            "The parent did not release the first writer"
                        )
                    return result

                reports._read_report = paused_read
            else:
                connection.send("attempt")
            if operation == "acknowledge":
                reports.acknowledge_legacy_migration_report(
                    directory, "table-generation-1", messages=shown_messages
                )
            else:
                record(
                    reports,
                    Path(directory),
                    AuditPlan(),
                    board_name=f"{operation}.kicad_pcb",
                )
            connection.send("done")
    except BaseException as error:
        connection.send(("error", type(error).__name__, str(error)))
    finally:
        connection.close()


@pytest.mark.parametrize("operation", ["second", "acknowledge"])
def test_real_process_contention_waits_then_merges_without_spurious_failure(
    reports: ModuleType, tmp_path: Path, operation: str
) -> None:
    """Two closing windows may briefly overlap without dropping either audit update."""
    original = record(reports, tmp_path, AuditPlan(), board_name="original.kicad_pcb")
    context = multiprocessing.get_context("spawn")
    first_parent, first_child = context.Pipe()
    second_parent, second_child = context.Pipe()
    first = context.Process(
        target=_report_process, args=(str(tmp_path), first_child, "first")
    )
    second = context.Process(
        target=_report_process,
        args=(str(tmp_path), second_child, operation, original.override_messages),
    )
    first.start()
    try:
        assert first_parent.poll(10)
        assert first_parent.recv() == "locked"
        second.start()
        assert second_parent.poll(10)
        assert second_parent.recv() == "attempt"
        assert not second_parent.poll(0.15), "The competitor failed instead of waiting"
        first_parent.send("release")
        assert first_parent.poll(10)
        assert first_parent.recv() == "done"
        assert second_parent.poll(10)
        assert second_parent.recv() == "done"
        pending = reports.load_pending_legacy_migration_reports(tmp_path)
        messages = pending[0].override_messages
        assert any("first.kicad_pcb" in message for message in messages)
        if operation == "second":
            assert any("second.kicad_pcb" in message for message in messages)
            assert any("original.kicad_pcb" in message for message in messages)
        else:
            assert all("original.kicad_pcb" not in message for message in messages)
    finally:
        for process in (first, second):
            if process.pid is not None:
                process.join(timeout=0.5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
        for connection in (first_parent, first_child, second_parent, second_child):
            connection.close()
    assert first.exitcode == second.exitcode == 0


@pytest.mark.parametrize("operation", ["record", "acknowledge"])
def test_real_process_lock_deadline_preserves_data_and_allows_retry(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A stalled owner yields a bounded failure instead of indefinite UI blocking."""
    original = record(reports, tmp_path, AuditPlan(), board_name="original.kicad_pcb")
    before = original.path.read_bytes()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    owner = context.Process(
        target=_report_process, args=(str(tmp_path), child, "first")
    )
    owner.start()

    def update() -> None:
        if operation == "record":
            record(reports, tmp_path, AuditPlan(), board_name="retry.kicad_pcb")
        else:
            reports.acknowledge_legacy_migration_report(
                tmp_path, original.generation_id, messages=original.override_messages
            )

    try:
        assert parent.poll(10)
        assert parent.recv() == "locked"
        monkeypatch.setattr(reports, "_LOCK_TIMEOUT_SECONDS", 0.05, raising=False)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            update()
        assert 0.04 <= time.monotonic() - start < 1
        assert original.path.read_bytes() == before
        assert (
            reports.load_pending_legacy_migration_reports(tmp_path)[0].override_messages
            == original.override_messages
        )
        parent.send("release")
        assert parent.poll(10)
        assert parent.recv() == "done"
        update()
        pending = reports.load_pending_legacy_migration_reports(tmp_path)
        assert any(
            "first.kicad_pcb" in message for message in pending[0].override_messages
        )
    finally:
        owner.join(timeout=0.5)
        if owner.is_alive():
            owner.terminate()
            owner.join(timeout=5)
        parent.close()
        child.close()
    assert owner.exitcode == 0


def test_retired_generation_ignores_stale_blockers_and_keeps_provenance_receipts(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A late active-plan writer cannot reopen blockers after confirmed archival."""
    original = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="board.kicad_pcb",
        provenance_only=True,
        provenance_order=200,
    )
    shown = record(
        reports,
        tmp_path,
        AuditPlan(),
        board_name="board.kicad_pcb",
        retention_messages=("sibling.kicad_pcb still needs active recovery.",),
    )

    reports.mark_legacy_migration_reports_retired(
        tmp_path,
        str(tmp_path / "jlcpcb" / "parts.db"),
        generation_id=shown.generation_id,
    )
    stale = record(
        reports,
        tmp_path,
        AuditPlan(rows=(AuditRow(native_value="C789"),)),
        board_name="board.kicad_pcb",
        retention_messages=("Another stale active-recovery blocker.",),
    )

    assert stale.retention_messages == ()
    assert stale.override_messages == original.override_messages
    imported = AuditPlan(
        rows=(AuditRow(status="planned", disposition="pending", native_value="C123"),)
    )
    retry = record(
        reports,
        tmp_path,
        imported,
        board_name="board.kicad_pcb",
        imported_assignments=(("uuid-r1", "C123"),),
        provenance_only=True,
        provenance_order=100,
    )
    assert retry.messages == ()
    reports.acknowledge_legacy_migration_report(
        tmp_path, shown.generation_id, messages=shown.messages
    )
    assert reports.load_pending_legacy_migration_reports(tmp_path) == ()
    generation = json.loads(retry.path.read_text(encoding="utf-8"))["generations"][0]
    assert generation["retired"] is True
    assert generation["observations"][0]["rows"][0]["disposition"] == "imported"
    assert generation["observations"][-1]["rows"][0]["native_value"] == "C789"
    assert (
        "Another stale active-recovery blocker."
        not in generation["retention_notices"]["board.kicad_pcb"]["known_messages"]
    )


def test_retirement_resolution_is_scoped_to_database_and_generation(
    reports: ModuleType, tmp_path: Path
) -> None:
    """A recreated generation and another database retain their independent notices."""
    no_override = AuditPlan(rows=(AuditRow(disposition="already_matches"),))
    first = record(
        reports, tmp_path, no_override, retention_messages=("first generation blocker",)
    )
    second = record(
        reports,
        tmp_path,
        replace(no_override, active_generation="second-generation"),
        retention_messages=("second generation blocker",),
    )
    other = reports.record_legacy_migration_report(
        tmp_path,
        str(tmp_path / "other.db"),
        replace(no_override, active_generation="other-database-generation"),
        retention_messages=("other database blocker",),
    )
    database = str(tmp_path / "jlcpcb" / "parts.db")
    reports.mark_legacy_migration_reports_retired(
        tmp_path, database, generation_id=first.generation_id
    )

    pending = {
        item.generation_id: item.retention_messages
        for item in reports.load_pending_legacy_migration_reports(tmp_path)
    }
    assert pending[first.generation_id] == ()
    assert pending[second.generation_id] == ("second generation blocker",)
    assert pending[other.generation_id] == ("other database blocker",)
    reports.mark_legacy_migration_reports_retired(
        tmp_path, database, source_check=lambda: True
    )
    pending = {
        item.generation_id: item.retention_messages
        for item in reports.load_pending_legacy_migration_reports(tmp_path)
    }
    assert pending[first.generation_id] == pending[second.generation_id] == ()
    assert pending[other.generation_id] == ("other database blocker",)


@pytest.mark.parametrize("existing_report", [False, True])
def test_broad_retirement_requires_source_check_before_any_storage_change(
    reports: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_report: bool,
) -> None:
    """An unguarded broad retirement cannot suppress notices or create storage."""
    if existing_report:
        record(
            reports,
            tmp_path,
            AuditPlan(),
            retention_messages=("board.kicad_pcb still needs active recovery.",),
        )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    write_report = reports._atomic_write_json
    writes = 0

    def count_write(path: Path, data: dict[str, Any], **kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        write_report(path, data, **kwargs)

    monkeypatch.setattr(reports, "_atomic_write_json", count_write)

    with pytest.raises(ValueError, match="source_check"):
        reports.mark_legacy_migration_reports_retired(
            tmp_path, str(tmp_path / "jlcpcb" / "parts.db")
        )

    assert writes == 0
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before
    if not existing_report:
        assert not (tmp_path / "jlcpcb").exists()


def test_retirement_resolution_without_report_creates_no_storage(
    reports: ModuleType, tmp_path: Path
) -> None:
    """Ordinary inactive projects need no report directory or lock sidecar."""
    reports.mark_legacy_migration_reports_retired(
        tmp_path, str(tmp_path / "jlcpcb" / "parts.db"), source_check=lambda: True
    )
    assert not (tmp_path / "jlcpcb").exists()


@pytest.mark.parametrize("check_fails", [False, True])
def test_retirement_rechecks_inactive_source_under_lock_before_clearing_new_generation(
    reports: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check_fails: bool,
) -> None:
    """A table recreated after the caller's inactive snapshot keeps its newer notice."""
    no_override = AuditPlan(rows=(AuditRow(disposition="already_matches"),))
    first = record(
        reports, tmp_path, no_override, retention_messages=("older blocker",)
    )
    record(
        reports,
        tmp_path,
        replace(no_override, active_generation="new-active-generation"),
        retention_messages=("new generation still needs active recovery",),
    )
    before = first.path.read_bytes()
    monkeypatch.setattr(reports, "_LOCK_TIMEOUT_SECONDS", 0.0)
    checked = False

    def source_unchanged() -> bool:
        nonlocal checked
        checked = True
        with pytest.raises(TimeoutError), reports._report_lock(first.path.parent):
            pass
        if check_fails:
            raise OSError("source cannot be rechecked")
        return False

    if check_fails:
        with pytest.raises(OSError, match="source cannot be rechecked"):
            reports.mark_legacy_migration_reports_retired(
                tmp_path,
                str(tmp_path / "jlcpcb" / "parts.db"),
                source_check=source_unchanged,
            )
    else:
        reports.mark_legacy_migration_reports_retired(
            tmp_path,
            str(tmp_path / "jlcpcb" / "parts.db"),
            source_check=source_unchanged,
        )

    assert checked
    assert first.path.read_bytes() == before
    pending = reports.load_pending_legacy_migration_reports(tmp_path)
    assert pending[-1].retention_messages == (
        "new generation still needs active recovery",
    )


@pytest.mark.parametrize("retired", [None, 1, "true"])
def test_corrupt_retired_state_is_preserved_for_recovery(
    reports: ModuleType, tmp_path: Path, retired: Any
) -> None:
    """Invalid terminal state cannot silently suppress or resurrect migration notices."""
    first = record(reports, tmp_path, AuditPlan())
    data = json.loads(first.path.read_text(encoding="utf-8"))
    data["generations"][0]["retired"] = retired
    first.path.write_text(json.dumps(data), encoding="utf-8")
    before = first.path.read_bytes()

    with pytest.raises(ValueError, match="retired"):
        reports.load_pending_legacy_migration_reports(tmp_path)
    with pytest.raises(ValueError, match="retired"):
        record(reports, tmp_path, AuditPlan())
    with pytest.raises(ValueError, match="retired"):
        reports.mark_legacy_migration_reports_retired(
            tmp_path, str(tmp_path / "jlcpcb" / "parts.db"), source_check=lambda: True
        )
    assert first.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["replace", "directory_sync"])
def test_failed_retirement_resolution_retries_without_losing_known_notices(
    reports: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A post-archive marker failure can settle on the next proven inactive close."""
    first = record(
        reports,
        tmp_path,
        AuditPlan(),
        retention_messages=("board.kicad_pcb still needs active recovery.",),
    )
    before = first.path.read_bytes()
    calls = 0
    operation = reports.os.replace if failure == "replace" else reports._sync_directory

    def fail_once(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("retirement resolution unavailable")
        operation(*args)

    if failure == "replace":
        monkeypatch.setattr(reports.os, "replace", fail_once)
    else:
        monkeypatch.setattr(reports, "_sync_directory", fail_once)
    database = str(tmp_path / "jlcpcb" / "parts.db")
    with pytest.raises(OSError, match="retirement resolution"):
        reports.mark_legacy_migration_reports_retired(
            tmp_path, database, generation_id=first.generation_id
        )
    if failure == "replace":
        assert first.path.read_bytes() == before
        assert (
            reports.load_pending_legacy_migration_reports(tmp_path)[
                0
            ].retention_messages
            == first.retention_messages
        )

    reports.mark_legacy_migration_reports_retired(
        tmp_path, database, source_check=lambda: True
    )

    assert calls == 2
    pending = reports.load_pending_legacy_migration_reports(tmp_path)
    assert pending[0].retention_messages == ()
    assert pending[0].override_messages == first.override_messages
    reports.acknowledge_legacy_migration_report(
        tmp_path, first.generation_id, messages=first.messages
    )
    assert reports.load_pending_legacy_migration_reports(tmp_path) == ()
