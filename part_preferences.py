"""Manage reusable LCSC part preferences shared across projects."""

import csv
import logging
import os
from typing import TYPE_CHECKING

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

from .helpers import HighResWxSize, loadBitmapScaled

if TYPE_CHECKING:
    from .mainwindow import JLCPCBTools


class PartPreferencesDialog(wx.Dialog):
    """Dialog for managing preferred LCSC parts by footprint and value."""

    def __init__(self, parent: "JLCPCBTools") -> None:
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Part preferences",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(800, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent

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

        self.parent.library.create_mapping_table()

        # ---------------------------------------------------------------------
        # ------------------------- Part preferences list ----------------------------
        # ---------------------------------------------------------------------

        self.part_preferences_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_MULTIPLE,
        )

        self.part_preferences_list.AppendTextColumn(
            "Footprint",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 150),
            align=wx.ALIGN_LEFT,
        )
        self.part_preferences_list.AppendTextColumn(
            "Value",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )
        self.part_preferences_list.AppendTextColumn(
            "LCSC Part",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )

        self.part_preferences_list.SetMinSize(
            HighResWxSize(parent.window, wx.Size(600, 500))
        )

        self.part_preferences_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.on_part_preference_selected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(parent.window, wx.Size(-1, 400)))
        table_sizer.Add(self.part_preferences_list, 20, wx.ALL | wx.EXPAND, 5)

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

        self.delete_button.Bind(wx.EVT_BUTTON, self.delete_selected_part_preferences)
        self.delete_button.SetToolTip(
            "Remove selected preferences without changing assignments on your boards."
        )
        self.import_button.Bind(wx.EVT_BUTTON, self.import_part_preferences_dialog)
        self.export_button.Bind(wx.EVT_BUTTON, self.export_part_preferences_dialog)

        self.delete_button.SetBitmap(
            loadBitmapScaled(
                "mdi-trash-can-outline.png",
                self.parent.scale_factor,
            )
        )
        self.delete_button.SetBitmapMargins((2, 0))

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
        tool_sizer.Add(self.delete_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.import_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.export_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 3, wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------------ Sizers  ------------------------------
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        description = wx.StaticText(
            self,
            label="Preferred LCSC parts for matching values and footprints, shared across projects.",
        )
        description.Wrap(HighResWxSize(parent.window, wx.Size(650, -1)).width)
        layout.Add(description, 0, wx.ALL | wx.EXPAND, 10)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        self.enable_toolbar_buttons(False)
        self.populate_part_preferences_list()

    def quit_dialog(self, *_: object) -> None:
        """Close this dialog."""
        self.Destroy()
        self.EndModal(0)

    def enable_toolbar_buttons(self, state: bool) -> None:
        """Control the state of all the buttons in toolbar on the right side."""
        for b in [
            self.delete_button,
        ]:
            b.Enable(bool(state))

    def populate_part_preferences_list(self) -> None:
        """Populate the list with all shared part preferences."""
        self.part_preferences_list.DeleteAllItems()

        if self.parent.library.get_all_mapping_data() is None:
            self.logger.info("empty")
            return

        for part_preference in self.parent.library.get_all_mapping_data():
            self.part_preferences_list.AppendItem(
                [str(field) for field in part_preference]
            )

    def delete_selected_part_preferences(self, *_: object) -> None:
        """Delete the selected part preferences from the shared database."""
        for item in self.part_preferences_list.GetSelections():
            row = self.part_preferences_list.ItemToRow(item)
            if row == -1:
                return
            footprint = self.part_preferences_list.GetTextValue(row, 0)
            value = self.part_preferences_list.GetTextValue(row, 1)
            self.parent.library.delete_mapping_data(footprint, value)
        self.populate_part_preferences_list()

    def on_part_preference_selected(self, *_: object) -> None:
        """Enable the toolbar buttons when a selection was made."""
        if self.part_preferences_list.GetSelectedItemsCount() > 0:
            self.enable_toolbar_buttons(True)
        else:
            self.enable_toolbar_buttons(False)

    def import_part_preferences_dialog(self, *_: object) -> None:
        """Choose a CSV file containing part preferences to import."""
        with wx.FileDialog(
            self,
            "Import part preferences CSV",
            "",
            "",
            "CSV files (*.csv)|*.csv",
            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as importFileDialog:
            if importFileDialog.ShowModal() == wx.ID_CANCEL:
                return
            path = importFileDialog.GetPath()
            self._import_part_preferences(path)

    def export_part_preferences_dialog(self, *_: object) -> None:
        """Choose a CSV file to export shared part preferences to."""
        with wx.FileDialog(
            self,
            "Export part preferences CSV",
            "",
            "part-preferences",
            "CSV files (*.csv)|*.csv",
            wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as exportFileDialog:
            if exportFileDialog.ShowModal() == wx.ID_CANCEL:
                return
            path = exportFileDialog.GetPath()
            self._export_part_preferences(path)

    def _import_part_preferences(self, path: str) -> None:
        """Import part preferences from a CSV file."""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                csvreader = csv.DictReader(f, fieldnames=("footprint", "value", "lcsc"))
                next(csvreader)
                for row in csvreader:
                    if self.parent.library.get_mapping_data(
                        row["footprint"], row["value"]
                    ):
                        self.parent.library.update_mapping_data(
                            row["footprint"], row["value"], row["lcsc"]
                        )
                    else:
                        self.parent.library.insert_mapping_data(
                            row["footprint"], row["value"], row["lcsc"]
                        )
            self.populate_part_preferences_list()

    def _export_part_preferences(self, path: str) -> None:
        """Export shared part preferences to a CSV file."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            csvwriter = csv.writer(f, quotechar='"', quoting=csv.QUOTE_ALL)
            csvwriter.writerow(["Footprint", "Part Value", "LCSC Part"])
            for m in self.parent.library.get_all_mapping_data():
                csvwriter.writerow([m[0], m[1], m[2]])
