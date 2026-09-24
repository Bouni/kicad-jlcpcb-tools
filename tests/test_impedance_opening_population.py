"""Opening projects each complete document once into the native controls."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)
from .test_impedance_workbook_sizing import _many_rows

draft_api = _draft_api


def test_open_and_refresh_project_specification_summaries_once(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview selection during a row rebuild cannot repeat summary work."""
    api = draft_api
    original = api.dialog.ImpedanceDialog._populate_specifications
    projections: list[int] = []

    def project(dialog: Any) -> None:
        projections.append(len(dialog.session.config.specifications))
        original(dialog)

    monkeypatch.setattr(api.dialog.ImpedanceDialog, "_populate_specifications", project)
    dialog = _open(api, _intent(api), _board(api))
    assert projections == [1]
    projections.clear()
    assert dialog._refresh_rows()
    assert projections == [1]
    projections.clear()
    dialog.session.replace_specifications(())
    assert dialog._refresh_rows()
    assert projections == [0]
    assert dialog.specifications.GetItemCount() == 0
    assert not dialog._review_row_labels
    assert dialog.sections.GetNumberRows() == 0


def test_open_installs_rows_in_one_native_notification(
    draft_api: SimpleNamespace,
) -> None:
    """The native view is notified once for a complete representative row set."""
    api = draft_api
    board = _many_rows(api)
    dialog = _open(api, _intent(api), board)
    import wx.grid as gridlib

    table = dialog.sections
    assert table.GetNumberRows() == 40
    assert [(item.message, item.arguments) for item in table.table_messages] == [
        (gridlib.GRIDTABLE_NOTIFY_ROWS_APPENDED, (40,))
    ]
    table.table_messages.clear()
    assert dialog._refresh_rows(board)
    assert [(item.message, item.arguments) for item in table.table_messages] == [
        (gridlib.GRIDTABLE_NOTIFY_ROWS_DELETED, (0, 40)),
        (gridlib.GRIDTABLE_NOTIFY_ROWS_APPENDED, (40,)),
    ]
    assert table.GetBatchCount() == 0


def test_ordinary_rows_inherit_native_default_text_colour(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary cells need no individual native attributes or theme rewrites."""
    api = draft_api
    grid_api = importlib.import_module(f"{api.dialog.__package__}.workbook_grid")
    original = grid_api.WorkbookGrid.SetCellTextColour
    assignments: list[tuple[int, int]] = []

    def colour(table: Any, row: int, column: int, value: object) -> None:
        assignments.append((row, column))
        original(table, row, column, value)

    monkeypatch.setattr(grid_api.WorkbookGrid, "SetCellTextColour", colour)
    dialog = _open(api, _intent(api), _many_rows(api))
    assert assignments == []
    table = dialog.sections
    for row in range(table.GetNumberRows()):
        for column in range(table.GetNumberCols()):
            assert table.GetCellTextColour(row, column) == (
                table.GetDefaultCellTextColour()
            )
    table.emit(api.wx.EVT_SYS_COLOUR_CHANGED)
    api.drain()
    assert assignments == []


def test_same_row_cursor_event_does_not_request_the_preview_again(
    draft_api: SimpleNamespace,
) -> None:
    """A native cursor notification for the restored row adds no image work."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    table = dialog.sections
    table.SelectRow(1, column=4)
    selected: list[int] = []
    table._on_select = selected.append
    table._on_select_cell(SimpleNamespace(GetRow=lambda: 1, Skip=lambda: None))
    api.drain()
    assert selected == []
    assert table.GetSelectedRow() == 1
    assert table.GetGridCursorCol() == 4


def test_horizontal_cursor_normalization_restores_selection_without_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Default horizontal navigation restores a row highlight without reselecting it."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    table = dialog.sections
    table.SelectRow(1, column=4)
    selected: list[int] = []
    table._on_select = selected.append
    # wx default horizontal navigation clears the row selection before moving
    # its cursor. Reproduce those state changes before its queued normalization.
    table.ClearSelection()
    table.SetGridCursor(1, 5)
    table._normalize_cursor_selection(table._row_epoch)
    api.drain()
    assert selected == []
    assert table.GetSelectedRows() == [1]
    assert table.GetGridCursorCol() == 5


@pytest.mark.parametrize("same_row", [False, True], ids=["native-row-change", "click"])
def test_user_row_navigation_and_explicit_same_row_click_request_preview(
    draft_api: SimpleNamespace, same_row: bool
) -> None:
    """User selection still reaches the preview for changed rows or explicit clicks."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    table = dialog.sections
    table.SelectRow(0, column=4)
    selected: list[int] = []
    table._on_select = selected.append
    row = 0 if same_row else 1
    event = SimpleNamespace(GetRow=lambda: row, GetCol=lambda: 5, Skip=lambda: None)
    if same_row:
        table._on_cell_click(event)
    else:
        table._on_select_cell(event)
        table.SetGridCursor(row, 5)
    api.drain()
    assert selected == [row]
    assert table.GetSelectedRows() == [row]
    assert table.GetGridCursorCol() == 5
