"""Contains the settings dialog."""

import contextlib
import logging
from typing import TYPE_CHECKING

import wx  # pylint: disable=import-error

# Import library configuration to populate choices
from .bom_estimation.help_text import show_bom_estimator_help
from .dblib import LIBRARY_CONFIGS
from .events import UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled

if TYPE_CHECKING:
    from .mainwindow import JLCPCBTools

# Display strings for the LCSC priority dropdown; the stored setting stays a boolean.
LCSC_PRIORITY_SCHEMATIC = "Schematic"
LCSC_PRIORITY_DATABASE = "Database"
LCSC_PRIORITY_CHOICES = [LCSC_PRIORITY_SCHEMATIC, LCSC_PRIORITY_DATABASE]


# Side of the square icon cell in every settings row (largest icon is 48 px).
ICON_CELL_SIZE = 48


class SettingsDialog(wx.Dialog):
    """Dialog for plugin settings."""

    def __init__(self, parent: "JLCPCBTools") -> None:
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB tools settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(1300, 800)),
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

        # ---------------------------------------------------------------------
        # ------------------------- Change settings ---------------------------
        # ---------------------------------------------------------------------

        ##### Tented vias #####

        self.tented_vias_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Tent vias",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_tented_vias",
        )

        self.tented_vias_setting.SetToolTip(wx.ToolTip("Cover vias with soldermask"))

        self.tented_vias_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("tented.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.tented_vias_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Fill zones #####

        self.fill_zones_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Fill zones",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_fill_zones",
        )

        self.fill_zones_setting.SetToolTip(
            wx.ToolTip("Whether zones should be filled on gerber generation")
        )

        self.fill_zones_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("fill-zones.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.fill_zones_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Force DRC before Gerber export #####

        self.force_drc_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Force DRC check before Gerber export (saves board and fills zones)",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_force_drc",
        )

        self.force_drc_setting.SetToolTip(
            wx.ToolTip(
                "Run kicad-cli DRC with error severity before generating Gerbers (Saves board and fills zones!)"
            )
        )

        self.force_drc_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled(
                "bug-check-outline.png", self.parent.scale_factor, static=True
            ),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.force_drc_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Plot values #####

        self.plot_values_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Plot values on silkscreen",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_plot_values",
        )

        self.plot_values_setting.SetToolTip(
            wx.ToolTip("Whether value should be plotted on gerber generation")
        )

        self.plot_values_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("plot_values.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.plot_values_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Plot references #####

        self.plot_references_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Plot references on silkscreen",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_plot_references",
        )

        self.plot_references_setting.SetToolTip(
            wx.ToolTip("Whether references should be plotted on gerber generation")
        )

        self.plot_references_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("plot_refs.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.plot_references_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Subtract mask from silkscreen #####

        self.subtract_mask_from_silk_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Subtract soldermask from silkscreen",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_subtract_mask_from_silk",
        )

        self.subtract_mask_from_silk_setting.SetToolTip(
            wx.ToolTip(
                "Whether silkscreen should be removed where soldermask openings are present"
            )
        )

        self.subtract_mask_from_silk_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### LCSC priority #####

        lcsc_priority_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Prefer LCSC numbers from:",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

        self.lcsc_priority_setting = wx.ComboBox(
            self,
            id=wx.ID_ANY,
            value="",
            choices=LCSC_PRIORITY_CHOICES,
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=wx.CB_READONLY,
            name="general_lcsc_priority",
        )

        self.lcsc_priority_setting.SetToolTip(
            wx.ToolTip(
                "When a part has an LCSC number in both the schematic and the plugin database, which one is used"
            )
        )

        self.lcsc_priority_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("schematic.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.lcsc_priority_setting.Bind(wx.EVT_COMBOBOX, self.update_settings)

        lcsc_priority_sizer = wx.BoxSizer(wx.HORIZONTAL)
        lcsc_priority_sizer.Add(
            lcsc_priority_label, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5
        )
        lcsc_priority_sizer.Add(self.lcsc_priority_setting, 0, wx.ALIGN_CENTER_VERTICAL)

        ##### Only parts with LCSC number in BOM/CPL #####

        self.lcsc_bom_cpl_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Add parts without LCSC number to BOM/CPL",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_lcsc_bom_cpl",
        )

        self.lcsc_bom_cpl_setting.SetToolTip(
            wx.ToolTip("Whether parts without LCSC number should be added to BOM/CPL")
        )

        self.lcsc_bom_cpl_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("bom.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.lcsc_bom_cpl_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Check if order/serial number placeholder is present #####

        self.order_number_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Check for an order/serial number placeholder on export",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="general_order_number",
        )

        self.order_number_setting.SetToolTip(
            wx.ToolTip("Is an order/serial number placeholder placed")
        )

        self.order_number_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("order_number.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.order_number_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Highlight text matches ######

        self.highlight_matches_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Highlight search matches",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="highlighting_matches",
        )

        self.highlight_matches_setting.SetToolTip(
            wx.ToolTip(
                "Highlight keyword matches in the part selector and main window LCSC Params column"
            )
        )

        self.highlight_matches_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        self.simplify_stock_setting = wx.CheckBox(
            self,
            label="Simplify stock",
            name="general_simplify_stock",
        )
        self.simplify_stock_setting.SetToolTip(
            wx.ToolTip(
                "Show compact stock in the parts lists, for example 22k or 8.8M. "
                "Turn off to show exact quantities."
            )
        )
        self.simplify_stock_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        self.stock_concern_setting = wx.CheckBox(
            self,
            label="Highlight stock concern",
            name="highlighting_stock_concern",
        )
        self.stock_concern_setting.SetToolTip(
            wx.ToolTip(
                "Highlight Stock when availability is unknown or fewer than 10 "
                "times the required quantity are available. Uses the BOM "
                "estimator's board quantity, even when its panel is hidden. "
                "Groups populated BOM parts by LCSC number, including parts "
                "excluded from CPL."
            )
        )
        self.stock_concern_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        ##### Library Selection #####

        library_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Parts Library:",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

        library_choices = [config.display_name for config in LIBRARY_CONFIGS.values()]
        self.library_selected_setting = wx.ComboBox(
            self,
            id=wx.ID_ANY,
            value="",
            choices=library_choices,
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=wx.CB_READONLY,
            name="library_selected_library",
        )

        self.library_selected_setting.SetToolTip(
            wx.ToolTip("Select which parts library to use")
        )

        self.library_selected_setting.Bind(wx.EVT_COMBOBOX, self.update_settings)

        library_sizer = wx.BoxSizer(wx.HORIZONTAL)
        library_sizer.Add(library_label, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5)
        library_sizer.Add(self.library_selected_setting, 1, wx.EXPAND)

        ##### Library Data Directory #####

        library_data_path_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Database directory:",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

        self.library_data_path_setting = wx.DirPickerCtrl(
            self,
            id=wx.ID_ANY,
            path="",
            message="Choose folder for global library database files",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=wx.DIRP_DEFAULT_STYLE | wx.DIRP_USE_TEXTCTRL,
            name="library_data_path",
        )

        self.library_data_path_setting.SetToolTip(
            wx.ToolTip(
                "Override where the global library database files are stored."
                " If you change this, you may want to copy existing part preferences and"
                " corrections files from the old location to the new one to avoid"
                " losing existing part preferences and corrections."
            )
        )

        self.library_data_path_setting.Bind(
            wx.EVT_DIRPICKER_CHANGED, self.update_settings
        )

        library_data_path_sizer = wx.BoxSizer(wx.HORIZONTAL)
        library_data_path_sizer.Add(
            library_data_path_label, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5
        )
        library_data_path_sizer.Add(self.library_data_path_setting, 1, wx.EXPAND)

        ##### Part preferences #####

        self.part_preferences_remember_lcsc_assignments_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Remember my part preferences",
            name="part_preferences.remember_lcsc_assignments",
        )
        self.part_preferences_remember_lcsc_assignments_setting.SetToolTip(
            wx.ToolTip(
                "When you select or paste an LCSC part, remember it for components"
                " with the same value and footprint across projects. A later choice"
                " replaces the previous preference. Opening a board does not change"
                " preferences. Save part preferences remains available in the"
                " right-click menu when this is disabled."
            )
        )
        self.part_preferences_remember_lcsc_assignments_setting.Bind(
            wx.EVT_CHECKBOX, self.update_settings
        )

        self.part_preferences_fill_empty_lcsc_assignments_on_open_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Parts preferences fill in empty LCSC assignments",
            name="part_preferences.fill_empty_lcsc_assignments_on_open",
        )
        self.part_preferences_fill_empty_lcsc_assignments_on_open_setting.SetToolTip(
            wx.ToolTip(
                "When the plugin window opens, use the preferred LCSC part for"
                " each matching value and footprint. Existing assignments are"
                " kept. Skip DNP parts and parts excluded from BOM or placement."
                " Cleared assignments may fill again on the next opening unless"
                " the part is excluded or this setting is disabled."
            )
        )
        self.part_preferences_fill_empty_lcsc_assignments_on_open_setting.Bind(
            wx.EVT_CHECKBOX, self.update_settings
        )

        part_preferences_box_sizer = wx.StaticBoxSizer(
            wx.HORIZONTAL, self, "Part preferences"
        )
        part_preferences_box_sizer.Add(
            self.part_preferences_remember_lcsc_assignments_setting,
            1,
            wx.ALL | wx.EXPAND,
            5,
        )
        part_preferences_box_sizer.Add(
            self.part_preferences_fill_empty_lcsc_assignments_on_open_setting,
            1,
            wx.ALL | wx.EXPAND,
            5,
        )

        ##### Generation hooks #####

        pre_hook_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Pre-generate hook script:",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

        self.pre_script_setting = wx.FilePickerCtrl(
            self,
            id=wx.ID_ANY,
            path="",
            message="Choose pre-generate hook script",
            wildcard="All files (*.*)|*.*",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=wx.FLP_DEFAULT_STYLE | wx.FLP_USE_TEXTCTRL,
            name="hooks_pre_script",
        )
        self.pre_script_setting.SetToolTip(
            wx.ToolTip(
                "Runs before fabrication generation."
                " A nonzero exit code shows a Continue/Cancel prompt."
            )
        )

        self.pre_script_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("mdi-terminal.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.pre_script_setting.Bind(wx.EVT_FILEPICKER_CHANGED, self.update_settings)

        pre_hook_sizer = wx.BoxSizer(wx.HORIZONTAL)
        pre_hook_sizer.Add(
            self.pre_script_image, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        pre_hook_sizer.Add(pre_hook_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        pre_hook_sizer.Add(self.pre_script_setting, 1, wx.ALL | wx.EXPAND, 5)

        post_hook_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Post-generate hook script:",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

        self.post_script_setting = wx.FilePickerCtrl(
            self,
            id=wx.ID_ANY,
            path="",
            message="Choose post-generate hook script",
            wildcard="All files (*.*)|*.*",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=wx.FLP_DEFAULT_STYLE | wx.FLP_USE_TEXTCTRL,
            name="hooks_post_script",
        )
        self.post_script_setting.SetToolTip(
            wx.ToolTip("Runs only after successful fabrication generation.")
        )

        self.post_script_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("mdi-terminal.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.post_script_setting.Bind(wx.EVT_FILEPICKER_CHANGED, self.update_settings)

        post_hook_sizer = wx.BoxSizer(wx.HORIZONTAL)
        post_hook_sizer.Add(
            self.post_script_image, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        post_hook_sizer.Add(post_hook_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        post_hook_sizer.Add(self.post_script_setting, 1, wx.ALL | wx.EXPAND, 5)

        hook_timeout_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Hook timeout (seconds):",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

        self.timeout_seconds_setting = wx.SpinCtrl(
            self,
            id=wx.ID_ANY,
            min=1,
            max=3600,
            initial=30,
            name="hooks_timeout_seconds",
        )
        self.timeout_seconds_setting.SetToolTip(
            wx.ToolTip("Maximum runtime for pre/post hook scripts.")
        )

        self.timeout_seconds_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled(
                "mdi-hourglass-top.png", self.parent.scale_factor, static=True
            ),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.timeout_seconds_setting.Bind(wx.EVT_SPINCTRL, self.update_settings)

        timeout_sizer = wx.BoxSizer(wx.HORIZONTAL)
        timeout_sizer.Add(
            self.timeout_seconds_image, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        timeout_sizer.Add(hook_timeout_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        timeout_sizer.Add(
            self.timeout_seconds_setting, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )

        hooks_box_sizer = wx.StaticBoxSizer(wx.VERTICAL, self, "Generation hooks")
        hooks_box_sizer.Add(pre_hook_sizer, 0, wx.ALL | wx.EXPAND, 5)
        hooks_box_sizer.Add(post_hook_sizer, 0, wx.ALL | wx.EXPAND, 5)
        hooks_box_sizer.Add(timeout_sizer, 0, wx.ALL | wx.EXPAND, 5)

        ##### Show BOM Cost Estimator panel #####

        self.bom_estimator_show_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Show BOM cost estimator",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="general_bom_estimator_show",
        )

        self.bom_estimator_show_setting.SetToolTip(
            wx.ToolTip(
                "Whether the BOM cost estimator panel is shown in the main window"
            )
        )

        self.bom_estimator_show_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("bom.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.bom_estimator_show_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)
        self.bom_estimator_help_button = wx.Button(
            self,
            wx.ID_ANY,
            "Help",
        )
        self.bom_estimator_help_button.SetToolTip(
            wx.ToolTip("Show BOM estimator assumptions and limitations")
        )
        self.bom_estimator_help_button.Bind(
            wx.EVT_BUTTON,
            self.show_bom_estimator_help,
        )

        bom_estimator_show_sizer = wx.BoxSizer(wx.HORIZONTAL)
        bom_estimator_show_sizer.Add(
            self.bom_estimator_show_setting, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5
        )
        bom_estimator_show_sizer.AddStretchSpacer()
        bom_estimator_show_sizer.Add(
            self.bom_estimator_help_button, 0, wx.ALIGN_CENTER_VERTICAL
        )

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        # Two settings columns, each a fixed-size icon cell plus a control cell,
        # so the controls line up and every row has the same height whatever
        # the icon size (or absence of an icon).
        settings_grid = wx.FlexGridSizer(0, 4, 0, 0)
        settings_grid.AddGrowableCol(1, 1)
        settings_grid.AddGrowableCol(3, 1)
        self._add_setting_row(
            settings_grid, self.tented_vias_image, self.tented_vias_setting
        )
        self._add_setting_row(
            settings_grid, self.fill_zones_image, self.fill_zones_setting
        )
        self._add_setting_row(
            settings_grid, self.force_drc_image, self.force_drc_setting
        )
        self._add_setting_row(
            settings_grid, self.plot_values_image, self.plot_values_setting
        )
        self._add_setting_row(
            settings_grid, self.plot_references_image, self.plot_references_setting
        )
        self._add_setting_row(settings_grid, None, self.subtract_mask_from_silk_setting)
        self._add_setting_row(
            settings_grid, self.lcsc_priority_image, lcsc_priority_sizer
        )
        self._add_setting_row(
            settings_grid, self.lcsc_bom_cpl_image, self.lcsc_bom_cpl_setting
        )
        self._add_setting_row(
            settings_grid, self.order_number_image, self.order_number_setting
        )
        self._add_setting_row(settings_grid, None, self.highlight_matches_setting)
        self._add_setting_row(settings_grid, None, self.simplify_stock_setting)
        self._add_setting_row(settings_grid, None, self.stock_concern_setting)
        self._add_setting_row(
            settings_grid,
            self.bom_estimator_show_image,
            bom_estimator_show_sizer,
            wx.EXPAND,
        )
        self._add_setting_row(settings_grid, None, library_sizer, wx.EXPAND)
        self._add_setting_row(settings_grid, None, library_data_path_sizer, wx.EXPAND)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(settings_grid, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(part_preferences_box_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(hooks_box_sizer, 0, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def _add_setting_row(self, grid, image, control, flags=wx.ALIGN_CENTER_VERTICAL):
        """Add an icon | control pair to the settings grid.

        The icon sits centred in a square cell of fixed size (empty when the
        setting has no icon), so controls share one left edge and rows share
        one pitch regardless of icon size.
        """
        side = int(round(ICON_CELL_SIZE * self.parent.scale_factor))
        icon_cell = wx.BoxSizer(wx.HORIZONTAL)
        icon_cell.SetMinSize(side, side)
        if image is not None:
            icon_cell.AddStretchSpacer()
            icon_cell.Add(image, 0, wx.ALIGN_CENTER_VERTICAL)
            icon_cell.AddStretchSpacer()
        grid.Add(icon_cell, 0, wx.ALL, 5)
        grid.Add(control, 0, wx.ALL | flags, 5)

    def update_tented_vias(self, tented):
        """Update settings dialog according to the settings."""
        self.tented_vias_setting.SetValue(tented)
        icon = "tented.png" if tented else "untented.png"
        self.tented_vias_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def update_fill_zones(self, fill):
        """Update settings dialog according to the settings."""
        self.fill_zones_setting.SetValue(fill)
        icon = "fill-zones.png" if fill else "unfill-zones.png"
        self.fill_zones_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def build_force_drc_bitmap(self, enabled):
        """Build the Force DRC icon, overlaying a red X when disabled."""
        bitmap = loadBitmapScaled(
            "bug-check-outline.png", self.parent.scale_factor, static=True
        )
        if enabled:
            return bitmap

        return self.create_disabled_bitmap(bitmap)

    def create_disabled_bitmap(self, bitmap):
        """Create a disabled-state bitmap by drawing a red X over it."""
        disabled_bitmap = bitmap.ConvertToImage().ConvertToBitmap()
        memory_dc = wx.MemoryDC()
        memory_dc.SelectObject(disabled_bitmap)
        try:
            pen_width = max(2, int(round(self.parent.scale_factor * 2)))
            margin = max(2, int(round(self.parent.scale_factor * 3)))
            width, height = disabled_bitmap.GetSize()
            memory_dc.SetPen(wx.Pen(wx.Colour(220, 0, 0), width=pen_width))
            memory_dc.DrawLine(margin, margin, width - margin, height - margin)
            memory_dc.DrawLine(margin, height - margin, width - margin, margin)
        finally:
            memory_dc.SelectObject(wx.NullBitmap)

        return disabled_bitmap

    def update_plot_values(self, plot_values):
        """Update settings dialog according to the settings."""
        self.plot_values_setting.SetValue(plot_values)
        icon = "plot_values.png" if plot_values else "no_values.png"
        self.plot_values_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def update_force_drc(self, force_drc):
        """Update settings dialog according to the settings."""
        self.force_drc_setting.SetValue(bool(force_drc))
        self.force_drc_image.SetBitmap(self.build_force_drc_bitmap(bool(force_drc)))
        if force_drc:
            self.update_fill_zones(True)
            self.fill_zones_setting.Disable()
        else:
            self.fill_zones_setting.Enable()

    def update_plot_references(self, plot_references):
        """Update settings dialog according to the settings."""
        self.plot_references_setting.SetValue(plot_references)
        icon = "plot_refs.png" if plot_references else "no_refs.png"
        self.plot_references_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def update_subtract_mask_from_silk(self, enabled):
        """Update subtract-mask-from-silk setting value."""
        self.subtract_mask_from_silk_setting.SetValue(bool(enabled))

    def update_lcsc_priority(self, priority):
        """Update settings dialog according to the settings."""
        if priority:
            self.lcsc_priority_setting.SetStringSelection(LCSC_PRIORITY_SCHEMATIC)
            icon = "schematic.png"
        else:
            self.lcsc_priority_setting.SetStringSelection(LCSC_PRIORITY_DATABASE)
            icon = "database-outline.png"
        self.lcsc_priority_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def update_lcsc_bom_cpl(self, add):
        """Update settings dialog according to the settings."""
        self.lcsc_bom_cpl_setting.SetValue(add)
        icon = "bom.png" if add else "no_bom.png"
        self.lcsc_bom_cpl_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def update_order_number(self, check):
        """Update settings dialog according to the settings."""
        self.order_number_setting.SetValue(check)
        icon = "order_number.png" if check else "no_order_number.png"
        self.order_number_image.SetBitmap(
            loadBitmapScaled(icon, self.parent.scale_factor, static=True)
        )

    def update_highlight_matches(self, enabled):
        """Update settings dialog according to the settings."""
        self.highlight_matches_setting.SetValue(bool(enabled))

    def update_matches(self, enabled):
        """Alias shared highlighting setting updates to the checkbox UI helper."""
        self.update_highlight_matches(enabled)

    def update_simplify_stock(self, enabled: bool) -> None:
        """Reflect the stock presentation preference in its checkbox."""
        self.simplify_stock_setting.SetValue(bool(enabled))

    def update_stock_concern(self, enabled: bool) -> None:
        """Reflect the independent stock concern preference in its checkbox."""
        self.stock_concern_setting.SetValue(bool(enabled))

    def update_bom_estimator_show(self, show):
        """Update settings dialog according to the BOM estimator visibility setting."""
        self.bom_estimator_show_setting.SetValue(bool(show))

    def show_bom_estimator_help(self, *_):
        """Show shared BOM estimator help text via the help_text helper."""
        show_bom_estimator_help(self)

    def update_part_preferences_remember_lcsc_assignments(self, enabled: bool) -> None:
        """Update whether explicit LCSC assignments become part preferences."""
        self.part_preferences_remember_lcsc_assignments_setting.SetValue(bool(enabled))

    def update_part_preferences_fill_empty_lcsc_assignments_on_open(
        self, enabled: bool
    ) -> None:
        """Update whether part preferences fill empty assignments on opening."""
        self.part_preferences_fill_empty_lcsc_assignments_on_open_setting.SetValue(
            bool(enabled)
        )

    def load_settings(self) -> None:
        """Load settings and set checkboxes accordingly."""
        self.update_tented_vias(
            self.parent.settings.get("gerber", {}).get("tented_vias", True)
        )
        self.update_fill_zones(
            self.parent.settings.get("gerber", {}).get("fill_zones", True)
        )
        self.update_force_drc(
            self.parent.settings.get("gerber", {}).get("force_drc", False)
        )
        self.update_plot_values(
            self.parent.settings.get("gerber", {}).get("plot_values", True)
        )
        self.update_plot_references(
            self.parent.settings.get("gerber", {}).get("plot_references", True)
        )
        self.update_subtract_mask_from_silk(
            self.parent.settings.get("gerber", {}).get("subtract_mask_from_silk", True)
        )
        self.update_lcsc_priority(
            self.parent.settings.get("general", {}).get("lcsc_priority", True)
        )
        self.update_lcsc_bom_cpl(
            self.parent.settings.get("gerber", {}).get("lcsc_bom_cpl", True)
        )
        self.update_order_number(
            self.parent.settings.get("general", {}).get("order_number", True)
        )
        self.update_highlight_matches(
            self.parent.settings.get("highlighting", {}).get("matches", True)
        )
        self.update_simplify_stock(
            self.parent.settings.get("general", {}).get("simplify_stock", True)
        )
        self.update_stock_concern(
            self.parent.settings.get("highlighting", {}).get("stock_concern", True)
        )
        self.update_bom_estimator_show(
            self.parent.settings.get("general", {}).get("bom_estimator_show", True)
        )
        self.update_selected_library(
            self.parent.settings.get("library", {}).get(
                "selected_library", "current-parts"
            )
        )
        self.update_data_path(
            self.parent.settings.get("library", {}).get("data_path", "")
        )
        self.update_part_preferences_remember_lcsc_assignments(
            self.parent.settings.get("part_preferences", {}).get(
                "remember_lcsc_assignments", True
            )
        )
        self.update_part_preferences_fill_empty_lcsc_assignments_on_open(
            self.parent.settings.get("part_preferences", {}).get(
                "fill_empty_lcsc_assignments_on_open", True
            )
        )
        self.update_pre_script(
            self.parent.settings.get("hooks", {}).get("pre_script", "")
        )
        self.update_post_script(
            self.parent.settings.get("hooks", {}).get("post_script", "")
        )
        self.update_timeout_seconds(
            self.parent.settings.get("hooks", {}).get("timeout_seconds", 30)
        )

    def update_selected_library(self, library_key):
        """Update settings dialog according to the selected library."""
        if library_key in LIBRARY_CONFIGS:
            display_name = LIBRARY_CONFIGS[library_key].display_name
            self.library_selected_setting.SetStringSelection(display_name)

    def update_data_path(self, data_path: object) -> None:
        """Update settings dialog according to the configured data path."""
        value = data_path.strip() if isinstance(data_path, str) else ""
        effective_path = value if value else getattr(self.parent.library, "datadir", "")
        self.library_data_path_setting.SetPath(effective_path)

    def update_pre_script(self, script_path):
        """Update settings dialog according to pre-hook script path."""
        value = script_path.strip() if isinstance(script_path, str) else ""
        self.pre_script_setting.SetPath(value)

    def update_post_script(self, script_path):
        """Update settings dialog according to post-hook script path."""
        value = script_path.strip() if isinstance(script_path, str) else ""
        self.post_script_setting.SetPath(value)

    def update_timeout_seconds(self, timeout_seconds):
        """Update settings dialog according to hook timeout."""
        with contextlib.suppress(ValueError, TypeError):
            timeout = int(timeout_seconds)
            self.timeout_seconds_setting.SetValue(max(1, timeout))
            return
        self.timeout_seconds_setting.SetValue(30)

    def update_settings(self, event: "wx.CommandEvent") -> None:
        """Update and persist a setting that was changed."""
        control_name = event.GetEventObject().GetName()
        if "." in control_name:
            # A dot separates section names that themselves contain underscores.
            section, name = control_name.split(".", 1)
            update_method = f"update_{section}_{name}"
        else:
            section, name = control_name.split("_", 1)
            update_method = f"update_{name}"
        if hasattr(event.GetEventObject(), "GetPath"):
            value = event.GetEventObject().GetPath()
        else:
            value = event.GetEventObject().GetValue()
        self.logger.debug(section)
        self.logger.debug(name)
        self.logger.debug(value)

        # Special handling for library selection: convert display name back to key
        if section == "library" and name == "selected_library":
            # Find the key for this display name
            for key, config in LIBRARY_CONFIGS.items():
                if config.display_name == value:
                    self.logger.debug("Selected library key: %s", key)
                    value = key
                    break

        # Special handling for LCSC priority: the dropdown text maps onto the
        # boolean that has always been stored (True = schematic wins).
        if section == "general" and name == "lcsc_priority":
            value = value == LCSC_PRIORITY_SCHEMATIC

        # If forced DRC is enabled, fill zones must stay enabled.
        if (
            section == "gerber"
            and name == "fill_zones"
            and self.force_drc_setting.GetValue()
        ):
            value = True

        getattr(self, update_method)(value)

        # Turning on forced DRC implies enabling fill zones.
        if section == "gerber" and name == "force_drc" and value:
            wx.PostEvent(
                self.parent,
                UpdateSetting(
                    section="gerber",
                    setting="fill_zones",
                    value=True,
                ),
            )

        wx.PostEvent(
            self.parent,
            UpdateSetting(
                section=section,
                setting=name,
                value=value,
            ),
        )

    def quit_dialog(self, *_):
        """Close this dialog."""
        self.Destroy()
        self.EndModal(0)
