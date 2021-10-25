import datetime
import logging
import os
import sys

import wx
import wx.dataview
from pcbnew import GetBoard

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
from .partdetails import PartDetailsDialog
from .partselector import PartSelectorDialog


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
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)

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
        self.download_button.Bind(wx.EVT_BUTTON, self.update_library)
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
        self.footprint_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.OnFootprintSelected
        )
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
        self.hide_bom_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Hide non BOM", wx.DefaultPosition, wx.DefaultSize
        )
        self.hide_bom_checkbox.Bind(wx.EVT_CHECKBOX, self.OnBomHideChecked)
        tool_sizer.Add(self.hide_bom_checkbox, 0, wx.ALL, 5)
        self.hide_cpl_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Hide non CPL", wx.DefaultPosition, wx.DefaultSize
        )
        self.hide_cpl_checkbox.Bind(wx.EVT_CHECKBOX, self.OnCplHideChecked)
        tool_sizer.Add(self.hide_cpl_checkbox, 0, wx.ALL, 5)
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

        self.get_footprints()
        self.populate_footprint_list()

        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

        self.enable_toolbar_buttons(False)

        # Note: a delay of 0 doesn't work
        wx.CallLater(1, self.update_library)

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def update_library(self, e=None):
        """Download and load library data if necessary or actively requested"""
        if self.dl_thread:
            return

        if self.library.need_download() or e:
            self.enable_all_buttons(False)
            self.dl_thread = self.library.download()
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.update_gauge, self.timer)
            self.timer.Start(200)
            self.then = datetime.datetime.now()
        else:
            self.load_library()

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
            self.load_library()
            self.enable_all_buttons(True)

    def load_library(self):
        self.library.load()
        fntxt = ""
        if self.library.filename:
            fntxt = self.library.filename + " with "
        self.library_desc.SetLabel(fntxt + "%d parts" % (self.library.partcount))

    def OnBomHideChecked(self, e):
        self.populate_footprint_list()

    def OnCplHideChecked(self, e):
        self.populate_footprint_list()

    def OnFootprintSelected(self, e):
        """Enable the toolbar buttons when a selection was made."""
        self.enable_toolbar_buttons(self.footprint_list.GetSelectedItemsCount() > 0)

    def enable_all_buttons(self, state):
        """Control state of all the buttons"""
        self.enable_top_buttons(state)
        self.enable_toolbar_buttons(state)

    def enable_top_buttons(self, state):
        """Control the state of all the buttons in the top section"""
        for b in [
            self.generate_button,
            self.download_button,
            self.layer_selection,
        ]:
            if state:
                b.Enable()
            else:
                b.Disable()

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side"""
        for b in [
            self.select_part_button,
            self.remove_part_button,
            self.toggle_bom_cpl_button,
            self.toggle_bom_button,
            self.toggle_cpl_button,
            self.part_details_button,
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
            if self.hide_bom_checkbox.GetValue() and get_exclude_from_bom(fp):
                continue
            if self.hide_cpl_checkbox.GetValue() and get_exclude_from_pos(fp):
                continue
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
        self.update_library()
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
