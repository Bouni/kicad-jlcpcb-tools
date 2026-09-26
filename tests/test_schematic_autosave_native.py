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
