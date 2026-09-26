"""Contains the part selector modal window."""

from __future__ import annotations

from collections.abc import Callable, Collection
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Optional

import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from .bom_estimation.pricing import get_unit_price
from .datamodel import PartSelectorDataModel
from .dataview_highlight import HighlightedTextRenderer
from .derive_params import params_for_part  # pylint: disable=import-error
from .events import AssignPartsEvent, UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled
from .lcsc import is_lcsc_part, normalize_lcsc
from .partdetails import PartDetailsDialog
from .partselector_columns import (
    DB_FIELDS,
    PARAMS_COLUMN_KEY,
    PARTSELECTOR_COLUMN_KEYS,
    PARTSELECTOR_COLUMNS,
)
from .window_layout import get_column_widths, restore_column_widths, to_dip

if TYPE_CHECKING:
    from .mainwindow import JLCPCBTools

HIGHLIGHTED_COLUMN_KEYS = {
    "lcsc",
    "mfr_number",
    "package",
    "params",
    "mfr",
    "description",
}


_LCSC_FIELD = DB_FIELDS.index("LCSC Part")


def _format_duration(seconds: float) -> str:
    """Format a duration as seconds or milliseconds for UI labels."""
    return f"{seconds:.2f}s" if seconds > 1 else f"{seconds * 1000.0:.0f}ms"


def typed_lcsc_offer(
    keyword: str,
    shown: Collection[str],
    lookup: Callable[[str], dict[str, Any]],
) -> Optional[tuple[str, str, dict[str, Any]]]:
    """Offer a searched LCSC number that the results do not show.

    Keyword search matches substrings, so a number the catalog lacks seldom
    finds nothing: C19702 also matches C1970200 and a hundred more. The offer
    therefore turns on the exact number being absent from the rows shown, and
    a direct lookup decides what to say. A listed number can be hidden by the
    filters or pushed past the 1000-row limit; an unlisted one can still be
    assembled through JLC pre-order or global sourcing.

    Returns the number, the button label and the catalog details to assign
    with, or None when the keyword is not exactly one number or it is shown.
    """
    lcsc = normalize_lcsc(keyword)
    if not is_lcsc_part(lcsc) or lcsc in shown:
        return None
    details = lookup(lcsc)
    if details:
        label = f"{lcsc} is in the library but not in these results. Assign it"
        return lcsc, label, details
    return lcsc, f"{lcsc} isn't in the JLC library. Assign it anyway", {}


class PartSelectorDialog(wx.Dialog):
    """The part selector window."""

    def __init__(
        self,
        parent: JLCPCBTools,
        parts: dict[str, str],
        *,
        assignment_context: object = None,
        assignment_label: Optional[str] = None,
    ) -> None:
        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self._init_context(parent)

        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=assignment_label or "JLCPCB Library",
            pos=wx.DefaultPosition,
            size=HighResWxSize(self.window, wx.Size(1400, 800)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.parts = dict(parts)
        self.assignment_context = assignment_context
        self.assignment_label = assignment_label
        self._typed_lcsc_offer: Optional[tuple[str, str, dict[str, Any]]] = None
        lcsc_selection = self.get_existing_selection(self.parts)

        self.search_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.search)

        # The default EVT_CLOSE handler for a modeless wx.Dialog hides the
        # window instead of destroying it. Override so X-button / Close() do
        # what we want — actually destroy, so the singleton ref clears.
        self.Bind(wx.EVT_CLOSE, self._on_close)

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

        keyword_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Keywords",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
            style=wx.ALIGN_RIGHT,
        )
        self.keyword = wx.TextCtrl(
            self,
            wx.ID_ANY,
            lcsc_selection,
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(800, -1)),
            wx.TE_PROCESS_ENTER,
        )
        self.keyword.SetHint("e.g. 10k 0603")

        self.ohm_button = wx.Button(
            self,
            wx.ID_ANY,
            "Ω",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(20, -1)),
            0,
        )
        self.ohm_button.SetToolTip("Append the Ω symbol to the search string")

        self.micro_button = wx.Button(
            self,
            wx.ID_ANY,
            "µ",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(20, -1)),
            0,
        )
        self.micro_button.SetToolTip("Append the µ symbol to the search string")

        manufacturer_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Manufacturer",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.manufacturer = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            wx.TE_PROCESS_ENTER,
        )
        self.manufacturer.SetHint("e.g. Vishay")

        package_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Package",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.package = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            wx.TE_PROCESS_ENTER,
        )
        self.package.SetHint("e.g. 0603")

        category_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Category",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.category = wx.ComboBox(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            choices=parent.library.categories if parent.is_catalog_available() else [],
            style=wx.CB_READONLY,
        )
        self.category.SetHint("e.g. Resistors")

        part_no_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Part number",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.part_no = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            wx.TE_PROCESS_ENTER,
        )
        self.part_no.SetHint("e.g. DS2411")

        solder_joints_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Solder joints",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.solder_joints = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            wx.TE_PROCESS_ENTER,
        )
        self.solder_joints.SetHint("e.g. 2")

        subcategory_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Subcategory",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.subcategory = wx.ComboBox(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            style=wx.CB_READONLY,
        )
        self.subcategory.SetHint("e.g. Variable Resistors")

        basic_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include basic parts",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.basic_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Basic",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            0,
            name="basic",
        )
        preferred_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include preferred parts",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.preferred_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Preferred",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            0,
            name="preferred",
        )
        extended_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include extended parts",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.extended_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Extended",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            0,
            name="extended",
        )
        stock_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Only show parts in stock",
            size=HighResWxSize(parent.window, wx.Size(150, -1)),
        )
        self.assert_stock_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "in Stock",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(200, -1)),
            0,
            name="stock",
        )

        self.basic_checkbox.SetValue(
            self.parent.settings.get("partselector", {}).get("basic", True)
        )
        self.extended_checkbox.SetValue(
            self.parent.settings.get("partselector", {}).get("extended", True)
        )
        self.preferred_checkbox.SetValue(
            self.parent.settings.get("partselector", {}).get("preferred", True)
        )
        self.assert_stock_checkbox.SetValue(
            self.parent.settings.get("partselector", {}).get("stock", False)
        )

        self.basic_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)
        self.extended_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)
        self.preferred_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)
        self.assert_stock_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)

        help_button = wx.Button(
            self,
            wx.ID_ANY,
            "Help",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(100, -1)),
            0,
        )

        keyword_search_row1 = wx.BoxSizer(wx.HORIZONTAL)
        keyword_search_row1.Add(keyword_label, 0, wx.ALL, 5)
        keyword_search_row1.Add(
            self.keyword,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        keyword_search_row1.Add(
            self.ohm_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        keyword_search_row1.Add(
            self.micro_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_one = wx.BoxSizer(wx.VERTICAL)
        search_sizer_one.Add(manufacturer_label, 0, wx.ALL, 5)
        search_sizer_one.Add(
            self.manufacturer,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_one.Add(package_label, 0, wx.ALL, 5)
        search_sizer_one.Add(
            self.package,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_two = wx.BoxSizer(wx.VERTICAL)
        search_sizer_two.Add(category_label, 0, wx.ALL, 5)
        search_sizer_two.Add(
            self.category,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_two.Add(part_no_label, 0, wx.ALL, 5)
        search_sizer_two.Add(
            self.part_no,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_two.Add(solder_joints_label, 0, wx.ALL, 5)
        search_sizer_two.Add(
            self.solder_joints,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_three = wx.BoxSizer(wx.VERTICAL)
        search_sizer_three.Add(subcategory_label, 0, wx.ALL, 5)
        search_sizer_three.Add(
            self.subcategory,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_four = wx.BoxSizer(wx.VERTICAL)
        search_sizer_four.Add(basic_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.basic_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_four.Add(preferred_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.preferred_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_four.Add(extended_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.extended_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_four.Add(stock_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.assert_stock_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_five = wx.BoxSizer(wx.VERTICAL)
        search_sizer_five.Add(
            help_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        help_button.SetBitmap(
            loadBitmapScaled(
                "mdi-help-circle-outline.png",
                self.parent.scale_factor,
            )
        )
        help_button.SetBitmapMargins((2, 0))

        search_sizer = wx.StaticBoxSizer(wx.VERTICAL, self, "Search")

        search_sizer.Add(keyword_search_row1)

        search_sizer_row2 = wx.StaticBoxSizer(wx.HORIZONTAL, self)
        search_sizer_row2.Add(search_sizer_one, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_two, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_three, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_four, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_five, 0, wx.RIGHT, 20)
        # search_sizer.Add(help_button, 0, wx.RIGHT, 20)

        search_sizer.Add(search_sizer_row2)

        self.keyword.Bind(wx.EVT_TEXT, self.search_dwell)
        self.ohm_button.Bind(wx.EVT_BUTTON, self.add_ohm_symbol)
        self.micro_button.Bind(wx.EVT_BUTTON, self.add_micro_symbol)
        self.manufacturer.Bind(wx.EVT_TEXT, self.search_dwell)
        self.package.Bind(wx.EVT_TEXT, self.search_dwell)
        self.category.Bind(wx.EVT_COMBOBOX, self.update_subcategories)
        self.category.Bind(wx.EVT_TEXT, self.update_subcategories)
        self.part_no.Bind(wx.EVT_TEXT, self.search_dwell)
        self.solder_joints.Bind(wx.EVT_TEXT, self.search_dwell)
        help_button.Bind(wx.EVT_BUTTON, self.help)

        # ---------------------------------------------------------------------
        # ------------------------ Result status line -------------------------
        # ---------------------------------------------------------------------

        self.result_count = wx.StaticText(
            self, wx.ID_ANY, "0 Results", wx.DefaultPosition, wx.DefaultSize
        )

        # Offers a searched number the results do not show; see typed_lcsc_offer.
        self.typed_lcsc_button = wx.Button(self, wx.ID_ANY, "")
        self.typed_lcsc_button.Bind(wx.EVT_BUTTON, self.assign_typed_lcsc)
        self.typed_lcsc_button.Hide()

        result_sizer = wx.BoxSizer(wx.HORIZONTAL)
        result_sizer.Add(self.result_count, 0, wx.LEFT | wx.TOP, 5)
        result_sizer.Add(self.typed_lcsc_button, 0, wx.LEFT, 10)

        # ---------------------------------------------------------------------
        # ------------------------- Result Part list --------------------------
        # ---------------------------------------------------------------------

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)

        table_scroller = wx.ScrolledWindow(self, style=wx.HSCROLL | wx.VSCROLL)
        table_scroller.SetScrollRate(20, 20)

        self.part_list = dv.DataViewCtrl(
            table_scroller,
            style=wx.BORDER_THEME | dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_SINGLE,
        )
        align_map = {
            "left": wx.ALIGN_LEFT,
            "center": wx.ALIGN_CENTER,
        }
        for idx, column in enumerate(PARTSELECTOR_COLUMNS):
            if column.key in HIGHLIGHTED_COLUMN_KEYS:
                renderer = HighlightedTextRenderer(
                    highlight_text_getter=self.get_highlight_text,
                    align=align_map[column.align],
                )
                view_col = dv.DataViewColumn(
                    column.label,
                    renderer,
                    idx,
                    width=int(parent.scale_factor * column.width),
                    align=align_map[column.align],
                )
                self.part_list.AppendColumn(view_col)
            else:
                view_col = self.part_list.AppendTextColumn(
                    column.label,
                    idx,
                    width=int(parent.scale_factor * column.width),
                    mode=dv.DATAVIEW_CELL_INERT,
                    align=align_map[column.align],
                )
            if column.sortable:
                view_col.SetSortable(True)

        self.part_list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.OnPartSelected)
        self.part_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.select_part)
        scrolled_sizer = wx.BoxSizer(wx.VERTICAL)
        scrolled_sizer.Add(self.part_list, 1, wx.EXPAND)
        table_scroller.SetSizer(scrolled_sizer)

        table_sizer.Add(table_scroller, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.select_part_button = wx.Button(
            self,
            wx.ID_ANY,
            "Select part",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )
        self.part_details_button = wx.Button(
            self,
            wx.ID_ANY,
            "Show part details",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(150, -1)),
            0,
        )

        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)

        self.select_part_button.SetBitmap(
            loadBitmapScaled(
                "mdi-check.png",
                self.parent.scale_factor,
            )
        )
        self.select_part_button.SetBitmapMargins((2, 0))

        self.part_details_button.SetBitmap(
            loadBitmapScaled(
                "mdi-text-box-search-outline.png",
                self.parent.scale_factor,
            )
        )
        self.part_details_button.SetBitmapMargins((2, 0))

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        self.cancel_button = wx.Button(self, wx.ID_CANCEL, "Cancel")
        # Escape synthesizes a button event on the dialog, not on the button.
        self.Bind(wx.EVT_BUTTON, self.quit_dialog, id=wx.ID_CANCEL)
        self.SetEscapeId(wx.ID_CANCEL)
        tool_sizer.AddStretchSpacer()
        tool_sizer.Add(self.cancel_button, 0, wx.ALL | wx.EXPAND, 5)
        table_sizer.Add(tool_sizer, 3, wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------------ Sizers  ------------------------------
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(search_sizer, 1, wx.ALL, 5)
        # layout.Add(self.search_button, 5, wx.ALL, 5)
        layout.Add(result_sizer, 1, wx.LEFT, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.part_list_model = PartSelectorDataModel(
            simplify_stock=self.parent.settings.get("general", {}).get(
                "simplify_stock", True
            )
        )
        self.part_list.AssociateModel(self.part_list_model)

        self.SetSizer(layout)
        settings = self.parent.settings.get("partselector", {})
        self._normal_size = self._restore_size(settings.get("size"))
        self.Layout()
        self.Centre(wx.BOTH)
        restore_column_widths(
            self.part_list, settings.get("column_widths", {}), PARTSELECTOR_COLUMN_KEYS
        )
        self.enable_toolbar_buttons(False)
        self._changing_window_state = False
        self._layout_ready = True
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_MAXIMIZE, self._on_maximize)

        # initiate the initial search now that the window has been constructed
        self.search(None)

    def _init_context(self, parent: Any) -> None:
        """Initialize display and context attributes from parent window."""
        self.window = getattr(parent, "window", parent)
        self.scale_factor = getattr(parent, "scale_factor", 1.0)
        self.project_path = getattr(parent, "project_path", "")

    def _restore_size(self, size: object) -> list[int]:
        """Restore bounded normal geometry without passing untrusted sizes to wx."""
        default = [1400, 800]
        available = HighResWxSize(self, wx.Size(*default))
        display = wx.Display.GetFromWindow(self.parent)
        if display == wx.NOT_FOUND and wx.Display.GetCount():
            display = 0
        if display != wx.NOT_FOUND:
            work_area = wx.Display(display).GetClientArea().GetSize()
            if all(value > 0 for value in work_area):
                available = work_area
            else:
                size = default
        else:
            size = default
        if not (
            isinstance(size, list)
            and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
        ):
            size = default
        maximum = [max(1, value) for value in to_dip(self, available)]
        minimum = [min(value, limit) for value, limit in zip((1100, 600), maximum)]
        size = [
            min(max(value, floor), limit)
            for value, floor, limit in zip(size, minimum, maximum)
        ]
        pixel_sizes = []
        for logical_size in (minimum, size):
            pixels = HighResWxSize(self, wx.Size(*logical_size))
            # Fractional DPI conversion can round past the available work area.
            pixel_sizes.append(
                wx.Size(min(pixels[0], available[0]), min(pixels[1], available[1]))
            )
        self.SetSizeHints(pixel_sizes[0], wx.DefaultSize)
        self.SetSize(pixel_sizes[1])
        return size

    def _remember_normal_size(self) -> None:
        """Cache geometry only while the window is in its normal state."""
        if not self.IsMaximized() and not self.IsIconized() and not self.IsFullScreen():
            self._normal_size = list(to_dip(self, self.GetSize()))

    def _on_size(self, event: wx.SizeEvent) -> None:
        """Remember normal resizes without recording zoom animation frames."""
        if event.GetEventObject() is self and not self._changing_window_state:
            self._remember_normal_size()
        event.Skip()

    def _on_maximize(self, event: wx.MaximizeEvent) -> None:
        """Preserve normal size throughout native maximize and restore animations."""
        if event.GetEventObject() is self:
            self._remember_normal_size()
            # Cocoa sends this before zooming, while IsMaximized is still false.
            self._changing_window_state = True
            wx.CallAfter(self._finish_window_state_change)
        event.Skip()

    def _finish_window_state_change(self) -> None:
        """Resume size tracking after native zooming returns to the event loop."""
        if not self:
            return
        self._changing_window_state = False
        self._remember_normal_size()

    def update_settings(self, event):
        """Update the settings on change."""
        wx.PostEvent(
            self.parent,
            UpdateSetting(
                section="partselector",
                setting=event.GetEventObject().GetName(),
                value=event.GetEventObject().GetValue(),
            ),
        )

        # initiate a search now that settings have changed
        self.search(None)

    @staticmethod
    def get_existing_selection(parts):
        """Check if exactly one LCSC part number is amongst the selected parts."""
        s = set(parts.values())
        if len(s) != 1:
            return ""
        return list(s)[0]

    def quit_dialog(self, *_: object) -> None:
        """Close this window (via EVT_CLOSE → _on_close → Destroy)."""
        self.Close()

    def _on_close(self, _event: wx.CloseEvent) -> None:
        """Destroy on close and clear the parent's singleton ref."""
        try:
            self.search_timer.Stop()
            if getattr(self, "_layout_ready", False):
                settings = self.parent.settings.setdefault("partselector", {})
                settings["column_widths"] = get_column_widths(
                    self.part_list, PARTSELECTOR_COLUMN_KEYS
                )
                settings["size"] = self._normal_size
                self.parent.save_settings()
        except OSError:
            self.logger.exception("Unable to save window layout")
        finally:
            # Do not leave the parent pointing at a destroyed selector.
            if getattr(self.parent, "_part_selector", None) is self:
                self.parent._part_selector = None
            self.Destroy()

    def update_for(
        self,
        parts: dict[str, str],
        *,
        assignment_context: object = None,
        assignment_label: Optional[str] = None,
    ) -> None:
        """Re-target this open selector at a new set of footprints.

        Called when the user invokes "Select Part" again from the main window
        while the selector is already open. We swap in the new parts, refresh
        the initial search keyword, and re-run the search so the visible list
        reflects what the user just clicked.
        """
        # Searches run synchronously. Clear the old result selection before
        # replacing its target, including when the following search fails.
        self.search_timer.Stop()
        self.part_list.UnselectAll()
        self.part_list_model.RemoveAll()
        self.enable_toolbar_buttons(False)
        self.parts = dict(parts)
        self.assignment_context = assignment_context
        if assignment_label is not None or self.assignment_label is not None:
            self.SetTitle(assignment_label or "JLCPCB Library")
        self.assignment_label = assignment_label
        self.keyword.ChangeValue(self.get_existing_selection(self.parts))
        self.search(None)

    def OnSortPartList(self, e):
        """Set order_by to the clicked column and trigger list refresh."""
        self.parent.library.set_order_by(e.GetColumn())
        self.search(None)

    def OnPartSelected(self, *_):
        """Enable the toolbar buttons when a selection was made."""
        if self.part_list.GetSelectedItemsCount() > 0:
            self.enable_toolbar_buttons(True)
        else:
            self.enable_toolbar_buttons(False)

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side."""
        for b in [
            self.select_part_button,
            self.part_details_button,
        ]:
            b.Enable(bool(state))

    def add_ohm_symbol(self, *_):
        """Append the Ω symbol to the search string."""
        self.keyword.AppendText("Ω")

    def add_micro_symbol(self, *_):
        """Append the µ symbol to the search string."""
        self.keyword.AppendText("µ")

    def search_dwell(self, *_):
        """Initiate a search once the timeout expires.

        Used to avoid continuous searches
        when input fields are still being changed by the user.
        """
        self.search_timer.StartOnce(750)

    def refresh_catalog(self) -> None:
        """Discard stale results and repopulate filters from the selected catalog."""
        self.search_timer.Stop()
        category = self.category.GetValue()
        self.part_list_model.RemoveAll()
        # Clear emits EVT_TEXT on Cocoa, even for an empty read-only combo.
        # Suppress callbacks until both filters contain the final catalog state.
        with wx.EventBlocker(self.category), wx.EventBlocker(self.subcategory):
            self.category.Clear()
            self.category.SetValue("")
            self.subcategory.Clear()
            self.subcategory.SetValue("")
            if not self.parent.is_catalog_available():
                self.result_count.SetLabel(
                    "Parts catalog unavailable; download it to search."
                )
                self._show_typed_lcsc_offer(None)
                return
            categories = self.parent.library.categories
            self.category.AppendItems(categories)
            category = category if category in categories else "All"
            self.category.SetValue(category)
            if category != "All":
                self.subcategory.AppendItems(
                    self.parent.library.get_subcategories(category)
                )
        self.search()

    def search(self, *_: object) -> None:
        """Search the library for parts that meet the search criteria."""
        if not self.parent.is_catalog_available():
            self.part_list_model.RemoveAll()
            self.result_count.SetLabel(
                "Parts catalog unavailable; download it to search."
            )
            self._show_typed_lcsc_offer(None)
            return
        parameters = {
            "keyword": self.keyword.GetValue(),
            "manufacturer": self.manufacturer.GetValue(),
            "package": self.package.GetValue(),
            "category": self.category.GetValue(),
            "subcategory": self.subcategory.GetValue(),
            "part_no": self.part_no.GetValue(),
            "solder_joints": self.solder_joints.GetValue(),
            "basic": self.basic_checkbox.GetValue(),
            "extended": self.extended_checkbox.GetValue(),
            "preferred": self.preferred_checkbox.GetValue(),
            "stock": self.assert_stock_checkbox.GetValue(),
            "quantity": len(self.parts),
        }
        start = time.time()
        try:
            result = self.parent.library.search(parameters)
        except Exception:
            # The offer names the last search's number; a failed search must not
            # leave it up under a new keyword or for new targets.
            self._show_typed_lcsc_offer(None)
            raise
        self.logger.debug("len(result) %d", len(result))
        search_duration = time.time() - start
        self.populate_part_list(result, search_duration)
        self.update_typed_lcsc_offer(result)

    def update_typed_lcsc_offer(self, parts: Any) -> None:
        """Offer the searched number when the results do not show it."""
        shown = {normalize_lcsc(row[_LCSC_FIELD]) for row in parts or ()}
        try:
            offer = typed_lcsc_offer(
                self.keyword.GetValue(),
                shown,
                lambda lcsc: self.parent._catalog_get_part_details(lcsc, strict=True),
            )
        except (sqlite3.Error, OSError) as error:
            self.logger.warning("Unable to look up the searched LCSC number: %s", error)
            offer = None
        self._show_typed_lcsc_offer(offer)

    def _show_typed_lcsc_offer(
        self, offer: Optional[tuple[str, str, dict[str, Any]]]
    ) -> None:
        """Show, relabel or hide the offer, keeping it clear of the result count."""
        previous, self._typed_lcsc_offer = self._typed_lcsc_offer, offer
        if offer is None and previous is None:
            return
        # The result count's text changes width with every search, and a label
        # resizing itself never moves its neighbours, so a visible offer is laid
        # out again even when its own label is unchanged.
        self.typed_lcsc_button.SetLabel(offer[1] if offer else "")
        self.typed_lcsc_button.Show(offer is not None)
        self.Layout()

    def assign_typed_lcsc(self, *_: object) -> None:
        """Assign the offered number to this session's targets, as a pick would."""
        if self._typed_lcsc_offer is None:
            return
        lcsc, _label, details = self._typed_lcsc_offer
        wx.PostEvent(
            self.parent,
            AssignPartsEvent(
                lcsc=lcsc,
                type=details.get("type", ""),
                stock=details.get("stock", ""),
                references=tuple(self.parts),
                assignment_context=self.assignment_context,
            ),
        )
        self.Close()

    def get_highlight_text(self) -> str:
        """Return the active keyword search text for result highlighting."""
        if not self.parent.settings.get("highlighting", {}).get("matches", True):
            return ""
        return self.keyword.GetValue()

    def update_subcategories(self, *_: object) -> None:
        """Update the possible subcategory selection."""
        if not self.parent.is_catalog_available():
            self.refresh_catalog()
            return
        self.subcategory.Clear()
        category = self.category.GetValue()
        if self.category.GetSelection() != wx.NOT_FOUND and category != "All":
            subcategories = self.parent.library.get_subcategories(category)
            self.subcategory.AppendItems(subcategories)

        # search now that categories might have changed
        self.search(None)

    def get_price(self, quantity, prices) -> float:
        """Find the price for the number of selected parts according to the price ranges."""
        return get_unit_price(quantity, prices)

    def populate_part_list(self, parts: Any, search_duration: float) -> None:
        """Populate the list with the result of the search."""
        search_duration_text = _format_duration(search_duration)
        start = time.time()
        self.part_list_model.RemoveAll()
        if parts is None:
            return
        limit_text = " (limited)" if len(parts) >= 1000 else ""
        for p in parts:
            db_row = {field: str(value) for field, value in zip(DB_FIELDS, p)}
            price = round(self.get_price(len(self.parts), db_row.get("Price", "")), 3)
            if price > 0:
                total_cost = round(price * len(self.parts), 3)
                db_row["Price"] = (
                    f"{len(self.parts)} parts: ${price} each / ${total_cost} total"
                )
            else:
                db_row["Price"] = "Error in price data"
            params = params_for_part(
                {
                    "description": db_row.get("Description", ""),
                    "category": db_row.get("First Category", ""),
                    "package": db_row.get("Package", ""),
                }
            )
            item = []
            for column in PARTSELECTOR_COLUMNS:
                if column.key == PARAMS_COLUMN_KEY:
                    item.append(params)
                elif column.db_field:
                    item.append(db_row.get(column.db_field, ""))
                else:
                    item.append("")
            self.part_list_model.AddEntry(item)
        render_duration = time.time() - start
        render_duration_text = _format_duration(render_duration)
        result_count_label = (
            f"{len(parts)} parts {limit_text}."
            f"Search in {search_duration_text}, "
            f"Render in {render_duration_text}."
        )
        self.result_count.SetLabel(result_count_label)

    def select_part(self, *_events: object) -> None:
        """Post the catalog choice with this session's captured target."""
        if self.part_list.GetSelectedItemsCount() > 0:
            item = self.part_list.GetSelection()
            wx.PostEvent(
                self.parent,
                AssignPartsEvent(
                    lcsc=self.part_list_model.get_lcsc(item),
                    type=self.part_list_model.get_type(item),
                    stock=self.part_list_model.get_stock(item),
                    references=tuple(self.parts),
                    assignment_context=self.assignment_context,
                ),
            )
            self.Close()

    def get_part_details(self, *_):
        """Fetch part details from LCSC and show them in a modeless dialog."""
        if self.part_list.GetSelectedItemsCount() > 0:
            item = self.part_list.GetSelection()
            dialog = PartDetailsDialog(self, self.part_list_model.get_lcsc(item))
            dialog.Show()

    def help(self, *_):
        """Show message box with help instructions."""
        title = "Help"
        text = """
        Use % as wildcard selector. \n
        For example DS24% will match DS2411\n
        %QFP% will match LQFP-64 as well as TQFP-32\n
        The keyword search box is automatically post- and prefixed with wildcard operators.
        The others are not by default.\n
        The keyword search field is applied to "LCSC Part", "Description", "MFR.Part",
        "Package" and "Manufacturer".\n
        Searching occurs as input fields are changed.\n
        The results are limited to 1000.
        """
        wx.MessageBox(text, title, style=wx.ICON_INFORMATION)
