"""Real wx drawing measurements shared by matrix layout and raster tests."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any, Optional
from unittest.mock import patch

from .variant_model_test_support import Snapshot, State


@contextmanager
def observe_cell_paints(
    view: Any,
    renderer: Any,
    inspect: Optional[Callable[[Any, int, int], Any]] = None,
) -> Iterator[list[Any]]:
    """Observe successful real paints without retaining native drawing contexts."""
    observations, errors = [], []
    original = renderer.Draw

    def draw(
        instance: Any,
        grid: Any,
        attr: Any,
        dc: Any,
        rect: Any,
        row: int,
        col: int,
        selected: bool,
    ) -> None:
        try:
            record = None
            if grid is view:
                record = (
                    inspect(grid, row, col)
                    if inspect
                    else (row, col, tuple(dc.GetClippingBox()))
                )
            original(instance, grid, attr, dc, rect, row, col, selected)
        except BaseException as error:
            errors.append(error)
            raise
        if grid is view:
            observations.append(record)

    with patch.object(renderer, "Draw", draw):
        yield observations
    assert not errors, errors


@dataclass
class Paint:
    """Pixels and observed operations from an actual wx.MemoryDC."""

    image: Any
    rect: Any
    operations: list[dict[str, Any]]

    def of(self, method: str) -> list[dict[str, Any]]:
        """Select observed operations without interpreting production behavior."""
        return [
            operation for operation in self.operations if operation["method"] == method
        ]

    @property
    def background(self) -> tuple[int, int, int]:
        """Read a cell fill pixel outside the content and one-pixel rails."""
        return pixel(self.image, self.rect.x + 3, self.rect.y + 3)


def column_widths(view: Any) -> list[int]:
    """Read the native grid's applied column geometry."""
    return [view.GetColSize(col) for col in range(view.GetNumberCols())]


@dataclass(frozen=True)
class HeaderText:
    """Primitive text observations; native contexts never escape the paint call."""

    text: str
    rect: tuple[int, int, int, int]
    bounds: tuple[int, int, int, int]
    clip: tuple[int, int, int, int]
    size: float
    color: tuple[int, int, int]
    weight: int
    rotation: int


@dataclass(frozen=True)
class HeaderPaint:
    """A native header raster and its independent text observations."""

    image: Any
    text: tuple[HeaderText, ...]


def paint_header(
    view: Any,
    wx: Any,
    columns: Optional[list[int]] = None,
    *,
    origin: int = 0,
    width: Optional[int] = None,
) -> HeaderPaint:
    """Draw both native header tiers, recording ink geometry and their allocations."""
    width = width or view.GetColRight(view.GetNumberCols() - 1) - origin
    bitmap = wx.Bitmap(width, view.GetColLabelSize())
    dc = wx.MemoryDC(bitmap)
    labels: list[HeaderText] = []
    rotate, label = dc.DrawRotatedText, view.DrawTextRectangle

    def record(text: str, rect: Any, bounds: Any, rotation: int = 0) -> None:
        labels.append(
            HeaderText(
                text,
                tuple(rect),
                tuple(bounds),
                tuple(dc.GetClippingBox()[-4:]),
                dc.GetFont().GetFractionalPointSize(),
                tuple(dc.GetTextForeground())[:3],
                dc.GetFont().GetWeight(),
                rotation,
            )
        )

    def rotated(text: str, x: int, y: int, angle: int) -> None:
        w, h = dc.GetTextExtent(text)
        record(text, dc.GetClippingBox()[-4:], (x, y - w, h, w), angle)
        rotate(text, x, y, angle)

    def horizontal(context: Any, text: str, rect: Any, x: int, y: int) -> None:
        w, h = context.GetTextExtent(text)
        record(
            text,
            rect,
            (rect.x + (rect.width - w) // 2, rect.y + (rect.height - h) // 2, w, h),
        )
        label(context, text, rect, x, y)

    try:
        with (
            patch.object(dc, "DrawRotatedText", rotated),
            patch.object(view, "DrawTextRectangle", horizontal),
        ):
            dc.SetBackground(wx.Brush(view.GetLabelBackgroundColour()))
            dc.Clear()
            dc.SetDeviceOrigin(-origin, 0)
            view._draw_header_labels(
                dc,
                list(range(view.GetNumberCols())) if columns is None else columns,
                origin,
                width,
            )
    finally:
        dc.SelectObject(wx.NullBitmap)
    return HeaderPaint(bitmap.ConvertToImage(), tuple(labels))


def sample_model(
    module: ModuleType,
    *,
    rows: int = 3,
    variants: tuple[str, ...] = ("", "A", "B"),
    changes: Optional[dict[tuple[int, str], dict[str, Any]]] = None,
    catalog: Optional[dict[tuple[int, str], dict[str, Any]]] = None,
    show_library: bool = False,
) -> Any:
    """Make literal equal rows; tests state every intentional variant difference."""
    states = tuple(
        replace(
            State(f"component-{row + 1}", f"R{row + 1}", variant),
            **(changes or {}).get((row, variant), {}),
        )
        for row in range(rows)
        for variant in variants
    )
    metadata = {
        (state.component_id, state.variant_name): module.CatalogMetadata(
            **(
                {"lcsc": state.lcsc, "status": "complete"}
                | (catalog or {}).get((row, state.variant_name), {})
            )
        )
        for row in range(rows)
        for state in states[row * len(variants) : (row + 1) * len(variants)]
    }
    return module.MatrixModel(
        Snapshot(states, names=variants),
        enrichment=metadata,
        corrections={
            state.component_id: module.CorrectionState(
                final_angle=(
                    state.pcb_angle
                    if state.side.upper() == "TOP"
                    else 180 - state.pcb_angle
                )
                % 360
            )
            for state in states
            if state.variant_name == ""
        },
        show_footprint_library=show_library,
    )


def outlier_model(module: ModuleType) -> Any:
    """Four ordinary samples and one outlier, with independently shorter B fields."""
    variants = ("", "A", "B")
    changes = {
        (row, variant): {
            "value": ("standard" if row < 4 else "rare_value_" * 12)
            if variant != "B"
            else "B001",
            "footprint": "Library:R_0603" if row < 4 else "Custom_" * 15 + ":R_0603",
            "bom": row == 4,
        }
        for row in range(5)
        for variant in variants
    }
    catalog = {
        (row, variant): {
            "params": (
                ("verbose specification " * 10 if row == 4 else "") + "10kΩ 0603"
            )
            if variant != "B"
            else "B-only"
        }
        for row in range(5)
        for variant in variants
    }
    return sample_model(
        module, rows=5, changes=changes, catalog=catalog, show_library=True
    )


def paint_cell(
    h: Any,
    row: int,
    col: int,
    *,
    width: Optional[int] = None,
    selected: Optional[bool] = None,
) -> Paint:
    """Observe real drawing with wx's raw selection flag, metrics and clipping."""
    view, wx, module = h.view, h.wx, h.view_module
    selected = view.IsInSelection(row, col) if selected is None else selected
    rect = wx.Rect(4, 4, width or view.GetColSize(col) - 1, view.GetRowSize(row) - 1)
    bitmap = wx.Bitmap(rect.width + 8, rect.height + 9)
    dc = wx.MemoryDC(bitmap)
    dc.SetBackground(wx.Brush(wx.Colour(255, 0, 255)))
    dc.Clear()
    operations: list[dict[str, Any]] = []
    attr = module.gridlib.GridCellAttr()
    attr.SetFont(view.GetCellFont(row, col))
    try:
        with ExitStack() as observers:
            for method in (
                "DrawText",
                "DrawLine",
                "DrawPolygon",
                "DrawBitmap",
                "DrawCircle",
            ):
                original = getattr(dc, method)

                def record(
                    *args: Any, _name: str = method, _draw: Any = original
                ) -> None:
                    operations.append(
                        {
                            "method": _name,
                            "args": args,
                            "size": dc.GetFont().GetFractionalPointSize(),
                            "color": tuple(dc.GetTextForeground())[:3],
                            "pen": tuple(dc.GetPen().GetColour())[:3],
                            "weight": dc.GetPen().GetWidth(),
                            "fill": tuple(dc.GetBrush().GetColour())[:3],
                            "clip": tuple(dc.GetClippingBox()),
                            "extent": tuple(dc.GetTextExtent(args[0]))
                            if _name == "DrawText"
                            else None,
                        }
                    )
                    _draw(*args)

                observers.enter_context(patch.object(dc, method, record))
            module.MatrixCellRenderer().Draw(view, attr, dc, rect, row, col, selected)
    finally:
        dc.SelectObject(wx.NullBitmap)
    return Paint(bitmap.ConvertToImage(), rect, operations)


def block_image(h: Any, first: int, last: int) -> Any:
    """Run wxGrid's complete drawing pass, including its native gridline pass."""
    view, wx, grid = h.view, h.wx, h.view_module.gridlib
    width = sum(view.GetColSize(col) for col in range(first, last + 1))
    height = sum(view.GetRowSize(row) for row in range(view.GetNumberRows()))
    bitmap = wx.Bitmap(width, height)
    dc = wx.MemoryDC(bitmap)
    try:
        view.Render(
            dc,
            pos=wx.Point(0, 0),
            size=wx.Size(width, height),
            topLeft=grid.GridCellCoords(0, first),
            bottomRight=grid.GridCellCoords(view.GetNumberRows() - 1, last),
            style=grid.GRID_DRAW_CELL_LINES,
        )
    finally:
        dc.SelectObject(wx.NullBitmap)
    return bitmap.ConvertToImage()


def resize(h: Any, col: int, width: Optional[int] = None) -> None:
    """Deliver a genuine grid size/auto-size event after native width mutation."""
    grid, view = h.view_module.gridlib, h.view
    if width is not None:
        view.SetColSize(col, width)
    kind = grid.wxEVT_GRID_COL_AUTO_SIZE if width is None else grid.wxEVT_GRID_COL_SIZE
    view.GetEventHandler().ProcessEvent(
        grid.GridSizeEvent(view.GetId(), kind, view, col)
    )


def pixel(image: Any, x: int, y: int) -> tuple[int, int, int]:
    """Read native raster RGB without resampling or color conversion."""
    return image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y)


def contrast(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    """Measure WCAG luminance contrast of two observed colors."""

    def luminance(color: tuple[int, ...]) -> float:
        values = [value / 255 for value in color[:3]]
        return sum(
            weight
            * (value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
            for value, weight in zip(values, (0.2126, 0.7152, 0.0722))
        )

    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


def repaired_boundary(
    h: Any, image: Any, first: int, last: int, owner: int, y: int
) -> Any:
    """Repair native clipped cells, retaining wx's raw whole-row selection input."""
    view, wx, module = h.view, h.wx, h.view_module
    bitmap = wx.Bitmap(image)
    dc = wx.MemoryDC(bitmap)
    dc.SetPen(wx.TRANSPARENT_PEN)
    dc.SetBrush(wx.Brush(wx.Colour(255, 0, 255)))
    dc.DrawRectangle(0, y, image.GetWidth(), 1)
    offset = view.GetColLeft(first)
    dc.SetDeviceOrigin(-offset, 0)
    clip = wx.DCClipper(dc, wx.Rect(offset, y, image.GetWidth(), 1))
    attr = module.gridlib.GridCellAttr()
    try:
        for col in range(first, last + 1):
            attr.SetFont(view.GetCellFont(owner, col))
            module.MatrixCellRenderer().Draw(
                view,
                attr,
                dc,
                view.CellToRect(owner, col),
                owner,
                col,
                view.IsInSelection(owner, col),
            )
    finally:
        del clip
        dc.SelectObject(wx.NullBitmap)
    return bitmap.ConvertToImage()
