import datetime
import io
import logging
import os
import sys
import webbrowser

import requests
import wx
import wx.dataview
import wx.grid
import wx.xrc
from pcbnew import *
from wx.core import ID_ANY

from .fabrication import JLCPCBFabrication
from .helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_footprint_by_ref,
    get_footprint_keys,
    get_valid_footprints,
    get_version_info,
    toggle_exclude_from_bom,
    toggle_exclude_from_pos,
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
            title=f"JLCPCB Tools [ Version: {get_version_info()} ]",
            pos=wx.DefaultPosition,
            size=wx.Size(906, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
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
        self.Bind(wx.EVT_MENU, self.do_quit, id=quitid)

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
        self.init_logger()

        # ---------------------------------------------------------------------
        self.library = JLCPCBLibrary(self)
        self.dl_thread = None
        self.fabrication = JLCPCBFabrication(self)
        # ---------------------------------------------------------------------

        self.SetSizeHints(wx.Size(800, -1), wx.DefaultSize)

        layout = wx.BoxSizer(wx.VERTICAL)

        # ---------------------------------------------------------------------
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.generate_button = wx.Button(
            self,
            wx.ID_ANY,
            "Generate fabrication files",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )
        self.generate_button.Bind(wx.EVT_BUTTON, self.generate_fabrication_data)
        button_sizer.Add(self.generate_button, 0, wx.ALL, 5)

        self.layer_selection = wx.Choice(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            ["Auto", "1 Layer", "2 Layers", "4 Layers", "6 Layers"],
            0,
        )
        self.layer_selection.SetSelection(0)
        button_sizer.Add(self.layer_selection, 0, wx.ALL, 5)
        self.library_desc = wx.StaticText(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.ALIGN_LEFT,
            "library_desc",
        )
        button_sizer.Add(self.library_desc, 0, wx.ALL, 5)
        # ---------------------------------------------------------------------
        button_sizer.Add(self.library_desc, 1, wx.TOP | wx.EXPAND, 10)
        self.download_button = wx.Button(
            self, wx.ID_ANY, "Update library", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.download_button.Bind(wx.EVT_BUTTON, self.load_library)
        button_sizer.Add(self.download_button, 0, wx.ALL, 5)

        layout.Add(button_sizer, 0, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(wx.Size(-1, 600))
        self.footprint_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_MULTIPLE,
        )
        self.footprint_list.SetMinSize(wx.Size(750, 400))
        self.reference = self.footprint_list.AppendTextColumn(
            "Reference",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.value = self.footprint_list.AppendTextColumn(
            "Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=200,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.footprint = self.footprint_list.AppendTextColumn(
            "Footprint",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=300,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.lcsc = self.footprint_list.AppendTextColumn(
            "LCSC",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=80,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.bom = self.footprint_list.AppendTextColumn(
            "in BOM",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=40,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.cpl = self.footprint_list.AppendTextColumn(
            "in CPL",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=40,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.get_footprints()
        self.populate_footprint_list()
        table_sizer.Add(self.footprint_list, 20, wx.ALL | wx.EXPAND, 5)
        # ---------------------------------------------------------------------
        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        self.select_part_button = wx.Button(
            self, wx.ID_ANY, "Select part", wx.DefaultPosition, (150, -1), 0
        )
        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        self.remove_part_button = wx.Button(
            self, wx.ID_ANY, "Remove part", wx.DefaultPosition, (150, -1), 0
        )
        self.remove_part_button.Bind(wx.EVT_BUTTON, self.remove_part)
        tool_sizer.Add(self.remove_part_button, 0, wx.ALL, 5)
        self.toggle_bom_cpl_button = wx.Button(
            self, wx.ID_ANY, "Toggle BOM/CPL", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_bom_cpl_button.Bind(wx.EVT_BUTTON, self.toogle_bom_cpl)
        tool_sizer.Add(self.toggle_bom_cpl_button, 0, wx.ALL, 5)
        self.toggle_bom_button = wx.Button(
            self, wx.ID_ANY, "Toggle BOM", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_bom_button.Bind(wx.EVT_BUTTON, self.toogle_bom)
        tool_sizer.Add(self.toggle_bom_button, 0, wx.ALL, 5)
        self.toggle_cpl_button = wx.Button(
            self, wx.ID_ANY, "Toggle CPL", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_cpl_button.Bind(wx.EVT_BUTTON, self.toogle_cpl)
        tool_sizer.Add(self.toggle_cpl_button, 0, wx.ALL, 5)
        self.part_details_button = wx.Button(
            self, wx.ID_ANY, "Show part details", wx.DefaultPosition, (150, -1), 0
        )
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 1, wx.EXPAND, 5)
        # ---------------------------------------------------------------------

        self.gauge = wx.Gauge(
            self, wx.ID_ANY, 100, wx.DefaultPosition, (100, -1), wx.GA_HORIZONTAL
        )
        self.gauge.SetValue(0)
        self.gauge.SetMinSize(wx.Size(-1, 5))

        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        layout.Add(self.logbox, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

        # Note: a delay of 0 doesn't work
        wx.CallLater(1, self.load_library)

    def do_quit(self, e):
        self.Destroy()
        self.EndModal(0)

    def load_library(self, e=None):
        """Download and load library data if necessary or actively requested"""
        if self.dl_thread:
            return

        if self.library.need_download() or e:
            self.enable_buttons(False)
            self.dl_thread = self.library.download()
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.update_gauge, self.timer)
            self.timer.Start(200)
            self.then = datetime.datetime.now()
        else:
            self.do_load()

    def update_gauge(self, evt):
        """Update the progress gauge and handle thread completion"""
        if self.dl_thread.is_alive():
            if self.dl_thread.pos:
                self.gauge.SetRange(1000)
                self.gauge.SetValue(self.dl_thread.pos * 1000)
            else:
                self.gauge.Pulse()
        else:
            self.timer.Stop()
            self.dl_thread = None
            now = datetime.datetime.now()
            self.logger.info(
                "Downloaded into %s in %.3f seconds",
                os.path.basename(self.library.dbfn),
                (now - self.then).total_seconds(),
            )
            self.gauge.SetRange(1000)
            self.gauge.SetValue(0)
            self.do_load()
            self.enable_buttons(True)

    def do_load(self):
        self.library.load()
        fntxt = ""
        if self.library.filename:
            fntxt = self.library.filename + " with "
        self.library_desc.SetLabel(fntxt + "%d parts" % (self.library.partcount))

    def enable_buttons(self, state):
        """Control state of all the buttons"""
        for b in [
            self.generate_button,
            self.download_button,
            self.select_part_button,
            self.remove_part_button,
            self.toggle_bom_cpl_button,
            self.toggle_bom_button,
            self.toggle_cpl_button,
            self.part_details_button,
            self.layer_selection,
        ]:
            if state:
                b.Enable()
            else:
                b.Disable()

    def get_footprints(self):
        """get all footprints from the board"""
        self.board = GetBoard()
        self.footprints = sorted(
            get_valid_footprints(self.board),
            key=get_footprint_keys,
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

    def toogle_bom_cpl(self, e):
        """Toggle the exclude from BOM/POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            row = self.footprint_list.ItemToRow(item)
            ref = self.footprint_list.GetTextValue(row, 0)
            fp = get_footprint_by_ref(self.board, ref)
            toggle_exclude_from_bom(fp)
            toggle_exclude_from_pos(fp)
        self.populate_footprint_list()
        self.fabrication.save_part_assignments()

    def toogle_bom(self, e):
        """Toggle the exclude from BOM attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            row = self.footprint_list.ItemToRow(item)
            ref = self.footprint_list.GetTextValue(row, 0)
            fp = get_footprint_by_ref(self.board, ref)
            toggle_exclude_from_bom(fp)
        self.populate_footprint_list()
        self.fabrication.save_part_assignments()

    def toogle_cpl(self, e):
        """Toggle the exclude from POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            row = self.footprint_list.ItemToRow(item)
            ref = self.footprint_list.GetTextValue(row, 0)
            fp = get_footprint_by_ref(self.board, ref)
            toggle_exclude_from_pos(fp)
        self.populate_footprint_list()
        self.fabrication.save_part_assignments()

    def select_part(self, e):
        """Select and assign a LCSC Part number to a footprint via modal dialog."""
        self.load_library()
        dialog = PartSelectorDialog(self)
        result = dialog.ShowModal()
        if result == wx.ID_OK:
            for item in self.footprint_list.GetSelections():
                row = self.footprint_list.ItemToRow(item)
                ref = self.footprint_list.GetTextValue(row, 0)
                self.fabrication.parts[ref]["lcsc"] = str(dialog.selection)
            self.populate_footprint_list()
            self.fabrication.save_part_assignments()
        dialog.Destroy()

    def remove_part(self, e):
        """Remove an assigned a LCSC Part number to a footprint."""
        for item in self.footprint_list.GetSelections():
            row = self.footprint_list.ItemToRow(item)
            ref = self.footprint_list.GetTextValue(row, 0)
            self.fabrication.parts[ref]["lcsc"] = ""
            self.populate_footprint_list()
            self.fabrication.save_part_assignments()

    def generate_fabrication_data(self, e):
        """Generate Fabrication data."""
        layer_selection = self.layer_selection.GetSelection()
        if layer_selection != 0:
            layer_count = int(self.layer_selection.GetString(layer_selection)[:1])
        else:
            layer_count = None
        self.fabrication.generate_geber(layer_count)
        self.fabrication.generate_excellon()
        self.fabrication.zip_gerber_excellon()
        self.fabrication.generate_cpl()
        self.fabrication.generate_bom()

    def get_part_details(self, e):
        """Fetch part details from LCSC and show them in a modal."""
        item = self.footprint_list.GetSelection()
        row = self.footprint_list.ItemToRow(item)
        if row == -1:
            return
        part = self.footprint_list.GetTextValue(row, 3)
        if part != "":
            dialog = PartDetailsDialog(self, part)
            dialog.Show()

    def init_logger(self):
        """Initialize logger to log into textbox"""
        # Remove all handlers associated with the root logger object.
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        # Log to stderr
        handler1 = logging.StreamHandler(sys.stderr)
        handler1.setLevel(logging.DEBUG)
        # and to our GUI
        handler2 = logging.StreamHandler(self.logbox)
        handler2.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y.%m.%d %H:%M:%S"
        )
        handler1.setFormatter(formatter)
        handler2.setFormatter(formatter)
        root.addHandler(handler1)
        root.addHandler(handler2)
        self.logger = logging.getLogger(__name__)

    def __del__(self):
        pass


class PartSelectorDialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Library",
            pos=wx.DefaultPosition,
            size=wx.Size(1206, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

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
        self.Bind(wx.EVT_MENU, self.do_quit, id=quitid)

        self.logger = logging.getLogger(__name__)
        self.library = parent.library

        self.SetSizeHints(wx.Size(1200, -1), wx.DefaultSize)

        layout = wx.BoxSizer(wx.VERTICAL)

        # ---------------------------------------------------------------------
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.keyword = wx.TextCtrl(
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            (300, -1),
            wx.TE_PROCESS_ENTER,
        )
        self.keyword.Bind(wx.EVT_TEXT_ENTER, self.search)
        button_sizer.Add(self.keyword, 0, wx.ALL, 5)
        self.keyword.SetFocus()

        self.search_button = wx.Button(
            self,
            wx.ID_ANY,
            "Search",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )
        self.search_button.Bind(wx.EVT_BUTTON, self.search)
        button_sizer.Add(self.search_button, 0, wx.ALL, 5)

        self.basic_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Basic", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.basic_checkbox.SetValue(True)
        button_sizer.Add(self.basic_checkbox, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)
        self.extended_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Extended", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.extended_checkbox.SetValue(True)
        button_sizer.Add(self.extended_checkbox, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)

        self.assert_stock_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "in Stock", wx.DefaultPosition, wx.DefaultSize, 0
        )
        self.assert_stock_checkbox.SetValue(True)
        button_sizer.Add(self.assert_stock_checkbox, 0, wx.TOP | wx.LEFT | wx.RIGHT, 8)

        layout.Add(button_sizer, 1, wx.ALL, 5)
        # ---------------------------------------------------------------------
        filter_sizer = wx.BoxSizer(wx.HORIZONTAL)

        package_filter_layout = wx.BoxSizer(wx.VERTICAL)
        package_filter_title = wx.StaticText(
            self,
            wx.ID_ANY,
            "Packages",
            wx.DefaultPosition,
        )
        package_filter_layout.Add(package_filter_title)
        package_filter_search = wx.TextCtrl(
            self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, (300, -1), 0
        )
        package_filter_search.Bind(wx.EVT_TEXT, self.OnPackageFilter)
        package_filter_layout.Add(package_filter_search)
        self.package_filter_list = wx.ListBox(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            (300, -1),
            choices=[],
            style=wx.LB_EXTENDED,
        )
        package_filter_layout.Add(self.package_filter_list)
        filter_sizer.Add(package_filter_layout, 1, wx.ALL, 5)

        manufacturer_filter_layout = wx.BoxSizer(wx.VERTICAL)
        manufacturer_filter_title = wx.StaticText(
            self,
            wx.ID_ANY,
            "Manufacturers",
            wx.DefaultPosition,
        )
        manufacturer_filter_layout.Add(manufacturer_filter_title)
        manufacturer_filter_search = wx.TextCtrl(
            self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, (300, -1), 0
        )
        manufacturer_filter_search.Bind(wx.EVT_TEXT, self.OnManufacturerFilter)
        manufacturer_filter_layout.Add(manufacturer_filter_search)
        self.manufacturer_filter_list = wx.ListBox(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            (300, -1),
            choices=[],
            style=wx.LB_EXTENDED,
        )
        manufacturer_filter_layout.Add(self.manufacturer_filter_list)
        filter_sizer.Add(manufacturer_filter_layout, 1, wx.ALL, 5)

        layout.Add(filter_sizer, 1, wx.ALL, 5)

        result_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.result_count = wx.StaticText(
            self, wx.ID_ANY, "0 Results", wx.DefaultPosition, wx.DefaultSize
        )
        result_sizer.Add(self.result_count, 0, wx.LEFT, 5)
        layout.Add(result_sizer, 1, wx.LEFT, 5)

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
            "LCSC",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=80,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.number = self.part_list.AppendTextColumn(
            "MFR Number",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=140,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.package = self.part_list.AppendTextColumn(
            "Package",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.joints = self.part_list.AppendTextColumn(
            "Joints",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=40,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.type = self.part_list.AppendTextColumn(
            "Type",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=80,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.manufacturer = self.part_list.AppendTextColumn(
            "Manufacturer",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=140,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.decription = self.part_list.AppendTextColumn(
            "Description",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=300,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.price = self.part_list.AppendTextColumn(
            "Price",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.stock = self.part_list.AppendTextColumn(
            "Stock",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )

        table_sizer.Add(self.part_list, 20, wx.ALL | wx.EXPAND, 5)
        # ---------------------------------------------------------------------
        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        self.select_part_button = wx.Button(
            self, wx.ID_ANY, "Select part", wx.DefaultPosition, (150, -1), 0
        )
        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        self.part_details_button = wx.Button(
            self, wx.ID_ANY, "Show part details", wx.DefaultPosition, (150, -1), 0
        )
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 1, wx.EXPAND, 5)
        # ---------------------------------------------------------------------

        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

        self.package_filter_choices = self.library.get_packages()
        self.package_filter_list.Set(self.package_filter_choices)
        self.manufacturer_filter_choices = self.library.get_manufacturers()
        self.manufacturer_filter_list.Set(self.manufacturer_filter_choices)

    def do_quit(self, e):
        self.Destroy()
        self.EndModal(0)

    def OnPackageFilter(self, e):
        search = e.GetString().lower()
        choices = [c for c in self.package_filter_choices if search in c.lower()]
        if choices != []:
            self.package_filter_list.Set(choices)
        else:
            self.package_filter_list.Set([""])

    def OnManufacturerFilter(self, e):
        search = e.GetString().lower()
        choices = [c for c in self.manufacturer_filter_choices if search in c.lower()]
        if choices != []:
            self.manufacturer_filter_list.Set(choices)
        else:
            self.manufacturer_filter_list.Set([""])

    def search(self, e):
        """Search the dataframe for the keyword."""
        if not self.library.loaded:
            self.load_library()

        filtered_packages = self.package_filter_list.GetStrings()
        packages = [
            filtered_packages[i] for i in self.package_filter_list.GetSelections()
        ]
        filtered_manufacturers = self.manufacturer_filter_list.GetStrings()
        manufacturers = [
            filtered_manufacturers[i]
            for i in self.manufacturer_filter_list.GetSelections()
        ]
        result = self.library.search(
            self.keyword.GetValue(),
            self.basic_checkbox.GetValue(),
            self.extended_checkbox.GetValue(),
            self.assert_stock_checkbox.GetValue(),
            packages,
            manufacturers,
        )
        self.result_count.SetLabel(f"{len(result)} Results")
        self.populate_part_list(result)

    def populate_part_list(self, parts):
        """Populate the list with the result of the search."""
        self.part_list.DeleteAllItems()
        if parts is None:
            return
        for p in parts:
            self.part_list.AppendItem([str(c) for c in p])

    def select_part(self, e):
        """Save the selected part number and close the modal."""
        item = self.part_list.GetSelection()
        row = self.part_list.ItemToRow(item)
        if row == -1:
            return
        self.selection = self.part_list.GetTextValue(row, 0)
        self.EndModal(wx.ID_OK)

    def get_part_details(self, e):
        """Fetch part details from LCSC and show them in a modal."""
        item = self.part_list.GetSelection()
        row = self.part_list.ItemToRow(item)
        if row == -1:
            return
        part = self.part_list.GetTextValue(row, 0)
        dialog = PartDetailsDialog(self, part)
        dialog.Show()


class PartDetailsDialog(wx.Dialog):
    def __init__(self, parent, part):
        self.part = part
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Part Details",
            pos=wx.DefaultPosition,
            size=wx.Size(800, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )
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
        self.Bind(wx.EVT_MENU, self.do_quit, id=quitid)
        self.logger = logging.getLogger(__name__)

        layout = wx.BoxSizer(wx.HORIZONTAL)

        self.data_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_SINGLE,
        )

        self.property = self.data_list.AppendTextColumn(
            "Property",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=200,
            align=wx.ALIGN_LEFT,
        )
        self.value = self.data_list.AppendTextColumn(
            "Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=300,
            align=wx.ALIGN_LEFT,
        )
        layout.Add(self.data_list, 20, wx.ALL | wx.EXPAND, 5)

        self.get_part_data()

        rhslayout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(rhslayout, 20, wx.ALL | wx.EXPAND, 5)

        if self.picture:
            staticImage = wx.StaticBitmap(
                self,
                wx.ID_ANY,
                self.get_scaled_bitmap(self.picture, 200, 200),
                wx.DefaultPosition,
                (200, 200),
                0,
            )
            rhslayout.Add(staticImage, 10, wx.ALL | wx.EXPAND, 5)

        if self.pdfurl:
            openpdf = wx.Button(
                self,
                wx.ID_ANY,
                "Open Datasheet",
                wx.DefaultPosition,
                wx.DefaultSize,
                0,
            )
            openpdf.Bind(wx.EVT_BUTTON, self.openpdf)
            rhslayout.AddStretchSpacer(50)
            rhslayout.Add(openpdf, 10, wx.LEFT | wx.RIGHT | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

    def do_quit(self, e):
        self.Destroy()
        self.EndModal(0)

    def openpdf(self, e):
        self.logger.info("opening %s", str(self.pdfurl))
        webbrowser.open(self.pdfurl)

    def get_scaled_bitmap(self, url, width, height):
        content = requests.get(url).content
        io_bytes = io.BytesIO(content)
        image = wx.Image(io_bytes)
        image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
        result = wx.Bitmap(image)
        return result

    def get_part_data(self):
        r = requests.get(
            f"https://wwwapi.lcsc.com/v1/products/detail?product_code={self.part}"
        )
        parameters = {
            "productCode": "Product Code",
            "productModel": "Model",
            "parentCatalogName": "Main Category",
            "catalogName": "Sub Category",
            "brandNameEn": "Brand",
            "encapStandard": "Package",
            "productUnit": "Unit",
            "productWeight": "Weight",
            "pdfUrl": "Data Sheet",
        }
        data = r.json()
        for k, v in parameters.items():
            val = data.get(k)
            if k == "pdfUrl":
                self.pdfurl = val
            if val:
                self.data_list.AppendItem([v, str(val)])
        if data.get("paramVOList"):
            for item in data.get("paramVOList", []):
                self.data_list.AppendItem(
                    [item["paramNameEn"], str(item["paramValueEn"])]
                )
        self.picture = data["productImages"][0]
