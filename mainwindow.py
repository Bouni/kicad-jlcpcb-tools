import datetime
import logging
import os
import sys

import wx
import wx.dataview
from pcbnew import GetBoard

# from .fabrication import JLCPCBFabrication
from .helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_footprint_by_ref,
    get_footprint_keys,
    get_valid_footprints,
    toggle_exclude_from_bom,
    toggle_exclude_from_pos,
)
from .store import Store

# from .library import JLCPCBLibrary
# from .partdetails import PartDetailsDialog
# from .partselector import PartSelectorDialog


class JLCBCBTools(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=f"JLCPCB Tools",
            pos=wx.DefaultPosition,
            size=wx.Size(1200, 800),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.plugin_path = os.path.split(os.path.abspath(__file__))[0]
        self.project_path = os.path.split(GetBoard().GetFileName())[0]

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
        # -------------------- Horizontal top buttons -------------------------
        # ---------------------------------------------------------------------
        top_button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.generate_button = wx.Button(
            self,
            wx.ID_ANY,
            "Generate fabrication files",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )
        self.layer_selection = wx.Choice(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            ["Auto", "1 Layer", "2 Layers", "4 Layers", "6 Layers"],
            0,
        )
        self.layer_selection.SetSelection(0)
        self.library_description = wx.StaticText(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.ALIGN_LEFT,
            "library_desc",
        )
        self.download_button = wx.Button(
            self, wx.ID_ANY, "Update library", wx.DefaultPosition, wx.DefaultSize, 0
        )

        top_button_sizer.Add(self.generate_button, 0, wx.ALL, 5)
        top_button_sizer.Add(self.layer_selection, 0, wx.ALL, 5)
        top_button_sizer.Add(self.library_description, 1, wx.TOP | wx.EXPAND, 10)
        top_button_sizer.Add(self.download_button, 0, wx.ALL, 5)

        self.generate_button.Bind(wx.EVT_BUTTON, self.generate_fabrication_data)
        self.download_button.Bind(wx.EVT_BUTTON, self.update_library)

        # ---------------------------------------------------------------------
        # ----------------------- Footprint List ------------------------------
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
            width=100,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.stock = self.footprint_list.AppendTextColumn(
            "Stock",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.bom = self.footprint_list.AppendTextColumn(
            "in BOM",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )
        self.cpl = self.footprint_list.AppendTextColumn(
            "in CPL",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE
            | wx.dataview.DATAVIEW_COL_SORTABLE,
        )

        table_sizer.Add(self.footprint_list, 20, wx.ALL | wx.EXPAND, 5)

        self.footprint_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.OnFootprintSelected
        )

        # ---------------------------------------------------------------------
        # ----------------------- Vertical Toolbar ----------------------------
        # ---------------------------------------------------------------------
        toolbar_sizer = wx.BoxSizer(wx.VERTICAL)
        self.select_part_button = wx.Button(
            self, wx.ID_ANY, "Select part", wx.DefaultPosition, (150, -1), 0
        )
        self.remove_part_button = wx.Button(
            self, wx.ID_ANY, "Remove part", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_bom_cpl_button = wx.Button(
            self, wx.ID_ANY, "Toggle BOM/CPL", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_bom_button = wx.Button(
            self, wx.ID_ANY, "Toggle BOM", wx.DefaultPosition, (150, -1), 0
        )
        self.toggle_cpl_button = wx.Button(
            self, wx.ID_ANY, "Toggle CPL", wx.DefaultPosition, (150, -1), 0
        )
        self.part_details_button = wx.Button(
            self, wx.ID_ANY, "Show part details", wx.DefaultPosition, (150, -1), 0
        )
        self.part_sum_button = wx.Button(
            self, wx.ID_ANY, "Calculate part costs", wx.DefaultPosition, (150, -1), 0
        )
        self.hide_bom_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Hide non BOM", wx.DefaultPosition, wx.DefaultSize
        )
        self.hide_cpl_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Hide non CPL", wx.DefaultPosition, wx.DefaultSize
        )

        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        self.remove_part_button.Bind(wx.EVT_BUTTON, self.remove_part)
        self.toggle_bom_cpl_button.Bind(wx.EVT_BUTTON, self.toogle_bom_cpl)
        self.toggle_bom_button.Bind(wx.EVT_BUTTON, self.toogle_bom)
        self.toggle_cpl_button.Bind(wx.EVT_BUTTON, self.toogle_cpl)
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)
        self.part_sum_button.Bind(wx.EVT_BUTTON, self.calculate_price)
        self.hide_bom_checkbox.Bind(wx.EVT_CHECKBOX, self.OnBomHideChecked)
        self.hide_cpl_checkbox.Bind(wx.EVT_CHECKBOX, self.OnCplHideChecked)

        toolbar_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.remove_part_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.toggle_bom_cpl_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.toggle_bom_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.toggle_cpl_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.part_sum_button, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.hide_bom_checkbox, 0, wx.ALL, 5)
        toolbar_sizer.Add(self.hide_cpl_checkbox, 0, wx.ALL, 5)

        table_sizer.Add(toolbar_sizer, 1, wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # --------------------- Bottom Logbox and Gauge -----------------------
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
        self.gauge = wx.Gauge(
            self, wx.ID_ANY, 100, wx.DefaultPosition, (100, -1), wx.GA_HORIZONTAL
        )
        self.gauge.SetValue(0)
        self.gauge.SetMinSize(wx.Size(-1, 5))

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        self.SetSizeHints(wx.Size(1000, -1), wx.DefaultSize)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(top_button_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.logbox, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.enable_toolbar_buttons(False)

        self.init_logger()
        self.init_store()

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def init_store(self):
        """Initialize the store of part assignments"""
        self.store = Store(self.project_path)
        self.populate_footprint_list()

    def populate_footprint_list(self):
        """Populate/Refresh list of footprints."""
        self.footprint_list.DeleteAllItems()
        for part in self.store.read_all():
            # remove the price from the list
            part = list(part)
            part.pop(5)
            # dont show the part if the checkbox for hide BOM is set
            if self.hide_bom_checkbox.GetValue() and part[5] == "No":
                continue
            # dont show the part if the checkbox for hide CPL is set
            if self.hide_cpl_checkbox.GetValue() and part[6] == "No":
                continue
            self.footprint_list.AppendItem(part)

    def update_library(self, e=None):
        pass

    #     """Download and load library data if necessary or actively requested"""
    #     if self.library.download_active:
    #         return
    #     if self.library.need_download() or e:
    #         self.enable_all_buttons(False)
    #         self.library.download()
    #         self.timer = wx.Timer(self)
    #         self.Bind(wx.EVT_TIMER, self.update_gauge, self.timer)
    #         self.timer.Start(200)
    #         self.then = datetime.datetime.now()
    #     else:
    #         self.load_library()

    # def update_gauge(self, evt):
    #     """Update the progress gauge and handle thread completion"""
    #     if self.library.dl_thread.is_alive():
    #         if self.library.dl_thread.pos:
    #             self.gauge.SetRange(1000)
    #             self.gauge.SetValue(self.library.dl_thread.pos * 1000)
    #         else:
    #             self.gauge.Pulse()
    #     else:
    #         self.timer.Stop()
    #         self.library.dl_thread = None
    #         self.library.get_info()
    #         if not self.library.download_success and self.library.isvalid:
    #             wx.MessageBox(
    #                 "Download of the CSV failed, will use existing library!",
    #                 "Download error",
    #                 style=wx.OK | wx.ICON_ERROR,
    #             )
    #         elif not self.library.download_success and not self.library.isvalid:
    #             wx.MessageBox(
    #                 "Download of the CSV failed, no existing library found, exit plugin now!",
    #                 "Download error",
    #                 style=wx.OK | wx.ICON_ERROR,
    #             )
    #             self.quit_dialog(None)
    #         else:
    #             now = datetime.datetime.now()
    #             self.logger.info(
    #                 "Downloaded into %s in %.3f seconds",
    #                 os.path.basename(self.library.dbfn),
    #                 (now - self.then).total_seconds(),
    #             )
    #         self.gauge.SetRange(1000)
    #         self.gauge.SetValue(0)
    #         self.load_library()
    #         self.enable_all_buttons(True)

    # def load_library(self):
    #     self.library.load()
    #     fntxt = ""
    #     if self.library.filename:
    #         fntxt = self.library.filename + " with "
    #     self.library_desc.SetLabel(fntxt + "%d parts" % (self.library.partcount))

    def OnBomHideChecked(self, e):
        pass

    #     self.populate_footprint_list()

    def OnCplHideChecked(self, e):
        pass

    #     self.populate_footprint_list()

    def OnFootprintSelected(self, e):
        pass

    #     """Enable the toolbar buttons when a selection was made."""
    #     self.enable_toolbar_buttons(self.footprint_list.GetSelectedItemsCount() > 0)

    def enable_all_buttons(self, state):
        pass

    #     """Control state of all the buttons"""
    #     self.enable_top_buttons(state)
    #     self.enable_toolbar_buttons(state)

    def enable_top_buttons(self, state):
        pass

    #     """Control the state of all the buttons in the top section"""
    #     for b in [
    #         self.generate_button,
    #         self.download_button,
    #         self.layer_selection,
    #     ]:
    #         b.Enable(bool(state))

    def enable_toolbar_buttons(self, state):
        pass

    #     """Control the state of all the buttons in toolbar on the right side"""
    #     for b in [
    #         self.select_part_button,
    #         self.remove_part_button,
    #         self.toggle_bom_cpl_button,
    #         self.toggle_bom_button,
    #         self.toggle_cpl_button,
    #         self.part_details_button,
    #     ]:
    #         b.Enable(bool(state))

    def get_footprints(self):
        pass

    #     """get all footprints from the board"""
    #     self.board = GetBoard()
    #     self.footprints = sorted(
    #         get_valid_footprints(self.board),
    #         key=get_footprint_keys,
    #     )

    def calculate_price(self, e):
        pass

    #     parts = {}
    #     count = self.footprint_list.GetItemCount()
    #     for i in range(0, count):
    #         _lcsc = self.footprint_list.GetTextValue(i, 3)
    #         if not _lcsc:
    #             continue
    #         if not _lcsc in parts:
    #             parts[_lcsc] = 1
    #         else:
    #             parts[_lcsc] += 1
    #     _sum = 0.0
    #     for part, count in parts.items():
    #         price = self.library.get_price(part, count)
    #         _sum += price
    #     wx.MessageBox(
    #         f"The price for all parts sums up to ${round(_sum,2)}", "Price calculation"
    #     )

    def toogle_bom_cpl(self, e):
        pass

    #     """Toggle the exclude from BOM/POS attribute of a footprint."""
    #     for item in self.footprint_list.GetSelections():
    #         row = self.footprint_list.ItemToRow(item)
    #         ref = self.footprint_list.GetTextValue(row, 0)
    #         fp = get_footprint_by_ref(self.board, ref)
    #         toggle_exclude_from_bom(fp)
    #         toggle_exclude_from_pos(fp)
    #     self.populate_footprint_list()
    #     self.fabrication.save_part_assignments()

    def toogle_bom(self, e):
        pass

    #     """Toggle the exclude from BOM attribute of a footprint."""
    #     for item in self.footprint_list.GetSelections():
    #         row = self.footprint_list.ItemToRow(item)
    #         ref = self.footprint_list.GetTextValue(row, 0)
    #         fp = get_footprint_by_ref(self.board, ref)
    #         toggle_exclude_from_bom(fp)
    #     self.populate_footprint_list()
    #     self.fabrication.save_part_assignments()

    def toogle_cpl(self, e):
        pass

    #     """Toggle the exclude from POS attribute of a footprint."""
    #     for item in self.footprint_list.GetSelections():
    #         row = self.footprint_list.ItemToRow(item)
    #         ref = self.footprint_list.GetTextValue(row, 0)
    #         fp = get_footprint_by_ref(self.board, ref)
    #         toggle_exclude_from_pos(fp)
    #     self.populate_footprint_list()
    #     self.fabrication.save_part_assignments()

    def select_part(self, e):
        pass

    #     """Select and assign a LCSC Part number to a footprint via modal dialog."""
    #     self.update_library()
    #     # Figure out what LCSC numbers are selected
    #     selection = []
    #     lcsc = ""
    #     for item in self.footprint_list.GetSelections():
    #         row = self.footprint_list.ItemToRow(item)
    #         _lcsc = self.footprint_list.GetTextValue(row, 3)
    #         if not _lcsc in selection:
    #             selection.append(_lcsc)
    #     # if we have not selected more than one LCSC number, pass it to the selection dialog
    #     # as search preset
    #     if len(selection) == 1:
    #         lcsc = selection[0]
    #     dialog = PartSelectorDialog(self, lcsc)
    #     result = dialog.ShowModal()
    #     if result == wx.ID_OK:
    #         for item in self.footprint_list.GetSelections():
    #             row = self.footprint_list.ItemToRow(item)
    #             ref = self.footprint_list.GetTextValue(row, 0)
    #             self.fabrication.parts[ref]["lcsc"] = str(dialog.selection)
    #         self.populate_footprint_list()
    #         self.fabrication.save_part_assignments()
    #     dialog.Destroy()

    def remove_part(self, e):
        pass

    #     """Remove an assigned a LCSC Part number to a footprint."""
    #     for item in self.footprint_list.GetSelections():
    #         row = self.footprint_list.ItemToRow(item)
    #         ref = self.footprint_list.GetTextValue(row, 0)
    #         self.fabrication.parts[ref]["lcsc"] = ""
    #         self.populate_footprint_list()
    #         self.fabrication.save_part_assignments()

    def generate_fabrication_data(self, e):
        pass

    #     """Generate Fabrication data."""
    #     layer_selection = self.layer_selection.GetSelection()
    #     if layer_selection != 0:
    #         layer_count = int(self.layer_selection.GetString(layer_selection)[:1])
    #     else:
    #         layer_count = None
    #     self.fabrication.generate_geber(layer_count)
    #     self.fabrication.generate_excellon()
    #     self.fabrication.zip_gerber_excellon()
    #     self.fabrication.generate_cpl()
    #     self.fabrication.generate_bom()

    def get_part_details(self, e):
        pass

    #     """Fetch part details from LCSC and show them in a modal."""
    #     item = self.footprint_list.GetSelection()
    #     row = self.footprint_list.ItemToRow(item)
    #     if row == -1:
    #         return
    #     part = self.footprint_list.GetTextValue(row, 3)
    #     if part != "":
    #         dialog = PartDetailsDialog(self, part)

    #         dialog.Show()

    def init_logger(self):
        """Initialize logger to log into textbox"""
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        # Log to stderr
        handler1 = logging.StreamHandler(sys.stderr)
        handler1.setLevel(logging.DEBUG)
        # and to our GUI
        handler2 = LogBoxHandler(self.logbox)
        handler2.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y.%m.%d %H:%M:%S",
        )
        handler1.setFormatter(formatter)
        handler2.setFormatter(formatter)
        root.addHandler(handler1)
        root.addHandler(handler2)
        self.logger = logging.getLogger(__name__)

    def __del__(self):
        pass


class LogBoxHandler(logging.StreamHandler):
    def __init__(self, textctrl):
        logging.StreamHandler.__init__(self)
        self.textctrl = textctrl

    def emit(self, record):
        """Pokemon exception that hopefully helps getting this working with threads."""
        try:
            msg = self.format(record)
            self.textctrl.WriteText(msg + "\n")
            self.flush()
        except:
            pass
