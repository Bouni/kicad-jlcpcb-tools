"""Modeless details dialog for the BOM estimator assembly mode."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

import wx  # pylint: disable=import-error
from wx import adv  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from .bom_estimation.view import (
    build_affected_part_rows,
    build_assembly_mode_reasons,
    format_assembly_mode_status,
)
from .helpers import HighResWxSize


class WhyStandardDialog(wx.Dialog):
    """Show current assembly-mode reasons, sources, and affected parts."""

    def __init__(self, parent, decision, parts):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Assembly mode details",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(720, 560)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.parent = parent
        self.Bind(wx.EVT_CLOSE, self._on_close)

        close_id = wx.NewId()
        self.Bind(wx.EVT_MENU, self._request_close, id=close_id)
        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), close_id)
        entries[1].Set(wx.ACCEL_NORMAL, wx.WXK_ESCAPE, close_id)
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))

        self.content_panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
        self.content_panel.SetScrollRate(0, 10)
        self.content_sizer = wx.BoxSizer(wx.VERTICAL)
        self.content_panel.SetSizer(self.content_sizer)
        self._wrapped_labels = []
        self.content_panel.Bind(wx.EVT_SIZE, self._on_content_size)

        close_button = wx.Button(self, wx.ID_CLOSE, "Close")
        close_button.Bind(wx.EVT_BUTTON, self._request_close)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.AddStretchSpacer()
        actions.Add(close_button, 0)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.content_panel, 1, wx.ALL | wx.EXPAND, 10)
        layout.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        self.SetSizer(layout)
        self.SetMinSize(HighResWxSize(parent.window, wx.Size(560, 400)))
        self.update_content(decision, parts)
        self.CentreOnParent()

    def _add_text(self, text, *, bold=False):
        """Add wrapped text to the scrollable content."""
        label = wx.StaticText(self.content_panel, wx.ID_ANY, text)
        if bold:
            font = label.GetFont()
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            label.SetFont(font)
        self._wrapped_labels.append((label, text))
        self._wrap_label(label, text)
        self.content_sizer.Add(label, 0, wx.BOTTOM | wx.EXPAND, 4)

    def _wrap_label(self, label, text):
        """Wrap a label to the current content width, including after resize."""
        label.SetLabel(text)
        label.Wrap(max(240, self.content_panel.GetClientSize().GetWidth() - 20))

    def _on_content_size(self, event):
        """Re-wrap reason copy when the resizable dialog changes width."""
        for label, text in self._wrapped_labels:
            self._wrap_label(label, text)
        self.content_panel.Layout()
        self.content_panel.FitInside()
        event.Skip()

    def _add_reason(self, reason):
        """Render one bold reason heading with regular supporting copy."""
        self._add_text(str(reason["heading"]), bold=True)
        self._add_text(str(reason["body"]))
        remedy = str(reason.get("remedy") or "")
        if remedy:
            self._add_text(f"Fix: {remedy}")
        for label, url in reason.get("sources", []):
            link = adv.HyperlinkCtrl(
                self.content_panel,
                wx.ID_ANY,
                str(label),
                str(url),
            )
            self.content_sizer.Add(link, 0, wx.BOTTOM, 4)
        self.content_sizer.AddSpacer(8)

    def _add_parts_table(self, rows):
        """Render the single affected-parts table when references are involved."""
        if not rows:
            return
        count = len(rows)
        noun = "part" if count == 1 else "parts"
        self._add_text(f"Affected parts ({count} {noun})", bold=True)

        table = dv.DataViewListCtrl(
            self.content_panel,
            wx.ID_ANY,
            style=dv.DV_ROW_LINES | dv.DV_VERT_RULES,
        )
        columns = [
            ("relationship", "Relationship", 190),
            ("reference", "Ref", 65),
            ("value", "Value", 140),
            ("lcsc", "LCSC", 105),
            ("side", "Side", 55),
        ]
        for _key, label, width in columns:
            table.AppendTextColumn(
                label,
                width=HighResWxSize(self.parent.window, wx.Size(width, -1)).GetWidth(),
                mode=dv.DATAVIEW_CELL_INERT,
            )
        for row in rows:
            table.AppendItem([row[key] for key, _label, _width in columns])

        table.SetMinSize(
            HighResWxSize(
                self.parent.window,
                wx.Size(-1, 32 + min(8, count) * 24),
            )
        )
        self.content_sizer.Add(table, 0, wx.EXPAND)

    def update_content(self, decision, parts):
        """Refresh the open dialog from the latest estimator state."""
        self._wrapped_labels.clear()
        self.content_sizer.Clear(True)
        self._add_text(format_assembly_mode_status(decision), bold=True)
        self.content_sizer.AddSpacer(8)
        for reason in build_assembly_mode_reasons(decision):
            self._add_reason(reason)
        self._add_parts_table(build_affected_part_rows(decision, parts))
        self.content_panel.FitInside()
        self.content_panel.Scroll(0, 0)
        self.Layout()

    def _request_close(self, *_):
        """Route buttons and accelerators through the normal close event."""
        self.Close()

    def _on_close(self, _event):
        """Clear the parent's singleton reference and destroy the dialog."""
        if getattr(self.parent, "_why_standard_dialog", None) is self:
            self.parent._why_standard_dialog = None
        self.Destroy()
