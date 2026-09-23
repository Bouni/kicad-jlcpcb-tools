"""Contains the main window of the plugin."""

from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# ruff: noqa: I001, UP045

from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack, contextmanager, suppress
from copy import deepcopy
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, Optional
import logging
import os
import re
import sqlite3
import sys
import time

import pcbnew as kicad_pcbnew
import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error
from wx import adv  # pylint: disable=import-error

from .board_context import BoardContextChanged, board_identity
from .board_part_edits import BoardEditRecoveryError, apply_board_part_edits
from .bom_estimation.assembly_mode import classify_component_product_type
from .bom_estimation.help_text import show_bom_estimator_help
from .bom_widget import BomEstimatorController, BomEstimatorWidget
from .core.settings_persistence import (
    VariantPreferencePatch,
    load_settings_document,
    save_settings_document,
)
from .correction_data import Correction, match_correction
from .corrections import CorrectionManagerDialog
from .datamodel import PartListDataModel
from .dataview_highlight import (
    HighlightedTextRenderer,
    decode_highlighted_value,
    simplify_footprint_name,
)
from .derive_params import params_for_part
from .enrichment.worker import AssemblyMetadataLookup
from .events import (
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
from .lcsc import normalize_lcsc
from .partdetails import PartDetailsDialog
from .part_preferences import PartPreferencesDialog
from .partselector import PartSelectorDialog
from .schematic_safety import (
    SchematicLockedError,
    authenticated_project_name,
    resolve_project_schematics,
)
from .schematicexport import SchematicExport
from .settings import SettingsDialog
from .store import Store
from .stock_concern import stock_concern_references
from .type_cell_tooltip import TypeCellTooltip
from .why_standard_dialog import WhyStandardDialog
from .window_layout import get_column_widths, restore_column_widths

FOOTPRINT_COLUMN_KEYS = {
    index: key
    for key, index in PartListDataModel.columns.items()
    if key not in {"TRAILING_SPACER_COL", "STANDARD_ONLY_COL", "ENRICH_COL"}
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
ID_CONTEXT_MENU_COPY_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_PASTE_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_NAME = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_APPLY_PART_PREFERENCES = wx.NewIdRef()
ID_CONTEXT_MENU_SAVE_PART_PREFERENCES = wx.NewIdRef()


def _board_has_variants(board: Any) -> bool:
    """Detect native variants independently of catalog or project initialization."""
    variant_names = getattr(board, "GetVariantNamesForUI", None)
    return callable(variant_names) and len(tuple(variant_names())) > 1


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
        board_action: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self.library: Optional[Library] = None
        self._catalog_details: dict[str, dict[str, Any]] = {}
        self._catalog_ready = False
        self._catalog_switch_pending = False
        self.store: Optional[Store] = None
        self._variant_controller = None
        self._closing = False
        self._saving_on_close = False
        self._generating = False
        self.pcbnew = kicad_provider.get_pcbnew()
        board = self.pcbnew.GetBoard()
        self._schematic_board_identity = board_identity(board)
        self._board_identity = board_identity(board)
        self._board_action = board_action
        self._board_unreliable = False
        self._ordinary_generating = False
        self._refreshing_board = False
        board_filename = str(board.GetFileName())
        if not board_filename.strip():
            raise ValueError("Save the PCB before opening the JLCPCB plugin.")
        board_suffix = os.path.splitext(board_filename)[1].lower()
        if board_suffix != ".kicad_pcb" or not os.path.isfile(board_filename):
            raise ValueError(
                "The PCB must have an existing .kicad_pcb file before opening "
                "the JLCPCB plugin."
            )
        # Catalog recovery must retain the table mode chosen at startup.
        self._variant_mode = _board_has_variants(board)
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
        self.window = wx.GetTopLevelParent(self)
        self.SetSize(HighResWxSize(self.window, wx.Size(1300, 800)))
        self.scale_factor = GetScaleFactor(self.window)
        self.project_path = os.path.split(self.pcbnew.GetBoard().GetFileName())[0]
        self.board_name = os.path.split(self.pcbnew.GetBoard().GetFileName())[1]
        self.schematic_name = f"{self.board_name.split('.')[0]}.kicad_sch"
        self.hide_bom_parts = False
        self.hide_pos_parts = False
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
        self.assembly_lookup = AssemblyMetadataLookup(
            self._apply_assembly_metadata,
            self._refresh_bom_after_enrichment_update,
            lambda message: self.logger.warning(
                "Assembly enrichment failed: %s", message
            ),
        )
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
            "Toggle exclude from BOM and POS attribute",
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

        self.Bind(wx.EVT_TOOL, self.select_part, self.select_part_button)
        self.Bind(wx.EVT_TOOL, self.remove_lcsc_number, self.remove_lcsc_number_button)
        self.Bind(wx.EVT_TOOL, self.toggle_select_alike, self.select_alike_button)
        self.Bind(wx.EVT_TOOL, self.toggle_bom_pos, self.toggle_bom_pos_button)
        self.Bind(wx.EVT_TOOL, self.toggle_bom, self.toggle_bom_button)
        self.Bind(wx.EVT_TOOL, self.toggle_pos, self.toggle_pos_button)
        self.Bind(wx.EVT_TOOL, self.get_part_details, self.part_details_button)
        self.Bind(wx.EVT_TOOL, self.OnBomHide, self.hide_bom_button)
        self.Bind(wx.EVT_TOOL, self.OnPosHide, self.hide_pos_button)

        self.right_toolbar.ToggleTool(ID_SELECT_ALIKE, self.auto_select_alike)

        self.right_toolbar.Realize()
        # Ensure the vertical toolbar is wide enough to avoid clipping tool
        # labels on Linux GTK / HiDPI without taking arbitrary extra space.
        toolbar_min_width = HighResWxSize(self.window, wx.Size(170, -1)).GetWidth()
        if hasattr(self.right_toolbar, "GetTextExtent"):
            with suppress(Exception):
                tool_labels = (
                    "Assign LCSC number",
                    "Remove LCSC number",
                    "Auto-select alike",
                    "Toggle BOM & POS",
                )
                text_widths = []
                for label in tool_labels:
                    extent = self.right_toolbar.GetTextExtent(label)
                    width = None
                    if hasattr(extent, "GetWidth") and callable(extent.GetWidth):
                        with suppress(Exception):
                            w = extent.GetWidth()
                            if isinstance(w, (int, float)):
                                width = w
                    if (
                        width is None
                        and hasattr(extent, "x")
                        and isinstance(extent.x, (int, float))
                    ):
                        width = extent.x
                    if width is None and isinstance(extent, (tuple, list)) and extent:
                        w = extent[0]
                        if isinstance(w, (int, float)):
                            width = w
                    if isinstance(width, (int, float)) and width > 0:
                        text_widths.append(width)
                if text_widths:
                    max_text_width = max(text_widths)
                    padding = HighResWxSize(self.window, wx.Size(24, -1)).GetWidth()
                    if max_text_width > 0:
                        toolbar_min_width = max(
                            toolbar_min_width, int(max_text_width + padding)
                        )
        self.right_toolbar.SetMinSize(wx.Size(toolbar_min_width, -1))

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
        self.footprint_list.AppendTextColumn(
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

        table_sizer.Add(self.right_toolbar, 0, wx.EXPAND, 5)
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
        self.Bind(EVT_BOM_DATA_CHANGED_EVENT, self.on_bom_data_changed)

        self.enable_part_specific_toolbar_buttons(False)

        self.init_logger()
        self.partlist_data_model = PartListDataModel(
            self.scale_factor,
            simplify_stock=self.settings.get("general", {}).get("simplify_stock", True),
        )
        self.footprint_list.AssociateModel(self.partlist_data_model)
        self._assembly_tooltip_text = ""
        self._footprint_list_main_window = (
            self.footprint_list.GetMainWindow() or self.footprint_list
        )
        self._type_cell_tooltip = TypeCellTooltip(
            self.footprint_list,
            PartListDataModel.columns["TYPE_COL"],
            self.partlist_data_model.get_assembly_tooltip,
            self._set_assembly_tooltip,
        )
        self.bom_estimator_controller = BomEstimatorController(
            read_parts=self.read_assembly_parts,
            get_part_details=self._bom_get_part_details,
            get_board=self._get_current_board,
            is_force_standard_enabled=lambda: self.bom_estimator_force_standard,
            set_price_label=self.partlist_data_model.set_bom_price,
            set_standard_only_refs=self._set_standard_only_refs,
            set_summary_text=self.bom_widget.set_summary_text,
            set_details_button_label=self.bom_widget.set_details_button_label,
        )

        try:
            self.init_data()
        except Exception:
            root = logging.getLogger()
            for name in ("logging_handler1", "logging_handler2"):
                handler = getattr(self, name, None)
                if handler is not None:
                    root.removeHandler(handler)
            self.Destroy()
            raise
        self.Bind(wx.EVT_ACTIVATE, self.on_window_activated)

    def Layout(self) -> bool:
        """Lay out the form after resizing or changing the visible controls."""
        result = wx.Frame.Layout(self)
        panel = getattr(self, "content_panel", None)
        if panel:
            panel.Layout()
        return result

    def init_data(self, *, download_if_missing: bool = True) -> None:
        """Initialize the library and populate the main window."""
        if controller := getattr(self, "_variant_controller", None):
            controller.catalog_changed()
            return
        try:
            self.init_library()
            self.init_fabrication()
            self.init_store()
            if self.library.state == LibraryState.UPDATE_NEEDED:
                if download_if_missing:
                    self.library.update()
                else:
                    self._clear_catalog_views()
        except (sqlite3.Error, OSError, ValueError, BoardContextChanged) as error:
            self._set_project_storage_error(error)

        self.logger.debug("kicad version: %s", kicad_pcbnew.GetBuildVersion())

    def _get_current_board(self) -> Any:
        """Validate the native board lifetime before accessing modeless state."""
        board = self.pcbnew.GetBoard()
        identity = board_identity(board)
        if identity != getattr(self, "_board_identity", identity):
            raise BoardContextChanged(
                "The PCB was closed, replaced, or saved under another name. "
                "Reopen JLCPCB Tools."
            )
        if getattr(self, "_board_unreliable", False):
            raise BoardContextChanged(
                "A board edit could not be restored. Inspect the PCB and reopen JLCPCB Tools."
            )
        if not getattr(self, "_variant_mode", False) and _board_has_variants(board):
            raise BoardContextChanged(
                "The PCB now contains variants. Reopen JLCPCB Tools to edit them."
            )
        return board

    def on_window_activated(self, event: wx.ActivateEvent) -> None:
        """Refresh external PCB edits and undo without applying opening preferences."""
        event.Skip()
        if (
            not event.GetActive()
            or getattr(self, "_closing", False)
            or getattr(self, "_refreshing_board", False)
            or getattr(self, "_ordinary_generating", False)
            or getattr(self, "_variant_controller", None)
            or self.store is None
        ):
            return
        self._refreshing_board = True
        try:
            self._get_current_board()
            self._refresh_footprints_preserving_selection()
            self.start_assembly_enrichment()
            self.recompute_stock_concerns()
            self.recompute_bom_estimate()
        except (BoardContextChanged, OSError, sqlite3.Error) as error:
            self._set_project_storage_error(error)
        finally:
            self._refreshing_board = False

    def _refresh_footprints_preserving_selection(self) -> None:
        """Rebuild native rows while retaining selected component references."""
        selected = {
            self.partlist_data_model.get_reference(item)
            for item in self.footprint_list.GetSelections()
        }
        self.populate_footprint_list()
        for row in self.partlist_data_model.get_all():
            if row[PartListDataModel.columns["REF_COL"]] in selected:
                self.footprint_list.Select(self.partlist_data_model.ObjectToItem(row))

    def _apply_board_change(self, change: Callable[[], None], action: str) -> bool:
        """Execute one native edit before publishing its dependent UI state."""
        try:
            self._get_current_board()
            if getattr(self, "_ordinary_generating", False):
                raise RuntimeError("Finish generation before editing part assignments.")

            def apply() -> None:
                self._get_current_board()
                change()

            callback = getattr(self, "_board_action", None)
            if callback is None:
                apply()
            else:
                callback(apply)
        except (BoardContextChanged, BoardEditRecoveryError) as error:
            self._board_unreliable = True
            self._set_project_storage_error(error)
            return False
        except Exception as error:
            self.logger.warning("Unable to %s: %s", action, error)
            return False
        # Redraw failure cannot undo a verified native transaction or suppress
        # publication of its current assignment in the plugin's own controls.
        try:
            refresh = getattr(self.pcbnew, "Refresh", None)
            if callable(refresh):
                refresh()
        except Exception as error:
            self.logger.warning("Unable to redraw the PCB after %s: %s", action, error)
        return True

    def _set_standard_only_refs(self, refs: Iterable[str]) -> None:
        """Update computed Standard-only cells and refresh the current row help."""
        self.partlist_data_model.set_standard_only_refs(refs)
        self.footprint_list.Refresh()
        self._refresh_assembly_tooltip()

    def _refresh_assembly_tooltip(self) -> None:
        """Re-hit-test row help after metadata changes without retaining stale items."""
        tooltip = getattr(self, "_type_cell_tooltip", None)
        if tooltip is not None:
            tooltip.refresh()

    def _set_assembly_tooltip(self, text: str) -> None:
        """Replace changed row help while leaving an unchanged tooltip visible."""
        if text == self._assembly_tooltip_text:
            return
        self._assembly_tooltip_text = text
        if text:
            self._footprint_list_main_window.SetToolTip(wx.ToolTip(text))
        else:
            self._footprint_list_main_window.UnsetToolTip()

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
        if not getattr(self, "_variant_controller", None):
            self.populate_footprint_list()
        self._refresh_catalog_outputs()

    def _refresh_catalog_outputs(self) -> None:
        """Refresh computed catalog values and the open selector after row changes."""
        if controller := getattr(self, "_variant_controller", None):
            controller.catalog_changed()
        else:
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
                if self.store is None:
                    return
            else:
                self._initialize_catalog_parts()
            self._refresh_catalog_outputs()
        except (sqlite3.Error, OSError, ValueError) as error:
            self.library.state = LibraryState.UPDATE_NEEDED
            self._set_project_storage_error(error)
            self._clear_catalog_views()

    def quit_dialog(self, event: Optional[wx.Event] = None) -> None:
        """Save the schematic before releasing any resources needed for a retry."""
        close_event = event if callable(getattr(event, "CanVeto", None)) else None
        forced = close_event is not None and not close_event.CanVeto()

        def veto() -> None:
            """Let native Close() report that the window remains open."""
            if close_event is not None and close_event.CanVeto():
                close_event.Veto()

        if getattr(self, "_closing", False):
            return
        if getattr(self, "_saving_on_close", False):
            if forced:
                self._forced_close_pending = True
                for child in self.GetChildren():
                    if isinstance(child, wx.Dialog) and child.IsModal():
                        child.EndModal(wx.ID_CANCEL)
            else:
                veto()
            return
        # Modal callers still need their dialog and parent after ShowModal returns.
        modal_children = [
            child
            for child in self.GetChildren()
            if isinstance(child, wx.Dialog) and child.IsModal()
        ]
        logger = logging.getLogger(__name__)
        logger.info("quit_dialog()")
        controller = getattr(self, "_variant_controller", None)

        def is_generating() -> bool:
            return getattr(self, "_generating", False) or (
                controller is not None and controller.session.generating
            )

        if modal_children or is_generating():
            if forced:
                self._forced_close_pending = True
                if not getattr(self, "_forced_close_waiting", False):
                    self._forced_close_waiting = True

                    def finish_close() -> None:
                        # Wait for modal callers and generation finally blocks to
                        # finish using the controls before destroying their parent.
                        if not self or self._closing:
                            return
                        if any(modal_children) or is_generating():
                            wx.CallLater(25, finish_close)
                        else:
                            self._forced_close_waiting = False
                            self.Close(force=True)

                    wx.CallLater(25, finish_close)
                for child in modal_children:
                    child.EndModal(wx.ID_CANCEL)
            else:
                veto()
            return
        if getattr(self, "store", None) is not None and not getattr(
            self, "_project_storage_unavailable", False
        ):
            self._saving_on_close = True
            try:
                saved = self.export_to_schematic(interactive=not forced)
            finally:
                self._saving_on_close = False
            forced = forced or getattr(self, "_forced_close_pending", False)
            if saved is False and not forced:
                veto()
                return
        if lookup := getattr(self, "assembly_lookup", None):
            lookup.close()
        tooltip = getattr(self, "_type_cell_tooltip", None)
        if tooltip is not None:
            tooltip.stop()
        self._closing = True
        layout_ready = getattr(self, "_layout_ready", False)
        selector = getattr(self, "_part_selector", None)
        try:
            if controller is not None:
                controller.close()
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
                try:
                    why_standard_dialog = getattr(self, "_why_standard_dialog", None)
                    if why_standard_dialog:
                        why_standard_dialog.Close()
                finally:
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
        """Initialize fabrication and the appropriate native or ordinary assignments."""
        if controller := getattr(self, "_variant_controller", None):
            try:
                controller.session._check_board()
                controller.refresh()
                if not controller.session.reliable:
                    raise ValueError(
                        "Variant data is unavailable. Reopen JLCPCB Tools before continuing."
                    )
                self.store = controller.cache
                self._set_project_storage_error(None)
                controller._update_enabled()
            except (sqlite3.Error, OSError, ValueError, RuntimeError) as error:
                self._set_project_storage_error(error)
                controller._update_enabled()
            return
        try:
            if getattr(self, "fabrication", None) is None:
                self.init_fabrication()
            board = self._get_current_board()
            if not self._variant_mode and _board_has_variants(board):
                raise ValueError(
                    "The PCB now contains variants. Reopen JLCPCB Tools to edit them."
                )
            store_type = Store
            if self._variant_mode:
                from .variant.store import VariantStore

                store_type = VariantStore
            self.assembly_lookup.invalidate()
            self.store = store_type(self, self.project_path, board)
            self._set_project_storage_error(None)
            if store_type is not Store:
                from .variant.controller import VariantMainController

                self._variant_controller = VariantMainController(self, self.store)
                self._variant_controller.start_enrichment()
                return
            self._initialize_catalog_parts()
        except (sqlite3.Error, OSError, ValueError, BoardContextChanged) as error:
            self._set_project_storage_error(error)

    def _initialize_catalog_parts(self) -> None:
        """Apply opening preferences once whenever project and catalog first meet."""
        if getattr(self, "_variant_controller", None):
            return
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
            tooltip = getattr(self, "_type_cell_tooltip", None)
            if tooltip is not None:
                tooltip.dismiss()
            self.partlist_data_model.RemoveAll()
            self.assembly_lookup.invalidate()
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
        self.fabrication = Fabrication(self, self._get_current_board())

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
        if controller := getattr(self, "_variant_controller", None):
            return controller.assign_parts(e)
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
        """Apply a verified native batch before publishing assignments or preferences."""
        if self.store is None or not self.is_catalog_available():
            return []
        try:
            board = self._get_current_board()
        except BoardContextChanged as error:
            self._set_project_storage_error(error)
            return []
        assignments = {ref: normalize_lcsc(lcsc) for ref, lcsc in assignments.items()}
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
                catalog[lcsc] = (part, params_for_part(part))
        except (sqlite3.Error, OSError) as error:
            self.logger.warning("Unable to apply LCSC assignments: %s", error)
            return []

        if not self._apply_board_change(
            lambda: apply_board_part_edits(
                [(fp, {"lcsc": assignments[ref]}) for ref, fp in footprints.items()]
            ),
            "apply LCSC assignments",
        ):
            return []

        preferences = []
        for reference, footprint in footprints.items():
            lcsc = assignments[reference]
            part, params = catalog[lcsc]
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
            self.refresh_corrections(assigned)
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
        """Fill truly empty eligible native fields once per plugin opening."""
        if getattr(self, "_variant_controller", None):
            return
        board = self._get_current_board()
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

    def read_assembly_parts(self) -> list[dict[str, Any]]:
        """Read native output rows or ordinary assignments for assembly consumers."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.output_rows()
        return self.store.read_all() if getattr(self, "store", None) else []

    def show_assembly_mode_details(self, *_: Any) -> None:
        """Open or raise the modeless assembly-mode details dialog."""
        controller = getattr(self, "_variant_controller", None)
        parts = None
        if controller is not None:
            # Validate an existing details window too, before raising stale content.
            try:
                parts = self.read_assembly_parts()
            except Exception as error:
                if self._why_standard_dialog is not None:
                    self._why_standard_dialog.Close()
                controller._error(error)
                return
        if self.bom_estimator_decision is None:
            self.recompute_bom_estimate()
        if self._why_standard_dialog is None:
            self._why_standard_dialog = WhyStandardDialog(
                self,
                self.bom_estimator_decision,
                parts if parts is not None else self.read_assembly_parts(),
            )
            self._why_standard_dialog.Show()
        self._why_standard_dialog.Raise()

    def recompute_bom_estimate(self) -> None:
        """Recompute and display estimated BOM+assembly cost."""
        if controller := getattr(self, "_variant_controller", None):
            if not controller.session.generating:
                controller.recompute()
            return
        board_count = self._normalize_board_count(self.bom_estimator_board_count)
        self.bom_estimator_decision = self.bom_estimator_controller.recompute(
            board_count
        )
        if self._why_standard_dialog is not None:
            parts = self.read_assembly_parts()
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
        if getattr(self, "_variant_controller", None):
            # The matrix computes availability from each variant's own population.
            return
        model = self.partlist_data_model
        if (
            not self.settings.get("highlighting", {}).get("stock_concern", True)
            or self.store is None
            or not self.is_catalog_available()
        ):
            model.set_stock_concern_refs(set())
            return
        try:
            board = self._get_current_board()
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

    def _get_enrichment_status_label(self, part: dict[str, Any]) -> str:
        """Build UI status text for per-part assembly enrichment state."""
        lcsc = str(part.get("lcsc") or "")
        if not lcsc:
            return ""
        if lcsc in self.assembly_lookup.pending:
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
        if controller := getattr(self, "_variant_controller", None):
            return controller.start_enrichment()
        if self.store is None:
            return
        try:
            targets = self.store.get_assembly_enrichment_targets(references)
        except sqlite3.Error as error:
            self.logger.warning("Unable to start assembly enrichment: %s", error)
            return
        # Supplier facts are shared by LCSC for this dialog's lifetime.
        self.assembly_lookup.request(targets, retry=True)
        for lcsc, refs in targets.items():
            if lcsc in self.assembly_lookup.pending:
                for reference in refs:
                    self.partlist_data_model.set_enrichment_status(reference, "Pending")
        self._refresh_assembly_tooltip()

    def _apply_assembly_metadata(self, lcsc: str, metadata: dict[str, Any]) -> None:
        """Apply results to current recipients, including assignments made in flight."""
        if getattr(self, "_closing", False) or self.store is None:
            return
        try:
            self._get_current_board()
        except BoardContextChanged as error:
            self._set_project_storage_error(error)
            return
        assembly_process = metadata.get("assembly_process", "")
        component_product_type = metadata.get("component_product_type")
        self.store.cache_lcsc_metadata(lcsc, assembly_process, component_product_type)
        for part in self.store.read_all():
            if part["lcsc"] == lcsc:
                self.partlist_data_model.set_assembly_metadata(part["reference"], part)
        self._refresh_assembly_tooltip()

    def _refresh_bom_after_enrichment_update(self) -> None:
        """Clear finished status even after failures, then refresh the BOM once."""
        if getattr(self, "_closing", False) or self.store is None:
            return
        try:
            for part in self.store.read_all():
                self.partlist_data_model.set_assembly_metadata(
                    part["reference"],
                    part,
                    pending=self._get_enrichment_status_label(part) == "Pending",
                )
        except (sqlite3.Error, OSError, BoardContextChanged) as error:
            self._set_project_storage_error(error)
        finally:
            self._refresh_assembly_tooltip()
            wx.PostEvent(self, BomDataChangedEvent(source="enrichment_update"))

    def display_message(self, e):
        """Display a message with the data from the event."""
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
            part["lcsc"],
        )
        if match is None:
            return "0°, 0.0/0.0"
        return f"{match.correction} ({match.source})"

    def refresh_corrections(self, references: Iterable[str]) -> None:
        """Recompute the Correction cells of parts whose LCSC number changed.

        The rule selected for a part depends on its part number as well as
        its reference, value and footprint, so the cell has to follow the
        store whenever the number is assigned, pasted, applied from a part
        preference or removed. Only the affected rows change; the list is
        not repopulated.
        """
        if self.store is None:
            return
        references = list(references)
        if not references:
            return
        snapshot = self.library.read_correction_data()
        self.update_correction_status(snapshot)
        for reference in references:
            part = self.store.get_part(reference)
            if not part:
                continue
            self.partlist_data_model.set_correction(
                reference,
                str(self.get_correction(part, snapshot.corrections))
                if snapshot.corrections is not None
                else "Unresolved",
            )

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
        tooltip = getattr(self, "_type_cell_tooltip", None)
        if tooltip is not None:
            tooltip.dismiss()
        if controller := getattr(self, "_variant_controller", None):
            return controller.refresh()
        if not self.store:
            if not self._project_storage_unavailable and self.is_catalog_available():
                self.init_store()
            else:
                self.partlist_data_model.RemoveAll()
            return
        try:
            self._populate_footprint_rows()
        except (sqlite3.Error, OSError, BoardContextChanged) as error:
            self._set_project_storage_error(error)

    def _populate_footprint_rows(self) -> None:
        """Read a complete project view, allowing the caller to recover storage errors."""
        board = self._get_current_board()
        self.partlist_data_model.RemoveAll()
        parts = self.store.read_all()
        snapshot = self.library.read_correction_data()
        self.update_correction_status(snapshot)
        corrections = snapshot.corrections
        for part in parts:
            fp = board.FindFootprintByReference(part["reference"])
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
            enrichment_status = self._get_enrichment_status_label(part)
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
                    enrichment_status,
                    "",  # bom price label
                ]
            )
            self.partlist_data_model.set_assembly_metadata(
                part["reference"],
                part,
                pending=enrichment_status == "Pending",
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
                fp = self._get_current_board().FindFootprintByReference(ref)
                if fp:
                    fp.SetSelected()
            # cause pcbnew to refresh the board with the changes to the selected footprint(s)
            self.pcbnew.Refresh()

    def enable_part_specific_toolbar_buttons(self, state: bool) -> None:
        """Enable toolbar actions that operate on selected footprints."""
        for button in (
            ID_SELECT_PART,
            ID_REMOVE_LCSC_NUMBER,
            ID_TOGGLE_BOM_POS,
            ID_TOGGLE_BOM,
            ID_TOGGLE_POS,
            ID_PART_DETAILS,
        ):
            self.right_toolbar.EnableTool(button, state)

    def toggle_bom_pos(self, *_: object) -> None:
        """Toggle the exclude from BOM/POS attribute of a footprint."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.toggle(("bom", "pos"))
        self._toggle_exclusions(bom=True, pos=True)

    def toggle_bom(self, *_: object) -> None:
        """Toggle the exclude from BOM attribute of a footprint."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.toggle(("bom",))
        self._toggle_exclusions(bom=True, pos=False)

    def toggle_pos(self, *_: object) -> None:
        """Toggle the exclude from POS attribute of a footprint."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.toggle(("pos",))
        self._toggle_exclusions(bom=False, pos=True)

    def _toggle_exclusions(self, *, bom: bool, pos: bool) -> None:
        """Change each selected native footprint's flags as one reversible batch."""
        if self.store is None:
            return
        try:
            board = self._get_current_board()
        except BoardContextChanged as error:
            self._set_project_storage_error(error)
            return
        selected = []
        selections = self.footprint_list.GetSelections()
        for item in selections:
            reference = self.partlist_data_model.get_reference(item)
            footprint = board.FindFootprintByReference(reference)
            if footprint is not None:
                selected.append((item, footprint))
        edits = []
        for _item, footprint in selected:
            changes = {}
            if bom:
                changes["exclude_from_bom"] = not bool(get_exclude_from_bom(footprint))
            if pos:
                changes["exclude_from_pos"] = not bool(get_exclude_from_pos(footprint))
            edits.append((footprint, changes))
        if not edits or not self._apply_board_change(
            lambda: apply_board_part_edits(edits), "change assembly exclusions"
        ):
            return
        for item, _footprint in selected:
            if bom and pos:
                self.partlist_data_model.toggle_bom_pos(item)
            elif bom:
                self.partlist_data_model.toggle_bom(item)
            else:
                self.partlist_data_model.toggle_pos(item)
        wx.PostEvent(self, BomDataChangedEvent(source="toggle_exclusions"))

    def remove_lcsc_number(self, *_: object) -> None:
        """Clear native assignments before changing displayed rows."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.remove()
        if self.store is None:
            return
        try:
            board = self._get_current_board()
        except BoardContextChanged as error:
            self._set_project_storage_error(error)
            return
        selected = []
        # wx selection items borrow storage from this native array. Keep it
        # alive until the post-commit model updates have finished using them.
        selections = self.footprint_list.GetSelections()
        for item in selections:
            ref = self.partlist_data_model.get_reference(item)
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                continue
            selected.append((item, ref, fp))
        if not selected:
            return
        if not self._apply_board_change(
            lambda: apply_board_part_edits(
                [(fp, {"lcsc": ""}) for _, _, fp in selected]
            ),
            "clear LCSC assignments",
        ):
            return
        for item, _ref, _fp in selected:
            self.partlist_data_model.remove_lcsc_number(item)
        self.refresh_corrections([ref for _item, ref, _fp in selected])
        wx.PostEvent(self, BomDataChangedEvent(source="remove_lcsc_number"))

    def select_alike_parts(self, *_: object) -> None:
        """Select all alike parts, starting from a single selected part."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.select_alike()
        if self.footprint_list.GetSelectedItemsCount() > 1:
            self.logger.warning("Select only one component, please.")
            return
        selected_item = self.footprint_list.GetSelection()
        # The focused row is the one the user last clicked, which is not
        # always the selected one: ctrl-clicking a row out of a two-row
        # selection leaves the other row selected but the focus on the row
        # that was clicked. The list scrolls the focused row into view once
        # the click has been handled, so the focus is what has to be put
        # back to keep the viewport still.
        focused_item = self.footprint_list.GetCurrentItem()
        already_selected = []
        to_select = []
        for alike_item in self.partlist_data_model.select_alike(selected_item):
            if self.footprint_list.IsSelected(alike_item):
                already_selected.append(alike_item)
            else:
                to_select.append(alike_item)
        if not to_select:
            # Nothing to add. Replacing the selection with itself is not free:
            # on GTK it moves the selection anchor onto the survivor, so a
            # following shift-click ranges from the wrong row. Leave the
            # control's native state alone.
            return
        # SetSelections() selects every row again, and GTK leaves the
        # shift-click anchor on the last row it selects, so the new rows go
        # last: the anchor lands where selecting only them would leave it.
        alike = dv.DataViewItemArray()
        for alike_item in already_selected + to_select:
            alike.append(alike_item)
        self.select_alike_in_progress = True
        try:
            self.footprint_list.SetSelections(alike)
            # wxOSX moves the focus onto the highest row of the new selection,
            # so it goes back. GTK never moves its cursor here, and focusing a
            # row there scrolls it into view even when the focus is unchanged,
            # so the focus is only put back when it actually moved.
            if (
                focused_item.IsOk()
                and self.footprint_list.GetCurrentItem() != focused_item
            ):
                self.footprint_list.SetCurrentItem(focused_item)
        finally:
            self.select_alike_in_progress = False

    def toggle_select_alike(self, e: Any) -> None:
        """Toggle auto-selecting alike parts on selection."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.select_alike()
        self.auto_select_alike = bool(e.IsChecked())
        self.settings.setdefault("general", {})["select_alike_auto"] = (
            self.auto_select_alike
        )
        self.save_settings()
        if self.auto_select_alike and self.footprint_list.GetSelectedItemsCount() == 1:
            self.select_alike_parts()

    def get_part_details(self, *_: object) -> None:
        """Show one modeless Part Details window per selected LCSC number."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.part_details()
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
        with CorrectionManagerDialog(self, "") as dialog:
            dialog.ShowModal()
        self.populate_footprint_list()

    def manage_part_preferences(self, *_: object) -> None:
        """Manage shared part preferences."""
        with PartPreferencesDialog(self) as dialog:
            dialog.ShowModal()

    def manage_settings(self, *_: object) -> None:
        """Manage settings."""
        with SettingsDialog(self) as dialog:
            dialog.ShowModal()

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
                if controller := getattr(self, "_variant_controller", None):
                    controller.view.ForceRefresh()
                else:
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
        """Load settings, seeding anything unset from default_settings.json."""
        self.settings = load_settings_document(PLUGIN_PATH)

    def decode_mainwindow_highlight_value(self, value: str) -> tuple[str, list[str]]:
        """Decode params cell text, optionally disabling highlight terms by setting."""
        text, terms = decode_highlighted_value(value)
        if not self.settings.get("highlighting", {}).get("matches", True):
            return text, []
        return text, terms

    def save_settings(
        self, variant_patch: Optional[VariantPreferencePatch] = None
    ) -> None:
        """Merge explicit variant changes without overwriting other windows' choices."""
        saved = save_settings_document(PLUGIN_PATH, self.settings, variant_patch)
        # Modeless children share this dictionary with the main window.
        self.settings.clear()
        self.settings.update(saved)

    def select_part(self, *_: object) -> None:
        """Select a part from the library and assign it to the selected footprint(s)."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.select_part()
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
        for drawing in iter_board_items(self._get_current_board().Drawings()):
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

    def build_generate_hook_env(
        self, stage: str, placeholder_count: int, generation_count: int
    ) -> dict[str, str]:
        """Build environment variables for configured generation hooks."""
        board_filename = self._get_current_board().GetFileName()
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
        if controller := getattr(self, "_variant_controller", None):
            env["JLCPCB_VARIANT"] = controller.session.output_variant
            env["JLCPCB_VARIANT_LABEL"] = controller.output_name
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
        self._generating = True
        self.generate_button.Enable(False)
        self.reset_gauge()
        wx.BeginBusyCursor()
        self._current_generation_step = "initialization"
        try:
            corrections = self.run_generation_step(
                "Validating corrections",
                self.read_valid_corrections_for_generation,
            )
            if controller := getattr(self, "_variant_controller", None):
                controller.begin_generation(corrections)
                placements = self.run_generation_step(
                    "Preparing placement data",
                    self.fabrication.prepare_cpl,
                    corrections,
                )
            else:
                self._get_current_board()
                self._ordinary_generating = True
                output = self.run_generation_step(
                    "Preparing placement data",
                    self.fabrication.begin_ordinary_generation,
                    corrections,
                )
                placements = output.cpl_rows
            layer_selection = self.layer_selection.GetSelection()
            number = re.search(r"\d+", self.layer_selection.GetString(layer_selection))
            layer_count = int(number.group(0)) if number else None
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

            self.fabrication.validate_generation()

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

            if getattr(self, "_variant_controller", None):
                self._current_generation_step = "Publishing fabrication files"
                self.report_generation_step(self._current_generation_step)
                with (
                    self.store.generation_publication_lock(),
                    ExitStack() as publication,
                    self.store.generation_counter_transaction(
                        expected_count=current_generation_count
                    ) as generation_count,
                ):
                    publication.enter_context(self.fabrication.generation_publication())
            else:
                self.fabrication.validate_generation()
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
            self._generating = False
            self.reset_gauge()
            if wx.IsBusy():
                wx.EndBusyCursor()
            if controller := getattr(self, "_variant_controller", None):
                controller.end_generation()
            else:
                self.fabrication.end_ordinary_generation()
                self._ordinary_generating = False
                self.generate_button.Enable(True)

    def save_board_for_drc(self) -> None:
        """Save the current board so DRC checks operate on latest board state."""
        board = self._get_current_board()
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

    def run_drc_before_gerber_export(self) -> bool:
        """Run optional DRC via KiCad Python API and prompt when violations exist."""
        if not self.settings.get("gerber", {}).get("force_drc", False):
            return True

        board_filename = self._get_current_board().GetFileName()
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

    def copy_part_lcsc(self, *_: object) -> None:
        """Copy the selected assignment value to the clipboard."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.dispatch_action(
                "copy_cell", controller.view.selected_target
            )
        for item in self.footprint_list.GetSelections():
            if lcsc := self.partlist_data_model.get_lcsc(item):
                if wx.TheClipboard.Open():
                    wx.TheClipboard.SetData(wx.TextDataObject(lcsc))
                    wx.TheClipboard.Close()

    def paste_part_lcsc(self, *_: object) -> None:
        """Paste a lcsc number from the clipboard to the current part."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.dispatch_action("paste", controller.view.selected_target)
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
        without_lcsc = []
        for item in self.footprint_list.GetSelections():
            if e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE:
                if reference := self.partlist_data_model.get_reference(item):
                    with CorrectionManagerDialog(
                        self, "^" + re.escape(reference) + "$"
                    ) as dialog:
                        dialog.ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE:
                if footprint := self.partlist_data_model.get_footprint(item):
                    with CorrectionManagerDialog(
                        self, "^" + re.escape(footprint)
                    ) as dialog:
                        dialog.ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_NAME:
                if value := self.partlist_data_model.get_value(item):
                    with CorrectionManagerDialog(self, re.escape(value)) as dialog:
                        dialog.ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_LCSC:
                if lcsc := self.partlist_data_model.get_lcsc(item):
                    with CorrectionManagerDialog(self, "", lcsc_part=lcsc) as dialog:
                        dialog.ShowModal()
                else:
                    without_lcsc.append(self.partlist_data_model.get_reference(item))
        if without_lcsc:
            wx.MessageBox(
                "No LCSC number is assigned to " + ", ".join(without_lcsc) + ".",
                "No LCSC number",
                style=wx.ICON_WARNING,
            )
        self.populate_footprint_list()

    def export_to_schematic(self, *, interactive: bool = True) -> Optional[bool]:
        """Save on close: True means saved, False keep open, None close unsaved.

        Projects without a matching schematic need no write or file picker.
        Forced shutdown never opens a dialog or approves a schematic lock.
        """
        try:

            def check_board() -> None:
                """Never save a stale window's assignments into another project."""
                identity = getattr(self, "_schematic_board_identity", None)
                if (
                    identity is not None
                    and board_identity(self.pcbnew.GetBoard()) != identity
                ):
                    raise BoardContextChanged(
                        "The PCB was closed, replaced, or saved under another name. "
                        "Reopen JLCPCB Tools before saving its schematic."
                    )

            paths = resolve_project_schematics(
                self.project_path,
                self.board_name,
                authenticated_project_name(
                    getattr(self, "pcbnew", None), self.project_path
                ),
            )
            if not paths:
                self.logger.info(
                    "No project schematic found; automatic schematic save skipped"
                )
                return None
            controller = getattr(self, "_variant_controller", None)

            def export(**approval: object) -> None:
                check_board()
                if controller is not None:
                    controller.export_to_schematic(paths, **approval)
                else:
                    SchematicExport(self).load_schematic(paths, **approval)

            try:
                export()
            except SchematicLockedError as exc:
                if not interactive:
                    raise
                decision = self.confirm_locked_schematic_export(exc)
                if decision is not True:
                    return decision
                export(approved_locks=[path for path, _info in exc.locks])
            return True
        except Exception as exc:
            self.logger.exception("Automatic schematic save failed")
            if not interactive:
                return False
            # Use wx's modal lifecycle so forced close can end the prompt on macOS.
            dialog = wx.GenericMessageDialog(
                self,
                f"Could not save the schematic:\n\n{exc}\n\n"
                "Keep this window open to correct the problem and try again, "
                "or close without saving the remaining changes.",
                "Schematic save failed",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_ERROR | wx.CENTER,
            )
            try:
                dialog.SetYesNoLabels("Close without saving", "Keep open")
                result = dialog.ShowModal()
            finally:
                dialog.Destroy()
            return None if result == wx.ID_YES else False

    def confirm_locked_schematic_export(
        self, error: SchematicLockedError
    ) -> Optional[bool]:
        """Choose save anyway, close unsaved, or cancel closing (the default).

        KiCad's lock file cannot show whether its session is still running,
        so this asks the way KiCad does when it finds one.
        """
        dialog = wx.GenericMessageDialog(
            self,
            f"{error}\n\nIf a Schematic Editor has a locked file open, save and "
            "close it first: its next save would overwrite this export. A lock "
            "file left over from a crash can be deleted.\n\n"
            "See KiCad issue #2077: https://gitlab.com/kicad/code/kicad/-/issues/2077",
            "Schematic Locked",
            wx.YES_NO | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_WARNING | wx.CENTER,
        )
        try:
            dialog.SetYesNoCancelLabels("Save Anyway", "Close without saving", "Cancel")
            result = dialog.ShowModal()
        finally:
            dialog.Destroy()
        self.logger.warning(
            "%s\nUser chose to %s the schematic export",
            error,
            "continue" if result == wx.ID_YES else "stop",
        )
        return True if result == wx.ID_YES else None if result == wx.ID_NO else False

    def save_selected_part_preferences(self, *_: object) -> None:
        """Remember the selected LCSC assignments as part preferences."""
        if controller := getattr(self, "_variant_controller", None):
            return controller.save_preferences()
        if self.store is None:
            return
        preferences = []
        try:
            for item in self.footprint_list.GetSelections():
                reference = self.partlist_data_model.get_reference(item)
                if part := self.store.get_part(reference):
                    preferences.append((part["footprint"], part["value"], part["lcsc"]))
        except BoardContextChanged as error:
            self._set_project_storage_error(error)
            return
        self._save_part_preferences(preferences)

    def apply_selected_part_preferences(self, *_: object) -> None:
        """Apply matching part preferences to the selected rows."""
        if not self._can_apply_user_assignments():
            return
        if controller := getattr(self, "_variant_controller", None):
            return controller.apply_preferences()
        assignments = {}
        try:
            for item in self.footprint_list.GetSelections():
                reference = self.partlist_data_model.get_reference(item)
                part = self.store.get_part(reference)
                if part is None:
                    continue
                footprint, value = part["footprint"], part["value"]
                if footprint and value:
                    if preference := self.library.get_part_preference(footprint, value):
                        assignments[reference] = preference
            updated_references = self._apply_lcsc_assignments(assignments)
        except BoardContextChanged as error:
            self._set_project_storage_error(error)
            return
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

        correction_by_lcsc = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_ADD_ROT_BY_LCSC, "Add Correction by LCSC"
        )
        right_click_menu.Append(correction_by_lcsc)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_lcsc)

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
    """Logging class for the logging textbox at the bottom of the mainwindow."""

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
