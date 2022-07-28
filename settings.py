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
        self.state = {"tented_vias": False}

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

        h_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.tented_vias_setting = wx.StaticBitmap(self, wx.ID_ANY, loadBitmapScaled(
                "mdi-toggle-switch-off-outline.png",
                self.parent.scale_factor, True
            ),
                   pos=wx.DefaultPosition, size=(64,64), style=0,
                   name="tented_vias")

        self.tented_vias_setting.Bind(wx.EVT_LEFT_DOWN, self.toggle)

        h_sizer.Add(self.tented_vias_setting)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(h_sizer, 1, wx.ALL | wx.EXPAND, 5)
       
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        

    def toggle(self, e):
        sender = e.GetEventObject()
        name = sender.GetName()
        self.state[name] = not self.state[name]
        bitmaps = ("mdi-toggle-switch-off-outline.png", "mdi-toggle-switch-outline.png")
        sender.SetBitmap(loadBitmapScaled(
                bitmaps[int(self.state[name])],
                self.parent.scale_factor, True
            ))
    
    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)
