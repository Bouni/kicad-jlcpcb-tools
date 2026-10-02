"""Startup migration provenance and one-time notices survive close and archival."""

from collections.abc import Iterator
from dataclasses import dataclass
import json
from pathlib import Path
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
    reports: ModuleType, tmp_path: Path
) -> None:
    """A competing window retains existing generations and retries after release."""
    first = record(reports, tmp_path, AuditPlan())
    before = first.path.read_bytes()
    with reports._report_lock(first.path.parent):
        with pytest.raises(OSError):
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
