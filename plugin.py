import csv
import logging
import os
import re
import sys
from pathlib import Path
from zipfile import ZipFile

import wx
import wx.dataview
import wx.grid
import wx.xrc
from pcbnew import *

from .fabrication import JLCPCBFabrication
from .helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    set_exclude_from_bom,
    set_exclude_from_pos,
)
from .library import JLCPCBLibrary


class JLCPCBPlugin(ActionPlugin):
    def __init__(self):
        super(JLCPCBPlugin, self).__init__()

        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
        self.show_toolbar_button = True
        path, filename = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self.description = "Generate JLCPCB conform Gerber, Excellon, BOM and CPL files"

    def Run(self):
        dialog = JLCBCBTools(None)
        dialog.Center()
        dialog.ShowModal()
        dialog.Destroy()


class JLCBCBTools(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=u"JLCPCB Tools",
            pos=wx.DefaultPosition,
            size=wx.Size(906, 600),
            style=wx.DEFAULT_DIALOG_STYLE,
        )

        # ---------------------------------------------------------------------
        self.logbox = wx.TextCtrl(
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.logbox.SetMinSize(wx.Size(-1, 150))
        sys.stdout = self.logbox  # redirect stdout = log textbox
        self.init_logger()  # set logger to log to stdout = log textbox

        # ---------------------------------------------------------------------
        self.library = JLCPCBLibrary()
        self.fabrication = JLCPCBFabrication()
        # ---------------------------------------------------------------------

        self.SetSizeHints(wx.Size(800, -1), wx.DefaultSize)

        layout = wx.BoxSizer(wx.VERTICAL)

        # ---------------------------------------------------------------------
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.generate_button = wx.Button(
            self,
            wx.ID_ANY,
            u"Generate fabrication files",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )
        self.generate_button.Bind(wx.EVT_BUTTON, self.generate_fabrication_data)
        button_sizer.Add(self.generate_button, 0, wx.ALL, 5)

        # ---------------------------------------------------------------------

        layout.Add(button_sizer, 1, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(wx.Size(-1, 400))
        self.footprint_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_MULTIPLE,
        )
        self.footprint_list.SetMinSize(wx.Size(750, 400))
        self.reference = self.footprint_list.AppendTextColumn(
            u"Reference",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.value = self.footprint_list.AppendTextColumn(
            u"Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=200,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.footprint = self.footprint_list.AppendTextColumn(
            u"Footprint",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=200,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.lcsc = self.footprint_list.AppendTextColumn(
            u"LCSC",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.bom = self.footprint_list.AppendTextColumn(
            u"BOM",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.cpl = self.footprint_list.AppendTextColumn(
            u"CPL",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.get_footprints()
        self.populate_footprint_list()
        table_sizer.Add(self.footprint_list, 0, wx.ALL | wx.EXPAND, 5)
        # ---------------------------------------------------------------------
        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        self.select_part_button = wx.Button(
            self, wx.ID_ANY, u"Select part", wx.DefaultPosition, (150, -1), 0
        )
        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        self.toggle_bom_button = wx.Button(
            self, wx.ID_ANY, u"Toggle BOM", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_bom_button.Bind(wx.EVT_BUTTON, self.toogle_bom)
        tool_sizer.Add(self.toggle_bom_button, 0, wx.ALL, 5)
        self.toggle_cpl_button = wx.Button(
            self, wx.ID_ANY, u"Toggle CPL", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_cpl_button.Bind(wx.EVT_BUTTON, self.toogle_cpl)
        tool_sizer.Add(self.toggle_cpl_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 1, wx.EXPAND, 5)
        # ---------------------------------------------------------------------

        layout.Add(table_sizer, 1, wx.ALL | wx.EXPAND, 5)

        layout.Add(self.logbox, 0, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

    def get_footprints(self):
        """get all footprints from the board"""
        self.board = GetBoard()
        self.footprints = sorted(
            self.board.GetFootprints(),
            key=lambda fp: (
                str(fp.GetFPID().GetLibItemName()),
                int(re.search("\d+", fp.GetReference())[0]),
            ),
        )

    def populate_footprint_list(self):
        """Populate/Refresh list of footprints."""
        self.footprint_list.DeleteAllItems()
        for fp in self.footprints:
            self.footprint_list.AppendItem(
                [
                    str(fp.GetReference()),
                    str(fp.GetValue()),
                    str(fp.GetFPID().GetLibItemName()),
                    str(
                        self.fabrication.parts.get(str(fp.GetReference()), {}).get(
                            "lcsc", ""
                        )
                    ),
                    "No" if get_exclude_from_bom(fp) else "Yes",
                    "No" if get_exclude_from_pos(fp) else "Yes",
                ]
            )

    def get_footprint_by_ref(self, ref):
        """get a footprint from the list of footprints by its Reference."""
        for fp in self.footprints:
            if str(fp.GetReference()) == ref:
                return fp

    def toogle_bom(self, e):
        """Toggle the exclude from BOM attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            row = self.footprint_list.ItemToRow(item)
            ref = self.footprint_list.GetTextValue(row, 0)
            fp = self.get_footprint_by_ref(ref)
            set_exclude_from_bom(fp)
        self.populate_footprint_list()

    def toogle_cpl(self, e):
        """Toggle the exclude from POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            row = self.footprint_list.ItemToRow(item)
            ref = self.footprint_list.GetTextValue(row, 0)
            fp = self.get_footprint_by_ref(ref)
            set_exclude_from_pos(fp)
        self.populate_footprint_list()

    def select_part(self, e):
        """Select and assign a LCSC Part number to a footprint via modal dialog."""
        dialog = PartSelectorDialog(self)
        result = dialog.ShowModal()
        if result == wx.ID_OK:
            for item in self.footprint_list.GetSelections():
                row = self.footprint_list.ItemToRow(item)
                ref = self.footprint_list.GetTextValue(row, 0)
                fp = self.get_footprint_by_ref(ref)
                self.fabrication.parts[ref] = {
                    "lcsc": str(dialog.selection),
                    "source": "csv",
                }
            self.populate_footprint_list()
            self.fabrication.save_part_assignments()
        dialog.Destroy()

    def generate_fabrication_data(self, e):
        """Generate Fabrication data."""
        self.fabrication.generate_geber()
        self.fabrication.generate_excellon()
        self.fabrication.zip_gerber_excellon()
        self.fabrication.generate_cpl()
        self.fabrication.generate_bom()

    def init_logger(self):
        """Initialize logger to log into textbox"""
        # Remove all handlers associated with the root logger object.
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y.%m.%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
        self.logger = logging.getLogger(__name__)

    def __del__(self):
        pass


class PartSelectorDialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=u"JLCPCB Library",
            pos=wx.DefaultPosition,
            size=wx.Size(1206, 600),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self.logger = logging.getLogger(__name__)
        self.library = parent.library

        self.SetSizeHints(wx.Size(1200, -1), wx.DefaultSize)

        layout = wx.BoxSizer(wx.VERTICAL)

        # ---------------------------------------------------------------------
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.keyword = wx.TextCtrl(
            self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, (300, -1), 0
        )
        button_sizer.Add(self.keyword, 0, wx.ALL, 5)
        self.basic_checkbox = wx.CheckBox(
            self, wx.ID_ANY, u"Basic", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.basic_checkbox.SetValue(True)
        button_sizer.Add(self.basic_checkbox, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        self.extended_checkbox = wx.CheckBox(
            self, wx.ID_ANY, u"Extended", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.extended_checkbox.SetValue(True)
        button_sizer.Add(self.extended_checkbox, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        self.search_button = wx.Button(
            self,
            wx.ID_ANY,
            u"Search",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )
        self.search_button.Bind(wx.EVT_BUTTON, self.search)
        button_sizer.Add(self.search_button, 0, wx.ALL, 5)

        button_sizer.Add((0, 0), 1, wx.EXPAND, 5)

        self.download_button = wx.Button(
            self, wx.ID_ANY, u"Update library", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.download_button.Bind(wx.EVT_BUTTON, self.download)
        button_sizer.Add(self.download_button, 0, wx.ALL, 5)
        wx.Gauge()
        self.download_gauge = wx.Gauge(
            self, wx.ID_ANY, 100, wx.DefaultPosition, (100, -1), wx.GA_HORIZONTAL
        )
        self.download_gauge.SetValue(0)
        self.download_gauge.SetMinSize(wx.Size(-1, 24))
        button_sizer.Add(self.download_gauge, 0, wx.ALL, 5)
        # ---------------------------------------------------------------------

        layout.Add(button_sizer, 1, wx.ALL, 5)

        # ---------------------------------------------------------------------
        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(wx.Size(-1, 400))
        self.part_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_SINGLE,
        )
        self.part_list.SetMinSize(wx.Size(1050, 500))
        self.reference = self.part_list.AppendTextColumn(
            u"LCSC",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.number = self.part_list.AppendTextColumn(
            u"MFR Number",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=140,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.package = self.part_list.AppendTextColumn(
            u"Package",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.joints = self.part_list.AppendTextColumn(
            u"Joints",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.type = self.part_list.AppendTextColumn(
            u"Type",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=80,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.manufacturer = self.part_list.AppendTextColumn(
            u"Manufacturer",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=140,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.decription = self.part_list.AppendTextColumn(
            u"Description",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=300,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.price = self.part_list.AppendTextColumn(
            u"Price",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        self.stock = self.part_list.AppendTextColumn(
            u"Stock",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )

        table_sizer.Add(self.part_list, 0, wx.ALL | wx.EXPAND, 5)
        # ---------------------------------------------------------------------
        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        self.select_part_button = wx.Button(
            self, wx.ID_ANY, u"Select part", wx.DefaultPosition, (150, -1), 0
        )
        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 1, wx.EXPAND, 5)
        # ---------------------------------------------------------------------

        layout.Add(table_sizer, 1, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

    def load_library(self):
        """Load library data from the excel file into a pandas dataframe"""
        busy_dialog = wx.BusyInfo("Loading library data, please wait ...")
        self.library.load()
        busy_dialog = None

    def download(self, e):
        """Download latest excel with library data."""
        self.library.download(self.download_gauge)
        self.load_library()

    def search(self, e):
        """Search the dataframe for the keyword."""
        if not self.library.loaded:
            self.load_library()
        result = self.library.search(
            self.keyword.GetValue(),
            self.basic_checkbox.GetValue(),
            self.extended_checkbox.GetValue(),
        )
        self.populate_part_list(result)

    def populate_part_list(self, parts):
        """Populate the list with the result of the search."""
        self.part_list.DeleteAllItems()
        for index, part in parts.iterrows():
            self.part_list.AppendItem(
                [
                    str(part["LCSC_Part"]),
                    str(part["MFR_Part"]),
                    str(part["Package"]),
                    str(part["Solder_Joint"]),
                    str(part["Library_Type"]),
                    str(part["Manufacturer"]),
                    str(part["Description"]),
                    str(part["Price"]),
                    str(part["Stock"]),
                ]
            )

    def select_part(self, e):
        """Save the selected part number and close the modal."""
        item = self.part_list.GetSelection()
        row = self.part_list.ItemToRow(item)
        if row == -1:
            return
        self.selection = self.part_list.GetTextValue(row, 0)
        self.EndModal(wx.ID_OK)
