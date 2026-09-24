"""Show intended empty-zone boundaries over a read-only native PCB plot."""

from __future__ import annotations

import math
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

# NanoSVG does not render <text>. Keep the physical scale readable in the same
# rasterizer as native KiCad strokes, independent of installed system fonts.
_SCALE_GLYPHS = {
    "0": "M 0,0 H 6 V 10 H 0 Z",
    "1": "M 1,2 L 3,0 V 10 M 0,10 H 6",
    "2": "M 0,0 H 6 V 5 H 0 V 10 H 6",
    "3": "M 0,0 H 6 V 10 H 0 M 0,5 H 6",
    "4": "M 0,0 V 5 H 6 M 6,0 V 10",
    "5": "M 6,0 H 0 V 5 H 6 V 10 H 0",
    "6": "M 6,0 H 0 V 10 H 6 V 5 H 0",
    "7": "M 0,0 H 6 L 1,10",
    "8": "M 0,0 H 6 V 10 H 0 Z M 0,5 H 6",
    "9": "M 6,10 V 0 H 0 V 5 H 6",
    ".": "M 3,9 V 10",
    "m": "M 0,10 V 3 H 3 V 10 M 3,3 H 6 V 10",
    "e": "M 0,6 H 6 V 3 H 0 V 10 H 6",
    "+": "M 0,5 H 6 M 3,2 V 8",
    "-": "M 0,5 H 6",
}


def _scale_length(maximum_mm: float) -> float:
    """Choose the largest 1/2/5 decade length that fits the reserved scale."""
    decade = 10 ** math.floor(math.log10(maximum_mm))
    return next(step * decade for step in (5, 2, 1) if step * decade <= maximum_mm)


def _scale_bar(
    pixels_per_nm: float,
    width_px: int,
    height_px: int,
    board_height: int,
    ui_scale: float,
) -> str:
    """Draw an accurate physical length in a footer that cannot hide the outline."""
    pixels_per_mm = pixels_per_nm * NM_PER_MM
    length_mm = _scale_length(min(160 * ui_scale, width_px * 0.25) / pixels_per_mm)
    x, y = 16 * ui_scale, height_px - 36 * ui_scale
    end = x + length_mm * pixels_per_mm
    label = f"{length_mm:g} mm"
    glyph_scale = 1.3 * ui_scale
    glyphs = []
    for index, character in enumerate(label):
        if character == " ":
            continue
        glyphs.append(
            f'<path d="{_SCALE_GLYPHS[character]}" '
            f'transform="translate({x + index * 10 * ui_scale:.3f},'
            f'{height_px - 26 * ui_scale:.3f}) scale({glyph_scale:.3f})"/>'
        )
    return (
        f'<rect data-copper-zone-scale="footer" x="0" y="{board_height}" '
        f'width="{width_px}" height="{height_px - board_height}" fill="#111827"/>'
        '<g fill="none" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round">'
        f'<line data-copper-zone-scale="bar" x1="{x:.3f}" y1="{y:.3f}" '
        f'x2="{end:.3f}" y2="{y:.3f}"/>'
        f'<path d="M {x:.3f},{y - 4 * ui_scale:.3f} V {y + 4 * ui_scale:.3f} '
        f'M {end:.3f},{y - 4 * ui_scale:.3f} V {y + 4 * ui_scale:.3f}"/>'
        f'<g data-copper-zone-scale="label" aria-label="{label}">'
        + "".join(glyphs)
        + "</g></g>"
    )


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
    ui_scale = min(1.0, width_px / 240, height_px / 120)
    board_height = max(1, round(height_px - 48 * ui_scale))
    viewport = _fit_viewport(
        (left - padding, top - padding, right + padding, bottom + padding),
        width_px,
        board_height,
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
        + _scale_bar(pixels_per_nm, width_px, height_px, board_height, ui_scale)
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
