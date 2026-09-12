"""Render library-type fee help using only wxWidgets controls."""

from __future__ import annotations

import wx

_TYPE_FEE_ROWS = (
    ("Basic", "None for Economic assembly;\nyes for Standard assembly"),
    ("Preferred", "None for Economic assembly;\nyes for Standard assembly"),
    ("Extended", "Yes"),
    ("Blank", "No assigned part or type\ninformation available"),
)


def create_type_fee_popup(parent: wx.Window) -> wx.PopupWindow:
    """Create a hidden, content-sized fee table without taking input focus."""
    popup = wx.PopupWindow(parent, flags=wx.BORDER_SIMPLE)
    try:
        popup.Hide()
        font = parent.GetFont()
        background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_INFOBK)
        foreground = wx.SystemSettings.GetColour(wx.SYS_COLOUR_INFOTEXT)
        popup.SetFont(font)
        popup.SetBackgroundColour(background)
        popup.SetForegroundColour(foreground)

        table = wx.FlexGridSizer(cols=2, vgap=popup.FromDIP(6), hgap=popup.FromDIP(14))
        for row_index, row in enumerate(
            (("Type", "Feeder loading fee"), *_TYPE_FEE_ROWS)
        ):
            for text in row:
                label = wx.StaticText(popup, label=text)
                label.SetFont(font.Bold() if row_index == 0 else font)
                label.SetBackgroundColour(background)
                label.SetForegroundColour(foreground)
                table.Add(label, flag=wx.ALIGN_LEFT | wx.ALIGN_TOP)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(table, flag=wx.ALL, border=popup.FromDIP(8))
        popup.SetSizerAndFit(layout)
    except Exception:
        # The caller cannot dispose of this window until the factory returns it.
        popup.Destroy()
        raise
    return popup
