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


class PartMapperManagerDialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Footprint Mapper",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(800, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        #self.import_legacy_corrections()

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
        # ------------------------- Mapping list ----------------------------
        # ---------------------------------------------------------------------

        self.mapping_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_SINGLE,
        )

        footprintcol = self.mapping_list.AppendTextColumn(
            "Footprint",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 250),
            align=wx.ALIGN_LEFT,
        )
        valuecol = self.mapping_list.AppendTextColumn(
            "Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 250),
            align=wx.ALIGN_LEFT,
        )
        partnumbercol = self.mapping_list.AppendTextColumn(
            "LCSC Part",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )

        self.mapping_list.SetMinSize(HighResWxSize(parent.window, wx.Size(600, 500)))

        self.mapping_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.on_mapping_selected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(parent.window, wx.Size(-1, 400)))
        table_sizer.Add(self.mapping_list, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.delete_button = wx.Button(
            self,
            wx.ID_ANY,
            "Delete",
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

        self.delete_button.Bind(wx.EVT_BUTTON, self.delete_mapping)
        self.import_button.Bind(wx.EVT_BUTTON, self.import_mappings_dialog)
        self.export_button.Bind(wx.EVT_BUTTON, self.export_mappings_dialog)


        self.delete_button.SetBitmap(self._load_icon("mdi-trash-can-outline.png"))
        self.delete_button.SetBitmapMargins((2, 0))

        self.import_button.SetBitmap(self._load_icon("mdi-database-import-outline.png"))
        self.import_button.SetBitmapMargins((2, 0))

        self.export_button.SetBitmap(self._load_icon("mdi-database-export-outline.png"))
        self.export_button.SetBitmapMargins((2, 0))

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.delete_button, 0, wx.ALL, 5)
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
        self.populate_mapping_list()

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side"""
        for b in [
            self.delete_button,
        ]:
            b.Enable(bool(state))

    def populate_mapping_list(self):
        """Populate the list with the result of the search."""
        self.mapping_list.DeleteAllItems()
        for mapping in self.parent.library.get_all_mapping_data():
            self.mapping_list.AppendItem([str(m) for m in mapping])

    def delete_mapping(self, e):
        """Delete a mapping from the database."""
        item = self.mapping_list.GetSelection()
        row = self.mapping_list.ItemToRow(item)
        if row == -1:
            return
        footprint = self.mapping_list.GetTextValue(row, 0)
        value = self.mapping_list.GetTextValue(row, 1)
        self.parent.library.delete_mapping_data(footprint,value)
        self.populate_mapping_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def on_mapping_selected(self, e):
        """Enable the toolbar buttons when a selection was made."""
        if self.mapping_list.GetSelectedItemsCount() > 0:
            self.enable_toolbar_buttons(True)
        else:
            self.enable_toolbar_buttons(False)

    def import_mapping_dialog(self, e=None):
        """Dialog to import footprint mappings from a CSV file."""
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
            self._import_mappings(path)

    def export_mappings_dialog(self, e=None):
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
            self._export_mappings(path)

    def _import_mappings(self, path):
        """mappings import logic"""
        if os.path.isfile(path):
            with open(path) as f:
                csvreader = csv.DictReader(f, fieldnames=("footprint", "value", "LCSC"))
                next(csvreader)
                for row in csvreader:
                    if self.parent.library.get_mapping_data(row["footprint"], row["value"]):
                        self.parent.library.update_mapping_data(
                            row["footprint"], row["value"], row["LCSC"]
                        )
                        self.logger.info(
                            f"Mapping '{row['footprint']}','{row['value']}' exists already in database updating with partnumber {row['LCSC']}. Overwrite it with local values from CSV."
                        )
                    else:
                        self.parent.library.insert_mapping_data(
                             row["footprint"], row["value"], row["LCSC"]
                        )
                        self.logger.info(
                            f"Mapping '{row['footprint']}','{row['value']}' with partnumber {row['LCSC']} is added to the database from local CSV."
                        )
            self.populate_mapping_list()
            wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def _export_mappings(self, path):
        """mappings export logic"""
        with open(path, "w", newline="") as f:
            csvwriter = csv.writer(f, quotechar='"', quoting=csv.QUOTE_ALL)
            csvwriter.writerow(["Footprint", "Value", "LCSC Part"])
            for c in self.parent.library.get_all_mapping_data():
                csvwriter.writerow([c[0], c[1]], c[2]])

    def _load_icon(self, filename):
        """Load an icon from a png file, handle wx difference between 6.0 and 6.99"""
        icon = loadBitmapScaled(
            os.path.join(PLUGIN_PATH, "icons", filename),
            self.parent.scale_factor,
        )
        if "6.99" in self.parent.KicadBuildVersion:
            icon = wx.BitmapBundle(icon)
        return icon
