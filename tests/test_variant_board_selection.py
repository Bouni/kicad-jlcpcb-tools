"""Real matrix selection synchronizes PCB UUIDs without changing variant scope."""

from typing import Any, Optional
from unittest.mock import patch

import pytest

from .native_window_support import _SelectableFootprint, _SelectedItem, window_ui
from .native_wx_support import wait_until
from .variant_matrix_native_test_support import click_native_cell
from .variant_native_support import Board

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _click(ui: Any, variant: Optional[str] = None, row: int = 0) -> Any:
    view = ui.controller.view
    col = view.model.column_for(variant, "ref" if variant is None else "lcsc")
    click_native_cell(view, ui.wx, row, col)
    wait_until(ui.wx, lambda: not view._selection_pending)
    return view.selected_target


@pytest.mark.parametrize("variant", [None, "B"])
def test_physical_selection_selects_uuid_and_preserves_variant_scope(
    window_ui: Any, variant: Optional[str]
) -> None:
    """Ref and variant cells highlight the same physical footprint through real events."""
    window_ui.board.parts[0].SetSelected()

    def check(ui: Any) -> None:
        c = ui.controller
        assert ui.board.parts[0].IsSelected()
        ui.dialog.pcbnew.Refresh.assert_not_called()
        assert c.view.selected_target is None
        other = _SelectedItem()
        other.SetSelected()
        ui.dialog.pcbnew.other_items.append(other)
        before = c.session.snapshot.source_token
        output = c.session.output_variant
        native_variant = ui.board.current
        target = _click(ui, variant)
        assert ui.board.parts[0].IsSelected() and not other.IsSelected()
        ui.dialog.pcbnew.Refresh.assert_called_once_with()
        assert target.component_id == "component-1" and target.variant == variant
        assert c.session.output_variant == output and ui.board.current == native_variant
        assert c.session.adapter.snapshot().source_token == before
        assert ui.dialog._part_selector is None

    window_ui.run(check)


def test_selection_moves_clears_and_reselects_after_external_changes(
    window_ui: Any,
) -> None:
    """Variant changes reset component batches and same-cell clicks repair PCB selection."""
    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )

    def check(ui: Any) -> None:
        view = ui.controller.view
        first, second = ui.board.parts
        view.select_components(("component-1", "component-2"), "A")
        wait_until(ui.wx, lambda: first.IsSelected() and second.IsSelected())
        assert all(
            view.is_variant_selected(row, col) == (column.variant == "A")
            for row in (0, 1)
            for col, column in enumerate(view.model.columns)
        )
        _click(ui, "B", 1)
        assert view.selected_component_ids() == ("component-2",)
        assert view.selected_target.variant == "B"
        assert not first.IsSelected() and second.IsSelected()
        second.ClearSelected()
        first.SetSelected()
        _click(ui, "B", 1)
        assert not first.IsSelected() and second.IsSelected()
        view.ClearSelection()
        wait_until(ui.wx, lambda: not first.IsSelected() and not second.IsSelected())
        assert ui.dialog.pcbnew.Refresh.call_count == 4

    window_ui.run(check)


@pytest.mark.parametrize("change", ["deleted", "renamed"])
def test_selection_uses_uuid_when_native_reference_changes(
    window_ui: Any, change: str
) -> None:
    """Stale R1 selects a renamed original, never a replacement with the same ref."""

    def check(ui: Any) -> None:
        ui.controller.timer.Stop()
        if change == "deleted":
            ui.board.parts = [_SelectableFootprint(ui.board, "replacement", "R1")]
        else:
            ui.board.parts[0].SetField("Reference", "R42")
        target = _click(ui)
        assert target.component_id == "component-1" and target.reference == "R1"
        assert ui.board.parts[0].IsSelected() is (change == "renamed")
        ui.dialog.pcbnew.Refresh.assert_called_once_with()

    window_ui.run(check)


@pytest.mark.parametrize("closed", [False, True])
def test_late_selection_cannot_touch_closed_or_replaced_board(
    window_ui: Any, closed: bool
) -> None:
    """A queued real grid callback cannot clear the closed or replacement design."""

    def check(ui: Any) -> None:
        c = ui.controller
        c.timer.Stop()
        original = ui.board.parts[0]
        original.SetSelected()
        c.view.ClearSelection()
        if closed:
            ui.dialog.Close()
        else:
            board = Board()
            board.parts = [_SelectableFootprint(board, "component-1")]
            board.parts[0].SetSelected()
            ui.dialog.pcbnew.board = board
        wait_until(ui.wx, lambda: not c.view._selection_pending)
        assert original.IsSelected() and ui.dialog.pcbnew.board.parts[0].IsSelected()
        ui.dialog.pcbnew.Refresh.assert_not_called()

    window_ui.run(check)


def test_native_selection_read_failure_preserves_previous_highlight(
    window_ui: Any,
) -> None:
    """A failed live-board read cannot clear selected flags before validation."""

    def check(ui: Any) -> None:
        c = ui.controller
        c.timer.Stop()
        footprint = ui.board.parts[0]
        footprint.SetSelected()
        with patch.object(
            ui.board,
            "GetFootprints",
            side_effect=RuntimeError("native footprint read failed"),
        ):
            c.view.ClearSelection()
            wait_until(ui.wx, lambda: not c.view._selection_pending)
        assert footprint.IsSelected() and not ui.messages
        ui.dialog.pcbnew.Refresh.assert_not_called()

    window_ui.run(check)


def test_previously_flagged_footprint_is_cleared_without_tool_selection(
    window_ui: Any,
) -> None:
    """Programmatic flags still clear if KiCad's selection tool omits those items."""

    def check(ui: Any) -> None:
        footprint = ui.board.parts[0]
        footprint.SetSelected()
        with patch.object(ui.dialog.pcbnew, "GetCurrentSelection", return_value=[]):
            ui.controller.view.ClearSelection()
            wait_until(ui.wx, lambda: not footprint.IsSelected())
        ui.dialog.pcbnew.Refresh.assert_called_once_with()

    window_ui.run(check)
