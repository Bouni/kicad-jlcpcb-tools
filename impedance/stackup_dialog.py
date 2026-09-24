"""Board-compatible stackup selection with a cached, automatic catalog check."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import wx
import wx.adv

from .catalog_controller import (
    CatalogController,
    CatalogState,
    compatible_stackup as _compatible,
)
from .stackup_construction import StackupConstructionPanel
from .stackup_model import Stackup
from .stackup_text import stackup_definition_text

STACKUPS_URL = "https://jlcpcb.com/impedance"
CALCULATOR_URL = "https://jlcpcb.com/pcb-impedance-calculator"
ICON_LICENSE_URL = "https://fontawesome.com/license/free"
_PRICE_LABELS = {
    "additional": "Additional",
    "none": "Normal",
    "unknown": "—",
}


def _choice_key(stackup: Stackup) -> tuple[object, ...]:
    """Keep actual catalog changes distinct, ignoring refresh-only provenance."""
    return (
        stackup.stackup_id,
        stackup.name,
        stackup.layer_count,
        stackup.thickness_mm,
        stackup.outer_copper_oz,
        stackup.inner_copper_oz,
        stackup.preferred,
        stackup.charge_status,
        stackup.layers,
        stackup.calculator_id,
    )


def _filter_number(value: str, label: str) -> Optional[Decimal]:
    """Accept an explicit positive dimension, or an unrestrictive empty/Any filter."""
    value = value.strip()
    if not value or value.casefold() == "any":
        return None
    if len(value) > 64:
        raise ValueError(f"{label}: enter a positive number or choose Any.")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label}: enter a positive number or choose Any.") from error
    if not number.is_finite() or number <= 0 or abs(number.adjusted()) > 12:
        raise ValueError(f"{label}: enter a positive finite number or choose Any.")
    return number


def _price_label(stackup: Stackup) -> str:
    """Report the vendor surcharge category, never infer price from the flame."""
    return _PRICE_LABELS[stackup.charge_status]


def _copy_text_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy on explicit request, leaving any other clipboard owner untouched."""
    clipboard = wx.TheClipboard
    copied = False
    try:
        if clipboard.IsOpened():
            return False, "Clipboard busy; try again."
        primary_selection = clipboard.IsUsingPrimarySelection()
        try:
            clipboard.UsePrimarySelection(False)
            if not clipboard.Open():
                return False, "Clipboard busy; try again."
            try:
                copied = bool(clipboard.SetData(wx.TextDataObject(text)))
                if copied:
                    with suppress(RuntimeError, AssertionError):
                        # False is normal on macOS. Successful SetData is the
                        # copy result; persistence is best effort on each port.
                        clipboard.Flush()
            finally:
                clipboard.Close()
        finally:
            clipboard.UsePrimarySelection(primary_selection)
    except (RuntimeError, AssertionError):
        if not copied:
            return False, "Could not copy; try again."
    return (True, "Copied.") if copied else (False, "Could not copy; try again.")


class StackupDialog(wx.Dialog):
    """Select an immutable stackup from the main dialog's shared catalog."""

    def __init__(
        self,
        parent: wx.Window,
        layer_count: int,
        current: Optional[Stackup] = None,
        *,
        catalog_controller: CatalogController,
        load_copper_colors: Optional[
            Callable[[], dict[str, tuple[int, int, int, int]]]
        ] = None,
    ) -> None:
        if type(layer_count) is not int or not 2 <= layer_count <= 64:
            raise ValueError("Choose a board with 2–64 enabled copper layers.")
        if catalog_controller.layer_count != layer_count:
            raise ValueError(
                "The shared catalog does not match this board's copper-layer count."
            )
        super().__init__(
            parent,
            title="Select JLCPCB stackup",
            size=(1160, 720),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.stackup: Optional[Stackup] = None
        self._catalog = catalog_controller.state.cache.stackups
        self._current = current
        self._selected = (
            current if current and _compatible(current, layer_count) else None
        )
        self._displayed_stackup: Optional[Stackup] = None
        self._layer_count = layer_count
        self._catalog_unsubscribe: Optional[Callable[[], None]] = None
        self._shown: tuple[Stackup, ...] = ()
        self._closed = False
        self._populating = False
        self._notice = ""
        self._notice_error = False
        self._selection_notice = ""
        self._status_text = ""
        self._fire_index = -1

        root = wx.BoxSizer(wx.VERTICAL)
        title_row = wx.BoxSizer(wx.HORIZONTAL)
        layer_label = wx.StaticText(
            self,
            label=f"{layer_count}-layer PCB · Rigid FR4 stackups",
        )
        title_row.Add(layer_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        root.Add(title_row, 0, wx.ALL | wx.EXPAND, 12)

        filters = wx.BoxSizer(wx.HORIZONTAL)
        self.thickness = self._add_filter(filters, "Thickness (mm)")
        self.outer_copper = self._add_filter(filters, "Outer copper (oz)")
        self.inner_copper = self._add_filter(filters, "Inner copper (oz)")
        if layer_count == 2:
            self.inner_copper.Disable()
        filters.AddStretchSpacer()
        root.Add(filters, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        content = wx.BoxSizer(wx.HORIZONTAL)
        self.choices = wx.ListCtrl(
            self,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_HRULES,
        )
        for index, (label, width) in enumerate(
            (
                ("", 36),  # Reserved flame gutter, separate from the Price text.
                ("Price", 90),
                ("Stackup", 175),
                ("mm", 60),
                ("Outer oz", 65),
                ("Inner oz", 65),
                ("Calculator", 105),
            )
        ):
            self.choices.InsertColumn(index, label, width=self.choices.FromDIP(width))
        price_width = max(
            self.choices.GetTextExtent(label).width
            for label in ("Price", "Normal", "Additional", "—")
        ) + self.choices.FromDIP(20)
        self.choices.SetColumnWidth(1, max(self.choices.FromDIP(90), price_width))
        self.choices.SetToolTip(
            "Flame: preferred by JLCPCB. Price: surcharge category, not a quoted amount. —: unavailable."
        )
        self._install_fire_icon()
        self.choices.Bind(wx.EVT_LIST_COL_BEGIN_DRAG, self._on_column_begin_drag)
        self.choices.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selection)
        self.choices.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_deselection)
        self.choices.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_accept)
        self.choices.SetMinSize(self.FromDIP(wx.Size(550, 280)))
        content.Add(self.choices, 5, wx.RIGHT | wx.EXPAND, 12)

        detail_panel = wx.Panel(self)
        detail_layout = wx.BoxSizer(wx.VERTICAL)
        copper_colors: Optional[dict[str, tuple[int, int, int, int]]] = None
        palette_notice = ""
        if load_copper_colors is not None:
            try:
                copper_colors = load_copper_colors()
            except Exception as failure:
                palette_notice = str(failure)[:500] or "KiCad palette unavailable."
        self.details = StackupConstructionPanel(
            detail_panel,
            copper_colors=copper_colors,
            palette_notice=palette_notice,
        )
        detail_layout.Add(self.details, 1, wx.EXPAND)
        copy_row = wx.BoxSizer(wx.HORIZONTAL)
        self.copy_button = wx.Button(detail_panel, label="Copy definition")
        self.copy_button.SetToolTip(
            "Copy the complete definition of the displayed stackup as text."
        )
        self.copy_button.Disable()
        self.copy_button.Bind(wx.EVT_BUTTON, self._on_copy_definition)
        copy_row.Add(
            self.copy_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8)
        )
        self.copy_status = wx.StaticText(
            detail_panel, style=wx.ST_ELLIPSIZE_END | wx.ST_NO_AUTORESIZE
        )
        self.copy_status.SetMinSize((1, self.copy_status.GetCharHeight()))
        copy_row.Add(self.copy_status, 1, wx.ALIGN_CENTER_VERTICAL)
        detail_layout.Add(copy_row, 0, wx.EXPAND | wx.TOP, self.FromDIP(8))
        detail_panel.SetSizer(detail_layout)
        detail_panel.SetMinSize(self.FromDIP(wx.Size(370, 280)))
        content.Add(detail_panel, 4, wx.EXPAND)
        root.Add(content, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)

        self.status = wx.StaticText(
            self, style=wx.ST_ELLIPSIZE_END | wx.ST_NO_AUTORESIZE
        )
        self.status.SetMinSize((1, self.status.GetCharHeight() + self.FromDIP(8)))
        root.Add(self.status, 0, wx.ALL | wx.EXPAND, 12)
        links = wx.BoxSizer(wx.HORIZONTAL)
        for label, url in (
            ("JLCPCB stackups", STACKUPS_URL),
            ("JLCPCB impedance calculator", CALCULATOR_URL),
            ("Icon: Font Awesome (CC BY 4.0)", ICON_LICENSE_URL),
        ):
            link = wx.adv.HyperlinkCtrl(self, wx.ID_ANY, label, url)
            links.Add(link, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 20)
        root.Add(links, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        root.Add(wx.StaticLine(self), 0, wx.EXPAND)
        buttons = wx.StdDialogButtonSizer()
        self.accept_button = wx.Button(self, wx.ID_OK, "Use stackup")
        self.accept_button.Bind(wx.EVT_BUTTON, self._on_accept)
        cancel_button = wx.Button(self, wx.ID_CANCEL)
        cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        buttons.AddButton(self.accept_button)
        buttons.AddButton(cancel_button)
        buttons.Realize()
        root.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        self.SetSizer(root)
        self.SetMinSize(self.FromDIP(wx.Size(1040, 560)))
        self.SetSize(self.FromDIP(wx.Size(1160, 720)))
        self.Bind(wx.EVT_CLOSE, self._on_cancel)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        if current and self._selected is None:
            self._notice = "The saved stackup has a different copper-layer count and cannot be used."
            self._notice_error = True
            self._selection_notice = self._notice
        self._catalog_unsubscribe = catalog_controller.subscribe(self._on_catalog_state)
        self.CentreOnParent()

    def _add_filter(self, layout: wx.BoxSizer, label: str) -> wx.ComboBox:
        """Build one locally applied editable dimension filter."""
        layout.Add(
            wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6
        )
        control = wx.ComboBox(self, value="Any", choices=["Any"], style=wx.CB_DROPDOWN)
        control.SetMinSize((95, -1))
        control.Bind(wx.EVT_TEXT, self._on_filter)
        control.Bind(wx.EVT_COMBOBOX, self._on_filter)
        layout.Add(control, 0, wx.RIGHT, 16)
        return control

    def _install_fire_icon(self) -> None:
        """Install the CC BY flame; construction details retain textual preference."""
        try:
            import wx.svg

            source = Path(__file__).parent / "resources" / "preferred-fire.svg"
            svg = wx.svg.SVGimage.CreateFromFile(str(source))
            size = self.choices.FromDIP(wx.Size(18, 18))
            bitmap = svg.ConvertToScaledBitmap(size)
            if not bitmap.IsOk():
                return
            images = wx.ImageList(size.width, size.height)
            self._fire_index = images.Add(bitmap)
            self.choices.AssignImageList(images, wx.IMAGE_LIST_SMALL)
        except (ImportError, AttributeError, OSError, RuntimeError, ValueError):
            self._fire_index = -1

    def _on_column_begin_drag(self, event: wx.ListEvent) -> None:
        """Keep the icon gutter wide enough while leaving text columns resizable."""
        if event.GetColumn() == 0:
            event.Veto()
        else:
            event.Skip()

    def _available(self) -> tuple[Stackup, ...]:
        """Keep a saved snapshot selectable when its catalog entry changes or disappears."""
        values = {_choice_key(item): item for item in self._catalog}
        if self._current and _compatible(self._current, self._layer_count):
            values[_choice_key(self._current)] = self._current
        return tuple(
            sorted(
                values.values(),
                key=lambda item: (
                    not (item.preferred and item.calculator_id),
                    item.name.casefold(),
                    Decimal(item.thickness_mm),
                    Decimal(item.outer_copper_oz),
                    Decimal(item.inner_copper_oz or "0"),
                    item.stackup_id,
                ),
            )
        )

    def _update_filter_choices(self) -> None:
        """Offer catalog values without silently changing entered filter text."""
        self._populating = True
        try:
            available = self._available()
            for control, attribute in (
                (self.thickness, "thickness_mm"),
                (self.outer_copper, "outer_copper_oz"),
                (self.inner_copper, "inner_copper_oz"),
            ):
                old = control.GetValue()
                values = {getattr(item, attribute) for item in available}
                control.SetItems(
                    ["Any"] + sorted((value for value in values if value), key=Decimal)
                )
                control.SetValue(old)
        finally:
            self._populating = False

    def _populate(self) -> None:
        """Apply exact filters without ever selecting the first result implicitly."""
        filters: list[tuple[str, Optional[Decimal]]] = []
        error = ""
        for control, attribute, label in (
            (self.thickness, "thickness_mm", "Thickness"),
            (self.outer_copper, "outer_copper_oz", "Outer copper"),
            (self.inner_copper, "inner_copper_oz", "Inner copper"),
        ):
            try:
                value = _filter_number(control.GetValue(), label)
                control.SetForegroundColour(
                    wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
                )
                filters.append((attribute, value))
            except ValueError as failure:
                control.SetForegroundColour(wx.Colour(195, 35, 35))
                error = str(failure)
            control.Refresh()
        available = self._available()
        self._shown = (
            ()
            if error
            else tuple(
                item
                for item in available
                if all(
                    value is None
                    or (
                        getattr(item, attribute)
                        and Decimal(getattr(item, attribute)) == value
                    )
                    for attribute, value in filters
                )
            )
        )
        selected_key = _choice_key(self._selected) if self._selected else None
        selected_index = -1
        self._populating = True
        self.choices.Freeze()
        try:
            # Header-divider double-click autosizing bypasses the drag veto.
            # Restore room for icons when empty/filtered rows become available.
            self.choices.SetColumnWidth(0, self.choices.FromDIP(36))
            self.choices.DeleteAllItems()
            for index, item in enumerate(self._shown):
                image_index = (
                    self._fire_index if item.preferred and item.calculator_id else -1
                )
                # Never put an image and text in the same cell: the dedicated
                # gutter keeps native image sizing independent of label layout.
                self.choices.InsertItem(index, "", image_index)
                name = item.name + (" (saved)" if item is self._current else "")
                for column, value in enumerate(
                    (
                        _price_label(item),
                        name,
                        item.thickness_mm,
                        item.outer_copper_oz,
                        item.inner_copper_oz or "N/A",
                        "Available" if item.calculator_id else "Unavailable",
                    ),
                    start=1,
                ):
                    self.choices.SetItem(index, column, value)
                if _choice_key(item) == selected_key:
                    selected_index = index
            if selected_index >= 0:
                self.choices.SetItemState(
                    selected_index, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED
                )
                self.choices.EnsureVisible(selected_index)
        finally:
            self.choices.Thaw()
            self._populating = False
        selected = self._shown[selected_index] if selected_index >= 0 else None
        self._show_selected_stackup(selected)
        if error:
            self._set_status(
                (self._notice + "\n" if self._notice else "") + error,
                error=True,
            )
        elif not available:
            self._set_status(
                self._notice
                or "No compatible stackups are cached. The catalog will be checked automatically.",
                error=self._notice_error,
            )
        elif not self._shown:
            self._set_status(
                (self._notice + "\n" if self._notice else "")
                + "No compatible stackups match these filters. Choose Any to broaden thickness or copper weight.",
                error=self._notice_error,
            )
        else:
            count = f"{len(self._shown)} compatible stackups."
            self._set_status(
                (self._notice + "\n" if self._notice else "") + count,
                error=self._notice_error,
            )

    def _set_status(self, message: str, *, error: bool = False) -> None:
        """Keep one status line; preserve full diagnostics in its label and tooltip."""
        self._status_text = message
        self.status.SetLabel(" ".join(message.split()))
        self.status.SetToolTip(message)
        self.status.SetForegroundColour(
            wx.Colour(195, 35, 35)
            if error
            else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        )
        self.Layout()

    def _on_filter(self, event: wx.CommandEvent) -> None:
        """Change only visible candidates; never issue HTTP for typing a filter."""
        if not self._populating:
            self._populate()

    def _on_selection(self, event: wx.ListEvent) -> None:
        """Require an actual selected row and preview exactly that immutable snapshot."""
        if self._populating:
            return
        index = event.GetIndex()
        if (
            not 0 <= index < len(self._shown)
            or self.choices.GetFirstSelected() != index
        ):
            return
        self._selected = self._shown[index]
        self._show_selected_stackup(self._selected)

    def _on_deselection(self, event: wx.ListEvent) -> None:
        """Disable acceptance and copying when the list selection is cleared."""
        if not self._populating:
            index = self.choices.GetFirstSelected()
            selected = self._shown[index] if 0 <= index < len(self._shown) else None
            self._show_selected_stackup(selected)

    def _show_selected_stackup(self, stackup: Optional[Stackup]) -> None:
        """Keep the displayed snapshot and both selection actions in agreement."""
        if stackup is not None and not _compatible(stackup, self._layer_count):
            stackup = None
        self._displayed_stackup = stackup
        self.details.set_stackup(
            stackup, saved=stackup is self._current and stackup is not None
        )
        self.accept_button.Enable(stackup is not None)
        self.copy_button.Enable(stackup is not None)
        self._set_copy_status("")

    def _set_copy_status(self, message: str, *, error: bool = False) -> None:
        """Keep clipboard feedback beside its button without adding instructions."""
        self.copy_status.SetLabel(message)
        self.copy_status.SetToolTip(message)
        self.copy_status.SetForegroundColour(
            wx.Colour(195, 35, 35)
            if error
            else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        )

    def _on_copy_definition(self, event: wx.CommandEvent) -> None:
        """Copy only the visibly selected snapshot, never a newer record by ID."""
        if self._closed or self._populating:
            return
        index = self.choices.GetFirstSelected()
        candidate = self._shown[index] if 0 <= index < len(self._shown) else None
        if (
            candidate is None
            or candidate is not self._displayed_stackup
            or not _compatible(candidate, self._layer_count)
        ):
            self._set_copy_status("Select a stackup to copy.", error=True)
            return
        text = stackup_definition_text(candidate, saved=candidate is self._current)
        copied, message = _copy_text_to_clipboard(text)
        self._set_copy_status(message, error=not copied)

    def _on_catalog_state(self, state: CatalogState) -> None:
        """Incorporate shared choices without changing saved or deliberate selection."""
        if self._closed or not self:
            return
        self._catalog = state.cache.stackups
        self._notice = (
            self._selection_notice + "\n" if self._selection_notice else ""
        ) + state.message
        self._notice_error = bool(self._selection_notice) or state.error
        self._update_filter_choices()
        self._populate()

    def _detach_catalog(self) -> None:
        """Detach the picker without cancelling the main dialog's catalog check."""
        if self._catalog_unsubscribe is not None:
            self._catalog_unsubscribe()
            self._catalog_unsubscribe = None

    def _on_accept(self, event: wx.Event) -> None:
        """Commit a deliberate visible selection to the caller, not to the database."""
        index = self.choices.GetFirstSelected()
        if not 0 <= index < len(self._shown):
            self._set_status(
                "Select a compatible stackup before continuing.", error=True
            )
            return
        candidate = self._shown[index]
        if not _compatible(candidate, self._layer_count):
            self._set_status(
                "This stackup does not match the board's enabled copper layers.",
                error=True,
            )
            return
        self.stackup = candidate
        self._detach_catalog()
        self._closed = True
        self.EndModal(wx.ID_OK)

    def _on_cancel(self, event: wx.Event) -> None:
        """Discard board-selection edits; a successfully cached public catalog remains."""
        self._detach_catalog()
        self._closed = True
        self.stackup = None
        self.EndModal(wx.ID_CANCEL)

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        """Also detach if the parent destroys this dialog without ending its modal."""
        if event.GetEventObject() is self:
            self._closed = True
            self._detach_catalog()
        event.Skip()
