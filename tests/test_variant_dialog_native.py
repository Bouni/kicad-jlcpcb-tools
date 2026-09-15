"""Opt-in integration of real variant windows with a disposable native board.

Set KICAD_JLCPCB_NATIVE_TESTS=1 in a desktop session with compatible wx and
pcbnew modules. These checks use native constructors and dispatched wx events;
they do not simulate OS input or verify KiCad editor dirty/undo integration.
Settings, correction storage and the board stay inside pytest's temporary path.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import pytest

from .native_window_support import (
    button as _button_event,
    focus,
    modal_handler,
    native_bindings,  # noqa: F401
    select_result,
    window_ui as shared_window_ui,
)
from .native_wx_support import pump as _pump, wait_until
from .variant_matrix_native_test_support import click_native_cell
from .variant_matrix_render_test_support import observe_cell_paints

native_ui = shared_window_ui

pytestmark = [
    pytest.mark.native_kicad,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx windows require an explicitly enabled desktop session",
    ),
]


def _cell_event(
    ui: Any, frame: Any, variant: Optional[str], field: str, kind: int
) -> None:
    """Resolve disposable R1 and deliver the complete native mouse gesture."""
    controller = frame._variant_controller
    component = str(ui.board.FindFootprintByReference("R1").m_Uuid.AsString())
    row = controller.model.row_for_component(component)
    col = controller.model.column_for(variant, field)
    click_native_cell(
        controller.view,
        ui.wx,
        row,
        col,
        double=kind == ui.view.gridlib.wxEVT_GRID_CELL_LEFT_DCLICK,
    )


def test_native_selector_assignment_survives_cursor_move_and_frame_reopening(
    native_ui: Any,
) -> None:
    """A real selector retains A while the cursor moves to B; parent close is safe."""
    ui, wx = native_ui, native_ui.wx
    double_click = ui.view.gridlib.wxEVT_GRID_CELL_LEFT_DCLICK

    def assign(ui: Any) -> None:
        frame = ui.dialog
        controller = frame._variant_controller
        grid = controller.view
        assert frame._variant_mode
        assert frame.content_panel.GetParent() is frame
        assert controller.panel.GetParent() is frame.content_panel
        assert grid.GetNumberFrozenCols() == 5
        labels = controller.output_choice.GetStrings()
        assert len(labels) == len(set(labels)) == 4
        _cell_event(ui, frame, "A", "lcsc", double_click)
        selector = frame._part_selector
        assert "A" in selector.GetTitle() and "R1" in selector.GetTitle()
        grid.GoToCell(grid.GetGridCursorRow(), controller.model.column_for("B", "lcsc"))
        select_result(ui, selector)
        _button_event(wx, selector.select_part_button)
        _pump(wx)
        assert frame._part_selector is None
        footprint = ui.board.FindFootprintByReference("R1")
        assert footprint.GetFieldValueForVariant("A", "LCSC") == "C321"
        assert footprint.GetFieldValueForVariant("B", "LCSC") == "C1"
        assert footprint.GetFieldText("LCSC") == "C1"
        assert controller.session.output_variant == ""
        assert ui.pcbnew.SaveBoard(ui.board.GetFileName(), ui.board, True)
        _cell_event(ui, frame, "B", "lcsc", double_click)
        assert frame._part_selector is not None

    def reopen(ui: Any) -> None:
        frame = ui.dialog
        component = str(ui.board.FindFootprintByReference("R1").m_Uuid.AsString())
        snapshot = frame._variant_controller.session.snapshot
        assert snapshot.get(component, "A").lcsc == "C321"
        assert snapshot.get(component, "B").lcsc == "C1"
        settings = json.loads((ui.path / "settings.json").read_text())
        assert settings["partselector"]["size"]
        _cell_event(ui, frame, "B", "lcsc", double_click)
        assert frame._part_selector.assignment_context.targets[0].variant_name == "B"

    ui.run(assign, reopen)
    saved_board = ui.pcbnew.LoadBoard(ui.board.GetFileName())
    saved = saved_board.FindFootprintByReference("R1")
    assert saved.GetFieldValueForVariant("A", "LCSC") == "C321"
    assert saved.GetFieldValueForVariant("B", "LCSC") == "C1"


def test_native_row_paste_preserves_all_settings_after_save_and_reload(
    native_ui: Any,
) -> None:
    """Multi-component clipboard settings persist in KiCad's board without a database."""
    ui = native_ui
    expected = {
        "value": "22k",
        "lcsc": "C321",
        "bom": False,
        "pos": False,
        "pop": False,
    }

    def copy_and_save(ui: Any) -> None:
        controller, view = ui.controller, ui.controller.view
        components = tuple(
            str(ui.board.FindFootprintByReference(reference).m_Uuid.AsString())
            for reference in ("R1", "R2")
        )
        for component in components:
            row = controller.model.row_for_component(component)
            for field, value in expected.items():
                controller._on_edit(focus(ui, "A", field, row), value)
        view.select_components(components, "A")
        controller.dispatch_action("copy", view.selected_target)
        view.select_components(components, "B", "stock")
        controller.dispatch_action("paste", view.selected_target)
        assert not ui.messages
        assert ui.pcbnew.SaveBoard(ui.board.GetFileName(), ui.board, True)

    ui.run(copy_and_save)
    saved = ui.pcbnew.LoadBoard(ui.board.GetFileName())
    for reference in ("R1", "R2"):
        part = saved.FindFootprintByReference(reference)
        for variant in ("A", "B"):
            assert part.GetFieldValueForVariant(variant, "Value") == expected["value"]
            assert part.GetFieldValueForVariant(variant, "LCSC") == expected["lcsc"]
            assert part.GetExcludedFromBOMForVariant(variant)
            assert part.GetExcludedFromPosFilesForVariant(variant)
            assert part.GetDNPForVariant(variant)
        assert part.GetValue() == "10k" and part.GetFieldText("LCSC") == "C1"
        assert not part.IsExcludedFromBOM() and not part.IsExcludedFromPosFiles()
        assert not part.IsDNP()
    assert (
        saved.FindFootprintByReference("R3").GetFieldValueForVariant("B", "LCSC")
        == "C1"
    )
    assert not (ui.path / "jlcpcb" / "project.db").exists()


def test_native_matrix_selection_updates_only_disposable_footprint_flags(
    native_ui: Any,
) -> None:
    """Real grid selection repairs native flags without changing output scope."""
    ui, wx = native_ui, native_ui.wx
    preselected = ui.board.FindFootprintByReference("R3")
    preselected.SetSelected()

    def exercise(ui: Any) -> None:
        frame = ui.dialog
        controller = frame._variant_controller
        grid = controller.view
        first, second = (ui.board.FindFootprintByReference(ref) for ref in ("R1", "R2"))
        identities = tuple(str(fp.m_Uuid.AsString()) for fp in (first, second))
        output = controller.session.output_variant
        native_variant = ui.board.GetCurrentVariant()
        assert preselected.IsSelected(), (
            "Opening the matrix cleared native PCB selection"
        )

        def selected_ids() -> set[str]:
            return {
                str(fp.m_Uuid.AsString())
                for fp in ui.board.GetFootprints()
                if fp.IsSelected()
            }

        def click(row: int, col: int) -> None:
            click_native_cell(grid, wx, row, col)

        row = controller.model.row_for_component(identities[0])
        ref = controller.model.column_for(None, "ref")
        grid.ClearSelection()
        grid.GoToCell(row, ref)
        click(row, ref)
        assert selected_ids() == {identities[0]}
        row = controller.model.row_for_component(identities[1])
        col = controller.model.column_for("A", "value")
        grid.GoToCell(row, col)
        click(row, col)
        assert selected_ids() == {identities[1]}
        second.ClearSelected()
        first.SetSelected()
        click(row, col)
        assert selected_ids() == {identities[1]}, (
            "Same-cell click did not repair external selection"
        )
        grid.select_components(identities, "A")
        _pump(wx)
        assert selected_ids() == set(identities)
        grid._show_variant("B")
        _pump(wx)
        assert selected_ids() == set(identities)
        grid.ClearSelection()
        _pump(wx)
        assert not selected_ids()
        assert controller.session.output_variant == output
        assert ui.board.GetCurrentVariant() == native_variant

    ui.run(exercise)


def test_native_shared_correction_manager_saves_and_reopens_one_local_rule(
    native_ui: Any,
) -> None:
    """A shared matrix cell opens the real manager and reloads persisted controls."""
    ui, wx = native_ui, native_ui.wx

    def exercise(ui: Any) -> None:
        frame = ui.dialog
        controller = frame._variant_controller
        frame.library.switch_to_global_correction_database(False)
        controller.refresh()
        initial = controller.session.adapter.snapshot().source_token

        def run_manager(reopening: bool) -> None:
            def edit_and_close(manager: Any) -> None:
                assert manager.IsModal()
                assert manager.correction_snapshot.scope == "local"
                if reopening:
                    assert manager.selected_record is not None
                    assert manager.rotation.GetValue() == "90"
                    assert manager.offset_x.GetValue() == "0.5"
                    assert manager.offset_y.GetValue() == "-0.25"
                else:
                    manager.rotation.SetValue("90")
                    manager.offset_x.SetValue("0.5")
                    manager.offset_y.SetValue("-0.25")
                    assert manager.save_button.IsEnabled()
                    _button_event(wx, manager.save_button)
                rows = frame.library.read_correction_data().rows
                assert len(rows) == 1
                assert rows[0].correction.rotation == 90
                assert rows[0].correction.offset == (0.5, -0.25)
                manager.quit_dialog()

            with modal_handler(
                ui, ui.module.CorrectionManagerDialog, edit_and_close
            ) as visited:
                _cell_event(
                    ui,
                    frame,
                    None,
                    "correction",
                    ui.view.gridlib.wxEVT_GRID_CELL_LEFT_DCLICK,
                )
            assert len(visited) == 1
            wait_until(wx, lambda: not visited[0])
            assert frame._part_selector is None
            model = controller.model
            shared = model.get_value(0, model.column_for(None, "correction"))
            assert shared.signature() == (90, 0.5, -0.25)
            assert controller.session.adapter.snapshot().source_token == initial

        for reopening in (False, True):
            run_manager(reopening)

    ui.run(exercise)


def test_native_flag_edits_repaint_current_difference_styles_without_refresh(
    native_ui: Any,
) -> None:
    """The real grid repaints each native flag mutation before its polling timer."""
    ui, wx = native_ui, native_ui.wx

    def exercise(ui: Any) -> None:
        frame = ui.dialog
        controller = frame._variant_controller
        grid = controller.view
        controller.timer.Stop()

        def inspect(view: Any, row: int, col: int) -> tuple[int, int, int, bool, bool]:
            style = view.model.cell_style(row, col)
            return id(view.model), row, col, style.different, style.variant_different

        with observe_cell_paints(grid, ui.view.MatrixCellRenderer, inspect) as draws:
            footprint = ui.board.FindFootprintByReference("R1")
            getters = {
                "bom": footprint.GetExcludedFromBOMForVariant,
                "pos": footprint.GetExcludedFromPosFilesForVariant,
                "pop": footprint.GetDNPForVariant,
            }
            flags = {
                "bom": ui.pcbnew.FP_EXCLUDE_FROM_BOM,
                "pos": ui.pcbnew.FP_EXCLUDE_FROM_POS_FILES,
            }

            def click(variant: str, field: str, differences: set[str]) -> None:
                col = controller.model.column_for(variant, field)
                grid.MakeCellVisible(0, col)
                _pump(wx)
                before = controller.model.get_value(0, col)
                draws.clear()
                _cell_event(
                    ui,
                    frame,
                    variant,
                    field,
                    ui.view.gridlib.wxEVT_GRID_CELL_LEFT_CLICK,
                )
                model = controller.model
                assert model.get_value(0, col) is not before
                native_excluded = (
                    bool(getters[field](variant))
                    if variant
                    else bool(footprint.IsDNP())
                    if field == "pop"
                    else bool(footprint.GetAttributes() & flags[field])
                )
                assert native_excluded is before
                for index, column in enumerate(model.columns):
                    if column.variant == "A":
                        style = model.cell_style(0, index)
                        assert style.different is (column.key in differences)
                        assert style.variant_different is bool(differences)
                wait_until(
                    wx,
                    lambda: any(entry[:3] == (id(model), 0, col) for entry in draws),
                    timeout_ms=1000,
                )
                painted = [
                    entry[3:] for entry in draws if entry[:3] == (id(model), 0, col)
                ]
                assert painted, "The edited cell did not repaint with the current model"
                style = model.cell_style(0, col)
                assert all(
                    value == (style.different, style.variant_different)
                    for value in painted
                )

            for variant in ("A", ""):
                for field in ("bom", "pos", "pop"):
                    click(variant, field, {field})
                    click(variant, field, set())

            # KiCad stores DNP separately from BOM/POS attributes. Creating A's
            # first attribute override must preserve the inherited native DNP flag.
            footprint.DeleteVariant("A")
            footprint.SetDNP(True)
            controller.refresh()
            controller.timer.Stop()
            click("A", "bom", {"bom"})
            assert footprint.IsDNP() and footprint.GetDNPForVariant("A")
            click("A", "pop", {"bom", "pop"})
            click("A", "pop", {"bom"})
            assert ui.pcbnew.SaveBoard(ui.board.GetFileName(), ui.board, True)
            saved_board = ui.pcbnew.LoadBoard(ui.board.GetFileName())
            saved = saved_board.FindFootprintByReference("R1")
            assert saved.IsDNP() and saved.GetDNPForVariant("A")
            assert saved.GetExcludedFromBOMForVariant("A")

    ui.run(exercise)
