import logging
import os
from sys import path

import wx

# from .events import AssignPartEvent
from .helpers import PLUGIN_PATH


class RotationManagerDialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Rotations Manager",
            pos=wx.DefaultPosition,
            size=wx.Size(800, 800),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.selection_regex = None
        self.selection_correction = None

        # ---------------------------------------------------------------------
        # ---------------------------- Hotkeys --------------------------------
        # ---------------------------------------------------------------------
        # This panel is unused, but without it the acceleraors don't work (on MacOS at least)
        self.panel = wx.Panel(parent=self, id=wx.ID_ANY)
        self.panel.Fit()

        quitid = wx.NewIdRef()
        aTable = wx.AcceleratorTable(
            [
                (wx.ACCEL_CTRL, ord("W"), quitid),
                (wx.ACCEL_CTRL, ord("Q"), quitid),
                (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, quitid),
            ]
        )
        self.SetAcceleratorTable(aTable)
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)

        # ---------------------------------------------------------------------
        # ------------------------- Add/Edit inputs ---------------------------
        # ---------------------------------------------------------------------

        regex_label = wx.StaticText(self, wx.ID_ANY, "Regex", size=(150, 15))
        self.regex = wx.TextCtrl(self, wx.ID_ANY, "", wx.DefaultPosition, (200, 24))

        sizer_left = wx.BoxSizer(wx.VERTICAL)
        sizer_left.Add(regex_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer_left.Add(
            self.regex,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        correction_label = wx.StaticText(self, wx.ID_ANY, "Correction", size=(150, 15))
        self.correction = wx.TextCtrl(
            self, wx.ID_ANY, "", wx.DefaultPosition, (200, 24)
        )

        sizer_right = wx.BoxSizer(wx.VERTICAL)
        sizer_right.Add(correction_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer_right.Add(
            self.correction,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        self.regex.Bind(wx.EVT_TEXT, self.on_textfield_change)
        self.correction.Bind(wx.EVT_TEXT, self.on_textfield_change)

        add_edit_sizer = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Add / Edit")
        add_edit_sizer.Add(sizer_left, 0, wx.RIGHT, 20)
        add_edit_sizer.Add(sizer_right, 0, wx.RIGHT, 20)

        # ---------------------------------------------------------------------
        # ------------------------- Rotations list ----------------------------
        # ---------------------------------------------------------------------

        self.rotations_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_SINGLE,
        )

        regex = self.rotations_list.AppendTextColumn(
            "Regex",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=480,
            align=wx.ALIGN_LEFT,
        )
        rotation = self.rotations_list.AppendTextColumn(
            "Correction",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
        )

        self.rotations_list.SetMinSize(wx.Size(600, 500))

        self.rotations_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.on_correction_selected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(wx.Size(-1, 400))
        table_sizer.Add(self.rotations_list, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.add_button = wx.Button(
            self, wx.ID_ANY, "Add", wx.DefaultPosition, (150, -1), 0
        )
        self.edit_button = wx.Button(
            self, wx.ID_ANY, "Edit", wx.DefaultPosition, (150, -1), 0
        )
        self.delete_button = wx.Button(
            self, wx.ID_ANY, "Delete", wx.DefaultPosition, (150, -1), 0
        )

        self.add_button.Bind(wx.EVT_BUTTON, self.add_correction)
        self.edit_button.Bind(wx.EVT_BUTTON, self.update_correction)
        self.delete_button.Bind(wx.EVT_BUTTON, self.delete_correction)

        add_icon = wx.Bitmap(
            os.path.join(PLUGIN_PATH, "icons", "mdi-plus-circle-outline.png")
        )
        self.add_button.SetBitmap(add_icon)
        self.add_button.SetBitmapMargins((2, 0))

        edit_icon = wx.Bitmap(os.path.join(PLUGIN_PATH, "icons", "mdi-lead-pencil.png"))
        self.edit_button.SetBitmap(edit_icon)
        self.edit_button.SetBitmapMargins((2, 0))

        delete_icon = wx.Bitmap(
            os.path.join(PLUGIN_PATH, "icons", "mdi-trash-can-outline.png")
        )
        self.delete_button.SetBitmap(delete_icon)
        self.delete_button.SetBitmapMargins((2, 0))

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.add_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.edit_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.delete_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 3, wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------------ Sizers  ------------------------------
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(add_edit_sizer, 1, wx.ALL | wx.EXPAND, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        self.enable_toolbar_buttons(False)
        self.add_button.Enable(False)
        self.populate_rotations_list()

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side"""
        for b in [
            self.edit_button,
            self.delete_button,
        ]:
            b.Enable(bool(state))

    def populate_rotations_list(self):
        """Populate the list with the result of the search."""
        self.rotations_list.DeleteAllItems()
        for corrections in self.parent.library.get_all_correction_data():
            self.rotations_list.AppendItem([str(c) for c in corrections])

    def add_correction(self, e):
        """Add a correction to the database."""
        regex = self.regex.GetValue()
        correction = self.correction.GetValue()
        self.parent.library.insert_correction_data(regex, correction)
        self.populate_rotations_list()

    def update_correction(self, e):
        """Update a correction to the database."""
        regex = self.regex.GetValue()
        correction = self.correction.GetValue()
        if regex == self.selection_regex:
            self.parent.library.update_correction_data(regex, correction)
        else:
            self.parent.library.delete_correction_data(self.selection_regex)
            self.parent.library.insert_correction_data(regex, correction)
        self.populate_rotations_list()

    def delete_correction(self, e):
        """Delete a correction from the database."""
        item = self.rotations_list.GetSelection()
        row = self.rotations_list.ItemToRow(item)
        if row == -1:
            return
        regex = self.rotations_list.GetTextValue(row, 0)
        self.parent.library.delete_correction_data(regex)
        self.populate_rotations_list()

    def on_correction_selected(self, e):
        """Enable the toolbar buttons when a selection was made."""
        if self.rotations_list.GetSelectedItemsCount() > 0:
            self.enable_toolbar_buttons(True)
            item = self.rotations_list.GetSelection()
            row = self.rotations_list.ItemToRow(item)
            if row == -1:
                return
            self.selection_regex = self.rotations_list.GetTextValue(row, 0)
            self.selection_correction = self.rotations_list.GetTextValue(row, 1)
            self.regex.SetValue(self.selection_regex)
            self.correction.SetValue(self.selection_correction)
        else:
            self.enable_toolbar_buttons(False)

    def on_textfield_change(self, e):
        """Check if the Add button should be activated."""
        if self.regex.GetValue() and self.correction.GetValue():
            self.add_button.Enable(True)
        else:
            self.add_button.Enable(False)
