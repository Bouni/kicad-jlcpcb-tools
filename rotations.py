import csv
import logging
import os

import requests
import wx

from .events import (
    MessageEvent,
    PopulateFootprintListEvent,
    ResetGaugeEvent,
    UpdateGaugeEvent,
)
from .helpers import PLUGIN_PATH, HighResWxSize, loadBitmapScaled


class RotationManagerDialog(wx.Dialog):
    def __init__(self, parent, footprint):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Rotations Manager",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(800, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.selection_regex = None
        self.selection_correction = None
        self.import_legacy_corrections()

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
        # ------------------------- Add/Edit inputs ---------------------------
        # ---------------------------------------------------------------------

        regex_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Regex",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.regex = wx.TextCtrl(
            self,
            wx.ID_ANY,
            footprint,
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
        )

        sizer_left = wx.BoxSizer(wx.VERTICAL)
        sizer_left.Add(regex_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer_left.Add(
            self.regex,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        correction_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Correction",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.correction = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
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
            width=int(parent.scale_factor * 480),
            align=wx.ALIGN_LEFT,
        )
        rotation = self.rotations_list.AppendTextColumn(
            "Correction",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )

        self.rotations_list.SetMinSize(HighResWxSize(parent.window, wx.Size(600, 500)))

        self.rotations_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.on_correction_selected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(parent.window, wx.Size(-1, 400)))
        table_sizer.Add(self.rotations_list, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.save_button = wx.Button(
            self,
            wx.ID_ANY,
            "Save",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.delete_button = wx.Button(
            self,
            wx.ID_ANY,
            "Delete",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.update_button = wx.Button(
            self,
            wx.ID_ANY,
            "Update",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.import_button = wx.Button(
            self,
            wx.ID_ANY,
            "Import",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.export_button = wx.Button(
            self,
            wx.ID_ANY,
            "Export",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )

        self.save_button.Bind(wx.EVT_BUTTON, self.save_correction)
        self.delete_button.Bind(wx.EVT_BUTTON, self.delete_correction)
        self.update_button.Bind(wx.EVT_BUTTON, self.download_correction_data)
        self.import_button.Bind(wx.EVT_BUTTON, self.import_corrections_dialog)
        self.export_button.Bind(wx.EVT_BUTTON, self.export_corrections_dialog)

        self.save_button.SetBitmap(
            loadBitmapScaled(
                "mdi-content-save-outline.png",
                self.parent.scale_factor,
            )
        )
        self.save_button.SetBitmapMargins((2, 0))

        self.delete_button.SetBitmap(
            loadBitmapScaled(
                "mdi-trash-can-outline.png",
                self.parent.scale_factor,
            )
        )
        self.delete_button.SetBitmapMargins((2, 0))

        self.update_button.SetBitmap(
            loadBitmapScaled(
                "mdi-cloud-download-outline.png",
                self.parent.scale_factor,
            )
        )
        self.update_button.SetBitmapMargins((2, 0))

        self.import_button.SetBitmap(
            loadBitmapScaled(
                "mdi-database-import-outline.png",
                self.parent.scale_factor,
            )
        )
        self.import_button.SetBitmapMargins((2, 0))

        self.export_button.SetBitmap(
            loadBitmapScaled(
                "mdi-database-export-outline.png",
                self.parent.scale_factor,
            )
        )
        self.export_button.SetBitmapMargins((2, 0))

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.save_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.delete_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.update_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.import_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.export_button, 0, wx.ALL, 5)
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
        self.populate_rotations_list()

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side"""
        for b in [
            self.save_button,
            self.delete_button,
        ]:
            b.Enable(bool(state))

    def populate_rotations_list(self):
        """Populate the list with the result of the search."""
        self.rotations_list.DeleteAllItems()
        for corrections in self.parent.library.get_all_correction_data():
            self.rotations_list.AppendItem([str(c) for c in corrections])

    def save_correction(self, e):
        """Add/Update a correction in the database."""
        regex = self.regex.GetValue()
        correction = self.correction.GetValue()
        if regex == self.selection_regex:
            self.parent.library.update_correction_data(regex, correction)
            self.selection_regex = None
        elif self.selection_regex == None:
            self.parent.library.insert_correction_data(regex, correction)
        else:
            self.parent.library.delete_correction_data(self.selection_regex)
            self.parent.library.insert_correction_data(regex, correction)
            self.selection_regex = None
        self.populate_rotations_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def delete_correction(self, e):
        """Delete a correction from the database."""
        item = self.rotations_list.GetSelection()
        row = self.rotations_list.ItemToRow(item)
        if row == -1:
            return
        regex = self.rotations_list.GetTextValue(row, 0)
        self.parent.library.delete_correction_data(regex)
        self.populate_rotations_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

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
            self.selection_regex = None
            self.enable_toolbar_buttons(False)

    def on_textfield_change(self, e):
        """Check if the Add button should be activated."""
        if self.regex.GetValue() and self.correction.GetValue():
            self.enable_toolbar_buttons(True)
        else:
            self.enable_toolbar_buttons(False)

    def download_correction_data(self, e):
        """Fetch the latest rotation correction table from Matthew Lai's JLCKicadTool repo"""
        self.parent.library.create_rotation_table()
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/matthewlai/JLCKicadTools/master/jlc_kicad_tools/cpl_rotations_db.csv"
            )
            corrections = csv.reader(r.text.splitlines(), delimiter=",", quotechar='"')
            next(corrections)
            for row in corrections:
                if not self.parent.library.get_correction_data(row[0]):
                    self.parent.library.insert_correction_data(row[0], row[1])
                else:
                    self.logger.info(
                        f"Correction '{row[0]}' exists already in database with correction value {row[1]}. Leaving this one out."
                    )
        except Exception as e:
            self.logger.debug(e)
        self.populate_rotations_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def import_legacy_corrections(self):
        """Check if corrections in CSV format are found and import them into the database."""
        csv_file = os.path.join(PLUGIN_PATH, "corrections", "cpl_rotations_db.csv")
        if os.path.isfile(csv_file):
            self._import_corrections(csv_file)
            os.rename(csv_file, f"{csv_file}.backup")

    def import_corrections_dialog(self, e=None):
        """Dialog to import correctios from a CSV file."""
        with wx.FileDialog(
            self,
            "Import",
            "",
            "",
            "CSV files (*.csv)|*.csv",
            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as importFileDialog:

            if importFileDialog.ShowModal() == wx.ID_CANCEL:
                return
            path = importFileDialog.GetPath()
            self._import_corrections(path)

    def export_corrections_dialog(self, e=None):
        """Dialog to export correctios to a CSV file."""
        with wx.FileDialog(
            self,
            "Export",
            "",
            "",
            "CSV files (*.csv)|*.csv",
            wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as exportFileDialog:
            if exportFileDialog.ShowModal() == wx.ID_CANCEL:
                return
            path = exportFileDialog.GetPath()
            self._export_corrections(path)

    def _import_corrections(self, path):
        """corrections import logic"""
        if os.path.isfile(path):
            with open(path) as f:
                csvreader = csv.DictReader(f, fieldnames=("regex", "correction"))
                next(csvreader)
                for row in csvreader:
                    if self.parent.library.get_correction_data(row["regex"]):
                        self.parent.library.update_correction_data(
                            row["regex"], row["correction"]
                        )
                        self.logger.info(
                            f"Correction '{row['regex']}' exists already in database with correction value {row['correction']}. Overwrite it with local values from CSV."
                        )
                    else:
                        self.parent.library.insert_correction_data(
                            row["regex"], row["correction"]
                        )
                        self.logger.info(
                            f"Correction '{row['regex']}' with correction value {row['correction']} is added to the database from local CSV."
                        )
            self.populate_rotations_list()
            wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def _export_corrections(self, path):
        """corrections export logic"""
        with open(path, "w", newline="") as f:
            csvwriter = csv.writer(f, quotechar='"', quoting=csv.QUOTE_ALL)
            csvwriter.writerow(["Footprint pattern", "Correction"])
            for c in self.parent.library.get_all_correction_data():
                csvwriter.writerow([c[0], c[1]])
