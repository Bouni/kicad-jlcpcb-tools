"""Compact, read-only workbook rows with explicit inclusion and preview actions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Optional

import wx
import wx.grid as gridlib

if "." in __package__:
    from ..grid_helpers.grid_sizing import measure_grid_content_widths
    from ..grid_helpers.grid_style import alternating_row_background
else:
    # Standalone impedance tools import without the plugin package bootstrap.
    from grid_helpers.grid_sizing import measure_grid_content_widths
    from grid_helpers.grid_style import alternating_row_background

from .width_checks import COVERED_STUB_COLOUR, WIDTH_MISMATCH_COLOUR

_COLUMN_COUNT = 15
_WIDTH_COLUMN = 7
_COMPARISON_COLUMN = 8
_TARGET_WIDTH_COLUMN = 9
_PAIR_SPACING_COLUMN = 11
_SPACING_COMPARISON_COLUMN = 12
_TARGET_SPACING_COLUMN = 13
_SPACING_CHECK_COLUMN = 14
_PADDING_DIP = 6


def workbook_cell_colour(
    values: Sequence[object], column: int
) -> Optional[tuple[int, int, int]]:
    """Mute covered stubs and highlight dimensional warnings in their own cells."""
    if values[_WIDTH_COLUMN] == "Covered stub":
        return COVERED_STUB_COLOUR
    if (
        column in (_WIDTH_COLUMN, _COMPARISON_COLUMN, _TARGET_WIDTH_COLUMN)
        and values[_COMPARISON_COLUMN] == "!="
    ):
        return WIDTH_MISMATCH_COLOUR
    if (
        column
        in (
            _PAIR_SPACING_COLUMN,
            _SPACING_COMPARISON_COLUMN,
            _TARGET_SPACING_COLUMN,
        )
        and values[_SPACING_COMPARISON_COLUMN] == "!="
    ):
        return WIDTH_MISMATCH_COLOUR
    if column == _SPACING_CHECK_COLUMN and values[column] not in (
        "",
        "Match",
        "Terminal transitions",
    ):
        return WIDTH_MISMATCH_COLOUR
    return None


class _WorkbookTable(gridlib.GridTableBase):
    """Keep the logical columns independent of available viewport width."""

    def __init__(self, labels: tuple[str, ...]) -> None:
        super().__init__()
        self.labels = labels
        self.rows: list[list[object]] = []
        self.measuring_cells = False

    def GetNumberRows(self) -> int:
        """Return all rows, including those outside the visible viewport."""
        return len(self.rows)

    def GetNumberCols(self) -> int:
        """Expose only workbook columns, without a trailing spacer column."""
        return len(self.labels)

    def GetColLabelValue(self, col: int) -> str:
        """Let native sizing measure body content without counting header padding."""
        return "" if self.measuring_cells else self.labels[col]

    def IsEmptyCell(self, row: int, col: int) -> bool:
        """Keep unchecked inclusion boxes visible to the content autosizer."""
        return col != 0 and not self.GetValue(row, col)

    def GetValue(self, row: int, col: int) -> str:
        """Expose text to wx while retaining inclusion as a logical boolean."""
        value = self.rows[row][col]
        return ("1" if value else "") if col == 0 else str(value)

    def SetValue(self, row: int, col: int, value: str) -> None:
        """Support programmatic cell updates while user editing stays disabled."""
        self.rows[row][col] = value not in ("", "0") if col == 0 else value


class _WorkbookCellRenderer(gridlib.GridCellRenderer):
    """Measure and paint exactly the same text or native checkbox artwork."""

    def Clone(self) -> _WorkbookCellRenderer:
        """Return a renderer whose native ownership is independent of the original."""
        return _WorkbookCellRenderer()

    def GetBestSize(
        self,
        grid: WorkbookGrid,
        attr: gridlib.GridCellAttr,
        dc: wx.DC,
        row: int,
        col: int,
    ) -> wx.Size:
        """Report painted content only; the caller normalizes wx's outer padding."""
        if col == 0:
            return wx.RendererNative.Get().GetCheckBoxSize(grid, wx.CONTROL_CELL)
        dc.SetFont(attr.GetFont())
        return wx.Size(*dc.GetTextExtent(grid.GetCellValue(row, col)))

    def Draw(
        self,
        grid: WorkbookGrid,
        attr: gridlib.GridCellAttr,
        dc: wx.DC,
        rect: wx.Rect,
        row: int,
        col: int,
        isSelected: bool,
    ) -> None:
        """Paint native selection text or unselected dimension/stub cues over fills."""
        background = (
            grid.GetSelectionBackground()
            if isSelected
            else grid.GetDefaultCellBackgroundColour()
        )
        if not isSelected and row % 2:
            # Preserve native dynamic/color-space information on even rows,
            # just as the variants renderer does with the original base color.
            background = wx.Colour(
                *alternating_row_background(
                    grid.GetDefaultCellBackgroundColour(),
                    grid.GetDefaultCellTextColour(),
                    row,
                )
            )
        colour = workbook_cell_colour(grid.GetTable().rows[row], col)
        foreground = (
            grid.GetSelectionForeground()
            if isSelected
            else wx.Colour(*colour)
            if colour is not None
            else grid.GetDefaultCellTextColour()
        )
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(background))
        dc.DrawRectangle(rect)
        if col == 0:
            renderer = wx.RendererNative.Get()
            size = renderer.GetCheckBoxSize(grid, wx.CONTROL_CELL)
            bounds = wx.Rect(
                rect.x + (rect.width - size.width) // 2,
                rect.y + (rect.height - size.height) // 2,
                size.width,
                size.height,
            )
            flags = wx.CONTROL_CELL
            if grid.GetToggleValue(row, col):
                flags |= wx.CONTROL_CHECKED
            renderer.DrawCheckBox(grid, dc, bounds, flags)
            return
        dc.SetFont(attr.GetFont())
        dc.SetTextForeground(foreground)
        text = grid.GetCellValue(row, col)
        height = dc.GetTextExtent(text)[1]
        with wx.DCClipper(dc, rect):
            dc.DrawText(
                text,
                rect.x + grid.FromDIP(_PADDING_DIP // 2),
                rect.y + (rect.height - height) // 2,
            )


class WorkbookGrid(gridlib.Grid):
    """Present workbook rows with content-sized columns and explicit actions."""

    def __init__(
        self,
        parent: wx.Window,
        labels: tuple[str, ...],
        *,
        on_include: Optional[Callable[[int], None]] = None,
        on_select: Optional[Callable[[int], None]] = None,
    ) -> None:
        if len(labels) != _COLUMN_COUNT:
            raise ValueError(
                f"The workbook grid requires exactly {_COLUMN_COUNT} columns."
            )
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.SetCanFocus(False)
        self._on_include = on_include
        self._on_select = on_select
        self._selected_row = wx.NOT_FOUND
        self._selecting = False
        self._row_epoch = 0
        self._pending_selection: Optional[tuple[int, int]] = None
        self._table = _WorkbookTable(labels)
        self.SetTable(self._table, takeOwnership=True, selmode=gridlib.Grid.SelectRows)
        self.SetRowLabelSize(0)
        self.SetColMinimalAcceptableWidth(0)
        self.SetTabBehaviour(gridlib.Grid.Tab_Leave)
        self.EnableEditing(False)
        self.DisableDragRowSize()
        self.DisableDragColMove()
        self.DisableDragGridSize()
        self.SetDefaultRenderer(_WorkbookCellRenderer())
        self.SetDefaultCellAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTER_VERTICAL)
        self.SetColLabelAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTER_VERTICAL)
        self.Bind(gridlib.EVT_GRID_SELECT_CELL, self._on_select_cell)
        self.Bind(gridlib.EVT_GRID_CELL_LEFT_CLICK, self._on_cell_click)
        self.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self._on_cell_double_click)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.GetGridWindow().Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_DPI_CHANGED, self._on_metrics_changed)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._on_theme_changed)

    def GetItemCount(self) -> int:
        """Read the current logical row count used by the parent dialog."""
        return len(self._table.rows)

    def GetColumnCount(self) -> int:
        """Read the number of actual workbook columns."""
        return len(self._table.labels)

    def GetSelectedRow(self) -> int:
        """Return selection independently of wx's retained cursor on an empty grid."""
        return self._selected_row

    def GetPendingSelectedRow(self) -> int:
        """Expose a real user choice that has not reached the parent's preview yet."""
        if (
            self
            and self._pending_selection == (self._selected_row, self._row_epoch)
            and 0 <= self._selected_row < self.GetItemCount()
        ):
            return self._selected_row
        return wx.NOT_FOUND

    def CancelPendingSelection(self) -> None:
        """Revoke queued navigation when its owner clears or closes the review."""
        self._pending_selection = None

    def SelectRow(
        self,
        row: int,
        addToSelected: bool = False,
        *,
        column: Optional[int] = None,
    ) -> None:
        """Restore one logical selection without invoking a user preview callback."""
        self._selecting = True
        try:
            if not 0 <= row < self.GetItemCount():
                super().ClearSelection()
                self._selected_row = wx.NOT_FOUND
                self.CancelPendingSelection()
                return
            self._selected_row = row
            super().SelectRow(row, addToSelected=False)
            col = max(0, self.GetGridCursorCol() if column is None else column)
            col = min(self.GetColumnCount() - 1, col)
            self.SetGridCursor(row, col)
        finally:
            self._selecting = False

    def DeleteAllItems(self) -> None:
        """Invalidate queued callbacks before replacing the parent-owned row projection."""
        self._row_epoch += 1
        self.CancelPendingSelection()
        self._selected_row = wx.NOT_FOUND
        count = len(self._table.rows)
        self._table.rows.clear()
        if count:
            self._notify_rows(gridlib.GRIDTABLE_NOTIFY_ROWS_DELETED, 0, count)

    def AppendItem(self, values: list[object]) -> None:
        """Append exactly one workbook row and install its persistent colour cues."""
        if len(values) != self.GetColumnCount():
            raise ValueError(
                f"Each workbook row requires exactly {_COLUMN_COUNT} values."
            )
        self._table.rows.append(list(values))
        self._notify_rows(gridlib.GRIDTABLE_NOTIFY_ROWS_APPENDED, 1)
        row = self.GetItemCount() - 1
        self._apply_row_text_colours(row)

    def SetItems(self, rows: Sequence[Sequence[object]]) -> None:
        """Replace complete rows with one deletion and one append notification."""
        values = [list(row) for row in rows]
        if any(len(row) != self.GetColumnCount() for row in values):
            raise ValueError(
                f"Each workbook row requires exactly {_COLUMN_COUNT} values."
            )
        self.DeleteAllItems()
        self._table.rows.extend(values)
        if values:
            self._notify_rows(gridlib.GRIDTABLE_NOTIFY_ROWS_APPENDED, len(values))
            for row in range(len(values)):
                self._apply_row_text_colours(row)

    def _notify_rows(self, message: int, *arguments: int) -> None:
        """Keep wx's programmatic cursor changes from queuing user navigation."""
        selecting = self._selecting
        self._selecting = True
        try:
            self.ProcessTableMessage(
                gridlib.GridTableMessage(self._table, message, *arguments)
            )
        finally:
            self._selecting = selecting

    def _apply_row_text_colours(self, row: int) -> None:
        """Keep diagnostic attributes native; ordinary text inherits the palette."""
        values = self._table.rows[row]
        for col in range(self.GetColumnCount()):
            colour = workbook_cell_colour(values, col)
            if colour is not None:
                self.SetCellTextColour(row, col, wx.Colour(*colour))

    def GetTextValue(self, row: int, col: int) -> str:
        """Read the exact text used by the grid renderer and width calculation."""
        return self._table.GetValue(row, col)

    def GetToggleValue(self, row: int, col: int) -> bool:
        """Read the inclusion value without treating its text as a dimension."""
        if col != 0:
            raise ValueError("Only the inclusion column contains a checkbox.")
        return bool(self._table.rows[row][col])

    def SetToggleValue(self, value: bool, row: int, col: int) -> None:
        """Update a logical inclusion value without triggering a second user action."""
        if col != 0:
            raise ValueError("Only the inclusion column contains a checkbox.")
        self._table.rows[row][col] = bool(value)
        self.ForceRefresh()

    def fit_columns(self) -> None:
        """Size all current content using separate header/body fonts and one inset."""
        self.BeginBatch()
        try:
            widths = measure_grid_content_widths(self)
            padding = self.FromDIP(_PADDING_DIP)
            native_padding = self.FromDIP(10)
            dc = wx.ClientDC(self)
            dc.SetFont(self.GetLabelFont())
            headers = [dc.GetTextExtent(label)[0] for label in self._table.labels]
            label_height = dc.GetTextExtent("Hg")[1]
            dc.SetFont(self.GetDefaultCellFont())
            marker_width = dc.GetTextExtent("!=")[0]
            text_height = dc.GetTextExtent("Hg")[1]
            checkbox = wx.RendererNative.Get().GetCheckBoxSize(self, wx.CONTROL_CELL)
            for col, header_width in enumerate(headers):
                content_width = max(0, widths[col] - native_padding)
                if col == 0:
                    content_width = max(content_width, checkbox.width)
                elif col in (_COMPARISON_COLUMN, _SPACING_COMPARISON_COLUMN):
                    content_width = max(content_width, marker_width)
                width = max(header_width, content_width) + padding
                self.SetColMinimalWidth(col, width)
                self.SetColSize(col, width)
            self.SetDefaultRowSize(max(text_height, checkbox.height) + padding, True)
            self.SetColLabelSize(label_height + padding)
        finally:
            self.EndBatch()

    def _on_metrics_changed(self, event: wx.Event) -> None:
        """Remeasure current native metrics after a theme or scale change settles."""
        event.Skip()
        wx.CallAfter(self._fit_if_alive)

    def _on_theme_changed(self, event: wx.SysColourChangedEvent) -> None:
        """Repaint existing rows from the current native list palette."""
        self.BeginBatch()
        try:
            self.SetDefaultCellBackgroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOX)
            )
            self.SetDefaultCellTextColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOXTEXT)
            )
            for row in range(self.GetItemCount()):
                self._apply_row_text_colours(row)
        finally:
            self.EndBatch()
        self.ForceRefresh()
        self._on_metrics_changed(event)

    def _fit_if_alive(self) -> None:
        """Avoid a queued metric update calling a destroyed native control."""
        if self:
            self.fit_columns()

    def _notify_selection(self, row: int, epoch: int) -> None:
        """Publish only the latest valid user selection after wx finishes its event."""
        if (
            self
            and self._pending_selection == (row, epoch)
            and epoch == self._row_epoch
            and row == self._selected_row
            and 0 <= row < self.GetItemCount()
        ):
            self.CancelPendingSelection()
            # wx's default Left/Right/Tab navigation clears the native row
            # selection before moving its cursor. Restore single-row selection
            # after that event, preserving the resulting column and viewport.
            self.SelectRow(row, column=self.GetGridCursorCol())
            if self._on_select is not None:
                self._on_select(row)

    def _normalize_cursor_selection(self, epoch: int) -> None:
        """Restore one row even when a native navigation key cannot move further."""
        if self and epoch == self._row_epoch:
            row = self.GetGridCursorRow()
            if 0 <= row < self.GetItemCount():
                if row == self._selected_row:
                    self.SelectRow(row, column=self.GetGridCursorCol())
                else:
                    self._selected_row = row
                    self._pending_selection = row, epoch
                    self._notify_selection(row, epoch)

    def _queue_selection(self, row: int) -> None:
        """Retain explicit intent until deferred navigation or a parent row refresh."""
        self._pending_selection = row, self._row_epoch
        wx.CallAfter(self._notify_selection, row, self._row_epoch)

    def _select_user_row(self, row: int, col: Optional[int] = None) -> None:
        """Select one row and defer preview navigation beyond the native event stack."""
        self.SelectRow(row)
        if col is not None:
            self._selecting = True
            try:
                self.SetGridCursor(row, col)
            finally:
                self._selecting = False
        self.MakeCellVisible(row, max(0, self.GetGridCursorCol()))
        self._queue_selection(row)

    def _toggle_user_row(self, row: int) -> None:
        """Commit inclusion before another action can replace the row projection.

        The callback updates the parent model immediately and must defer native
        row rebuilding until this input handler returns.
        """
        included = not self.GetToggleValue(row, 0)
        self.SetToggleValue(included, row, 0)
        if self._on_include is not None:
            self._on_include(row)

    def _on_select_cell(self, event: gridlib.GridEvent) -> None:
        """Observe native cursor navigation without turning programmatic restore into review."""
        if (
            not self._selecting
            and event.GetRow() != self._selected_row
            and 0 <= event.GetRow() < self.GetItemCount()
        ):
            self._selected_row = event.GetRow()
            self._queue_selection(self._selected_row)
        event.Skip()

    def _on_cell_click(self, event: gridlib.GridEvent) -> None:
        """Make every cell selectable; only inclusion cells mutate a row."""
        row = event.GetRow()
        if not 0 <= row < self.GetItemCount():
            event.Skip()
            return
        self.SetFocus()
        self._select_user_row(row, event.GetCol())
        if event.GetCol() == 0:
            self._toggle_user_row(row)

    def _on_cell_double_click(self, event: gridlib.GridEvent) -> None:
        """Keep the second click from opening an editor or toggling inclusion again."""
        if not 0 <= event.GetRow() < self.GetItemCount():
            event.Skip()

    def _on_key(self, event: wx.KeyEvent) -> None:
        """Support row navigation and a single Space toggle without inline editing."""
        count = self.GetItemCount()
        key = event.GetKeyCode()
        if not count:
            event.Skip()
            return
        row = self.GetSelectedRow()
        if key == wx.WXK_SPACE and row != wx.NOT_FOUND:
            self._toggle_user_row(row)
        elif key in (wx.WXK_UP, wx.WXK_DOWN):
            if row == wx.NOT_FOUND:
                row = 0
            else:
                row = min(count - 1, max(0, row + (1 if key == wx.WXK_DOWN else -1)))
            self._select_user_row(row)
        else:
            if key in (
                wx.WXK_LEFT,
                wx.WXK_RIGHT,
                wx.WXK_HOME,
                wx.WXK_END,
                wx.WXK_PAGEUP,
                wx.WXK_PAGEDOWN,
                wx.WXK_TAB,
            ) or (event.ControlDown() and key in (ord("A"), ord("a"))):
                # Default navigation can clear or extend selection without a
                # cursor-change event, including arrows at the table edges.
                wx.CallAfter(self._normalize_cursor_selection, self._row_epoch)
            event.Skip()
