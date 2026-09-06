"""Contains the corrections manager."""

from __future__ import annotations

from collections.abc import Sequence
import csv
import logging
import os
from typing import TYPE_CHECKING, Any

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

from .correction_data import (
    Correction,
    CorrectionDataError,
    parse_corrections_csv,
    validate_correction,
)
from .events import PopulateFootprintListEvent
from .helpers import PLUGIN_PATH, HighResWxSize, loadBitmapScaled

if TYPE_CHECKING:
    from .library import StoredCorrection


class CorrectionManagerDialog(wx.Dialog):
    """Dialog for managing part corrections."""

    def __init__(self, parent: Any, footprint: str) -> None:
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
        self.selected_record = None
        self.selection_db_path = None
        self.correction_snapshot = None
        self._populating = False

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
            style=wx.dataview.DV_SINGLE,
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

        self.corrections_list.AppendTextColumn(
            "Status",
            mode=wx.dataview.DATAVIEW_CELL_INERT,
            width=int(parent.scale_factor * 280),
            align=wx.ALIGN_LEFT,
        )
        self.correction_status = wx.StaticText(self, wx.ID_ANY, "")

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
        self.global_corrections.SetValue(self._uses_global_corrections())

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
        layout.Add(self.correction_status, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        self.enable_toolbar_buttons()
        if self._uses_global_corrections():
            self.parent.library.retry_correction_migrations()
        self.import_legacy_corrections()
        self.populate_corrections_list()

    def quit_dialog(self, *_: object) -> None:
        """Close this dialog."""
        self.Destroy()
        self.EndModal(0)

    def enable_toolbar_buttons(self) -> None:
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

    def _clear_selection(self) -> None:
        """Forget the stored row identity without changing unsaved input fields."""
        self.selected_record = None
        self.selection_db_path = None

    def _input_values(self) -> tuple[str, str, str, str]:
        """Read exact editor text to distinguish actual edits from stale display values."""
        return tuple(
            control.GetValue()
            for control in (self.regex, self.rotation, self.offset_x, self.offset_y)
        )

    @staticmethod
    def _record_editor_values(record: StoredCorrection) -> tuple[str, str, str, str]:
        """Present valid values consistently while preserving invalid original text."""
        if record.correction is not None:
            return record.correction.editor_values()
        return (
            str(record.pattern),
            str(record.rotation),
            str(record.offset[0]),
            str(record.offset[1]),
        )

    def _select_record(self, record: StoredCorrection, db_path: str) -> None:
        """Retain the exact stored record and synchronize the editor with it."""
        self.selected_record = record
        self.selection_db_path = db_path
        for control, value in zip(
            (self.regex, self.rotation, self.offset_x, self.offset_y),
            self._record_editor_values(record),
        ):
            control.SetValue(value)

    def populate_corrections_list(
        self, *, selected_rowid: int | None = None, preserve_inputs: bool = False
    ) -> None:
        """Refresh rows without granting stale unsaved edits a newer record identity."""
        snapshot = self.parent.library.read_correction_data()
        preserve_inputs = preserve_inputs or (
            selected_rowid is None
            and self.selected_record is not None
            and self._input_values() != self._record_editor_values(self.selected_record)
        )
        if selected_rowid is None and self.selected_record is not None:
            selected_rowid = self.selected_record.rowid
        self._populating = True
        try:
            self.corrections_list.DeleteAllItems()
            self.correction_snapshot = snapshot
            for index, record in enumerate(snapshot.rows):
                self.corrections_list.AppendItem(
                    [
                        *self._record_editor_values(record),
                        "; ".join(
                            f"{issue.field}: {issue.message}" for issue in record.issues
                        ),
                    ]
                )
                if record.rowid == selected_rowid and (
                    self.selected_record is None
                    or self.selection_db_path == snapshot.db_path
                ):
                    if not preserve_inputs:
                        self._select_record(record, snapshot.db_path)
                        self.corrections_list.SelectRow(index)
        finally:
            self._populating = False

        warning = f"Using {snapshot.scope} corrections: {snapshot.db_path}"
        if snapshot.issues:
            if any(row.issues for row in snapshot.rows):
                warning = (
                    f"{snapshot.scope.capitalize()} corrections need repair "
                    f"({len(snapshot.issues)} errors). "
                    "Select a row to repair or delete it. "
                )
            else:
                warning = f"{snapshot.scope.capitalize()} corrections are unavailable. "
            warning += (
                "Export and fabrication are blocked until all errors are resolved."
            )
            general_issues = [issue for issue in snapshot.issues if issue.rowid is None]
            if general_issues:
                warning += "\n" + "\n".join(map(str, general_issues))
                warning += (
                    "\nResolve the file errors above and reopen Corrections Manager "
                    "to retry."
                )
        if snapshot.warnings:
            warning += "\nCorrection warnings:\n" + "\n".join(
                map(str, snapshot.warnings)
            )
            warning += (
                "\nResolve the warnings above and reopen Corrections Manager to retry."
            )
        self.correction_status.SetLabel(warning)
        self.correction_status.SetToolTip(
            "\n".join(map(str, (*snapshot.issues, *snapshot.warnings)))
        )
        self.correction_status.Wrap(int(self.parent.scale_factor * 700))
        self.Layout()
        self.enable_toolbar_buttons()

    def _show_error(self, title: str, error: object) -> None:
        """Show a handled correction error while preserving the current inputs."""
        self.logger.warning("%s: %s", title, error)
        wx.MessageBox(str(error), title, wx.OK | wx.ICON_ERROR, self)

    def _selection_matches_database(self) -> bool:
        """Refuse to reuse a selected row identity in a different database."""
        return self.selected_record is None or os.path.realpath(
            self.selection_db_path
        ) == os.path.realpath(self.parent.library.correctionsdb_file)

    def _confirm_replacement(
        self, correction: Correction, conflicts: Sequence[StoredCorrection]
    ) -> bool:
        """Ask before atomically replacing other records with the same pattern."""
        existing = "\n".join(
            f"Row {row.rowid}: "
            + (
                str(row.correction)
                if row.correction is not None
                else f"{row.rotation}°, {row.offset[0]}/{row.offset[1]}"
            )
            for row in conflicts
        )
        dialog = wx.MessageDialog(
            self,
            f"A rule for '{correction.pattern}' already exists!",
            "Regex exists!",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        dialog.ExtendedMessage = (
            f"{existing}\n\nReplace these {len(conflicts)} existing entries with "
            f"{correction}?"
        )
        if self.selected_record is not None:
            dialog.ExtendedMessage += (
                f"\nThe selected row {self.selected_record.rowid} will become "
                f"'{correction.pattern}'."
            )
        try:
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    def save_correction(self, *_: object) -> bool:
        """Validate input and save or repair one row in a single transaction."""
        if not self._selection_matches_database():
            self._show_error(
                "Correction Save Error",
                "The corrections database changed. Select the row again before saving.",
            )
            self.populate_corrections_list(preserve_inputs=True)
            return False
        library = self.parent.library
        target = str(library.correctionsdb_file)
        rowid = self.selected_record.rowid if self.selected_record else None
        try:
            correction = validate_correction(
                self.regex.GetValue(),
                self.rotation.GetValue(),
                (self.offset_x.GetValue(), self.offset_y.GetValue()),
                source=target,
                rowid=rowid,
            )
            snapshot = library.read_correction_data(target)
            conflicts = [
                row
                for row in snapshot.rows
                if row.pattern == correction.pattern and row.rowid != rowid
            ]
            if (
                self.selected_record is None
                and len(conflicts) == 1
                and conflicts[0].correction == correction
            ):
                self.populate_corrections_list(selected_rowid=conflicts[0].rowid)
                return True
            if conflicts and not self._confirm_replacement(correction, conflicts):
                return False
            rowid = library.save_correction_data(
                correction,
                rowid=rowid,
                replace=bool(conflicts),
                db_path=target,
                expected_record=self.selected_record,
                expected_conflicts=conflicts,
            )
        except CorrectionDataError as error:
            self._show_error("Correction Save Error", error)
            self.populate_corrections_list(preserve_inputs=True)
            return False

        self.populate_corrections_list(selected_rowid=rowid)
        wx.PostEvent(self.parent, PopulateFootprintListEvent())
        return True

    def delete_correction(self, *_: object) -> bool:
        """Delete only the selected stored row, even for null or duplicate patterns."""
        if self.selected_record is None:
            return False
        if not self._selection_matches_database():
            self._show_error(
                "Correction Delete Error",
                "The corrections database changed. Select the row again before deleting.",
            )
            self.populate_corrections_list(preserve_inputs=True)
            return False
        try:
            self.parent.library.delete_correction_row(
                self.selected_record.rowid,
                db_path=self.selection_db_path,
                expected_record=self.selected_record,
            )
        except CorrectionDataError as error:
            self._show_error("Correction Delete Error", error)
            self.populate_corrections_list(preserve_inputs=True)
            return False
        self._clear_selection()
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())
        return True

    def on_correction_selected(self, event: wx.dataview.DataViewEvent) -> None:
        """Copy original field text without coercing invalid values to zero."""
        if self._populating:
            return
        row = self.corrections_list.GetSelectedRow()
        if row == wx.NOT_FOUND or not 0 <= row < len(self.correction_snapshot.rows):
            self._clear_selection()
        else:
            self._select_record(
                self.correction_snapshot.rows[row], self.correction_snapshot.db_path
            )
        self.enable_toolbar_buttons()

    def on_textfield_change(self, *_: object) -> None:
        """Check whether changed text enables saving."""
        self.enable_toolbar_buttons()

    def _uses_global_corrections(self) -> bool:
        """Read the active scope without inferring it from unrelated stored tables."""
        library = self.parent.library
        return os.path.realpath(library.correctionsdb_file) == os.path.realpath(
            library.globalcorrectionsdb_file
        )

    def on_global_corrections_changed(self, *_: object) -> bool:
        """Switch scope only after a successful, validated database transfer."""
        library = self.parent.library
        was_global = self._uses_global_corrections()
        dialog = wx.MessageDialog(
            self,
            "Do you want to switch to the "
            + ("local" if was_global else "global")
            + " corrections database?",
            "Switching corrections database",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if was_global:
            dialog.ExtendedMessage = "Switching to a board local database copies the current global database."
        else:
            dialog.ExtendedMessage = (
                "The project-specific corrections will be discarded. "
                "The global corrections will be used for this board."
            )
        try:
            confirmed = dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()
        if not confirmed:
            self.global_corrections.SetValue(was_global)
            return False
        try:
            library.switch_to_global_correction_database(not was_global)
        except CorrectionDataError as error:
            self.global_corrections.SetValue(was_global)
            self._show_error(
                "Correction Database Error",
                f"{error}\nResolve the file errors and try switching databases again.",
            )
            return False
        self._clear_selection()
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())
        self.global_corrections.SetValue(self._uses_global_corrections())
        return True

    def download_correction_data(self, *_: object) -> bool:
        """Refresh the display only after the remote batch has committed."""
        result = self.parent.library.fetch_remote_corrections()
        if result is None:
            return False
        self.populate_corrections_list()
        wx.PostEvent(self.parent, PopulateFootprintListEvent())
        return True

    def import_legacy_corrections(self) -> bool:
        """Import an old CSV once, after controls exist, and preserve its archive."""
        path = os.path.join(PLUGIN_PATH, "corrections", "cpl_rotations_db.csv")
        if not os.path.isfile(path):
            return False
        library = self.parent.library
        try:
            with open(path, "rb") as source:
                contents = source.read()
            key = library.correction_csv_migration_key(path, contents)
            completed = library.has_correction_migration(key)
        except (OSError, CorrectionDataError) as error:
            self._show_error("Legacy Correction Import Error", f"{path}: {error}")
            return False
        if not completed and not self._import_corrections(
            path, contents=contents, migration_key=key, refresh=False
        ):
            return False
        try:
            # A hard link refuses an existing archive atomically. Keep the source
            # if archival fails; the committed marker prevents replay after repair.
            os.link(path, f"{path}.backup")
            os.unlink(path)
        except OSError as error:
            wx.MessageBox(
                f"The legacy corrections were already imported successfully. "
                f"The source remains at {path}, but could not be archived: {error}. "
                "It will not be imported again unless its contents change.",
                "Legacy Correction Archive Warning",
                wx.OK | wx.ICON_WARNING,
                self,
            )
        return True

    def import_corrections_dialog(self, *_: object) -> bool:
        """Ask for a correction CSV and import it after confirmation."""
        with wx.FileDialog(
            self,
            "Import",
            "",
            "",
            "CSV files (*.csv)|*.csv",
            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as file_dialog:
            if file_dialog.ShowModal() == wx.ID_CANCEL:
                return False
            return self._import_corrections(file_dialog.GetPath())

    def export_corrections_dialog(self, *_: object) -> bool:
        """Ask for the destination of a validated correction CSV."""
        with wx.FileDialog(
            self,
            "Export",
            "",
            "",
            "CSV files (*.csv)|*.csv",
            wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as file_dialog:
            if file_dialog.ShowModal() == wx.ID_CANCEL:
                return False
            return self._export_corrections(file_dialog.GetPath())

    def _import_corrections(
        self,
        path: str | os.PathLike[str],
        *,
        contents: bytes | None = None,
        migration_key: str | None = None,
        refresh: bool = True,
    ) -> bool:
        """Validate the complete CSV before committing any insert or replacement."""
        library = self.parent.library
        target = str(library.correctionsdb_file)
        try:
            if contents is None:
                with open(path, "rb") as source:
                    contents = source.read()
            corrections = parse_corrections_csv(
                contents.decode("utf-8"), source=str(path)
            )
            result = library.apply_corrections(
                corrections, db_path=target, migration_key=migration_key
            )
        except (OSError, UnicodeError, CorrectionDataError) as error:
            self._show_error("Correction Import Error", f"{path}: {error}")
            return False
        if result.changed:
            if refresh:
                self.populate_corrections_list()
            wx.PostEvent(self.parent, PopulateFootprintListEvent())
        return True

    def _export_corrections(self, path: str | os.PathLike[str]) -> bool:
        """Validate a complete snapshot before opening the export destination."""
        try:
            snapshot = self.parent.library.read_correction_data()
            corrections = snapshot.corrections
            if corrections is None:
                self._show_error(
                    "Correction Export Error",
                    f"{path}: The active {snapshot.scope} corrections are not ready. "
                    "Resolve the errors shown in Corrections Manager before exporting.",
                )
                return False
            with open(path, "w", newline="", encoding="utf-8") as destination:
                writer = csv.writer(destination, quotechar='"', quoting=csv.QUOTE_ALL)
                writer.writerow(["Pattern", "Rotation", "Offset X", "Offset Y"])
                for correction in corrections:
                    writer.writerow(correction.csv_row())
        except (OSError, UnicodeError, CorrectionDataError) as error:
            self._show_error("Correction Export Error", f"{path}: {error}")
            return False
        return True
