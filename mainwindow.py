"""Contains the main window of the plugin."""

from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# ruff: noqa: I001

from collections.abc import Iterable, Sequence
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import datetime as dt
from threading import Thread
from typing import TYPE_CHECKING, Any, Optional
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time

import pcbnew as kicad_pcbnew
import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error
from wx import adv  # pylint: disable=import-error

from .bom_estimation.assembly_mode import classify_component_product_type
from .bom_estimation.help_text import show_bom_estimator_help
from .bom_widget import BomEstimatorController, BomEstimatorWidget
from .correction_data import Correction, match_correction
from .corrections import CorrectionManagerDialog
from .datamodel import PartListDataModel, STANDARD_ONLY_TOOLTIP
from .dataview_highlight import (
    HighlightedTextRenderer,
    decode_highlighted_value,
    simplify_footprint_name,
)
from .derive_params import params_for_part
from .enrichment.providers import LCSCAssemblyMetadataProvider
from .events import (
    EVT_ASSEMBLY_ENRICHMENT_COMPLETED_EVENT,
    EVT_ASSEMBLY_ENRICHMENT_PROGRESS_EVENT,
    EVT_ASSIGN_PARTS_EVENT,
    EVT_BOM_DATA_CHANGED_EVENT,
    EVT_DOWNLOAD_COMPLETED_EVENT,
    EVT_DOWNLOAD_FINISHED_EVENT,
    EVT_DOWNLOAD_PROGRESS_EVENT,
    EVT_DOWNLOAD_STARTED_EVENT,
    EVT_LOGBOX_APPEND_EVENT,
    EVT_MESSAGE_EVENT,
    EVT_POPULATE_FOOTPRINT_LIST_EVENT,
    EVT_UNZIP_COMBINING_PROGRESS_EVENT,
    EVT_UNZIP_COMBINING_STARTED_EVENT,
    EVT_UNZIP_EXTRACTING_COMPLETED_EVENT,
    EVT_UNZIP_EXTRACTING_PROGRESS_EVENT,
    EVT_UNZIP_EXTRACTING_STARTED_EVENT,
    EVT_UPDATE_SETTING,
    AssemblyEnrichmentCompletedEvent,
    AssemblyEnrichmentProgressEvent,
    BomDataChangedEvent,
    LogboxAppendEvent,
)
from .fabrication import Fabrication
from .footprint_helpers import (
    get_exclude_from_bom,
    get_exclude_from_pos,
    get_is_dnp,
    find_lcsc_assignment_text,
    iter_board_items,
    set_lcsc_value,
    toggle_exclude_from_bom,
    toggle_exclude_from_pos,
)
from .generate_hooks import format_hook_error, run_configured_hook
from .helpers import (
    PLUGIN_PATH,
    GetScaleFactor,
    HighResWxSize,
    getVersion,
    loadBitmapScaled,
)
from .kicad_drc import DRCViolationCounter
from .library import CorrectionState, Library, LibraryState
from .partdetails import PartDetailsDialog
from .part_preferences import PartPreferencesDialog
from .partselector import PartSelectorDialog
from .schematicexport import SchematicExport
from .settings import SettingsDialog
from .store import Store
from .stock_concern import stock_concern_references
from .stock_display import parse_stock
from .why_standard_dialog import WhyStandardDialog
from .window_layout import get_column_widths, restore_column_widths

FOOTPRINT_COLUMN_KEYS = {
    index: key
    for key, index in PartListDataModel.columns.items()
    if key not in {"TRAILING_SPACER_COL", "STANDARD_ONLY_COL"}
}

if TYPE_CHECKING:
    from .library import CorrectionSnapshot

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

ID_GENERATE = 0
ID_LAYERS = 1
ID_CORRECTIONS = 2
ID_PART_PREFERENCES = 3
ID_DOWNLOAD = 4
ID_SETTINGS = 5
ID_SELECT_PART = 6
ID_REMOVE_LCSC_NUMBER = 7
ID_SELECT_ALIKE = 8
ID_TOGGLE_BOM_POS = 9
ID_TOGGLE_BOM = 10
ID_TOGGLE_POS = 11
ID_PART_DETAILS = 12
ID_HIDE_BOM = 13
ID_HIDE_POS = 14
ID_EXPORT_TO_SCHEMATIC = 16
ID_CONTEXT_MENU_COPY_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_PASTE_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_NAME = wx.NewIdRef()
ID_CONTEXT_MENU_APPLY_PART_PREFERENCES = wx.NewIdRef()
ID_CONTEXT_MENU_SAVE_PART_PREFERENCES = wx.NewIdRef()


class KicadProvider:
    """KiCad implementation of the provider, see standalone_impl.py for the stub version."""

    def get_pcbnew(self):
        """Get the pcbnew instance."""
        return kicad_pcbnew


class JLCPCBTools(wx.Frame):
    """JLCPCBTools main application window."""

    def __init__(
        self,
        parent: Optional[wx.Window],
        kicad_provider: KicadProvider = KicadProvider(),
    ) -> None:
        self.library: Optional[Library] = None
        self._catalog_details: dict[str, dict[str, Any]] = {}
        self._catalog_ready = False
        self._catalog_switch_pending = False
        self.store: Optional[Store] = None
        while not wx.GetApp():
            time.sleep(1)
        wx.Frame.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=f"JLCPCB Tools [ {getVersion()} ]",
            pos=wx.DefaultPosition,
            size=wx.Size(1300, 800),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        # Host the form on a wx.Panel to retain tab navigation and native colours.
        self.content_panel = wx.Panel(self)
        self.pcbnew = kicad_provider.get_pcbnew()
        self.window = wx.GetTopLevelParent(self)
        self.SetSize(HighResWxSize(self.window, wx.Size(1300, 800)))
        self.scale_factor = GetScaleFactor(self.window)
        self.project_path = os.path.split(self.pcbnew.GetBoard().GetFileName())[0]
        self.board_name = os.path.split(self.pcbnew.GetBoard().GetFileName())[1]
        self.schematic_name = f"{self.board_name.split('.')[0]}.kicad_sch"
        self.hide_bom_parts = False
        self.hide_pos_parts = False
        self.library: Library
        self.store: Store
        self.settings = {}
        self.load_settings()
        # Normalize and write-back BOM-estimator settings into the in-memory
        # dict so subsequent reads see canonical values. The on-disk JSON is
        # not rewritten here; the next settings change or window close saves it.
        general_settings = self.settings.setdefault("general", {})
        raw_board_count = general_settings.get("bom_estimator_boards", 5)
        try:
            self.bom_estimator_board_count = self._normalize_board_count(
                raw_board_count
            )
        except (TypeError, ValueError):
            self.bom_estimator_board_count = 5
        general_settings["bom_estimator_boards"] = self.bom_estimator_board_count
        self.bom_estimator_force_standard = bool(
            general_settings.get("bom_estimator_force_standard", False)
        )
        general_settings["bom_estimator_force_standard"] = (
            self.bom_estimator_force_standard
        )
        self.bom_estimator_show = bool(general_settings.get("bom_estimator_show", True))
        general_settings["bom_estimator_show"] = self.bom_estimator_show
        self.auto_select_alike = bool(
            self.settings.get("general", {}).get("select_alike_auto", False)
        )
        self.select_alike_in_progress = False
        self._part_preferences_applied_on_open = False
        self._project_storage_unavailable = False
        # Singleton reference for the modeless PartSelectorDialog. Re-invoking
        # "Select Part" while one is open re-targets it instead of opening a
        # second window.
        self._part_selector = None
        self._why_standard_dialog = None
        self.bom_estimator_decision = None
        self.pending_assembly_enrichment = set()
        # Monotonic counter incremented each time assembly enrichment is started.
        # Worker threads capture the value at spawn; progress events with a stale
        # generation are discarded by on_assembly_enrichment_progress so that a
        # mid-flight reassignment of a reference cannot have stale metadata
        # written back to it.
        self.assembly_enrichment_generation = 0
        # Latch used by on_bom_data_changed to coalesce a burst of mutations
        # into a single recompute. SQLite commits are synchronous, so async
        # event dispatch is safe to defer here.
        self._bom_recompute_scheduled = False
        self.Bind(wx.EVT_CLOSE, self.quit_dialog)

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
        # -------------------- Horizontal top buttons -------------------------
        # ---------------------------------------------------------------------

        self.upper_toolbar = wx.ToolBar(
            self.content_panel,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.Size(1300, -1),
            wx.TB_HORIZONTAL | wx.TB_TEXT | wx.TB_NODIVIDER,
        )

        self.generate_button = self.upper_toolbar.AddTool(
            ID_GENERATE,
            "Generate",
            loadBitmapScaled("fabrication.png", self.scale_factor),
            "Generate fabrication files for JLCPCB",
        )

        self.upper_toolbar.AddSeparator()

        self.layer_selection = adv.BitmapComboBox(
            self.upper_toolbar, ID_LAYERS, style=wx.CB_READONLY
        )

        layer_options = [
            "Auto",
            "1 Layer",
            "2 Layer",
            "4 Layer",
            "6 Layer",
            "8 Layer",
            "10 Layer",
            "12 Layer",
            "14 Layer",
            "16 Layer",
            "18 Layer",
            "20 Layer",
        ]

        for option in layer_options:
            self.layer_selection.Append(
                option,
                loadBitmapScaled(
                    "mdi-layers-triple-outline.png", self.scale_factor, True
                ),
            )

        self.layer_selection.SetSelection(0)

        self.upper_toolbar.AddControl(self.layer_selection)

        self.upper_toolbar.AddStretchableSpace()

        self.correction_button = self.upper_toolbar.AddTool(
            ID_CORRECTIONS,
            "Corrections",
            loadBitmapScaled("mdi-format-rotate-90.png", self.scale_factor),
            "Manage part corrections",
        )

        self.part_preferences_button = self.upper_toolbar.AddTool(
            ID_PART_PREFERENCES,
            "Part preferences",
            loadBitmapScaled("mdi-selection.png", self.scale_factor),
            "Manage preferred LCSC parts for matching values and footprints across projects",
        )

        self.upper_toolbar.AddSeparator()

        self.download_button = self.upper_toolbar.AddTool(
            ID_DOWNLOAD,
            "Download",
            loadBitmapScaled("mdi-cloud-download-outline.png", self.scale_factor),
            "Download latest JLCPCB parts database",
        )

        self.settings_button = self.upper_toolbar.AddTool(
            ID_SETTINGS,
            "Settings",
            loadBitmapScaled("mdi-cog-outline.png", self.scale_factor),
            "Manage settings",
        )

        self.upper_toolbar.Realize()

        self.Bind(wx.EVT_TOOL, self.generate_fabrication_data, self.generate_button)
        self.Bind(wx.EVT_TOOL, self.manage_corrections, self.correction_button)
        self.Bind(
            wx.EVT_TOOL, self.manage_part_preferences, self.part_preferences_button
        )
        self.Bind(wx.EVT_TOOL, self.update_library, self.download_button)
        self.Bind(wx.EVT_TOOL, self.manage_settings, self.settings_button)

        # ---------------------------------------------------------------------
        # ------------------ Right side toolbar List --------------------------
        # ---------------------------------------------------------------------

        # An explicit width overrides GTK's content-based minimum size.
        self.right_toolbar = wx.ToolBar(
            self.content_panel,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TB_VERTICAL | wx.TB_TEXT | wx.TB_NODIVIDER,
        )

        self.select_part_button = self.right_toolbar.AddTool(
            ID_SELECT_PART,
            "Assign LCSC number",
            loadBitmapScaled(
                "mdi-database-search-outline.png",
                self.scale_factor,
            ),
            "Assign a LCSC number to a footprint",
        )

        self.remove_lcsc_number_button = self.right_toolbar.AddTool(
            ID_REMOVE_LCSC_NUMBER,
            "Remove LCSC number",
            loadBitmapScaled(
                "mdi-close-box-outline.png",
                self.scale_factor,
            ),
            "Remove a LCSC number from a footprint",
        )

        self.select_alike_button = self.right_toolbar.AddCheckTool(
            ID_SELECT_ALIKE,
            "Auto-select alike",
            loadBitmapScaled(
                "mdi-checkbox-multiple-marked.png",
                self.scale_factor,
            ),
            wx.NullBitmap,
            "Automatically select footprints with the same value and footprint",
        )

        self.toggle_bom_pos_button = self.right_toolbar.AddTool(
            ID_TOGGLE_BOM_POS,
            "Toggle BOM & POS",
            loadBitmapScaled(
                "bom-pos.png",
                self.scale_factor,
            ),
            "Toggle exclud from BOM and POS attribute",
        )

        self.toggle_bom_button = self.right_toolbar.AddTool(
            ID_TOGGLE_BOM,
            "Toggle BOM",
            loadBitmapScaled(
                "mdi-format-list-bulleted.png",
                self.scale_factor,
            ),
            "Toggle exclude from BOM attribute",
        )

        self.toggle_pos_button = self.right_toolbar.AddTool(
            ID_TOGGLE_POS,
            "Toggle POS",
            loadBitmapScaled(
                "mdi-crosshairs-gps.png",
                self.scale_factor,
            ),
            "Toggle exclude from POS attribute",
        )

        self.part_details_button = self.right_toolbar.AddTool(
            ID_PART_DETAILS,
            "Part details",
            loadBitmapScaled(
                "mdi-text-box-search-outline.png",
                self.scale_factor,
            ),
            "Show details of an assigned LCSC part",
        )

        self.hide_bom_button = self.right_toolbar.AddCheckTool(
            ID_HIDE_BOM,
            "Hide excluded BOM",
            loadBitmapScaled(
                "mdi-eye-off-outline.png",
                self.scale_factor,
            ),
            wx.NullBitmap,
            "Hide excluded BOM parts",
        )

        self.hide_pos_button = self.right_toolbar.AddCheckTool(
            ID_HIDE_POS,
            "Hide excluded POS",
            loadBitmapScaled(
                "mdi-eye-off-outline.png",
                self.scale_factor,
            ),
            wx.NullBitmap,
            "Hide excluded POS parts",
        )

        self.export_schematic_button = self.right_toolbar.AddTool(
            ID_EXPORT_TO_SCHEMATIC,
            "Export to schematic",
            loadBitmapScaled(
                "mdi-application-export.png",
                self.scale_factor,
            ),
            "Export LCSC assignments to schematic",
        )

        self.Bind(wx.EVT_TOOL, self.select_part, self.select_part_button)
        self.Bind(wx.EVT_TOOL, self.remove_lcsc_number, self.remove_lcsc_number_button)
        self.Bind(wx.EVT_TOOL, self.toggle_select_alike, self.select_alike_button)
        self.Bind(wx.EVT_TOOL, self.toggle_bom_pos, self.toggle_bom_pos_button)
        self.Bind(wx.EVT_TOOL, self.toggle_bom, self.toggle_bom_button)
        self.Bind(wx.EVT_TOOL, self.toggle_pos, self.toggle_pos_button)
        self.Bind(wx.EVT_TOOL, self.get_part_details, self.part_details_button)
        self.Bind(wx.EVT_TOOL, self.OnBomHide, self.hide_bom_button)
        self.Bind(wx.EVT_TOOL, self.OnPosHide, self.hide_pos_button)
        self.Bind(wx.EVT_TOOL, self.export_to_schematic, self.export_schematic_button)

        self.right_toolbar.ToggleTool(ID_SELECT_ALIKE, self.auto_select_alike)

        self.right_toolbar.Realize()

        # ---------------------------------------------------------------------
        # ----------------------- Footprint List ------------------------------
        # ---------------------------------------------------------------------

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 600)))

        self.footprint_list = dv.DataViewCtrl(
            self.content_panel,
            style=wx.BORDER_THEME | dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_MULTIPLE,
        )

        reference = self.footprint_list.AppendTextColumn(
            "Ref", 0, width=50, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        value = self.footprint_list.AppendTextColumn(
            "Value (Name)",
            1,
            width=150,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        footprint = self.footprint_list.AppendTextColumn(
            "Footprint",
            2,
            width=250,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        params_renderer = HighlightedTextRenderer(
            value_decoder=self.decode_mainwindow_highlight_value,
            align=wx.ALIGN_CENTER,
        )
        params = dv.DataViewColumn(
            "LCSC Params",
            params_renderer,
            11,
            width=150,
            align=wx.ALIGN_CENTER,
        )
        self.footprint_list.AppendColumn(params)
        lcsc = self.footprint_list.AppendTextColumn(
            "LCSC", 3, width=100, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        type = self.footprint_list.AppendTextColumn(
            "Type", 4, width=100, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        self.footprint_list.AppendToggleColumn(
            "Std",
            PartListDataModel.columns["STANDARD_ONLY_COL"],
            width=HighResWxSize(self.window, wx.Size(36, -1)).GetWidth(),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
            flags=0,
        )
        stock = self.footprint_list.AppendTextColumn(
            "Stock", 5, width=100, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        bom = self.footprint_list.AppendIconTextColumn(
            "BOM", 6, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        pos = self.footprint_list.AppendIconTextColumn(
            "POS", 7, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        dnp = self.footprint_list.AppendIconTextColumn(
            "POP", 8, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        price = self.footprint_list.AppendTextColumn(
            "BOM Price",
            PartListDataModel.columns["PRICE_COL"],
            width=100,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        correction = self.footprint_list.AppendTextColumn(
            "Correction",
            9,
            width=120,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        side = self.footprint_list.AppendTextColumn(
            "Side",
            10,
            width=50,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        enrichment = self.footprint_list.AppendTextColumn(
            "Enrichment",
            PartListDataModel.columns["ENRICH_COL"],
            width=110,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        trailing_spacer = self.footprint_list.AppendTextColumn(
            " ",
            PartListDataModel.columns["TRAILING_SPACER_COL"],
            width=24,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )

        reference.SetSortable(True)
        value.SetSortable(True)
        footprint.SetSortable(True)
        lcsc.SetSortable(True)
        type.SetSortable(True)
        stock.SetSortable(True)
        price.SetSortable(True)
        bom.SetSortable(True)
        pos.SetSortable(False)
        dnp.SetSortable(True)
        enrichment.SetSortable(True)
        correction.SetSortable(True)
        side.SetSortable(True)
        params.SetSortable(True)
        trailing_spacer.SetSortable(False)

        table_sizer.Add(self.footprint_list, 20, wx.ALL | wx.EXPAND, 5)

        self.footprint_list.Bind(
            dv.EVT_DATAVIEW_SELECTION_CHANGED, self.OnFootprintSelected
        )

        self.footprint_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.select_part)

        self.footprint_list.Bind(dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU, self.OnRightDown)

        table_sizer.Add(self.right_toolbar, 1, wx.EXPAND, 5)
        # ---------------------------------------------------------------------
        # --------------------- Bottom Logbox and Gauge -----------------------
        # ---------------------------------------------------------------------
        self.logbox = wx.TextCtrl(
            self.content_panel,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.logbox.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 150)))
        self.gauge = wx.Gauge(
            self.content_panel,
            wx.ID_ANY,
            100,
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(100, -1)),
            wx.GA_HORIZONTAL,
        )
        self.gauge.SetValue(0)
        self.gauge.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 5)))

        # ---------------------------------------------------------------------
        # ---------------------- BOM Cost Estimator ---------------------------
        # ---------------------------------------------------------------------

        self.bom_widget = BomEstimatorWidget(
            self.content_panel,
            window=self.window,
            board_count=self.bom_estimator_board_count,
            force_standard=self.bom_estimator_force_standard,
            on_board_count_spin=self.on_bom_estimator_board_count_spinctrl,
            on_board_count_text=self.on_bom_estimator_board_count_text,
            on_board_count_text_timer=self.on_bom_estimator_board_count_text_timer,
            on_force_standard_changed=self.on_bom_estimator_force_standard_changed,
            on_details=self.show_assembly_mode_details,
            on_help=self.show_bom_estimator_help,
        )

        # Backward-compatible aliases while BOM logic is still in this class.
        self.estimator_sizer = self.bom_widget.sizer
        estimator_sizer = self.estimator_sizer
        self.bom_estimator_boards_input = self.bom_widget.boards_input
        self.bom_estimator_text_timer = self.bom_widget.text_timer
        self.bom_estimator_standard_checkbox = self.bom_widget.standard_checkbox
        self.bom_estimator_help_button = self.bom_widget.help_button
        self.bom_estimator_summary = self.bom_widget.summary_label

        # This status must exist before init_data() first populates the list.
        # Invalid stored corrections remain repairable through the manager.
        self.correction_status = wx.StaticText(self.content_panel, label="")
        self.project_storage_status = wx.StaticText(self.content_panel, label="")
        self.project_storage_status.Hide()
        self.correction_status.Hide()

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        self.SetSizeHints(HighResWxSize(self.window, wx.Size(1000, -1)), wx.DefaultSize)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.upper_toolbar, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(estimator_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        layout.Add(
            self.project_storage_status,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND,
            5,
        )
        layout.Add(
            self.correction_status,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND,
            5,
        )
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.logbox, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)

        self.content_panel.SetSizer(layout)
        frame_layout = wx.BoxSizer(wx.VERTICAL)
        frame_layout.Add(self.content_panel, 1, wx.EXPAND)
        self.SetSizer(frame_layout)
        self.Layout()
        self.bom_widget.set_visible(self.bom_estimator_show)
        self.Layout()
        self.Centre(wx.BOTH)
        restore_column_widths(
            self.footprint_list,
            self.settings.get("mainwindow", {}).get("column_widths", {}),
            FOOTPRINT_COLUMN_KEYS,
        )
        self._layout_ready = True

        # ---------------------------------------------------------------------
        # ------------------------ Custom Events ------------------------------
        # ---------------------------------------------------------------------

        self.Bind(EVT_MESSAGE_EVENT, self.display_message)
        self.Bind(EVT_ASSIGN_PARTS_EVENT, self.assign_parts)
        self.Bind(EVT_POPULATE_FOOTPRINT_LIST_EVENT, self.populate_footprint_list)
        self.Bind(EVT_UPDATE_SETTING, self.update_settings)

        self.Bind(EVT_DOWNLOAD_STARTED_EVENT, self.download_started)
        self.Bind(EVT_DOWNLOAD_PROGRESS_EVENT, self.download_progress)
        self.Bind(EVT_DOWNLOAD_COMPLETED_EVENT, self.download_completed)
        self.Bind(EVT_DOWNLOAD_FINISHED_EVENT, self.download_finished)

        self.Bind(EVT_UNZIP_COMBINING_STARTED_EVENT, self.unzip_combining_started)
        self.Bind(EVT_UNZIP_COMBINING_PROGRESS_EVENT, self.unzip_combining_progress)
        self.Bind(EVT_UNZIP_EXTRACTING_STARTED_EVENT, self.unzip_extracting_started)
        self.Bind(EVT_UNZIP_EXTRACTING_PROGRESS_EVENT, self.unzip_extracting_progress)
        self.Bind(EVT_UNZIP_EXTRACTING_COMPLETED_EVENT, self.unzip_extracting_completed)

        self.Bind(EVT_LOGBOX_APPEND_EVENT, self.logbox_append)
        self.Bind(
            EVT_ASSEMBLY_ENRICHMENT_PROGRESS_EVENT,
            self.on_assembly_enrichment_progress,
        )
        self.Bind(
            EVT_ASSEMBLY_ENRICHMENT_COMPLETED_EVENT,
            self.on_assembly_enrichment_completed,
        )
        self.Bind(EVT_BOM_DATA_CHANGED_EVENT, self.on_bom_data_changed)

        self.enable_part_specific_toolbar_buttons(False)

        self.init_logger()
        self.partlist_data_model = PartListDataModel(
            self.scale_factor,
            simplify_stock=self.settings.get("general", {}).get("simplify_stock", True),
        )
        self.footprint_list.AssociateModel(self.partlist_data_model)
        self._standard_only_tooltip_active = False
        self._footprint_list_main_window = (
            self.footprint_list.GetMainWindow() or self.footprint_list
        )
        self._footprint_list_main_window.Bind(
            wx.EVT_MOTION, self.on_footprint_list_motion
        )
        self._footprint_list_main_window.Bind(
            wx.EVT_LEAVE_WINDOW, self.on_footprint_list_leave
        )
        self.bom_estimator_controller = BomEstimatorController(
            read_parts=lambda: (
                self.store.read_all()
                if hasattr(self, "store") and self.store is not None
                else []
            ),
            get_part_details=self._bom_get_part_details,
            get_board=self._get_current_board,
            is_force_standard_enabled=lambda: self.bom_estimator_force_standard,
            set_price_label=self.partlist_data_model.set_bom_price,
            set_standard_only_refs=self._set_standard_only_refs,
            set_summary_text=self.bom_widget.set_summary_text,
            set_details_button_label=self.bom_widget.set_details_button_label,
        )

        self.init_data()

    def Layout(self) -> bool:
        """Lay out the form after resizing or changing the visible controls."""
        result = wx.Frame.Layout(self)
        panel = getattr(self, "content_panel", None)
        if panel:
            panel.Layout()
        return result

    def init_data(self, *, download_if_missing: bool = True) -> None:
        """Initialize the library and populate the main window."""
        try:
            self.init_library()
            self.init_fabrication()
            if self.library.state == LibraryState.UPDATE_NEEDED:
                if download_if_missing:
                    self.library.update()
                else:
                    self.init_store()
                    self._clear_catalog_views()
            else:
                self.init_store()
        except (sqlite3.Error, OSError, ValueError) as error:
            self._set_project_storage_error(error)

        self.logger.debug("kicad version: %s", kicad_pcbnew.GetBuildVersion())

    def _get_current_board(self):
        """Return current board instance for BOM controller callbacks."""
        return self.pcbnew.GetBoard()

    def _set_standard_only_refs(self, refs):
        """Update computed Standard-only cells and clear any stale tooltip."""
        self.partlist_data_model.set_standard_only_refs(refs)
        self.footprint_list.Refresh()
        self._set_standard_only_tooltip(False)

    def _set_standard_only_tooltip(self, active):
        """Show or clear the Standard-only row tooltip without flicker."""
        active = bool(active)
        if active == self._standard_only_tooltip_active:
            return
        self._standard_only_tooltip_active = active
        if active:
            self._footprint_list_main_window.SetToolTip(
                wx.ToolTip(STANDARD_ONLY_TOOLTIP)
            )
        else:
            self._footprint_list_main_window.UnsetToolTip()

    def on_footprint_list_motion(self, event):
        """Show the explanation while hovering anywhere on a checked row."""
        source = event.GetEventObject()
        control_point = self.footprint_list.ScreenToClient(
            source.ClientToScreen(event.GetPosition())
        )
        item, _column = self.footprint_list.HitTest(control_point)
        active = bool(
            item and item.IsOk() and self.partlist_data_model.is_standard_only(item)
        )
        self._set_standard_only_tooltip(active)
        event.Skip()

    def on_footprint_list_leave(self, event):
        """Clear the Standard-only explanation when the pointer leaves the list."""
        self._set_standard_only_tooltip(False)
        event.Skip()

    def is_catalog_available(self) -> bool:
        """Report whether the selected catalog has completed initialization."""
        return self.library is not None and getattr(self, "_catalog_ready", True)

    def _invalidate_catalog_details(self) -> None:
        """Start a fresh raw-details snapshot for the current catalog."""
        self._catalog_details = {}

    def _catalog_get_part_details(
        self, lcsc: str, *, strict: bool = False
    ) -> dict[str, Any]:
        """Reuse raw catalog records, distinguishing confirmed misses from failures."""
        key = str(lcsc or "").strip().upper()
        if not key or not self.is_catalog_available():
            return {}
        if not hasattr(self, "_catalog_details"):
            self._invalidate_catalog_details()
        if key not in self._catalog_details:
            try:
                self._catalog_details[key] = deepcopy(
                    self.library.get_part_details(key)
                )
            except (sqlite3.Error, OSError) as error:
                if strict:
                    raise
                self.logger.warning("Unable to read catalog part %s: %s", key, error)
                return {}
            details = self._catalog_details[key]
            self.partlist_data_model.set_catalog_details(
                key,
                details.get("type", ""),
                details.get("stock", ""),
                params_for_part(details),
            )
        return deepcopy(self._catalog_details[key])

    def _bom_get_part_details(self, lcsc: str) -> dict[str, Any]:
        """Share the current raw catalog snapshot with estimator callbacks."""
        return self._catalog_get_part_details(lcsc)

    def _refresh_catalog_views(self) -> None:
        """Replace catalog-dependent display, concerns, prices and selector results."""
        self.populate_footprint_list()
        self._refresh_catalog_outputs()

    def _refresh_catalog_outputs(self) -> None:
        """Refresh computed catalog values and the open selector after row changes."""
        self.recompute_stock_concerns()
        self.recompute_bom_estimate()
        selector = getattr(self, "_part_selector", None)
        if selector is not None:
            selector.refresh_catalog()

    def _clear_catalog_views(self) -> None:
        """Discard supply and prices when catalog replacement cannot be trusted."""
        self._catalog_ready = False
        self._invalidate_catalog_details()
        self._refresh_catalog_views()

    def _publish_catalog(self) -> None:
        """Validate catalog consumers before publishing a fresh initialized snapshot."""
        self._catalog_ready = False
        self._invalidate_catalog_details()
        try:
            if not self.library.has_usable_parts_catalog(check_integrity=False):
                raise sqlite3.DatabaseError(
                    "The parts catalog is unreadable or incomplete. Download it again."
                )
            self._update_library_title()
            self.library.category_map = {}
            self._catalog_ready = True
            if self.store is None:
                self.init_store()
            else:
                self._initialize_catalog_parts()
            self._refresh_catalog_outputs()
        except (sqlite3.Error, OSError, ValueError) as error:
            self.library.state = LibraryState.UPDATE_NEEDED
            self._set_project_storage_error(error)
            self._clear_catalog_views()

    def quit_dialog(self, *_: object) -> None:
        """Save layout and destroy the frame and its child windows on close."""
        logger = logging.getLogger(__name__)
        logger.info("quit_dialog()")
        layout_ready = getattr(self, "_layout_ready", False)
        selector = getattr(self, "_part_selector", None)
        try:
            if layout_ready:
                self.settings.setdefault("mainwindow", {})["column_widths"] = (
                    get_column_widths(self.footprint_list, FOOTPRINT_COLUMN_KEYS)
                )
                if not selector:
                    self.save_settings()
        except OSError:
            logger.exception("Unable to save window layout")
        finally:
            try:
                if selector:
                    # Its close handler saves both windows' updated settings once.
                    selector.Close()
            finally:
                why_standard_dialog = getattr(self, "_why_standard_dialog", None)
                if why_standard_dialog:
                    why_standard_dialog.Close()
                root = logging.getLogger()
                with suppress(AttributeError):
                    root.removeHandler(self.logging_handler1)
                with suppress(AttributeError):
                    root.removeHandler(self.logging_handler2)
                self.Destroy()

    def init_library(self) -> None:
        """Initialize the parts library and start a new catalog snapshot."""
        self._catalog_ready = False
        self._invalidate_catalog_details()
        self.library = Library(self)
        try:
            if (
                self.library.state == LibraryState.INITIALIZED
                and not self.library.has_usable_parts_catalog(check_integrity=False)
            ):
                raise sqlite3.DatabaseError(
                    "The parts catalog is unreadable or incomplete. Download it again."
                )
            self._update_library_title()
        except (sqlite3.Error, OSError, ValueError):
            self.library.state = LibraryState.UPDATE_NEEDED
            raise
        self._catalog_ready = self.library.state == LibraryState.INITIALIZED

    def _update_library_title(self) -> None:
        """Show metadata from the currently active catalog after initialization."""
        meta = self.library.get_parts_db_info()
        if meta is not None:
            if not isinstance(meta.last_update, str):
                raise ValueError(
                    "The parts catalog update date must be an ISO date string."
                )
            last_update = dt.fromisoformat(meta.last_update).strftime("%Y-%m-%d %H:%M")
            self.SetTitle(
                f"JLCPCB Tools [ {getVersion()} ] | Last database update: {last_update}",
            )
            self.logger.debug(
                "JLCPCB version %s, last database update %s, part count %d, size (bytes) %d",
                getVersion(),
                meta.last_update,
                meta.part_count,
                meta.size,
            )
        else:
            self.SetTitle(
                f"JLCPCB Tools [ {getVersion()} ] | Last database update: No DB found",
            )
            self.logger.debug("JLCPCB version %s, no parts db info found", getVersion())

    def init_store(self) -> None:
        """Initialize fabrication and assignments before enabling project actions."""
        try:
            if getattr(self, "fabrication", None) is None:
                self.init_fabrication()
            self.store = Store(self, self.project_path, self.pcbnew.GetBoard())
            self._set_project_storage_error(None)
            self._initialize_catalog_parts()
        except (sqlite3.Error, OSError) as error:
            self._set_project_storage_error(error)

    def _initialize_catalog_parts(self) -> None:
        """Apply opening preferences once whenever project and catalog first meet."""
        if (
            self.store is None
            or self.library.state != LibraryState.INITIALIZED
            or not self.is_catalog_available()
        ):
            return
        if not getattr(self, "_part_preferences_applied_on_open", False):
            self._part_preferences_applied_on_open = True
            if self.settings.get("part_preferences", {}).get(
                "fill_empty_lcsc_assignments_on_open", True
            ):
                self._fill_empty_lcsc_assignments_from_part_preferences()
        self.populate_footprint_list()
        if self.store is not None:
            self.start_assembly_enrichment()
            self.recompute_bom_estimate()

    def _set_project_storage_error(self, error: Optional[BaseException]) -> None:
        """Keep Settings usable while unavailable project data disables assignments."""
        unavailable = error is not None
        self._project_storage_unavailable = unavailable
        if unavailable:
            self.logger.warning("Part assignments are unavailable: %s", error)
            self.store = None
            self.partlist_data_model.RemoveAll()
            self.assembly_enrichment_generation += 1
            self.pending_assembly_enrichment.clear()
        self.project_storage_status.SetLabel(
            "Part assignments are unavailable; assignment actions and generation are disabled.\n"
            "Check the log, close other windows using this project, then reopen. Settings remains available."
            if unavailable
            else ""
        )
        self.project_storage_status.SetToolTip(str(error) if unavailable else "")
        self.project_storage_status.Show(unavailable)
        self.footprint_list.Enable(not unavailable)
        self.right_toolbar.Enable(not unavailable)
        self.upper_toolbar.EnableTool(ID_GENERATE, not unavailable)
        for tool in (ID_DOWNLOAD, ID_CORRECTIONS, ID_PART_PREFERENCES):
            self.upper_toolbar.EnableTool(tool, self.library is not None)
        self.Layout()

    def init_fabrication(self) -> None:
        """Initialize the fabrication."""
        self.fabrication = Fabrication(self, self.pcbnew.GetBoard())

    def reset_gauge(self, *_):
        """Initialize the gauge."""
        self.gauge.SetRange(100)
        self.gauge.SetValue(0)

    def report_generation_step(self, text: str):
        """Report fabrication generation progress to the log and gauge."""
        self.logger.info("[Generate] %s", text)
        self.gauge.Pulse()
        self.flush_generation_ui()

    def flush_generation_ui(self):
        """Force pending log/gauge UI updates to be painted."""
        for handler in logging.getLogger().handlers:
            if hasattr(handler, "flush"):
                with suppress(Exception):
                    handler.flush()

        if hasattr(self, "logbox") and self.logbox is not None:
            with suppress(Exception):
                self.logbox.SetInsertionPointEnd()
                self.logbox.ShowPosition(self.logbox.GetLastPosition())
                self.logbox.Refresh()
                self.logbox.Update()

        if hasattr(self, "gauge") and self.gauge is not None:
            with suppress(Exception):
                self.gauge.Refresh()
                self.gauge.Update()

        with suppress(Exception):
            self.Refresh()
            self.Update()

    @contextmanager
    def generation_step(self, description: str):
        """Wrap a fabrication generation step with start/end feedback."""
        self._current_generation_step = description
        self.report_generation_step(f"{description}...")
        start = time.perf_counter()
        completed = False
        try:
            yield
            completed = True
        finally:
            if completed:
                elapsed = time.perf_counter() - start
                self.report_generation_step(f"{description} done ({elapsed:.1f}s)")

    def run_generation_step(
        self,
        description: str,
        func,
        *args,
    ):
        """Run a callable inside a timed generation step wrapper."""
        with self.generation_step(description):
            return func(*args)

    def download_started(self, *_):
        """Initialize the gauge."""
        self.reset_gauge()

    def download_progress(self, e):
        """Update the gauge."""
        self.gauge.SetValue(int(e.value))

    def _is_current_catalog_event(self, event: Any) -> bool:
        """Reject queued completions from an earlier library or source."""
        if self.library is None:
            return False
        source = (self.library.selected_library, self.library.partsdb_file)
        return (
            getattr(event, "library", self.library) is self.library
            and getattr(event, "source", source) == source
            and getattr(event, "attempt", self.library.download_attempt)
            == self.library.download_attempt
        )

    def download_completed(self, event: Any = None) -> None:
        """Publish a successful replacement only if its source is still selected."""
        if (
            not self._is_current_catalog_event(event)
            or self.library.is_download_running()
            or getattr(self, "_catalog_switch_pending", False)
        ):
            return
        self._publish_catalog()

    def download_finished(self, event: Any) -> None:
        """Apply deferred catalog settings after either download success or failure."""
        if not self._is_current_catalog_event(event):
            return
        if getattr(self, "_catalog_switch_pending", False):
            self._apply_library_settings()
        elif not event.succeeded:
            if self.library.state == LibraryState.INITIALIZED:
                self._publish_catalog()
            else:
                self._clear_catalog_views()

    def unzip_combining_started(self, *_):
        """Initialize the gauge."""
        self.reset_gauge()

    def unzip_combining_progress(self, e):
        """Update the gauge."""
        self.gauge.SetValue(int(e.value))

    def unzip_extracting_started(self, *_):
        """Initialize the gauge."""
        self.reset_gauge()

    def unzip_extracting_progress(self, e):
        """Update the gauge."""
        self.gauge.SetValue(int(e.value))

    def unzip_extracting_completed(self, *_: object) -> None:
        """Update progress; source-tagged completion publishes the extracted catalog."""
        self.reset_gauge()

    def _can_apply_user_assignments(self) -> bool:
        """Explain unavailable assignment dependencies for a user-initiated action."""
        if self.store is None:
            self.logger.warning(
                "Cannot apply LCSC assignments while project storage is unavailable. "
                "Check the storage error, then retry after recovery or reopen the dialog."
            )
            return False
        if not self.is_catalog_available():
            self.logger.warning(
                "Cannot apply LCSC assignments: the selected parts catalog is unavailable. "
                "Download it or select an available catalog in Settings."
            )
            return False
        return True

    def assign_parts(self, e: Any) -> None:
        """Assign the selected catalog part and remember its preferences."""
        if not self._can_apply_user_assignments():
            return
        try:
            details = self._catalog_get_part_details(e.lcsc, strict=True)
            details.update(type=e.type, stock=e.stock)
            assigned = self._apply_lcsc_assignments(
                dict.fromkeys(e.references, e.lcsc),
                details={e.lcsc: details},
                remember_part_preferences=True,
            )
            if assigned:
                key = str(e.lcsc).strip().upper()
                self._catalog_details[key] = deepcopy(details)
                self.partlist_data_model.set_catalog_details(
                    key,
                    details.get("type", ""),
                    details.get("stock", ""),
                    params_for_part(details),
                )
        except (sqlite3.Error, OSError) as error:
            self.logger.warning("Unable to read the selected part: %s", error)

    def _apply_lcsc_assignments(
        self,
        assignments: dict[str, str],
        *,
        details: Optional[dict[str, dict[str, Any]]] = None,
        remember_part_preferences: bool = False,
        notify: bool = True,
    ) -> list[str]:
        """Commit project changes before updating board fields or displayed rows."""
        if self.store is None or not self.is_catalog_available():
            return []
        board = self.pcbnew.GetBoard()
        footprints = {
            reference: footprint
            for reference in assignments
            if (footprint := board.FindFootprintByReference(reference)) is not None
        }
        if not footprints:
            return []
        catalog = {}
        try:
            for lcsc in dict.fromkeys(assignments[ref] for ref in footprints):
                part = (details or {}).get(lcsc)
                if part is None:
                    part = self._catalog_get_part_details(lcsc, strict=True)
                stored_stock = parse_stock(part.get("stock"))
                catalog[lcsc] = (part, stored_stock, params_for_part(part))
            self.store.set_lcsc_assignments(
                (ref, assignments[ref], catalog[assignments[ref]][1])
                for ref in footprints
            )
        except (sqlite3.Error, OSError) as error:
            self.logger.warning("Unable to apply LCSC assignments: %s", error)
            return []

        preferences = []
        for reference, footprint in footprints.items():
            lcsc = assignments[reference]
            part, _stored_stock, params = catalog[lcsc]
            set_lcsc_value(footprint, lcsc)
            stock = part.get("stock")
            self.partlist_data_model.set_lcsc(
                reference,
                lcsc,
                part.get("type", ""),
                stock if stock is not None else "",
                params,
            )
            preferences.append(
                (str(footprint.GetFPID().GetLibItemName()), footprint.GetValue(), lcsc)
            )
        if remember_part_preferences and self.settings.get("part_preferences", {}).get(
            "remember_lcsc_assignments", True
        ):
            self._save_part_preferences(preferences, automatic=True)
        assigned = list(footprints)
        if notify:
            self.start_assembly_enrichment(assigned)
            wx.PostEvent(self, BomDataChangedEvent(source="assign_parts"))
        return assigned

    def _save_part_preferences(
        self, preferences: Iterable[tuple[str, str, str]], *, automatic: bool = False
    ) -> int:
        """Remember one action's complete preferences without losing its assignments."""
        complete = [
            preference
            for preference in preferences
            if all(isinstance(text, str) and text.strip() for text in preference[:2])
        ]
        if not complete:
            return 0
        try:
            saved = self.library.save_part_preferences(complete)
        except sqlite3.Error as error:
            self.logger.warning("Unable to save part preferences: %s", error)
            return 0
        if saved:
            message = "Saved %d part preference(s)."
            if automatic:
                message += (
                    " To disable automatic remembering, clear 'Remember my part preferences'"
                    " in Settings > Part preferences."
                )
            self.logger.info(message, saved)
        return saved

    def _fill_empty_lcsc_assignments_from_part_preferences(self) -> None:
        """Fill truly empty eligible fields once, in one project transaction."""
        board = self.pcbnew.GetBoard()
        part_preferences = {}
        assignments = {}
        try:
            for part in self.store.read_all():
                if part["lcsc"] or not part["footprint"] or not part["value"]:
                    continue
                footprint = board.FindFootprintByReference(part["reference"])
                if (
                    footprint is None
                    or get_exclude_from_bom(footprint)
                    or get_exclude_from_pos(footprint)
                    or get_is_dnp(footprint)
                ):
                    continue
                key = (part["footprint"], part["value"])
                if key not in part_preferences:
                    part_preferences[key] = self.library.get_part_preference(*key)
                if lcsc := part_preferences[key]:
                    occupied = find_lcsc_assignment_text(footprint)
                    if occupied:
                        self.logger.info(
                            "Skipped part preference for %s: field %r already contains %r.",
                            part["reference"],
                            *occupied,
                        )
                    else:
                        assignments[part["reference"]] = lcsc
            assigned = self._apply_lcsc_assignments(assignments, notify=False)
        except sqlite3.Error as error:
            self.logger.warning(
                "Unable to fill assignments from part preferences: %s", error
            )
            return
        if assigned:
            self.logger.info(
                "Filled %d empty LCSC assignment(s) from part preferences. "
                "To disable, clear 'Parts preferences fill in empty LCSC assignments' "
                "in Settings > Part preferences.",
                len(assigned),
            )

    def _set_bom_estimator_board_count(self, value: int) -> None:
        """Persist board count and update estimate when value changed."""
        if value == self.bom_estimator_board_count:
            return
        self.bom_estimator_board_count = value
        self.settings.setdefault("general", {})["bom_estimator_boards"] = value
        self.save_settings()
        self.recompute_stock_concerns()
        self.recompute_bom_estimate()

    def on_bom_estimator_board_count_spinctrl(self, e: Any) -> None:
        """Handle SpinCtrl arrows immediately, using step=5 increments."""
        value = self._normalize_board_count(e.GetEventObject().GetValue())
        if e.GetEventObject().GetValue() != value:
            e.GetEventObject().SetValue(value)
        self._set_bom_estimator_board_count(value)

    def on_bom_estimator_board_count_text(self, *_: object) -> None:
        """Debounce manual text entry to avoid recompute flicker while typing."""
        if hasattr(self.bom_estimator_text_timer, "StartOnce"):
            self.bom_estimator_text_timer.StartOnce(300)
        else:
            self.bom_estimator_text_timer.Start(300, oneShot=True)

    def on_bom_estimator_board_count_text_timer(self, *_: object) -> None:
        """Apply board count from text field after debounce delay."""
        value = self._normalize_board_count(self.bom_estimator_boards_input.GetValue())
        if self.bom_estimator_boards_input.GetValue() != value:
            self.bom_estimator_boards_input.SetValue(value)
        self._set_bom_estimator_board_count(value)

    def _normalize_board_count(self, value: Any) -> int:
        """Normalize board count to a minimum of 5 boards."""
        return max(5, int(value))

    def on_bom_estimator_force_standard_changed(self, e):
        """Persist standard override preference and update BOM estimate."""
        value = bool(e.GetEventObject().GetValue())
        if value == self.bom_estimator_force_standard:
            return
        self.bom_estimator_force_standard = value
        self.settings.setdefault("general", {})["bom_estimator_force_standard"] = value
        self.save_settings()
        self.recompute_bom_estimate()

    def show_bom_estimator_help(self, *_):
        """Show shared BOM estimator help text via the help_text helper."""
        show_bom_estimator_help(self)

    def show_assembly_mode_details(self, *_):
        """Open or raise the modeless assembly-mode details dialog."""
        if self.bom_estimator_decision is None:
            self.recompute_bom_estimate()
        if self._why_standard_dialog is None:
            parts = self.store.read_all() if getattr(self, "store", None) else []
            self._why_standard_dialog = WhyStandardDialog(
                self, self.bom_estimator_decision, parts
            )
            self._why_standard_dialog.Show()
        self._why_standard_dialog.Raise()

    def recompute_bom_estimate(self):
        """Recompute and display estimated BOM+assembly cost."""
        board_count = self._normalize_board_count(self.bom_estimator_board_count)
        self.bom_estimator_decision = self.bom_estimator_controller.recompute(
            board_count
        )
        if self._why_standard_dialog is not None:
            parts = self.store.read_all() if getattr(self, "store", None) else []
            self._why_standard_dialog.update_content(
                self.bom_estimator_decision,
                parts,
            )

    def on_bom_data_changed(self, _e):
        """Coalesce a burst of BomDataChangedEvent posts into one recompute.

        Mutation handlers signal "BOM data changed" by posting an event rather
        than calling recompute directly. Many UI actions (e.g. multi-select
        toggle, populate_footprint_list) emit several posts in one event tick;
        the latch + CallAfter pattern collapses them so we recompute once per
        idle drain instead of once per mutation.
        """
        if self._bom_recompute_scheduled:
            return
        self._bom_recompute_scheduled = True
        wx.CallAfter(self._run_coalesced_bom_recompute)

    def _run_coalesced_bom_recompute(self) -> None:
        """Drain the coalesced recompute latch and run a single estimate."""
        self._bom_recompute_scheduled = False
        self.recompute_stock_concerns()
        self.recompute_bom_estimate()

    def recompute_stock_concerns(self) -> None:
        """Refresh Stock attributes from all live BOM parts, before view filtering."""
        model = self.partlist_data_model
        if (
            not self.settings.get("highlighting", {}).get("stock_concern", True)
            or self.store is None
            or not self.is_catalog_available()
        ):
            model.set_stock_concern_refs(set())
            return
        try:
            board = self.pcbnew.GetBoard()
            parts = []
            if board is not None:
                for part in self.store.read_all():
                    fp = board.FindFootprintByReference(part["reference"])
                    if fp is not None:
                        parts.append(
                            {
                                **part,
                                "exclude_from_bom": get_exclude_from_bom(fp),
                                "is_dnp": get_is_dnp(fp),
                            }
                        )
            refs = stock_concern_references(
                parts,
                lambda lcsc: self._catalog_get_part_details(lcsc).get("stock"),
                board_count=self._normalize_board_count(
                    getattr(self, "bom_estimator_board_count", 5)
                ),
            )
        except (sqlite3.Error, OSError) as error:
            self.logger.warning("Unable to update stock concerns: %s", error)
            refs = set()
        model.set_stock_concern_refs(refs)

    def _get_enrichment_status_label(self, part: dict) -> str:
        """Build UI status text for per-part assembly enrichment state."""
        lcsc = str(part.get("lcsc") or "")
        if not lcsc:
            return ""
        if lcsc in self.pending_assembly_enrichment:
            return "Pending"
        if (
            classify_component_product_type(part.get("component_product_type"))
            is not None
        ):
            return "Done"
        return "Class missing"

    def start_assembly_enrichment(
        self, references: Optional[Iterable[str]] = None
    ) -> None:
        """Start background enrichment for missing assembly process metadata."""
        if self.store is None:
            return
        try:
            targets = self.store.get_assembly_enrichment_targets(references)
        except sqlite3.Error as error:
            self.logger.warning("Unable to start assembly enrichment: %s", error)
            return
        targets = {
            lcsc: refs
            for lcsc, refs in targets.items()
            if lcsc not in self.pending_assembly_enrichment
        }
        if not targets:
            return

        self.pending_assembly_enrichment.update(targets.keys())
        for refs in targets.values():
            for reference in refs:
                self.partlist_data_model.set_enrichment_status(reference, "Pending")

        self.assembly_enrichment_generation += 1
        generation = self.assembly_enrichment_generation
        Thread(
            target=self._assembly_enrichment_worker,
            args=(targets, generation),
            daemon=True,
        ).start()

    def _assembly_enrichment_worker(self, targets: dict, generation: int):
        """Fetch assembly metadata values from LCSC API in a worker thread.

        Thread ownership stays in mainwindow. This worker must not mutate store,
        datamodel, or BOM UI state directly; it only posts progress events back
        to the UI thread. The generation passed in is echoed back on every event
        so the UI thread can discard results from a superseded run.
        """
        provider = LCSCAssemblyMetadataProvider(min_interval_seconds=1.0)
        for lcsc, metadata in provider.fetch_iter(list(targets.keys())):
            refs = targets[lcsc]
            wx.PostEvent(
                self,
                AssemblyEnrichmentProgressEvent(
                    lcsc=lcsc, refs=refs, metadata=metadata, generation=generation
                ),
            )
        wx.PostEvent(
            self,
            AssemblyEnrichmentCompletedEvent(generation=generation),
        )

    def on_assembly_enrichment_progress(self, e):
        """Persist one enrichment result and update row-level feedback."""
        # Drop events from superseded enrichment runs. A reassignment between
        # spawn and event delivery would otherwise let stale metadata for the
        # old LCSC be written to a reference that now points elsewhere.
        generation = getattr(e, "generation", None)
        if generation is not None and generation != self.assembly_enrichment_generation:
            return
        lcsc = getattr(e, "lcsc", "")
        refs = getattr(e, "refs", [])
        metadata = getattr(e, "metadata", {}) or {}

        assembly_process = metadata.get("assembly_process", "")
        component_product_type = metadata.get("component_product_type")
        for reference in refs:
            updated = self.store.set_assembly_metadata(
                reference,
                assembly_process,
                component_product_type,
                expected_lcsc=lcsc,
            )
            if updated:
                current_part = self.store.get_part(reference) or {}
                status = (
                    "Done"
                    if classify_component_product_type(
                        current_part.get("component_product_type")
                    )
                    is not None
                    else "Class missing"
                )
                self.partlist_data_model.set_enrichment_status(reference, status)

        self.pending_assembly_enrichment.discard(lcsc)

    def on_assembly_enrichment_completed(self, e):
        """Run a single BOM recompute after a worker finishes its batch.

        Per-progress events update store/datamodel rows individually but no
        longer trigger a recompute of their own; the cost estimate is refreshed
        once, here, when the worker's fetch_iter exhausts. Stale completion
        events from superseded runs are dropped.
        """
        generation = getattr(e, "generation", None)
        if generation is not None and generation != self.assembly_enrichment_generation:
            return
        self._refresh_bom_after_enrichment_update()

    def _refresh_bom_after_enrichment_update(self):
        """Main-thread boundary after enrichment updates.

        Called only from enrichment event handlers after per-row store/datamodel
        updates are applied on the UI thread. Delegates BOM rendering/recompute
        through the BOM controller path.
        """
        wx.PostEvent(self, BomDataChangedEvent(source="enrichment_update"))

    def display_message(self, e):
        """Dispaly a message with the data from the event."""
        styles = {
            "info": wx.ICON_INFORMATION,
            "warning": wx.ICON_WARNING,
            "error": wx.ICON_ERROR,
        }
        wx.MessageBox(e.text, e.title, style=styles.get(e.style, wx.ICON_INFORMATION))

    def get_correction(
        self, part: dict[str, Any], corrections: Sequence[Correction]
    ) -> str:
        """Display the same complete correction rule used for fabrication."""
        match = match_correction(
            corrections,
            str(part["reference"]),
            str(part["value"]),
            str(part["footprint"]),
        )
        if match is None:
            return "0°, 0.0/0.0"
        return f"{match.correction} ({match.source})"

    def update_correction_status(self, snapshot: CorrectionSnapshot) -> None:
        """Show aggregate readiness; detailed repair diagnostics stay in the manager."""
        unresolved = snapshot.corrections is None
        if unresolved:
            reason = (
                "unavailable"
                if snapshot.state == CorrectionState.UNAVAILABLE
                else "unresolved"
            )
            label = (
                f"Corrections {reason} in the active {snapshot.scope} database.\n"
                "Open Corrections Manager to repair or retry loading before generating fabrication files."
            )
            details = str(snapshot.db_path)
        else:
            label = ""
            details = ""
        self.correction_status.SetLabel(label)
        self.correction_status.SetToolTip(details)
        self.correction_status.Show(unresolved)
        self.Layout()

    def read_valid_corrections_for_generation(self) -> tuple[Correction, ...]:
        """Read a fresh complete snapshot before any generation side effects."""
        snapshot = self.library.read_correction_data()
        self.update_correction_status(snapshot)
        if snapshot.corrections is None:
            raise ValueError(
                f"Corrections are unresolved in the active {snapshot.scope} "
                f"database ({snapshot.db_path}). Open Corrections Manager "
                "to repair or retry loading before generating fabrication files."
            )
        return snapshot.corrections

    def populate_footprint_list(self, *_: object) -> None:
        """Populate list of footprints."""
        if not self.store:
            if not self._project_storage_unavailable and self.is_catalog_available():
                self.init_store()
            else:
                self.partlist_data_model.RemoveAll()
            return
        try:
            self._populate_footprint_rows()
        except (sqlite3.Error, OSError) as error:
            self._set_project_storage_error(error)

    def _populate_footprint_rows(self) -> None:
        """Read a complete project view, allowing the caller to recover storage errors."""
        self.partlist_data_model.RemoveAll()
        parts = self.store.read_all()
        snapshot = self.library.read_correction_data()
        self.update_correction_status(snapshot)
        corrections = snapshot.corrections
        for part in parts:
            fp = self.pcbnew.GetBoard().FindFootprintByReference(part["reference"])
            if fp is None:
                continue
            is_dnp = get_is_dnp(fp)
            # Warm all live assignments before view filters, for every stock consumer.
            details = self._catalog_get_part_details(part["lcsc"])
            # don't show the part if hide BOM is set
            if self.hide_bom_parts and part["exclude_from_bom"]:
                continue
            # don't show the part if hide POS is set
            if self.hide_pos_parts and part["exclude_from_pos"]:
                continue
            self.partlist_data_model.AddEntry(
                [
                    part["reference"],
                    part["value"],
                    part["footprint"],
                    part["lcsc"],
                    details.get("type", ""),  # type
                    details.get("stock", ""),  # stock
                    part["exclude_from_bom"],
                    part["exclude_from_pos"],
                    int(is_dnp),
                    (
                        str(self.get_correction(part, corrections))
                        if corrections is not None
                        else "Unresolved"
                    ),
                    str(fp.GetLayer()),
                    params_for_part(details),
                    self._get_enrichment_status_label(part),  # enrichment
                    "",  # bom price label
                ]
            )
        wx.PostEvent(self, BomDataChangedEvent(source="populate_footprint_list"))

    def OnBomHide(self, *_):
        """Hide all parts from the list that have 'in BOM' set to No."""
        self.hide_bom_parts = not self.hide_bom_parts
        if self.hide_bom_parts:
            self.hide_bom_button.SetNormalBitmap(
                loadBitmapScaled(
                    "",
                    self.scale_factor,
                )
            )
            self.hide_bom_button.SetNormalBitmap(
                loadBitmapScaled(
                    "mdi-eye-outline.png",
                    self.scale_factor,
                )
            )
            self.hide_bom_button.SetLabel("Show excluded BOM")
        else:
            self.hide_bom_button.SetNormalBitmap(
                loadBitmapScaled(
                    "",
                    self.scale_factor,
                )
            )
            self.hide_bom_button.SetNormalBitmap(
                loadBitmapScaled(
                    "mdi-eye-off-outline.png",
                    self.scale_factor,
                )
            )
            self.hide_bom_button.SetLabel("Hide excluded BOM")
        self.populate_footprint_list()

    def OnPosHide(self, *_):
        """Hide all parts from the list that have 'in pos' set to No."""
        self.hide_pos_parts = not self.hide_pos_parts
        if self.hide_pos_parts:
            self.hide_pos_button.SetNormalBitmap(
                loadBitmapScaled(
                    "",
                    self.scale_factor,
                )
            )
            self.hide_pos_button.SetNormalBitmap(
                loadBitmapScaled(
                    "mdi-eye-outline.png",
                    self.scale_factor,
                )
            )
            self.hide_pos_button.SetLabel("Show excluded POS")
        else:
            self.hide_pos_button.SetNormalBitmap(
                loadBitmapScaled(
                    "",
                    self.scale_factor,
                )
            )
            self.hide_pos_button.SetNormalBitmap(
                loadBitmapScaled(
                    "mdi-eye-off-outline.png",
                    self.scale_factor,
                )
            )
            self.hide_pos_button.SetLabel("Hide excluded POS")
        self.populate_footprint_list()

    def OnFootprintSelected(self, *_):
        """Enable the toolbar buttons when a selection was made."""
        if self.select_alike_in_progress:
            return

        self.enable_part_specific_toolbar_buttons(
            self.footprint_list.GetSelectedItemsCount() > 0
        )

        if self.auto_select_alike and self.footprint_list.GetSelectedItemsCount() == 1:
            self.select_alike_parts()

        # clear the present selections
        selection = self.pcbnew.GetCurrentSelection()
        for selected in selection:
            selected.ClearSelected()

        # select all of the selected items in the footprint_list
        if self.footprint_list.GetSelectedItemsCount() > 0:
            for item in self.footprint_list.GetSelections():
                ref = self.partlist_data_model.get_reference(item)
                fp = self.pcbnew.GetBoard().FindFootprintByReference(ref)
                if fp:
                    fp.SetSelected()
            # cause pcbnew to refresh the board with the changes to the selected footprint(s)
            self.pcbnew.Refresh()

    def enable_part_specific_toolbar_buttons(self, state):
        """Control the state of all the buttons that relate to parts in toolbar on the right side."""
        for button in (
            ID_SELECT_PART,
            ID_REMOVE_LCSC_NUMBER,
            ID_TOGGLE_BOM_POS,
            ID_TOGGLE_BOM,
            ID_TOGGLE_POS,
            ID_PART_DETAILS,
            ID_HIDE_BOM,
            ID_HIDE_POS,
        ):
            self.right_toolbar.EnableTool(button, state)

    def toggle_bom_pos(self, *_):
        """Toggle the exclude from BOM/POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                continue
            bom = toggle_exclude_from_bom(fp)
            pos = toggle_exclude_from_pos(fp)
            self.store.set_bom(ref, int(bool(bom)))
            self.store.set_pos(ref, int(bool(pos)))
            self.partlist_data_model.toggle_bom_pos(item)
        wx.PostEvent(self, BomDataChangedEvent(source="toggle_bom_pos"))

    def toggle_bom(self, *_):
        """Toggle the exclude from BOM attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                continue
            bom = toggle_exclude_from_bom(fp)
            self.store.set_bom(ref, int(bool(bom)))
            self.partlist_data_model.toggle_bom(item)
        wx.PostEvent(self, BomDataChangedEvent(source="toggle_bom"))

    def toggle_pos(self, *_):
        """Toggle the exclude from POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                continue
            pos = toggle_exclude_from_pos(fp)
            self.store.set_pos(ref, int(bool(pos)))
            self.partlist_data_model.toggle_pos(item)
        wx.PostEvent(self, BomDataChangedEvent(source="toggle_pos"))

    def remove_lcsc_number(self, *_: object) -> None:
        """Clear selected assignments after committing one project transaction."""
        if self.store is None:
            return
        selected = []
        # wx selection items borrow storage from this native array. Keep it
        # alive until the post-commit model updates have finished using them.
        selections = self.footprint_list.GetSelections()
        for item in selections:
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                continue
            selected.append((item, ref, fp))
        if not selected:
            return
        try:
            self.store.set_lcsc_assignments(
                (ref, "", None) for _item, ref, _fp in selected
            )
        except sqlite3.Error as error:
            self.logger.warning("Unable to clear LCSC assignments: %s", error)
            return
        for item, _ref, fp in selected:
            set_lcsc_value(fp, "")
            self.partlist_data_model.remove_lcsc_number(item)
        wx.PostEvent(self, BomDataChangedEvent(source="remove_lcsc_number"))

    def select_alike_parts(self, *_):
        """Select all alike parts, starting from a single selected part."""
        if self.footprint_list.GetSelectedItemsCount() > 1:
            self.logger.warning("Select only one component, please.")
            return
        selected_item = self.footprint_list.GetSelection()
        self.select_alike_in_progress = True
        try:
            for alike_item in self.partlist_data_model.select_alike(selected_item):
                if not self.footprint_list.IsSelected(alike_item):
                    self.footprint_list.Select(alike_item)
        finally:
            self.select_alike_in_progress = False

    def toggle_select_alike(self, e):
        """Toggle auto-selecting alike parts on selection."""
        self.auto_select_alike = bool(e.IsChecked())
        self.settings.setdefault("general", {})["select_alike_auto"] = (
            self.auto_select_alike
        )
        self.save_settings()
        if self.auto_select_alike and self.footprint_list.GetSelectedItemsCount() == 1:
            self.select_alike_parts()

    def get_part_details(self, *_: object) -> None:
        """Show one modeless Part Details window per selected LCSC number."""
        seen: set[str] = set()
        for item in self.footprint_list.GetSelections():
            lcsc = self.partlist_data_model.get_lcsc(item)
            if not lcsc or lcsc in seen:
                continue
            seen.add(lcsc)
            self.show_part_details_dialog(lcsc)

    def show_part_details_dialog(self, part):
        """Show the part details dialog (modeless so it doesn't block the app)."""
        dialog = PartDetailsDialog(self, part)
        dialog.Show()

    def update_library(self, *_: object) -> None:
        """Update the library from the JLCPCB CSV file."""
        if self.library is not None:
            self.library.update()

    def manage_corrections(self, *_: object) -> None:
        """Refresh displayed corrections after the manager's recovery attempts."""
        CorrectionManagerDialog(self, "").ShowModal()
        self.populate_footprint_list()

    def manage_part_preferences(self, *_: object) -> None:
        """Manage shared part preferences."""
        PartPreferencesDialog(self).ShowModal()

    def manage_settings(self, *_):
        """Manage settings."""
        SettingsDialog(self).ShowModal()

    def update_settings(self, e: Any) -> None:
        """Update the settings on change."""
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value

        if e.section == "general":
            if e.setting == "simplify_stock":
                self.partlist_data_model.set_simplify_stock(bool(e.value))
                selector = getattr(self, "_part_selector", None)
                if selector is not None:
                    selector.part_list_model.set_simplify_stock(bool(e.value))
            elif e.setting == "bom_estimator_show":
                self.bom_estimator_show = bool(e.value)
                self.bom_widget.set_visible(self.bom_estimator_show)
                if (
                    not self.bom_estimator_show
                    and self._why_standard_dialog is not None
                ):
                    self._why_standard_dialog.Close()
                self.Layout()
        elif e.section == "highlighting":
            if e.setting == "matches":
                self.footprint_list.Refresh()
            elif e.setting == "stock_concern":
                self.recompute_stock_concerns()

        self.save_settings()

        if e.section == "library" and e.setting in ["selected_library", "data_path"]:
            self._apply_library_settings()

    def _apply_library_settings(self) -> None:
        """Switch catalogs after any active download has released its source paths."""
        self._catalog_ready = False
        self._invalidate_catalog_details()
        if self.library is not None and self.library.is_download_running():
            self._catalog_switch_pending = True
            self._refresh_catalog_views()
            return
        self._catalog_switch_pending = False
        try:
            if self.library is None:
                self.init_data(download_if_missing=False)
            else:
                if self.library.refresh_library_config() is False:
                    self._catalog_switch_pending = True
                    self._refresh_catalog_views()
                    return
                if self.library.state == LibraryState.INITIALIZED:
                    self._publish_catalog()
                else:
                    if self._project_storage_unavailable:
                        self.init_store()
                    self._clear_catalog_views()
        except (sqlite3.Error, OSError, ValueError) as error:
            self._set_project_storage_error(error)
            self._clear_catalog_views()

    def logbox_append(self, e):
        """Write text to the logbox."""
        self.logbox.WriteText(e.msg)

    def load_settings(self) -> None:
        """Load settings from settings.json."""
        with open(os.path.join(PLUGIN_PATH, "settings.json"), encoding="utf-8") as j:
            self.settings = json.load(j)

        general_settings = self.settings.setdefault("general", {})
        gerber_settings = self.settings.setdefault("gerber", {})
        highlighting_settings = self.settings.setdefault("highlighting", {})
        partselector_settings = self.settings.setdefault("partselector", {})
        part_preferences_settings = self.settings.setdefault("part_preferences", {})
        migrated = False

        if "simplify_stock" not in general_settings:
            general_settings["simplify_stock"] = True
            migrated = True

        if "stock_concern" not in highlighting_settings:
            highlighting_settings["stock_concern"] = True
            migrated = True

        for setting in (
            "remember_lcsc_assignments",
            "fill_empty_lcsc_assignments_on_open",
        ):
            if setting not in part_preferences_settings:
                part_preferences_settings[setting] = True
                migrated = True

        if "matches" not in highlighting_settings:
            if "highlight_matches" in partselector_settings:
                highlighting_settings["matches"] = partselector_settings.pop(
                    "highlight_matches"
                )
                migrated = True
            else:
                highlighting_settings["matches"] = True
                migrated = True

        if gerber_settings.get("force_drc", False) and not gerber_settings.get(
            "fill_zones", True
        ):
            gerber_settings["fill_zones"] = True
            migrated = True

        if "subtract_mask_from_silk" not in gerber_settings:
            gerber_settings["subtract_mask_from_silk"] = True
            migrated = True

        if migrated:
            self.save_settings()

    def decode_mainwindow_highlight_value(self, value: str) -> tuple[str, list[str]]:
        """Decode params cell text, optionally disabling highlight terms by setting."""
        text, terms = decode_highlighted_value(value)
        if not self.settings.get("highlighting", {}).get("matches", True):
            return text, []
        return text, terms

    def save_settings(self) -> None:
        """Replace settings.json only after the complete document is written."""
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=PLUGIN_PATH, delete=False
            ) as settings_file:
                temporary_path = settings_file.name
                json.dump(self.settings, settings_file)
            os.replace(temporary_path, os.path.join(PLUGIN_PATH, "settings.json"))
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    os.unlink(temporary_path)

    def select_part(self, *_):
        """Select a part from the library and assign it to the selected footprint(s)."""
        selection = {}
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            value = self.partlist_data_model.get_value(item)
            footprint = self.partlist_data_model.get_footprint(item)
            if ref.startswith("R"):
                """ Auto remove alphabet unit if applicable """
                if value.endswith("R") or value.endswith("r") or value.endswith("o"):
                    value = value[:-1]
                value += "Ω"
            if simplified_footprint := simplify_footprint_name(footprint):
                value += f" {simplified_footprint}"
            selection[ref] = value
        if self._part_selector is not None:
            # Already open — re-target it at the new selection rather than
            # spawning a second window.
            self._part_selector.update_for(selection)
            self._part_selector.Raise()
            return
        # The selector clears self._part_selector itself from its EVT_CLOSE
        # handler before it destroys — no need for a destroy hook here.
        self._part_selector = PartSelectorDialog(self, selection)
        self._part_selector.Show()
        self._part_selector.Raise()

    def count_order_number_placeholders(self):
        """Count the JLC order/serial number placeholders."""
        count = 0
        for drawing in iter_board_items(self.pcbnew.GetBoard().Drawings()):
            if drawing.IsOnLayer(kicad_pcbnew.F_SilkS) or drawing.IsOnLayer(
                kicad_pcbnew.B_SilkS
            ):
                if isinstance(drawing, kicad_pcbnew.PCB_TEXT):
                    if drawing.GetText().strip() == "JLCJLCJLCJLC":
                        self.logger.info(
                            "Found placeholder for order number at %.1f/%.1f.",
                            kicad_pcbnew.ToMM(drawing.GetCenter().x),
                            kicad_pcbnew.ToMM(drawing.GetCenter().y),
                        )
                        count += 1

                if (
                    isinstance(drawing, kicad_pcbnew.PCB_SHAPE)
                    and drawing.GetShape() == kicad_pcbnew.S_RECT
                    and (
                        (hasattr(drawing, "IsFilled") and drawing.IsFilled())
                        or (hasattr(drawing, "IsSolidFill") and drawing.IsSolidFill())
                    )
                ):
                    corners = drawing.GetRectCorners()

                    top_left_x = min([p.x for p in corners], default=0)
                    top_left_y = min([p.y for p in corners], default=0)
                    bottom_right_x = max([p.x for p in corners], default=0)
                    bottom_right_y = max([p.y for p in corners], default=0)
                    width = kicad_pcbnew.ToMM(bottom_right_x - top_left_x)
                    height = kicad_pcbnew.ToMM(bottom_right_y - top_left_y)

                    if (
                        (width == 5 and height == 5)
                        or (width == 8 and height == 8)
                        or (width == 10 and height == 10)
                    ):
                        self.logger.info(
                            "Found placeholder for 2D barcode (%dmm x %dmm) at %.1f/%.1f.",
                            width,
                            height,
                            kicad_pcbnew.ToMM(drawing.GetCenter().x),
                            kicad_pcbnew.ToMM(drawing.GetCenter().y),
                        )
                        count += 1

                    if (width == 10 and height == 2) or (width == 2 and height == 10):
                        self.logger.info(
                            "Found placeholder for serial number at %.1f/%.1f.",
                            kicad_pcbnew.ToMM(drawing.GetCenter().x),
                            kicad_pcbnew.ToMM(drawing.GetCenter().y),
                        )
                        count += 1

        return count

    def build_generate_hook_env(self, stage, placeholder_count, generation_count):
        """Build environment variables for configured generation hooks."""
        board_filename = self.pcbnew.GetBoard().GetFileName()
        artifact_paths = self.fabrication.get_artifact_paths()
        env = os.environ.copy()
        env.update(
            {
                "JLCPCB_HOOK_STAGE": stage,
                "JLCPCB_BOARD_PATH": board_filename,
                "JLCPCB_PROJECT_DIR": self.project_path,
                "JLCPCB_OUTPUT_DIR": self.fabrication.outputdir,
                "JLCPCB_GERBER_DIR": self.fabrication.gerberdir,
                "JLCPCB_GENERATION_COUNT": str(generation_count),
                "JLCPCB_PLACEHOLDER_COUNT": str(placeholder_count),
                "JLCPCB_ARTIFACT_GERBER_ZIP": artifact_paths["gerber_zip"],
                "JLCPCB_ARTIFACT_BOM_CSV": artifact_paths["bom_csv"],
                "JLCPCB_ARTIFACT_CPL_CSV": artifact_paths["cpl_csv"],
            }
        )
        return env

    def run_generate_hook(self, stage, env, allow_continue):
        """Run one configured generation hook and handle UI prompts on failures."""
        hooks_settings = self.settings.get("hooks", {})
        result = run_configured_hook(
            stage=stage,
            hooks_settings=hooks_settings,
            env_updates=env,
            working_dir=self.project_path,
            logger=self.logger,
        )
        if not result.command:
            return True

        if result.succeeded:
            return True

        error_text = format_hook_error(result)
        if allow_continue:
            dialog = wx.MessageDialog(
                self,
                f"The {stage}-generate hook failed.\n\n{error_text}\n\nContinue generation anyway?",
                "Pre-generate hook failed",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING | wx.CENTER,
            )
            dialog.SetYesNoLabels("Continue", "Cancel")
            choice = dialog.ShowModal()
            dialog.Destroy()
            return choice == wx.ID_YES

        wx.MessageBox(
            f"The {stage}-generate hook failed after generation completed.\n\n{error_text}",
            "Post-generate hook failed",
            style=wx.ICON_WARNING,
        )
        return False

    def generate_fabrication_data(self, *_: object) -> None:
        """Generate fabrication data."""
        if self._project_storage_unavailable:
            self.logger.warning(
                "Cannot generate fabrication files while part assignments are unavailable."
            )
            return
        self.generate_button.Enable(False)
        self.reset_gauge()
        wx.BeginBusyCursor()
        self._current_generation_step = "initialization"
        try:
            corrections = self.run_generation_step(
                "Validating corrections",
                self.read_valid_corrections_for_generation,
            )
            placements = self.run_generation_step(
                "Preparing placement data", self.fabrication.prepare_cpl, corrections
            )
            warnings = self.run_generation_step(
                "Checking part consistency",
                self.fabrication.get_part_consistency_warnings,
            )
            if warnings:
                result = wx.MessageBox(
                    "There are items with identical LCSC number but different values in the list:\n"
                    + warnings
                    + "Continue?",
                    "Plausibility check",
                    wx.OK | wx.CANCEL | wx.CENTER,
                )
                if result == wx.CANCEL:
                    self.report_generation_step(
                        "Cancelled by user during plausibility check"
                    )
                    return

            if self.settings.get("general", {}).get("order_number"):
                count = self.run_generation_step(
                    "Checking order/serial placeholders",
                    self.count_order_number_placeholders,
                )
                if count == 0:
                    result = wx.MessageBox(
                        "JLC order/serial number placeholder not present! Continue?",
                        "JLC order/serial number placeholder",
                        wx.OK | wx.CANCEL | wx.CENTER,
                    )
                    if result == wx.CANCEL:
                        self.report_generation_step(
                            "Cancelled by user due to missing placeholder"
                        )
                        return
                elif count > 1:
                    result = wx.MessageBox(
                        "Multiple order/serial number placeholders present! Continue?",
                        "JLC order/serial number placeholder",
                        wx.OK | wx.CANCEL | wx.CENTER,
                    )
                    if result == wx.CANCEL:
                        self.report_generation_step(
                            "Cancelled by user due to multiple placeholders"
                        )
                        return

            refill = self.settings.get("gerber", {}).get("fill_zones", True)
            empty_pours = self.run_generation_step(
                "Filling copper zones" if refill else "Checking copper zone fills",
                self.fabrication.fill_zones,
            )
            if empty_pours:
                listed = "\n".join(f"  {pour}" for pour in empty_pours)
                dialog = wx.MessageDialog(
                    self,
                    f"These copper zones contain no filled copper:\n\n{listed}\n\n"
                    "Plotting now ships the board without that copper.",
                    "Empty copper zones",
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING | wx.CENTER,
                )
                try:
                    dialog.SetYesNoLabels("Continue Anyway", "Cancel Export")
                    result = dialog.ShowModal()
                finally:
                    dialog.Destroy()
                self.logger.warning(
                    "Copper zones with no filled copper, user chose to %s export:\n%s",
                    "continue" if result == wx.ID_YES else "stop",
                    listed,
                )
                if result != wx.ID_YES:
                    self.report_generation_step("Export stopped by empty copper zones")
                    return

            drc_ok = self.run_generation_step(
                "Running pre-export DRC check",
                self.run_drc_before_gerber_export,
            )
            if not drc_ok:
                self.report_generation_step("Export stopped by DRC check")
                return

            layer_selection = self.layer_selection.GetSelection()
            number = re.search(r"\d+", self.layer_selection.GetString(layer_selection))
            if number:
                layer_count = int(number.group(0))
            else:
                layer_count = None

            if self.settings.get("general", {}).get("order_number"):
                placeholder_count = count
            else:
                # Only the generation hooks need the count here, but a failure
                # must still be attributed to this step rather than the last one.
                placeholder_count = self.run_generation_step(
                    "Counting order/serial placeholders",
                    self.count_order_number_placeholders,
                )

            current_generation_count = self.store.get_generation_count()
            pre_hook_env = self.build_generate_hook_env(
                stage="pre",
                placeholder_count=placeholder_count,
                generation_count=current_generation_count,
            )
            if not self.run_generate_hook("pre", pre_hook_env, allow_continue=True):
                return

            self.run_generation_step(
                "Plotting Gerbers",
                self.fabrication.generate_geber,
                layer_count,
            )

            self.run_generation_step(
                "Generating Excellon drill/map files",
                self.fabrication.generate_excellon,
            )

            self.run_generation_step(
                "Creating Gerber archive (.zip)",
                self.fabrication.zip_gerber_excellon,
            )

            self.run_generation_step(
                "Generating placement file (CPL)",
                self.fabrication.write_cpl,
                placements,
            )

            self.run_generation_step(
                "Generating BOM",
                self.fabrication.generate_bom,
            )

            generation_count = self.store.increment_generation_count()
            post_hook_env = self.build_generate_hook_env(
                stage="post",
                placeholder_count=placeholder_count,
                generation_count=generation_count,
            )
            self.run_generate_hook("post", post_hook_env, allow_continue=False)

            self.report_generation_step("Fabrication data generation complete")
            self.reset_gauge()
        except Exception as exc:
            self.logger.exception(
                "Fabrication data generation failed during %s",
                self._current_generation_step,
            )
            wx.MessageBox(
                f"Fabrication data generation failed during: {self._current_generation_step}\n\n{exc}",
                "Generate fabrication data",
                wx.OK | wx.ICON_ERROR | wx.CENTER,
            )
        finally:
            self._current_generation_step = "initialization"
            self.reset_gauge()
            if wx.IsBusy():
                wx.EndBusyCursor()
            self.generate_button.Enable(True)

    def save_board_for_drc(self):
        """Save the current board so DRC checks operate on latest board state."""
        board = self.pcbnew.GetBoard()
        board_filename = board.GetFileName()
        if not board_filename:
            raise RuntimeError("Board must be saved before running DRC checks")

        if hasattr(board, "Save"):
            try:
                board.Save(board_filename)
            except TypeError:
                board.Save()
            return

        if hasattr(self.pcbnew, "SaveBoard"):
            self.pcbnew.SaveBoard(board_filename, board)
            return

        raise RuntimeError("Unable to save board using current KiCad API")

    def run_drc_before_gerber_export(self):
        """Run optional DRC via KiCad Python API and prompt when violations exist."""
        if not self.settings.get("gerber", {}).get("force_drc", False):
            return True

        board_filename = self.pcbnew.GetBoard().GetFileName()
        if not board_filename:
            wx.MessageBox(
                "Board must be saved before DRC can be run.",
                "DRC check",
                style=wx.ICON_ERROR,
            )
            return False

        try:
            self.save_board_for_drc()
        except Exception as exc:
            wx.MessageBox(
                f"Failed to save board before DRC: {exc}",
                "DRC check",
                style=wx.ICON_ERROR,
            )
            return False

        try:
            drc_counter = DRCViolationCounter(
                pcbnew_module=self.pcbnew,
                working_dir=self.project_path,
            )
            self.flush_generation_ui()
            violation_count = drc_counter.get_violation_count(board_filename)

            if violation_count > 0:
                dialog = wx.MessageDialog(
                    self,
                    f"DRC found {violation_count} error violation(s).\n\n"
                    "Resolve or exclude DRC errors before manufacturing whenever possible.",
                    "DRC violations found",
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING | wx.CENTER,
                )
                dialog.SetYesNoLabels("Continue Anyway", "Cancel Export")
                result = dialog.ShowModal()
                dialog.Destroy()
                if result != wx.ID_YES:
                    return False

            return True
        except Exception as exc:
            self.logger.exception("Unexpected error while running forced DRC")
            self.report_generation_step(f"DRC check failed: {exc}")
            wx.MessageBox(
                f"Unexpected error while running DRC: {exc}",
                "DRC check",
                style=wx.ICON_ERROR,
            )
            return False

    def copy_part_lcsc(self, *_):
        """Fetch part details from LCSC and show them in a modal."""
        for item in self.footprint_list.GetSelections():
            if lcsc := self.partlist_data_model.get_lcsc(item):
                if wx.TheClipboard.Open():
                    wx.TheClipboard.SetData(wx.TextDataObject(lcsc))
                    wx.TheClipboard.Close()

    def paste_part_lcsc(self, *_: object) -> None:
        """Paste a lcsc number from the clipboard to the current part."""
        text_data = wx.TextDataObject()
        success = False
        if wx.TheClipboard.Open():
            success = wx.TheClipboard.GetData(text_data)
            wx.TheClipboard.Close()
        if success:
            if (lcsc := self.sanitize_lcsc(text_data.GetText())) != "":
                references = [
                    self.partlist_data_model.get_reference(item)
                    for item in self.footprint_list.GetSelections()
                ]
                if not references or not self._can_apply_user_assignments():
                    return
                self._apply_lcsc_assignments(
                    dict.fromkeys(references, lcsc), remember_part_preferences=True
                )

    def add_correction(self, e: wx.CommandEvent) -> None:
        """Add part correction for the current part."""
        for item in self.footprint_list.GetSelections():
            if e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE:
                if reference := self.partlist_data_model.get_reference(item):
                    CorrectionManagerDialog(
                        self, "^" + re.escape(reference) + "$"
                    ).ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE:
                if footprint := self.partlist_data_model.get_footprint(item):
                    CorrectionManagerDialog(
                        self, "^" + re.escape(footprint)
                    ).ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_NAME:
                if value := self.partlist_data_model.get_value(item):
                    CorrectionManagerDialog(self, re.escape(value)).ShowModal()
        self.populate_footprint_list()

    def export_to_schematic(self, *_):
        """Dialog to select schematics."""
        with wx.FileDialog(
            self,
            "Select Schematics",
            self.project_path,
            self.schematic_name,
            "KiCad V6 Schematics (*.kicad_sch)|*.kicad_sch",
            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as openFileDialog:
            if openFileDialog.ShowModal() == wx.CANCEL:
                return
            paths = openFileDialog.GetPaths()
            SchematicExport(self).load_schematic(paths)

    def save_selected_part_preferences(self, *_: object) -> None:
        """Remember the selected LCSC assignments as part preferences."""
        preferences = []
        for item in self.footprint_list.GetSelections():
            footprint = self.partlist_data_model.get_footprint(item)
            value = self.partlist_data_model.get_value(item)
            lcsc = self.partlist_data_model.get_lcsc(item)
            preferences.append((footprint, value, lcsc))
        self._save_part_preferences(preferences)

    def apply_selected_part_preferences(self, *_: object) -> None:
        """Apply matching part preferences to the selected rows."""
        if not self._can_apply_user_assignments():
            return
        assignments = {}
        try:
            for item in self.footprint_list.GetSelections():
                reference = self.partlist_data_model.get_reference(item)
                footprint = self.partlist_data_model.get_footprint(item)
                value = self.partlist_data_model.get_value(item)
                if footprint and value:
                    if preference := self.library.get_part_preference(footprint, value):
                        assignments[reference] = preference
            updated_references = self._apply_lcsc_assignments(assignments)
        except sqlite3.Error as error:
            self.logger.warning("Unable to apply part preferences: %s", error)
            return
        if updated_references:
            self.logger.info(
                "Applied part preferences to %d assignment(s).", len(updated_references)
            )

    def sanitize_lcsc(self, lcsc_PN: str) -> str:
        """Sanitize a given LCSC number using a regex."""
        m = re.search("C\\d+", lcsc_PN, re.IGNORECASE)
        if m:
            return m.group(0).upper()
        return ""

    def OnRightDown(self, *_: object) -> None:
        """Right click context menu for action on parts table."""
        right_click_menu = wx.Menu()

        copy_lcsc = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_COPY_LCSC, "Copy LCSC"
        )
        right_click_menu.Append(copy_lcsc)
        right_click_menu.Bind(wx.EVT_MENU, self.copy_part_lcsc, copy_lcsc)

        paste_lcsc = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_PASTE_LCSC, "Paste LCSC"
        )
        right_click_menu.Append(paste_lcsc)
        right_click_menu.Bind(wx.EVT_MENU, self.paste_part_lcsc, paste_lcsc)

        correction_by_reference = wx.MenuItem(
            right_click_menu,
            ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE,
            "Add Correction by reference",
        )
        right_click_menu.Append(correction_by_reference)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_reference)

        correction_by_package = wx.MenuItem(
            right_click_menu,
            ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE,
            "Add Correction by package",
        )
        right_click_menu.Append(correction_by_package)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_package)

        correction_by_name = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_ADD_ROT_BY_NAME, "Add Correction by name"
        )
        right_click_menu.Append(correction_by_name)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_name)

        apply_part_preferences = wx.MenuItem(
            right_click_menu,
            ID_CONTEXT_MENU_APPLY_PART_PREFERENCES,
            "Apply part preferences",
        )
        right_click_menu.Append(apply_part_preferences)
        right_click_menu.Bind(
            wx.EVT_MENU, self.apply_selected_part_preferences, apply_part_preferences
        )

        save_part_preferences = wx.MenuItem(
            right_click_menu,
            ID_CONTEXT_MENU_SAVE_PART_PREFERENCES,
            "Save part preferences",
        )
        right_click_menu.Append(save_part_preferences)
        right_click_menu.Bind(
            wx.EVT_MENU, self.save_selected_part_preferences, save_part_preferences
        )

        self.footprint_list.PopupMenu(right_click_menu)
        right_click_menu.Destroy()  # destroy to avoid memory leak

    def init_logger(self):
        """Initialize logger to log into textbox."""
        root = logging.getLogger()
        # Clear any existing handlers that might be problematic
        root.handlers.clear()
        root.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(funcName)s -  %(message)s",
            datefmt="%Y.%m.%d %H:%M:%S",
        )
        # Only add stderr handler if stderr is available
        if sys.stderr is not None:
            self.logging_handler1 = logging.StreamHandler(sys.stderr)
            self.logging_handler1.setLevel(logging.DEBUG)
            self.logging_handler1.setFormatter(formatter)
            root.addHandler(self.logging_handler1)

        self.logging_handler2 = LogBoxHandler(self)
        self.logging_handler2.setLevel(logging.DEBUG)
        self.logging_handler2.setFormatter(formatter)
        root.addHandler(self.logging_handler2)

        self.logger = logging.getLogger(__name__)

    def __del__(self):
        """Cleanup."""
        pass


class LogBoxHandler(logging.StreamHandler):
    """Logging class for the logging textbox at th ebottom of the mainwindow."""

    def __init__(self, event_destination):
        logging.StreamHandler.__init__(self)
        self.event_destination = event_destination

    def emit(self, record):
        """Marshal the event over to the main thread."""
        try:
            msg = self.format(record)
            wx.QueueEvent(self.event_destination, LogboxAppendEvent(msg=f"{msg}\n"))
        except Exception:
            self.handleError(record)
