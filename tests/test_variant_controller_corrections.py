"""Real correction windows resolve and edit physical placement across variants."""

from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from .native_window_support import (
    button,
    choose_output,
    focus,
    modal_handler,
    window_ui,
)
from .native_wx_support import wait_until
from .variant_native_support import Board

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _output_correction(ui: Any) -> Any:
    """Read the current output's correction in the one physical component column."""
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
            state = _output_correction(ui)
            assert state.signature() == (45, 0.125, -0.25)
            assert state.source == "val" and state.final_angle == 135
            assert not any(
                column.variant is not None and column.key == "correction"
                for column in ui.controller.model.columns
            )
        if rule == "unavailable-database":
            assert _output_correction(ui).status == "error"
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
            assert _output_correction(ui).signature() == (30, 0.5, 0.25)
        assert ui.controller.session.adapter.snapshot() == before
        assert ui.controller.session.output_variant == output
        assert ui.controller.session.reliable
        assert ui.dialog._part_selector is None
        assert not ui.messages

    window_ui.run(check)


def test_output_corrections_follow_part_assignment_and_edit_the_selected_rule(
    window_ui: Any,
) -> None:
    """Native choices and the modal editor retain output and typed rule identity."""

    def check(ui: Any) -> None:
        footprint = ui.board.parts[0]
        variant_a = footprint.AddVariant("A")
        variant_a.SetFieldValue("LCSC", "C2")
        variant_a.SetFieldValue("Value", "22k")
        footprint.AddVariant("B").SetFieldValue("LCSC", "C3")
        fallback_row = ui.catalog.save_correction_data("^10k$", 45, (1, 2))
        other_pattern = ui.catalog.save_correction_data("^22k$", 180, (9, 9))
        ui.catalog.insert_lcsc_correction_data("C1", 0, (0, 0))
        selected_row = ui.catalog.insert_lcsc_correction_data("C2", 90, (0.25, -0.5))
        ui.catalog.insert_lcsc_correction_data("C3", 180, (0.75, 0.5))
        assert selected_row == other_pattern
        ui.controller.refresh()
        before = ui.controller.session.adapter.snapshot()

        def assert_output(
            variant: str, signature: tuple[float, float, float], angle: float
        ) -> None:
            choose_output(ui, variant)
            correction = _output_correction(ui)
            assert correction.source == "lcsc"
            assert correction.signature() == signature
            assert correction.final_angle == angle
            assert ui.controller.session.output_variant == variant
            assert ui.controller.view.output_variant == variant
            assert ui.controller.output_choice.GetSelection() == (
                {"": 0, "A": 1, "B": 2}[variant]
            )
            model = ui.controller.model
            details = model.cell_details(
                model.row_for_component("component-1"),
                model.column_for(None, "correction"),
            )
            label = variant or ui.board.GetVariantNamesForUI()[0]
            assert f"Output variant: {label}" in details
            assert ui.controller.session.adapter.snapshot() == before

        for variant, signature, angle in (
            ("", (0, 0, 0), 90),
            ("B", (180, 0.75, 0.5), 270),
            ("A", (90, 0.25, -0.5), 180),
        ):
            assert_output(variant, signature, angle)
        focus(ui, "B", "lcsc")
        assert ui.controller.session.output_variant == "A"

        def edit_selected(manager: Any) -> None:
            selected = manager.selected_record
            assert selected is not None
            assert (selected.kind, selected.rowid) == ("lcsc", selected_row)
            assert manager.lcsc_mode.GetValue()
            assert manager.regex.GetValue() == "C2"
            assert float(manager.rotation.GetValue()) == 90
            manager.rotation.SetValue("120")
            manager.offset_x.SetValue("0.5")
            manager.offset_y.SetValue("-0.25")
            button(ui.wx, manager.save_button)
            manager.quit_dialog()

        with modal_handler(
            ui, ui.module.CorrectionManagerDialog, edit_selected
        ) as managers:
            ui.controller.dispatch_action("correction", focus(ui, None, "correction"))
        assert len(managers) == 1
        wait_until(ui.wx, lambda: not managers[0])
        assert ui.controller.session.output_variant == "A"
        assert _output_correction(ui).signature() == (120, 0.5, -0.25)
        assert _output_correction(ui).final_angle == 210
        stored = ui.catalog.read_correction_data()
        by_key = {(record.kind, record.pattern): record for record in stored.rows}
        assert by_key[("footprint", "^22k$")].correction.rotation == 180
        assert by_key[("footprint", "^22k$")].correction.offset == (9, 9)
        for variant, signature, angle in (
            ("", (0, 0, 0), 90),
            ("B", (180, 0.75, 0.5), 270),
            ("A", (120, 0.5, -0.25), 210),
        ):
            assert_output(variant, signature, angle)

        # Removing only A's assignment must use Default's Value (10k), not 22k.
        variant_a.SetFieldValue("LCSC", "")
        ui.controller.refresh()
        before = ui.controller.session.adapter.snapshot()
        fallback = _output_correction(ui)
        assert fallback.source == "val"
        assert fallback.signature() == (45, 1, 2)
        assert fallback.final_angle == 135

        def inspect_fallback(manager: Any) -> None:
            selected = manager.selected_record
            assert selected is not None
            assert (selected.kind, selected.rowid) == ("footprint", fallback_row)
            assert not manager.lcsc_mode.GetValue()
            assert manager.regex.GetValue() == "^10k$"
            manager.quit_dialog()

        with modal_handler(
            ui, ui.module.CorrectionManagerDialog, inspect_fallback
        ) as managers:
            ui.controller.dispatch_action("correction", focus(ui, None, "correction"))
        assert len(managers) == 1
        wait_until(ui.wx, lambda: not managers[0])
        assert ui.controller.session.adapter.snapshot() == before
        assert ui.controller.session.output_variant == "A"
        assert ui.controller.view.output_variant == "A"
        assert ui.controller.output_choice.GetSelection() == 1
        assert ui.board.current == "A"
        assert ui.controller.session.reliable
        assert not ui.messages

    window_ui.run(check)


def test_correction_modal_error_destroys_manager_and_refreshes_output_rules(
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
        assert _output_correction(ui).signature() == (0, 0, 0)
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
