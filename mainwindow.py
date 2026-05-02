"""Contains the main window of the plugin."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# ruff: noqa: I001

from contextlib import contextmanager, suppress
from datetime import datetime as dt
from threading import Thread
import json
import logging
import os
import re
import sys
import time

import pcbnew as kicad_pcbnew
import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error
from wx import adv  # pylint: disable=import-error

from .bom_estimation.help_text import (
    BOM_ESTIMATOR_HELP_TITLE,
    get_bom_estimator_help_text,
)
from .bom_widget import BomEstimatorController, BomEstimatorWidget
from .corrections import CorrectionManagerDialog
from .datamodel import PartListDataModel
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
    EVT_DOWNLOAD_COMPLETED_EVENT,
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
    AssemblyEnrichmentProgressEvent,
    LogboxAppendEvent,
)
from .fabrication import Fabrication
from .generate_hooks import format_hook_error, run_configured_hook
from .helpers import (
    PLUGIN_PATH,
    GetScaleFactor,
    HighResWxSize,
    get_is_dnp,
    getVersion,
    loadBitmapScaled,
    set_lcsc_value,
    toggle_exclude_from_bom,
    toggle_exclude_from_pos,
)
from .kicad_drc import DRCViolationCounter
from .library import Library, LibraryState
from .partdetails import PartDetailsDialog
from .partmapper import PartMapperManagerDialog
from .partselector import PartSelectorDialog
from .schematicexport import SchematicExport
from .settings import SettingsDialog
from .store import Store

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

ID_GENERATE = 0
ID_LAYERS = 1
ID_CORRECTIONS = 2
ID_MAPPINGS = 3
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
ID_SAVE_MAPPINGS = 15
ID_EXPORT_TO_SCHEMATIC = 16
ID_CONTEXT_MENU_COPY_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_PASTE_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_NAME = wx.NewIdRef()
ID_CONTEXT_MENU_FIND_MAPPING = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_MAPPING = wx.NewIdRef()


class KicadProvider:
    """KiCad implementation of the provider, see standalone_impl.py for the stub version."""

    def get_pcbnew(self):
        """Get the pcbnew instance."""
        return kicad_pcbnew


class JLCPCBTools(wx.Dialog):
    """JLCPCBTools main dialog."""

    def __init__(self, parent, kicad_provider=KicadProvider()):
        while not wx.GetApp():
            time.sleep(1)
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=f"JLCPCB Tools [ {getVersion()} ]",
            pos=wx.DefaultPosition,
            size=wx.Size(1300, 800),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
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
        self.highlight_standard_parts = bool(
            general_settings.get("highlight_standard_parts", True)
        )
        general_settings["highlight_standard_parts"] = self.highlight_standard_parts
        self.auto_select_alike = bool(
            self.settings.get("general", {}).get("select_alike_auto", False)
        )
        self.select_alike_in_progress = False
        self.pending_assembly_enrichment = set()
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
            self,
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

        self.mapping_button = self.upper_toolbar.AddTool(
            ID_MAPPINGS,
            "Mappings",
            loadBitmapScaled("mdi-selection.png", self.scale_factor),
            "Manage part mappings",
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
        self.Bind(wx.EVT_TOOL, self.manage_mappings, self.mapping_button)
        self.Bind(wx.EVT_TOOL, self.update_library, self.download_button)
        self.Bind(wx.EVT_TOOL, self.manage_settings, self.settings_button)

        # ---------------------------------------------------------------------
        # ------------------ Right side toolbar List --------------------------
        # ---------------------------------------------------------------------

        self.right_toolbar = wx.ToolBar(
            self,
            wx.ID_ANY,
            wx.DefaultPosition,
            wx.Size(int(self.scale_factor * 128), -1),
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

        self.save_all_button = self.right_toolbar.AddTool(
            ID_SAVE_MAPPINGS,
            "Save mappings",
            loadBitmapScaled(
                "mdi-content-save-settings.png",
                self.scale_factor,
            ),
            "Save all mappings",
        )

        self.export_schematic_button = self.right_toolbar.AddTool(
            ID_EXPORT_TO_SCHEMATIC,
            "Export to schematic",
            loadBitmapScaled(
                "mdi-application-export.png",
                self.scale_factor,
            ),
            "Export mappings to schematic",
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
        self.Bind(wx.EVT_TOOL, self.save_all_mappings, self.save_all_button)
        self.Bind(wx.EVT_TOOL, self.export_to_schematic, self.export_schematic_button)

        self.right_toolbar.ToggleTool(ID_SELECT_ALIKE, self.auto_select_alike)

        self.right_toolbar.Realize()

        # ---------------------------------------------------------------------
        # ----------------------- Footprint List ------------------------------
        # ---------------------------------------------------------------------

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 600)))

        self.footprint_list = dv.DataViewCtrl(
            self,
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
            13,
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
        side = self.footprint_list.AppendIconTextColumn(
            "Side", 10, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        enrichment = self.footprint_list.AppendTextColumn(
            "Enrichment",
            12,
            width=110,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        trailing_spacer = self.footprint_list.AppendTextColumn(
            " ",
            14,
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
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.logbox.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 150)))
        self.gauge = wx.Gauge(
            self,
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
            self,
            window=self.window,
            board_count=self.bom_estimator_board_count,
            force_standard=self.bom_estimator_force_standard,
            on_board_count_spin=self.on_bom_estimator_board_count_spinctrl,
            on_board_count_text=self.on_bom_estimator_board_count_text,
            on_board_count_text_timer=self.on_bom_estimator_board_count_text_timer,
            on_force_standard_changed=self.on_bom_estimator_force_standard_changed,
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

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        self.SetSizeHints(HighResWxSize(self.window, wx.Size(1000, -1)), wx.DefaultSize)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.upper_toolbar, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(estimator_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.logbox, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.bom_widget.set_visible(self.bom_estimator_show)
        self.Layout()
        self.Centre(wx.BOTH)

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

        self.enable_part_specific_toolbar_buttons(False)

        self.init_logger()
        self.partlist_data_model = PartListDataModel(self.scale_factor)
        self.partlist_data_model.set_standard_trigger_highlighting_enabled(
            self.highlight_standard_parts
        )
        self.footprint_list.AssociateModel(self.partlist_data_model)
        self.bom_estimator_controller = BomEstimatorController(
            read_parts=lambda: self.store.read_all()
            if hasattr(self, "store") and self.store is not None
            else [],
            get_part_details=self._bom_get_part_details,
            get_board=self._get_current_board,
            is_force_standard_enabled=lambda: self.bom_estimator_force_standard,
            set_price_label=self.partlist_data_model.set_bom_price,
            set_trigger_refs=self.partlist_data_model.set_standard_trigger_refs,
            refresh_rows=self.footprint_list.Refresh,
            set_summary_text=self.bom_widget.set_summary_text,
        )

        self.init_data()

    def init_data(self):
        """Initialize the library and populate the main window."""
        self.init_library()
        self.init_fabrication()
        if self.library.state == LibraryState.UPDATE_NEEDED:
            self.library.update()
        else:
            self.init_store()
        self.library.create_mapping_table()

        self.logger.debug("kicad version: %s", kicad_pcbnew.GetBuildVersion())

    def _get_current_board(self):
        """Return current board instance for BOM controller callbacks."""
        return self.pcbnew.GetBoard()

    def _bom_get_part_details(self, lcsc: str) -> dict:
        """Safely proxy part-detail lookups for BOM controller callbacks."""
        if not hasattr(self, "library") or self.library is None:
            return {}
        return self.library.get_part_details(lcsc)

    def quit_dialog(self, *_):
        """Destroy dialog on close."""
        self.logger.info("quit_dialog()")
        root = logging.getLogger()
        with suppress(AttributeError):
            root.removeHandler(self.logging_handler1)
        with suppress(AttributeError):
            root.removeHandler(self.logging_handler2)

        self.Destroy()
        self.EndModal(0)

    def init_library(self):
        """Initialize the parts library."""
        self.library = Library(self)
        meta = self.library.get_parts_db_info()
        if meta is not None:
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

    def init_store(self):
        """Initialize the store of part assignments."""
        self.store = Store(self, self.project_path, self.pcbnew.GetBoard())
        if self.library.state == LibraryState.INITIALIZED:
            self.populate_footprint_list()
            self.start_assembly_enrichment()
            self.recompute_bom_estimate()

    def init_fabrication(self):
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

    def download_completed(self, *_):
        """Populate the footprint list."""
        self.populate_footprint_list()

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

    def unzip_extracting_completed(self, *_):
        """Update the gauge."""
        self.reset_gauge()
        self.init_data()

    def assign_parts(self, e):
        """Assign a selected LCSC number to parts."""
        details = self.library.get_part_details(e.lcsc)
        params = params_for_part(details)
        for reference in e.references:
            self.store.set_lcsc(reference, e.lcsc)
            self.store.set_stock(reference, int(e.stock))
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(reference)
            set_lcsc_value(fp, e.lcsc)
            self.partlist_data_model.set_lcsc(
                reference, e.lcsc, e.type, e.stock, params
            )
        self.start_assembly_enrichment(e.references)
        self.recompute_bom_estimate()

    def _set_bom_estimator_board_count(self, value: int) -> None:
        """Persist board count and update estimate when value changed."""
        if value == self.bom_estimator_board_count:
            return
        self.bom_estimator_board_count = value
        self.settings.setdefault("general", {})["bom_estimator_boards"] = value
        self.save_settings()
        self.recompute_bom_estimate()

    def on_bom_estimator_board_count_spinctrl(self, e):
        """Handle SpinCtrl arrows immediately, using step=5 increments."""
        value = self._normalize_board_count(e.GetEventObject().GetValue())
        if e.GetEventObject().GetValue() != value:
            e.GetEventObject().SetValue(value)
        self._set_bom_estimator_board_count(value)

    def on_bom_estimator_board_count_text(self, *_):
        """Debounce manual text entry to avoid recompute flicker while typing."""
        if hasattr(self.bom_estimator_text_timer, "StartOnce"):
            self.bom_estimator_text_timer.StartOnce(300)
        else:
            self.bom_estimator_text_timer.Start(300, oneShot=True)

    def on_bom_estimator_board_count_text_timer(self, *_):
        """Apply board count from text field after debounce delay."""
        value = self._normalize_board_count(self.bom_estimator_boards_input.GetValue())
        if self.bom_estimator_boards_input.GetValue() != value:
            self.bom_estimator_boards_input.SetValue(value)
        self._set_bom_estimator_board_count(value)

    def _normalize_board_count(self, value) -> int:
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
        """Show shared BOM estimator help text.

        Text is sourced from `bom_estimation.help_text` so wording matches the
        settings dialog and can be reviewed in one place.
        """
        wx.MessageBox(
            get_bom_estimator_help_text(),
            BOM_ESTIMATOR_HELP_TITLE,
            style=wx.OK | wx.ICON_INFORMATION,
        )

    def recompute_bom_estimate(self):
        """Recompute and display estimated BOM+assembly cost."""
        board_count = self._normalize_board_count(self.bom_estimator_board_count)
        self.bom_estimator_controller.recompute(board_count)

    def _get_enrichment_status_label(self, part: dict) -> str:
        """Build UI status text for per-part assembly enrichment state."""
        lcsc = str(part.get("lcsc") or "")
        if not lcsc:
            return ""
        if lcsc in self.pending_assembly_enrichment:
            return "Pending"
        if (
            str(part.get("assembly_process") or "")
            or part.get("component_product_type") is not None
        ):
            return "Done"
        return "Queued"

    def start_assembly_enrichment(self, references=None):
        """Start background enrichment for missing assembly process metadata."""
        targets = self.store.get_assembly_enrichment_targets(references)
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

        Thread(
            target=self._assembly_enrichment_worker,
            args=(targets,),
            daemon=True,
        ).start()

    def _assembly_enrichment_worker(self, targets: dict):
        """Fetch assembly metadata values from LCSC API in a worker thread."""
        provider = LCSCAssemblyMetadataProvider(min_interval_seconds=1.0)
        for lcsc, metadata in provider.fetch_iter(list(targets.keys())):
            refs = targets[lcsc]
            wx.PostEvent(
                self,
                AssemblyEnrichmentProgressEvent(
                    lcsc=lcsc, refs=refs, metadata=metadata
                ),
            )

    def on_assembly_enrichment_progress(self, e):
        """Persist one enrichment result and update row-level feedback."""
        lcsc = getattr(e, "lcsc", "")
        refs = getattr(e, "refs", [])
        metadata = getattr(e, "metadata", {}) or {}

        assembly_process = metadata.get("assembly_process", "")
        component_product_type = metadata.get("component_product_type")
        status = (
            "Done"
            if assembly_process or component_product_type is not None
            else "No data"
        )

        for reference in refs:
            self.store.set_assembly_metadata(
                reference,
                assembly_process,
                component_product_type,
            )
            self.partlist_data_model.set_enrichment_status(reference, status)

        self.pending_assembly_enrichment.discard(lcsc)
        self.recompute_bom_estimate()

    def on_assembly_enrichment_completed(self, e):
        """Compatibility handler for old-style batch enrichment events (now unused)."""
        self.recompute_bom_estimate()

    def display_message(self, e):
        """Dispaly a message with the data from the event."""
        styles = {
            "info": wx.ICON_INFORMATION,
            "warning": wx.ICON_WARNING,
            "error": wx.ICON_ERROR,
        }
        wx.MessageBox(e.text, e.title, style=styles.get(e.style, wx.ICON_INFORMATION))

    def get_correction(self, part: dict, corrections: list) -> str:
        """Try to find correction data for a given part."""
        # First check if the part name matches
        for regex, rotation, offset in corrections:
            if re.search(regex, str(part["reference"])):
                return f"{str(rotation)}°, {str(offset[0])}/{str(offset[1])} (ref)"
        # Then try to match by value
        for regex, rotation, offset in corrections:
            if re.search(regex, str(part["value"])):
                return f"{str(rotation)}°, {str(offset[0])}/{str(offset[1])} (val)"
        # If there was no match for the part name or value, check if the package matches
        for regex, rotation, offset in corrections:
            if re.search(regex, str(part["footprint"])):
                return f"{str(rotation)}°, {str(offset[0])}/{str(offset[1])} (fpt)"
        return "0°, 0.0/0.0"

    def populate_footprint_list(self, *_):
        """Populate list of footprints."""
        if not self.store:
            self.init_store()
        self.partlist_data_model.RemoveAll()
        parts = self.store.read_all()
        details = {}
        corrections = self.library.get_all_correction_data()
        for part in parts:
            fp = self.pcbnew.GetBoard().FindFootprintByReference(part["reference"])
            is_dnp = get_is_dnp(fp)
            # Get part stock and type from library, skip if part number was already looked up before
            if part["lcsc"] and part["lcsc"] not in details:
                details[part["lcsc"]] = self.library.get_part_details(part["lcsc"])
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
                    details.get(part["lcsc"], {}).get("type", ""),  # type
                    details.get(part["lcsc"], {}).get("stock", ""),  # stock
                    part["exclude_from_bom"],
                    part["exclude_from_pos"],
                    int(is_dnp),
                    str(self.get_correction(part, corrections)),
                    str(fp.GetLayer()),
                    params_for_part(details.get(part["lcsc"], {})),
                    self._get_enrichment_status_label(part),  # enrichment
                    "",  # bom price label
                ]
            )
        self.recompute_bom_estimate()

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
            bom = toggle_exclude_from_bom(fp)
            pos = toggle_exclude_from_pos(fp)
            self.store.set_bom(ref, int(bool(bom)))
            self.store.set_pos(ref, int(bool(pos)))
            self.partlist_data_model.toggle_bom_pos(item)
        self.recompute_bom_estimate()

    def toggle_bom(self, *_):
        """Toggle the exclude from BOM attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            bom = toggle_exclude_from_bom(fp)
            self.store.set_bom(ref, int(bool(bom)))
            self.partlist_data_model.toggle_bom(item)
        self.recompute_bom_estimate()

    def toggle_pos(self, *_):
        """Toggle the exclude from POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            pos = toggle_exclude_from_pos(fp)
            self.store.set_pos(ref, int(bool(pos)))
            self.partlist_data_model.toggle_pos(item)
        self.recompute_bom_estimate()

    def remove_lcsc_number(self, *_):
        """Remove an assigned a LCSC Part number to a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            self.store.set_lcsc(ref, "")
            self.store.set_stock(ref, None)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            set_lcsc_value(fp, "")
            self.partlist_data_model.remove_lcsc_number(item)
        self.recompute_bom_estimate()

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

    def get_part_details(self, *_):
        """Fetch part details from LCSC and show them one after another each in a modal."""
        for item in self.footprint_list.GetSelections():
            if lcsc := self.partlist_data_model.get_lcsc(item):
                self.show_part_details_dialog(lcsc)

    def show_part_details_dialog(self, part):
        """Show the part details modal dialog."""
        wx.BeginBusyCursor()
        try:
            dialog = PartDetailsDialog(self, part)
            dialog.ShowModal()
        finally:
            wx.EndBusyCursor()

    def update_library(self, *_):
        """Update the library from the JLCPCB CSV file."""
        self.library.update()

    def manage_corrections(self, *_):
        """Manage corrections."""
        CorrectionManagerDialog(self, "").ShowModal()

    def manage_mappings(self, *_):
        """Manage footprint mappings."""
        PartMapperManagerDialog(self).ShowModal()

    def manage_settings(self, *_):
        """Manage settings."""
        SettingsDialog(self).ShowModal()

    def update_settings(self, e):
        """Update the settings on change."""
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value

        if e.section == "general":
            if e.setting == "bom_estimator_show":
                self.bom_estimator_show = bool(e.value)
                self.bom_widget.set_visible(self.bom_estimator_show)
                self.Layout()
            elif e.setting == "highlight_standard_parts":
                self.highlight_standard_parts = bool(e.value)
                self.partlist_data_model.set_standard_trigger_highlighting_enabled(
                    self.highlight_standard_parts
                )
                self.footprint_list.Refresh()
        elif e.section == "highlighting" and e.setting == "matches":
            self.footprint_list.Refresh()

        self.save_settings()

        # Refresh library configuration if relevant library settings changed
        if e.section == "library" and e.setting in ["selected_library", "data_path"]:
            self.library.refresh_library_config()

    def logbox_append(self, e):
        """Write text to the logbox."""
        self.logbox.WriteText(e.msg)

    def load_settings(self):
        """Load settings from settings.json."""
        with open(os.path.join(PLUGIN_PATH, "settings.json"), encoding="utf-8") as j:
            self.settings = json.load(j)

        gerber_settings = self.settings.setdefault("gerber", {})
        highlighting_settings = self.settings.setdefault("highlighting", {})
        partselector_settings = self.settings.setdefault("partselector", {})
        migrated = False

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

        general_settings = self.settings.setdefault("general", {})
        for legacy_key in (
            "bom_order_handling_fee",
            "bom_panelization_per_board_fee",
            "bom_panelization_threshold_boards",
        ):
            if legacy_key in general_settings:
                general_settings.pop(legacy_key, None)
                migrated = True

        if migrated:
            self.save_settings()

    def decode_mainwindow_highlight_value(self, value: str) -> tuple[str, list[str]]:
        """Decode params cell text, optionally disabling highlight terms by setting."""
        text, terms = decode_highlighted_value(value)
        if not self.settings.get("highlighting", {}).get("matches", True):
            return text, []
        return text, terms

    def save_settings(self):
        """Save settings to settings.json."""
        with open(
            os.path.join(PLUGIN_PATH, "settings.json"), "w", encoding="utf-8"
        ) as j:
            json.dump(self.settings, j)

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
        PartSelectorDialog(self, selection).ShowModal()

    def count_order_number_placeholders(self):
        """Count the JLC order/serial number placeholders."""
        count = 0
        for drawing in self.pcbnew.GetBoard().GetDrawings():
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

    def generate_fabrication_data(self, *_):
        """Generate fabrication data."""
        self.generate_button.Enable(False)
        self.reset_gauge()
        wx.BeginBusyCursor()
        self._current_generation_step = "initialization"
        try:
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

            self.run_generation_step(
                "Filling copper zones",
                self.fabrication.fill_zones,
            )

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
                placeholder_count = self.count_order_number_placeholders()

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
                self.fabrication.generate_cpl,
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

    def paste_part_lcsc(self, *_):
        """Paste a lcsc number from the clipboard to the current part."""
        text_data = wx.TextDataObject()
        success = False
        if wx.TheClipboard.Open():
            success = wx.TheClipboard.GetData(text_data)
            wx.TheClipboard.Close()
        if success:
            if (lcsc := self.sanitize_lcsc(text_data.GetText())) != "":
                updated_references = []
                for item in self.footprint_list.GetSelections():
                    details = self.library.get_part_details(lcsc)
                    params = params_for_part(details)
                    reference = self.partlist_data_model.get_reference(item)
                    self.partlist_data_model.set_lcsc(
                        reference, lcsc, details["type"], details["stock"], params
                    )
                    self.store.set_lcsc(reference, lcsc)
                    updated_references.append(reference)
                self.start_assembly_enrichment(updated_references)
                self.recompute_bom_estimate()

    def add_correction(self, e):
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

    def save_all_mappings(self, *_):
        """Save all mappings."""
        for item in self.partlist_data_model.get_all():
            value = item[1]
            footprint = item[2]
            lcsc = item[3]
            if footprint != "" and value != "" and lcsc != "":
                if self.library.get_mapping_data(footprint, value):
                    self.library.update_mapping_data(footprint, value, lcsc)
                else:
                    self.library.insert_mapping_data(footprint, value, lcsc)
        self.logger.info("All mappings saved")

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

    def add_foot_mapping(self, *_):
        """Add a footprint mapping."""
        for item in self.footprint_list.GetSelections():
            footprint = self.partlist_data_model.get_footprint(item)
            value = self.partlist_data_model.get_value(item)
            lcsc = self.partlist_data_model.get_lcsc(item)
            if footprint != "" and value != "" and lcsc != "":
                if self.library.get_mapping_data(footprint, value):
                    self.library.update_mapping_data(footprint, value, lcsc)
                else:
                    self.library.insert_mapping_data(footprint, value, lcsc)

    def search_foot_mapping(self, *_):
        """Search for a footprint mapping."""
        for item in self.footprint_list.GetSelections():
            reference = self.partlist_data_model.get_reference(item)
            footprint = self.partlist_data_model.get_footprint(item)
            value = self.partlist_data_model.get_value(item)
            if footprint != "" and value != "":
                if self.library.get_mapping_data(footprint, value):
                    lcsc = self.library.get_mapping_data(footprint, value)[2]
                    self.store.set_lcsc(reference, lcsc)
                    self.logger.info("Found %s", lcsc)
                    details = self.library.get_part_details(lcsc)
                    params = params_for_part(self.library.get_part_details(lcsc))
                    self.partlist_data_model.set_lcsc(
                        reference, lcsc, details["type"], details["stock"], params
                    )
                    self.start_assembly_enrichment([reference])
        self.recompute_bom_estimate()

    def sanitize_lcsc(self, lcsc_PN):
        """Sanitize a given LCSC number using a regex."""
        m = re.search("C\\d+", lcsc_PN, re.IGNORECASE)
        if m:
            return m.group(0)
        return ""

    def OnRightDown(self, *_):
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

        find_mapping = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_FIND_MAPPING, "Find LCSC from Mappings"
        )
        right_click_menu.Append(find_mapping)
        right_click_menu.Bind(wx.EVT_MENU, self.search_foot_mapping, find_mapping)

        add_mapping = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_ADD_MAPPING, "Add Footprint Mapping"
        )
        right_click_menu.Append(add_mapping)
        right_click_menu.Bind(wx.EVT_MENU, self.add_foot_mapping, add_mapping)

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
