import json
import logging
import os
from sys import path

import wx

from .events import UpdateSetting
from .helpers import PLUGIN_PATH, HighResWxSize, loadBitmapScaled


class SettingsDialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB tools settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1300, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent

        # ---------------------------------------------------------------------
        # ---------------------------- Hotkeys --------------------------------
        # ---------------------------------------------------------------------
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)

        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        accel = wx.AcceleratorTable(entries)
        self.SetAcceleratorTable(accel)

        # ---------------------------------------------------------------------
        # ------------------------- Change settings ---------------------------
        # ---------------------------------------------------------------------

        self.tented_vias_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Do not tent vias",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="tented_vias",
        )

        self.tented_vias_setting.SetToolTip(
            wx.ToolTip("Whether vias should be coverd by soldermask or not")
        )

        self.tented_vias_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        layout = wx.GridSizer(10, 2, 0, 0)
        layout.Add(self.tented_vias_setting, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def load_settings(self):
        """Load settings and set checkboxes accordingly"""
        self.tented_vias_setting.SetValue(
            self.parent.settings.get("gerber", {}).get("tented_vias", True)
        )

    def update_settings(self, event):
        """Update and persist a setting that was changed."""
        wx.PostEvent(
            self.parent,
            UpdateSetting(
                section="gerber",
                setting=event.GetEventObject().GetName(),
                value=event.GetEventObject().GetValue(),
            ),
        )

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)
