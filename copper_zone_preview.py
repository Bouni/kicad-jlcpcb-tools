"""Show intended empty-zone boundaries over a read-only native PCB plot."""

from __future__ import annotations

from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any, Optional
from xml.etree import ElementTree as ET

from .copper_zones import EmptyCopperZone
from .impedance.model import Bounds
from .impedance.render import (
    BACKGROUND_COLOR,
    HIGHLIGHT_COLOR,
    NM_PER_MM,
    Rasterizer,
    RenderError,
    SectionRenderer,
    _fit_viewport,
    _native_svg,
    _valid_bounds,
    _validate_png,
    _wx_rasterize,
    board_outline_bounds,
)
from .impedance.service import CapturedImage

PREVIEW_WIDTH = 1000
PREVIEW_HEIGHT = 600


def _geometry_bounds(region: EmptyCopperZone) -> Bounds:
    """Reject absent outlines before plotting any native context."""
    if region.geometry_error:
        raise RenderError(region.geometry_error)
    if (
        not region.contours
        or any(len(set(contour)) < 3 for contour in region.contours)
        or region.bounds is None
        or not _valid_bounds(region.bounds)
    ):
        raise RenderError("The empty copper zone outline is unavailable or invalid.")
    return region.bounds


def zone_preview_svg(
    source: str,
    region: EmptyCopperZone,
    *,
    board_bounds: Optional[Bounds] = None,
    zoom: bool = False,
    background_color: str = BACKGROUND_COLOR,
    width_px: int = PREVIEW_WIDTH,
    height_px: int = PREVIEW_HEIGHT,
) -> str:
    """Frame the PCB and draw every intended boundary without implying a fill."""
    left, top, right, bottom = _geometry_bounds(region)
    if not 1 <= width_px <= 4096 or not 1 <= height_px <= 4096:
        raise RenderError("Image dimensions must be between 1 and 4096 pixels.")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", background_color):
        raise RenderError("The selected PCB background color is invalid.")
    if not zoom and board_bounds is not None and _valid_bounds(board_bounds):
        left, top = min(left, board_bounds[0]), min(top, board_bounds[1])
        right, bottom = max(right, board_bounds[2]), max(bottom, board_bounds[3])
    padding = max(2 * NM_PER_MM, (right - left) * 0.05, (bottom - top) * 0.05)
    viewport = _fit_viewport(
        (left - padding, top - padding, right + padding, bottom + padding),
        width_px,
        height_px,
    )
    root, units_per_mm, _width, _height = _native_svg(source)
    units_per_nm = units_per_mm / NM_PER_MM
    factor = width_px / (viewport.width * units_per_nm)
    transform = (
        f"translate({-viewport.left * units_per_nm * factor:.6f},"
        f"{-viewport.top * units_per_nm * factor:.6f}) scale({factor:.9f})"
    )
    pixels_per_nm = width_px / viewport.width
    highlights = []
    for contour in region.contours:
        points = [
            ((x - viewport.left) * pixels_per_nm, (y - viewport.top) * pixels_per_nm)
            for x, y in contour
        ]
        path = "M " + " L ".join(f"{x:.3f},{y:.3f}" for x, y in points) + " Z"
        highlights.append(
            f'<path data-copper-zone="outline" d="{path}" fill="none" '
            f'stroke="{HIGHLIGHT_COLOR}" stroke-width="3" stroke-linejoin="round"/>'
        )
    body = "".join(ET.tostring(child, encoding="unicode") for child in root)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">'
        f'<rect width="{width_px}" height="{height_px}" fill="{background_color}"/>'
        f'<g data-copper-zone-view="board" transform="{transform}">{body}</g>'
        + "".join(highlights)
        + "</svg>"
    )


def _board_bounds(board: Any) -> Optional[Bounds]:
    """Prefer board edges, falling back to actual item extents rather than paper."""
    bounds = board_outline_bounds(board)
    if bounds is not None:
        return bounds
    getter = getattr(board, "ComputeBoundingBox", None)
    if not callable(getter):
        return None
    box = getter()
    bounds = (
        int(box.GetX()),
        int(box.GetY()),
        int(box.GetX() + box.GetWidth()),
        int(box.GetY() + box.GetHeight()),
    )
    return bounds if _valid_bounds(bounds) else None


class ZonePreviewRenderer:
    """Share safely detached layer plots while the diagnostic dialog is open.

    Existing fill caches are visual context only: diagnostic previews permit
    unfilled or flagged zones and never refill or certify impedance approvals.
    """

    def __init__(
        self,
        board: Any,
        pcbnew_module: Any,
        *,
        rasterizer: Optional[Rasterizer] = None,
    ) -> None:
        self._renderer = SectionRenderer(
            board, pcbnew_module, allow_unfilled_zones=True
        )
        self._layers = {
            layer_id: name for name, layer_id in self._renderer._copper_ids.items()
        }
        self._bounds = _board_bounds(board)
        self._rasterizer = rasterizer or _wx_rasterize

    def __enter__(self) -> ZonePreviewRenderer:
        """Scope all detached allocations to one diagnostic dialog."""
        self._renderer.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Release context even when plotting or display fails."""
        self.close()

    def close(self) -> None:
        """Release cached context once without touching the editor's board."""
        self._renderer.close()

    def render(self, region: EmptyCopperZone, *, zoom: bool = False) -> CapturedImage:
        """Own immutable PNG bytes after all temporary image files are removed."""
        _geometry_bounds(region)
        layer = self._layers.get(region.layer_id)
        if layer is None:
            raise RenderError("The empty copper zone's layer is unavailable.")
        source = zone_preview_svg(
            self._renderer.context_svg(layer),
            region,
            board_bounds=self._bounds,
            zoom=zoom,
            background_color=self._renderer.background_color,
        )
        with TemporaryDirectory(prefix="jlcpcb-empty-zone-preview-") as directory:
            svg, png = Path(directory) / "preview.svg", Path(directory) / "preview.png"
            svg.write_text(source, encoding="utf-8")
            self._rasterizer(svg, png, PREVIEW_WIDTH, PREVIEW_HEIGHT)
            _validate_png(png, PREVIEW_WIDTH, PREVIEW_HEIGHT)
            return CapturedImage.load(png)
