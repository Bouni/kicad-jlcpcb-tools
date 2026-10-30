"""Opening projects each complete document once into the native controls."""

from __future__ import annotations

from dataclasses import replace
import importlib
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_automatic_rows import _cached_success, _show
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    _stackup,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)
from .test_impedance_workbook_sizing import _many_rows

draft_api = _draft_api


@pytest.mark.parametrize("reorder", [False, True], ids=["same-order", "reordered"])
def test_width_completion_preserves_queued_user_preview(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, reorder: bool
) -> None:
    """A delivered solve cannot lose a click whose preview callback is still queued."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _dialog, _count=None: None,
    )
    board = _board(api)
    config = replace(_intent(api), stackup=_stackup(api))
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    previous = _cached_success(model, config.stackup, config.specifications[0], "F.Cu")
    dialog = _open(api, replace(config, width_results=(previous,)), board)
    _show(api, dialog)
    selected_id = dialog._row_ids[1]
    assert dialog.preview_pane.selected_entry.section_id != selected_id
    dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)
    assert dialog.sections.GetSelectedRow() == 1
    assert dialog.preview_pane.selected_entry.section_id != selected_id
    if reorder:
        dialog.session.set_included((selected_id,))
    delivered = replace(previous, target_width_nm=200_000)
    dialog._calculation_cancel = Event()
    dialog._finish_calculation(
        dialog._calculation_generation,
        dialog._calculation_key(dialog.session.config, board),
        (delivered,),
        "",
    )
    assert dialog.session.config.width_results == (delivered,)
    api.drain()
    api.paint(dialog.preview_pane)

    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected_id
    assert dialog.sections.GetGridCursorCol() == 4
    assert dialog.preview_pane.selected_entry.section_id == selected_id
    assert api.preview_calls[-1].section_id == selected_id
    assert dialog.preview_pane.displayed


@pytest.mark.parametrize("pending", [False, True], ids=["settled", "queued-grid"])
def test_later_image_list_selection_survives_row_refresh(
    draft_api: SimpleNamespace, pending: bool
) -> None:
    """Explicit image navigation wins over an older, still-queued grid choice."""
    api = draft_api
    board = _many_rows(api)
    dialog = _open(api, _intent(api), replace(board, traces=board.traces[:3]))
    api.drain()
    if pending:
        dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)
    selected = dialog.preview_pane.entries[2]
    dialog.image_list.Select(2)
    dialog.image_list.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    assert dialog.preview_pane.selected_entry == selected

    dialog._populate_sections()
    api.drain()

    assert dialog.preview_pane.selected_entry == selected


def test_latest_queued_grid_choice_survives_refresh(
    draft_api: SimpleNamespace,
) -> None:
    """A refresh transfers the latest gesture, never an earlier queued row number."""
    api = draft_api
    board = _many_rows(api)
    dialog = _open(api, _intent(api), replace(board, traces=board.traces[:3]))
    api.drain()
    selected_id = dialog._row_ids[2]
    for row in (1, 2):
        dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=row, column=4)

    dialog._populate_sections()
    api.drain()

    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected_id
    assert dialog.preview_pane.selected_entry.section_id == selected_id
    assert dialog.sections.GetPendingSelectedRow() == api.wx.NOT_FOUND


@pytest.mark.parametrize(
    "programmatic", [False, True], ids=["delivered", "programmatic"]
)
def test_same_image_list_selection_distinguishes_guarded_refresh(
    draft_api: SimpleNamespace, programmatic: bool
) -> None:
    """Delivered list selection wins; restoring its highlight is not new navigation."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    selected_id = dialog._row_ids[1 if programmatic else 0]
    dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)
    dialog.image_list.Select(0, False)
    if programmatic:
        dialog.preview_pane.refresh_review_labels()
    else:
        dialog.image_list.Select(0)

    dialog._populate_sections()
    api.drain()

    assert dialog.preview_pane.selected_entry.section_id == selected_id


def test_cursor_normalization_preserves_pending_user_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Restoring a native row highlight cannot discard its queued user navigation."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    selected_id = dialog._row_ids[1]
    dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)
    dialog.sections.ClearSelection()
    dialog.sections.SetGridCursor(1, 5)
    dialog.sections._normalize_cursor_selection(dialog.sections._row_epoch)

    dialog._populate_sections()
    api.drain()

    assert dialog.preview_pane.selected_entry.section_id == selected_id
    assert dialog.sections.GetSelectedRows() == [1]
    assert dialog.sections.GetGridCursorCol() == 5


def test_preferred_specification_supersedes_pending_grid_choice(
    draft_api: SimpleNamespace,
) -> None:
    """An explicit specification restore cannot be overridden by older navigation."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    preferred_id = dialog._row_ids[0]
    dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)

    dialog._populate_sections(preferred_spec_id="rf")
    api.drain()

    assert dialog._row_ids[dialog.sections.GetSelectedRow()] == preferred_id
    assert dialog.preview_pane.selected_entry.section_id == preferred_id


@pytest.mark.parametrize("empty", [False, True], ids=["target-removed", "empty"])
def test_removed_pending_row_cannot_select_its_replacement(
    draft_api: SimpleNamespace, empty: bool
) -> None:
    """Removed geometry cannot replay a queued click against a new row projection."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _intent(api), board)
    api.drain()
    removed_id = dialog._row_ids[1]
    dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)

    assert dialog._refresh_rows(
        replace(board, traces=() if empty else board.traces[:1])
    )
    api.drain()

    assert removed_id not in dialog._row_ids
    assert dialog.sections.GetPendingSelectedRow() == api.wx.NOT_FOUND
    if empty:
        assert dialog.sections.GetSelectedRow() == api.wx.NOT_FOUND
        assert dialog.preview_pane.selected_entry is None
    else:
        assert dialog.preview_pane.selected_entry.section_id == dialog._row_ids[0]


@pytest.mark.parametrize(
    "action", ["invalid-selection", "clear", "cancel", "save", "close", "destroy"]
)
def test_clearing_or_closing_revokes_queued_grid_navigation(
    draft_api: SimpleNamespace, action: str
) -> None:
    """A queued gesture cannot reopen cleared pixels or navigate a closing owner."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    previous = dialog.preview_pane.selected_entry
    dialog.sections.emit(api.grid.EVT_GRID_CELL_LEFT_CLICK, row=1, column=4)
    if action == "invalid-selection":
        dialog.sections.SelectRow(api.wx.NOT_FOUND)
    elif action == "clear":
        dialog._invalidate_visual_review()
    elif action in ("cancel", "save"):
        dialog.emit(
            api.wx.EVT_BUTTON, api.wx.ID_CANCEL if action == "cancel" else api.wx.ID_OK
        )
    else:
        dialog.emit(
            api.wx.EVT_CLOSE if action == "close" else api.wx.EVT_WINDOW_DESTROY
        )
    api.drain()

    assert dialog.sections.GetPendingSelectedRow() == api.wx.NOT_FOUND
    assert dialog.preview_pane.selected_entry == (
        None if action == "clear" else previous
    )
    assert api.preview_calls == []


def test_open_and_refresh_project_specification_summaries_once(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview selection during a row rebuild cannot repeat summary work."""
    api = draft_api
    original = api.dialog.ImpedanceDialog._populate_specifications
    projections: list[int] = []

    def project(dialog: Any, *args: Any, **kwargs: Any) -> None:
        projections.append(len(dialog.session.config.specifications))
        original(dialog, *args, **kwargs)

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


@pytest.mark.parametrize("mutation", ["replace", "append"])
def test_programmatic_row_notifications_do_not_navigate_preview(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Native cursor creation during row insertion is not an explicit row choice."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    table = dialog.sections
    values = [list(row) for row in table.GetTable().rows]
    table.DeleteAllItems()
    selected: list[int] = []
    table._on_select = selected.append
    notify = table.ProcessTableMessage

    def notify_with_cursor(message: Any) -> bool:
        """Deliver the selection event emitted by real wx when rows first appear."""
        result = notify(message)
        if table.GetNumberRows():
            table._on_select_cell(SimpleNamespace(GetRow=lambda: 0, Skip=lambda: None))
        return result

    monkeypatch.setattr(table, "ProcessTableMessage", notify_with_cursor)
    if mutation == "replace":
        table.SetItems(values)
    else:
        for row in values:
            table.AppendItem(row)
    table.SelectRow(0)
    api.drain()
    assert selected == []
    assert table.GetSelectedRow() == 0
    table._on_cell_click(
        SimpleNamespace(GetRow=lambda: 0, GetCol=lambda: 5, Skip=lambda: None)
    )
    api.drain()
    assert selected == [0]


def test_row_replacement_invalidates_queued_user_selection(
    draft_api: SimpleNamespace,
) -> None:
    """An event queued against an older row projection cannot select its replacement."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    table = dialog.sections
    values = [list(row) for row in table.GetTable().rows]
    selected: list[int] = []
    table._on_select = selected.append
    table._on_select_cell(SimpleNamespace(GetRow=lambda: 1, Skip=lambda: None))
    table.SetItems(values)
    table.SelectRow(1, column=4)
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
