import logging
import os
from sys import path

import wx

from .partdetails import PartDetailsDialog


class PartSelectorDialog(wx.Dialog):
    def __init__(self, parent, lcsc_selection=""):
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

        # self.SetSizeHints(wx.Size(1200, -1), wx.DefaultSize)

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
            lcsc_selection,
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

        help_text = wx.StaticText(
            self,
            wx.ID_ANY,
            "Help\n\n"
            "Keyword applys to all fields, the others are only applied to the corresponding column.\n"
            "A blank textfield will be ignored.\n"
            "Use % as wildcard, e.g. %1117% to match AMS1117-5.0 and AMS1117-3.3.\n"
            "ENTER triggers the search.",
            size=(530, 100),
        )

        # self.search_button = wx.Button(
        #     self,
        #     wx.ID_ANY,
        #     "Search",
        #     wx.DefaultPosition,
        #     wx.DefaultSize,
        #     0,
        # )
        # self.basic_checkbox = wx.CheckBox(
        #     self, wx.ID_ANY, "Basic", wx.DefaultPosition, wx.DefaultSize, 0
        # )
        # self.extended_checkbox = wx.CheckBox(
        #     self, wx.ID_ANY, "Extended", wx.DefaultPosition, wx.DefaultSize, 0
        # )
        # self.assert_stock_checkbox = wx.CheckBox(
        #     self, wx.ID_ANY, "in Stock", wx.DefaultPosition, wx.DefaultSize, 0
        # )

        # self.keyword.SetFocus()

        self.basic_checkbox.SetValue(True)
        self.extended_checkbox.SetValue(True)
        self.assert_stock_checkbox.SetValue(True)

        # self.keyword.Bind(wx.EVT_TEXT_ENTER, self.search)
        # self.search_button.Bind(wx.EVT_BUTTON, self.search)
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

        search_sizer = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Search")
        search_sizer.Add(search_sizer_one, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_two, 0, wx.RIGHT, 20)
        search_sizer.Add(search_sizer_three, 0, wx.RIGHT, 50)
        search_sizer.Add(help_text)

        # search_sizer.Add(self.keyword, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        # search_sizer.Add(self.search_button, 0, wx.ALL, 5)
        # search_sizer.Add(
        #     self.basic_checkbox, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8
        # )
        # search_sizer.Add(
        #     self.extended_checkbox, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8
        # )
        # search_sizer.Add(
        #     self.assert_stock_checkbox,
        #     0,
        #     wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
        #     8,
        # )

        # search_icon = wx.Bitmap(
        #     os.path.join(self.parent.plugin_path, "icons", "mdi-magnify.png")
        # )
        # self.search_button.SetBitmap(search_icon)
        # self.search_button.SetBitmapMargins((2, 0))

        # ---------------------------------------------------------------------
        # ------------------ Package / Manufacturer filters -------------------
        # ---------------------------------------------------------------------

        # package_filter_title = wx.StaticText(
        #     self,
        #     wx.ID_ANY,
        #     "Packages",
        #     wx.DefaultPosition,
        # )
        # package_filter_search = wx.TextCtrl(
        #     self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, (300, -1), 0
        # )
        # package_filter_search.SetHint("e.g. 0603")
        # self.package_filter_list = wx.ListBox(
        #     self,
        #     wx.ID_ANY,
        #     wx.DefaultPosition,
        #     (300, 100),
        #     choices=[],
        #     style=wx.LB_EXTENDED,
        # )
        # manufacturer_filter_title = wx.StaticText(
        #     self,
        #     wx.ID_ANY,
        #     "Manufacturers",
        #     wx.DefaultPosition,
        # )
        # manufacturer_filter_search = wx.TextCtrl(
        #     self, wx.ID_ANY, wx.EmptyString, wx.DefaultPosition, (300, -1), 0
        # )
        # manufacturer_filter_search.SetHint("e.g. Vishay")
        # self.manufacturer_filter_list = wx.ListBox(
        #     self,
        #     wx.ID_ANY,
        #     wx.DefaultPosition,
        #     (300, 100),
        #     choices=[],
        #     style=wx.LB_EXTENDED,
        # )

        # package_filter_search.Bind(wx.EVT_TEXT, self.OnPackageFilter)
        # manufacturer_filter_search.Bind(wx.EVT_TEXT, self.OnManufacturerFilter)

        # package_filter_layout = wx.BoxSizer(wx.VERTICAL)
        # package_filter_layout.Add(package_filter_title)
        # package_filter_layout.Add(
        #     package_filter_search, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, 5
        # )
        # package_filter_layout.Add(self.package_filter_list, 5, wx.EXPAND, 5)

        # manufacturer_filter_layout = wx.BoxSizer(wx.VERTICAL)
        # manufacturer_filter_layout.Add(manufacturer_filter_title)
        # manufacturer_filter_layout.Add(
        #     manufacturer_filter_search, 1, wx.TOP | wx.BOTTOM | wx.EXPAND, 5
        # )
        # manufacturer_filter_layout.Add(self.manufacturer_filter_list, 5, wx.EXPAND, 5)

        # filter_sizer = wx.BoxSizer(wx.HORIZONTAL)
        # filter_sizer.Add(package_filter_layout, 1, wx.RIGHT, 20)
        # filter_sizer.Add(manufacturer_filter_layout, 1)

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

        self.part_list.SetMinSize(wx.Size(1050, 500))

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

        check_icon = wx.Bitmap(
            os.path.join(self.parent.plugin_path, "icons", "mdi-check.png")
        )
        self.select_part_button.SetBitmap(check_icon)
        self.select_part_button.SetBitmapMargins((2, 0))

        details_icon = wx.Bitmap(
            os.path.join(
                self.parent.plugin_path, "icons", "mdi-text-box-search-outline.png"
            )
        )
        self.part_details_button.SetBitmap(details_icon)
        self.part_details_button.SetBitmapMargins((2, 0))

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 3, wx.EXPAND, 5)
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(search_sizer, 1, wx.ALL, 5)
        # layout.Add(filter_sizer, 5, wx.ALL, 5)
        layout.Add(result_sizer, 1, wx.LEFT, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()

        self.Centre(wx.BOTH)

        self.enable_toolbar_buttons(False)

    def quit_dialog(self, e):
        self.Destroy()
        self.EndModal(0)

    def OnPackageFilter(self, e):

        packages = [
            x[0] for x in self.parent.library.search_package(e.GetString().lower())
        ]
        self.logger.debug(packages)
        if packages != []:
            self.package_filter_list.Set(packages)
        else:
            self.package_filter_list.Set([""])

    #     search = e.GetString().lower()
    #     choices = [c for c in self.package_filter_choices if search in c.lower()]
    #     if choices != []:
    #         self.package_filter_list.Set(choices)
    #     else:
    #         self.package_filter_list.Set([""])

    def OnManufacturerFilter(self, e):
        pass

    #     search = e.GetString().lower()
    #     choices = [c for c in self.manufacturer_filter_choices if search in c.lower()]
    #     if choices != []:
    #         self.manufacturer_filter_list.Set(choices)
    #     else:
    #         self.manufacturer_filter_list.Set([""])

    def OnPartSelected(self, e):
        pass

    #     """Enable the toolbar buttons when a selection was made."""
    #     self.enable_toolbar_buttons(self.part_list.GetSelectedItemsCount() > 0)

    def enable_toolbar_buttons(self, state):
        pass

    #     """Control the state of all the buttons in toolbar on the right side"""
    #     for b in [
    #         self.select_part_button,
    #         self.part_details_button,
    #     ]:
    #         b.Enable(bool(state))

    def search(self, e):
        pass

    #     """Search the dataframe for the keyword."""
    #     if not self.library.loaded:
    #         self.load_library()

    #     filtered_packages = self.package_filter_list.GetStrings()
    #     packages = [
    #         filtered_packages[i] for i in self.package_filter_list.GetSelections()
    #     ]
    #     filtered_manufacturers = self.manufacturer_filter_list.GetStrings()
    #     manufacturers = [
    #         filtered_manufacturers[i]
    #         for i in self.manufacturer_filter_list.GetSelections()
    #     ]
    #     result = self.library.search(
    #         self.keyword.GetValue(),
    #         self.basic_checkbox.GetValue(),
    #         self.extended_checkbox.GetValue(),
    #         self.assert_stock_checkbox.GetValue(),
    #         packages,
    #         manufacturers,
    #     )
    #     self.result_count.SetLabel(f"{len(result)} Results")
    #     self.populate_part_list(result)

    def populate_part_list(self, parts):
        pass

    #     """Populate the list with the result of the search."""
    #     self.part_list.DeleteAllItems()
    #     if parts is None:
    #         return
    #     for p in parts:
    #         self.part_list.AppendItem([str(c) for c in p])

    def select_part(self, e):
        pass

    #     """Save the selected part number and close the modal."""
    #     item = self.part_list.GetSelection()
    #     row = self.part_list.ItemToRow(item)
    #     if row == -1:
    #         return
    #     self.selection = self.part_list.GetTextValue(row, 0)
    #     self.EndModal(wx.ID_OK)

    def get_part_details(self, e):
        pass

    #     """Fetch part details from LCSC and show them in a modal."""
    #     item = self.part_list.GetSelection()
    #     row = self.part_list.ItemToRow(item)
    #     if row == -1:
    #         return
    #     part = self.part_list.GetTextValue(row, 0)
    #     dialog = PartDetailsDialog(self, part)
    #     dialog.Show()
