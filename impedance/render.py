"""Render whole-route impedance captures from native KiCad layer plots."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import importlib
import math
from pathlib import Path
import re
import struct
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING, Any, Optional, Union
from xml.etree import ElementTree as ET

from . import capture_geometry
from .board_copy import BoardCopyError, copy_for_render
from .capture_geometry import capture_viewport
from .model import Bounds, Section, Trace
from .palette import PaletteError, ThemeContext, read_theme_context
from .pcbnew_adapter import copper_layers
from .render_context import NONCOPPER_CONTEXT_LAYERS

if TYPE_CHECKING:
    from .spacing_review import SpacingReviewEntry

NM_PER_MM = 1_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BACKGROUND_COLOR = "#10151d"
# Bump matching.CAPTURE_POLICY_REVISION for material framing/highlight changes
# so saved approvals cannot silently authorize a visually different capture.
HIGHLIGHT_COLOR = "#FFFF00"
SPACING_GUIDE_COLOR = "#69E4FF"
SPACING_TICK_COLOR = "#FFFFFF"
SPACING_SPAN_COLOR = "#FFE066"
# Retain the renderer's public constants as explicit compatibility re-exports.
HIGHLIGHT_PADDING_PX = capture_geometry.HIGHLIGHT_PADDING_PX
HIGHLIGHT_STROKE_PX = capture_geometry.HIGHLIGHT_STROKE_PX
HIGHLIGHT_EDGE_GUARD_PX = capture_geometry.HIGHLIGHT_EDGE_GUARD_PX
CAPTURE_ZOOM_FACTOR = capture_geometry.CAPTURE_ZOOM_FACTOR
MIN_BOARD_CAPTURE_FRACTION = capture_geometry.MIN_BOARD_CAPTURE_FRACTION
# Change only native paint RGB: represented strokes move toward the contrasting
# endpoint while surrounding copper moves toward the selected background.
SURROUNDING_COPPER_DIMMING = 0.50
SELECTED_TRACE_CONTRAST = 0.30
_NATIVE_MATCH_TOLERANCE_NM = 100
Rasterizer = Callable[[Path, Path, int, int], None]
_TraceGeometry = tuple[float, tuple[tuple[float, float], ...], bool]
_SHAPES = {"path", "circle", "ellipse", "polyline", "polygon", "rect", "line"}
_PAINT_KEYS = {
    "fill",
    "stroke",
    "stroke-width",
    "fill-opacity",
    "stroke-opacity",
    "stroke-linecap",
    "stroke-linejoin",
    "fill-rule",
    "stroke-dasharray",
    "stroke-dashoffset",
}
_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_POINT = rf"{_NUMBER.pattern}\s*(?:,\s*|\s+){_NUMBER.pattern}"
_LINEAR_PATH = re.compile(
    rf"(?:\s*M\s*{_POINT}(?:\s+{_POINT}|\s*L\s*{_POINT})*(?:\s*Z)?)+\s*"
)
_LINEAR_POINTS = re.compile(rf"\s*{_POINT}(?:\s+{_POINT})*\s*")
_RASTER_PANEL = re.compile(
    r'<svg\b[^>]*\bdata-spacing-raster-panel="[^"]+"[^>]*>.*?</svg>', re.DOTALL
)
_NATIVE_SCENE_LIMIT = 4
_NATIVE_SCENE_BYTES = 64 * 1024 * 1024
_NATIVE_DOCTYPE = re.compile(
    r"<!DOCTYPE\s+svg\s+PUBLIC\s+[\"\']-//W3C//DTD SVG 1\.1//EN[\"\']"
    r"\s+[\"\']http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd[\"\']\s*>"
)
_FATAL_PLOT_STATE: Optional[tuple[Any, Any]] = None
_FATAL_PLOT_MESSAGE = (
    "KiCad could not safely finish the impedance image plot. "
    "Restart KiCad before generating more impedance images. "
    "The uncertain native plot resources are retained until restart."
)


class RenderError(RuntimeError):
    """Report an unavailable renderer or an invalid generated image."""


def _check_native_plot_available() -> None:
    """Stop after a native close failure rather than retry partial teardown."""
    if _FATAL_PLOT_STATE is not None:
        raise RenderError(_FATAL_PLOT_MESSAGE)


@dataclass(frozen=True)
class Viewport:
    """Describe the complete capture in board nanometres."""

    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True)
class _RasterFragment:
    """Own immutable XML and a proved paint bound independently of viewports."""

    head: str
    tail: str
    children: Optional[tuple["_RasterFragment", ...]]
    bounds: Optional[tuple[float, float, float, float]]
    weight: int

    def visible(self, viewport: tuple[float, float, float, float], guard: float) -> str:
        """Select only already-certified bounds; uncertain content stays complete."""
        bounds = self.bounds
        if bounds is not None and (
            bounds[2] + guard < viewport[0]
            or bounds[0] - guard > viewport[2]
            or bounds[3] + guard < viewport[1]
            or bounds[1] - guard > viewport[3]
        ):
            return ""
        if self.children is None:
            return self.head
        return (
            self.head
            + "".join(child.visible(viewport, guard) for child in self.children)
            + self.tail
        )


@dataclass(frozen=True)
class _NativeScene:
    """Own validated geometry and invariant serialized paint for one native layer."""

    source: str
    root_attributes: tuple[tuple[str, str], ...]
    units_per_nm: float
    background: str
    original_body: str
    dimmed_body: str
    opacity: str
    original_fragments: tuple[_RasterFragment, ...]
    dimmed_fragments: tuple[_RasterFragment, ...]
    weight: int


@dataclass(frozen=True)
class _TraceSelection:
    """Select copper paint without coupling an immutable scene to a review item."""

    layer: str
    traces: tuple[Trace, ...] = ()


def _spacing_path(
    start: tuple[float, float],
    end: tuple[float, float],
    viewport: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
    center: Optional[tuple[float, float]] = None,
    sweep: float = 0.0,
) -> str:
    """Project a certified native line/arc without sampled chord approximations."""
    left, top, width, height = viewport
    first = (
        (start[0] - left) / width * width_px,
        (start[1] - top) / height * height_px,
    )
    last = ((end[0] - left) / width * width_px, (end[1] - top) / height * height_px)
    prefix = f"M {first[0]:.6f},{first[1]:.6f} "
    if center is None:
        return prefix + f"L {last[0]:.6f},{last[1]:.6f}"
    radius = math.dist(start, center) / width * width_px
    return (
        prefix
        + f"A {radius:.6f},{radius:.6f} 0 {int(abs(sweep) > math.pi)} {int(sweep > 0)} {last[0]:.6f},{last[1]:.6f}"
    )


def _spacing_annotations(entry: "SpacingReviewEntry") -> str:
    """Burn exact affected spans and target edges into diagnostic PNG evidence."""
    from .spacing_overlays import spacing_guide_paths
    from .spacing_review import spacing_review_spans

    viewport, width_px, height_px = entry.viewport_nm, entry.width_px, entry.height_px
    root = ET.Element("g", {"data-impedance-spacing-annotations": "measured"})

    def physical(point: tuple[float, float]) -> str:
        """Keep physical endpoints inspectable independently of SVG projection."""
        return ",".join(f"{value:.6f}".rstrip("0").rstrip(".") for value in point)

    guides = spacing_guide_paths(entry)
    selected = entry.composition.measurement
    guides = tuple(
        guide
        for guide in guides
        if guide.kind not in ("measured-gap", "target-gap") or guide.section == selected
    )
    for guide in guides:
        edge = guide.facing_edge
        if edge is None:
            continue
        outward = _spacing_path(
            guide.start,
            guide.end,
            viewport,
            width_px,
            height_px,
            guide.center,
            guide.sweep,
        )
        inward = _spacing_path(
            edge.end,
            edge.start,
            viewport,
            width_px,
            height_px,
            edge.center,
            -edge.sweep,
        )
        # The target and source-facing edge share certified native line/arc
        # geometry. Joining their finite endpoints gives a truthful clearance
        # envelope without extrapolating across a bend or unmeasured mate.
        path = (
            outward
            + " L "
            + inward.split("M ", 1)[1].split(" ", 1)[0]
            + " "
            + inward.split(" ", 2)[2]
            + " Z"
        )
        ET.SubElement(
            root,
            "path",
            {
                "data-impedance-spacing-clearance": "certified",
                "d": path,
                "fill": SPACING_GUIDE_COLOR,
                "fill-opacity": "0.18",
                "stroke": "none",
            },
        )

    for span in spacing_review_spans(entry):
        ET.SubElement(
            root,
            "path",
            {
                "data-impedance-spacing-span": span.trace.trace_id,
                "data-board-start-nm": physical(span.start),
                "data-board-end-nm": physical(span.end),
                "d": _spacing_path(
                    span.start,
                    span.end,
                    viewport,
                    width_px,
                    height_px,
                    span.center,
                    span.sweep,
                ),
                "fill": "none",
                "stroke": SPACING_SPAN_COLOR,
                "stroke-width": f"{span.trace.width_nm / viewport[2] * width_px:.6f}",
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
            },
        )
    colored_guides = []
    for guide in guides:
        path = _spacing_path(
            guide.start,
            guide.end,
            viewport,
            width_px,
            height_px,
            guide.center,
            guide.sweep,
        )
        target = guide.kind.startswith("target")
        if guide.kind in ("measured-gap", "target-gap"):
            dx, dy = guide.end[0] - guide.start[0], guide.end[1] - guide.start[1]
            length = math.hypot(dx, dy)
            if length:
                # Visible caps survive the complete image's minimum fitted scale.
                offset = (-dy / length * 8, dx / length * 8)
                for point in (guide.start, guide.end):
                    x = (point[0] - viewport[0]) / viewport[2] * width_px
                    y = (point[1] - viewport[1]) / viewport[3] * height_px
                    path += f" M {x - offset[0]:.6f},{y - offset[1]:.6f} L {x + offset[0]:.6f},{y + offset[1]:.6f}"
        paint = {"d": path, "fill": "none", "stroke-linecap": "round"}
        if target:
            paint["stroke-dasharray"] = "18 10"
        ET.SubElement(
            root, "path", dict(paint, stroke="#10151d", **{"stroke-width": "10"})
        )
        attributes = dict(
            paint,
            stroke=SPACING_GUIDE_COLOR if target else SPACING_TICK_COLOR,
            **{
                "stroke-width": "4",
                "data-impedance-spacing-guide": guide.kind,
                "data-board-start-nm": physical(guide.start),
                "data-board-end-nm": physical(guide.end),
            },
        )
        if guide.start[1] == guide.end[1] and guide.center is None:
            attributes["data-board-y-nm"] = str(guide.start[1])
        colored_guides.append(ET.Element("path", attributes))
    # Local actual and target rulers share a physical origin. Paint all contrast
    # keylines underneath the colored paths, so a later dashed target's dark
    # stroke cannot erase the measured ruler between its cyan dashes.
    root.extend(colored_guides)
    return ET.tostring(root, encoding="unicode")


def _spacing_valid_viewport(
    entry: "SpacingReviewEntry", dimensions: tuple[int, int]
) -> None:
    """Require true proportions for the physical detail rather than its canvas."""
    _left, _top, width, height = entry.viewport_nm
    if (
        not all(math.isfinite(value) for value in entry.viewport_nm)
        or width <= 0
        or height <= 0
        or not math.isclose(width / height, dimensions[0] / dimensions[1], rel_tol=1e-9)
        or not entry.traces
    ):
        raise RenderError("The spacing image has no valid native review viewport.")


def _spacing_panel(
    scene: _NativeScene,
    viewport: tuple[float, float, float, float],
    rect: tuple[int, int, int, int],
    background: str,
    *,
    kind: str,
    annotations: str = "",
    dimmed: bool = False,
    prepared: bool = False,
) -> str:
    """Render one independently clipped native viewport in composite pixels."""
    x, y, width_px, height_px = rect
    left, top, width, _height = viewport
    factor = width_px / (width * scene.units_per_nm)
    transform = (
        f"translate({-left * scene.units_per_nm * factor:.6f},"
        f"{-top * scene.units_per_nm * factor:.6f}) scale({factor:.9f})"
    )
    paint = scene.opacity
    body = scene.dimmed_body if dimmed else scene.original_body
    if prepared:
        # Match the serialized transform actually consumed by NanoSVG, including
        # its existing decimal rounding, before applying the antialiasing guard.
        scale = float(f"{factor:.9f}")
        tx, ty = (
            float(f"{-left * scene.units_per_nm * factor:.6f}"),
            float(f"{-top * scene.units_per_nm * factor:.6f}"),
        )
        visible = (
            -tx / scale,
            -ty / scale,
            (width_px - tx) / scale,
            (height_px - ty) / scale,
        )
        fragments = scene.dimmed_fragments if dimmed else scene.original_fragments
        body = "".join(fragment.visible(visible, 2 / scale) for fragment in fragments)
    return (
        f'<svg x="{x}" y="{y}" width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {width_px} {height_px}" overflow="hidden" '
        f'data-spacing-raster-panel="{kind}" data-spacing-{kind}-panel="true">'
        f'<rect width="{width_px}" height="{height_px}" fill="{background}"/>'
        f'<g data-impedance-spacing-view="{kind}"{paint} transform="{transform}">{body}</g>'
        f"{annotations}</svg>"
    )


def _spacing_locator(entry: "SpacingReviewEntry") -> str:
    """Highlight the complete concern and locate its selected physical detail."""
    from .spacing_review import spacing_review_spans

    left, top, width, height = entry.composition.overview_viewport_nm
    detail_left, detail_top, detail_width, detail_height = entry.viewport_nm
    pixels_w, pixels_h = entry.composition.overview_rect[2:]
    root = ET.Element("g", {"data-spacing-overview-concern": entry.issue_id})
    highlights = []
    for span in spacing_review_spans(entry):
        # This overview locates the complete affected native interval. A minimum
        # visible line weight makes a long run recognisable at Fit Image; the
        # separate physical detail retains true copper widths and proportions.
        attributes = {
            "d": _spacing_path(
                span.start,
                span.end,
                (left, top, width, height),
                pixels_w,
                pixels_h,
                span.center,
                span.sweep,
            ),
            "fill": "none",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        }
        line_width = max(8, span.trace.width_nm / width * pixels_w)
        ET.SubElement(
            root,
            "path",
            dict(
                attributes,
                stroke="#10151d",
                **{"stroke-width": f"{line_width + 6:.6f}"},
            ),
        )
        highlights.append(
            ET.Element(
                "path",
                dict(
                    attributes,
                    stroke=SPACING_SPAN_COLOR,
                    **{
                        "stroke-width": f"{line_width:.6f}",
                        "data-spacing-overview-span": span.trace.trace_id,
                        "data-board-start-nm": ",".join(
                            f"{value:.6f}".rstrip("0").rstrip(".")
                            for value in span.start
                        ),
                        "data-board-end-nm": ",".join(
                            f"{value:.6f}".rstrip("0").rstrip(".") for value in span.end
                        ),
                    },
                ),
            )
        )
    locator = {
        "x": f"{(detail_left - left) / width * pixels_w:.6f}",
        "y": f"{(detail_top - top) / height * pixels_h:.6f}",
        "width": f"{detail_width / width * pixels_w:.6f}",
        "height": f"{detail_height / height * pixels_h:.6f}",
        "fill": "none",
    }
    ET.SubElement(
        root,
        "rect",
        dict(
            locator,
            stroke="#10151d",
            **{"data-spacing-overview-locator": "true", "stroke-width": "10"},
        ),
    )
    root.extend(highlights)
    ET.SubElement(
        root,
        "rect",
        dict(
            locator,
            stroke=SPACING_TICK_COLOR,
            **{"data-spacing-overview-locator": "outline", "stroke-width": "4"},
        ),
    )
    return ET.tostring(root, encoding="unicode")


def _spacing_gap_deviation(actual: Optional[float], target: Optional[float]) -> str:
    """Compare the unrounded signed copper gap with a valid positive target."""
    if (
        actual is None
        or not math.isfinite(actual)
        or target is None
        or not math.isfinite(target)
        or target <= 0
    ):
        return "Unavailable"
    deviation = (actual - target) / target * 100
    if not math.isfinite(deviation):
        return "Unavailable"
    if deviation == 0:
        return "0.0% at target"
    direction = "below" if deviation < 0 else "above"
    magnitude = abs(deviation)
    percentage = "<0.1" if magnitude < 0.1 else f"{magnitude:.1f}"
    return f"{percentage}% {direction} target"


def _spacing_summary_svg(entry: "SpacingReviewEntry") -> str:
    """Keep fit-readable mm values and the target-relative gap outside copper."""
    from .spacing_composition import spacing_label

    def mm(value: float) -> str:
        """Use mm consistently, retaining meaningful nanometre-sized differences."""
        if not math.isfinite(value):
            return "Unavailable"
        return f"{value / NM_PER_MM:.6f}".rstrip("0").rstrip(".") + " mm"

    composition = entry.composition
    actual, target = composition.actual_gap_nm, entry.target_spacing_nm
    metric_x, metric_y, metric_width, _metric_height = composition.metrics_rect
    actual_x, actual_y, actual_value_y = metric_x, metric_y, metric_y + 48
    target_x, target_y, target_value_y = metric_x, metric_y + 100, metric_y + 148
    delta_x, delta_y, delta_value_y = metric_x, metric_y + 200, metric_y + 248
    if composition.metric_columns:
        actual_value_y = target_value_y = delta_value_y = metric_y + 60
        target_y = delta_y = metric_y
        target_x = metric_x + metric_width / (3 if actual is not None else 2)
        delta_x = metric_x + 2 * metric_width / 3
    region_x, region_y, _region_width, _region_height = composition.region_rect
    footer_x, footer_y, _footer_width, _footer_height = composition.schematic_rect
    parts = [
        spacing_label(
            "Worst gap - true proportions"
            if entry.representative is not None
            else "Physical detail - true proportions",
            16,
            8,
        ),
        spacing_label(
            "Affected run",
            composition.overview_rect[0],
            composition.overview_label_top,
        ),
    ]
    for x, y, width, height in (composition.detail_rect, composition.overview_rect):
        parts.append(
            f'<rect x="{x - 1}" y="{y - 1}" width="{width + 2}" '
            f'height="{height + 2}" fill="none" stroke="#617185" stroke-width="2"/>'
        )
    if actual is not None and target is not None:
        difference = actual - target
        parts.extend(
            (
                spacing_label("Actual gap", actual_x, actual_y),
                spacing_label(mm(actual), actual_x, actual_value_y, size=48),
                spacing_label(
                    "Too close by" if difference < 0 else "Wider by",
                    delta_x,
                    delta_y,
                    color=SPACING_SPAN_COLOR,
                ),
                spacing_label(
                    mm(abs(difference)),
                    delta_x,
                    delta_value_y,
                    size=48,
                    color=SPACING_SPAN_COLOR,
                ),
            )
        )
    else:
        parts.append(spacing_label("See region gap range", metric_x, metric_y))
    if target is not None:
        parts.extend(
            (
                spacing_label(
                    "Target gap", target_x, target_y, color=SPACING_GUIDE_COLOR
                ),
                spacing_label(
                    mm(target),
                    target_x,
                    target_value_y,
                    size=48,
                    color=SPACING_GUIDE_COLOR,
                ),
            )
        )
    gap = composition.region_gap_nm
    label = "Unavailable"
    if gap is not None:
        label = (
            mm(gap.lower_nm)
            if gap.lower_nm == gap.upper_nm
            else (mm(gap.lower_nm).removesuffix(" mm") + "-" + mm(gap.upper_nm))
        )
    parts.extend(
        (
            spacing_label("Region gap range", region_x, region_y),
            spacing_label(label, region_x + 420, region_y, size=48),
        )
    )
    parts.extend(
        (
            spacing_label("Gap deviation from target", footer_x, footer_y),
            spacing_label(
                _spacing_gap_deviation(actual, target),
                footer_x,
                footer_y + 64,
                size=72,
                color=SPACING_SPAN_COLOR,
            ),
        )
    )
    return "".join(parts)


def _fit_viewport(
    bounds: tuple[float, float, float, float], width_px: int, height_px: int
) -> Viewport:
    """Expand a rectangle to the image aspect without stretching geometry."""
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    aspect = width_px / height_px
    if width / height < aspect:
        width = height * aspect
    else:
        height = width / aspect
    return Viewport(
        (left + right - width) / 2, (top + bottom - height) / 2, width, height
    )


def _valid_bounds(bounds: Bounds) -> bool:
    """Require a finite, positive physical rectangle, including negative origins."""
    return (
        len(bounds) == 4
        and all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in bounds
        )
        and bounds[2] > bounds[0]
        and bounds[3] > bounds[1]
    )


def section_viewport(
    section: Section,
    width_px: int,
    height_px: int,
    board_bounds: Optional[Bounds] = None,
) -> Viewport:
    """Tighten the previous contextual crop by 15%, keeping the whole box safely in view."""
    if not 1 <= width_px <= 4096 or not 1 <= height_px <= 4096:
        raise RenderError("Image dimensions must be between 1 and 4096 pixels.")
    if not section.traces or not _valid_bounds(section.bounds):
        raise RenderError("The PCB section has no valid geometry.")
    if board_bounds is not None and not _valid_bounds(board_bounds):
        raise RenderError("The PCB outline has invalid bounds.")
    try:
        return Viewport(
            *capture_viewport(section.bounds, width_px, height_px, board_bounds)
        )
    except ValueError as error:
        raise RenderError(
            "Image dimensions are too small to contain the selected route box."
        ) from error


def _svg_mm(value: str) -> float:
    """Read a physical SVG dimension emitted by KiCad's SVG plotter."""
    match = re.fullmatch(r"\s*([\d.eE+-]+)\s*(mm|cm|in)\s*", value)
    if match is None:
        raise RenderError("KiCad SVG must declare its physical page dimensions.")
    number = float(match.group(1)) * {"mm": 1, "cm": 10, "in": 25.4}[match.group(2)]
    if not math.isfinite(number) or number <= 0:
        raise RenderError("KiCad SVG has invalid page dimensions.")
    return number


def _native_svg(source: str) -> tuple[ET.Element, float, float, float]:
    """Validate a locally generated physical SVG without loading external data."""
    # KiCad emits this standard declaration even for standalone SVG plots. Strip
    # only its known external declaration: custom DTDs/internal entities remain
    # prohibited and no network access is needed to parse the document.
    source = _NATIVE_DOCTYPE.sub("", source, count=1)
    if "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        raise RenderError("KiCad SVG must not contain document entities.")
    try:
        root = ET.fromstring(source)  # noqa: S314 -- Generated SVG, entities rejected above.
    except ET.ParseError as error:
        raise RenderError("KiCad did not produce a complete SVG image.") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise RenderError("KiCad did not produce an SVG image.")
    try:
        viewbox = [
            float(value)
            for value in re.split(r"[\s,]+", root.attrib["viewBox"].strip())
        ]
        width_mm = _svg_mm(root.attrib["width"])
        height_mm = _svg_mm(root.attrib["height"])
    except (KeyError, ValueError) as error:
        raise RenderError(
            "KiCad SVG page coordinates are missing or invalid."
        ) from error
    if (
        len(viewbox) != 4
        or not all(math.isfinite(value) for value in viewbox)
        or viewbox[:2] != [0, 0]
    ):
        raise RenderError("KiCad SVG uses an unsupported page origin.")
    units_per_mm = viewbox[2] / width_mm
    if units_per_mm <= 0 or not math.isclose(
        units_per_mm, viewbox[3] / height_mm, rel_tol=1e-5
    ):
        raise RenderError("KiCad SVG uses non-uniform physical scale.")
    # Native CLI plots include invisible searchable text beside the actual
    # stroked geometry. NanoSVG does not render text; discard only text proven
    # invisible, retaining all corresponding native stroke paths.
    for parent in root.iter():
        for node in list(parent):
            if node.tag.rsplit("}", 1)[-1] == "text":
                paint = _style(node)
                if paint.get("opacity") == "0" or (
                    (
                        paint.get("fill", "black") == "none"
                        or paint.get("fill-opacity") == "0"
                    )
                    and (
                        paint.get("stroke", "none") == "none"
                        or paint.get("stroke-opacity") == "0"
                    )
                ):
                    parent.remove(node)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
        if node.tag in {"script", "image", "foreignObject", "svg"} and node is not root:
            raise RenderError(
                "KiCad SVG contains unsupported nested or external content."
            )
        if node.tag == "text":
            raise RenderError(
                "KiCad SVG must plot reference and value text as strokes."
            )
        for key in list(node.attrib):
            if key.startswith("{http://www.w3.org/1999/xlink}"):
                node.set("xlink:" + key.split("}", 1)[1], node.attrib.pop(key))
    return root, units_per_mm, width_mm, height_mm


def context_layer_names(
    copper_layer_names: tuple[str, ...], active_layer: str
) -> tuple[str, ...]:
    """Choose only signal-layer copper plus both faces' noncopper context."""
    if active_layer not in copper_layer_names:
        raise RenderError(f"Copper layer {active_layer} is no longer enabled.")
    if active_layer not in {"F.Cu", "B.Cu"}:
        match = re.fullmatch(r"In([1-9]\d*)\.Cu", active_layer)
        if match is None or int(match.group(1)) > 30:
            raise RenderError(
                f"PCB copper layer {active_layer} is not a canonical layer name."
            )
    return (active_layer,) + NONCOPPER_CONTEXT_LAYERS


def _style(node: ET.Element) -> dict[str, str]:
    """Read native SVG presentation properties with inline-style precedence."""
    result = {name: node.attrib[name] for name in _PAINT_KEYS if name in node.attrib}
    for item in node.get("style", "").split(";"):
        if ":" in item:
            name, value = item.split(":", 1)
            result[name.strip()] = value.strip()
    if "opacity" in node.attrib and "opacity" not in result:
        result["opacity"] = node.attrib["opacity"]
    return result


def _opacity(value: str) -> float:
    """Require bounded native opacity rather than silently losing drawing content."""
    try:
        number = float(value)
    except ValueError as error:
        raise RenderError("KiCad SVG contains invalid opacity.") from error
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise RenderError("KiCad SVG contains invalid opacity.")
    return number


def _paint(node: ET.Element, properties: dict[str, str]) -> ET.Element:
    """Materialize resolved paint once on an exclusively owned native shape."""
    # NanoSVG replaces inherited opacity. Bake alpha into each paint channel.
    effective = _opacity(properties.get("opacity", "1"))
    properties.update(
        opacity="1",
        **{
            "fill-opacity": f"{effective * _opacity(properties.get('fill-opacity', '1')):.6f}",
            "stroke-opacity": f"{effective * _opacity(properties.get('stroke-opacity', '1')):.6f}",
        },
    )
    for name in _PAINT_KEYS | {"opacity"}:
        node.attrib.pop(name, None)
    node.set(
        "style", ";".join(f"{key}:{value}" for key, value in sorted(properties.items()))
    )
    return node


def _flatten_native(
    root: ET.Element, prefix: str
) -> tuple[list[tuple[ET.Element, dict[str, str]]], list[ET.Element]]:
    """Flatten inherited paint/transforms and namespace each layer's native IDs."""
    ids = {
        node.attrib["id"]: f"{prefix}-{node.attrib['id']}"
        for node in root.iter()
        if "id" in node.attrib
    }
    for node in root.iter():
        for key, value in list(node.attrib.items()):
            if key == "id":
                node.set(key, ids[value])
            else:
                for old, new in ids.items():
                    value = value.replace(f"url(#{old})", f"url(#{new})")
                    if key in {"href", "xlink:href"} and value == f"#{old}":
                        value = f"#{new}"
                node.set(key, value)
    leaves: list[tuple[ET.Element, dict[str, str]]] = []
    definitions: list[ET.Element] = []

    def visit(
        node: ET.Element,
        inherited: dict[str, str],
        transforms: tuple[str, ...],
        opacity: float,
    ) -> None:
        """Resolve inherited paint on owned geometry without intermediate XML paint."""
        if node.tag == "defs":
            definitions.append(node)
            return
        properties = dict(inherited)
        own = _style(node)
        opacity *= _opacity(own.pop("opacity", "1"))
        properties.update(own)
        transform = node.get("transform")
        if transform:
            transforms += (transform,)
        if node.tag in _SHAPES:
            properties["opacity"] = f"{opacity:.6f}"
            # Keep the native attribute order while postponing paint serialization.
            node.set("style", "")
            if transforms:
                node.set("transform", " ".join(transforms))
            leaves.append((node, properties))
        else:
            for child in node:
                visit(child, properties, transforms, opacity)

    visit(root, {}, (), 1.0)
    return leaves, definitions


def _large_copper_fill(node: ET.Element, units_per_mm: float) -> bool:
    """Recognize broad filled polygons for the native default zone opacity."""
    if node.tag == "polygon":
        numbers = [float(value) for value in _NUMBER.findall(node.get("points", ""))]
    elif node.tag == "path":
        data = node.get("d", "")
        if re.search(r"[A-HJ-KN-YZa-hj-kn-yz]", data.replace("Z", "").replace("z", "")):
            return False
        numbers = [float(value) for value in _NUMBER.findall(data)]
    elif node.tag == "rect":
        return (
            float(node.get("width", "0"))
            * float(node.get("height", "0"))
            / units_per_mm**2
            >= 4
        )
    else:
        return False
    if len(numbers) < 6 or len(numbers) % 2:
        return False
    xs, ys = numbers[::2], numbers[1::2]
    return (max(xs) - min(xs)) * (max(ys) - min(ys)) / units_per_mm**2 >= 4


def _dimmed_color(
    color: str, background: tuple[int, int, int], dimming_factor: float
) -> str:
    """Apply PCB_RENDER_SETTINGS' DIMMED RGB mix without changing paint alpha."""
    if color == "none" or color.startswith("url(#"):
        return color
    if color in {"black", "white"}:
        color = {"black": "#000000", "white": "#ffffff"}[color]
    # KiCad's SVG plotter writes six-digit hex colors and separate alpha channels.
    # Reject unknown paint rather than quietly retaining an undimmed layer.
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise RenderError("KiCad SVG contains an unsupported native paint color.")
    channels = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(
        round(channel * (1 - dimming_factor) + backdrop * dimming_factor)
        for channel, backdrop in zip(channels, background)
    )
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _dimmed_definitions(
    definitions: list[ET.Element],
    background: tuple[int, int, int],
    dimming_factor: float,
) -> None:
    """Keep layer-local gradient definitions consistent with dimmed paint refs."""
    for definition in definitions:
        for node in definition.iter():
            if node.tag != "stop":
                continue
            paint = _style(node)
            color = paint.get("stop-color", node.get("stop-color", "black"))
            paint["stop-color"] = _dimmed_color(color, background, dimming_factor)
            node.attrib.pop("stop-color", None)
            node.set(
                "style",
                ";".join(f"{key}:{value}" for key, value in sorted(paint.items())),
            )


def compose_layer_svgs(
    sources: Mapping[str, str],
    active_layer: str,
    *,
    background_color: str,
    dimming_factor: float = 0.8,
    zone_opacity: float = 0.6,
) -> str:
    """Show selected-layer native copper with dimmed noncopper board context.

    Inactive copper plots are excluded, even when callers supply the whole stack.
    Tracks, zones, pads and vias come only from the selected signal-layer plot;
    through-hole items remain only where KiCad plots them on that selected layer.
    Silkscreen and outline colors mix toward the user's background.
    This hidden-copper/dim-context policy is not a pixel-identical GAL Dim view.
    Broad fills use KiCad's default zone opacity (0.6), not undocumented live
    canvas state or persisted project-local display preferences.
    """
    if not isinstance(background_color, str) or not re.fullmatch(
        r"#[0-9a-fA-F]{6}", background_color
    ):
        raise RenderError("The selected PCB background color is invalid.")
    for value, description in (
        (dimming_factor, "inactive-layer dimming factor"),
        (zone_opacity, "zone opacity"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
            or not math.isfinite(value)
        ):
            raise RenderError(f"The selected PCB {description} is invalid.")
    background = tuple(
        int(background_color[index : index + 2], 16) for index in (1, 3, 5)
    )
    if active_layer not in sources:
        raise RenderError("The active copper layer is missing from the native plots.")
    active, units_per_mm, width_mm, height_mm = _native_svg(sources[active_layer])
    copper = tuple(name for name in sources if name.endswith(".Cu"))
    # Whitelist sources independently of the renderer's plot list: gallery and
    # CLI callers can supply cached plots of every copper layer. Do not parse,
    # copy definitions from, or composite any inactive copper into this image.
    order = context_layer_names(copper, active_layer)
    definitions: list[ET.Element] = []
    roles: dict[str, list[tuple[str, ET.Element]]] = {
        "plane": [],
        "pad": [],
        "reference": [],
        "active": [],
        "edge": [],
    }
    for index, name in enumerate(order):
        if name not in sources:
            continue
        root, scale, width, height = (
            (active, units_per_mm, width_mm, height_mm)
            if name == active_layer
            else _native_svg(sources[name])
        )
        if not all(
            math.isclose(first, second, rel_tol=1e-6)
            for first, second in (
                (scale, units_per_mm),
                (width, width_mm),
                (height, height_mm),
            )
        ):
            raise RenderError(
                "Native KiCad layers use different physical page coordinates."
            )
        leaves, defs = _flatten_native(root, f"native-{index}")
        if name != active_layer:
            _dimmed_definitions(defs, background, dimming_factor)
        definitions.extend(defs)
        for leaf, properties in leaves:
            if name != active_layer:
                properties.update(
                    fill=_dimmed_color(
                        properties.get("fill", "black"), background, dimming_factor
                    ),
                    stroke=_dimmed_color(
                        properties.get("stroke", "none"), background, dimming_factor
                    ),
                )
                role = "edge" if name == "Edge.Cuts" else "reference"
                roles[role].append((name, _paint(leaf, properties)))
                continue
            filled = properties.get("fill", "black") != "none"
            stroked = properties.get("stroke", "none") != "none"
            if filled:
                plane = _large_copper_fill(leaf, units_per_mm)
                alpha = zone_opacity if plane else 1.0
                fill = deepcopy(leaf) if stroked else leaf
                fill_properties = dict(properties) if stroked else properties
                fill_properties.update(
                    stroke="none",
                    **{
                        "fill-opacity": f"{_opacity(properties.get('fill-opacity', '1')) * alpha:.6f}"
                    },
                )
                if "id" in fill.attrib:
                    fill.set("id", fill.attrib["id"] + "-fill")
                roles["plane" if plane else "pad"].append(
                    (name, _paint(fill, fill_properties))
                )
            if stroked:
                properties["fill"] = "none"
                if "id" in leaf.attrib:
                    leaf.set("id", leaf.attrib["id"] + "-stroke")
                roles["active"].append((name, _paint(leaf, properties)))
    result = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "xmlns:xlink": "http://www.w3.org/1999/xlink",
            "width": active.attrib["width"],
            "height": active.attrib["height"],
            "viewBox": active.attrib["viewBox"],
            "data-impedance-inactive-layer-mode": "hidden-copper-dimmed-context",
            "data-impedance-inactive-copper-visibility": "hidden",
            "data-impedance-context-layer-mode": "dimmed",
            "data-impedance-active-copper-layer": active_layer,
            "data-impedance-dimming-factor": f"{dimming_factor:.6f}",
            "data-impedance-zone-opacity": f"{zone_opacity:.6f}",
        },
    )
    result.extend(definitions)
    for role, records in roles.items():
        for name in order:
            nodes = [node for layer, node in records if layer == name]
            if not nodes:
                continue
            group = ET.SubElement(
                result,
                "g",
                {
                    "data-impedance-layer": name,
                    "data-impedance-role": role,
                    "opacity": "1",
                },
            )
            group.extend(nodes)
    return ET.tostring(result, encoding="unicode")


def _highlight_box(
    section: Section, viewport: Viewport, width_px: int, height_px: int
) -> str:
    """Enclose all selected copper with one visible, unfilled yellow rectangle."""
    factor = width_px / viewport.width
    left, top, right, bottom = section.bounds
    padding_px, stroke_px = HIGHLIGHT_PADDING_PX, HIGHLIGHT_STROKE_PX
    x = (left - viewport.left) * factor - padding_px
    y = (top - viewport.top) * factor - padding_px
    width = (right - left) * factor + 2 * padding_px
    height = (bottom - top) * factor + 2 * padding_px
    half_stroke = stroke_px / 2
    if (
        x <= half_stroke
        or y <= half_stroke
        or x + width >= width_px - half_stroke
        or y + height >= height_px - half_stroke
    ):
        raise RenderError("The selected route box is outside the capture viewport.")
    return (
        f'<rect data-impedance-highlight="box" x="{x:.3f}" y="{y:.3f}" '
        f'width="{width:.3f}" height="{height:.3f}" fill="none" '
        f'stroke="{HIGHLIGHT_COLOR}" stroke-width="{stroke_px:.1f}" '
        'stroke-linejoin="miter" opacity="1" stroke-opacity="1"/>'
    )


def _identity_native_transform(transform: str) -> bool:
    """Recognize KiCad's identity wrapper without guessing transformed geometry."""
    pattern = re.compile(r"([A-Za-z]+)\(([^()]*)\)")
    if pattern.sub("", transform).strip():
        return False
    for match in pattern.finditer(transform):
        try:
            values = tuple(
                float(value) for value in re.split(r"[\s,]+", match[2].strip())
            )
        except ValueError:
            return False
        if match[1] == "translate":
            if values not in ((0.0,), (0.0, 0.0)):
                return False
        elif match[1] == "scale":
            if values not in ((1.0,), (1.0, 1.0)):
                return False
        else:
            return False
    return True


def _native_stroke_geometry(node: ET.Element) -> tuple[str, tuple[float, ...]]:
    """Read only the native untransformed line/circular-arc stroke primitives."""
    if not _identity_native_transform(node.get("transform", "")):
        return "", ()
    try:
        if node.tag == "line":
            return "line", tuple(
                float(node.attrib[key]) for key in ("x1", "y1", "x2", "y2")
            )
        if node.tag == "polyline":
            values = tuple(
                float(value) for value in _NUMBER.findall(node.get("points", ""))
            )
            if len(values) >= 4 and len(values) % 2 == 0:
                return "line", values
        if node.tag == "path":
            data = node.get("d", "")
            commands = re.findall(r"[A-DF-Za-df-z]", _NUMBER.sub("", data))
            values = tuple(float(value) for value in _NUMBER.findall(data))
            if (
                commands
                and commands[0] == "M"
                and all(command == "L" for command in commands[1:])
            ):
                if len(values) >= 4 and len(values) % 2 == 0:
                    return "line", values
            if commands == ["M", "A"] and len(values) == 9:
                return "arc", values
    except (KeyError, ValueError):
        pass
    return "", ()


def _native_arc_matches(
    values: tuple[float, ...], points: tuple[tuple[float, float], ...], tolerance: float
) -> bool:
    """Prove the native circular SVG arc follows the sampled route's actual side."""
    start_x, start_y, radius_x, radius_y, rotation, large, sweep, end_x, end_y = values
    if (
        radius_x <= 0
        or abs(radius_x - radius_y) > tolerance
        or rotation != 0
        or large not in (0, 1)
        or sweep not in (0, 1)
        or len(points) < 3
    ):
        return False
    half_x, half_y = (start_x - end_x) / 2, (start_y - end_y) / 2
    distance_squared = half_x**2 + half_y**2
    if distance_squared == 0 or distance_squared > (radius_x + tolerance) ** 2:
        return False
    coefficient = math.sqrt(max(0, radius_x**2 / distance_squared - 1))
    if large == sweep:
        coefficient = -coefficient
    center_x = (start_x + end_x) / 2 + coefficient * half_y
    center_y = (start_y + end_y) / 2 - coefficient * half_x
    start_angle = math.atan2(start_y - center_y, start_x - center_x)
    end_angle = math.atan2(end_y - center_y, end_x - center_x)
    span = (
        (end_angle - start_angle) % math.tau
        if sweep
        else (start_angle - end_angle) % math.tau
    )
    angle_tolerance = tolerance / radius_x
    for x, y in points:
        if abs(math.hypot(x - center_x, y - center_y) - radius_x) > tolerance:
            return False
        angle = math.atan2(y - center_y, x - center_x)
        progress = (
            (angle - start_angle) if sweep else (start_angle - angle)
        ) % math.tau
        # Serialization can move a start endpoint just across the angle wrap.
        if progress > span + angle_tolerance and progress < math.tau - angle_tolerance:
            return False
    return True


def _represented_stroke(
    node: ET.Element,
    paint: Mapping[str, str],
    traces: tuple[_TraceGeometry, ...],
    tolerance: float,
) -> bool:
    """Match exact native track geometry and width, never its surrounding corridor."""
    kind, values = _native_stroke_geometry(node)
    if not kind or not all(math.isfinite(value) for value in values):
        return False
    try:
        width = float(paint.get("stroke-width", "0"))
    except ValueError:
        return False
    if not math.isfinite(width) or width <= 0:
        return False
    native_end = values[-2:]
    native_start = values[:2]
    for trace_width, points, native_arc in traces:
        if abs(width - trace_width) > tolerance:
            continue
        direct = all(
            abs(a - b) <= tolerance
            for a, b in zip(native_start + native_end, points[0] + points[-1])
        )
        reverse = all(
            abs(a - b) <= tolerance
            for a, b in zip(native_start + native_end, points[-1] + points[0])
        )
        if not (direct or reverse):
            continue
        if kind == "arc":
            if native_arc and _native_arc_matches(values, points, tolerance):
                return True
        elif not native_arc and len(values) == len(points) * 2:
            ordered = points if direct else tuple(reversed(points))
            if all(
                abs(a - b) <= tolerance
                for a, b in zip(
                    values, (coordinate for point in ordered for coordinate in point)
                )
            ):
                return True
    return False


def _luminance(color: tuple[int, int, int]) -> float:
    """Measure relative luminance after decoding the native sRGB channels."""
    linear = tuple(
        channel / 255 / 12.92
        if channel / 255 <= 0.04045
        else ((channel / 255 + 0.055) / 1.055) ** 2.4
        for channel in color
    )
    return sum(
        channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722))
    )


def _emphasized_color(color: str, background_luminance: float) -> str:
    """Choose the actual 30% white/black mix that best contrasts with the canvas."""
    if color == "none" or color.startswith("url(#"):
        return color
    candidates = tuple(
        _dimmed_color(color, endpoint, SELECTED_TRACE_CONTRAST)
        for endpoint in ((255, 255, 255), (0, 0, 0))
    )

    def contrast(candidate: str) -> float:
        """Compare the mixed native stroke rather than its unattainable endpoint."""
        luminance = _luminance(
            tuple(int(candidate[index : index + 2], 16) for index in (1, 3, 5))
        )
        return (max(luminance, background_luminance) + 0.05) / (
            min(luminance, background_luminance) + 0.05
        )

    return max(candidates, key=contrast)


def _emphasize_native_traces(
    root: ET.Element,
    section: Union[Section, "SpacingReviewEntry", _TraceSelection],
    units_per_nm: float,
    background_color: str,
) -> None:
    """Boost represented strokes and dim surrounding copper without changing alpha.

    Native SVG plots do not carry track UUIDs. Accept only complete matching
    stroke primitives; unsupported/ambiguous geometry stays subdued. In
    particular, never restore pads, zones or parallel routes inside a mask.
    """
    background = tuple(
        int(background_color[index : index + 2], 16) for index in (1, 3, 5)
    )
    background_luminance = _luminance(background)
    composed = root.get("data-impedance-active-copper-layer") is not None
    traces = tuple(
        (
            trace.width_nm * units_per_nm,
            tuple((x * units_per_nm, y * units_per_nm) for x, y in trace.points),
            trace.is_native_arc,
        )
        for trace in section.traces
    )
    tolerance = units_per_nm * _NATIVE_MATCH_TOLERANCE_NM

    def visit(
        node: ET.Element,
        role: str,
        layer: str,
        inherited: dict[str, str],
        transformed: bool,
    ) -> None:
        """Preserve context groups and apply native inherited copper paint to leaves."""
        if node.tag == "defs":
            return
        role = node.get("data-impedance-role", role)
        layer = node.get("data-impedance-layer", layer)
        paint = dict(inherited)
        paint.update(_style(node))
        transformed = transformed or not _identity_native_transform(
            node.get("transform", "")
        )
        if node.tag in _SHAPES and (not composed or layer == section.layer):
            selected = (
                role == "active"
                and paint.get("fill", "black") == "none"
                and not transformed
                and _represented_stroke(node, paint, traces, tolerance)
            )
            emphasized = dict(paint)
            if selected:
                emphasized["stroke"] = _emphasized_color(
                    paint.get("stroke", "none"), background_luminance
                )
            else:
                emphasized["fill"] = _dimmed_color(
                    paint.get("fill", "black"),
                    background,
                    SURROUNDING_COPPER_DIMMING,
                )
                emphasized["stroke"] = _dimmed_color(
                    paint.get("stroke", "none"),
                    background,
                    SURROUNDING_COPPER_DIMMING,
                )
            # This tree belongs only to this annotation. Keep native geometry
            # attributes in place instead of cloning each leaf twice for paint.
            for name in _PAINT_KEYS | {"opacity"}:
                node.attrib.pop(name, None)
            node.set(
                "style",
                ";".join(f"{key}:{value}" for key, value in sorted(emphasized.items())),
            )
        # Opacity composites an element and its descendants; it is not inherited
        # paint. Copying it onto a child would apply the same group alpha twice.
        child_paint = dict(paint)
        child_paint.pop("opacity", None)
        for child in node:
            visit(child, role, layer, child_paint, transformed)

    visit(root, "active", section.layer if not composed else "", {}, False)


def annotated_svg(
    source: str,
    section: Section,
    width_px: int = 800,
    height_px: int = 420,
    board_bounds: Optional[Bounds] = None,
    background_color: str = BACKGROUND_COLOR,
) -> str:
    """Frame native traces with contrasting paint over subdued copper and a yellow box."""
    if not isinstance(background_color, str) or not re.fullmatch(
        r"#[0-9a-fA-F]{6}", background_color
    ):
        raise RenderError("The selected PCB background color is invalid.")
    root, units_per_mm, _width, _height = _native_svg(source)
    if not any(node.tag in _SHAPES for node in root.iter()):
        raise RenderError("KiCad SVG contains no PCB geometry.")
    viewport = section_viewport(section, width_px, height_px, board_bounds)
    units_per_nm = units_per_mm / NM_PER_MM
    _emphasize_native_traces(root, section, units_per_nm, background_color)
    factor = width_px / (viewport.width * units_per_nm)
    transform = (
        f"translate({-viewport.left * units_per_nm * factor:.6f},"
        f"{-viewport.top * units_per_nm * factor:.6f}) scale({factor:.9f})"
    )
    # The source SVG root is replaced by the capture canvas. Retain its own
    # compositing opacity on PCB content only, outside all original child groups.
    source_opacity = _style(root).get("opacity")
    content_opacity = (
        f' opacity="{_opacity(source_opacity):.6f}"'
        if source_opacity is not None
        else ""
    )
    body = "".join(ET.tostring(child, encoding="unicode") for child in root)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">'
        f'<rect width="{width_px}" height="{height_px}" fill="{background_color}"/>'
        f'<g data-impedance-view="segment"{content_opacity} transform="{transform}">{body}</g>'
        + _highlight_box(section, viewport, width_px, height_px)
        + "</svg>"
    )


def board_outline_bounds(board: Any) -> Optional[Bounds]:
    """Read PCB extents without modifying the board or substituting plot page size."""
    getter = getattr(board, "GetBoardEdgesBoundingBox", None)
    if getter is None:
        return None
    box = getter()
    bounds = (
        int(box.GetX()),
        int(box.GetY()),
        int(box.GetX() + box.GetWidth()),
        int(box.GetY() + box.GetHeight()),
    )
    return bounds if _valid_bounds(bounds) else None


def _native_shape_bounds(
    node: ET.Element,
) -> Optional[tuple[float, float, float, float]]:
    """Bound only certified native primitives; retain every unfamiliar command."""
    try:
        if node.tag in {"circle", "ellipse"}:
            x, y = float(node.get("cx", "0")), float(node.get("cy", "0"))
            rx = float(node.attrib["r" if node.tag == "circle" else "rx"])
            ry = rx if node.tag == "circle" else float(node.attrib["ry"])
            if rx < 0 or ry < 0:
                return None
            bounds = x - rx, y - ry, x + rx, y + ry
        elif node.tag == "rect":
            x, y = float(node.get("x", "0")), float(node.get("y", "0"))
            width, height = float(node.attrib["width"]), float(node.attrib["height"])
            if width < 0 or height < 0:
                return None
            bounds = x, y, x + width, y + height
        elif node.tag == "line":
            x1, y1, x2, y2 = (
                float(node.get(key, "0")) for key in ("x1", "y1", "x2", "y2")
            )
            bounds = min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
        elif node.tag in {"path", "polyline", "polygon"}:
            data = node.get("d" if node.tag == "path" else "points", "")
            if node.tag == "path":
                # Implicit line-to coordinate pairs after M are native plotter
                # output. Curves, arcs, relative commands and malformed syntax
                # remain untouched rather than guessing extrema.
                if (
                    re.sub(r"[MLZ\s,]", "", _NUMBER.sub("", data))
                    or _LINEAR_PATH.fullmatch(data) is None
                ):
                    return None
            elif _LINEAR_POINTS.fullmatch(data) is None:
                return None
            values = tuple(float(value) for value in _NUMBER.findall(data))
            if len(values) < 4 or len(values) % 2:
                return None
            xs, ys = values[::2], values[1::2]
            bounds = min(xs), min(ys), max(xs), max(ys)
        else:
            return None
    except (KeyError, ValueError, OverflowError):
        return None
    return bounds if all(math.isfinite(value) for value in bounds) else None


def _pruning_paint(node: ET.Element, inherited: Mapping[str, str]) -> dict[str, str]:
    """Retain every property needed to prove safe bounds, including inline style."""
    paint = dict(inherited)
    local = _style(node)
    for name in (
        "stroke-miterlimit",
        "filter",
        "mask",
        "clip-path",
        "vector-effect",
        "marker",
        "marker-start",
        "marker-mid",
        "marker-end",
    ):
        if name in node.attrib and name not in local:
            local[name] = node.attrib[name]
    paint.update(local)
    return paint


def _prepare_raster_fragment(
    node: ET.Element, inherited: Mapping[str, str]
) -> _RasterFragment:
    """Resolve inherited geometry/paint once, retaining opaque uncertain subtrees."""
    paint = _pruning_paint(node, inherited)
    uncertain = (
        "id" in node.attrib
        or node.tag in {"defs", "svg"}
        or not _identity_native_transform(node.get("transform", ""))
        or not _identity_native_transform(paint.get("transform", ""))
        or any(
            paint.get(name, "none") != "none"
            for name in (
                "filter",
                "mask",
                "clip-path",
                "vector-effect",
                "marker",
                "marker-start",
                "marker-mid",
                "marker-end",
            )
        )
    )
    if node.tag == "g" and not uncertain:
        children = tuple(_prepare_raster_fragment(child, paint) for child in node)
        head, tail = _raster_group_markup(node)
        return _RasterFragment(
            head,
            tail,
            children,
            None,
            256 + len((head + tail).encode()) + sum(child.weight for child in children),
        )
    bounds = None if uncertain else _native_shape_bounds(node)
    if bounds is not None:
        try:
            stroke = (
                float(paint.get("stroke-width", "1"))
                if paint.get("stroke", "none") != "none"
                else 0.0
            )
            miter = max(4.0, float(paint.get("stroke-miterlimit", "4")))
            padding = stroke * miter
            if math.isfinite(padding) and stroke >= 0:
                bounds = (
                    bounds[0] - padding,
                    bounds[1] - padding,
                    bounds[2] + padding,
                    bounds[3] + padding,
                )
            else:
                bounds = None
        except (ValueError, OverflowError):
            bounds = None
    content = ET.tostring(node, encoding="unicode")
    return _RasterFragment(content, "", None, bounds, 256 + len(content.encode()))


def _repaint_raster_fragment(
    fragment: _RasterFragment, node: ET.Element
) -> _RasterFragment:
    """Share certified geometry with the paint-only dimmed version of the tree."""
    if fragment.children is None:
        content = ET.tostring(node, encoding="unicode")
        return _RasterFragment(
            content, "", None, fragment.bounds, 256 + len(content.encode())
        )
    children = tuple(
        _repaint_raster_fragment(child, native)
        for child, native in zip(fragment.children, node)
    )
    head, tail = _raster_group_markup(node)
    return _RasterFragment(
        head,
        tail,
        children,
        fragment.bounds,
        256 + len((head + tail).encode()) + sum(child.weight for child in children),
    )


def _raster_group_markup(node: ET.Element) -> tuple[str, str]:
    """Keep group text and tails escaped by the same serializer as native leaves."""
    shallow = ET.Element(node.tag, node.attrib)
    shallow.text, shallow.tail = node.text, node.tail
    ending = f"</{node.tag}>"
    head, _closing, tail = ET.tostring(
        shallow, encoding="unicode", short_empty_elements=False
    ).partition(ending)
    return head, ending + tail


def _prune_panel_geometry(panel: ET.Element) -> None:
    """Skip provably invisible native geometry before NanoSVG transforms paths.

    The emitted SVG remains complete. This private raster copy alone loses
    primitive leaves whose conservative painted bounds cannot touch the canvas.
    Unknown transforms, effects, curves, definitions and stroke units stay intact.
    """
    try:
        width, height = float(panel.attrib["width"]), float(panel.attrib["height"])
    except (KeyError, ValueError):
        return
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        return
    for content in panel:
        if not (
            content.get("data-impedance-spacing-view")
            or content.get("data-impedance-view")
        ):
            continue
        match = re.fullmatch(
            r"translate\(([-+\d.eE]+),([-+\d.eE]+)\) scale\(([-+\d.eE]+)\)",
            content.get("transform", ""),
        )
        if match is None:
            continue
        if not _identity_native_transform(_style(content).get("transform", "")):
            continue
        tx, ty, scale = (float(value) for value in match.groups())
        if not all(math.isfinite(value) for value in (tx, ty, scale)) or scale <= 0:
            continue
        viewport = -tx / scale, -ty / scale, (width - tx) / scale, (height - ty) / scale

        def prune(
            parent: ET.Element,
            inherited: dict[str, str],
            viewport: tuple[float, float, float, float] = viewport,
            scale: float = scale,
        ) -> None:
            """Respect native inherited paint and leave unknown subtrees alone."""
            for node in list(parent):
                if (
                    "id" in node.attrib
                    or node.tag == "defs"
                    or not _identity_native_transform(node.get("transform", ""))
                ):
                    continue
                paint = _pruning_paint(node, inherited)
                if not _identity_native_transform(paint.get("transform", "")):
                    continue
                if any(
                    paint.get(name, "none") != "none"
                    for name in (
                        "filter",
                        "mask",
                        "clip-path",
                        "vector-effect",
                        "marker",
                        "marker-start",
                        "marker-mid",
                        "marker-end",
                    )
                ):
                    continue
                if node.tag in {"g", "svg"}:
                    # A nested SVG introduces its own viewport. Native composed
                    # scenes prohibit it, but conservatively retain future input.
                    if node.tag == "g":
                        prune(node, paint)
                    continue
                bounds = _native_shape_bounds(node)
                if bounds is None:
                    continue
                try:
                    stroke = (
                        float(paint.get("stroke-width", "1"))
                        if paint.get("stroke", "none") != "none"
                        else 0.0
                    )
                    miter = max(4.0, float(paint.get("stroke-miterlimit", "4")))
                except (ValueError, OverflowError):
                    continue
                if not math.isfinite(stroke * miter) or stroke < 0:
                    continue
                # Twice the default miter extension, round/square caps and two
                # output pixels of antialiasing guard remain inside this bound.
                padding = stroke * miter + 2 / scale
                if (
                    bounds[2] + padding < viewport[0]
                    or bounds[0] - padding > viewport[2]
                    or bounds[3] + padding < viewport[1]
                    or bounds[1] - padding > viewport[3]
                ):
                    parent.remove(node)

        prune(content, _style(content))


def _raster_svg_namespaces(root: ET.Element) -> None:
    """Keep literal NanoSVG namespace spellings without changing global XML state."""
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
        for key in tuple(node.attrib):
            if key.startswith("{http://www.w3.org/1999/xlink}"):
                node.set("xlink:" + key.rsplit("}", 1)[-1], node.attrib.pop(key))
    root.set("xmlns", "http://www.w3.org/2000/svg")
    root.set("xmlns:xlink", "http://www.w3.org/1999/xlink")


def _wx_rasterize(
    source: Path,
    destination: Path,
    width_px: int,
    height_px: int,
    *,
    prepared_panels: Optional[tuple[str, ...]] = None,
    prepared_native: bool = False,
) -> None:
    """Rasterize native stroked SVG without touching the live editor canvas."""
    try:
        wx = importlib.import_module("wx")
        svg = importlib.import_module("wx.svg")
    except ImportError as error:
        raise RenderError("Impedance images require KiCad's wx.svg support.") from error
    try:
        from .spacing_review import SPACING_RENDER_POLICY_REVISION

        content = source.read_text(encoding="utf-8")
        if "xlink:" in content and "xmlns:xlink=" not in content.partition(">")[0]:
            # Historical Original SVGs relied on NanoSVG's literal-prefix
            # parser. Bind that same spelling in the private XML copy before
            # ElementTree reads it, without changing emitted source or pixels.
            content = re.sub(
                r"<svg\b",
                '<svg xmlns:xlink="http://www.w3.org/1999/xlink"',
                content,
                count=1,
            )
        if prepared_panels is not None:
            replacements = iter(prepared_panels)
            if len(_RASTER_PANEL.findall(content)) != len(prepared_panels):
                raise RenderError(
                    "Prepared spacing panels differ from the capture composition."
                )
            content = _RASTER_PANEL.sub(lambda match: next(replacements), content)
        root = (
            ET.fromstring(content)  # noqa: S314 -- Local renderer SVG.
            if f'data-spacing-composition="{SPACING_RENDER_POLICY_REVISION}"' in content
            else None
        )
        if root is not None:
            _raster_svg_namespaces(root)
            panels = [child for child in root if child.get("data-spacing-raster-panel")]
        else:
            panels = []
        if root is not None and panels:
            # NanoSVG has no reliable nested viewport clipping. Rasterize each
            # physical viewport independently, then paste its bounded bitmap.
            # NanoSVG does not draw fonts. Preserve SVG label descriptors and
            # paint them into the same final bitmap with native antialiasing.
            labels = []
            for parent in root.iter():
                for node in list(parent):
                    if node.tag == "text" and node.get("data-spacing-label"):
                        labels.append(node)
                        parent.remove(node)
            for panel in panels:
                root.remove(panel)
            image = svg.SVGimage.CreateFromBytes(ET.tostring(root))
            bitmap = image.ConvertToBitmap(width=width_px, height=height_px)
            dc = wx.MemoryDC(bitmap)
            try:
                for panel in panels:
                    x, y = int(panel.attrib.pop("x")), int(panel.attrib.pop("y"))
                    panel_width, panel_height = (
                        int(panel.get("width")),
                        int(panel.get("height")),
                    )
                    _raster_svg_namespaces(panel)
                    if prepared_panels is None and not prepared_native:
                        _prune_panel_geometry(panel)
                    panel_image = svg.SVGimage.CreateFromBytes(ET.tostring(panel))
                    panel_bitmap = panel_image.ConvertToBitmap(
                        width=panel_width, height=panel_height
                    )
                    if not panel_bitmap.IsOk():
                        raise RenderError(
                            "KiCad could not draw a spacing review panel."
                        )
                    dc.DrawBitmap(panel_bitmap, x, y, True)
                dc.SetBackgroundMode(wx.TRANSPARENT)
                for label in labels:
                    size = round(float(label.get("font-size", "18")))
                    font = wx.Font(
                        wx.Size(0, size),
                        wx.FONTFAMILY_SWISS,
                        wx.FONTSTYLE_NORMAL,
                        wx.FONTWEIGHT_BOLD
                        if label.get("font-weight") == "bold"
                        else wx.FONTWEIGHT_NORMAL,
                    )
                    dc.SetFont(font)
                    dc.SetTextForeground(wx.Colour(label.get("fill", "#FFFFFF")))
                    text = label.text or ""
                    x = round(float(label.get("x", "0")))
                    y = round(float(label.get("data-spacing-text-top", "0")))
                    text_width, text_height = dc.GetTextExtent(text)
                    if (
                        x < 0
                        or y < 0
                        or x + text_width > width_px
                        or y + text_height > height_px
                    ):
                        raise RenderError(
                            "The spacing image labels do not fit the required composition."
                        )
                    dc.DrawText(text, x, y)
            finally:
                dc.SelectObject(wx.NullBitmap)
        else:
            if prepared_native:
                image = svg.SVGimage.CreateFromBytes(content.encode())
            elif (
                "data-impedance-spacing-view" in content
                or "data-impedance-view" in content
            ):
                root = ET.fromstring(content)  # noqa: S314 -- Local renderer SVG.
                _raster_svg_namespaces(root)
                for panel in root.iter("svg"):
                    if prepared_panels is None and panel.get(
                        "data-spacing-raster-panel"
                    ):
                        _prune_panel_geometry(panel)
                if prepared_panels is None:
                    _prune_panel_geometry(root)
                image = svg.SVGimage.CreateFromBytes(ET.tostring(root))
            else:
                image = svg.SVGimage.CreateFromFile(str(source))
            bitmap = image.ConvertToBitmap(width=width_px, height=height_px)
        if not bitmap.IsOk() or not bitmap.SaveFile(
            str(destination), wx.BITMAP_TYPE_PNG
        ):
            raise RenderError("KiCad could not save the impedance image.")
    except (AttributeError, RuntimeError, ValueError) as error:
        raise RenderError(
            f"KiCad could not rasterize the impedance image: {error}"
        ) from error


_NATIVE_RASTERIZER = _wx_rasterize


def _validate_png(path: Path, width_px: int, height_px: int) -> None:
    """Reject missing output, invalid PNG signatures and incorrect dimensions."""
    try:
        with path.open("rb") as source:
            header = source.read(24)
    except OSError as error:
        raise RenderError("The impedance renderer did not create an image.") from error
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise RenderError("The impedance renderer created an invalid PNG.")
    if struct.unpack(">II", header[16:24]) != (width_px, height_px):
        raise RenderError("The impedance renderer created an incorrectly sized PNG.")


def _check_plot_transform(controller: Any) -> None:
    """Reject an actual backend origin or mirror inconsistent with the native crop."""
    if not hasattr(controller, "GetPlotter"):
        return
    plotter = controller.GetPlotter()
    if plotter is None:
        raise RenderError("KiCad did not initialize its SVG plotter.")
    if hasattr(plotter, "GetPlotOffsetUserUnits"):
        offset = plotter.GetPlotOffsetUserUnits()
        if int(offset.x) != 0 or int(offset.y) != 0:
            raise RenderError("KiCad SVG plotting applied an unexpected board origin.")
    if hasattr(plotter, "GetPlotMirrored") and plotter.GetPlotMirrored():
        raise RenderError("KiCad SVG plotting unexpectedly mirrored the PCB.")


def _configured_color_theme(pcbnew_module: Any) -> str:
    """Read the selected native theme, with an actionable settings failure."""
    try:
        return read_theme_context(pcbnew_module).name
    except PaletteError as error:
        raise RenderError(str(error)) from error


def _native_color_settings(
    pcbnew_module: Any,
    options: Any = None,
    source_options: Any = None,
    *,
    theme_name: Optional[str] = None,
) -> Any:
    """Resolve the configured theme through KiCad, never an unloaded plot palette.

    PCB_PLOT_PARAMS.ColorSettings() is non-null even for its unloaded dummy
    palette, so neither source nor detached plot options identify usable colors.
    Native manager lookup also preserves KiCad's own missing-theme fallback.
    """
    theme = _configured_color_theme(pcbnew_module) if theme_name is None else theme_name
    try:
        manager_getter = getattr(pcbnew_module, "GetSettingsManager", None)
        manager = manager_getter() if callable(manager_getter) else None
        getter = getattr(manager, "GetColorSettings", None)
        if not callable(getter):
            getter = getattr(pcbnew_module, "GetColorSettings", None)
        palette = getter(theme) if callable(getter) else None
        if palette is not None:
            return palette
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise RenderError(
            f"KiCad could not load the selected color palette: {theme}"
        ) from error
    raise RenderError(f"KiCad could not load the selected color palette: {theme}")


class SectionRenderer:
    """Retain completed copper contexts and shared noncopper plots for one revision."""

    def __init__(
        self,
        board: Any,
        pcbnew_module: Any,
        rasterizer: Optional[Rasterizer] = None,
        *,
        allow_unfilled_zones: bool = False,
        appearance: Optional[ThemeContext] = None,
    ) -> None:
        _check_native_plot_available()
        self.board = board
        self.pcbnew = pcbnew_module
        self._rasterizer = rasterizer or _wx_rasterize
        self._use_native_panels = (
            rasterizer is None and self._rasterizer is _NATIVE_RASTERIZER
        )
        self._allow_unfilled_zones = allow_unfilled_zones
        self._copper_ids = copper_layers(board, pcbnew_module)
        self._layer_ids = dict(self._copper_ids)
        for name in NONCOPPER_CONTEXT_LAYERS:
            layer_id = getattr(pcbnew_module, name.replace(".", "_"), None)
            if layer_id is not None:
                self._layer_ids[name] = int(layer_id)
        self._layer_svg: dict[str, str] = {}
        self._native_scenes: dict[str, _NativeScene] = {}
        self.render_timings: dict[str, dict[str, Union[int, float]]] = {}
        self._plot_owner: Any = None
        self._plot_board: Any = None
        self._board_bounds: Optional[Bounds] = None
        self._closed = False
        self._quarantined = False
        self._theme = appearance

    def __enter__(self) -> "SectionRenderer":
        """Keep detached native allocations scoped to one preview or export."""
        _check_native_plot_available()
        if self._closed:
            raise RenderError("The impedance renderer has already been closed.")
        return self

    def __exit__(self, *exc: Any) -> None:
        """Release the detached board after native plotters are safely closed."""
        self.close()

    def close(self) -> None:
        """Release a cached detached allocation without changing the source board."""
        if self._closed:
            return
        self._closed = True
        if self._quarantined:
            # Keep one uncertain resource pair strongly referenced by the fuse.
            # Never retry native close, delete its board, or toggle SWIG ownership.
            return
        owner = self._plot_owner
        self._plot_board = None
        self._plot_owner = None
        self._board_bounds = None
        self._layer_svg.clear()
        self._native_scenes.clear()
        self._theme = None
        if owner is not None:
            owner.close()

    def _theme_context(self) -> ThemeContext:
        """Use one selected theme and background consistently throughout a capture."""
        if self._theme is None:
            try:
                self._theme = read_theme_context(self.pcbnew)
            except PaletteError as error:
                raise RenderError(str(error)) from error
        return self._theme

    def _board_for_plot(self) -> Any:
        """Snapshot current geometry once; native plotting only touches the copy."""
        _check_native_plot_available()
        if self._closed:
            raise RenderError("The impedance renderer has already been closed.")
        if self._plot_board is None:
            try:
                if self._allow_unfilled_zones:
                    self._plot_owner = copy_for_render(
                        self.board, self.pcbnew, allow_unfilled_zones=True
                    )
                else:
                    self._plot_owner = copy_for_render(self.board, self.pcbnew)
                self._plot_board = self._plot_owner.board
                self._board_bounds = board_outline_bounds(self._plot_board)
            except BoardCopyError as error:
                raise RenderError(str(error)) from error
        return self._plot_board

    def _check_appearance(self) -> None:
        """Reject native layer pixels from a different persisted appearance."""
        expected = self._theme_context()
        try:
            current = read_theme_context(self.pcbnew)
        except PaletteError as error:
            raise RenderError(str(error)) from error
        if current != expected:
            raise RenderError(
                "The PCB appearance changed during capture. Review the updated images."
            )

    def _plot_layer(self, layer: str) -> str:
        """Plot one native layer with independent options, colors and stroked text."""
        if layer not in self._layer_ids:
            raise RenderError(f"PCB layer {layer} is unavailable.")
        if not hasattr(self.pcbnew, "PLOT_CONTROLLER") or not hasattr(
            self.pcbnew, "PLOT_FORMAT_SVG"
        ):
            raise RenderError("This KiCad version does not support SVG plotting.")
        if not hasattr(self.pcbnew, "PLOT_TEXT_MODE_STROKE"):
            raise RenderError(
                "KiCad SVG plotting requires stroked reference/value text."
            )
        self._check_appearance()
        with TemporaryDirectory(prefix="jlcpcb-impedance-plot-") as directory:
            controller = self.pcbnew.PLOT_CONTROLLER(self._board_for_plot())
            options: Any = None
            try:
                options = controller.GetPlotOptions()
                settings = {
                    "SetOutputDirectory": directory,
                    "SetFormat": self.pcbnew.PLOT_FORMAT_SVG,
                    "SetUseAuxOrigin": False,
                    "SetScale": 1.0,
                    "SetScaleSelection": 1,
                    "SetAutoScale": False,
                    "SetMirror": False,
                    "SetNegative": False,
                    "SetPlotFrameRef": False,
                    "SetPlotValue": True,
                    "SetPlotReference": True,
                    "SetPlotFPText": True,
                    "SetBlackAndWhite": False,
                    "SetTextMode": self.pcbnew.PLOT_TEXT_MODE_STROKE,
                    "SetSvgFitPageToBoard": False,
                    "SetA4Output": False,
                    "SetFineScaleAdjustX": 1.0,
                    "SetFineScaleAdjustY": 1.0,
                    "SetWidthAdjust": 0,
                    "SetExcludeEdgeLayer": True,
                }
                required = {
                    "SetOutputDirectory",
                    "SetFormat",
                    "SetUseAuxOrigin",
                    "SetScale",
                    "SetAutoScale",
                    "SetMirror",
                    "SetNegative",
                    "SetPlotFrameRef",
                    "SetTextMode",
                    "SetPlotValue",
                    "SetPlotReference",
                    "SetPlotFPText",
                }
                for name, value in settings.items():
                    setter = getattr(options, name, None)
                    if setter is not None:
                        setter(value)
                    elif name in required:
                        raise RenderError(f"KiCad SVG plotting requires {name}.")
                set_palette = getattr(options, "SetColorSettings", None)
                if not callable(set_palette):
                    raise RenderError("KiCad SVG plotting requires SetColorSettings.")
                palette = _native_color_settings(
                    self.pcbnew, theme_name=self._theme_context().name
                )
                set_palette(palette)
                controller.SetLayer(self._layer_ids[layer])
                if not controller.OpenPlotfile(
                    "impedance", self.pcbnew.PLOT_FORMAT_SVG, "Controlled impedance"
                ):
                    raise RenderError("KiCad could not open the impedance SVG plot.")
                if not hasattr(controller, "SetColorMode"):
                    raise RenderError(
                        "KiCad SVG plotting requires native color output."
                    )
                controller.SetColorMode(True)
                _check_plot_transform(controller)
                if not controller.PlotLayer():
                    raise RenderError("KiCad could not plot the impedance PCB context.")
                filename = Path(str(controller.GetPlotFileName()))
            finally:
                try:
                    controller.ClosePlot()
                except Exception as error:
                    global _FATAL_PLOT_STATE  # noqa: PLW0603 -- Process-wide native safety fuse.
                    self._quarantined = True
                    _FATAL_PLOT_STATE = (self, controller)
                    raise RenderError(_FATAL_PLOT_MESSAGE) from error
                finally:
                    # A traceback can retain a closed wrapper. KiCad's controller
                    # destructor does not access BOARD after successful ClosePlot.
                    options = None
                    controller = None
            if not filename.is_absolute():
                filename = Path(directory) / filename
            try:
                source = filename.read_text(encoding="utf-8")
            except OSError as error:
                raise RenderError(
                    "KiCad did not create the impedance SVG plot."
                ) from error
            self._check_appearance()
            return source

    @property
    def background_color(self) -> str:
        """Return the same native background used for layer composition."""
        return self._theme_context().background

    def context_svg(self, layer: str) -> str:
        """Compose each copper context once, retaining common raw noncopper plots."""
        _check_native_plot_available()
        if self._closed:
            raise RenderError("The impedance renderer has already been closed.")
        names = context_layer_names(tuple(self._copper_ids), layer)
        if layer in self._layer_svg:
            return self._layer_svg[layer]
        sources = {layer: self._plot_layer(layer)}
        for name in names:
            if name == layer or name not in self._layer_ids:
                continue
            if name not in self._layer_svg:
                self._layer_svg[name] = self._plot_layer(name)
            sources[name] = self._layer_svg[name]
        context = compose_layer_svgs(
            sources,
            layer,
            background_color=self._theme_context().background,
            dimming_factor=self._theme_context().dimming_factor,
        )
        # Copper entries contain completed contexts only. Keeping raw copper here
        # would turn a failed composition into an apparently usable cache hit.
        self._layer_svg[layer] = context
        return context

    def viewport(
        self, section: Section, width_px: int = 800, height_px: int = 420
    ) -> tuple[float, float, float, float]:
        """Expose the physical frame used by the native capture, without guessing."""
        self._board_for_plot()
        value = section_viewport(section, width_px, height_px, self._board_bounds)
        return value.left, value.top, value.width, value.height

    def svg(self, section: Section, width_px: int = 800, height_px: int = 420) -> str:
        """Return the same full-frame native capture used by preview and workbook."""
        started = perf_counter()
        result = annotated_svg(
            self.context_svg(section.layer),
            section,
            width_px,
            height_px,
            board_bounds=self._board_bounds,
            background_color=self._theme_context().background,
        )
        self._record_render_timing("trace_compose", started)
        return result

    def render(
        self,
        section: Section,
        destination: Path,
        width_px: int = 800,
        height_px: int = 420,
    ) -> Path:
        """Create one native-color PNG without changing the board, fills or canvas."""
        native = self._native_document_available("svg")
        content = (
            self._native_trace_svg(section, width_px, height_px)
            if native
            else self.svg(section, width_px, height_px)
        )
        destination = Path(destination)
        with TemporaryDirectory(prefix="jlcpcb-impedance-image-") as directory:
            source, output = (
                Path(directory) / "section.svg",
                Path(directory) / "section.png",
            )
            source.write_text(content, encoding="utf-8")
            started = perf_counter()
            if native:
                self._rasterizer(
                    source, output, width_px, height_px, prepared_native=True
                )
            else:
                self._rasterizer(source, output, width_px, height_px)
            self._record_render_timing("rasterize", started)
            started = perf_counter()
            _validate_png(output, width_px, height_px)
            destination.write_bytes(output.read_bytes())
            self._record_render_timing("png_validate_copy", started)
        return destination

    def _spacing_scene(self, entry: "SpacingReviewEntry") -> _NativeScene:
        """Read unchanged native geometry once for diagnostic panels."""
        return self._layer_scene(entry.layer)

    def _native_document_available(self, method: str) -> bool:
        """Keep public overrides and every custom callback on the four-argument path."""
        return (
            getattr(self, "_use_native_panels", False)
            and self._rasterizer is _NATIVE_RASTERIZER
            and getattr(getattr(self, method), "__func__", None)
            is _NATIVE_PUBLIC_SVGS[method]
        )

    def _layer_scene(self, layer: str) -> _NativeScene:
        """Prepare one immutable physical layer independently of any review item."""
        started = perf_counter()
        source = self.context_svg(layer)
        background = self._theme_context().background
        scenes = getattr(self, "_native_scenes", None)
        if scenes is None:
            self._native_scenes = scenes = {}
        cached = scenes.get(layer)
        if (
            cached is not None
            and cached.source == source
            and cached.background == background
        ):
            scenes.pop(layer)
            scenes[layer] = cached
            return cached
        root, units_per_mm, _page_width, _page_height = _native_svg(source)
        if not any(node.tag in _SHAPES for node in root.iter()):
            raise RenderError("KiCad SVG contains no PCB geometry.")
        original_body = "".join(
            ET.tostring(child, encoding="unicode") for child in root
        )
        dimmed = deepcopy(root)
        _emphasize_native_traces(
            dimmed,
            _TraceSelection(layer),
            units_per_mm / NM_PER_MM,
            background,
        )
        opacity = _style(root).get("opacity")
        original_fragments = tuple(
            _prepare_raster_fragment(child, _style(root)) for child in root
        )
        dimmed_fragments = tuple(
            _repaint_raster_fragment(fragment, child)
            for fragment, child in zip(original_fragments, dimmed)
        )
        dimmed_body = "".join(
            ET.tostring(child, encoding="unicode") for child in dimmed
        )
        weight = (
            len(source.encode())
            + len(original_body.encode())
            + len(dimmed_body.encode())
            + sum(fragment.weight for fragment in original_fragments + dimmed_fragments)
        )
        scene = _NativeScene(
            source,
            tuple(root.attrib.items()),
            units_per_mm / NM_PER_MM,
            background,
            original_body,
            dimmed_body,
            f' opacity="{_opacity(opacity):.6f}"' if opacity is not None else "",
            original_fragments,
            dimmed_fragments,
            weight,
        )
        scenes.pop(layer, None)
        if weight <= _NATIVE_SCENE_BYTES:
            while scenes and (
                len(scenes) >= _NATIVE_SCENE_LIMIT
                or sum(item.weight for item in scenes.values()) + weight
                > _NATIVE_SCENE_BYTES
            ):
                scenes.pop(next(iter(scenes)))
            scenes[layer] = scene
        self._record_render_timing("scene_prepare", started)
        return scene

    def _native_trace_svg(self, section: Section, width_px: int, height_px: int) -> str:
        """Paint a private native PNG document after proving off-canvas geometry."""
        started = perf_counter()
        scene = self._layer_scene(section.layer)
        background = scene.background
        if not isinstance(background, str) or not re.fullmatch(
            r"#[0-9a-fA-F]{6}", background
        ):
            raise RenderError("The selected PCB background color is invalid.")
        viewport = section_viewport(section, width_px, height_px, self._board_bounds)
        factor = width_px / (viewport.width * scene.units_per_nm)
        tx = -viewport.left * scene.units_per_nm * factor
        ty = -viewport.top * scene.units_per_nm * factor
        transform = f"translate({tx:.6f},{ty:.6f}) scale({factor:.9f})"
        scale = float(f"{factor:.9f}")
        left, top = -float(f"{tx:.6f}") / scale, -float(f"{ty:.6f}") / scale
        visible = left, top, left + width_px / scale, top + height_px / scale
        selected = "".join(
            fragment.visible(visible, 2 / scale)
            for fragment in scene.original_fragments
        )
        root = ET.fromstring(  # noqa: S314 -- Validated native fragments.
            '<svg xmlns:xlink="http://www.w3.org/1999/xlink">' + selected + "</svg>"
        )
        _raster_svg_namespaces(root)
        root.attrib.update(dict(scene.root_attributes))
        _emphasize_native_traces(root, section, scene.units_per_nm, background)
        body = "".join(ET.tostring(child, encoding="unicode") for child in root)
        result = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">'
            f'<rect width="{width_px}" height="{height_px}" fill="{background}"/>'
            f'<g data-impedance-view="segment"{scene.opacity} transform="{transform}">{body}</g>'
            + _highlight_box(section, viewport, width_px, height_px)
            + "</svg>"
        )
        self._record_render_timing("trace_compose", started)
        return result

    def _record_render_timing(self, phase: str, started: float) -> None:
        """Aggregate bounded phase measurements without retaining image inputs."""
        timings = getattr(self, "render_timings", None)
        if timings is None:
            self.render_timings = timings = {}
        value = timings.setdefault(phase, {"calls": 0, "seconds": 0.0})
        value["calls"] += 1
        value["seconds"] += perf_counter() - started

    def spacing_svg(
        self,
        entry: "SpacingReviewEntry",
        width_px: int = 1600,
        height_px: int = 1000,
    ) -> str:
        """Compose true native detail, locator, mm values, and a labeled schematic."""
        return self._spacing_document(entry, width_px, height_px)

    def _spacing_document(
        self,
        entry: "SpacingReviewEntry",
        width_px: int,
        height_px: int,
        *,
        prepared: bool = False,
    ) -> str:
        """Keep public complete SVG and private native PNG framing identical."""
        from .spacing_review import SPACING_RENDER_POLICY_REVISION

        if (width_px, height_px) != (entry.width_px, entry.height_px):
            raise RenderError(
                "Spacing image dimensions differ from the required review viewport."
            )
        if not 1 <= width_px <= 4096 or not 1 <= height_px <= 4096:
            raise RenderError("Image dimensions must be between 1 and 4096 pixels.")
        composition = entry.composition
        started = perf_counter()
        _spacing_valid_viewport(entry, composition.detail_rect[2:])
        if width_px != 1600 or height_px != 1000:
            raise RenderError(
                "The combined spacing composition requires 1600 by 1000 pixels."
            )
        scene = self._spacing_scene(entry)
        detail, overview = self._spacing_panels(entry, scene, prepared=prepared)
        result = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}" '
            f'data-spacing-composition="{SPACING_RENDER_POLICY_REVISION}">'
            f'<rect width="{width_px}" height="{height_px}" fill="#10151d"/>'
            f"{detail}{overview}{_spacing_summary_svg(entry)}"
            "</svg>"
        )
        self._record_render_timing("spacing_compose", started)
        return result

    def _spacing_panels(
        self,
        entry: "SpacingReviewEntry",
        scene: _NativeScene,
        *,
        prepared: bool = False,
    ) -> tuple[str, str]:
        """Keep public and internally pruned panels on identical physical geometry."""
        composition = entry.composition
        detail = _spacing_panel(
            scene,
            entry.viewport_nm,
            composition.detail_rect,
            scene.background,
            kind="detail",
            annotations=_spacing_annotations(
                replace(
                    entry,
                    width_px=composition.detail_rect[2],
                    height_px=composition.detail_rect[3],
                )
            ),
            dimmed=True,
            prepared=prepared,
        )
        overview = _spacing_panel(
            scene,
            composition.overview_viewport_nm,
            composition.overview_rect,
            scene.background,
            kind="overview",
            annotations=_spacing_locator(entry),
            dimmed=True,
            prepared=prepared,
        )
        return detail, overview

    def render_spacing(
        self,
        entry: "SpacingReviewEntry",
        destination: Path,
        width_px: int = 1600,
        height_px: int = 1000,
    ) -> Path:
        """Rasterize all panels into the one immutable image explicitly approved."""
        native = self._native_document_available("spacing_svg")
        content = (
            self._spacing_document(entry, width_px, height_px, prepared=True)
            if native
            else self.spacing_svg(entry, width_px, height_px)
        )
        return self._render_spacing_svg(
            content,
            destination,
            width_px,
            height_px,
            prepared_native=native,
        )

    def spacing_original_svg(self, entry: "SpacingReviewEntry") -> str:
        """Return unchanged native copper at the detailed image's exact viewport."""
        return self._spacing_original_document(entry)

    def _spacing_original_document(
        self, entry: "SpacingReviewEntry", *, prepared: bool = False
    ) -> str:
        """Select a small native scratch SVG while retaining the public Original."""
        width, height = entry.composition.detail_rect[2:]
        _spacing_valid_viewport(entry, (width, height))
        scene = self._spacing_scene(entry)
        panel = _spacing_panel(
            scene,
            entry.viewport_nm,
            (0, 0, width, height),
            scene.background,
            kind="original",
            prepared=prepared,
        )
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f"{panel}</svg>"
        )

    def render_spacing_original(
        self,
        entry: "SpacingReviewEntry",
        destination: Path,
        width_px: Optional[int] = None,
        height_px: Optional[int] = None,
    ) -> Path:
        """Keep a separate unannotated comparison without granting it approval."""
        width, height = entry.composition.detail_rect[2:]
        if (width_px is not None and width_px != width) or (
            height_px is not None and height_px != height
        ):
            raise RenderError(
                "Original spacing image dimensions differ from its detail panel."
            )
        native = self._native_document_available("spacing_original_svg")
        return self._render_spacing_svg(
            self._spacing_original_document(entry, prepared=True)
            if native
            else self.spacing_original_svg(entry),
            destination,
            width,
            height,
            prepared_native=native,
        )

    def _render_spacing_svg(
        self,
        content: str,
        destination: Path,
        width_px: int,
        height_px: int,
        *,
        prepared_panels: Optional[tuple[str, ...]] = None,
        prepared_native: bool = False,
    ) -> Path:
        """Render a fresh bounded SVG and validate its immutable native PNG."""
        destination = Path(destination)
        with TemporaryDirectory(prefix="jlcpcb-spacing-image-") as directory:
            source = Path(directory) / "spacing.svg"
            output = Path(directory) / "spacing.png"
            source.write_text(content, encoding="utf-8")
            started = perf_counter()
            if prepared_panels is None and not prepared_native:
                self._rasterizer(source, output, width_px, height_px)
            else:
                self._rasterizer(
                    source,
                    output,
                    width_px,
                    height_px,
                    prepared_panels=prepared_panels,
                    prepared_native=prepared_native,
                )
            self._record_render_timing("rasterize", started)
            started = perf_counter()
            _validate_png(output, width_px, height_px)
            destination.write_bytes(output.read_bytes())
            self._record_render_timing("png_validate_copy", started)
        return destination


_NATIVE_PUBLIC_SVGS: dict[str, Callable[..., str]] = {
    "svg": SectionRenderer.svg,
    "spacing_svg": SectionRenderer.spacing_svg,
    "spacing_original_svg": SectionRenderer.spacing_original_svg,
}


def render_section(
    board: Any,
    pcbnew_module: Any,
    section: Section,
    destination: Path,
    width_px: int = 800,
    height_px: int = 420,
) -> Path:
    """Render one section; use SectionRenderer to share native plots across rows."""
    with SectionRenderer(board, pcbnew_module) as renderer:
        return renderer.render(section, destination, width_px, height_px)
