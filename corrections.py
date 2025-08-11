"""Contains the corrections manager."""

import csv
import logging
import os

import requests  # pylint: disable=import-error
import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

from .events import PopulateFootprintListEvent
from .helpers import PLUGIN_PATH, HighResWxSize, loadBitmapScaled


class CorrectionManagerDialog(wx.Dialog):
    """Dialog for managing part corrections."""

    def __init__(self, parent, footprint):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="Corrections Manager",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(800, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self.selection_regex = None
        self.selection_rotation = None
        self.selection_offset_x = None
        self.selection_offset_y = None
        self.import_legacy_corrections()

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
        # ------------------------- Add/Edit inputs ---------------------------
        # ---------------------------------------------------------------------

        regex_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Regex",
            size=HighResWxSize(parent.window, wx.Size(150, 15)),
        )
        self.regex = wx.TextCtrl(
            self,
            wx.ID_ANY,
            footprint,
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, 24)),
        )

        sizer_regex = wx.BoxSizer(wx.VERTICAL)
        sizer_regex.Add(regex_label, 0, wx.ALL, 5)
        sizer_regex.Add(
            self.regex,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        rotation_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Rotation",
            size=HighResWxSize(parent.window, wx.Size(100, 15)),
        )
        self.rotation = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "0",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, 24)),
        )

        sizer_rotation = wx.BoxSizer(wx.VERTICAL)
        sizer_rotation.Add(rotation_label, 0, wx.ALL, 5)
        sizer_rotation.Add(
            self.rotation,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        offset_x_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Offset X",
            size=HighResWxSize(parent.window, wx.Size(100, 15)),
        )
        self.offset_x = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "0.00",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, 24)),
        )

        sizer_offset_x = wx.BoxSizer(wx.VERTICAL)
        sizer_offset_x.Add(offset_x_label, 0, wx.ALL, 5)
        sizer_offset_x.Add(
            self.offset_x,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        offset_y_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Offset Y",
            size=HighResWxSize(parent.window, wx.Size(100, 15)),
        )
        self.offset_y = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "0.00",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, 24)),
        )

        sizer_offset_y = wx.BoxSizer(wx.VERTICAL)
        sizer_offset_y.Add(offset_y_label, 0, wx.ALL, 5)
        sizer_offset_y.Add(
            self.offset_y,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        self.regex.Bind(wx.EVT_TEXT, self.on_textfield_change)
        self.rotation.Bind(wx.EVT_TEXT, self.on_textfield_change)
        self.offset_x.Bind(wx.EVT_TEXT, self.on_textfield_change)
        self.offset_y.Bind(wx.EVT_TEXT, self.on_textfield_change)

        add_edit_sizer = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Add / Edit")
        add_edit_sizer.Add(sizer_regex, 0, wx.RIGHT, 20)
        add_edit_sizer.Add(sizer_rotation, 0, wx.RIGHT, 20)
        add_edit_sizer.Add(sizer_offset_x, 0, wx.RIGHT, 20)
        add_edit_sizer.Add(sizer_offset_y, 0, wx.RIGHT, 20)

        # ---------------------------------------------------------------------
        # ------------------------ Corrections list ---------------------------
        # ---------------------------------------------------------------------

        self.corrections_list = wx.dataview.DataViewListCtrl(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            style=wx.dataview.DV_MULTIPLE,
        )

        self.corrections_list.AppendTextColumn(
            "Regex",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 280),
            align=wx.ALIGN_LEFT,
        )
        self.corrections_list.AppendTextColumn(
            "Rotation",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )
        self.corrections_list.AppendTextColumn(
            "Offset X",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )
        self.corrections_list.AppendTextColumn(
            "Offset Y",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 100),
            align=wx.ALIGN_LEFT,
        )

        self.corrections_list.SetMinSize(
            HighResWxSize(parent.window, wx.Size(600, 500))
        )

        self.corrections_list.Bind(
            wx.dataview.EVT_DATAVIEW_SELECTION_CHANGED, self.on_correction_selected
        )

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(parent.window, wx.Size(-1, 400)))
        table_sizer.Add(self.corrections_list, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.save_button = wx.Button(
            self,
            wx.ID_ANY,
            "Save",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.delete_button = wx.Button(
            self,
            wx.ID_ANY,
            "Delete",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.update_button = wx.Button(
            self,
            wx.ID_ANY,
            "Update",
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

        self.save_button.Bind(wx.EVT_BUTTON, self.save_correction)
        self.delete_button.Bind(wx.EVT_BUTTON, self.delete_correction)
        self.update_button.Bind(wx.EVT_BUTTON, self.download_correction_data)
        self.import_button.Bind(wx.EVT_BUTTON, self.import_corrections_dialog)
        self.export_button.Bind(wx.EVT_BUTTON, self.export_corrections_dialog)

        self.save_button.SetBitmap(
            loadBitmapScaled(
                "mdi-content-save-outline.png",
                self.parent.scale_factor,
            )
        )
        self.save_button.SetBitmapMargins((2, 0))

        self.delete_button.SetBitmap(
            loadBitmapScaled(
                "mdi-trash-can-outline.png",
                self.parent.scale_factor,
            )
        )
        self.delete_button.SetBitmapMargins((2, 0))

        self.update_button.SetBitmap(
            loadBitmapScaled(
                "mdi-cloud-download-outline.png",
                self.parent.scale_factor,
            )
        )
        self.update_button.SetBitmapMargins((2, 0))

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

        self.global_corrections = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Use global corrections",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="corrections_global_corrections",
        )

        self.global_corrections.SetToolTip(
            wx.ToolTip(
                "Whether the global corrections database is used or a project local one"
            )
        )
        self.global_corrections.Bind(
            wx.EVT_CHECKBOX, self.on_global_corrections_changed
        )
        self.global_corrections.SetValue(
            self.parent.library.uses_global_correction_database()
        )

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.save_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.delete_button, 0, wx.ALL, 5)
        tool_sizer.AddStretchSpacer()
        tool_sizer.Add(self.update_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.import_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.export_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.global_corrections, 0, wx.ALL, 5)

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
        self.enable_toolbar_buttons()
        self.populate_corrections_list()

    def quit_dialog(self, *_):
        """Close this dialog."""
        self.Destroy()
        self.EndModal(0)

    def enable_toolbar_buttons(self):
        """Control the state of all the buttons in toolbar on the right side."""
        if (
            self.regex.GetValue()
            and self.rotation.GetValue()
            and self.offset_x.GetValue()
            and self.offset_y.GetValue()
        ):
            self.save_button.Enable(True)
        else:
            self.save_button.Enable(False)

        if self.corrections_list.GetSelectedRow() != wx.NOT_FOUND:
            self.delete_button.Enable(True)
        else:
            self.delete_button.Enable(False)

    def to_float(self, value):
        """Convert the given value to a float, return 0 if convertion fails."""
        try:
            return float(value)
        except ValueError:
            return 0

    def str_from_float(self, value):
        """Convert the given floating point value to a string.

        Us as many decimal digits as required but for small numbers
        at least two decimal digits are used.
        """
        s = str(value)
        return f"{value:.2f}" if len(s) < 4 else s

    def populate_corrections_list(self):
        """Populate the list with the result of the search."""
        self.corrections_list.DeleteAllItems()
        for regex, rotation, offset in self.parent.library.get_all_correction_data():
            self.corrections_list.AppendItem(
                [
                    str(regex),
                    str(rotation),
                    self.str_from_float(offset[0]),
                    self.str_from_float(offset[1])
                ]
            )
        selected_row = None
        if self.selection_regex is not None:
            for row in range(self.corrections_list.GetItemCount()):
                row_regex = self.corrections_list.GetTextValue(row, 0)
                if row_regex == self.selection_regex:
                    selected_row = row
            if selected_row is not None:
                self.corrections_list.SelectRow(selected_row)

    def save_correction(self, *_):
        """Add/Update a correction in the database."""
        regex = self.regex.GetValue()
        rotation = int(self.to_float(self.rotation.GetValue()))
        offset_x = self.to_float(self.offset_x.GetValue())
        offset_y = self.to_float(self.offset_y.GetValue())
        offset = (offset_x, offset_y)
        if regex == self.selection_regex:
            # the regex of the selection was not changed, just update values.
            self.parent.library.update_correction_data(regex, rotation, offset)
        else:
            # regex was modified or nothing was selected.
            # Check if there is a existing rule for that regex
            row_of_that_regex = None
            for row in range(self.corrections_list.GetItemCount()):
                row_regex = self.corrections_list.GetTextValue(row, 0)
                if row_regex == regex:
                    row_of_that_regex = row

            if row_of_that_regex is None:
                # the regex is a new one, just create it or update the selected entry

                if self.selection_regex is not None:
                    # remove old line, if one existed
                    self.parent.library.delete_correction_data(self.selection_regex)

                # Add the modified regex and values
                self.parent.library.insert_correction_data(regex, rotation, offset)
                self.selection_regex = regex
            else:
                # The regex already exists.
                existing_rotation = int(
                    self.to_float(self.corrections_list.GetTextValue(row, 1))
                )
                existing_offset_x = self.to_float(
                    self.corrections_list.GetTextValue(row, 2)
                )
                existing_offset_y = self.to_float(
                    self.corrections_list.GetTextValue(row, 3)
                )

                if (
                    rotation == existing_rotation
                    and offset_x == existing_offset_x
                    and offset_y == existing_offset_y
                ):
                    # User entered a regex that already exists, just select that one
                    self.selection_regex = regex
                else:
                    # The regex exists with different values, ask the user what to do.
                    existing_correction = "(" + \
                        str(existing_rotation) + "°, " + \
                        self.str_from_float(existing_offset_x) + "/" + \
                        self.str_from_float(existing_offset_y) + \
                        ")"
                    new_correction = "(" + \
                        str(rotation) + "°, " + \
                        self.str_from_float(offset_x) + "/" + \
                        self.str_from_float(offset_y) + \
                        ")"

                    dialog = wx.MessageDialog(
                        self,
                        f"A rule for '{regex}' already exists!",
                        "Regex exists!",
                        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    )
                    if self.selection_regex is None:
                        # The user entered a regex that already exists with different values.
                        dialog.ExtendedMessage = "Do you want to update the corrections " + \
                           existing_correction + \
                           " to " + \
                           new_correction
                    else:
                        # The user has selected regex_a, changed it to regex_b
                        # (but regex_b exists).
                        dialog.ExtendedMessage = "Do you want to replace the corrections " + \
                           existing_correction + \
                           " with " + \
                           new_correction + \
                           ",\n" + \
                           f"removing the rule for '{self.selection_regex}'?"
                    result = dialog.ShowModal()

                    if result == wx.ID_YES:
                        if self.selection_regex is not None:
                            self.parent.library.delete_correction_data(self.selection_regex)
                        self.parent.library.update_correction_data(regex, rotation, offset)
                        self.selection_regex = regex

        self.rotation.SetValue(str(rotation))
        self.offset_x.SetValue(self.str_from_float(offset_x))
        self.offset_y.SetValue(self.str_from_float(offset_y))
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def delete_correction(self, *_):
        """Delete a correction from the database."""
        item = self.corrections_list.GetSelection()
        row = self.corrections_list.ItemToRow(item)
        if row == -1:
            return
        regex = self.corrections_list.GetTextValue(row, 0)
        self.parent.library.delete_correction_data(regex)
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def on_correction_selected(self, event):
        """Enable the toolbar buttons when a selection was made."""
        if len(self.corrections_list.GetSelections()) > 1:
            for item in self.corrections_list.GetSelections():
                if item != event.GetItem():
                    self.corrections_list.Unselect(item)

        if self.corrections_list.GetSelectedItemsCount() > 0:
            item = self.corrections_list.GetSelection()
            row = self.corrections_list.ItemToRow(item)
            if row == -1:
                return

            self.selection_regex = self.corrections_list.GetTextValue(row, 0)
            self.selection_rotation = int(
                self.to_float(self.corrections_list.GetTextValue(row, 1))
            )
            self.selection_offset_x = self.to_float(
                self.corrections_list.GetTextValue(row, 2)
            )
            self.selection_offset_y = self.to_float(
                self.corrections_list.GetTextValue(row, 3)
            )
            self.regex.SetValue(self.selection_regex)
            self.rotation.SetValue(str(self.selection_rotation))
            self.offset_x.SetValue(self.str_from_float(self.selection_offset_x))
            self.offset_y.SetValue(self.str_from_float(self.selection_offset_y))
        else:
            self.selection_row = None
            self.selection_regex = None

        self.enable_toolbar_buttons()

    def on_textfield_change(self, *_):
        """Check if the texfield change affects toolbars."""
        self.enable_toolbar_buttons()

    def on_global_corrections_changed(self, use_global):
        """Switch between global or local correction database file."""
        if self.parent.library.uses_global_correction_database():
            dialog = wx.MessageDialog(
                self,
                "Do you want to switch to the local corrections database?",
                "Switching corrections database",
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION,
            )
            dialog.ExtendedMessage = "Switching to a board local database copies the current global database."
        else:
            dialog = wx.MessageDialog(
                self,
                "Do you want to switch to the global corrections database?",
                "Switching corrections database",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            )
        result = dialog.ShowModal()

        if result == wx.ID_NO:
            self.global_corrections.SetValue(
                self.parent.library.uses_global_correction_database()
            )
            return

        self.parent.library.switch_to_global_correction_database(
            not self.parent.library.uses_global_correction_database()
        )
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())
        self.global_corrections.SetValue(
            self.parent.library.uses_global_correction_database()
        )

    def download_correction_data(self, *_):
        """Fetch the latest rotation correction table from Matthew Lai's JLCKicadTool repo."""
        self.parent.library.create_correction_table()
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/matthewlai/JLCKicadTools/master/jlc_kicad_tools/cpl_rotations_db.csv",
                timeout=5,
            )
            corrections = csv.reader(r.text.splitlines(), delimiter=",", quotechar='"')
            next(corrections)
            for row in corrections:
                if not self.parent.library.get_correction_data(row[0]):
                    if len(row) >= 4:
                        self.parent.library.insert_correction_data(
                            row[0], row[1], (row[2], row[3])
                        )
                    else:
                        self.parent.library.insert_correction_data(
                            row[0], row[1], (0, 0)
                        )
                else:
                    self.logger.info(
                        "Correction '%s' exists already in database. Leaving this one out.",
                        row[0],
                    )
        except Exception as err:  # pylint: disable=broad-exception-caught
            self.logger.debug(err)
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def import_legacy_corrections(self):
        """Check if corrections in CSV format are found and import them into the database."""
        csv_file = os.path.join(PLUGIN_PATH, "corrections", "cpl_rotations_db.csv")
        if os.path.isfile(csv_file):
            self._import_corrections(csv_file)
            os.rename(csv_file, f"{csv_file}.backup")

    def import_corrections_dialog(self, *_):
        """Dialog to import correctios from a CSV file."""
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
            self._import_corrections(path)

    def export_corrections_dialog(self, *_):
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
            self._export_corrections(path)

    def _import_corrections(self, path):
        """Corrections import logic."""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                csvreader = csv.DictReader(
                    f, fieldnames=("regex", "rotation", "offset_x", "offset_y")
                )
                next(csvreader)
                for row in csvreader:
                    if "regex" in row and row["regex"] is not None:
                        regex = row["regex"]
                        rotation = row["rotation"] if row["rotation"] is not None else 0
                        offset_x = row["offset_x"] if row["offset_x"] is not None else 0
                        offset_y = row["offset_y"] if row["offset_y"] is not None else 0
                        existing_data = self.parent.library.get_correction_data(regex)
                        if existing_data:
                            self.parent.library.update_correction_data(
                                regex, rotation, (offset_x, offset_y)
                            )
                            self.logger.info(
                                "Correction '%s' exists already in database with correction value '%s, %s/%s'. Overwrite it with local values from CSV (%s, %s/%s).",
                                regex,
                                existing_data[1],
                                existing_data[2],
                                existing_data[3],
                                rotation,
                                offset_x,
                                offset_y,
                            )
                        else:
                            self.parent.library.insert_correction_data(
                                regex, rotation, (offset_x, offset_y)
                            )
                            self.logger.info(
                                "Correction '%s' with correction value '%s, %s/%s' is added to the database from local CSV.",
                                regex,
                                rotation,
                                offset_x,
                                offset_y,
                            )
            self.populate_corrections_list()
            wx.PostEvent(self.parent, PopulateFootprintListEvent())

    def _export_corrections(self, path):
        """Corrections export logic."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            csvwriter = csv.writer(f, quotechar='"', quoting=csv.QUOTE_ALL)
            csvwriter.writerow(["Pattern", "Rotation", "Offset X", "Offset Y"])
            for (
                regex,
                rotation,
                offset,
            ) in self.parent.library.get_all_correction_data():
                csvwriter.writerow([regex, rotation, offset[0], offset[1]])
