"""Locate empty copper pours before continuing controlled-impedance review."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

import wx

from .copper_zone_preview import ZonePreviewRenderer
from .copper_zones import EmptyCopperZone
from .impedance.dialog_preview import PreviewCanvas


class EmptyCopperZonesDialog(wx.Dialog):
    """Show copied zone boundaries with one explicit Continue/Cancel decision."""

    def __init__(
        self,
        parent: Any,
        board: Any,
        pcbnew_module: Any,
        regions: Sequence[EmptyCopperZone],
        *,
        for_review: bool = True,
    ) -> None:
        super().__init__(
            parent,
            title="Empty copper zones",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._renderer: Optional[ZonePreviewRenderer] = None
        self._renderer_error = ""
        self._regions = tuple(regions)
        self._zoom = False
        try:
            operation = "Review" if for_review else "Export"
            consequence = (
                "Preview images will show the board without that zone's copper."
                if for_review
                else "Plotting now ships the board without that zone's copper."
            )
            self._warning_text = (
                "These copper zones contain no filled copper. " + consequence
            )
            self.warning = wx.StaticText(self, label=self._warning_text)
            self.warning.SetMinSize((1, -1))
            self.zones_list = wx.ListBox(
                self,
                choices=[
                    f"{index + 1}. {region.label}"
                    for index, region in enumerate(self._regions)
                ],
                style=wx.LB_SINGLE,
            )
            self.zones_list.SetMinSize((220, 180))
            self.preview = PreviewCanvas(
                self, lambda: None, self._preview_failed, lambda: True
            )
            self.detail = wx.TextCtrl(
                self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 60)
            )
            self.detail.SetMinSize((1, 60))
            self.preview_status = wx.StaticText(self)
            self.preview_status.SetMinSize((1, -1))
            self._preview_error = ""
            self.zoom_button = wx.Button(self, label="Zoom to zone")
            self.overview_button = wx.Button(self, label="Show whole board")
            self._caption_text = (
                "The yellow outline marks the empty zone's intended boundary. "
                "Other tracks or pads may occupy that region. The preview uses existing "
                "copper fills, which may be out of date if refilling is disabled."
            )
            self.caption = wx.StaticText(self, label=self._caption_text)
            self.caption.SetMinSize((1, -1))
            self.continue_button = wx.Button(self, wx.ID_YES, "Continue Anyway")
            self.cancel_button = wx.Button(self, wx.ID_CANCEL, f"Cancel {operation}")
            self.cancel_button.SetDefault()
            self.SetEscapeId(wx.ID_CANCEL)

            view_buttons = wx.BoxSizer(wx.HORIZONTAL)
            view_buttons.Add(self.zoom_button, 0, wx.RIGHT, 8)
            view_buttons.Add(self.overview_button, 0)
            view = wx.BoxSizer(wx.VERTICAL)
            view.Add(self.preview, 1, wx.EXPAND)
            view.Add(self.detail, 0, wx.TOP | wx.EXPAND, 8)
            view.Add(self.preview_status, 0, wx.TOP | wx.EXPAND, 6)
            view.Add(view_buttons, 0, wx.TOP, 8)
            body = wx.BoxSizer(wx.HORIZONTAL)
            body.Add(self.zones_list, 1, wx.RIGHT | wx.EXPAND, 12)
            body.Add(view, 3, wx.EXPAND)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            buttons.AddStretchSpacer()
            buttons.Add(self.continue_button, 0, wx.RIGHT, 8)
            buttons.Add(self.cancel_button, 0)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(self.warning, 0, wx.ALL | wx.EXPAND, 12)
            layout.Add(body, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
            layout.Add(self.caption, 0, wx.ALL | wx.EXPAND, 12)
            layout.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
            self.SetSizer(layout)
            self.SetMinSize((720, 480))
            self.SetSize((1000, 660))
            self.zones_list.Bind(wx.EVT_LISTBOX, self._on_selection)
            self.zoom_button.Bind(wx.EVT_BUTTON, self._on_zoom)
            self.overview_button.Bind(wx.EVT_BUTTON, self._on_overview)
            self.continue_button.Bind(wx.EVT_BUTTON, self._on_continue)
            self.cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
            self.Bind(wx.EVT_CLOSE, self._on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
            self.Bind(wx.EVT_SIZE, self._on_size)
            self._wrap_text()
            self.Layout()
            try:
                self._renderer = ZonePreviewRenderer(board, pcbnew_module)
            except Exception as error:
                self._renderer_error = str(error)
            if self._regions:
                self.zones_list.SetSelection(0)
            self._update_preview()
            self.cancel_button.SetFocus()
            self.CentreOnParent()
        except BaseException:
            self.Destroy()
            raise

    def _update_preview(self) -> None:
        """Show only the current selected region, clearing any earlier capture."""
        self.preview.clear()
        row = self.zones_list.GetSelection()
        region = self._regions[row] if 0 <= row < len(self._regions) else None
        can_zoom = (
            region is not None
            and region.bounds is not None
            and not region.geometry_error
        )
        self.zoom_button.Enable(can_zoom and not self._zoom)
        self.overview_button.Enable(can_zoom and self._zoom)
        self._preview_error = ""
        if region is None:
            self.detail.SetValue("Select a zone to see its board location.")
            self._wrap_text()
            self.Layout()
            return
        self.detail.SetValue(f"{row + 1}. {region.label}\n{region.location_text}")
        try:
            if self._renderer is None:
                raise ValueError(
                    self._renderer_error or "The board preview is unavailable."
                )
            self.preview.set_capture(self._renderer.render(region, zoom=self._zoom))
        except Exception as error:
            self._preview_failed(str(error))
        self._wrap_text()
        self.Layout()

    def _preview_failed(self, message: str) -> None:
        """Retain an actionable location message when image generation fails."""
        self.preview.clear()
        self._preview_error = f"Preview unavailable: {message}"
        self._wrap_text()
        self.Layout()

    def _wrap_text(self) -> None:
        """Fit explanations to current client width while preserving their full text."""
        width = max(200, self.GetClientSize().GetWidth() - 24)
        for control, text in (
            (self.warning, self._warning_text),
            (self.caption, self._caption_text),
        ):
            self._wrap_label(control, text, width)
        # Read the canvas's new allocation, rather than its pre-resize width.
        self.Layout()
        self._wrap_label(
            self.preview_status,
            self._preview_error,
            max(200, self.preview.GetClientSize().GetWidth()),
        )

    @staticmethod
    def _wrap_label(control: wx.StaticText, text: str, width: int) -> None:
        """Preserve explicit line breaks and enough height for the current font."""
        dc = wx.ClientDC(control)
        dc.SetFont(control.GetFont())
        # Reset wx's cached wrap width: SetLabel() alone doesn't reset it, so
        # another Wrap(width) at the same width can leave the new label unwrapped.
        control.Wrap(-1)
        control.SetLabel(text)
        # Native wrapping and drawing can round text extents differently.
        control.Wrap(max(1, width - control.FromDIP(2)))
        # Clear the previous wrapped height before measuring a wider/empty label.
        control.SetMinSize((1, -1))
        control.InvalidateBestSize()
        height = max(
            dc.GetMultiLineTextExtent(control.GetLabel()).GetHeight(),
            control.GetBestSize().GetHeight(),
        )
        control.SetMinSize((1, height))

    def _on_size(self, event: Any) -> None:
        """Reflow text without reading the board or re-rendering the selected zone."""
        self._wrap_text()
        self.Layout()
        event.Skip()

    def _on_selection(self, _event: Any) -> None:
        """Start each newly selected zone in whole-board context."""
        self._zoom = False
        self._update_preview()

    def _on_zoom(self, _event: Any) -> None:
        """Inspect the intended zone boundary more closely."""
        self._zoom = True
        self._update_preview()

    def _on_overview(self, _event: Any) -> None:
        """Restore the selected zone's location within the board."""
        self._zoom = False
        self._update_preview()

    def _finish(self, result: int) -> None:
        """Handle modal and modeless closure without changing review settings."""
        if self.IsModal():
            self.EndModal(result)
        else:
            self.SetReturnCode(result)
            self.Hide()

    def _on_continue(self, _event: Any) -> None:
        """Proceed only from the explicit Continue button."""
        self._finish(wx.ID_YES)

    def _on_cancel(self, _event: Any) -> None:
        """Return to the PCB so its empty zones can be addressed."""
        self._finish(wx.ID_CANCEL)

    def _on_close(self, _event: Any) -> None:
        """Treat the titlebar close action as cancellation."""
        self._finish(wx.ID_CANCEL)

    def _close_renderer(self) -> None:
        """Discard the diagnostic's detached board once the dialog is destroyed."""
        renderer, self._renderer = self._renderer, None
        if renderer is not None:
            renderer.close()

    def _on_destroy(self, event: Any) -> None:
        """Release native context even when a parent destroys this window."""
        if event.GetEventObject() is self:
            self._close_renderer()
        event.Skip()

    def Destroy(self) -> bool:
        """Close cached plots before wx queues native window deletion."""
        try:
            self._close_renderer()
        finally:
            destroyed = super().Destroy()
        return destroyed
