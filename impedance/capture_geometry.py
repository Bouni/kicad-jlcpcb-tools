"""Pure capture framing shared by region discovery and native rendering."""

from typing import Optional

from .model import Bounds

CAPTURE_WIDTH_PX = 800
CAPTURE_HEIGHT_PX = 420
HIGHLIGHT_PADDING_PX = 16.0
HIGHLIGHT_STROKE_PX = 3.0
HIGHLIGHT_EDGE_GUARD_PX = 1.0
CAPTURE_ZOOM_FACTOR = 0.85
MIN_BOARD_CAPTURE_FRACTION = 0.15


def capture_viewport(
    bounds: Bounds,
    width_px: int = CAPTURE_WIDTH_PX,
    height_px: int = CAPTURE_HEIGHT_PX,
    board_bounds: Optional[Bounds] = None,
) -> tuple[float, float, float, float]:
    """Return the physical crop while keeping fixed-pixel annotation clearance."""
    margin = HIGHLIGHT_PADDING_PX + HIGHLIGHT_STROKE_PX / 2 + HIGHLIGHT_EDGE_GUARD_PX
    if width_px <= 2 * margin or height_px <= 2 * margin:
        raise ValueError("Image dimensions are too small for the selected route box.")
    left, top, right, bottom = bounds
    span_x, span_y = right - left, bottom - top
    padding = max(2_000_000, span_x * 0.15, span_y * 0.15)
    width, height = span_x + 2 * padding, span_y + 2 * padding
    if board_bounds is not None:
        width = max(
            width, (board_bounds[2] - board_bounds[0]) * MIN_BOARD_CAPTURE_FRACTION
        )
        height = max(
            height, (board_bounds[3] - board_bounds[1]) * MIN_BOARD_CAPTURE_FRACTION
        )
    scale = max(
        max(width / width_px, height / height_px) * CAPTURE_ZOOM_FACTOR,
        span_x / (width_px - 2 * margin),
        span_y / (height_px - 2 * margin),
    )
    width, height = width_px * scale, height_px * scale
    return (left + right - width) / 2, (top + bottom - height) / 2, width, height
