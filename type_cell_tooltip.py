"""Show the library-type fee table over Type cells using wxWidgets only."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

import wx
import wx.dataview as dv

from .part_type_tooltip import create_type_fee_popup


class TypeCellTooltip:
    """Coordinate delayed Type help with the existing Standard-only row help."""

    def __init__(
        self,
        control: dv.DataViewCtrl,
        type_column: int,
        is_standard_only: Callable[[dv.DataViewItem], bool],
        set_standard_help: Callable[[bool], None],
    ) -> None:
        self.control = control
        self.type_column = type_column
        self.is_standard_only = is_standard_only
        self.set_standard_help = set_standard_help
        self._popup = None
        self._pointer = None
        self._hover_started = None
        self._failed = False
        self._stopped = False
        self._timer = wx.Timer(control)
        body = control.GetMainWindow() or control
        body.Bind(wx.EVT_MOTION, self._on_motion)
        body.Bind(wx.EVT_LEAVE_WINDOW, self._on_activity)
        for event_type in (wx.EVT_LEFT_DOWN, wx.EVT_MOUSEWHEEL, wx.EVT_KEY_DOWN):
            body.Bind(event_type, self._on_activity)
        for event_type in (
            wx.EVT_SCROLLWIN,
            wx.EVT_SIZE,
            dv.EVT_DATAVIEW_COLUMN_SORTED,
            dv.EVT_DATAVIEW_COLUMN_REORDERED,
        ):
            control.Bind(event_type, self._on_activity)
        control.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        control.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        # Some native DataView backends don't forward every mouse/scroll event.
        # Re-hit-test the current pointer so help cannot outlive its target.
        self._timer.Start(100)

    def _clear_type_help(self) -> None:
        self._pointer = None
        self._hover_started = None
        self._failed = False
        if self._popup:
            self._popup.Destroy()
        self._popup = None

    def dismiss(self) -> None:
        """Clear pending or visible help before navigation or a model reset."""
        self._clear_type_help()
        self.set_standard_help(False)

    def stop(self) -> None:
        """Stop polling and close the popup before the dialog is destroyed."""
        if not self._stopped:
            self._stopped = True
            self._timer.Stop()
            self._clear_type_help()

    def refresh(self) -> None:
        """Re-evaluate the live cell without storing a model item across updates."""
        if self._stopped:
            return
        control = self.control
        mouse = wx.GetMouseState()
        if (
            not control.IsShownOnScreen()
            or not control.IsEnabled()
            or not wx.GetTopLevelParent(control).IsActive()
            or mouse.LeftIsDown()
            or mouse.MiddleIsDown()
            or mouse.RightIsDown()
            or mouse.Aux1IsDown()
            or mouse.Aux2IsDown()
        ):
            self.dismiss()
            return
        point = wx.GetMousePosition()
        # On macOS and GTK, FindWindowAtPoint searches window creation order,
        # so a modeless dialog behind this active owner can hide valid cells.
        # Use the table body's client area and native cell hit-testing instead.
        body = control.GetMainWindow() or control
        if not body.GetClientRect().Contains(body.ScreenToClient(point)) or (
            self._popup and self._popup.GetScreenRect().Contains(point)
        ):
            self.dismiss()
            return
        item, column = control.HitTest(control.ScreenToClient(point))
        if not item or not item.IsOk():
            self.dismiss()
            return
        if column is None or column.GetModelColumn() != self.type_column:
            self._clear_type_help()
            self.set_standard_help(bool(self.is_standard_only(item)))
            return

        # Library type and Standard-only eligibility describe separate things.
        # The Type popup contains only the approved fee table.
        self.set_standard_help(False)
        pointer = (point.x, point.y)
        now = time.monotonic()
        if pointer != self._pointer:
            self._clear_type_help()
            self._pointer = pointer
            self._hover_started = now
            return
        if self._popup or self._failed:
            return
        if self._hover_started is None or now - self._hover_started < 0.6:
            return
        try:
            self._popup = create_type_fee_popup(control)
            self._popup.Position(point, wx.Size(0, control.GetCharHeight()))
            self._popup.Show()
        except Exception:
            # A help popup must not interrupt assignment or selection workflows.
            if self._popup:
                self._popup.Destroy()
            self._popup = None
            self._failed = True
            logging.getLogger(__name__).warning(
                "Unable to show library type help", exc_info=True
            )

    def _on_motion(self, event: wx.MouseEvent) -> None:
        self.refresh()
        event.Skip()

    def _on_timer(self, event: wx.TimerEvent) -> None:
        self.refresh()
        event.Skip()

    def _on_activity(self, event: wx.Event) -> None:
        self.dismiss()
        event.Skip()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() == self.control:
            self.stop()
        event.Skip()
