"""Native, frozen-column comparison grid with explicit variant edit targets.

The view only emits captured targets. Board writes, clipboard validation and
assignment sessions belong to the dialog coordinator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Any, Optional

import wx
import wx.grid as gridlib

from ..dataview_highlight import expand_footprint, expand_value, find_highlight_spans
from ..helpers import loadBitmapScaled
from .text_layout import (
    FittedText,
    fit_text_suffix,
    percentile_text_width,
    visible_highlight_spans,
)

FIXED_COLUMNS = 5
FLAG_FIELDS = frozenset(("bom", "pos", "pop"))
_COMPACT_FIELDS = FLAG_FIELDS | {
    "type",
    "standard",
    "side",
    "price",
    "stock",
}
_VERTICAL_HEADERS = _COMPACT_FIELDS | {"pcb_angle", "correction"}
_SIDE_HEADER_SCALE = 0.8
_SIDE_PADDING_DIP = 6
_VALUE_LEFT_PADDING_DIP = 2
_FLAG_WIDTH_SCALE = 0.9
_READ_ONLY_STATUS_FIELDS = frozenset(("type", "price", "standard", "stock"))
_STATUS_WIDTH_KEY = "information"
_STATUS_WIDTH_SCALE = 0.765
# Bound verbose fields in font units; compact fields fit their complete content.
_TEXT_WIDTH_EM = {"correction": 24}
_INDEPENDENT_TEXT_FIELDS = frozenset(("value", "params", "lcsc"))
_PERCENTILE_TEXT_FIELDS = frozenset(("footprint", "value", "params"))
_TEXT_CACHE_LIMIT = 4096


@dataclass(frozen=True)
class MatrixTarget:
    """A captured cell identity; ``variant=None`` is physical identity only."""

    component_id: str
    variant: Optional[str]
    field: str
    reference: str = ""
    display_name: str = ""

    @property
    def label(self) -> str:
        """Describe the component and unambiguous native variant label."""
        variant = (
            "Physical footprint"
            if self.variant is None
            else self.display_name or self.variant or "Default"
        )
        return f"{self.reference or self.component_id} · {variant} · {self.field}"


@dataclass(frozen=True)
class ViewState:
    """Transient selection and scroll; persisted layout preferences are separate."""

    target: Optional[MatrixTarget] = None
    scroll: tuple[int, int] = (0, 0)
    components: tuple[str, ...] = ()


def target_at(model: Any, row: int, col: int) -> Optional[MatrixTarget]:
    """Resolve display coordinates immediately, never when a later event arrives."""
    if not (0 <= row < len(model.rows) and 0 <= col < len(model.columns)):
        return None
    item, column = model.rows[row], model.columns[col]
    label = model.variant_label(column.variant) if column.variant is not None else ""
    return MatrixTarget(
        item.component_id, column.variant, column.key, item.reference, label
    )


def coordinates_for(model: Any, target: MatrixTarget) -> Optional[tuple[int, int]]:
    """Find a saved identity after complete-row sorting, filtering, or refresh."""
    row = model.row_for_component(target.component_id)
    try:
        col = model.column_for(target.variant, target.field)
    except StopIteration:
        return None
    return (row, col) if row is not None else None


def _blend(
    background: wx.Colour, tint: tuple[int, int, int], weight: float
) -> wx.Colour:
    return wx.Colour(
        *(
            round(background[index] * (1 - weight) + tint[index] * weight)
            for index in range(3)
        )
    )


def _price_foreground(background: wx.Colour, comparison: Any) -> wx.Colour:
    """Increase red saturation with the premium while retaining theme contrast."""
    dark = sum(background[index] for index in range(3)) < 3 * 128
    neutral = wx.Colour(170, 170, 170) if dark else wx.Colour(128, 128, 128)
    if comparison.direction == "cheapest":
        return wx.Colour(80, 200, 115) if dark else wx.Colour(25, 128, 65)
    if comparison.direction == "higher":
        ratio = comparison.intensity_ratio
        intensity = 1.0 if ratio == float("inf") else ratio / (1 + ratio)
        red = (255, 80, 80) if dark else (205, 35, 35)
        return _blend(neutral, red, 0.4 + 0.6 * intensity)
    return neutral


def _comparison_accent(background: wx.Colour) -> wx.Colour:
    """Keep difference markers and parameter matches readable on either theme."""
    if sum(background[index] for index in range(3)) < 3 * 128:
        return wx.Colour(255, 210, 110)
    return wx.Colour(145, 85, 0)


def _luminance(colour: wx.Colour) -> float:
    """Measure the background brightness seen by a small status symbol."""
    channels = [colour[index] / 255 for index in range(3)]
    return sum(
        weight
        * (value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
        for weight, value in zip((0.2126, 0.7152, 0.0722), channels)
    )


def _cross_foreground(background: wx.Colour) -> wx.Colour:
    """Keep the red cross distinct from the actual stripe, amber, or selection fill."""
    base_luminance = _luminance(background)

    def contrast(colour: wx.Colour) -> float:
        low, high = sorted((base_luminance, _luminance(colour)))
        return (high + 0.05) / (low + 0.05)

    colour = max((wx.Colour(255, 155, 155), wx.Colour(170, 20, 20)), key=contrast)
    if contrast(colour) >= 3:
        return colour
    # Intermediate custom themes may need a lighter or deeper red. Preserve
    # the hue while increasing contrast instead of assuming a black/white fill.
    target = (255, 255, 255) if _luminance(colour) > base_luminance else (0, 0, 0)
    for step in range(1, 11):
        adjusted = _blend(colour, target, step / 10)
        if contrast(adjusted) >= 3:
            return adjusted
    return adjusted


def _status_column_width(width: int, padding: int) -> int:
    """Size the information group relative to native content and font metrics."""
    previous = max(round(width * 0.9), width - padding // 2)
    return max(1, round(previous * _STATUS_WIDTH_SCALE))


def _width_key(column: Any) -> str:
    """Persist independently fitted text using an unambiguous variant identity."""
    if column.key in _READ_ONLY_STATUS_FIELDS:
        return _STATUS_WIDTH_KEY
    if column.variant is not None and column.key in _INDEPENDENT_TEXT_FIELDS:
        return json.dumps([column.variant, column.key], ensure_ascii=False)
    return column.key


class MatrixGridTable(gridlib.GridTableBase):
    """Lazy wx bridge: painting asks the pure model only for visible cells."""

    def __init__(self, model: Any) -> None:
        super().__init__()
        self.model = model
        self.measuring_cells = False

    def GetNumberRows(self) -> int:
        """Return the physical row count."""
        return len(self.model.rows)

    def GetNumberCols(self) -> int:
        """Return shared columns followed by complete variant groups."""
        return len(self.model.columns)

    def GetValue(self, row: int, col: int) -> str:
        """Resolve display values lazily."""
        return self.model.get_display(row, col)

    def SetValue(self, row: int, col: int, value: str) -> None:
        """Reject inline writes bypassing the edit coordinator."""
        raise RuntimeError("Matrix mutations require a captured variant target")

    def GetColLabelValue(self, col: int) -> str:
        """Expose full variant identity for accessibility."""
        if self.measuring_cells:
            # Our mixed, two-tier headers have their own geometry. Native
            # autosizing otherwise counts the full accessible label horizontally.
            return ""
        column = self.model.columns[col]
        if column.variant is None:
            return column.label
        return f"{self.model.variant_label(column.variant)} · {column.label}"


class MatrixCellRenderer(gridlib.GridCellRenderer):
    """Keep comparison, status and parameter cues visible under selection."""

    def Clone(self) -> MatrixCellRenderer:
        """Return a renderer for native wx ownership."""
        return MatrixCellRenderer()

    @staticmethod
    def _inheritance_metrics(dc: wx.DC) -> tuple[int, int, int]:
        """Size the solid arrow, gap, and shaft using the current cell font."""
        size = max(5, round(dc.GetTextExtent("Hg")[1] * 0.85))
        gap = max(1, round(dc.GetTextExtent("M")[0] / 2))
        return size, gap, max(2, round(size / 5))

    @staticmethod
    def _draw_inheritance(
        dc: wx.DC,
        x: int,
        y: int,
        size: int,
        shaft: int,
        background: wx.Colour,
    ) -> None:
        """Paint a bold bent arrow with a filled head, independent of font glyphs."""
        dark = sum(background[index] for index in range(3)) < 3 * 128
        shade = 235 if dark else 65
        edge = size - 1
        head = max(2, round(size / 3))
        center = edge - head
        upper = center - shaft // 2
        lower = upper + shaft
        points = (
            (0, 0),
            (shaft, 0),
            (shaft, upper),
            (edge - head, upper),
            (edge - head, center - head),
            (edge, center),
            (edge - head, edge),
            (edge - head, lower),
            (0, lower),
        )
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.Colour(shade, shade, shade)))
        dc.DrawPolygon([wx.Point(x + px, y + py) for px, py in points])

    @staticmethod
    def _content(model: Any, row: int, col: int, style: Any) -> tuple[str, str, bool]:
        """Share the exact visible text and status marker with native sizing."""
        key = model.columns[col].key
        text = model.get_display(row, col)
        marker = style.symbol
        if key in ("price", "stock") or (
            key in _COMPACT_FIELDS and not style.different
        ):
            marker = ""
        return text, marker, bool(marker and key in _COMPACT_FIELDS)

    def GetBestSize(
        self,
        grid: VariantMatrixView,
        attr: gridlib.GridCellAttr,
        dc: wx.DC,
        row: int,
        col: int,
    ) -> wx.Size:
        """Report painted content; wx's autosizer supplies the outer padding."""
        dc.SetFont(attr.GetFont())
        if grid.model.columns[col].key in FLAG_FIELDS | {"stock"}:
            size = grid.flag_bitmap(True).GetSize()
            return wx.Size(size.width, size.height)
        text, marker, corner = self._content(
            grid.model, row, col, grid.model.cell_style(row, col)
        )
        width, height = dc.GetTextExtent(text)
        compact = getattr(grid, "_compact_text_widths", {})
        column = grid.model.columns[col]
        if column.key in _PERCENTILE_TEXT_FIELDS and compact:
            width = min(width, compact[_width_key(column)])
        if column.key == "value":
            width += grid.FromDIP(_VALUE_LEFT_PADDING_DIP)
        if grid.model.columns[col].key == "lcsc":
            size, gap, _shaft = self._inheritance_metrics(dc)
            width += size + gap
            height = max(height, size)
        if marker and not corner:
            marker_width, marker_height = dc.GetTextExtent(marker)
            width += marker_width + grid.FromDIP(4)
            height = max(height, marker_height)
        limit = _TEXT_WIDTH_EM.get(grid.model.columns[col].key)
        if limit is not None:
            width = min(width, limit * dc.GetTextExtent("M")[0])
        return wx.Size(width, height)

    def Draw(
        self,
        grid: VariantMatrixView,
        attr: gridlib.GridCellAttr,
        dc: wx.DC,
        rect: wx.Rect,
        row: int,
        col: int,
        isSelected: bool,
    ) -> None:
        """Keep status, comparison and selection visible together."""
        model = grid.model
        column = model.columns[col]
        style = model.cell_style(row, col)
        background = grid.GetDefaultCellBackgroundColour()
        foreground = grid.GetDefaultCellTextColour()
        row_active = isSelected
        isSelected = grid.is_variant_selected(row, col)
        if row % 2:
            dark_background = sum(background[index] for index in range(3)) < 3 * 128
            background = _blend(
                background,
                (foreground[0], foreground[1], foreground[2]),
                0.16 if dark_background else 0.10,
            )
        if row_active or isSelected:
            highlight = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
            background = _blend(
                background,
                (highlight[0], highlight[1], highlight[2]),
                0.30 if isSelected else 0.15,
            )
        status = style.status
        if style.different and status == "known" and column.key != "price":
            # Most fields match Default. Emphasize only exceptions, leaving the
            # ordinary zebra/selection background intact everywhere else.
            # Price already has comparison-colored arrows; amber behind those
            # small red/green glyphs would reduce their contrast on dark themes.
            dark = sum(background[index] for index in range(3)) < 3 * 128
            background = _blend(background, (255, 190, 35), 0.36 if dark else 0.28)
        if status == "error":
            background = _blend(background, (210, 65, 60), 0.2)
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(background))
        dc.DrawRectangle(rect)
        dc.SetFont(attr.GetFont())
        dc.SetTextForeground(foreground)
        dc.SetBackgroundMode(wx.TRANSPARENT)
        text, marker, corner_marker = self._content(model, row, col, style)
        if column.key == "side":
            foreground = (
                wx.Colour(77, 127, 196)
                if str(text).upper() == "B"
                else wx.Colour(200, 52, 52)
            )
        elif column.key == "price":
            foreground = _price_foreground(background, model.price_comparison(row, col))
        margin = grid.FromDIP(
            2 if column.key in _READ_ONLY_STATUS_FIELDS | {"side"} else 4
        )
        left_padding = (
            grid.FromDIP(_VALUE_LEFT_PADDING_DIP) if column.key == "value" else 0
        )
        x = rect.x + margin + left_padding
        y = rect.y + max(0, (rect.height - dc.GetTextExtent("Hg")[1]) // 2)
        marker_width = (
            dc.GetTextExtent(marker)[0] + margin if marker and not corner_marker else 0
        )
        text_rect = wx.Rect(
            rect.x, rect.y, max(0, rect.width - marker_width), rect.height
        )
        clipping = wx.DCClipper(dc, text_rect)
        try:
            inheritance_width = 0
            if column.key == "lcsc":
                size, gap, shaft = self._inheritance_metrics(dc)
                inheritance_width = size + gap
                if style.provenance == "inherited":
                    self._draw_inheritance(
                        dc,
                        x,
                        rect.y + max(0, (rect.height - size) // 2),
                        size,
                        shaft,
                        background,
                    )
                x += inheritance_width
            fitted = None
            body_y = y
            if column.key in _PERCENTILE_TEXT_FIELDS or (
                column.key in _READ_ONLY_STATUS_FIELDS - {"stock"}
                and dc.GetTextExtent(text)[0] > text_rect.width - margin * 2
            ):
                fitted = grid._fit_text(
                    dc,
                    attr.GetFont(),
                    text,
                    max(0, text_rect.width - margin * 2 - left_padding),
                )
                dc.SetFont(grid._scaled_text_font(attr.GetFont(), fitted.scale))
                body_y = rect.y + max(0, (rect.height - dc.GetTextExtent("Hg")[1]) // 2)
            display = fitted.display_text if fitted is not None else text
            if column.key in _COMPACT_FIELDS:
                x = rect.x + max(
                    margin, (rect.width - dc.GetTextExtent(display)[0]) // 2
                )
            dc.SetTextForeground(foreground)
            if column.key == "stock" and not model.stock_check(row, col).sufficient:
                self._draw_stock_warning(grid, dc, rect)
            elif column.key in FLAG_FIELDS and model.get_value(row, col) is False:
                self._draw_cross(grid, dc, rect, background)
            elif column.key in FLAG_FIELDS or column.key == "stock":
                bitmap = (
                    grid.stock_bitmap(self._stock_icon_size(grid, rect))
                    if column.key == "stock"
                    else grid.flag_bitmap(bool(model.get_value(row, col)))
                )
                dc.DrawBitmap(
                    bitmap,
                    rect.x + (rect.width - bitmap.GetWidth()) // 2,
                    rect.y + (rect.height - bitmap.GetHeight()) // 2,
                    True,
                )
            elif column.key == "params":
                self._draw_params(
                    grid, dc, row, col, text, x, body_y, foreground, fitted
                )
            else:
                dc.DrawText(
                    display
                    if fitted is not None
                    else wx.Control.Ellipsize(
                        display,
                        dc,
                        wx.ELLIPSIZE_END,
                        max(1, text_rect.width - margin * 2 - inheritance_width),
                    ),
                    x,
                    body_y,
                )
        finally:
            dc.SetFont(attr.GetFont())
            del clipping
        if corner_marker:
            extent = grid.FromDIP(4)
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(_comparison_accent(background)))
            dc.DrawPolygon(
                [
                    wx.Point(rect.GetRight() - extent, rect.y),
                    wx.Point(rect.GetRight(), rect.y),
                    wx.Point(rect.GetRight(), rect.y + extent),
                ]
            )
        elif marker:
            dc.SetTextForeground(_comparison_accent(background))
            dc.DrawText(
                marker, rect.GetRight() - dc.GetTextExtent(marker)[0] - margin, y
            )
        self._draw_row_borders(grid, dc, rect, row, col, isSelected)

    @staticmethod
    def _draw_cross(
        grid: VariantMatrixView, dc: wx.DC, rect: wx.Rect, background: wx.Colour
    ) -> None:
        """Draw a bold red X within the existing font-scaled status icon footprint."""
        size = grid.flag_bitmap(False).GetWidth()
        left = rect.x + (rect.width - size) // 2
        top = rect.y + (rect.height - size) // 2
        start, end = round(size * 0.25), round(size * 0.75)
        dc.SetPen(wx.Pen(_cross_foreground(background), max(2, round(size / 7))))
        dc.DrawLine(left + start, top + start, left + end, top + end)
        dc.DrawLine(left + start, top + end, left + end, top + start)

    @staticmethod
    def _draw_row_borders(
        grid: VariantMatrixView,
        dc: wx.DC,
        rect: wx.Rect,
        row: int,
        col: int,
        selected: bool,
    ) -> None:
        """Give either adjacent cell the same shared separator to repaint."""
        columns = grid.model.columns
        variant = columns[col].variant

        def row_style(index: int) -> tuple[bool, int]:
            if not 0 <= index < len(grid.model.rows):
                return False, 0
            different = (
                variant is not None
                and grid.model.cell_style(index, col).variant_different
            )
            active = grid.IsInSelection(index, col)
            native_selected = (
                selected if index == row else grid.is_variant_selected(index, col)
            )
            width = grid.FromDIP(2 if native_selected else 1) if active else 0
            return different, width

        previous, current, following = (
            row_style(index) for index in (row - 1, row, row + 1)
        )
        # Native CellToRect excludes the bottom separator. Both neighbors must
        # use that exact pixel: upper.y + upper.height - 1 == lower.y - 1.
        top = rect.y - int(row > 0)
        bottom = rect.y + grid.GetRowSize(row) - 1
        boundaries = (
            (top, previous[0] or current[0], max(previous[1], current[1])),
            (bottom, current[0] or following[0], max(current[1], following[1])),
        )
        if grid.GridLinesEnabled():
            dc.SetPen(wx.Pen(grid.GetGridLineColour()))
            for y, _different, _width in boundaries:
                if y != top or row > 0:
                    dc.DrawLine(rect.x, y, rect.GetRight() + 1, y)

        for y, _different, width in boundaries:
            if width:
                dc.SetPen(
                    wx.Pen(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT), width)
                )
                dc.DrawLine(rect.x, y, rect.GetRight(), y)

        # Keep differences above selection on the same shared row boundaries.
        first = col == 0 or columns[col - 1].variant != variant
        last = col == len(columns) - 1 or columns[col + 1].variant != variant
        inset = max(1, grid.FromDIP(2))
        left = rect.x + (inset if first else 0)
        right = rect.GetRight() - (inset if last else 0)
        if left <= right and top <= bottom:
            dc.SetPen(
                wx.Pen(
                    _comparison_accent(grid.GetDefaultCellBackgroundColour()),
                    max(1, grid.FromDIP(1)),
                )
            )
            for y, different, _width in boundaries:
                if different:
                    dc.DrawLine(left, y, right, y)
            if current[0]:
                if first:
                    dc.DrawLine(left, top, left, bottom)
                if last:
                    dc.DrawLine(right, top, right, bottom)

    @staticmethod
    def _stock_icon_size(grid: VariantMatrixView, rect: wx.Rect) -> int:
        """Keep Stock artwork inside the shared informational-column width."""
        return max(
            1,
            min(
                grid.flag_bitmap(True).GetWidth(),
                min(rect.width, rect.height) - grid.FromDIP(4),
            ),
        )

    def _draw_stock_warning(
        self, grid: VariantMatrixView, dc: wx.DC, rect: wx.Rect
    ) -> None:
        """Draw a recognizable yellow warning without relying on emoji fonts."""
        size = self._stock_icon_size(grid, rect)
        left = rect.x + (rect.width - size) // 2
        top = rect.y + (rect.height - size) // 2
        center = left + size // 2
        unit = size / 24
        dc.SetPen(wx.Pen(wx.Colour(135, 100, 0), max(1, round(unit))))
        dc.SetBrush(wx.Brush(wx.Colour(255, 201, 40)))
        dc.DrawPolygon(
            [
                wx.Point(center, top + round(2 * unit)),
                wx.Point(left + round(2 * unit), top + round(22 * unit)),
                wx.Point(left + round(22 * unit), top + round(22 * unit)),
            ]
        )
        dc.SetPen(wx.Pen(wx.Colour(35, 30, 15), max(1, round(2 * unit))))
        dc.DrawLine(center, top + round(8 * unit), center, top + round(14 * unit))
        dc.SetBrush(wx.Brush(wx.Colour(35, 30, 15)))
        dc.DrawCircle(center, top + round(18 * unit), max(1, round(unit)))

    def _draw_params(
        self,
        grid: VariantMatrixView,
        dc: wx.DC,
        row: int,
        col: int,
        text: str,
        x: int,
        y: int,
        foreground: wx.Colour,
        fitted: Optional[FittedText] = None,
    ) -> None:
        """Keep parameter matches aligned with the original visible text suffix."""
        model = grid.model
        variant = model.columns[col].variant
        value_col = model.column_for(variant, "value")
        reference = model.rows[row].reference
        terms = expand_value(reference, str(model.get_value(row, value_col)))
        terms += expand_footprint(reference, str(model.get_value(row, 1)))
        spans = find_highlight_spans(
            text, [term.casefold() for term in terms if len(term) > 1]
        )
        if fitted is not None:
            spans = visible_highlight_spans(spans, fitted)
            text = fitted.display_text
        dc.SetTextForeground(foreground)
        dc.DrawText(text, x, y)
        if not spans:
            return
        # Drawing runs separately changes kerning at their boundaries and can
        # widen a suffix after it has been fitted. Redraw the same shaped text
        # through match clips so highlighting changes only its color.
        # GetPartialTextExtents can report glyph clusters instead of character
        # positions (wxGTK 3.2). Measure complete prefixes so ligatures before a
        # match cannot shift its clip or erase trailing highlights.
        height = dc.GetTextExtent(text)[1]
        for start, end in spans:
            left = dc.GetTextExtent(text[:start])[0] if start else 0
            right = dc.GetTextExtent(text[:end])[0]
            if right <= left:
                continue
            with wx.DCClipper(dc, wx.Rect(x + left, y, right - left, height)):
                dc.SetTextForeground(
                    _comparison_accent(grid.GetDefaultCellBackgroundColour())
                )
                dc.DrawText(text, x, y)


class _VariantDragPreview(wx.PopupWindow):
    """Paint a cached ghost without taking focus or the header's mouse capture."""

    def __init__(self, view: VariantMatrixView, drag: _VariantHeaderDrag) -> None:
        bitmap, hotspot = view._drag_preview_bitmap(drag)
        super().__init__(view, flags=wx.BORDER_NONE)
        self.view, self.drag = view, drag
        self.bitmap, self.hotspot = bitmap, hotspot
        self.closed = False
        # wxGTK popups inherit the unsupported SetTransparent implementation.
        # Keep their captured columns visible as an opaque surface; the header
        # still composites its preview in software below the insertion marker.
        self.SetTransparent(153)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def AcceptsFocus(self) -> bool:
        return False

    def draw(self) -> None:
        """Move the native surface; the compositor restores its previous position."""
        if self.closed:
            return
        body = self.view.GetGridWindow()
        rect = wx.Rect(
            self.drag.window.ClientToScreen(
                (self.drag.x - self.hotspot.x, self.drag.y - self.hotspot.y)
            ),
            self.bitmap.GetLogicalSize(),
        ).Intersect(wx.Rect(body.ClientToScreen((0, 0)), body.GetClientSize()))
        if not rect.IsEmpty():
            self.SetPosition(rect.GetPosition())
            self.SetClientSize(rect.GetSize())
            self.Refresh()
        self.Show(not rect.IsEmpty())

    def _on_paint(self, event: wx.PaintEvent) -> None:
        # The popup's native opacity applies to its whole surface. The header
        # instead composites in its existing paint event, below the drop marker.
        self._draw(wx.PaintDC(self), self, opacity=1)

    def _draw(self, dc: wx.DC, window: wx.Window, *, opacity: float = 0.6) -> None:
        """Paint the same source pixels in header and body coordinates."""
        if self.closed:
            return
        position = window.ScreenToClient(
            self.drag.window.ClientToScreen(
                (self.drag.x - self.hotspot.x, self.drag.y - self.hotspot.y)
            )
        )
        graphics = wx.GraphicsContext.Create(dc)
        graphics.BeginLayer(opacity)
        graphics.DrawBitmap(self.bitmap, *position, *self.bitmap.GetLogicalSize())
        graphics.EndLayer()
        graphics.Flush()

    def close(self) -> None:
        """Discard the native surface before releasing the drag's capture."""
        self.closed = True
        self.Hide()
        self.Destroy()


@dataclass
class _VariantHeaderDrag:
    """A pending header click or whole-group drag, before any order is committed."""

    window: wx.Window
    variant: str
    start_x: int
    start_y: int
    x: int
    y: int
    active: bool = False
    before: Optional[str] = None
    marker_x: Optional[int] = None
    valid: bool = False
    preview: Optional[_VariantDragPreview] = None


class VariantMatrixView(gridlib.Grid):
    """One physical row, explicit variant blocks, and a frozen shared prefix."""

    def __init__(
        self,
        parent: wx.Window,
        model: Any,
        *,
        on_target_changed: Optional[Callable[[Optional[MatrixTarget]], None]] = None,
        on_selection_changed: Optional[Callable[[tuple[str, ...]], None]] = None,
        on_activate: Optional[Callable[[MatrixTarget], None]] = None,
        on_edit: Optional[Callable[[MatrixTarget, bool], None]] = None,
        on_action: Optional[Callable[[str, MatrixTarget], None]] = None,
    ) -> None:
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.model = model
        self.output_variant: Optional[str] = None
        self.selected_target: Optional[MatrixTarget] = None
        self._on_target_changed = on_target_changed
        self._on_selection_changed = on_selection_changed
        self._selection_notifications_ready = False
        self._selection_pending = False
        self._selection_force = False
        self._navigation: Optional[tuple[object, Optional[str]]] = None
        self._last_physical_selection: tuple[str, ...] = ()
        self._on_activate = on_activate
        self._on_edit = on_edit
        self._on_action = on_action
        self._mutations_enabled = True
        self._changing_model = False
        self._resizing_columns = False
        self._resize_pending = False
        self._column_widths: dict[str, int] = {}
        self._auto_widths: Optional[dict[str, int]] = None
        self._minimum_widths: dict[str, int] = {}
        self._sizing_signature: Optional[tuple[str, str, int]] = None
        self._group_header_height = 0
        self._flag_bitmaps: dict[bool, wx.Bitmap] = {}
        self._reset_text_layout()
        self._header_name_hover = False
        self._header_drag: Optional[_VariantHeaderDrag] = None
        self._deferred_projection: Optional[Any] = None
        self._display_identity: Optional[tuple[Any, ...]] = None
        self._header_drag_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_header_drag_timer, self._header_drag_timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._sort_state: Optional[tuple[Optional[str], str, bool]] = None
        # A group can fit the viewport yet straddle the old 15-pixel scroll
        # increments. Pixel precision lets header navigation reveal both edges.
        self.SetScrollLineX(1)
        self.SetRowLabelSize(0)
        self.SetMinSize(self.FromDIP(wx.Size(600, 180)))
        self.SetColMinimalAcceptableWidth(0)
        self.EnableEditing(False)
        self.DisableDragColMove()
        self.DisableDragRowMove()
        self.DisableDragGridSize()
        self.SetDefaultRenderer(MatrixCellRenderer())
        self.SetGridLineColour(
            _blend(self.GetDefaultCellBackgroundColour(), (125, 125, 125), 0.25)
        )
        self.Bind(gridlib.EVT_GRID_SELECT_CELL, self._on_select_cell)
        self.Bind(gridlib.EVT_GRID_RANGE_SELECTED, self._on_range_selected)
        self.Bind(gridlib.EVT_GRID_CELL_LEFT_CLICK, self._on_cell_click)
        self.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self._on_cell_double_click)
        self.Bind(gridlib.EVT_GRID_CELL_RIGHT_CLICK, self._on_cell_right_click)
        self.Bind(gridlib.EVT_GRID_LABEL_LEFT_CLICK, self._on_label_click)
        self.Bind(gridlib.EVT_GRID_COL_SIZE, self._on_column_size)
        self.Bind(gridlib.EVT_GRID_COL_AUTO_SIZE, self._on_column_auto_size)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_KEY_DOWN, self._on_navigation_key)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._on_theme_changed)
        self.Bind(wx.EVT_DPI_CHANGED, self._on_dpi_changed)
        header = self.GetGridColLabelWindow()
        header.Bind(wx.EVT_PAINT, self._on_header_paint)
        header.Bind(wx.EVT_LEFT_DOWN, self._on_header_left_down)
        header.Bind(wx.EVT_LEFT_DCLICK, self._on_header_left_down)
        header.Bind(wx.EVT_LEFT_UP, self._on_header_left_up)
        header.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_header_capture_lost)
        header.Bind(wx.EVT_CONTEXT_MENU, self._on_header_context_menu)
        header.Bind(wx.EVT_MOTION, self._on_header_motion)
        header.Bind(wx.EVT_LEAVE_WINDOW, self._on_header_leave)
        for mouse_event in (wx.EVT_MOTION, wx.EVT_LEFT_DOWN, wx.EVT_LEFT_DCLICK):
            header.Bind(mouse_event, self._on_header_seam_mouse)
        self.set_model(model, preserve_state=False)
        self._selection_notifications_ready = True

    def flag_bitmap(self, included: bool) -> wx.Bitmap:
        """Cache the legacy green-check/red-cross artwork without empty side padding."""
        if included not in self._flag_bitmaps:
            bitmap = loadBitmapScaled(
                "mdi-check-color.png" if included else "mdi-close-color.png",
                static=True,
            )
            image = bitmap.ConvertToImage()
            # Crop the source asset's transparent padding, then size the artwork
            # with the cell font so it follows both font and display scaling.
            # Use 85% of the original size for more compact status columns.
            image = image.GetSubImage(wx.Rect(8, 0, 24, 24))
            dc = wx.ClientDC(self)
            dc.SetFont(self.GetDefaultCellFont())
            original_size = round(dc.GetTextExtent("Hg")[1] * 1.5)
            size = max(1, round(original_size * 0.85))
            if size != 24:
                image = image.Scale(size, size, wx.IMAGE_QUALITY_HIGH)
            self._flag_bitmaps[included] = wx.Bitmap(image)
        return self._flag_bitmaps[included]

    def stock_bitmap(self, size: int) -> wx.Bitmap:
        """Fit the read-only Stock check without shrinking editable flag artwork."""
        source = self.flag_bitmap(True)
        size = max(1, size)
        if size >= source.GetWidth():
            return source
        if size not in self._stock_bitmaps:
            if len(self._stock_bitmaps) >= 64:
                self._stock_bitmaps.clear()
            self._stock_bitmaps[size] = wx.Bitmap(
                source.ConvertToImage().Scale(size, size, wx.IMAGE_QUALITY_HIGH)
            )
        return self._stock_bitmaps[size]

    def _variant_boundary_after(self, col: int) -> bool:
        """Separate neighboring variant groups without changing frozen columns."""
        columns = self.model.columns
        return (
            0 <= col < len(columns) - 1
            and columns[col].variant is not None
            and columns[col + 1].variant is not None
            and columns[col + 1].variant != columns[col].variant
        )

    def _variant_divider_pen(self) -> wx.Pen:
        """Keep group rules prominent against both light and dark table themes."""
        foreground = self.GetDefaultCellTextColour()
        return wx.Pen(
            _blend(
                self.GetDefaultCellBackgroundColour(),
                (foreground[0], foreground[1], foreground[2]),
                0.65,
            ),
            self.FromDIP(3),
        )

    def GetColGridLinePen(self, col: int) -> wx.Pen:
        """Let native scrolling/clipping paint continuous variant dividers."""
        if self._variant_boundary_after(col):
            return self._variant_divider_pen()
        return super().GetColGridLinePen(col)

    def GetRowGridLinePen(self, row: int) -> wx.Pen:
        """Row separators are painted before outlines by the cell renderer."""
        return wx.TRANSPARENT_PEN

    def set_mutations_enabled(self, enabled: bool) -> None:
        """Guard edits while retaining harmless inspection."""
        self._mutations_enabled = enabled
        if not enabled:
            # A failed native read has no replacement model to invalidate a
            # gesture. End capture before disabling edits.
            self._cancel_header_drag()
            if getattr(self, "_resize_pending", False):
                self._resize_viewport()

    def set_output_variant(self, name: Optional[str]) -> None:
        """Identify the generation target without changing cell focus or layout."""
        if self.output_variant != name:
            self.output_variant = name
            self.GetGridColLabelWindow().Refresh()

    def set_model(self, model: Any, *, preserve_state: bool = True) -> None:
        """Repaint stable identities; rebuild only when their native coordinates change."""
        if self._navigation is not None:
            self._finish_navigation(self._navigation)
        table = self.GetTable()
        if table and self.model.show_footprint_library != model.show_footprint_library:
            self._column_widths.pop("footprint", None)
        if preserve_state and table:
            model.set_variant_order(self.model.variant_order)
        if self._sort_state:
            variant, field, descending = self._sort_state
            for col, column in enumerate(model.columns):
                if (column.variant, column.key) == (variant, field):
                    model.sort_by(col, descending=descending)
                    break
        identity = (
            model.snapshot.board_id,
            tuple(row.component_id for row in model.rows),
            tuple((column.variant, column.key) for column in model.columns),
        )
        stable = table is not None and identity == self._display_identity
        current = self.model.snapshot
        incoming = model.snapshot
        source_changed = any(
            getattr(current, key) != getattr(incoming, key)
            for key in (
                "board_id",
                "board_token",
                "variants",
                "inventory",
                "components",
            )
        )
        self._deferred_projection = None
        if self._header_drag is not None and not stable:
            if preserve_state and not source_changed:
                # Supplier results may change a sorted/filtered row mapping.
                # Keep the captured native windows until the gesture finishes.
                self._deferred_projection = model
                return
        if not stable or source_changed:
            self._cancel_header_drag()
        state = (
            self.capture_state() if preserve_state and table and not stable else None
        )
        for window in (self.GetGridWindow(), self.GetFrozenColGridWindow()):
            if window:
                window.SetToolTip("")
        self._changing_model = True
        self.BeginBatch()
        try:
            if not stable and table and self.GetNumberFrozenCols():
                self._set_column_resize_enabled(self.GetNumberFrozenCols() - 1, True)
                self.FreezeTo(0, 0)
            self.model = model
            if stable:
                table.model = model
            else:
                self.SetTable(
                    MatrixGridTable(model),
                    takeOwnership=True,
                    selmode=gridlib.Grid.SelectRows,
                )
                self.selected_target = None
            self._display_identity = identity
            self._reset_text_layout()
            self._auto_widths = None
            if self._header_drag is None:
                self._apply_column_widths()
            else:
                self._resize_pending = True
        finally:
            self.EndBatch()
            self._changing_model = False
        if self._header_drag is None:
            self._ensure_frozen()
            self._resize_pending = False
        if state:
            self.restore_state(state)
        elif stable and self.selected_target is not None:
            coords = coordinates_for(model, self.selected_target)
            target = target_at(model, *coords) if coords else None
            if target != self.selected_target:
                self.selected_target = target
                if self._on_target_changed:
                    self._on_target_changed(target)
        elif not stable and self._on_target_changed:
            self._on_target_changed(None)
        self.ForceRefresh()

    def capture_preferences(self) -> dict[str, Any]:
        """Persist presentation choices without cursor, selection, or editing state."""
        return {
            "widths": dict(self._column_widths),
            "show_footprint_library": self.model.show_footprint_library,
            "sort": self._sort_state,
            "variant_order": self.model.variant_order,
        }

    def restore_preferences(self, state: dict[str, Any]) -> None:
        """Validate current layout settings; unreleased formats are not migrated."""
        changed = "variant_order" in state and self.model.set_variant_order(
            state["variant_order"]
        )
        sort = state.get("sort")
        if (
            isinstance(sort, (tuple, list))
            and len(sort) == 3
            and (sort[0] is None or isinstance(sort[0], str))
            and isinstance(sort[1], str)
            and type(sort[2]) is bool
            and any(
                (column.variant, column.key) == tuple(sort[:2])
                for column in self.model.columns
            )
        ):
            changed = changed or self._sort_state != tuple(sort)
            self._sort_state = tuple(sort)
        if changed:
            self.set_model(self.model, preserve_state=False)
        same_footprint = (
            state.get("show_footprint_library", self.model.show_footprint_library)
            == self.model.show_footprint_library
        )
        if not same_footprint:
            self._column_widths.pop("footprint", None)
        widths = state.get("widths", {})
        if isinstance(widths, dict):
            keys = {_width_key(column) for column in self.model.columns}
            scale = max(1.0, self.FromDIP(1000) / 1000)
            limit = int((2**31 - 1) / (2 * max(1, len(self.model.columns)) * scale))
            self._column_widths.update(
                {
                    key: value
                    for key, value in widths.items()
                    if key in keys
                    and type(value) is int
                    and 0 < value <= limit
                    and (key != "footprint" or same_footprint)
                }
            )
        self._apply_column_widths()
        self._ensure_frozen()
        self.ForceRefresh()

    def capture_state(self) -> ViewState:
        """Capture component identities and focused variant/field before remapping."""
        if self._navigation is not None:
            self._finish_navigation(self._navigation)
        return ViewState(
            target=self.selected_target,
            scroll=tuple(self.GetViewStart()),
            components=self.selected_physical_component_ids(),
        )

    def restore_state(self, state: ViewState) -> None:
        """Restore live selection without reapplying persisted layout preferences."""
        self._cancel_header_drag(apply_pending=True)
        target = state.target
        if target is not None:
            self.select_components(state.components, target.variant, target.field)
            coords = coordinates_for(self.model, target)
            if coords and (
                not state.components or self.selected_physical_component_ids()
            ):
                self._focus_cell(*coords)
        else:
            self.select_components((), None, "ref")
        self.Scroll(*state.scroll)
        self.ForceRefresh()
        self._queue_selection_changed()

    def _set_variant_order(self, order: object) -> None:
        """Capture native indices before changing their semantic column mapping."""
        state = self.capture_state()
        if not self.model.set_variant_order(order):
            return
        self.set_model(self.model, preserve_state=False)
        self.restore_state(state)

    def reorder_variant(self, variant: str, before: Optional[str]) -> None:
        """Move a complete presentation group before another, or to the end."""
        order = list(self.model.variant_order)
        if variant not in order or before == variant:
            return
        if before is not None and before not in order:
            return
        order.remove(variant)
        order.insert(order.index(before) if before is not None else len(order), variant)
        self._set_variant_order(order)
        self._show_variant(variant)

    def reset_variant_order(self) -> None:
        """Restore the board's canonical order without changing edit targets."""
        self._set_variant_order(self.model.variants)
        self._show_variant("")

    def select_components(
        self,
        component_ids: tuple[str, ...],
        variant: Optional[str],
        field: str = "lcsc",
    ) -> None:
        """Select only explicitly requested component blocks in one variant."""
        self._navigation = None
        super().ClearSelection()
        try:
            column = self.model.column_for(variant, field)
        except StopIteration:
            self._publish_target(None)
            return
        wanted = set(component_ids)
        matching = [
            row
            for row, item in enumerate(self.model.rows)
            if item.component_id in wanted
        ]
        if matching:
            self._focus_cell(matching[0], column)
            for row in matching:
                self.SelectRow(row, addToSelected=True)
        else:
            self._publish_target(None)

    def selected_component_ids(self) -> tuple[str, ...]:
        """Return selected components in the one active variant."""
        target = self.selected_target
        if target is None or target.variant is None:
            return ()
        return self.selected_physical_component_ids()

    def is_variant_selected(self, row: int, col: int) -> bool:
        """Project native row selection onto the active variant's complete block."""
        target = self.selected_target
        return bool(
            target is not None
            and target.variant is not None
            and self.model.columns[col].variant == target.variant
            and self.IsInSelection(row, col)
        )

    def selected_physical_component_ids(self) -> tuple[str, ...]:
        """Resolve every selected native row independently of the edit variant."""
        return tuple(
            self.model.rows[row].component_id
            for block in self.GetSelectedRowBlocks()
            for row in range(
                max(0, block.GetTopRow()),
                min(len(self.model.rows), block.GetBottomRow() + 1),
            )
        )

    def ClearSelection(self) -> None:
        """Clear physical selection without treating the retained cursor as selected."""
        self._navigation = None
        super().ClearSelection()
        self._queue_selection_changed(force=True)

    def _queue_selection_changed(self, *, force: bool = False) -> None:
        """Wait until wx completes its cursor and range changes before notifying."""
        if (
            not getattr(self, "_selection_notifications_ready", False)
            or self._changing_model
        ):
            return
        self._selection_force = self._selection_force or force
        if not self._selection_pending:
            self._selection_pending = True
            wx.CallAfter(self._notify_selection_changed)

    def _notify_selection_changed(self) -> None:
        """Publish the finalized physical UUID set once per native selection turn."""
        self._selection_pending = False
        force, self._selection_force = self._selection_force, False
        if not self or self._changing_model:
            return
        # Native range invalidation can omit the shared separator just above
        # the range. Repaint after wx finalizes selection, including deselection.
        self.ForceRefresh()
        selected = self.selected_physical_component_ids()
        if force or selected != self._last_physical_selection:
            self._last_physical_selection = selected
            if self._on_selection_changed:
                self._on_selection_changed(selected)

    def _on_range_selected(self, event: gridlib.GridRangeSelectEvent) -> None:
        """Observe complete range changes, including Ctrl-deselect and Shift ranges."""
        if not self._changing_model:
            self._queue_selection_changed()
        event.Skip()

    def _publish_target(self, target: Optional[MatrixTarget]) -> None:
        """Update edit focus and defer physical selection until native changes finish."""
        self.selected_target = target
        if self._on_target_changed:
            self._on_target_changed(target)
        self.ForceRefresh()
        self._queue_selection_changed()

    def _focus_cell(self, row: int, col: int) -> None:
        """Restore semantic focus even when wx suppresses a same-cell selection event."""
        self._navigation = None
        self.SetGridCursor(row, col)
        target = target_at(self.model, row, col)
        if self.selected_target != target:
            self._publish_target(target)
        self._queue_selection_changed()

    def _on_select_cell(self, event: gridlib.GridEvent) -> None:
        """Track keyboard/cursor focus while allowing wx to finalize selection."""
        if not self._changing_model:
            self._publish_target(target_at(self.model, event.GetRow(), event.GetCol()))
        event.Skip()

    def _on_cell_click(self, event: gridlib.GridEvent) -> None:
        """Let wx select rows, keeping every batch within the clicked variant."""
        self._navigation = None
        row, col = event.GetRow(), event.GetCol()
        target = target_at(self.model, row, col)
        if target is None:
            event.Skip()
            return
        previous = self.selected_target
        changed_variant = previous is None or previous.variant != target.variant
        modified = event.ShiftDown() or event.MetaDown() or event.ControlDown()
        if changed_variant:
            self.ClearSelection()
        if changed_variant and modified:
            # A native Shift click would extend from the previous variant's anchor.
            self.SelectRow(row)
            self._focus_cell(row, col)
            return
        self._publish_target(target)
        self._queue_selection_changed(force=True)
        if target.field not in FLAG_FIELDS or modified:
            event.Skip()
            return
        self.ClearSelection()
        self.SelectRow(row)
        self._focus_cell(row, col)
        if self._mutations_enabled and self._on_edit:
            self._on_edit(target, not bool(self.model.get_value(row, col)))

    def _on_cell_double_click(self, event: gridlib.GridEvent) -> None:
        """Open shared corrections or the explicitly targeted variant part selector."""
        target = target_at(self.model, event.GetRow(), event.GetCol())
        if target is None or target.field in FLAG_FIELDS:
            return
        self._focus_cell(event.GetRow(), event.GetCol())
        self._activate_target(target)

    def _activate_target(self, target: MatrixTarget) -> bool:
        """Share keyboard and mouse activation without changing native selection."""
        if not self._mutations_enabled:
            return False
        if target.field == "correction":
            self._dispatch_action("correction", target)
        elif (
            target.variant is not None
            and target.field not in FLAG_FIELDS
            and self._on_activate
        ):
            self._on_activate(target)
        else:
            return False
        return True

    def _on_cell_right_click(self, event: gridlib.GridEvent) -> None:
        """Build actions appropriate to shared physical or variant-specific fields."""
        target = target_at(self.model, event.GetRow(), event.GetCol())
        if target is None:
            return
        if (
            self.selected_target is None
            or self.selected_target.variant != target.variant
            or not self.IsInSelection(event.GetRow(), event.GetCol())
        ):
            self.ClearSelection()
            self.SelectRow(event.GetRow())
        self._focus_cell(event.GetRow(), event.GetCol())
        menu = wx.Menu()
        actions = [
            ("copy", "Copy\tCmd/Ctrl+C"),
            ("copy_cell", "Copy cell value"),
            ("details", "Cell details\tF2"),
        ]
        if target.field == "correction":
            actions.append(("correction", "Edit shared correction…"))
        if target.variant is not None:
            actions.extend(
                (
                    ("paste", "Paste\tCmd/Ctrl+V"),
                    ("enter_lcsc", "Enter LCSC…"),
                    ("copy_to", "Copy to variants…"),
                    ("remove", "Clear assignment"),
                    ("use_base", "Use base assignment"),
                    ("save_preferences", "Save part preference"),
                    ("apply_preferences", "Apply part preference"),
                )
            )
        for action, label in actions:
            item = menu.Append(wx.ID_ANY, label)
            if action in (
                "paste",
                "enter_lcsc",
                "copy_to",
                "remove",
                "use_base",
                "save_preferences",
                "apply_preferences",
                "correction",
            ):
                item.Enable(
                    self._mutations_enabled
                    and (action != "use_base" or target.variant != "")
                )
            menu.Bind(
                wx.EVT_MENU,
                lambda _event,
                captured_action=action,
                captured_target=target: self._dispatch_action(
                    captured_action, captured_target
                ),
                item,
            )
        try:
            self.PopupMenu(menu)
        finally:
            menu.Destroy()

    def _dispatch_action(self, action: str, target: MatrixTarget) -> None:
        """Route cell actions through the controller that owns editing and dialogs."""
        if self._on_action:
            self._on_action(action, target)

    def _on_navigation_key(self, event: wx.KeyEvent) -> None:
        """Complete native keyboard navigation before applying variant scope."""
        if event.GetKeyCode() in (
            wx.WXK_LEFT,
            wx.WXK_RIGHT,
            wx.WXK_UP,
            wx.WXK_DOWN,
            wx.WXK_HOME,
            wx.WXK_END,
            wx.WXK_PAGEUP,
            wx.WXK_PAGEDOWN,
            wx.WXK_TAB,
            wx.WXK_RETURN,
            wx.WXK_NUMPAD_ENTER,
        ):
            target = self.selected_target
            self._navigation = (object(), target.variant if target else None)
            wx.CallAfter(self._finish_navigation, self._navigation)
        event.Skip()

    def _finish_navigation(self, pending: tuple[object, Optional[str]]) -> None:
        """Native arrows move the cursor but can clear all selected rows."""
        if not self or self._changing_model or self._navigation is not pending:
            return
        self._navigation = None
        row, col = self.GetGridCursorRow(), self.GetGridCursorCol()
        target = target_at(self.model, row, col)
        if target is None:
            return
        if target.variant != pending[1]:
            self.ClearSelection()
        if not self.IsSelection():
            self.SelectRow(row)
        self._publish_target(target)
        self._queue_selection_changed(force=True)

    def _on_key(self, event: wx.KeyEvent) -> None:
        """Route shortcuts while leaving unhandled native navigation alone."""
        target = self.selected_target
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE and self._header_drag is not None:
            self._cancel_header_drag(apply_pending=True)
            return
        if target is not None:
            if event.CmdDown() and key in (ord("C"), ord("V")):
                if key == ord("C") or self._mutations_enabled:
                    self._dispatch_action(
                        "copy" if key == ord("C") else "paste", target
                    )
                return
            if key == wx.WXK_F2:
                self._dispatch_action("details", target)
                return
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self._activate_target(
                target
            ):
                return
            if (
                key == wx.WXK_SPACE
                and target.variant is not None
                and target.field in FLAG_FIELDS
                and self._mutations_enabled
                and self._on_edit
            ):
                coords = coordinates_for(self.model, target)
                if coords:
                    self._on_edit(target, not bool(self.model.get_value(*coords)))
                return
        event.Skip()

    def _on_header_motion(self, event: wx.MouseEvent) -> None:
        """Track group dragging and keep field resize cursors in their header tier."""
        if getattr(self, "_header_drag", None) is not None:
            if event.LeftIsDown():
                self._update_header_drag(event.GetPosition())
            else:
                self._cancel_header_drag(apply_pending=True)
            return
        position = event.GetPosition()
        header = self.GetGridColLabelWindow()
        if header.HasCapture():
            # A lower-tier native resize owns its complete gesture, even if
            # the pointer subsequently crosses into the variant-name tier.
            event.Skip()
            return
        if self._header_variant_at(position) is not None:
            self._enter_name_header(position)
            return
        if self._header_name_hover:
            self._header_name_hover = False
            self._set_header_field_cursor(header, position)
        event.Skip()

    def _on_header_leave(self, event: wx.MouseEvent) -> None:
        """Restore the pointer after leaving the draggable variant-name header."""
        if getattr(self, "_header_drag", None) is not None:
            return
        if self._header_name_hover:
            self._header_name_hover = False
            event.GetEventObject().SetCursor(wx.NullCursor)
        event.Skip()

    def _on_header_seam_mouse(self, event: wx.MouseEvent) -> None:
        """Keep both sides of the frozen seam inert, even after scrolling."""
        window = event.GetEventObject()
        if (
            self._header_drag is not None
            or window.HasCapture()
            or not self.GetNumberFrozenCols()
        ):
            event.Skip()
            return
        position = event.GetPosition()
        margin = self.FromDIP(4)
        scrolling = window == self.GetGridColLabelWindow()
        at_seam = (
            0 <= position.x < margin
            if scrolling
            else 0 <= window.GetClientSize().width - position.x <= margin
        )
        if at_seam:
            if not getattr(window, "_variant_seam_hover", False):
                # Reset a prior native resize cursor before taking ownership
                # of this narrow strip. The normal leave path does no edits.
                leave = wx.MouseEvent(wx.wxEVT_LEAVE_WINDOW)
                leave.SetEventObject(window)
                leave.SetId(window.GetId())
                leave.SetPosition(position)
                window.GetEventHandler().ProcessEvent(leave)
                window._variant_seam_hover = True
            window.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
            return
        if getattr(window, "_variant_seam_hover", False):
            window._variant_seam_hover = False
            self._set_header_field_cursor(window, position)
        event.Skip()

    def _set_header_field_cursor(self, window: wx.Window, position: wx.Point) -> None:
        """Restore a real field cursor even when wx retains its previous mode."""
        grid_window = (
            self.GetGridWindow()
            if window == self.GetGridColLabelWindow()
            else self.GetFrozenColGridWindow()
        )
        offset = self.GetGridWindowOffset(grid_window)
        logical_x, _ = self.CalcGridWindowUnscrolledPosition(
            position.x + offset.x, position.y + offset.y, grid_window
        )
        edge = self.XToEdgeOfCol(logical_x)
        resizable = edge >= 0 and self.CanDragColSize(edge)
        # NullCursor inherits the parent's arrow. wx's cached resize mode can
        # suppress reapplying its cursor at the same divider, so choose using
        # wx's own hit test instead of relying on a mode transition.
        window.SetCursor(wx.Cursor(wx.CURSOR_SIZEWE if resizable else wx.CURSOR_ARROW))

    def _enter_name_header(self, position: wx.Point) -> None:
        """Leave wx's field-resize cursor mode before owning the name tier."""
        header = self.GetGridColLabelWindow()
        if not self._header_name_hover:
            # wx exposes no cursor-mode reset in Python. Its leave handler
            # resets that internal mode, so returning to a lower-tier edge
            # can display the native resize cursor again. Only do this before
            # our capture; never interrupt a captured native resize.
            leave = wx.MouseEvent(wx.wxEVT_LEAVE_WINDOW)
            leave.SetEventObject(header)
            leave.SetId(header.GetId())
            leave.SetPosition(position)
            header.GetEventHandler().ProcessEvent(leave)
            self._header_name_hover = True
        header.SetCursor(wx.Cursor(wx.CURSOR_HAND))

    def _on_header_left_down(self, event: wx.MouseEvent) -> None:
        """Reserve the name tier for click navigation or a complete-group drag."""
        self._cancel_header_drag(apply_pending=True)
        position = event.GetPosition()
        variant = self._header_variant_at(position)
        if variant is None:
            event.Skip()
            return
        header = self.GetGridColLabelWindow()
        self._enter_name_header(position)
        self._header_drag = _VariantHeaderDrag(
            header, variant, position.x, position.y, position.x, position.y
        )
        self.SetFocus()
        header.SetToolTip("")
        if not header.HasCapture():
            header.CaptureMouse()

    def _header_logical_x(self, x: int) -> int:
        """Convert the scrolling label child's x to the grid's column coordinates."""
        window = self.GetGridWindow()
        offset = self.GetGridWindowOffset(window)
        logical_x, _ = self.CalcGridWindowUnscrolledPosition(
            x + offset.x, offset.y, window
        )
        return logical_x

    def _header_variant_at(self, position: wx.Point) -> Optional[str]:
        """Hit test a variant name, including its subcolumn resize boundaries."""
        if not 0 <= position.y < self._group_header_height:
            return None
        col = self.XToCol(
            self._header_logical_x(position.x), False, self.GetGridWindow()
        )
        if 0 <= col < len(self.model.columns):
            return self.model.columns[col].variant
        return None

    def _drag_preview_bitmap(
        self, drag: _VariantHeaderDrag
    ) -> tuple[wx.Bitmap, wx.Point]:
        """Render visible source cells at their natural size into a bounded bitmap."""
        origin = self._header_logical_x(0)
        viewport_width = drag.window.GetClientSize().width
        cols = [
            col
            for col, column in enumerate(self.model.columns)
            if column.variant == drag.variant
            and self.GetColRight(col) > origin
            and self.GetColLeft(col) < origin + viewport_width
        ]
        if not cols or viewport_width <= 0:
            raise RuntimeError("The dragged variant is outside the viewport")
        left = max(origin, self.GetColLeft(cols[0]))
        right = min(origin + viewport_width, self.GetColRight(cols[-1]))
        body = self.GetGridWindow()
        offset = self.GetGridWindowOffset(body)
        _, top = self.CalcGridWindowUnscrolledPosition(*offset, body)
        header_height = self.GetColLabelSize()
        body_height = 0
        if self.GetNumberRows():
            last = self.GetNumberRows() - 1
            end = self.CellToRect(last, cols[0]).y + self.GetRowSize(last)
            body_height = max(0, min(body.GetClientSize().height, end - top))
        size = wx.Size(right - left, header_height + body_height)
        scale = self.GetContentScaleFactor()
        bitmap = wx.Bitmap()
        if not bitmap.CreateWithDIPSize(size, scale):
            raise RuntimeError("Unable to allocate variant drag preview")
        dc = wx.MemoryDC(bitmap)
        try:
            dc.SetBackground(wx.Brush(self.GetLabelBackgroundColour()))
            dc.Clear()
            dc.SetDeviceOrigin(-left, 0)
            with wx.DCClipper(dc, wx.Rect(left, 0, size.width, header_height)):
                self._draw_header_labels(dc, cols, left, size.width)
            dc.SetDeviceOrigin(0, 0)
            if body_height:
                first_row = self.YToRow(top)
                last_row = self.YToRow(top + body_height - 1)
                dc.SetDeviceOrigin(-left, header_height - top)
                with wx.DCClipper(dc, wx.Rect(left, top, size.width, body_height)):
                    # Grid.Render's native selection overlay requires a PaintDC
                    # on some wx versions. Reuse our renderer without mutating
                    # live selection or allocating offscreen columns/rows.
                    renderer, attr = MatrixCellRenderer(), gridlib.GridCellAttr()
                    for row in range(first_row, last_row + 1):
                        for col in cols:
                            attr.SetFont(self.GetCellFont(row, col))
                            renderer.Draw(
                                self,
                                attr,
                                dc,
                                self.CellToRect(row, col),
                                row,
                                col,
                                self.IsInSelection(row, col),
                            )
                    if self.GridLinesEnabled():
                        for col in cols:
                            dc.SetPen(self.GetColGridLinePen(col))
                            x = self.GetColRight(col) - 1
                            dc.DrawLine(x, top, x, top + body_height)
        finally:
            dc.SelectObject(wx.NullBitmap)
        image = bitmap.ConvertToImage()
        image.SetAlpha(bytes([255]) * (image.GetWidth() * image.GetHeight()))
        bitmap = wx.Bitmap(image)
        bitmap.SetScaleFactor(scale)
        return bitmap, wx.Point(drag.start_x - (left - origin), drag.start_y)

    def _update_header_drag(self, position: wx.Point) -> None:
        """Preview a group insertion while leaving the table untouched until drop."""
        drag = self._header_drag
        if drag is None:
            return
        drag.x, drag.y = position.x, position.y
        if not drag.active:
            threshold_x = max(
                self.FromDIP(4), wx.SystemSettings.GetMetric(wx.SYS_DRAG_X, self)
            )
            threshold_y = max(
                self.FromDIP(4), wx.SystemSettings.GetMetric(wx.SYS_DRAG_Y, self)
            )
            if (
                abs(drag.x - drag.start_x) < threshold_x
                and abs(drag.y - drag.start_y) < threshold_y
            ):
                return
            drag.active = True
            try:
                drag.preview = _VariantDragPreview(self, drag)
            except (MemoryError, RuntimeError):
                # A preview allocation failure must leave ordering usable.
                drag.preview = None
        width = drag.window.GetClientSize().width
        logical_x = self._header_logical_x(drag.x)
        within_header = 0 <= drag.y < self.GetColLabelSize()
        drag.valid = (
            within_header
            and 0 <= drag.x < width
            and logical_x >= self.GetColLeft(FIXED_COLUMNS)
        )
        drag.window.SetCursor(
            wx.Cursor(wx.CURSOR_HAND if drag.valid else wx.CURSOR_NO_ENTRY)
        )
        groups: dict[str, tuple[int, int]] = {}
        for col, column in enumerate(self.model.columns):
            if column.variant is not None:
                first, _ = groups.get(column.variant, (col, col))
                groups[column.variant] = first, col
        drag.before = None
        drag.marker_x = self.GetColRight(len(self.model.columns) - 1)
        for variant in self.model.variant_order:
            if variant == drag.variant:
                continue
            first, last = groups[variant]
            left, right = self.GetColLeft(first), self.GetColRight(last)
            midpoint = (left + right) / 2
            # At the center of an earlier group, insert before it. At the
            # center of a later group, insert after it. Either adjacent move
            # therefore works when dropped directly on the target name.
            source_first = groups[drag.variant][0]
            if logical_x < midpoint or (logical_x == midpoint and first < source_first):
                drag.before = variant
                drag.marker_x = left
                break
        edge = max(self.FromDIP(16), self.GetCharHeight())
        if within_header and (drag.x < edge or drag.x >= width - edge):
            if not self._header_drag_timer.IsRunning():
                self._header_drag_timer.Start(40)
        else:
            self._header_drag_timer.Stop()
        drag.window.Refresh()
        if drag.preview is not None:
            drag.preview.draw()

    def _on_header_left_up(self, event: wx.MouseEvent) -> None:
        """Commit one group move, or perform the existing click-to-reveal action."""
        drag = getattr(self, "_header_drag", None)
        if drag is None:
            event.Skip()
            return
        position = event.GetPosition()
        self._update_header_drag(position)
        deferred = self._deferred_projection
        self._cancel_header_drag()
        reveal = None
        if drag.active:
            if drag.valid:
                self.reorder_variant(drag.variant, drag.before)
                reveal = drag.variant
        elif self._header_variant_at(position) == drag.variant:
            reveal = drag.variant
        # Commit against the geometry seen during the gesture before allowing
        # fresh metadata to resize columns or change filtered/sorted rows.
        if deferred is not None:
            self.set_model(deferred)
        if self._resize_pending:
            self._resize_viewport()
        if reveal is not None:
            self._show_variant(reveal)

    def _on_header_drag_timer(self, event: wx.TimerEvent) -> None:
        """Continue horizontal scrolling even while the captured pointer is still."""
        drag = self._header_drag
        if drag is None or not drag.active:
            self._header_drag_timer.Stop()
            return
        width = drag.window.GetClientSize().width
        edge = max(self.FromDIP(16), self.GetCharHeight())
        direction = -1 if drag.x < edge else 1 if drag.x >= width - edge else 0
        if not direction or not 0 <= drag.y < self.GetColLabelSize():
            self._header_drag_timer.Stop()
            return
        x, y = self.GetViewStart()
        step = max(self.FromDIP(12), self.GetCharWidth() * 3)
        unit = max(1, self.GetScrollPixelsPerUnit()[0])
        self.Scroll(max(0, x + direction * max(1, (step + unit - 1) // unit)), y)
        self._update_header_drag(wx.Point(drag.x, drag.y))

    def _cancel_header_drag(self, *, apply_pending: bool = False) -> None:
        """End capture; discard deferred data unless this is a normal gesture end."""
        deferred = getattr(self, "_deferred_projection", None)
        self._deferred_projection = None
        drag = getattr(self, "_header_drag", None)
        if drag is None:
            return
        self._header_drag = None
        preview, drag.preview = drag.preview, None
        try:
            if preview is not None:
                preview.close()
        finally:
            # Retain cursor ownership until the next motion restores a name or
            # field cursor, including after Escape or a drop in the lower tier.
            self._header_drag_timer.Stop()
            if drag.window:
                if drag.window.HasCapture():
                    drag.window.ReleaseMouse()
                if not drag.window.IsBeingDeleted():
                    drag.window.SetCursor(wx.NullCursor)
                    drag.window.Refresh()
        if apply_pending:
            if deferred is not None:
                self.set_model(deferred)
            if self._resize_pending:
                self._resize_viewport()

    def _on_header_capture_lost(self, event: wx.MouseCaptureLostEvent) -> None:
        """Abort rather than commit when another native control takes the mouse."""
        self._cancel_header_drag(apply_pending=True)
        event.Skip()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        """Stop drag timers before native children are destroyed."""
        if event.GetEventObject() is self:
            self._cancel_header_drag()
            self._resize_pending = False
        event.Skip()

    def _on_header_context_menu(self, event: wx.ContextMenuEvent) -> None:
        """Offer an explicit reset alongside direct header manipulation."""
        self._cancel_header_drag(apply_pending=True)
        menu = wx.Menu()
        item = menu.Append(wx.ID_ANY, "Reset variant order")
        item.Enable(self.model.variant_order != self.model.variants)
        try:
            chosen = self.GetGridColLabelWindow().GetPopupMenuSelectionFromUser(menu)
            if chosen == item.GetId():
                self.reset_variant_order()
        finally:
            menu.Destroy()

    def _show_variant(self, variant: str) -> None:
        """Reveal a whole variant block with minimal horizontal scrolling."""
        columns = [
            index
            for index, column in enumerate(self.model.columns)
            if column.variant == variant
        ]
        if not columns:
            return
        window = self.GetGridWindow()
        width = window.GetClientSize().width
        if width <= 0:
            return
        offset = self.GetGridWindowOffset(window)
        visible_left, _ = self.CalcGridWindowUnscrolledPosition(*offset, window)
        current_x, current_y = self.GetViewStart()
        unit = max(1, self.GetScrollPixelsPerUnit()[0])
        origin = visible_left - current_x * unit
        left, right = self.GetColLeft(columns[0]), self.GetColRight(columns[-1])
        minimum = (right - width - origin + unit - 1) // unit
        maximum = (left - origin) // unit
        if right - left > width or minimum > maximum:
            # An oversized group starts at its first column. With a coarser
            # external scroll rate, keep its left edge visible after rounding.
            destination = maximum
        else:
            destination = max(minimum, min(current_x, maximum))
        destination = max(0, destination)
        if destination != current_x:
            self.Scroll(destination, current_y)

    def _on_label_click(self, event: gridlib.GridEvent) -> None:
        """Reveal variant groups or sort full rows through their field headers."""
        col = event.GetCol()
        if 0 <= col < len(self.model.columns):
            column = self.model.columns[col]
            if (
                column.variant is not None
                and event.GetPosition().y < self._group_header_height
            ):
                self._show_variant(column.variant)
                return
            previous = self._sort_state
            descending = (
                previous is not None
                and previous[:2] == (column.variant, column.key)
                and not previous[2]
            )
            self._sort_state = (column.variant, column.key, descending)
            state = self.capture_state()
            self.model.sort_by(col, descending=descending)
            self.set_model(self.model, preserve_state=False)
            self.restore_state(state)
        else:
            self._publish_target(None)

    def _apply_column_widths(self) -> None:
        """Use native content sizing, with our custom header geometry and overrides."""
        self._resizing_columns = True
        self.BeginBatch()
        try:
            signature = (
                self.GetDefaultCellFont().GetNativeFontInfoDesc(),
                self.GetLabelFont().GetNativeFontInfoDesc(),
                self.FromDIP(100),
            )
            if signature != self._sizing_signature:
                self._auto_widths = None
                self._flag_bitmaps.clear()
                self._reset_text_layout()
                self._sizing_signature = signature
            if self._auto_widths is None:
                self._measure_layout()
                self._compact_text_widths = self._measure_compact_columns()
                # Reset old minima before asking wx to measure: setAsMin=False
                # still respects minima left by an earlier font or table.
                for index in range(self.GetNumberCols()):
                    self.SetColMinimalWidth(index, 1)
                table = self.GetTable()
                table.measuring_cells = True
                try:
                    if self.GetNumberRows():
                        self.AutoSizeColumns(setAsMin=False)
                finally:
                    table.measuring_cells = False
                widths = {
                    _width_key(column): self._minimum_widths[column.key]
                    for column in self.model.columns
                    if column.key not in _READ_ONLY_STATUS_FIELDS
                    or column.key == "standard"
                }
                if self.GetNumberRows():
                    for index, column in enumerate(self.model.columns):
                        if column.key in _READ_ONLY_STATUS_FIELDS - {"standard"}:
                            continue
                        key = _width_key(column)
                        content_width = self.GetColSize(index)
                        if column.key == "side":
                            # Keep native content measurement, trimming only its
                            # standard outer padding for this one-letter field.
                            content_width += self.FromDIP(
                                _SIDE_PADDING_DIP
                            ) - self.FromDIP(10)
                        widths[key] = max(widths[key], content_width)
                # These fields use the board-wide percentile even when filtering
                # hides every row. Native sizing still owns the other columns.
                for column in self.model.columns:
                    if column.key in _PERCENTILE_TEXT_FIELDS:
                        key = _width_key(column)
                        content = (
                            self._compact_text_widths[key]
                            + self._compact_marker_widths[key]
                        )
                        if column.key == "value":
                            content += self.FromDIP(_VALUE_LEFT_PADDING_DIP)
                        widths[key] = max(
                            self._minimum_widths[column.key], content + self.FromDIP(10)
                        )
                # Trim flag-column whitespace from the fresh native baseline.
                # Keep four DIP around the existing artwork/header at large
                # font sizes, where a full 10% reduction could clip content.
                for key in FLAG_FIELDS:
                    original_minimum = self._minimum_widths[key]
                    minimum = max(
                        round(original_minimum * _FLAG_WIDTH_SCALE),
                        original_minimum - self.FromDIP(6),
                    )
                    self._minimum_widths[key] = minimum
                    widths[key] = max(minimum, round(widths[key] * _FLAG_WIDTH_SCALE))
                # Measure Std at its original font for a stable baseline,
                # then reduce the shared width once. Headers and status contents
                # fit the resulting slot instead of widening the group.
                widths[_STATUS_WIDTH_KEY] = _status_column_width(
                    widths[_STATUS_WIDTH_KEY], self.FromDIP(10)
                )
                status_minimum = _status_column_width(
                    self._minimum_widths["standard"], self.FromDIP(10)
                )
                for key in _READ_ONLY_STATUS_FIELDS:
                    self._minimum_widths[key] = status_minimum
                self._auto_widths = widths
            for index, column in enumerate(self.model.columns):
                minimum = self._minimum_widths[column.key]
                key = _width_key(column)
                requested = (
                    self.FromDIP(self._column_widths[key])
                    if key in self._column_widths
                    else self._auto_widths[key]
                )
                self.SetColMinimalWidth(index, minimum)
                self.SetColSize(index, max(minimum, requested))
        finally:
            self.EndBatch()
            self._resizing_columns = False

    def _reset_text_layout(self) -> None:
        """Bound text/font caches to one coherent model and display configuration."""
        self._compact_text_widths: dict[str, int] = {}
        self._compact_marker_widths: dict[str, int] = {}
        self._text_width_cache: dict[tuple[str, str], int] = {}
        self._fitted_text_cache: dict[tuple[str, str, int], FittedText] = {}
        self._scaled_font_cache: dict[tuple[str, float], wx.Font] = {}
        self._stock_bitmaps: dict[int, wx.Bitmap] = {}

    def _text_width(self, dc: wx.DC, font: wx.Font, text: str) -> int:
        """Measure each distinct string/font once within a bounded display cache."""
        key = (font.GetNativeFontInfoDesc(), text)
        if key not in self._text_width_cache:
            dc.SetFont(font)
            if len(self._text_width_cache) >= _TEXT_CACHE_LIMIT:
                self._text_width_cache.clear()
            self._text_width_cache[key] = dc.GetTextExtent(text)[0]
        return self._text_width_cache[key]

    def _measure_compact_columns(self) -> dict[str, int]:
        """Choose independent row-weighted widths from the full physical inventory."""
        dc = wx.ClientDC(self)
        font = self.GetDefaultCellFont()
        widths = {}
        for col, column in enumerate(self.model.columns):
            if column.key not in _PERCENTILE_TEXT_FIELDS:
                continue
            key = _width_key(column)
            widths[key] = percentile_text_width(
                self.model.column_display_samples(col),
                lambda text: self._text_width(dc, font, text),
            )
            self._compact_marker_widths[key] = max(
                (
                    self._text_width(dc, font, marker) + self.FromDIP(4)
                    for marker in self.model.column_display_markers(col)
                    if marker
                ),
                default=0,
            )
        return widths

    def _scaled_text_font(self, font: wx.Font, scale: float) -> wx.Font:
        """Derive fractional sizes from the original font, never an earlier fit."""
        if scale >= 1:
            return font
        scale = max(0.8, scale)
        key = (font.GetNativeFontInfoDesc(), scale)
        if key not in self._scaled_font_cache:
            fitted = wx.Font(font)
            fitted.SetFractionalPointSize(font.GetFractionalPointSize() * scale)
            if len(self._scaled_font_cache) >= 64:
                self._scaled_font_cache.clear()
            self._scaled_font_cache[key] = fitted
        return self._scaled_font_cache[key]

    def _fit_text(
        self, dc: wx.DC, font: wx.Font, text: str, available_width: int
    ) -> FittedText:
        """Fit body text against the actual manual or automatically chosen width."""
        key = (font.GetNativeFontInfoDesc(), text, available_width)
        if key not in self._fitted_text_cache:
            fitted = fit_text_suffix(
                text,
                available_width,
                lambda value, scale: self._text_width(
                    dc, self._scaled_text_font(font, scale), value
                ),
            )
            if len(self._fitted_text_cache) >= _TEXT_CACHE_LIMIT:
                self._fitted_text_cache.clear()
            self._fitted_text_cache[key] = fitted
        return self._fitted_text_cache[key]

    def _measure_layout(self) -> None:
        """Measure only custom headers/artwork; leave cell iteration to wx."""
        for field in ("pcb_angle", "correction"):
            attr = gridlib.GridCellAttr()
            attr.SetFont(self.GetDefaultCellFont().Scaled(0.8))
            self.SetColAttr(self.model.column_for(None, field), attr)
        dc = wx.ClientDC(self)
        padding = self.FromDIP(10)  # Match wxGrid's autosizing padding.
        dc.SetFont(self.GetDefaultCellFont())
        self._scrolling_reserve = dc.GetTextExtent("M")[0] * 16
        icon_size = self.flag_bitmap(True).GetWidth()
        row_height = max(icon_size, dc.GetTextExtent("Hg✓—↓↑?")[1]) + self.FromDIP(4)
        self.SetDefaultRowSize(row_height, resizeExistingRows=True)
        fallbacks = {"side": "TB", "type": "BE-", "standard": "✓—", "price": "↓↑-?"}
        empty_widths = (
            {
                key: max(dc.GetTextExtent(char)[0] for char in chars)
                + (self.FromDIP(_SIDE_PADDING_DIP) if key == "side" else padding)
                for key, chars in fallbacks.items()
            }
            if not self.GetNumberRows()
            else {}
        )
        label_font = self.GetLabelFont()
        dc.SetFont(label_font)
        self._group_header_height = (
            max(
                dc.GetTextExtent(self.model.variant_label(variant))[1]
                for variant in self.model.variants
            )
            + padding
        )
        lower_height = 0
        minima = {}
        for column in self.model.columns:
            dc.SetFont(
                label_font.Scaled(_SIDE_HEADER_SCALE)
                if column.key == "side"
                else label_font
            )
            width, height = dc.GetTextExtent(column.label)
            if column.key in _VERTICAL_HEADERS:
                width, height = height, width
            lower_height = max(lower_height, height + padding)
            minimum = width + (
                self.FromDIP(_SIDE_PADDING_DIP) if column.key == "side" else padding
            )
            if column.key in FLAG_FIELDS | {"stock"}:
                minimum = max(minimum, icon_size + padding)
            minima[column.key] = max(minimum, empty_widths.get(column.key, 0))
        self._minimum_widths = minima
        self.SetColLabelSize(self._group_header_height + lower_height)

    def _on_column_auto_size(self, event: gridlib.GridSizeEvent) -> None:
        """Return the resized column, or shared compact field, to content sizing."""
        col = event.GetRowOrCol()
        if col == self.GetNumberFrozenCols() - 1:
            # wx 3.2's double-click hit test can report the preceding edge
            # even though it checks the following column's resize permission.
            return
        if 0 <= col < len(self.model.columns):
            self._column_widths.pop(_width_key(self.model.columns[col]), None)
            self._apply_column_widths()
            self._ensure_frozen()
            self.ForceRefresh()

    def _on_column_size(self, event: gridlib.GridSizeEvent) -> None:
        if event.GetRowOrCol() == self.GetNumberFrozenCols() - 1:
            return
        if not self._resizing_columns:
            col = event.GetRowOrCol()
            key = _width_key(self.model.columns[col])
            self._column_widths[key] = self.ToDIP(self.GetColSize(col))
            self._apply_column_widths()
            self._ensure_frozen()
            self.GetGridColLabelWindow().Refresh()
            frozen_header = self._frozen_col_label_window()
            if frozen_header:
                frozen_header.Refresh()
        event.Skip()

    def _frozen_col_label_window(self) -> Optional[wx.Window]:
        """Find the separate header on both KiCad's wx 3.2 and newer bindings."""
        getter = getattr(self, "GetFrozenColLabelWindow", None)
        if callable(getter):
            return getter()
        frozen = self.GetFrozenColGridWindow()
        if not self.GetNumberFrozenCols() or not frozen:
            return None
        header = self.GetGridColLabelWindow()
        cells_rect, header_rect = frozen.GetRect(), header.GetRect()
        expected = wx.Rect(
            cells_rect.x, header_rect.y, cells_rect.width, header_rect.height
        )
        if expected.width <= 0 or expected.height <= 0:
            return None
        # wx 3.2 CalcWindowSizes places this direct child above the frozen
        # cells. Its Python binding exposes no getter. Do not cache the child:
        # FreezeTo(0, 0) deletes it and the next FreezeTo creates a new window.
        known_windows = (
            header,
            frozen,
            self.GetGridWindow(),
            self.GetGridRowLabelWindow(),
            self.GetGridCornerLabelWindow(),
            self.GetFrozenRowGridWindow(),
            self.GetFrozenCornerGridWindow(),
        )
        matches = [
            child
            for child in self.GetChildren()
            if child not in known_windows
            and child.IsShown()
            and child.GetRect() == expected
        ]
        return matches[0] if len(matches) == 1 else None

    def _set_column_resize_enabled(self, col: int, enabled: bool) -> None:
        """Use native seam locks only when wx can release them after refreezing."""
        enable_resize = getattr(self, "EnableColResize", None)
        if callable(enable_resize):
            if enabled:
                enable_resize(col)
            else:
                self.DisableColResize(col)
        # wx 3.2 cannot release per-column locks. Its seam remains protected by
        # the header mouse handlers and disabled cell-edge resizing instead.

    def _ensure_frozen(self) -> None:
        if not self or self._changing_model or self.GetNumberCols() <= FIXED_COLUMNS:
            return
        if not self.GetNumberRows():
            return
        budget = max(0, self.GetClientSize().width - self._scrolling_reserve)
        total = sum(self.GetColSize(col) for col in range(FIXED_COLUMNS))
        for col in (1, 4, 3, 2, 0):
            if total <= budget:
                break
            minimum = self.GetColMinimalWidth(col)
            old = self.GetColSize(col)
            new = max(minimum, old - (total - budget))
            self.SetColSize(col, new)
            total -= old - new
        # Keep Ref visible when a large font or narrow viewport cannot fit the
        # complete physical prefix. The remaining fields join the scrolled area.
        count = FIXED_COLUMNS if total <= budget else 1
        previous = self.GetNumberFrozenCols()
        if count == 1:
            available = max(
                1,
                self.GetClientSize().width
                - min(self._scrolling_reserve, self.GetClientSize().width // 2),
            )
            self.SetColMinimalWidth(0, 1)
            self.SetColSize(0, min(self.GetColSize(0), available))
        if previous != count:
            if previous:
                self._set_column_resize_enabled(previous - 1, True)
                self.FreezeTo(0, 0)
            self._set_column_resize_enabled(count - 1, False)
        self.FreezeTo(0, count)

        frozen_header = self._frozen_col_label_window()
        if frozen_header and not getattr(frozen_header, "_variant_header_bound", False):
            frozen_header.Bind(wx.EVT_PAINT, self._on_header_paint)
            for mouse_event in (wx.EVT_MOTION, wx.EVT_LEFT_DOWN, wx.EVT_LEFT_DCLICK):
                frozen_header.Bind(mouse_event, self._on_header_seam_mouse)
            frozen_header._variant_header_bound = True
        for window in (self.GetGridWindow(), self.GetFrozenColGridWindow()):
            if window and not getattr(window, "_variant_tooltip_bound", False):
                window.Bind(wx.EVT_MOTION, self._on_motion)
                window._variant_tooltip_bound = True

    def _on_size(self, event: wx.SizeEvent) -> None:
        event.Skip()
        drag = self._header_drag
        if drag is not None and drag.preview is not None:
            # Reclip after native layout, even while column resizing is deferred.
            wx.CallAfter(drag.preview.draw)
        if not self._changing_model:
            self._schedule_resize()

    def _schedule_resize(self) -> None:
        """Coalesce font, DPI and window changes into one layout pass."""
        if not self._resize_pending:
            self._resize_pending = True
            wx.CallAfter(self._resize_viewport)

    def SetDefaultCellFont(self, font: wx.Font) -> None:
        """Recalculate content and artwork after an application font change."""
        super().SetDefaultCellFont(font)
        if hasattr(self, "_resize_pending"):
            self._schedule_resize()

    def SetLabelFont(self, font: wx.Font) -> None:
        """Recalculate both header tiers after a label font change."""
        super().SetLabelFont(font)
        if hasattr(self, "_resize_pending"):
            self._schedule_resize()

    def _on_dpi_changed(self, event: wx.DPIChangedEvent) -> None:
        """Refresh sizing and bitmap resolution when moving between displays."""
        self._sizing_signature = None
        self._reset_text_layout()
        self._schedule_resize()
        event.Skip()

    def _resize_viewport(self) -> None:
        """Reapply preferred widths after temporary narrowing of the frozen area."""
        if self and self._header_drag is not None:
            return
        self._resize_pending = False
        if self and not self._changing_model:
            self._apply_column_widths()
            self._ensure_frozen()

    def _on_theme_changed(self, event: wx.SysColourChangedEvent) -> None:
        self.SetDefaultCellBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOX)
        )
        self.SetDefaultCellTextColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOXTEXT)
        )
        self.SetGridLineColour(
            _blend(self.GetDefaultCellBackgroundColour(), (125, 125, 125), 0.25)
        )
        self.ForceRefresh()
        event.Skip()

    def _on_motion(self, event: wx.MouseEvent) -> None:
        """Read hover text from the current model and native child window."""
        window = event.GetEventObject()
        pos = event.GetPosition()
        offset = self.GetGridWindowOffset(window)
        logical = self.CalcGridWindowUnscrolledPosition(
            pos.x + offset.x, pos.y + offset.y, window
        )
        cell = self.XYToCell(*logical, window)
        target = target_at(self.model, cell.GetRow(), cell.GetCol())
        details = (
            self.model.cell_tooltip(cell.GetRow(), cell.GetCol()) if target else ""
        )
        tooltip = window.GetToolTip()
        if details != (tooltip.GetTip() if tooltip else ""):
            window.SetToolTip(details)
        event.Skip()

    def _on_header_paint(self, event: wx.PaintEvent) -> None:
        """Paint both frozen and scrolling labels using their native window origins.

        KiCad's wx bindings expose DrawColLabels but do not dispatch Python
        overrides. An explicit paint binding was verified in the native gate.
        """
        window = event.GetEventObject()
        dc = wx.PaintDC(window)
        dc.SetBackground(wx.Brush(self.GetLabelBackgroundColour()))
        dc.Clear()
        if not self.GetNumberCols():
            return
        frozen = window == self._frozen_col_label_window()
        grid_window = self.GetFrozenColGridWindow() if frozen else self.GetGridWindow()
        offset = self.GetGridWindowOffset(grid_window)
        x, _ = self.CalcGridWindowUnscrolledPosition(*offset, grid_window)
        dc.SetDeviceOrigin(-x, 0)
        cols = self.CalcColLabelsExposed(window.GetUpdateRegion(), grid_window)
        self._draw_header_labels(dc, cols, x, window.GetClientSize().width)
        if not frozen:
            drag = self._header_drag
            if drag is not None and drag.preview is not None:
                dc.SetDeviceOrigin(0, 0)
                drag.preview._draw(dc, window)
                dc.SetDeviceOrigin(-x, 0)
            self._draw_header_drag(dc, x, window.GetClientSize().width)

    def _draw_header_drag(
        self, dc: wx.DC, left_origin: int, visible_width: int
    ) -> None:
        """Keep the insertion point prominent above the translucent source preview."""
        drag = self._header_drag
        if drag is None or not drag.active or not drag.valid or drag.marker_x is None:
            return
        if visible_width <= 0:
            return
        dc.SetFont(self.GetLabelFont())
        padding = self.FromDIP(6)
        dark = _luminance(self.GetLabelBackgroundColour()) < 0.3
        destination = wx.Colour(0, 235, 255) if dark else wx.Colour(0, 95, 115)
        outline = wx.Colour(0, 0, 0) if dark else wx.Colour(255, 255, 255)
        stroke = max(self.FromDIP(6), self.GetCharHeight() // 2)
        border = max(1, self.FromDIP(2))
        outer_stroke = stroke + border * 2
        inset = min((visible_width - 1) // 2, (outer_stroke + 1) // 2)
        marker = max(
            left_origin + inset,
            min(drag.marker_x, left_origin + visible_width - inset - 1),
        )
        height = self.GetColLabelSize()
        arrow = max(self.FromDIP(9), self.GetCharHeight() * 2 // 3)
        arrow = min(arrow, max(1, height // 3))
        arrow_left = max(left_origin + border, marker - arrow)
        arrow_right = min(left_origin + visible_width - border - 1, marker + arrow)

        # Keep the stationary destination label beside the marker in the field tier.
        text = (
            "Drop at end"
            if drag.before is None
            else f"Drop before {self.model.variant_label(drag.before)}"
        )
        gap = self.FromDIP(4)
        label_right = left_origin + visible_width - gap
        right_start = arrow_right + gap
        left_end = arrow_left - gap
        right_space = max(0, label_right - right_start)
        left_space = max(0, left_end - left_origin - gap)
        desired_width, text_height = dc.GetTextExtent(text)
        label_width = min(desired_width + padding * 2, max(left_space, right_space))
        label_x = right_start if right_space >= label_width else left_end - label_width
        label_y = self._group_header_height + gap
        label_height = min(text_height + gap * 2, height - label_y - border)
        dc.SetBrush(wx.Brush(destination))
        dc.SetTextForeground(outline)
        dc.SetPen(wx.Pen(outline, border))
        if label_width > padding * 2 and label_height > 0:
            destination_rect = wx.Rect(label_x, label_y, label_width, label_height)
            dc.DrawRectangle(destination_rect)
            text = wx.Control.Ellipsize(
                text, dc, wx.ELLIPSIZE_END, label_width - padding * 2
            )
            with wx.DCClipper(dc, destination_rect):
                self.DrawTextRectangle(
                    dc, text, destination_rect, wx.ALIGN_CENTER, wx.ALIGN_CENTER
                )
        dc.SetPen(wx.Pen(outline, outer_stroke))
        dc.DrawLine(marker, border, marker, height - border)
        dc.SetPen(wx.Pen(destination, stroke))
        dc.DrawLine(marker, border, marker, height - border)
        dc.SetPen(wx.Pen(outline, border))
        dc.DrawPolygon(
            [
                (arrow_left, border),
                (arrow_right, border),
                (marker, border + arrow),
            ]
        )
        dc.DrawPolygon(
            [
                (arrow_left, height - border - 1),
                (arrow_right, height - border - 1),
                (marker, height - border - arrow - 1),
            ]
        )

    def _draw_header_labels(
        self, dc: wx.DC, cols: list[int], left_origin: int, visible_width: int
    ) -> None:
        """Share native label drawing with the optional visual inspection probe."""
        label_font = self.GetLabelFont()
        dc.SetFont(label_font)
        dc.SetTextForeground(self.GetLabelTextColour())
        dc.SetBrush(wx.Brush(self.GetLabelBackgroundColour()))
        header_pen = wx.Pen(
            _blend(self.GetLabelBackgroundColour(), (125, 125, 125), 0.4)
        )
        dc.SetPen(header_pen)
        tier = self._group_header_height
        groups: dict[str, tuple[int, int]] = {}
        for index, column in enumerate(self.model.columns):
            if column.variant is not None:
                first, _ = groups.get(column.variant, (index, index))
                groups[column.variant] = (first, index)
        visible_variants = set()
        for col in cols:
            if col < 0:
                continue
            column = self.model.columns[col]
            left, right = self.GetColLeft(col), self.GetColRight(col)
            top = tier if column.variant is not None else 0
            rect = wx.Rect(left, top, right - left, self.GetColLabelSize() - top)
            dc.DrawRectangle(rect)
            dc.SetFont(
                label_font.Scaled(_SIDE_HEADER_SCALE)
                if column.key == "side"
                else label_font.Scaled(_STATUS_WIDTH_SCALE)
                if column.key in _READ_ONLY_STATUS_FIELDS
                else label_font
            )
            if column.key in _VERTICAL_HEADERS:
                width, height = dc.GetTextExtent(column.label)
                with wx.DCClipper(dc, rect):
                    dc.DrawRotatedText(
                        column.label,
                        rect.x + (rect.width - height) // 2,
                        rect.y + (rect.height + width) // 2,
                        90,
                    )
            else:
                self.DrawTextRectangle(
                    dc, column.label, rect, wx.ALIGN_CENTER, wx.ALIGN_CENTER
                )
            if column.variant is not None:
                visible_variants.add(column.variant)
        dc.SetFont(label_font)
        drag = getattr(self, "_header_drag", None)
        for variant in visible_variants:
            first, last = groups[variant]
            left, right = self.GetColLeft(first), self.GetColRight(last)
            rect = wx.Rect(left, 0, right - left, tier)
            moving = drag is not None and drag.active and drag.variant == variant
            output = variant == getattr(self, "output_variant", None)
            dc.SetPen(header_pen)
            dc.SetBrush(
                wx.Brush(
                    wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
                    if moving or output
                    else self.GetLabelBackgroundColour()
                )
            )
            dc.SetTextForeground(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT)
                if moving or output
                else self.GetLabelTextColour()
            )
            dc.DrawRectangle(rect)
            # Center within the visible part so partially clipped groups retain names.
            visible_left = max(left, left_origin)
            visible_right = min(right, left_origin + visible_width)
            label_rect = wx.Rect(
                visible_left, 0, max(0, visible_right - visible_left), tier
            )
            self.DrawTextRectangle(
                dc,
                self.model.variant_label(variant),
                label_rect,
                wx.ALIGN_CENTER,
                wx.ALIGN_CENTER,
            )
            if output:
                # Retain a distinct output cue when another group has the
                # temporary drag highlight. Keep it within the name tier.
                thickness = min(tier, max(1, self.FromDIP(2)))
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.SetBrush(
                    wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT))
                )
                dc.DrawRectangle(
                    wx.Rect(left, tier - thickness, right - left, thickness)
                )
        # Match the native grid's right-edge coordinate and draw after labels
        # so the rule stays continuous through both header tiers.
        dc.SetPen(self._variant_divider_pen())
        for col in range(len(self.model.columns) - 1):
            if self._variant_boundary_after(col):
                boundary = self.GetColRight(col) - 1
                if left_origin <= boundary <= left_origin + visible_width:
                    dc.DrawLine(boundary, 0, boundary, self.GetColLabelSize())
