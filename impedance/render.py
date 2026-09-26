"""Render whole-route impedance captures from native KiCad layer plots."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import importlib
import math
from pathlib import Path
import re
import struct
from tempfile import TemporaryDirectory
from typing import Any, Optional
from xml.etree import ElementTree as ET

from .board_copy import BoardCopyError, copy_for_render
from .model import Bounds, Section
from .palette import PaletteError, ThemeContext, read_theme_context
from .pcbnew_adapter import copper_layers

NM_PER_MM = 1_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BACKGROUND_COLOR = "#10151d"
# Bump matching.CAPTURE_POLICY_REVISION for material framing/highlight changes
# so saved approvals cannot silently authorize a visually different capture.
HIGHLIGHT_COLOR = "#FFFF00"
HIGHLIGHT_PADDING_PX = 16.0
HIGHLIGHT_STROKE_PX = 2.0
HIGHLIGHT_EDGE_GUARD_PX = 1.0
CAPTURE_ZOOM_FACTOR = 0.85
MIN_BOARD_CAPTURE_FRACTION = 0.15
Rasterizer = Callable[[Path, Path, int, int], None]
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
    # The box clearance and stroke stay fixed in output pixels, so compact
    # previews may need more physical area than the requested 15% tighter crop.
    # A one-pixel outer guard protects antialiasing and rounded SVG coordinates.
    margin_px = HIGHLIGHT_PADDING_PX + HIGHLIGHT_STROKE_PX / 2 + HIGHLIGHT_EDGE_GUARD_PX
    available_width = width_px - 2 * margin_px
    available_height = height_px - 2 * margin_px
    if available_width <= 0 or available_height <= 0:
        raise RenderError(
            "Image dimensions are too small to contain the selected route box."
        )
    left, top, right, bottom = section.bounds
    padding = max(2 * NM_PER_MM, (right - left) * 0.15, (bottom - top) * 0.15)
    width, height = right - left + 2 * padding, bottom - top + 2 * padding
    if board_bounds is not None:
        width = max(
            width, (board_bounds[2] - board_bounds[0]) * MIN_BOARD_CAPTURE_FRACTION
        )
        height = max(
            height, (board_bounds[3] - board_bounds[1]) * MIN_BOARD_CAPTURE_FRACTION
        )
    previous = _fit_viewport(
        (
            (left + right - width) / 2,
            (top + bottom - height) / 2,
            (left + right + width) / 2,
            (top + bottom + height) / 2,
        ),
        width_px,
        height_px,
    )
    nm_per_pixel = max(
        previous.width * CAPTURE_ZOOM_FACTOR / width_px,
        previous.height * CAPTURE_ZOOM_FACTOR / height_px,
        (right - left) / available_width,
        (bottom - top) / available_height,
    )
    width, height = width_px * nm_per_pixel, height_px * nm_per_pixel
    return Viewport(
        (left + right - width) / 2, (top + bottom - height) / 2, width, height
    )


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
    return (active_layer, "B.SilkS", "F.SilkS", "Edge.Cuts")


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


def _paint(node: ET.Element, **changes: str) -> ET.Element:
    """Change visibility of native paint while preserving its original colors."""
    result = deepcopy(node)
    properties = _style(result)
    properties.update(changes)
    for name in _PAINT_KEYS | {"opacity"}:
        result.attrib.pop(name, None)
    result.set(
        "style", ";".join(f"{key}:{value}" for key, value in sorted(properties.items()))
    )
    return result


def _flatten_native(
    root: ET.Element, prefix: str
) -> tuple[list[ET.Element], list[ET.Element]]:
    """Flatten inherited paint/transforms and namespace each layer's native IDs."""
    root = deepcopy(root)
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
    leaves: list[ET.Element] = []
    definitions: list[ET.Element] = []

    def visit(
        node: ET.Element,
        inherited: dict[str, str],
        transforms: tuple[str, ...],
        opacity: float,
    ) -> None:
        """Copy native geometry with all inherited display state made explicit."""
        if node.tag == "defs":
            definitions.append(deepcopy(node))
            return
        properties = dict(inherited)
        own = _style(node)
        opacity *= _opacity(own.pop("opacity", "1"))
        properties.update(own)
        transform = node.get("transform")
        if transform:
            transforms += (transform,)
        if node.tag in _SHAPES:
            leaf = _paint(node, **properties, opacity=f"{opacity:.6f}")
            if transforms:
                leaf.set("transform", " ".join(transforms))
            leaves.append(leaf)
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
        root, scale, width, height = _native_svg(sources[name])
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
        for leaf in leaves:
            if name != active_layer:
                paint = _style(leaf)
                leaf = _paint(
                    leaf,
                    fill=_dimmed_color(
                        paint.get("fill", "black"), background, dimming_factor
                    ),
                    stroke=_dimmed_color(
                        paint.get("stroke", "none"), background, dimming_factor
                    ),
                )
            if name == active_layer:
                properties = _style(leaf)
                if properties.get("fill", "black") != "none":
                    plane = _large_copper_fill(leaf, units_per_mm)
                    if plane:
                        alpha = zone_opacity
                        role = "plane"
                    else:
                        alpha = 1.0
                        role = "pad"
                    fill = _paint(
                        leaf,
                        stroke="none",
                        **{
                            "fill-opacity": f"{_opacity(properties.get('fill-opacity', '1')) * alpha:.6f}"
                        },
                    )
                    if "id" in fill.attrib:
                        fill.set("id", fill.attrib["id"] + "-fill")
                    roles[role].append((name, fill))
                if properties.get("stroke", "none") != "none":
                    stroke = _paint(leaf, fill="none")
                    if "id" in stroke.attrib:
                        stroke.set("id", stroke.attrib["id"] + "-stroke")
                    roles["active"].append((name, stroke))
            elif name == "Edge.Cuts":
                roles["edge"].append((name, leaf))
            else:
                roles["reference"].append((name, leaf))
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
            # NanoSVG treats leaf opacity as replacing inherited group opacity.
            # Bake native alpha into each paint channel instead of
            # depending on nested SVG compositing semantics.
            for node in nodes:
                paint = _style(node)
                effective = _opacity(paint.get("opacity", "1"))
                group.append(
                    _paint(
                        node,
                        opacity="1",
                        **{
                            "fill-opacity": f"{effective * _opacity(paint.get('fill-opacity', '1')):.6f}",
                            "stroke-opacity": f"{effective * _opacity(paint.get('stroke-opacity', '1')):.6f}",
                        },
                    )
                )
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


def annotated_svg(
    source: str,
    section: Section,
    width_px: int = 800,
    height_px: int = 420,
    board_bounds: Optional[Bounds] = None,
    background_color: str = BACKGROUND_COLOR,
) -> str:
    """Create one full-frame, native-color capture with a yellow selected-route box."""
    if not isinstance(background_color, str) or not re.fullmatch(
        r"#[0-9a-fA-F]{6}", background_color
    ):
        raise RenderError("The selected PCB background color is invalid.")
    root, units_per_mm, _width, _height = _native_svg(source)
    if not any(node.tag in _SHAPES for node in root.iter()):
        raise RenderError("KiCad SVG contains no PCB geometry.")
    viewport = section_viewport(section, width_px, height_px, board_bounds)
    units_per_nm = units_per_mm / NM_PER_MM
    factor = width_px / (viewport.width * units_per_nm)
    transform = (
        f"translate({-viewport.left * units_per_nm * factor:.6f},"
        f"{-viewport.top * units_per_nm * factor:.6f}) scale({factor:.9f})"
    )
    body = "".join(ET.tostring(child, encoding="unicode") for child in root)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">'
        f'<rect width="{width_px}" height="{height_px}" fill="{background_color}"/>'
        f'<g data-impedance-view="segment" transform="{transform}">{body}</g>'
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


def _wx_rasterize(
    source: Path, destination: Path, width_px: int, height_px: int
) -> None:
    """Rasterize native stroked SVG without touching the live editor canvas."""
    try:
        wx = importlib.import_module("wx")
        svg = importlib.import_module("wx.svg")
    except ImportError as error:
        raise RenderError("Impedance images require KiCad's wx.svg support.") from error
    try:
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
    """Cache native-color plots for all context layers during one export run."""

    def __init__(
        self, board: Any, pcbnew_module: Any, rasterizer: Optional[Rasterizer] = None
    ) -> None:
        _check_native_plot_available()
        self.board = board
        self.pcbnew = pcbnew_module
        self._rasterizer = rasterizer or _wx_rasterize
        self._copper_ids = copper_layers(board, pcbnew_module)
        self._layer_ids = dict(self._copper_ids)
        for name in ("B.SilkS", "F.SilkS", "Edge.Cuts"):
            layer_id = getattr(pcbnew_module, name.replace(".", "_"), None)
            if layer_id is not None:
                self._layer_ids[name] = int(layer_id)
        self._layer_svg: dict[str, str] = {}
        self._plot_owner: Any = None
        self._plot_board: Any = None
        self._closed = False
        self._quarantined = False
        self._theme: Optional[ThemeContext] = None

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
        self._layer_svg.clear()
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
                self._plot_owner = copy_for_render(self.board, self.pcbnew)
                self._plot_board = self._plot_owner.board
            except BoardCopyError as error:
                raise RenderError(str(error)) from error
        return self._plot_board

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
                return filename.read_text(encoding="utf-8")
            except OSError as error:
                raise RenderError(
                    "KiCad did not create the impedance SVG plot."
                ) from error

    def svg(self, section: Section, width_px: int = 800, height_px: int = 420) -> str:
        """Return the same full-frame native capture used by preview and workbook."""
        _check_native_plot_available()
        if self._closed:
            raise RenderError("The impedance renderer has already been closed.")
        names = context_layer_names(tuple(self._copper_ids), section.layer)
        sources = {}
        for name in names:
            if name not in self._layer_ids:
                continue
            if name not in self._layer_svg:
                self._layer_svg[name] = self._plot_layer(name)
            sources[name] = self._layer_svg[name]
        return annotated_svg(
            compose_layer_svgs(
                sources,
                section.layer,
                background_color=self._theme_context().background,
                dimming_factor=self._theme_context().dimming_factor,
            ),
            section,
            width_px,
            height_px,
            board_bounds=board_outline_bounds(self.board),
            background_color=self._theme_context().background,
        )

    def render(
        self,
        section: Section,
        destination: Path,
        width_px: int = 800,
        height_px: int = 420,
    ) -> Path:
        """Create one native-color PNG without changing the board, fills or canvas."""
        content = self.svg(section, width_px, height_px)
        destination = Path(destination)
        with TemporaryDirectory(prefix="jlcpcb-impedance-image-") as directory:
            source, output = (
                Path(directory) / "section.svg",
                Path(directory) / "section.png",
            )
            source.write_text(content, encoding="utf-8")
            self._rasterizer(source, output, width_px, height_px)
            _validate_png(output, width_px, height_px)
            destination.write_bytes(output.read_bytes())
        return destination


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
