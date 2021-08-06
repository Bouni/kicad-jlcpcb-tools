import csv
import logging
import os
import re
import sys
from pathlib import Path
from zipfile import ZipFile

import wx
import wx.grid
from pcbnew import *

from .fabrication import JLCPCBFabrication
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
        """Run is caled when the action button is clicked."""
        dialog = Dialog(None)
        dialog.Center()
        dialog.ShowModal()
        dialog.Destroy()


class FabricationTab(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)

        description = wx.StaticText(
            self, label="Generate JLCPCB production and assembly files."
        )
        generate_button = wx.Button(self, label="Generate")
        generate_button.Bind(wx.EVT_BUTTON, self.generate)

        buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
        buttonSizer.Add(generate_button)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(description, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5)
        layout.Add(buttonSizer, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5)
        self.SetSizer(layout)
        self.Refresh()
        self.Layout()

    def generate(self, e):
        """Generate Fabrication data."""
        e.Skip()
        self.fabrication = JLCPCBFabrication()
        self.fabrication.setup()
        self.fabrication.generate_geber()
        self.fabrication.generate_excellon()
        self.fabrication.zip_gerber_excellon()
        self.fabrication.generate_cpl()
        self.fabrication.generate_bom()


class LibraryGrid(wx.grid.Grid):
    def __init__(self, parent):
        wx.grid.Grid.__init__(self, parent, -1, size=(-1, 600))
        self.CreateGrid(0, 9)
        self.HideRowLabels()
        self.SetColLabelValue(0, "LCSC")
        self.SetColSize(0, 100)
        self.SetColLabelValue(1, "Part No.")
        self.SetColSize(1, 100)
        self.SetColLabelValue(2, "Package")
        self.SetColSize(2, 100)
        self.SetColLabelValue(3, "Solder Joints")
        self.SetColSize(3, 100)
        self.SetColLabelValue(4, "Type")
        self.SetColSize(4, 100)
        self.SetColLabelValue(5, "Manufacturer")
        self.SetColSize(5, 100)
        self.SetColLabelValue(6, "Price")
        self.SetColSize(6, 100)
        self.SetColLabelValue(7, "Stock")
        self.SetColSize(7, 100)
        self.SetColLabelValue(8, "Description")
        self.SetColSize(8, 360)


class PartSelectorDialog(wx.Dialog):
    """Modal dialog for JLCPCB part search and selection"""

    def __init__(self, parent):
        super(PartSelectorDialog, self).__init__(
            parent,
            title="JLCPCB library",
            size=(1200, 620),
            style=wx.DEFAULT_DIALOG_STYLE | wx.CLOSE_BOX,
        )
        self.logger = logging.getLogger(__name__)
        self.library = parent.library
        panel = wx.Panel(self)
        self.selection = ""
        self.keyword = wx.TextCtrl(panel, size=(400, 20))
        self.basic = wx.CheckBox(panel, label="Basic")
        self.basic.SetValue(True)
        self.extended = wx.CheckBox(panel, label="Extended")
        self.extended.SetValue(True)
        search_button = wx.Button(panel, -1, label="Search", size=(50, 20))
        searchSizer = wx.BoxSizer(wx.HORIZONTAL)
        searchSizer.Add(
            self.keyword, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5
        )
        searchSizer.Add(self.basic, flag=wx.TOP | wx.LEFT | wx.RIGHT, border=5)
        searchSizer.Add(self.extended, flag=wx.TOP | wx.LEFT | wx.RIGHT, border=5)
        searchSizer.Add(search_button, flag=wx.TOP | wx.LEFT | wx.RIGHT, border=5)

        search_button.Bind(wx.EVT_BUTTON, self.onSearch)

        self.table = LibraryGrid(panel)
        self.table.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.onSelect)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(searchSizer, flag=wx.EXPAND | wx.ALL | wx.CENTER, border=5)
        layout.Add(self.table, flag=wx.ALL | wx.CENTER, border=5)
        panel.SetSizer(layout)

    def onSearch(self, e):
        result = self.library.search(
            self.keyword.GetValue(), self.basic.GetValue(), self.extended.GetValue()
        )
        self.populate_table(result)
        e.skip()

    def onSelect(self, e):
        self.selection = self.table.GetCellValue(e.GetRow(), 0)
        self.EndModal(wx.ID_OK)
        e.skip()

    def populate_table(self, data):
        self.table.DeleteRows(numRows=self.table.GetNumberRows())
        row = 0
        for index, part in data.iterrows():
            self.table.AppendRows()
            self.table.SetCellValue(row, 0, str(part["LCSC_Part"]))
            self.table.SetCellValue(row, 1, str(part["MFR_Part"]))
            self.table.SetCellValue(row, 2, str(part["Package"]))
            self.table.SetCellValue(row, 3, str(part["Solder_Joint"]))
            self.table.SetCellValue(row, 4, str(part["Library_Type"]))
            self.table.SetCellValue(row, 5, str(part["Manufacturer"]))
            self.table.SetCellValue(row, 6, str(part["Price"]))
            self.table.SetCellValue(row, 7, str(part["Stock"]))
            self.table.SetCellValue(row, 8, str(part["Description"]))
            row += 1


class LibraryTab(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        description = wx.StaticText(
            self, label="Generate JLCPCB production and assembly files."
        )
        self.logger = logging.getLogger(__name__)
        self.download_button = wx.Button(self, label="Update library", size=(200, -1))
        self.download_button.Bind(wx.EVT_BUTTON, self.download)

        self.progress = wx.Gauge(self, wx.ID_ANY, 100)

        self.setup_table()

        buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
        buttonSizer.Add(
            self.download_button, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5
        )
        buttonSizer.Add(
            self.progress, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5
        )
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(description, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5)
        layout.Add(buttonSizer, flag=wx.EXPAND | wx.TOP | wx.RIGHT, border=5)
        layout.Add(self.table, flag=wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=5)
        self.SetSizer(layout)
        self.Refresh()
        self.Layout()
        self.library = JLCPCBLibrary()
        self.populate_table()

    def get_footprints(self):
        self.board = GetBoard()
        self.footprints = sorted(
            self.board.GetFootprints(),
            key=lambda fp: (
                str(fp.GetFPID().GetLibItemName()),
                int(re.search("\d+", fp.GetReference())[0]),
            ),
        )

    def setup_table(self):
        self.get_footprints()
        self.table = wx.grid.Grid(self, wx.ID_ANY, size=(-1, 300))
        self.table.CreateGrid(len(self.footprints), 4)
        self.table.SetColLabelValue(0, "Reference")
        self.table.SetColSize(0, 150)
        self.table.SetColLabelValue(1, "Value")
        self.table.SetColSize(1, 150)
        self.table.SetColLabelValue(2, "Footprint")
        self.table.SetColSize(2, 150)
        self.table.SetColLabelValue(3, "LCSC")
        self.table.SetColSize(3, 150)
        self.table.SetSelectionMode(wx.grid.Grid.SelectRows)
        self.table.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.onRowClick)

    def onRowClick(self, e):
        # fp = self.footprints[e.GetRow()]
        clicked = self.footprints[e.GetRow()]
        self.selected = [self.footprints[idx] for idx in self.table.GetSelectedRows()]
        if clicked not in self.selected:
            self.selected.append(clicked)
        if not self.library.loaded:
            busy_dialog = wx.BusyInfo("Loading library data, please wait ...")
            self.library.load()
            busy_dialog = None
        dialog = PartSelectorDialog(self)
        result = dialog.ShowModal()
        if result == wx.ID_OK:
            value = dialog.selection
            for fp in self.selected:
                fp.SetProperty("LCSC", str(value))
            self.populate_table()
        dialog.Destroy()

    def populate_table(self):
        self.get_footprints()
        for row, fp in enumerate(self.footprints):
            self.table.SetCellValue(row, 0, str(fp.GetReference()))
            self.table.SetCellValue(row, 1, str(fp.GetValue()))
            self.table.SetCellValue(row, 2, str(fp.GetFPID().GetLibItemName()))
            self.table.SetCellValue(row, 3, str(fp.GetProperties().get("LCSC", "")))

    def download(self, e):
        """Download latest library data."""
        e.Skip()
        self.library.download(self.progress)


class Dialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=-1,
            title="KiCAD JLCPCB tools",
            size=(820, 620),
            style=wx.DEFAULT_DIALOG_STYLE
            | wx.CLOSE_BOX
            | wx.MAXIMIZE_BOX
            | wx.RESIZE_BORDER,
        )
        self.SetIcon(
            wx.Icon(
                os.path.join(
                    os.path.abspath(os.path.dirname(__file__)), "jlcpcb-icon.png"
                )
            )
        )
        panel = wx.Panel(self)
        # Important to setup logger here before tabs are created so that we can log from every sub window
        log = wx.TextCtrl(
            panel, wx.ID_ANY, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 200)
        )
        sys.stdout = log  # redirect stdout = log textbox
        self.init_logger()  # set logger to log to stdout = log textbox

        notebook = wx.Notebook(panel)
        fabrication_tab = FabricationTab(notebook)
        library_tab = LibraryTab(notebook)
        notebook.AddPage(fabrication_tab, "Fabrication data")
        notebook.AddPage(library_tab, "Parts library")

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(notebook, 1, wx.EXPAND)
        sizer.Add(log, 1, wx.EXPAND)
        panel.SetSizer(sizer)
        self.Show()

    def init_logger(self):
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


JLCPCBPlugin().register()
