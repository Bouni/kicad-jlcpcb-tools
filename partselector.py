import json
import logging
import os
from sys import path

import wx

from .events import AssignPartsEvent
from .helpers import PLUGIN_PATH, HighResWxSize, loadBitmapScaled
from .partdetails import PartDetailsDialog


class PartSelectorDialog(wx.Dialog):
    def __init__(self, parent, parts):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Library",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1300, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.parts = parts
        self.settings = None
        self.load_settings()
        lcsc_selection = self.get_existing_selection(parts)

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
        # --------------------------- Search bar ------------------------------
        # ---------------------------------------------------------------------

        keyword_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Keyword",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.keyword = wx.TextCtrl(
            self,
            wx.ID_ANY,
            lcsc_selection,
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.keyword.SetHint("e.g. 10k 0603")

        manufacturer_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Manufacturer",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.manufacturer = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.manufacturer.SetHint("e.g. Vishay")

        package_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Package",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.package = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.package.SetHint("e.g. 0603")

        category_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Category",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.category = wx.ComboBox(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            choices=parent.library.categories,
        )
        self.category.SetHint("e.g. Resistors")

        part_no_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Part number",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.part_no = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.part_no.SetHint("e.g. DS2411")

        solder_joints_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Solder joints",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.solder_joints = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.solder_joints.SetHint("e.g. 2")

        subcategory_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Subcategory",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.subcategory = wx.ComboBox(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
        )
        self.subcategory.SetHint("e.g. Variable Resistors")

        basic_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include basic parts",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.basic_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Basic",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            0,
        )
        extended_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include extended parts",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.extended_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Extended",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            0,
        )
        stock_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Only show parts in stock",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.assert_stock_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "in Stock",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
            0,
        )

        self.basic_checkbox.SetValue(
            self.settings.get("partselector", {}).get("basic", True)
        )
        self.extended_checkbox.SetValue(
            self.settings.get("partselector", {}).get("extended", True)
        )
        self.assert_stock_checkbox.SetValue(
            self.settings.get("partselector", {}).get("stock", False)
        )

        self.basic_checkbox.Bind(wx.EVT_CHECKBOX, self.upadate_settings)
        self.extended_checkbox.Bind(wx.EVT_CHECKBOX, self.upadate_settings)
        self.assert_stock_checkbox.Bind(wx.EVT_CHECKBOX, self.upadate_settings)

        help_button = wx.Button(
            self,
            wx.ID_ANY,
            "Help",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(100, -1)),
            0,
        )

        self.search_button = wx.Button(
            self,
            wx.ID_ANY,
            "Search",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(100, -1)),
            0,
        )

        search_sizer_one = wx.BoxSizer(wx.VERTICAL)
        search_sizer_one.Add(keyword_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_one.Add(
            self.keyword,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_one.Add(
            manufacturer_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        search_sizer_one.Add(
            self.manufacturer,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_one.Add(package_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_one.Add(
            self.package,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        search_sizer_two = wx.BoxSizer(wx.VERTICAL)
        search_sizer_two.Add(category_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_two.Add(
            self.category,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_two.Add(part_no_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_two.Add(
            self.part_no,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_two.Add(
            solder_joints_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        search_sizer_two.Add(
            self.solder_joints,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        search_sizer_three = wx.BoxSizer(wx.VERTICAL)
        search_sizer_three.Add(
            subcategory_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        search_sizer_three.Add(
            self.subcategory,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        search_sizer_four = wx.BoxSizer(wx.VERTICAL)
        search_sizer_four.Add(basic_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_four.Add(
            self.basic_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_four.Add(extended_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_four.Add(
            self.extended_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_four.Add(stock_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_four.Add(
            self.assert_stock_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        search_sizer_five = wx.BoxSizer(wx.VERTICAL)
        search_sizer_five.Add(
            help_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_five.AddSpacer(80)
        search_sizer_five.Add(
            self.search_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        help_button.SetBitmap(
            loadBitmapScaled(
                "mdi-help-circle-outline.png",
                self.parent.scale_factor,
            )
        )
        help_button.SetBitmapMargins((2, 0))

        self.search_button.SetBitmap(
            loadBitmapScaled(
                "mdi-database-search-outline.png",
                self.parent.scale_factor,
            )
        )
        self.search_button.SetBitmapMargins((2, 0))

        search_sizer = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Search")
        search_sizer.Add(search_sizer_one, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_two, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_three, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_four, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_five, 0, wx.RIGHT, 20)
        # search_sizer.Add(help_button, 0, wx.RIGHT, 20)

        self.keyword.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.manufacturer.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.package.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.category.Bind(wx.EVT_COMBOBOX, self.update_subcategories)
        self.category.Bind(wx.EVT_TEXT, self.update_subcategories)
        self.category.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.part_no.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.solder_joints.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.search_button.Bind(wx.EVT_BUTTON, self.search)
        help_button.Bind(wx.EVT_BUTTON, self.help)

        # ---------------------------------------------------------------------
        # ------------------------ Result status line -------------------------
        # ---------------------------------------------------------------------

        self.result_count = wx.StaticText(
            self, wx.ID_ANY, "0 Results", wx.DefaultPosition, wx.DefaultSize
        )

        result_sizer = wx.BoxSizer(wx.HORIZONTAL)
        result_sizer.Add(self.result_count, 0, wx.LEFT | wx.TOP, 5)

        # ---------------------------------------------------------------------
        # ------------------------- Result Part list --------------------------
        # ---------------------------------------------------------------------

        self.part_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_SINGLE,
        )

        reference = self.part_list.AppendTextColumn(
            "LCSC",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 80),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        number = self.part_list.AppendTextColumn(
            "MFR Number",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 140),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        package = self.part_list.AppendTextColumn(
            "Package",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        joints = self.part_list.AppendTextColumn(
            "Joints",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 40),
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        type = self.part_list.AppendTextColumn(
            "Type",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 80),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        manufacturer = self.part_list.AppendTextColumn(
            "Manufacturer",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 140),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        description = self.part_list.AppendTextColumn(
            "Description",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 300),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        price = self.part_list.AppendTextColumn(
            "Price",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        stock = self.part_list.AppendTextColumn(
            "Stock",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 50),
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )

        self.part_list.SetMinSize(HighResWxSize(parent.window, wx.Size(1050, 500)))

        self.part_list.Bind(
            wx.dataview.EVT_DATAVIEW_COLUMN_HEADER_CLICK, self.OnSortPartList
        )

        self.part_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.OnPartSelected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(parent.window, wx.Size(-1, 400)))
        table_sizer.Add(self.part_list, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.select_part_button = wx.Button(
            self,
            wx.ID_ANY,
            "Select part",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.part_details_button = wx.Button(
            self,
            wx.ID_ANY,
            "Show part details",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )

        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)

        self.select_part_button.SetBitmap(
            loadBitmapScaled(
                "mdi-check.png",
                self.parent.scale_factor,
            )
        )
        self.select_part_button.SetBitmapMargins((2, 0))

        self.part_details_button.SetBitmap(
            loadBitmapScaled(
                "mdi-text-box-search-outline.png",
                self.parent.scale_factor,
            )
        )
        self.part_details_button.SetBitmapMargins((2, 0))

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 3, wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------------ Sizers  ------------------------------
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(search_sizer, 1, wx.ALL, 5)
        # layout.Add(self.search_button, 5, wx.ALL, 5)
        layout.Add(result_sizer, 1, wx.LEFT, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        self.enable_toolbar_buttons(False)

    def upadate_settings(self, state):
        """Update the settings on change"""
        self.settings["partselector"]["basic"] = self.basic_checkbox.GetValue()
        self.settings["partselector"]["extended"] = self.extended_checkbox.GetValue()
        self.settings["partselector"]["stock"] = self.assert_stock_checkbox.GetValue()
        self.save_settings()

    def load_settings(self):
        """Load settings from settings.json"""
        with open(os.path.join(PLUGIN_PATH, "settings.json")) as j:
            self.logger.debug("Load settings")
            self.settings = json.load(j)
            self.logger.debug(self.settings)

    def save_settings(self):
        """Save settings to settings.json"""
        self.logger.debug("Save settings")
        with open(os.path.join(PLUGIN_PATH, "settings.json"), "w") as j:
            json.dump(self.settings, j)

    @staticmethod
    def get_existing_selection(parts):
        """Check if exactly one LCSC part number is amongst the selected parts."""
        s = set(val for val in parts.values())
        if len(s) != 1:
            return ""
        else:
            return list(s)[0]

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def OnSortPartList(self, e):
        """Set order_by to the clicked column and trigger list refresh."""
        self.parent.library.set_order_by(e.GetColumn())
        self.search(None)

    def OnPartSelected(self, e):
        """Enable the toolbar buttons when a selection was made."""
        if self.part_list.GetSelectedItemsCount() > 0:
            self.enable_toolbar_buttons(True)
        else:
            self.enable_toolbar_buttons(False)

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side"""
        for b in [
            self.select_part_button,
            self.part_details_button,
        ]:
            b.Enable(bool(state))

    def search(self, e):
        """Search the library for parts that meet the search criteria."""
        parameters = {
            "keyword": self.keyword.GetValue(),
            "manufacturer": self.manufacturer.GetValue(),
            "package": self.package.GetValue(),
            "category": self.category.GetValue(),
            "subcategory": self.subcategory.GetValue(),
            "part_no": self.part_no.GetValue(),
            "solder_joints": self.solder_joints.GetValue(),
            "basic": self.basic_checkbox.GetValue(),
            "extended": self.extended_checkbox.GetValue(),
            "stock": self.assert_stock_checkbox.GetValue(),
        }
        result = self.parent.library.search(parameters)
        self.populate_part_list(result)

    def update_subcategories(self, e):
        """Update the possible subcategory selection."""
        self.subcategory.Clear()
        if self.category.GetSelection() != wx.NOT_FOUND:
            subcategories = self.parent.library.get_subcategories(
                self.category.GetValue()
            )
            self.subcategory.AppendItems(subcategories)

    def populate_part_list(self, parts):
        """Populate the list with the result of the search."""
        self.part_list.DeleteAllItems()
        if parts is None:
            return
        count = len(parts)
        if count == 1000:
            self.result_count.SetLabel(f"{count} Results (limited)")
        else:
            self.result_count.SetLabel(f"{count} Results")
        for p in parts:
            self.part_list.AppendItem([str(c) for c in p])

    def select_part(self, e):
        """Save the selected part number and close the modal."""
        item = self.part_list.GetSelection()
        row = self.part_list.ItemToRow(item)
        if row == -1:
            return
        selection = self.part_list.GetTextValue(row, 0)
        stock = self.part_list.GetTextValue(row, 8)
        wx.PostEvent(
            self.parent,
            AssignPartsEvent(
                lcsc=selection,
                stock=stock,
                references=self.parts.keys(),
            ),
        )
        self.EndModal(wx.ID_OK)

    def get_part_details(self, e):
        """Fetch part details from LCSC and show them in a modal."""
        item = self.part_list.GetSelection()
        row = self.part_list.ItemToRow(item)
        if row == -1:
            return
        part = self.part_list.GetTextValue(row, 0)
        if part != "":
            self.busy_cursor = wx.BusyCursor()
            dialog = PartDetailsDialog(self.parent, part)
            del self.busy_cursor
            dialog.ShowModal()

    def help(self, e):
        """Show message box with help instructions"""
        title = "Help"
        text = """
        Use % as wildcard selector. \n
        For example DS24% will match DS2411\n
        %QFP% wil match LQFP-64 as well as TQFP-32\n
        The keyword search box is automatically post- and prefixed with wildcard operators.
        The others are not by default.\n
        The keyword search field is applied to "LCSC Part", "Description", "MFR.Part",
        "Package" and "Manufacturer".\n
        Enter triggers the search the same way the search button does.\n
        The results are limited to 1000.
        """
        wx.MessageBox(text, title, style=wx.ICON_INFORMATION)
