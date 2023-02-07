import logging

import wx

from .events import UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled


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

        self.tented_vias_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("tented.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.tented_vias_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        tented_vias_sizer = wx.BoxSizer(wx.HORIZONTAL)
        tented_vias_sizer.Add(self.tented_vias_image, 10, wx.ALL | wx.EXPAND, 5)
        tented_vias_sizer.Add(self.tented_vias_setting, 100, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        layout = wx.GridSizer(10, 2, 0, 0)
        layout.Add(tented_vias_sizer, 0, wx.ALL | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def update_tented_vias(self, tented):
        """Update settings dialog according to the settings."""
        if tented:
            self.tented_vias_setting.SetValue(tented)
            self.tented_vias_setting.SetLabel("Tented vias")
            self.tented_vias_image.SetBitmap(
                loadBitmapScaled("tented.png", self.parent.scale_factor, static=True)
            )
        else:
            self.tented_vias_setting.SetValue(tented)
            self.tented_vias_setting.SetLabel("Untented vias")
            self.tented_vias_image.SetBitmap(
                loadBitmapScaled("untented.png", self.parent.scale_factor, static=True)
            )

    def load_settings(self):
        """Load settings and set checkboxes accordingly"""
        self.update_tented_vias(
            self.parent.settings.get("gerber", {}).get("tented_vias", True)
        )

    def update_settings(self, event):
        """Update and persist a setting that was changed."""
        upd = getattr(self, f"update_{event.GetEventObject().GetName()}")
        upd(event.GetEventObject().GetValue())
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
