"""Retire the obsolete assignment table only after complete schematic saving."""

from contextlib import closing
from importlib import import_module
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import _SelectableFootprint, focus, modal_handler, window_ui
from .test_schematic_autosave_native import associated_schematic
from .test_variant_schematic_autosave import SCHEMATIC_ROOT, _schematic_symbol

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx

LEGACY_ROWS = [("R1", "10k", "R0603", "C999", 53, 0, 0, "SMT")]


def seed_project(ui: Any, variants: bool) -> Path:
    """Keep stale assignments and unrelated project data across real constructors."""
    if not variants:
        ui.board.names.clear()
        ui.board.current = ""
    path = ui.path / "jlcpcb" / "project.db"
    path.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE part_info (reference TEXT PRIMARY KEY, value TEXT, "
            "footprint TEXT, lcsc TEXT, stock NUMERIC, exclude_from_bom NUMERIC, "
            "exclude_from_pos NUMERIC, assembly_process TEXT)"
        )
        connection.executemany(
            "INSERT INTO part_info VALUES (?,?,?,?,?,?,?,?)", LEGACY_ROWS
        )
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('generation_count', '17')")
        connection.execute(
            "INSERT INTO metadata VALUES ('schematic_storage_notice_acked', '1')"
        )
        connection.execute("CREATE TABLE other_project_data (payload BLOB)")
        connection.execute("INSERT INTO other_project_data VALUES (?)", (b"keep",))
    return path


def has_legacy(path: Path) -> bool:
    """Inspect persisted schema from a new connection, independently of the store."""
    with closing(sqlite3.connect(path)) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='part_info'"
            ).fetchone()
            is not None
        )


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("assignment", ["C111", ""])
def test_complete_autosave_archives_legacy_rows_and_reopen_does_not_recreate_table(
    window_ui: Any, variants: bool, assignment: str
) -> None:
    """Changing or clearing Default saves current native data before retiring SQL."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    first = True

    def check(ui: Any) -> None:
        nonlocal first
        assert has_legacy(dbfile) is first
        # Both table modes must retain explicit native data over stale SQL C999.
        if variants:
            assert ui.controller.session.snapshot.get("component-1", "").lcsc != "C999"
        else:
            assert ui.dialog.store.get_part("R1")["lcsc"] != "C999"
        if first:
            if variants:
                ui.board.parts[0].SetField("LCSC", assignment)
            else:
                assert ui.dialog._apply_lcsc_assignments({"R1": assignment}) == ["R1"]
        exporter = import_module(ui.mainwindow.__package__ + ".schematicexport")
        write = exporter.atomic_write_schematic

        def checked_write(target: str, content: str) -> None:
            assert has_legacy(dbfile) is first
            write(target, content)

        with patch.object(
            exporter, "atomic_write_schematic", side_effect=checked_write
        ):
            assert ui.dialog.Close() is True
        assert f'(property "LCSC" "{assignment}"' in path.read_text(encoding="utf-8")
        assert not has_legacy(dbfile)
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )
            assert connection.execute(
                "SELECT * FROM metadata ORDER BY key"
            ).fetchall() == [
                ("generation_count", "17"),
                ("schematic_storage_notice_acked", "1"),
            ]
            assert connection.execute(
                "SELECT * FROM other_project_data"
            ).fetchall() == [(b"keep",)]
        first = False

    window_ui.run(check, check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("outcome", ["cancel", "discard", "write", "partial"])
def test_skipped_canceled_and_incomplete_saves_keep_legacy_assignments(
    window_ui: Any, variants: bool, outcome: str
) -> None:
    """A close decision or an earlier written sheet cannot authorize retirement."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    original = path.read_bytes()
    safety = import_module(window_ui.mainwindow.__package__ + ".schematic_safety")
    lock = Path(safety.get_schematic_lock_path(str(path)))
    second = window_ui.path / "second.kicad_sch"
    if outcome in ("cancel", "discard"):
        lock.write_text('{"username": "test", "hostname": "local"}')
    elif outcome == "partial":
        second.write_text(
            original.decode("utf-8").replace(
                SCHEMATIC_ROOT, "00000000-0000-0000-0000-000000000002"
            ),
            encoding="utf-8",
        )
    second_original = second.read_bytes() if second.exists() else None

    def check(ui: Any) -> None:
        exporter = import_module(ui.mainwindow.__package__ + ".schematicexport")
        write = exporter.atomic_write_schematic
        written = []

        def fail_write(target: str, content: str) -> None:
            assert has_legacy(dbfile)
            if outcome == "write" or (outcome == "partial" and written):
                raise OSError("injected schematic write failure")
            write(target, content)
            written.append(target)

        def answer(dialog: Any) -> None:
            result = ui.wx.ID_NO if outcome == "discard" else ui.wx.ID_CANCEL
            dialog.EndModal(result)

        targets = [str(path)]
        if outcome == "partial":
            targets.append(str(second))
        discovery = import_module(ui.mainwindow.__package__ + ".schematic_discovery")
        try:
            with (
                patch.object(
                    ui.mainwindow,
                    "discover_project_schematics",
                    return_value=discovery.SchematicDiscovery(
                        paths=tuple(targets),
                        status="present",
                        diagnostics=(),
                        source_state=(),
                    ),
                ),
                patch.object(
                    exporter, "atomic_write_schematic", side_effect=fail_write
                ),
                modal_handler(ui, ui.wx.GenericMessageDialog, answer),
            ):
                assert ui.dialog.Close() is (outcome == "discard")
            assert has_legacy(dbfile)
            with closing(sqlite3.connect(dbfile)) as connection:
                assert connection.execute("SELECT * FROM part_info").fetchall() == (
                    LEGACY_ROWS
                )
            if outcome == "partial":
                assert written == [str(path)]
                assert second.read_bytes() == second_original
        finally:
            # Fixture teardown closes the window; leave no further save to attempt.
            path.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("decision", ["keep_open", "close_unsaved", "forced", "save"])
def test_failed_pre_write_backup_retires_assignments_only_after_explicit_save(
    window_ui: Any, variants: bool, decision: str
) -> None:
    """Backup failure cannot authorize cleanup before a complete schematic save."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    original = path.read_bytes()

    def check(ui: Any) -> None:
        if variants:
            ui.board.parts[0].SetField("LCSC", "C222")
        else:
            assert ui.dialog._apply_lcsc_assignments({"R1": "C222"}) == ["R1"]

        def answer(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic backup failed"
            assert has_legacy(dbfile)
            assert path.read_bytes() == original
            result = {
                "keep_open": ui.wx.ID_CANCEL,
                "close_unsaved": ui.wx.ID_NO,
                "save": ui.wx.ID_YES,
            }
            dialog.EndModal(result[decision])

        try:
            with patch.object(
                ui.mainwindow,
                "backup_schematics",
                side_effect=OSError("injected schematic backup failure"),
            ):
                if decision == "forced":
                    with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
                        closed = ui.dialog.Close(force=True)
                        show.assert_not_called()
                else:
                    with modal_handler(
                        ui, ui.wx.GenericMessageDialog, answer
                    ) as dialogs:
                        closed = ui.dialog.Close()
                    assert len(dialogs) == 1
            if decision == "save":
                assert '(property "LCSC" "C222"' in path.read_text(encoding="utf-8")
                assert path.with_name(path.name + "_old").read_bytes() == original
                assert not has_legacy(dbfile)
            else:
                assert path.read_bytes() == original
                assert not path.with_name(path.name + "_old").exists()
                assert has_legacy(dbfile)
                with closing(sqlite3.connect(dbfile)) as connection:
                    assert connection.execute("SELECT * FROM part_info").fetchall() == (
                        LEGACY_ROWS
                    )
            assert closed is (decision != "keep_open")
            if decision == "keep_open":
                assert ui.dialog.IsEnabled() and not ui.dialog._closing
                # The retained window can retry once backup creation succeeds.
                assert ui.dialog.Close() is True
                assert '(property "LCSC" "C222"' in path.read_text(encoding="utf-8")
                assert not has_legacy(dbfile)
        finally:
            # A failing assertion must not let teardown save over the evidence.
            path.unlink(missing_ok=True)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("decision", ["keep_open", "close_unsaved", "save"])
def test_approved_lock_retry_with_failed_backup_requires_its_own_save_decision(
    window_ui: Any, variants: bool, decision: str
) -> None:
    """Approving a schematic lock does not approve saving past a failed backup."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    original = path.read_bytes()
    safety = import_module(window_ui.mainwindow.__package__ + ".schematic_safety")
    lock = Path(safety.get_schematic_lock_path(str(path)))
    lock.write_text('{"username": "test", "hostname": "local"}')

    def check(ui: Any) -> None:
        if variants:
            ui.board.parts[0].SetField("LCSC", "C222")
        else:
            assert ui.dialog._apply_lcsc_assignments({"R1": "C222"}) == ["R1"]
        backup = ui.mainwindow.backup_schematics
        backup_attempts = []
        captions = []

        def fail_approved_backup(*args: Any, **kwargs: Any) -> Any:
            backup_attempts.append(kwargs["approved_locks"])
            if kwargs["approved_locks"]:
                assert kwargs["approved_locks"] == [str(path)]
                raise OSError("injected backup failure after lock approval")
            return backup(*args, **kwargs)

        def answer(dialog: Any) -> None:
            captions.append(dialog.GetCaption())
            assert has_legacy(dbfile)
            assert path.read_bytes() == original
            if dialog.GetCaption() == "Schematic Locked":
                dialog.EndModal(ui.wx.ID_YES)
            else:
                assert dialog.GetCaption() == "Schematic backup failed"
                result = {
                    "keep_open": ui.wx.ID_CANCEL,
                    "close_unsaved": ui.wx.ID_NO,
                    "save": ui.wx.ID_YES,
                }
                dialog.EndModal(result[decision])

        try:
            with (
                patch.object(
                    ui.mainwindow, "backup_schematics", side_effect=fail_approved_backup
                ),
                modal_handler(ui, ui.wx.GenericMessageDialog, answer),
            ):
                closed = ui.dialog.Close()
            assert captions == ["Schematic Locked", "Schematic backup failed"]
            assert backup_attempts == [(), [str(path)]]
            if decision == "save":
                assert '(property "LCSC" "C222"' in path.read_text(encoding="utf-8")
                assert path.with_name(path.name + "_old").read_bytes() == original
                assert not has_legacy(dbfile)
            else:
                assert path.read_bytes() == original
                assert not path.with_name(path.name + "_old").exists()
                assert has_legacy(dbfile)
                with closing(sqlite3.connect(dbfile)) as connection:
                    assert connection.execute("SELECT * FROM part_info").fetchall() == (
                        LEGACY_ROWS
                    )
            assert closed is (decision != "keep_open")
            if decision == "keep_open":
                assert ui.dialog.IsEnabled() and not ui.dialog._closing
        finally:
            path.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("decision", ["retry", "close", "forced"])
def test_cleanup_failure_reports_saved_schematic_and_preserves_retry(
    window_ui: Any, variants: bool, decision: str
) -> None:
    """A locked SQL database cannot turn successful schematic saving into data loss."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)

    def check(ui: Any) -> None:
        if variants:
            ui.board.parts[0].SetField("LCSC", "C222")
        else:
            assert ui.dialog._apply_lcsc_assignments({"R1": "C222"}) == ["R1"]

        def answer(dialog: Any) -> None:
            assert dialog.GetCaption() == "Legacy assignment cleanup failed"
            assert "The schematics were saved" in dialog.GetMessage()
            assert '(property "LCSC" "C222"' in path.read_text(encoding="utf-8")
            assert has_legacy(dbfile)
            dialog.EndModal(ui.wx.ID_NO if decision == "retry" else ui.wx.ID_YES)

        with closing(sqlite3.connect(dbfile)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            if decision == "forced":
                with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
                    assert ui.dialog.Close(force=True) is True
                    show.assert_not_called()
            else:
                with modal_handler(ui, ui.wx.GenericMessageDialog, answer):
                    assert ui.dialog.Close() is (decision == "close")
            blocker.rollback()
        assert has_legacy(dbfile)
        if decision == "retry":
            assert ui.dialog.IsEnabled() and not ui.dialog._closing
            assert ui.dialog.Close() is True
            assert not has_legacy(dbfile)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("ambiguous", ["invalid", "conflict"])
def test_unsafe_native_fields_preserve_schematic_while_safe_peers_save(
    window_ui: Any, variants: bool, ambiguous: str
) -> None:
    """Unsafe fields preserve recovery data without blocking another saved part."""
    dbfile = seed_project(window_ui, variants)
    peer = _SelectableFootprint(window_ui.board, "component-2", "R2")
    peer.SetField("LCSC", "C444")
    window_ui.board.parts.append(peer)
    path = associated_schematic(window_ui)
    report = window_ui.path / "jlcpcb" / "schematic-save-report.txt"
    part = window_ui.board.parts[0]
    if ambiguous == "invalid":
        part.SetField("LCSC", "invalid")
    else:
        part.SetField("LCSC", "C111")
        part.SetField("JLC_PN", "C222")

    def check(ui: Any) -> None:
        exporter = import_module(ui.mainwindow.__package__ + ".schematicexport")
        load = exporter.SchematicExport.load_schematic
        outcomes = []

        def record_export(export: Any, *args: Any, **kwargs: Any) -> Any:
            result = load(export, *args, **kwargs)
            outcomes.append(result)
            return result

        def acknowledge(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic assignments preserved"
            assert str(report) in dialog.GetMessage()
            assert "R1 [component-1]" in report.read_text(encoding="utf-8")
            dialog.EndModal(ui.wx.ID_OK)

        with (
            patch.object(exporter.SchematicExport, "load_schematic", record_export),
            modal_handler(ui, ui.wx.GenericMessageDialog, acknowledge) as dialogs,
        ):
            assert ui.dialog.Close() is True
        assert len(dialogs) == 1
        written = path.read_text(encoding="utf-8")
        assert '(property "LCSC" "C100"' in written
        assert '(property "LCSC" "C444"' in written
        assert '(field (name "LCSC") (value "C999"))' in written
        assert len(outcomes) == 1
        assert outcomes[0].saved == ("component-2",)
        assert not outcomes[0].retirement_eligible
        affected = outcomes[0].preserved + outcomes[0].skipped
        assert affected == ("component-1",)
        assert any("R1 [component-1]" in text for text in outcomes[0].diagnostics)
        report_text = report.read_text(encoding="utf-8")
        assert all(text in report_text for text in outcomes[0].diagnostics)
        assert has_legacy(dbfile)
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info").fetchall() == LEGACY_ROWS
            )
        part.SetField("LCSC", "C333")
        if ambiguous == "conflict":
            part.SetField("JLC_PN", "C333")

    def repaired(ui: Any) -> None:
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        assert '(property "LCSC" "C333"' in path.read_text(encoding="utf-8")
        assert not report.exists()
        assert not has_legacy(dbfile)
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )

    window_ui.run(check, repaired)


@pytest.mark.parametrize("variants", [False, True])
def test_pinless_symbol_assignment_is_saved_before_retirement(
    window_ui: Any, variants: bool
) -> None:
    """Pinless mounting-hole-style symbols still receive an absent part field."""
    dbfile = seed_project(window_ui, variants)
    path = associated_schematic(window_ui)
    text = path.read_text(encoding="utf-8")
    text = text.replace('    (pin "1"\n      (uuid "test-pin")\n    )\n', "")
    text = text.replace('    (property "LCSC" "C100"\n      (at 0 1 0)\n    )\n', "")
    path.write_text(text, encoding="utf-8")

    def check(ui: Any) -> None:
        assert ui.dialog.Close() is True
        assert '(property "LCSC" "C1"' in path.read_text(encoding="utf-8")
        assert not has_legacy(dbfile)

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_reused_sheet_conflicting_assignments_do_not_authorize_retirement(
    window_ui: Any, variants: bool, force: bool
) -> None:
    """Actual repeated hierarchy links preserve disagreements and save safe peers."""
    dbfile = seed_project(window_ui, variants)
    second = _SelectableFootprint(window_ui.board, "component-2", "R2")
    second.SetField("LCSC", "C222")
    peer = _SelectableFootprint(window_ui.board, "component-3", "R3")
    peer.SetField("LCSC", "C444")
    window_ui.board.parts.extend((second, peer))
    path = associated_schematic(window_ui)
    report = window_ui.path / "jlcpcb" / "schematic-save-report.txt"
    child = window_ui.path / "reused.kicad_sch"
    child_root = "00000000-0000-0000-0000-000000000002"
    symbol_uuid = "10000000-0000-0000-0000-000000000001"
    peer_uuid = "10000000-0000-0000-0000-000000000003"
    sheet_uuids = (
        "20000000-0000-0000-0000-000000000001",
        "20000000-0000-0000-0000-000000000002",
    )
    sheets = "".join(
        f'  (sheet (uuid "{sheet_uuid}") (property "Sheetfile" "reused.kicad_sch"))\n'
        for sheet_uuid in sheet_uuids
    )
    path.write_text(
        f'(kicad_sch (uuid "{SCHEMATIC_ROOT}")\n'
        + sheets
        + _schematic_symbol("R3", peer_uuid)
        + ")\n",
        encoding="utf-8",
    )
    child.write_text(
        f'(kicad_sch (uuid "{child_root}")\n'
        + _schematic_symbol("R1", symbol_uuid)
        + ")\n",
        encoding="utf-8",
    )
    for part, sheet_uuid in zip(window_ui.board.parts[:2], sheet_uuids):
        part.schematic_path = f"/{SCHEMATIC_ROOT}/{sheet_uuid}/{symbol_uuid}"
    peer.schematic_path = f"/{SCHEMATIC_ROOT}/{peer_uuid}"

    def check(ui: Any) -> None:
        exporter = import_module(ui.mainwindow.__package__ + ".schematicexport")
        load = exporter.SchematicExport.load_schematic
        outcomes = []

        def record_export(export: Any, *args: Any, **kwargs: Any) -> Any:
            result = load(export, *args, **kwargs)
            outcomes.append(result)
            return result

        def acknowledge(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic assignments preserved"
            assert str(report) in dialog.GetMessage()
            assert report.exists()
            dialog.EndModal(ui.wx.ID_OK)

        with (
            patch.object(exporter.SchematicExport, "load_schematic", record_export),
            modal_handler(ui, ui.wx.GenericMessageDialog, acknowledge) as dialogs,
        ):
            assert ui.dialog.Close(force=force) is True
        assert len(dialogs) == (0 if force else 1)
        assert '(property "LCSC" "C100"' in child.read_text(encoding="utf-8")
        assert '(property "LCSC" "C444"' in path.read_text(encoding="utf-8")
        assert len(outcomes) == 1
        assert outcomes[0].saved == ("component-3",)
        assert set(outcomes[0].skipped) == {"component-1", "component-2"}
        assert not outcomes[0].retirement_eligible
        report_text = report.read_text(encoding="utf-8")
        assert all(text in report_text for text in outcomes[0].diagnostics)
        for reference, component in (("R1", "component-1"), ("R2", "component-2")):
            assert any(
                f"{reference} [{component}]" in text for text in outcomes[0].diagnostics
            )
            assert f"{reference} [{component}]" in report_text
        assert has_legacy(dbfile)
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info").fetchall() == LEGACY_ROWS
            )

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("schematic_assignment", ["missing", "agrees"])
def test_missing_default_migrates_through_constructor_close_archive_and_reopen(
    window_ui: Any,
    variants: bool,
    schematic_assignment: str,
) -> None:
    """Real windows recover before first display, persist, then keep a later clear."""
    dbfile = seed_project(window_ui, variants)
    footprint = window_ui.board.parts[0]
    footprint.fields.pop("LCSC")
    if variants:
        footprint.AddVariant("A").SetFieldValue("LCSC", "C222")
    path = associated_schematic(window_ui)
    text = path.read_text(encoding="utf-8")
    if schematic_assignment == "missing":
        text = text.replace(
            '    (property "LCSC" "C100"\n      (at 0 1 0)\n    )\n', ""
        )
    else:
        text = text.replace('(property "LCSC" "C100"', '(property "LCSC" "C999"')
    path.write_text(text, encoding="utf-8")
    # Recovery precedes the first catalog download and must not need its data.
    window_ui.failure = "missing"

    def migrated(ui: Any) -> None:
        assert footprint.fields["LCSC"] == "C999"
        assert has_legacy(dbfile)
        if variants:
            assert ui.controller.session.snapshot.get("component-1", "").lcsc == "C999"
            assert ui.controller.session.snapshot.get("component-1", "A").lcsc == "C222"
            model = ui.controller.model
            assert (
                model.get_value(
                    model.row_for_component("component-1"), model.column_for("", "lcsc")
                )
                == "C999"
            )
        else:
            assert ui.dialog.store.get_part("R1")["lcsc"] == "C999"
            model = ui.dialog.partlist_data_model
            assert model.get_all()[0][model.columns["LCSC_COL"]] == "C999"
        assert ui.dialog.Close() is True
        assert '(property "LCSC" "C999"' in path.read_text(encoding="utf-8")
        assert not has_legacy(dbfile)
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )

    def clear_reopened(ui: Any) -> None:
        assert footprint.fields["LCSC"] == "C999" and not has_legacy(dbfile)
        if variants:
            focus(ui, variant="")
        else:
            model = ui.dialog.partlist_data_model
            ui.dialog.footprint_list.Select(model.ObjectToItem(model.get_all()[0]))
        ui.dialog.remove_lcsc_number()
        assert footprint.fields["LCSC"] == ""
        assert ui.dialog.Close() is True
        assert '(property "LCSC" ""' in path.read_text(encoding="utf-8")

    def clear_stays_cleared(ui: Any) -> None:
        assert footprint.fields["LCSC"] == "" and not has_legacy(dbfile)
        if variants:
            assert ui.controller.session.snapshot.get("component-1", "").lcsc == ""
            assert ui.controller.session.snapshot.get("component-1", "A").lcsc == "C222"
        else:
            assert ui.dialog.store.get_part("R1")["lcsc"] == ""
        assert ui.dialog.Close() is True
        assert '(property "LCSC" ""' in path.read_text(encoding="utf-8")
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )
        assert not ui.messages

    window_ui.run(migrated, clear_reopened, clear_stays_cleared)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("project", ["no_schematic", "pcb_only"])
def test_native_only_recovery_survives_discard_and_archives_after_explicit_pcb_save(
    window_ui: Any, variants: bool, project: str
) -> None:
    """Discarded native-only imports remain recoverable until a user saves the PCB."""
    dbfile = seed_project(window_ui, variants)
    part = window_ui.board.parts[0]
    part.fields.pop("LCSC")
    schematic = associated_schematic(window_ui) if project == "pcb_only" else None
    part.schematic_path = ""
    window_ui.save_board()
    pcbfile = Path(window_ui.board.GetFileName())
    initial_bytes = pcbfile.read_bytes()
    report = window_ui.path / "jlcpcb" / "schematic-save-report.txt"

    def recovered_then_discarded(ui: Any) -> None:
        assert ui.board.parts[0].fields["LCSC"] == "C999"
        assert has_legacy(dbfile)
        saved = ui.pcbnew.LoadBoard(str(pcbfile))
        assert saved is not ui.board
        assert "LCSC" not in saved.parts[0].fields
        saved.parts[0].SetField("LCSC", "C555")
        assert "LCSC" not in ui.pcbnew.LoadBoard(str(pcbfile)).parts[0].fields

        with (
            patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show,
            patch.object(ui.pcbnew, "LoadBoard", return_value=ui.board) as cached_load,
        ):
            assert ui.dialog.Close() is True
            show.assert_not_called()
            cached_load.assert_not_called()
        assert has_legacy(dbfile)
        assert not report.exists()
        assert pcbfile.read_bytes() == initial_bytes
        if schematic is not None:
            assert '(property "LCSC" "C100"' in schematic.read_text(encoding="utf-8")
        ui.reload_board()
        assert "LCSC" not in ui.board.parts[0].fields

    def recovered_then_saved(ui: Any) -> None:
        assert ui.board.parts[0].fields["LCSC"] == "C999"
        assert has_legacy(dbfile)
        ui.save_board()
        saved_bytes = pcbfile.read_bytes()
        assert saved_bytes != initial_bytes
        assert ui.pcbnew.LoadBoard(str(pcbfile)).parts[0].fields["LCSC"] == "C999"
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        assert pcbfile.read_bytes() == saved_bytes
        assert not has_legacy(dbfile)
        assert not report.exists()
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )
        ui.reload_board()

    def durable_reopen(ui: Any) -> None:
        assert ui.board.parts[0].fields["LCSC"] == "C999"
        assert not has_legacy(dbfile)
        if variants:
            assert ui.controller.session.snapshot.get("component-1", "").lcsc == "C999"
        else:
            assert ui.dialog.store.get_part("R1")["lcsc"] == "C999"
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        assert not report.exists()

    window_ui.run(recovered_then_discarded, recovered_then_saved, durable_reopen)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize("change", ["deleted", "changed_value"])
def test_unsaved_obsolete_legacy_match_is_retained_until_pcb_change_is_saved(
    window_ui: Any, variants: bool, change: str
) -> None:
    """A discarded deletion or tuple change must not lose a saved PCB's recovery."""
    dbfile = seed_project(window_ui, variants)
    part = window_ui.board.parts[0]
    part.fields.pop("LCSC")
    window_ui.save_board()
    if change == "deleted":
        window_ui.board.parts.clear()
    else:
        part.fields["Value"] = "47k"
    report = window_ui.path / "jlcpcb" / "schematic-save-report.txt"

    def unsaved(ui: Any) -> None:
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        assert has_legacy(dbfile)
        assert not report.exists()
        assert "LCSC" not in ui.pcbnew.LoadBoard(ui.board.GetFileName()).parts[0].fields
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info").fetchall() == LEGACY_ROWS
            )
        ui.save_board()
        ui.reload_board()

    def saved_obsolete(ui: Any) -> None:
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        assert not has_legacy(dbfile)
        assert not report.exists()
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )

    window_ui.run(unsaved, saved_obsolete)


@pytest.mark.parametrize("variants", [False, True])
def test_multiunit_recovery_and_reopened_assignment_updates_every_placed_unit(
    window_ui: Any, variants: bool
) -> None:
    """One native anchor recovers and saves the complete authenticated component."""
    dbfile = seed_project(window_ui, variants)
    footprint = window_ui.board.parts[0]
    footprint.fields.pop("LCSC")
    path = associated_schematic(window_ui)
    symbols = []
    for unit in (1, 2, 3):
        symbol = _schematic_symbol("R1", f"10000000-0000-0000-0000-{unit:012d}")
        symbol = symbol.replace("(unit 1)", f"(unit {unit})")
        symbol = symbol.replace(
            '(lib_id "Device:R")', f'(lib_id "Device:R")\n    (unit {unit})'
        )
        symbols.append(
            symbol.replace('(property "LCSC" "C100"', '(property "LCSC" "C999"')
        )
    path.write_text(
        f'(kicad_sch (uuid "{SCHEMATIC_ROOT}")\n'
        '(lib_symbols (symbol "Device:R" '
        '(symbol "R_1_1") (symbol "R_2_1") (symbol "R_3_1")))\n'
        + "".join(symbols)
        + ")\n",
        encoding="utf-8",
    )
    window_ui.save_board()

    def migrated(ui: Any) -> None:
        assert footprint.fields["LCSC"] == "C999"
        if variants:
            assert ui.controller.session.snapshot.get("component-1", "").lcsc == "C999"
            footprint.SetField("LCSC", "C111")
        else:
            assert ui.dialog.store.get_part("R1")["lcsc"] == "C999"
            assert ui.dialog._apply_lcsc_assignments({"R1": "C111"}) == ["R1"]
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        written = path.read_text(encoding="utf-8")
        assert written.count('(property "LCSC" "C111"') == 3
        assert written.count('(field (name "LCSC") (value "C999"))') == 3
        assert not has_legacy(dbfile)

    def reopened(ui: Any) -> None:
        footprint.SetField("LCSC", "C222")
        footprint.SetAttributes(footprint.GetAttributes() | 8)
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        written = path.read_text(encoding="utf-8")
        assert written.count('(property "LCSC" "C222"') == 3
        assert written.count("(in_bom no)") == 3
        assert written.count('(field (name "LCSC") (value "C999"))') == 3
        assert not (ui.path / "jlcpcb" / "schematic-save-report.txt").exists()
        with closing(sqlite3.connect(dbfile)) as connection:
            assert (
                connection.execute("SELECT * FROM part_info_retired").fetchall()
                == LEGACY_ROWS
            )

    window_ui.run(migrated, reopened)
