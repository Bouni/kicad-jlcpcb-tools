"""Save-on-close workflows through real wx windows and persisted schematics."""

from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import choose_output, modal_handler, window_ui
from .native_wx_support import pump, wait_until
from .test_variant_schematic_autosave import _schematic

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def associated_schematic(ui: Any) -> Path:
    """Give the exporter a real project target it can discover without a picker."""
    return _schematic(ui).rename(ui.path / "board.kicad_sch")


@pytest.mark.parametrize("variants", [False, True])
def test_native_close_saves_assignments_and_reopened_window_can_save_again(
    window_ui: Any, variants: bool
) -> None:
    """Both table modes save before closing and persist changes over reopening."""
    if not variants:
        window_ui.board.names.clear()
        window_ui.board.current = ""
    path = associated_schematic(window_ui)

    def check(ui: Any) -> None:
        original = path.read_bytes()
        before = path.read_text(encoding="utf-8")
        assignment = "C111" if '"C111"' not in before else "C222"
        frame = ui.dialog
        if variants:
            choose_output(ui, "B")
            ui.board.parts[0].SetField("LCSC", assignment)
        else:
            assert frame._apply_lcsc_assignments({"R1": assignment}) == ["R1"]
        labels = [
            frame.right_toolbar.GetToolByPos(index).GetLabel()
            for index in range(frame.right_toolbar.GetToolsCount())
        ]
        assert "Export to schematic" not in labels
        assert frame.Close() is True
        written = path.read_text(encoding="utf-8")
        assert f'(property "LCSC" "{assignment}"' in written
        assert '(field (name "LCSC") (value "C999"))' in written
        assert path.with_name(path.name + "_old").read_bytes() == original
        assert frame._closing
        if variants:
            assert ui.controller.closed and not ui.controller.timer.IsRunning()
            assert ui.controller.session.output_variant == "B"
        assert not ui.messages

    window_ui.run(check, check)


@pytest.mark.parametrize("decision", ["cancel", "save", "discard"])
def test_native_lock_dialog_controls_close_and_retry(
    window_ui: Any, decision: str
) -> None:
    """Actual modal cancellation vetoes close; explicit decisions save or discard."""
    path = associated_schematic(window_ui)
    safety = import_module(window_ui.mainwindow.__package__ + ".schematic_safety")
    lock = Path(safety.get_schematic_lock_path(str(path)))
    lock.write_text('{"username": "test", "hostname": "local"}')
    original = path.read_bytes()

    def check(ui: Any) -> None:
        frame, wx = ui.dialog, ui.wx
        ui.board.parts[0].SetField("LCSC", "C111")

        def answer(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic Locked"
            assert dialog.GetDefaultItem().GetId() == wx.ID_CANCEL
            assert path.read_bytes() == original
            assert not frame._closing and frame._saving_on_close
            # A nested ordinary close must not destroy its modal caller.
            assert frame.Close() is False
            assert not ui.controller.closed
            result = {"cancel": wx.ID_CANCEL, "save": wx.ID_YES, "discard": wx.ID_NO}
            dialog.EndModal(result[decision])

        try:
            with modal_handler(ui, wx.GenericMessageDialog, answer) as dialogs:
                closed = frame.Close()
            assert len(dialogs) == 1
            assert closed is (decision != "cancel")
            if decision == "save":
                assert '(property "LCSC" "C111"' in path.read_text(encoding="utf-8")
                assert path.with_name(path.name + "_old").read_bytes() == original
            else:
                assert path.read_bytes() == original
                assert not path.with_name(path.name + "_old").exists()
            if decision == "cancel":
                assert frame.IsEnabled() and not frame._closing
                assert ui.controller.timer.IsRunning() and not ui.controller.closed
                lock.unlink()
                ui.board.parts[0].SetField("LCSC", "C222")
                assert frame.Close() is True
                assert '(property "LCSC" "C222"' in path.read_text(encoding="utf-8")
            assert ui.controller.closed and not ui.controller.timer.IsRunning()
        finally:
            lock.unlink(missing_ok=True)

    window_ui.run(check)


def test_native_forced_close_ends_save_prompt_before_destroying_parent(
    window_ui: Any,
) -> None:
    """A forced close inside the lock prompt unwinds the actual modal loop."""
    path = associated_schematic(window_ui)
    safety = import_module(window_ui.mainwindow.__package__ + ".schematic_safety")
    lock = Path(safety.get_schematic_lock_path(str(path)))
    lock.write_text('{"username": "test", "hostname": "local"}')
    original = path.read_bytes()

    def check(ui: Any) -> None:
        def force(dialog: Any) -> None:
            assert dialog.IsModal()
            assert ui.dialog.Close(force=True) is True
            assert not dialog.IsModal()
            assert ui.dialog and not ui.controller.closed

        try:
            with modal_handler(ui, ui.wx.GenericMessageDialog, force) as dialogs:
                assert ui.dialog.Close() is True
            assert len(dialogs) == 1
            assert ui.controller.closed
            assert path.read_bytes() == original
        finally:
            lock.unlink()

    window_ui.run(check)


@pytest.mark.parametrize("generating", [False, True])
def test_native_forced_close_waits_for_modal_caller_and_generation_cleanup(
    window_ui: Any, generating: bool
) -> None:
    """Corrections Manager returns to live controls before deferred destruction."""

    def check(ui: Any) -> None:
        frame = ui.dialog
        if generating:
            ui.controller.begin_generation(())

        def force(manager: Any) -> None:
            assert manager.IsModal()
            assert frame.Close(force=True) is True
            pump(ui.wx)
            assert frame and not frame._closing
            assert not manager.IsModal()

        with modal_handler(ui, ui.mainwindow.CorrectionManagerDialog, force):
            frame.manage_corrections()
        if generating:
            pump(ui.wx)
            assert frame and not ui.controller.closed
            ui.controller.end_generation()
        wait_until(ui.wx, lambda: not frame)
        assert ui.controller.closed and not ui.controller.timer.IsRunning()

    window_ui.run(check)


def test_native_forced_close_respects_lock_without_prompting(window_ui: Any) -> None:
    """Host shutdown closes without approving locks or entering a modal loop."""
    path = associated_schematic(window_ui)
    safety = import_module(window_ui.mainwindow.__package__ + ".schematic_safety")
    lock = Path(safety.get_schematic_lock_path(str(path)))
    lock.write_text('{"username": "test", "hostname": "local"}')
    original = path.read_bytes()

    def check(ui: Any) -> None:
        try:
            with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
                assert ui.dialog.Close(force=True) is True
                show.assert_not_called()
            assert path.read_bytes() == original
            assert not path.with_name(path.name + "_old").exists()
            assert ui.controller.closed and not ui.controller.timer.IsRunning()
        finally:
            lock.unlink()

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
@pytest.mark.parametrize(
    "state", ["missing_missing", "missing_empty", "missing_code", "pcb_only"]
)
def test_routine_preservation_closes_without_warning_or_report(
    window_ui: Any, variants: bool, state: str
) -> None:
    """Normal absent assignments and unlinked hardware do not create modal noise."""
    if not variants:
        window_ui.board.names.clear()
        window_ui.board.current = ""
    path = associated_schematic(window_ui)
    part = window_ui.board.parts[0]
    if state == "pcb_only":
        part.schematic_path = ""
    else:
        part.fields.pop("LCSC")
    text = path.read_text(encoding="utf-8")
    if state == "missing_missing":
        text = text.replace(
            '    (property "LCSC" "C100"\n      (at 0 1 0)\n    )\n', ""
        )
    elif state == "missing_empty":
        text = text.replace('(property "LCSC" "C100"', '(property "LCSC" ""')
    path.write_text(text, encoding="utf-8")
    report = window_ui.path / "jlcpcb" / "schematic-save-report.txt"

    def check(ui: Any) -> None:
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()
        assert path.read_text(encoding="utf-8") == text
        assert not report.exists()
        assert not ui.messages

    window_ui.run(check, check)


@pytest.mark.parametrize("variants", [False, True])
def test_native_forced_close_unwinds_interactive_preservation_report(
    window_ui: Any,
    variants: bool,
) -> None:
    """Shutdown ends the preservation warning while its parent remains alive."""
    if not variants:
        window_ui.board.names.clear()
        window_ui.board.current = ""
    path = associated_schematic(window_ui)
    window_ui.board.parts[0].SetField("LCSC", "invalid")
    report = window_ui.path / "jlcpcb" / "schematic-save-report.txt"

    def check(ui: Any) -> None:
        frame = ui.dialog

        def force(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic assignments preserved"
            assert dialog.IsModal() and frame._saving_on_close
            assert "R1 [component-1]" in report.read_text(encoding="utf-8")
            assert frame.Close(force=True) is True
            assert not dialog.IsModal()
            assert frame and not frame._closing
            if variants:
                assert not ui.controller.closed

        with modal_handler(ui, ui.wx.GenericMessageDialog, force) as dialogs:
            assert frame.Close() is True
        assert len(dialogs) == 1 and frame._closing
        if variants:
            assert ui.controller.closed and not ui.controller.timer.IsRunning()
        assert '(property "LCSC" "C100"' in path.read_text(encoding="utf-8")
        assert "R1 [component-1]" in report.read_text(encoding="utf-8")

    window_ui.run(check)


@pytest.mark.parametrize("variants", [False, True])
def test_forced_close_in_preservation_report_defers_pending_audit_until_reopen(
    window_ui: Any, variants: bool
) -> None:
    """Shutdown cannot open a second modal or lose an undisplayed migration audit."""
    from .test_part_info_autosave_retirement import has_legacy, seed_project

    database = seed_project(window_ui, variants)
    window_ui.acknowledge_legacy_audits = False
    path = associated_schematic(window_ui)
    transient = window_ui.path / "jlcpcb" / "schematic-save-report.txt"
    helper = import_module(
        window_ui.mainwindow.__package__ + ".legacy_migration_report"
    )

    def interrupted(ui: Any) -> None:
        assert helper.load_pending_legacy_migration_reports(str(ui.path))
        ui.board.parts[0].SetField("LCSC", "invalid")

        def force(dialog: Any) -> None:
            assert dialog.GetCaption() == "Schematic assignments preserved"
            assert dialog.IsModal() and ui.dialog._saving_on_close
            assert ui.dialog.Close(force=True) is True
            assert not dialog.IsModal()

        with modal_handler(ui, ui.wx.GenericMessageDialog, force) as dialogs:
            assert ui.dialog.Close() is True
        assert len(dialogs) == 1
        assert has_legacy(database)
        assert '(property "LCSC" "C100"' in path.read_text(encoding="utf-8")
        assert transient.exists()
        pending = helper.load_pending_legacy_migration_reports(str(ui.path))
        assert len(pending) == 1 and pending[0].override_messages
        ui.board.parts[0].SetField("LCSC", "C1")

    def reopened(ui: Any) -> None:
        def acknowledge(dialog: Any) -> None:
            assert dialog.GetCaption() == "Legacy assignment migration"
            assert "C999" in dialog.GetMessage() and "C1" in dialog.GetMessage()
            assert not has_legacy(database)
            assert not transient.exists()
            dialog.EndModal(ui.wx.ID_OK)

        with modal_handler(ui, ui.wx.GenericMessageDialog, acknowledge) as dialogs:
            assert ui.dialog.Close() is True
        assert len(dialogs) == 1
        assert not helper.load_pending_legacy_migration_reports(str(ui.path))

    def acknowledged(ui: Any) -> None:
        with patch.object(ui.wx.GenericMessageDialog, "ShowModal") as show:
            assert ui.dialog.Close() is True
            show.assert_not_called()

    window_ui.run(interrupted, reopened, acknowledged)
