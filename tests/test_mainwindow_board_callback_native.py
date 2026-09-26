"""Real wx delivery retires ordinary rows after their board context changes."""

from copy import copy
from typing import Any

import pytest

from .native_window_support import _SelectableFootprint, window_ui
from .native_wx_support import pump

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


@pytest.mark.parametrize("change", ["renamed", "variants"])
def test_queued_bom_event_disables_stale_ordinary_window(
    window_ui: Any, change: str
) -> None:
    """A queued event's native callback catches Save As or a switch to variant mode."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""

    def check(ui: Any) -> None:
        frame = ui.dialog
        assert ui.controller is None
        assert frame.partlist_data_model.data
        assert frame.footprint_list.IsEnabled()
        frame.on_bom_data_changed(None)
        assert frame._bom_recompute_scheduled
        if change == "renamed":
            ui.board.GetFileName = lambda: str(ui.path / "renamed.kicad_pcb")
        else:
            ui.board.names.append("A")

        pump(ui.wx)

        assert not frame._bom_recompute_scheduled
        assert frame._project_storage_unavailable
        assert frame.store is None
        assert frame.partlist_data_model.data == []
        assert not frame.footprint_list.IsEnabled()
        assert not frame.generate_button.IsEnabled()
        assert frame.project_storage_status.IsShown()

    ui.run(check)


def test_native_selection_event_preserves_replacement_board_selection(
    window_ui: Any,
) -> None:
    """A real DataView event cannot clear selected items belonging to a new PCB."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""

    def check(ui: Any) -> None:
        frame = ui.dialog
        frame.auto_select_alike = False
        model = frame.partlist_data_model
        item = model.ObjectToItem(model.data[0])
        frame.footprint_list.Select(item)
        pump(ui.wx)
        event = ui.wx.dataview.DataViewEvent(
            ui.wx.dataview.wxEVT_DATAVIEW_SELECTION_CHANGED,
            frame.footprint_list,
            item,
        )
        replacement = copy(ui.board)
        selected = _SelectableFootprint(replacement, "replacement-component", "R1")
        selected.SetSelected()
        replacement.parts = [selected]
        frame.pcbnew.board = replacement

        frame.footprint_list.GetEventHandler().ProcessEvent(event)
        pump(ui.wx)

        assert selected.IsSelected()
        assert frame._project_storage_unavailable
        assert frame.store is None
        assert model.data == []
        assert not frame.footprint_list.IsEnabled()

    ui.run(check)
