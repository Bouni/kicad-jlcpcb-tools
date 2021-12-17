import logging
import os
from sys import path

import wx

from .events import AssignPartEvent
from .helpers import PLUGIN_PATH
from .partdetails import PartDetailsDialog


class PartSelectorDialog(wx.Dialog):
    def __init__(self, parent, parts):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB Library",
            pos=wx.DefaultPosition,
            size=wx.Size(1300, 800),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.parts = parts
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

        keyword_label = wx.StaticText(self, wx.ID_ANY, "Keyword", size=(150, 15))
        self.keyword = wx.TextCtrl(
            self,
            wx.ID_ANY,
            lcsc_selection,
            wx.DefaultPosition,
            (200, 24),
            wx.TE_PROCESS_ENTER,
        )
        self.keyword.SetHint("e.g. 10k 0603")

        manufacturer_label = wx.StaticText(
            self, wx.ID_ANY, "Manufacturer", size=(150, 15)
        )
        self.manufacturer = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            (200, 24),
            wx.TE_PROCESS_ENTER,
        )
        self.manufacturer.SetHint("e.g. Vishay")

        package_label = wx.StaticText(self, wx.ID_ANY, "Package", size=(150, 15))
        self.package = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            (200, 24),
            wx.TE_PROCESS_ENTER,
        )
        self.package.SetHint("e.g. 0603")

        category_label = wx.StaticText(self, wx.ID_ANY, "Category", size=(150, 15))
        self.category = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            (200, 24),
            wx.TE_PROCESS_ENTER,
        )
        self.category.SetHint("e.g. Resistor")

        part_no_label = wx.StaticText(self, wx.ID_ANY, "Part number", size=(150, 15))
        self.part_no = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            (200, 24),
            wx.TE_PROCESS_ENTER,
        )
        self.part_no.SetHint("e.g. DS2411")

        solder_joints_label = wx.StaticText(
            self, wx.ID_ANY, "Solder joints", size=(150, 15)
        )
        self.solder_joints = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            (200, 24),
            wx.TE_PROCESS_ENTER,
        )
        self.solder_joints.SetHint("e.g. 2")

        basic_label = wx.StaticText(
            self, wx.ID_ANY, "Include basic parts", size=(150, 15)
        )
        self.basic_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Basic", wx.DefaultPosition, (200, 24), 0
        )
        extended_label = wx.StaticText(
            self, wx.ID_ANY, "Include extended parts", size=(150, 15)
        )
        self.extended_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "Extended", wx.DefaultPosition, (200, 24), 0
        )
        stock_label = wx.StaticText(
            self, wx.ID_ANY, "Only show parts in stock", size=(150, 15)
        )
        self.assert_stock_checkbox = wx.CheckBox(
            self, wx.ID_ANY, "in Stock", wx.DefaultPosition, (200, 24), 0
        )

        self.basic_checkbox.SetValue(True)
        self.extended_checkbox.SetValue(True)

        help_button = wx.Button(
            self,
            wx.ID_ANY,
            "Help",
            wx.DefaultPosition,
            (100, -1),
            0,
        )

        self.search_button = wx.Button(
            self,
            wx.ID_ANY,
            "Search",
            wx.DefaultPosition,
            (100, -1),
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
        search_sizer_three.Add(basic_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_three.Add(
            self.basic_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_three.Add(extended_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_three.Add(
            self.extended_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_three.Add(stock_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        search_sizer_three.Add(
            self.assert_stock_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        search_sizer_four = wx.BoxSizer(wx.VERTICAL)
        search_sizer_four.Add(
            help_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )
        search_sizer_four.AddSpacer(80)
        search_sizer_four.Add(
            self.search_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL,
            5,
        )

        help_icon = wx.Bitmap(
            os.path.join(PLUGIN_PATH, "icons", "mdi-help-circle-outline.png")
        )
        help_button.SetBitmap(help_icon)
        help_button.SetBitmapMargins((2, 0))

        select_icon = wx.Bitmap(
            os.path.join(PLUGIN_PATH, "icons", "mdi-database-search-outline.png")
        )
        self.search_button.SetBitmap(select_icon)
        self.search_button.SetBitmapMargins((2, 0))

        search_sizer = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Search")
        search_sizer.Add(search_sizer_one, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_two, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_three, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_four, 0, wx.RIGHT, 20)
        # search_sizer.Add(help_button, 0, wx.RIGHT, 20)

        self.keyword.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.manufacturer.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.package.Bind(wx.EVT_TEXT_ENTER, self.search)
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
            width=80,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        number = self.part_list.AppendTextColumn(
            "MFR Number",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=140,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        package = self.part_list.AppendTextColumn(
            "Package",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        joints = self.part_list.AppendTextColumn(
            "Joints",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=40,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        type = self.part_list.AppendTextColumn(
            "Type",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=80,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        manufacturer = self.part_list.AppendTextColumn(
            "Manufacturer",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=140,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        decription = self.part_list.AppendTextColumn(
            "Description",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=300,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        price = self.part_list.AppendTextColumn(
            "Price",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=100,
            align=wx.ALIGN_LEFT,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )
        stock = self.part_list.AppendTextColumn(
            "Stock",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=50,
            align=wx.ALIGN_CENTER,
            flags=wx.dataview.DATAVIEW_COL_RESIZABLE,
        )

        self.part_list.SetMinSize(wx.Size(1050, 500))

        self.part_list.Bind(
            wx.dataview.EVT_DATAVIEW_COLUMN_HEADER_CLICK, self.OnSortPartList
        )

        self.part_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.OnPartSelected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(wx.Size(-1, 400))
        table_sizer.Add(self.part_list, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.select_part_button = wx.Button(
            self, wx.ID_ANY, "Select part", wx.DefaultPosition, (150, -1), 0
        )
        self.part_details_button = wx.Button(
            self, wx.ID_ANY, "Show part details", wx.DefaultPosition, (150, -1), 0
        )

        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)

        check_icon = wx.Bitmap(os.path.join(PLUGIN_PATH, "icons", "mdi-check.png"))
        self.select_part_button.SetBitmap(check_icon)
        self.select_part_button.SetBitmapMargins((2, 0))

        details_icon = wx.Bitmap(
            os.path.join(PLUGIN_PATH, "icons", "mdi-text-box-search-outline.png")
        )
        self.part_details_button.SetBitmap(details_icon)
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
        """Search the librery for parts that meet the search criteria."""
        parameters = {
            "keyword": self.keyword.GetValue(),
            "manufacturer": self.manufacturer.GetValue(),
            "package": self.package.GetValue(),
            "category": self.category.GetValue(),
            "part_no": self.part_no.GetValue(),
            "solder_joints": self.solder_joints.GetValue(),
            "basic": self.basic_checkbox.GetValue(),
            "extended": self.extended_checkbox.GetValue(),
            "stock": self.assert_stock_checkbox.GetValue(),
        }
        result = self.parent.library.search(parameters)
        self.populate_part_list(result)

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
        for reference in self.parts.keys():
            wx.PostEvent(
                self.parent,
                AssignPartEvent(
                    lcsc=selection,
                    stock=stock,
                    reference=reference,
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
            PartDetailsDialog(self, part).Show()

    def help(sefl, e):
        """Show message box with help instructions"""
        title = "Help"
        text = """
        Use % as wildcard selector. \n
        For example DS24% will match DS2411\n
        %QFP% wil match LQFP-64 as well as TQFP-32\n
        The keyword search box is automatically post- and prefixed with wildcard operators.
        The others are not by default.\n
        The keyowrd search field is applied to "LCSC Part", "Description", "MFR.Part",
        "Package" and "Manufacturer".\n
        Enter triggers the search the same way the search button does.\n
        The results are limited to 1000.
        """
        wx.MessageBox(text, title, style=wx.ICON_INFORMATION)
