"""Contains the settings dialog."""

import logging

import wx  # pylint: disable=import-error

# Import library configuration to populate choices
from .dblib import LIBRARY_CONFIGS
from .events import UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled


class SettingsDialog(wx.Dialog):
    """Dialog for plugin settings."""

    def __init__(self, parent):
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
            label="Do not tent vias",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_tented_vias",
        )

        self.tented_vias_setting.SetToolTip(
            wx.ToolTip("Whether vias should be coverd by soldermask or not")
        )

        self.tented_vias_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("tented.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.tented_vias_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        tented_vias_sizer = wx.BoxSizer(wx.HORIZONTAL)
        tented_vias_sizer.Add(self.tented_vias_image, 10, wx.ALL | wx.EXPAND, 5)
        tented_vias_sizer.Add(self.tented_vias_setting, 100, wx.ALL | wx.EXPAND, 5)

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

        fill_zones_sizer = wx.BoxSizer(wx.HORIZONTAL)
        fill_zones_sizer.Add(self.fill_zones_image, 10, wx.ALL | wx.EXPAND, 5)
        fill_zones_sizer.Add(self.fill_zones_setting, 100, wx.ALL | wx.EXPAND, 5)

        ##### Force DRC before Gerber export #####

        self.force_drc_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Force DRC check before Gerber export - Saves board and fills zones!",
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
            loadBitmapScaled("bug-check-outline.png", self.parent.scale_factor, static=True),
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )

        self.force_drc_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        force_drc_sizer = wx.BoxSizer(wx.HORIZONTAL)
        force_drc_sizer.Add(self.force_drc_image, 10, wx.ALL | wx.EXPAND, 5)
        force_drc_sizer.Add(self.force_drc_setting, 100, wx.ALL | wx.EXPAND, 5)

        ##### Plot values #####

        self.plot_values_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Plot values",
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

        plot_values_sizer = wx.BoxSizer(wx.HORIZONTAL)
        plot_values_sizer.Add(self.plot_values_image, 10, wx.ALL | wx.EXPAND, 5)
        plot_values_sizer.Add(self.plot_values_setting, 100, wx.ALL | wx.EXPAND, 5)

        ##### Plot references #####

        self.plot_references_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Plot references",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_plot_references",
        )

        self.plot_references_setting.SetToolTip(
            wx.ToolTip("Whether value should be plotted on gerber generation")
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

        plot_references_sizer = wx.BoxSizer(wx.HORIZONTAL)
        plot_references_sizer.Add(self.plot_references_image, 10, wx.ALL | wx.EXPAND, 5)
        plot_references_sizer.Add(
            self.plot_references_setting, 100, wx.ALL | wx.EXPAND, 5
        )

        ##### LCSC priority #####

        self.lcsc_priority_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="LCSC number priority",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="general_lcsc_priority",
        )

        self.lcsc_priority_setting.SetToolTip(
            wx.ToolTip(
                "Whether LCSC number from schematic should overrule those in the database"
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

        self.lcsc_priority_setting.Bind(wx.EVT_CHECKBOX, self.update_settings)

        lcsc_priority_sizer = wx.BoxSizer(wx.HORIZONTAL)
        lcsc_priority_sizer.Add(self.lcsc_priority_image, 10, wx.ALL | wx.EXPAND, 5)
        lcsc_priority_sizer.Add(self.lcsc_priority_setting, 100, wx.ALL | wx.EXPAND, 5)

        ##### Only parts with LCSC number in BOM/CPL #####

        self.lcsc_bom_cpl_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Add parts without LCSC numbers to BOM/CPL",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
            style=0,
            name="gerber_lcsc_bom_cpl",
        )

        self.lcsc_bom_cpl_setting.SetToolTip(
            wx.ToolTip("Whether parts wihout LCSC number should be added to BOM/CPL")
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

        lcsc_bom_cpl_sizer = wx.BoxSizer(wx.HORIZONTAL)
        lcsc_bom_cpl_sizer.Add(self.lcsc_bom_cpl_image, 10, wx.ALL | wx.EXPAND, 5)
        lcsc_bom_cpl_sizer.Add(self.lcsc_bom_cpl_setting, 100, wx.ALL | wx.EXPAND, 5)

        ##### Check if order/serial number placeholder is present #####

        self.order_number_setting = wx.CheckBox(
            self,
            id=wx.ID_ANY,
            label="Check if an order/serial number placeholder is placed",
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

        order_number_sizer = wx.BoxSizer(wx.HORIZONTAL)
        order_number_sizer.Add(self.order_number_image, 10, wx.ALL | wx.EXPAND, 5)
        order_number_sizer.Add(self.order_number_setting, 100, wx.ALL | wx.EXPAND, 5)

        ##### Highlight text matches ######

        highlight_matches_label = wx.StaticText(
            self,
            id=wx.ID_ANY,
            label="Match highlighting",
            pos=wx.DefaultPosition,
            size=wx.DefaultSize,
        )

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

        highlight_matches_sizer = wx.BoxSizer(wx.HORIZONTAL)
        highlight_matches_sizer.Add(
            highlight_matches_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        highlight_matches_sizer.Add(
            self.highlight_matches_setting, 0, wx.ALL | wx.EXPAND, 5
        )

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
        library_sizer.Add(library_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        library_sizer.Add(self.library_selected_setting, 1, wx.ALL | wx.EXPAND, 5)

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
                " If you change this, you may want to copy existing mapping and"
                " corrections files from the old location to the new one to avoid"
                " losing existing mappings and corrections."
            )
        )

        self.library_data_path_setting.Bind(
            wx.EVT_DIRPICKER_CHANGED, self.update_settings
        )

        library_data_path_sizer = wx.BoxSizer(wx.HORIZONTAL)
        library_data_path_sizer.Add(
            library_data_path_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5
        )
        library_data_path_sizer.Add(
            self.library_data_path_setting, 1, wx.ALL | wx.EXPAND, 5
        )

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        layout = wx.GridSizer(12, 2, 0, 0)
        layout.Add(tented_vias_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(fill_zones_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(force_drc_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(plot_values_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(plot_references_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(lcsc_priority_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(lcsc_bom_cpl_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(order_number_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(highlight_matches_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(library_sizer, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(library_data_path_sizer, 0, wx.ALL | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def update_tented_vias(self, tented):
        """Update settings dialog according to the settings."""
        if tented:
            self.tented_vias_setting.SetValue(tented)
            self.tented_vias_setting.SetLabel("Tented vias")
            self.tented_vias_image.SetBitmap(
                loadBitmapScaled("tented.png", self.parent.scale_factor, static=True)
            )
        else:
            self.tented_vias_setting.SetValue(tented)
            self.tented_vias_setting.SetLabel("Untented vias")
            self.tented_vias_image.SetBitmap(
                loadBitmapScaled("untented.png", self.parent.scale_factor, static=True)
            )

    def update_fill_zones(self, fill):
        """Update settings dialog according to the settings."""
        if fill:
            self.fill_zones_setting.SetValue(fill)
            self.fill_zones_setting.SetLabel("Fill zones")
            self.fill_zones_image.SetBitmap(
                loadBitmapScaled(
                    "fill-zones.png", self.parent.scale_factor, static=True
                )
            )
        else:
            self.fill_zones_setting.SetValue(fill)
            self.fill_zones_setting.SetLabel("Don't fill zones")
            self.fill_zones_image.SetBitmap(
                loadBitmapScaled(
                    "unfill-zones.png", self.parent.scale_factor, static=True
                )
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
        if plot_values:
            self.plot_values_setting.SetValue(plot_values)
            self.plot_values_setting.SetLabel("Plot values on silkscreen")
            self.plot_values_image.SetBitmap(
                loadBitmapScaled(
                    "plot_values.png", self.parent.scale_factor, static=True
                )
            )
        else:
            self.plot_values_setting.SetValue(plot_values)
            self.plot_values_setting.SetLabel("Don't plot values on silkscreen")
            self.plot_values_image.SetBitmap(
                loadBitmapScaled("no_values.png", self.parent.scale_factor, static=True)
            )

    def update_force_drc(self, force_drc):
        """Update settings dialog according to the settings."""
        self.force_drc_setting.SetValue(bool(force_drc))
        self.force_drc_image.SetBitmap(self.build_force_drc_bitmap(bool(force_drc)))
        if force_drc:
            self.force_drc_setting.SetLabel(
                "Force DRC check before Gerber export - Saves board and fills zones!"
            )
            self.update_fill_zones(True)
            self.fill_zones_setting.Disable()
        else:
            self.force_drc_setting.SetLabel(
                "Do not force DRC check before Gerber export"
            )
            self.fill_zones_setting.Enable()

    def update_plot_references(self, plot_references):
        """Update settings dialog according to the settings."""
        if plot_references:
            self.plot_references_setting.SetValue(plot_references)
            self.plot_references_setting.SetLabel("Plot references on silkscreen")
            self.plot_references_image.SetBitmap(
                loadBitmapScaled("plot_refs.png", self.parent.scale_factor, static=True)
            )
        else:
            self.plot_references_setting.SetValue(plot_references)
            self.plot_references_setting.SetLabel("Don't plot references on silkscreen")
            self.plot_references_image.SetBitmap(
                loadBitmapScaled("no_refs.png", self.parent.scale_factor, static=True)
            )

    def update_lcsc_priority(self, priority):
        """Update settings dialog according to the settings."""
        if priority:
            self.lcsc_priority_setting.SetValue(priority)
            self.lcsc_priority_setting.SetLabel(
                "LCSC numbers from schematic have priority"
            )
            self.lcsc_priority_image.SetBitmap(
                loadBitmapScaled("schematic.png", self.parent.scale_factor, static=True)
            )
        else:
            self.lcsc_priority_setting.SetValue(priority)
            self.lcsc_priority_setting.SetLabel(
                "LCSC numbers from database have priority"
            )
            self.lcsc_priority_image.SetBitmap(
                loadBitmapScaled(
                    "database-outline.png", self.parent.scale_factor, static=True
                )
            )

    def update_lcsc_bom_cpl(self, add):
        """Update settings dialog according to the settings."""
        if add:
            self.lcsc_bom_cpl_setting.SetValue(add)
            self.lcsc_bom_cpl_setting.SetLabel(
                "Add parts without LCSC number to BOM/POS"
            )
            self.lcsc_bom_cpl_image.SetBitmap(
                loadBitmapScaled("bom.png", self.parent.scale_factor, static=True)
            )
        else:
            self.lcsc_bom_cpl_setting.SetValue(add)
            self.lcsc_bom_cpl_setting.SetLabel(
                "Don't add parts without LCSC number to BOM/POS"
            )
            self.lcsc_bom_cpl_image.SetBitmap(
                loadBitmapScaled("no_bom.png", self.parent.scale_factor, static=True)
            )

    def update_order_number(self, check):
        """Update settings dialog according to the settings."""
        self.logger.debug(check)
        if check:
            self.order_number_setting.SetValue(check)
            self.order_number_setting.SetLabel(
                "Check if an order/serial number placeholder is placed"
            )
            self.order_number_image.SetBitmap(
                loadBitmapScaled(
                    "order_number.png", self.parent.scale_factor, static=True
                )
            )
        else:
            self.order_number_setting.SetValue(check)
            self.order_number_setting.SetLabel(
                "Don't check if an order/serial number placeholder is placed"
            )
            self.order_number_image.SetBitmap(
                loadBitmapScaled(
                    "no_order_number.png", self.parent.scale_factor, static=True
                )
            )

    def update_highlight_matches(self, enabled):
        """Update settings dialog according to the settings."""
        self.highlight_matches_setting.SetValue(bool(enabled))
        if enabled:
            self.highlight_matches_setting.SetLabel("Highlight search matches")
        else:
            self.highlight_matches_setting.SetLabel("Do not highlight search matches")

    def update_matches(self, enabled):
        """Alias shared highlighting setting updates to the checkbox UI helper."""
        self.update_highlight_matches(enabled)

    def load_settings(self):
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
        self.update_selected_library(
            self.parent.settings.get("library", {}).get(
                "selected_library", "current-parts"
            )
        )
        self.update_data_path(
            self.parent.settings.get("library", {}).get("data_path", "")
        )

    def update_selected_library(self, library_key):
        """Update settings dialog according to the selected library."""
        if library_key in LIBRARY_CONFIGS:
            display_name = LIBRARY_CONFIGS[library_key].display_name
            self.library_selected_setting.SetStringSelection(display_name)

    def update_data_path(self, data_path):
        """Update settings dialog according to the configured data path."""
        value = data_path.strip() if isinstance(data_path, str) else ""
        effective_path = value if value else self.parent.library.datadir
        self.library_data_path_setting.SetPath(effective_path)

    def update_settings(self, event):
        """Update and persist a setting that was changed."""
        section, name = event.GetEventObject().GetName().split("_", 1)
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

        # If forced DRC is enabled, fill zones must stay enabled.
        if (
            section == "gerber"
            and name == "fill_zones"
            and self.force_drc_setting.GetValue()
        ):
            value = True

        getattr(self, f"update_{name}")(value)

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
