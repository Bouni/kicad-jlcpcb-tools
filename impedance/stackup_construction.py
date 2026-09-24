"""Readable, schematic stackup construction using native, single-line controls.

The diagram is deliberately not a thickness plot: every physical copper layer
stays visible, and every vendor dielectric row keeps its own label and values.
No image files, board rendering, or electrical assumptions are involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

import wx

from .stackup_model import Stackup

RGBA = tuple[int, int, int, int]
_MISSING = "—"


def _single_line(value: str) -> str:
    """Keep provider names from introducing newlines into compact controls."""
    return " ".join(value.split())


def construction_number(value: str) -> str:
    """Show exact decimal dimensions without insignificant trailing zeroes."""
    if not value:
        return _MISSING
    try:
        number = Decimal(value)
    except InvalidOperation:
        return _MISSING
    if not number.is_finite() or number <= 0:
        return _MISSING
    # Accepted model values are bounded; remain safe for incomplete UI inputs.
    if abs(number.adjusted()) > 30:
        return _single_line(value)
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


@dataclass(frozen=True)
class ConstructionRow:
    """One displayed physical layer, without native-control dependencies."""

    label: str
    kind: str
    thickness_mm: str
    dielectric_constant: str
    tooltip: str


def construction_rows(stackup: Stackup) -> tuple[ConstructionRow, ...]:
    """Preserve provider order and map only actual copper rows to KiCad names."""
    rows = []
    copper_index = 0
    for index, layer in enumerate(stackup.layers, start=1):
        if layer.kind == "copper":
            copper_index += 1
            label = (
                "F.Cu"
                if copper_index == 1
                else "B.Cu"
                if copper_index == stackup.layer_count
                else f"In{copper_index - 1}.Cu"
            )
        elif layer.kind == "core":
            label = "Core"
        elif layer.kind == "prepreg":
            label = "Prepreg"
        elif layer.kind == "soldermask":
            label = "Solder mask"
        elif "bare board" in (layer.name + " " + layer.material).casefold():
            label = "Bare board"
        else:
            label = "Dielectric" if layer.kind == "dielectric" else "Other"
        thickness = construction_number(layer.thickness_mm)
        dk = construction_number(layer.dielectric_constant)
        description = [f"Layer {index}: {label}"]
        for value in (layer.name, layer.material):
            value = _single_line(value)
            if value and value not in description:
                description.append(value)
        description.extend((f"Thickness: {thickness} mm", f"Dk: {dk}"))
        rows.append(
            ConstructionRow(label, layer.kind, thickness, dk, " · ".join(description))
        )
    return tuple(rows)


def _valid_color(value: object) -> bool:
    """Reject partial or malformed palette entries without inventing colors."""
    return (
        isinstance(value, tuple)
        and len(value) == 4
        and all(type(channel) is int and 0 <= channel <= 255 for channel in value)
    )


def _blend(background: wx.Colour, foreground: wx.Colour, amount: float) -> wx.Colour:
    """Derive neutral, theme-aware shades or explicitly composite palette alpha."""
    return wx.Colour(
        *(
            round(background[index] * (1.0 - amount) + foreground[index] * amount)
            for index in range(3)
        )
    )


class _LayerBand(wx.Panel):
    """A resizable vector band; adjacent row bands meet without raster scaling."""

    def __init__(
        self,
        parent: wx.Window,
        row: ConstructionRow,
        color: Optional[RGBA],
    ) -> None:
        super().__init__(parent)
        self._row = row
        self._color = color
        self.SetName(f"{row.label} construction band")
        self.SetToolTip(row.tooltip)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(self.FromDIP(wx.Size(48, 1)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._on_color_change)

    def _on_paint(self, event: wx.PaintEvent) -> None:
        """Draw at current native resolution, retaining all user color channels."""
        del event
        dc = wx.AutoBufferedPaintDC(self)
        background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        foreground = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        dc.SetBackground(wx.Brush(background))
        dc.Clear()
        if self._color is not None:
            fill = _blend(background, wx.Colour(*self._color[:3]), self._color[3] / 255)
        else:
            amount = {
                "copper": 0.38,
                "core": 0.16,
                "prepreg": 0.06,
                "soldermask": 0.24,
            }.get(self._row.kind, 0.22)
            fill = _blend(background, foreground, amount)
        size = self.GetClientSize()
        if size.width < 1 or size.height < 1:
            return
        dc.SetPen(wx.Pen(_blend(background, foreground, 0.35), max(1, self.FromDIP(1))))
        dc.SetBrush(wx.Brush(fill))
        dc.DrawRectangle(0, 0, size.width, size.height)

    def _on_size(self, event: wx.SizeEvent) -> None:
        self.Refresh(False)
        event.Skip()

    def _on_color_change(self, event: wx.SysColourChangedEvent) -> None:
        self.Refresh(False)
        event.Skip()


class StackupConstructionPanel(wx.Panel):
    """A compact cross-section with readable rows and vertical-only scrolling."""

    def __init__(
        self,
        parent: wx.Window,
        *,
        copper_colors: Optional[Mapping[str, RGBA]] = None,
        palette_notice: str = "",
    ) -> None:
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self._colors = {
            name: color
            for name, color in (copper_colors or {}).items()
            if _valid_color(color)
        }
        self._palette_notice = _single_line(palette_notice)
        self._stackup: Optional[Stackup] = None
        self._saved = False
        self._fitting = False
        self._presentation_key: Optional[tuple[object, ...]] = None
        self.SetName("Layer construction")
        self.SetMinSize(self.FromDIP(wx.Size(370, 280)))

        layout = wx.BoxSizer(wx.VERTICAL)
        self._title = self._label(self, "Select a stackup")
        title_font = self._title.GetFont()
        title_font.SetWeight(wx.FONTWEIGHT_BOLD)
        self._title.SetFont(title_font)
        layout.Add(self._title, 0, wx.EXPAND | wx.ALL, self.FromDIP(6))
        self._summary = self._label(self, "")
        layout.Add(
            self._summary,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            self.FromDIP(6),
        )
        self._selection_status = self._label(self, "")
        layout.Add(
            self._selection_status,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            self.FromDIP(6),
        )
        layout.Add(wx.StaticLine(self), 0, wx.EXPAND)

        self._viewport = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.BORDER_NONE)
        self._viewport.SetName("Stackup layers, top to bottom")
        self._viewport.SetScrollRate(0, self.FromDIP(24))
        self._viewport.SetMinSize((1, 1))
        self._viewport.Bind(wx.EVT_SIZE, self._on_viewport_size)
        self._rows_layout = wx.BoxSizer(wx.VERTICAL)
        self._viewport.SetSizer(self._rows_layout)
        layout.Add(self._viewport, 1, wx.EXPAND | wx.ALL, self.FromDIP(6))

        layout.Add(wx.StaticLine(self), 0, wx.EXPAND)
        self._schematic = self._label(self, "Top → bottom · Schematic, not to scale")
        layout.Add(self._schematic, 0, wx.EXPAND | wx.ALL, self.FromDIP(6))
        self._palette_status = self._label(self, "")
        layout.Add(
            self._palette_status,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            self.FromDIP(6),
        )
        self.SetSizer(layout)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_DPI_CHANGED, self._on_dpi_change)
        self.set_stackup(None)

    @staticmethod
    def _label(parent: wx.Window, text: str, *, right: bool = False) -> wx.StaticText:
        """Use native accessible labels that never wrap or enlarge their columns."""
        label = wx.StaticText(
            parent,
            label=_single_line(text),
            style=wx.ST_ELLIPSIZE_END
            | wx.ST_NO_AUTORESIZE
            | (wx.ALIGN_RIGHT if right else 0),
        )
        label.SetMinSize((1, -1))
        label.SetToolTip(_single_line(text))
        return label

    def _set_label(self, label: wx.StaticText, text: str, *, tooltip: str = "") -> None:
        """Keep visible text single-line and expose its full value on hover."""
        label.SetLabel(_single_line(text))
        label.SetToolTip(_single_line(tooltip or text))

    def _add_row(self, row: Optional[ConstructionRow] = None) -> None:
        """Give header and physical rows identical, bounded column geometry."""
        panel = wx.Panel(self._viewport)
        layout = wx.BoxSizer(wx.HORIZONTAL)
        label = self._label(panel, row.label if row else "Layer")
        thickness = self._label(panel, row.thickness_mm if row else "mm", right=True)
        dk = self._label(panel, row.dielectric_constant if row else "Dk", right=True)
        label.SetMinSize(self.FromDIP(wx.Size(100, -1)))
        thickness.SetMinSize(self.FromDIP(wx.Size(72, -1)))
        dk.SetMinSize(self.FromDIP(wx.Size(46, -1)))
        if row is None:
            band: wx.Window = self._label(panel, "Construction")
        else:
            color = self._colors.get(row.label) if row.kind == "copper" else None
            band = _LayerBand(panel, row, color)
            for control in (label, thickness, dk):
                control.SetToolTip(row.tooltip)
        layout.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        layout.Add(band, 1, wx.EXPAND if row is not None else wx.ALIGN_CENTER_VERTICAL)
        layout.Add(thickness, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(8))
        layout.Add(dk, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(8))
        panel.SetSizer(layout)
        row_height = 20 if row is not None and row.kind == "copper" else 24
        panel.SetMinSize(
            (
                1,
                max(
                    self.FromDIP(row_height),
                    label.GetBestSize().height + self.FromDIP(6),
                ),
            )
        )
        self._rows_layout.Add(panel, 0, wx.EXPAND)

    def set_stackup(self, stackup: Optional[Stackup], *, saved: bool = False) -> None:
        """Replace only this presentation, never the saved board or catalog data."""
        self._stackup = stackup
        self._saved = saved
        presentation_key = (
            ()
            if stackup is None
            else (
                stackup.name,
                stackup.layer_count,
                stackup.thickness_mm,
                stackup.preferred,
                bool(stackup.calculator_id),
                stackup.layers,
                saved,
            )
        )
        if presentation_key == self._presentation_key:
            return
        self.Freeze()
        try:
            self._rows_layout.Clear(delete_windows=True)
            self._viewport.Scroll(0, 0)
            if stackup is None:
                self._set_label(self._title, "Select a stackup")
                self._set_label(self._summary, "Layer construction")
                self._set_label(self._selection_status, "")
                empty = self._label(self._viewport, "No stackup selected")
                self._rows_layout.Add(empty, 0, wx.EXPAND | wx.ALL, self.FromDIP(8))
                rows: tuple[ConstructionRow, ...] = ()
            else:
                self._set_label(self._title, stackup.name)
                summary = f"{stackup.layer_count} copper layers · {construction_number(stackup.thickness_mm)} mm finished"
                self._set_label(self._summary, summary)
                preference = (
                    "Preference unknown"
                    if not stackup.calculator_id
                    else "Preferred"
                    if stackup.preferred
                    else "Not preferred"
                )
                self._set_label(
                    self._selection_status,
                    preference + (" · Saved selection" if saved else ""),
                )
                rows = construction_rows(stackup)
                if rows:
                    self._add_row()
                    for row in rows:
                        self._add_row(row)
                else:
                    empty = self._label(self._viewport, "Construction unavailable")
                    self._rows_layout.Add(empty, 0, wx.EXPAND | wx.ALL, self.FromDIP(8))
            missing_colors = any(
                row.kind == "copper" and row.label not in self._colors for row in rows
            )
            if missing_colors:
                self._set_label(
                    self._palette_status,
                    "Unavailable copper colors shown neutral",
                    tooltip=self._palette_notice
                    or "KiCad palette colors are unavailable for one or more copper layers.",
                )
            else:
                self._set_label(self._palette_status, "")
            self._palette_status.Show(missing_colors)
            self._selection_status.Show(stackup is not None)
            self.Layout()
            self._fit_scroll_area()
            self._presentation_key = presentation_key
        finally:
            self.Thaw()

    def _fit_scroll_area(self) -> None:
        """Allow vertical overflow without letting long labels widen the diagram."""
        if self._fitting:
            return
        self._fitting = True
        try:
            height = self._rows_layout.GetMinSize().height
            self._viewport.SetVirtualSize(
                (self._viewport.GetClientSize().width, height)
            )
            self._viewport.Layout()
        finally:
            self._fitting = False

    def _on_size(self, event: wx.SizeEvent) -> None:
        self.Layout()
        self._fit_scroll_area()
        event.Skip()

    def _on_viewport_size(self, event: wx.SizeEvent) -> None:
        """Refit when a vertical scrollbar changes the usable column width."""
        self._fit_scroll_area()
        event.Skip()

    def _on_dpi_change(self, event: wx.DPIChangedEvent) -> None:
        """Rebuild fixed row dimensions using the monitor's current DIP scaling."""
        _, scroll_y = self._viewport.GetViewStart()
        self.SetMinSize(self.FromDIP(wx.Size(370, 280)))
        self._viewport.SetScrollRate(0, self.FromDIP(24))
        for item in self.GetSizer().GetChildren():
            if item.GetBorder():
                item.SetBorder(self.FromDIP(6))
        self._presentation_key = None
        self.set_stackup(self._stackup, saved=self._saved)
        self._viewport.Scroll(0, scroll_y)
        event.Skip()
