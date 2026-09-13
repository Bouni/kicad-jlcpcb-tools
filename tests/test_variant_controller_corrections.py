"""Real correction windows resolve and edit physical placement across variants."""

from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from .native_window_support import button, focus, modal_handler, window_ui
from .native_wx_support import wait_until
from .variant_native_support import Board

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _shared_correction(ui: Any) -> Any:
    """Read the physical component's one correction column after a refresh."""
    model = ui.controller.model
    return model.get_value(
        model.row_for_component("component-1"), model.column_for(None, "correction")
    )


@pytest.mark.parametrize("rule", ["matched-record", "new-rule", "unavailable-database"])
def test_correction_activation_selects_record_or_seeds_physical_footprint(
    window_ui: Any, rule: str
) -> None:
    """Real controls select a stored rule, create one, or expose unavailable data."""

    def check(ui: Any) -> None:
        rowid: Optional[int] = None
        if rule == "matched-record":
            ui.board.parts[0].AddVariant("A").SetFieldValue("Value", "22k")
            rowid = ui.catalog.save_correction_data("^10k$", 45, (0.125, -0.25))
            ui.catalog.save_correction_data("^22k$", 180, (9, 9))
        elif rule == "unavailable-database":
            Path(ui.catalog.correctionsdb_file).write_bytes(b"unreadable database")
        ui.controller.refresh()
        if rule == "matched-record":
            state = _shared_correction(ui)
            assert state.signature() == (45, 0.125, -0.25)
            assert state.source == "val" and state.final_angle == 135
            assert not any(
                column.variant is not None and column.key == "correction"
                for column in ui.controller.model.columns
            )
        if rule == "unavailable-database":
            assert _shared_correction(ui).status == "error"
            assert ui.dialog.correction_status.IsShown()
        before = ui.controller.session.adapter.snapshot()
        output = ui.controller.session.output_variant

        def inspect(manager: Any) -> None:
            assert manager.IsModal() and manager.GetParent() is ui.dialog
            selected = manager.selected_record
            assert (selected.rowid if selected else None) == rowid
            assert manager.regex.GetValue() == (
                "^10k$" if rowid is not None else "^R0603$"
            )
            if rule == "new-rule":
                manager.rotation.SetValue("30")
                manager.offset_x.SetValue("0.5")
                manager.offset_y.SetValue("0.25")
                button(ui.wx, manager.save_button)
            elif rule == "unavailable-database":
                assert manager.correction_snapshot.corrections is None
                assert "unavailable" in manager.correction_status.GetLabel()
            manager.quit_dialog()

        with modal_handler(ui, ui.module.CorrectionManagerDialog, inspect) as managers:
            ui.controller.dispatch_action("correction", focus(ui, None, "correction"))
        assert len(managers) == 1
        wait_until(ui.wx, lambda: not managers[0])
        if rule == "new-rule":
            assert _shared_correction(ui).signature() == (30, 0.5, 0.25)
        assert ui.controller.session.adapter.snapshot() == before
        assert ui.controller.session.output_variant == output
        assert ui.controller.session.reliable
        assert ui.dialog._part_selector is None
        assert not ui.messages

    window_ui.run(check)


def test_correction_modal_error_destroys_manager_and_refreshes_shared_rules(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception at the real modal boundary still rereads rules and frees controls."""

    def check(ui: Any) -> None:
        before = ui.controller.session.adapter.snapshot()
        initial_model = ui.controller.model
        manager_type = ui.module.CorrectionManagerDialog
        original = manager_type.ShowModal

        def fail_after_close(manager: Any) -> int:
            original(manager)
            raise RuntimeError("correction editor failed")

        def inspect(manager: Any) -> None:
            assert manager.IsModal()
            manager.quit_dialog()

        with monkeypatch.context() as patch:
            patch.setattr(manager_type, "ShowModal", fail_after_close)
            with modal_handler(ui, manager_type, inspect) as managers:
                ui.controller.dispatch_action(
                    "correction", focus(ui, None, "correction")
                )
        assert len(managers) == 1
        wait_until(ui.wx, lambda: not managers[0])
        assert ui.controller.model is not initial_model
        assert _shared_correction(ui).signature() == (0, 0, 0)
        assert ui.controller.session.adapter.snapshot() == before
        assert ui.controller.session.reliable
        assert ui.messages == ["correction editor failed"]

    window_ui.run(check)


@pytest.mark.parametrize(
    "invalid", ["variant", "deleted", "closed", "generating", "replaced"]
)
def test_invalid_correction_target_does_not_open_editor(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    """Stale, variant-specific and blocked targets cannot construct a modal editor."""

    def check(ui: Any) -> None:
        created: list[Any] = []
        manager_type = ui.module.CorrectionManagerDialog
        original = manager_type.__init__

        def initialize(manager: Any, parent: Any, pattern: str) -> None:
            created.append(manager)
            original(manager, parent, pattern)

        monkeypatch.setattr(manager_type, "__init__", initialize)
        target = focus(ui, None, "correction")
        if invalid == "variant":
            target = replace(target, variant="A")
        elif invalid == "deleted":
            ui.board.parts = []
        elif invalid == "closed":
            ui.controller.close()
        elif invalid == "generating":
            ui.controller.session.generating = True
        elif invalid == "replaced":
            ui.dialog.pcbnew.board = Board()
        with modal_handler(ui, manager_type, lambda manager: None) as managers:
            ui.controller.dispatch_action("correction", target)
        assert not created and not managers

    window_ui.run(check)
