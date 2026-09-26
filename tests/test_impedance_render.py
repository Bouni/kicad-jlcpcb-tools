"""Exercise impedance extraction and off-screen render geometry without KiCad."""

from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import re
import struct
from types import SimpleNamespace
from typing import Any, Optional
import weakref
from xml.etree import ElementTree as ET

import pytest

from impedance import render as renderer_module
from impedance.model import Bounds, Section, Trace
from impedance.pcbnew_adapter import (
    BoardSnapshotError,
    _without_fill_cache,
    copper_layers,
    sample_arc,
    snapshot_board,
)
from impedance.render import (
    RenderError,
    SectionRenderer,
    _wx_rasterize,
    annotated_svg,
    board_outline_bounds,
    section_viewport,
)

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="240mm" height="120mm" '
    'viewBox="0 0 24000 12000"><path d="M 10000,4000 L 11000,4000" '
    'fill="none" stroke="#c83434" stroke-width="20"/></svg>'
)


@pytest.fixture(autouse=True)
def detached_render_board(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """Keep native-copy mechanics in their own suite while tracking renderer ownership."""
    copies = []

    def copy_board(board: Any, pcbnew_module: Any) -> Any:
        """Return a distinct board and record every requested copy."""
        assert pcbnew_module is not None
        detached = deepcopy(board)
        copies.append((board, detached))
        return SimpleNamespace(board=detached, close=lambda: None)

    monkeypatch.setattr(renderer_module, "copy_for_render", copy_board, raising=False)
    monkeypatch.setattr(
        renderer_module,
        "read_theme_context",
        lambda module: renderer_module.ThemeContext("_builtin_default", "#001023"),
    )
    return copies


class FakeTrack:
    """Expose the small PCB_TRACK interface needed for extraction."""

    def __init__(
        self,
        start: tuple[int, int] = (0, 0),
        end: tuple[int, int] = (10_000_000, 0),
        layer: int = 0,
        width: int = 200_000,
        net: str = "SIGNAL",
        uuid: Optional[str] = "trace",
    ) -> None:
        self.start, self.end, self.layer, self.width, self.net = (
            start,
            end,
            layer,
            width,
            net,
        )
        if uuid is not None:
            self.m_Uuid = SimpleNamespace(AsString=lambda: uuid)

    def GetStart(self) -> Any:
        """Return a copied start vector."""
        return SimpleNamespace(x=self.start[0], y=self.start[1])

    def GetEnd(self) -> Any:
        """Return a copied end vector."""
        return SimpleNamespace(x=self.end[0], y=self.end[1])

    def GetLayer(self) -> int:
        """Return the runtime layer identifier."""
        return self.layer

    def GetWidth(self) -> int:
        """Return the exact track width."""
        return self.width

    def GetNetname(self) -> str:
        """Return the exact net name."""
        return self.net


class FakeVia(FakeTrack):
    """Retain the track base API to exercise explicit via exclusion."""

    serialized = "via"


class FakeArc(FakeTrack):
    """Expose the arc midpoint in addition to ordinary track geometry."""

    def GetMid(self) -> Any:
        """Return the midpoint of an upward half circle."""
        return SimpleNamespace(x=5_000_000, y=-5_000_000)


class FakeSerializer:
    """Serialize context records without filesystem or board mutations."""

    def Format(self, item: Any) -> None:
        """Capture the complete context representation."""
        self.output = item.serialized

    def GetStringOutput(self, clear: bool) -> str:
        """Return the captured representation."""
        assert clear
        return self.output


class FakeBoard:
    """Hold tracks, enabled layers and context independently of pcbnew."""

    def __init__(
        self,
        tracks: tuple[Any, ...] = (),
        layers: tuple[int, ...] = (0, 2),
        context: tuple[Any, ...] = (),
    ) -> None:
        self.tracks, self.layers, self.context = tracks, layers, context

    def GetTracks(self) -> tuple[Any, ...]:
        """Return the live items."""
        return self.tracks

    def GetEnabledLayers(self) -> Any:
        """Return the layer stack in physical order."""
        return SimpleNamespace(CuStack=lambda: self.layers)

    def GetFootprints(self) -> tuple[Any, ...]:
        """Return serialized context placeholders."""
        return self.context

    def GetDrawings(self) -> tuple[Any, ...]:
        """Expose an empty native board-drawing collection for text inspection."""
        return ()


def pcbnew() -> Any:
    """Use non-contiguous runtime layer IDs as KiCad 9 and later do."""
    return SimpleNamespace(
        PCB_VIA=FakeVia, F_Cu=0, In1_Cu=4, B_Cu=2, PCB_IO_KICAD_SEXPR=FakeSerializer
    )


def section(layer: str = "F.Cu") -> Section:
    """Return one copper route with exact half-width bounds."""
    trace = Trace(
        "trace",
        layer,
        "SIGNAL",
        200_000,
        ((100_000_000, 40_000_000), (110_000_000, 40_000_000)),
    )
    return Section(
        "section",
        "spec",
        layer,
        trace.width_nm,
        (trace,),
        (99_900_000, 39_900_000, 110_100_000, 40_100_000),
        (trace.net,),
    )


def test_copper_layers_use_runtime_stack_and_ignore_custom_labels() -> None:
    """Preserve physical order without using legacy numeric ranges."""
    assert copper_layers(FakeBoard(layers=(0, 4, 2)), pcbnew()) == {
        "F.Cu": 0,
        "In1.Cu": 4,
        "B.Cu": 2,
    }


def test_snapshot_excludes_vias_and_disabled_layers() -> None:
    """Do not mistake a via diameter for an impedance track width."""
    board = FakeBoard((FakeTrack(), FakeVia(), FakeTrack(layer=4)))
    snapshot = snapshot_board(board, pcbnew())
    assert snapshot.layers == ("F.Cu", "B.Cu")
    assert len(snapshot.traces) == 1
    assert snapshot.traces[0].trace_id == "trace"


def test_snapshot_keeps_arc_extrema_and_geometry_after_live_changes() -> None:
    """Detach centerline data from mutable KiCad objects."""
    arc = FakeArc()
    snapshot = snapshot_board(FakeBoard((arc,)), pcbnew())
    arc.start = (99, 99)
    points = snapshot.traces[0].points
    assert points[0] == (0, 0)
    assert points[-1] == (10_000_000, 0)
    assert (5_000_000, -5_000_000) in points
    assert len(points) > 20


@pytest.mark.parametrize("middle", [(5_000_000, -5_000_000), (5_000_000, 5_000_000)])
def test_arc_sampling_follows_requested_side(middle: tuple[int, int]) -> None:
    """Distinguish clockwise and counterclockwise half circles."""
    points = sample_arc((0, 0), middle, (10_000_000, 0), 200_000)
    assert middle in points
    assert all(y <= 0 if middle[1] < 0 else y >= 0 for _, y in points)


def test_arc_sampling_keeps_major_arc_and_cardinal_extrema() -> None:
    """Follow the midpoint along a 270-degree arc rather than the short chord."""
    points = sample_arc((5_000_000, 0), (-5_000_000, 0), (0, -5_000_000), 200_000)
    assert (-5_000_000, 0) in points
    assert (0, 5_000_000) in points
    assert points[-1] == (0, -5_000_000)


@pytest.mark.parametrize(
    "points", [((0, 0), (5, 0), (10, 0)), ((0, 0), (0, 0), (0, 0))]
)
def test_degenerate_arcs_do_not_divide_by_zero(
    points: tuple[tuple[int, int], ...],
) -> None:
    """Retain collinear and zero-length items for core validation."""
    result = sample_arc(*points, width_nm=200_000)
    assert result[0] == points[0]
    assert result[-1] == points[-1]


def test_geometry_fallback_id_is_stable_and_detects_width_change() -> None:
    """Avoid unstable SWIG object addresses in fallback identifiers."""
    first = snapshot_board(FakeBoard((FakeTrack(uuid=None),)), pcbnew())
    same = snapshot_board(FakeBoard((FakeTrack(uuid=None),)), pcbnew())
    changed = snapshot_board(
        FakeBoard((FakeTrack(uuid=None, width=200_001),)), pcbnew()
    )
    assert first == same
    assert first.traces[0].trace_id != changed.traces[0].trace_id


def test_context_digest_is_order_independent_and_invalidates_changed_pads() -> None:
    """Ensure reviewed screenshots become stale when their context changes."""
    pad = SimpleNamespace(serialized="pad-size=100")
    zone = SimpleNamespace(serialized="zone-points=0,1,2")
    first = snapshot_board(FakeBoard(context=(pad, zone)), pcbnew())
    same = snapshot_board(FakeBoard(context=(zone, pad)), pcbnew())
    pad.serialized = "pad-size=200"
    changed = snapshot_board(FakeBoard(context=(pad, zone)), pcbnew())
    assert first.context_digest == same.context_digest
    assert first.context_digest != changed.context_digest


def test_missing_context_serializer_fails_with_actionable_error() -> None:
    """Do not silently review an incomplete board fingerprint."""
    module = pcbnew()
    del module.PCB_IO_KICAD_SEXPR
    with pytest.raises(BoardSnapshotError, match="fingerprint PCB context"):
        snapshot_board(FakeBoard(context=(SimpleNamespace(serialized="pad"),)), module)


@pytest.mark.parametrize("method", ["Groups", "Generators"])
def test_rule_scoped_membership_changes_invalidate_review(method: str) -> None:
    """A changed native group/generator can change the rules applying to copper."""
    item = SimpleNamespace(serialized='(group "RF" (members "a"))')
    board = FakeBoard()
    setattr(board, method, lambda: (item,))
    first = snapshot_board(board, pcbnew())
    item.serialized = '(group "RF" (members "a" "b"))'
    assert snapshot_board(board, pcbnew()).context_digest != first.context_digest


def test_external_project_definitions_invalidate_native_review(tmp_path: Path) -> None:
    """Read actual adjacent files instead of ignoring external fill-rule changes."""
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    board = FakeBoard()
    board.GetFileName = lambda: str(board_path)
    initial = snapshot_board(board, pcbnew()).context_digest
    rules = tmp_path / "shared-project.kicad_dru"
    rules.write_text('(version 1) (rule "RF" (constraint clearance (min 0.2)))')
    added = snapshot_board(board, pcbnew()).context_digest
    assert added != initial
    rules.write_text('(version 1) (rule "RF" (constraint clearance (min 0.3)))')
    assert snapshot_board(board, pcbnew()).context_digest != added


def test_invalid_external_project_is_an_actionable_snapshot_error(
    tmp_path: Path,
) -> None:
    """Do not approve a board while its adjacent native rule settings are unreadable."""
    board = FakeBoard()
    board.GetFileName = lambda: str(tmp_path / "board.kicad_pcb")
    (tmp_path / "project.kicad_pro").write_text("{malformed", encoding="utf-8")
    with pytest.raises(BoardSnapshotError, match="project|Project"):
        snapshot_board(board, pcbnew())


def test_definition_normalizer_excludes_fill_geometry_but_not_thermal_rules() -> None:
    """Exercise the definition helper; rendered zone snapshots retain their fills."""
    first = '(zone (net 1) (fill yes (thermal_gap 0.3)) (polygon (pts (xy 1 2))) (filled_polygon (layer "F.Cu") (pts (xy 1 2))))'
    refilled = first.replace("(xy 1 2))))", "(xy 9 8))))")
    changed_definition = first.replace("(thermal_gap 0.3)", "(thermal_gap 0.4)")
    assert _without_fill_cache(first) == _without_fill_cache(refilled)
    assert _without_fill_cache(first) != _without_fill_cache(changed_definition)
    assert "(fill (thermal_gap 0.3))" in _without_fill_cache(first)


def test_zone_fill_state_transition_invalidates_reviewed_capture_revision() -> None:
    """New filled pixels must invalidate captures even when zone outlines agree."""
    unfilled = "(zone (net 1) (fill (thermal_gap 0.3) (thermal_bridge_width 0.3)) (polygon (pts (xy 1 2))))"
    filled = unfilled.replace("(fill ", "(fill yes ").replace(
        "(polygon (pts (xy 1 2)))",
        '(polygon (pts (xy 1 2))) (filled_polygon (layer "F.Cu") (pts (xy 2 3)))',
    )
    zone = SimpleNamespace(serialized=unfilled)
    board = FakeBoard()
    board.Zones = lambda: (zone,)
    reviewed = snapshot_board(board, pcbnew())
    zone.serialized = filled
    refilled = snapshot_board(board, pcbnew())
    assert refilled.context_digest != reviewed.context_digest
    assert snapshot_board(board, pcbnew()) == refilled
    zone.serialized = filled.replace("(thermal_gap 0.3)", "(thermal_gap 0.4)")
    assert snapshot_board(board, pcbnew()).context_digest != refilled.context_digest


@pytest.mark.parametrize("value", ["(fill yes)", "(fill\tyes)", "(fill\nyes)"])
def test_generated_fill_state_without_parameters_is_normalized(value: str) -> None:
    """Accept empty fill records without consuming the closing parenthesis."""
    assert _without_fill_cache(value) == "(fill)"


def test_zone_cache_removal_retains_quoted_literal_text() -> None:
    """Do not parse parentheses in PCB text as generated fill records."""
    text = '(gr_text "the (filled_polygon) and (fill yes) text" (at 1 2))'
    assert _without_fill_cache(text) == text


def test_stackup_changes_invalidate_review_but_plot_preferences_do_not() -> None:
    """Hash physical board setup separately from mutable export preferences."""

    class Output:
        """Stand in for KiCad's in-memory STRING_FORMATTER."""

        text = ""

        def GetString(self) -> str:
            """Return the formatted board."""
            return self.text

    class Serializer(FakeSerializer):
        """Support formatting to a caller-owned memory buffer."""

        def LoadBoard(
            self,
            filename: str,
            append_to: Any = None,
            properties: Any = None,
            project: Any = None,
        ) -> Any:
            """Return an empty board through the native file-loader constructor path."""
            assert append_to is None and properties is None and project is None
            assert (
                Path(filename)
                .read_text(encoding="utf-8")
                .lstrip()
                .startswith("(kicad_pcb")
            )
            return SettingsBoard()

        def FormatBoardToFormatter(self, output: Output, board: Any) -> None:
            """Model KiCad updating embedded fonts during full formatting."""
            board.fonts_changed = True
            output.text = board.serialized

    class SettingsBoard(FakeBoard):
        """Copy settings into a detached board while retaining live geometry."""

        thisown = False

        def GetDesignSettings(self) -> str:
            """Return a simple value representing the live stackup."""
            return self.serialized

        def SetDesignSettings(self, settings: str) -> None:
            """Copy stackup data into this board."""
            self.serialized = settings

        def SetEnabledLayers(self, layers: Any) -> None:
            """Copy enabled runtime layer IDs."""
            self.layers = tuple(layers.CuStack())

    module = pcbnew()
    module.PCB_IO_KICAD_SEXPR, module.STRING_FORMATTER = Serializer, Output

    def unusable_board_constructor() -> None:
        """Fail if fingerprinting depends on the constructor that returns null in KiCad."""
        raise AssertionError("Fingerprinting must use the raw native board loader.")

    module.BOARD = unusable_board_constructor
    setattr(unusable_board_constructor, "__swig_destroy__", lambda board: None)
    board = SettingsBoard()
    board.serialized = '(kicad_pcb (general (thickness 1.6)) (layers (0 "F.Cu" signal)) (setup (stackup (layer "F.Cu" (thickness 0.035))) (pcbplotparams (outputdirectory "old"))))'
    original = snapshot_board(board, module)
    board.serialized = board.serialized.replace('"old"', '"new"')
    plotted = snapshot_board(board, module)
    assert original.context_digest == plotted.context_digest
    board.serialized = board.serialized.replace("0.035", "0.07")
    changed = snapshot_board(board, module)
    assert original.context_digest != changed.context_digest
    assert not hasattr(board, "fonts_changed")


@pytest.mark.parametrize("dimensions", [(800, 420), (420, 800), (640, 640)])
def test_viewport_preserves_aspect_and_annotation_padding(
    dimensions: tuple[int, int],
) -> None:
    """Keep copper and its outline inside each requested image shape."""
    value = section()
    viewport = section_viewport(value, *dimensions)
    assert viewport.width / viewport.height == pytest.approx(
        dimensions[0] / dimensions[1]
    )
    assert viewport.left < value.bounds[0] - 200_000
    assert viewport.top < value.bounds[1] - 200_000
    assert viewport.left + viewport.width > value.bounds[2] + 200_000
    assert viewport.top + viewport.height > value.bounds[3] + 200_000


def _capture_route(shape: str) -> Section:
    """Supply complete short, spanning, curved and paired copper for framing tests."""
    points = {
        "short": ((0, 0), (2_000_000, 0)),
        "horizontal": ((0, 0), (200_000_000, 0)),
        "vertical": ((0, 0), (0, 200_000_000)),
        "diagonal": ((-75_000_000, -35_000_000), (125_000_000, 165_000_000)),
        "meander": (
            (-100_000_000, -50_000_000),
            (0, -50_000_000),
            (0, 0),
            (-50_000_000, 0),
            (-50_000_000, 40_000_000),
        ),
        "negative": ((-200_000_000, -100_000_000), (-198_000_000, -100_000_000)),
        "differential": ((0, 0), (200_000_000, 0)),
    }.get(shape)
    if shape == "arc":
        points = sample_arc(
            (-100_000_000, 0), (0, -100_000_000), (100_000_000, 0), 200_000
        )
    assert points is not None
    trace = replace(section().traces[0], points=points)
    traces = (trace,)
    if shape == "differential":
        traces += (
            replace(
                trace,
                trace_id="pair-n",
                net="N",
                points=tuple((x, y + 600_000) for x, y in points),
            ),
        )
    return replace(
        section(),
        traces=traces,
        bounds=(
            min(x for item in traces for x, _ in item.points) - 100_000,
            min(y for item in traces for _, y in item.points) - 100_000,
            max(x for item in traces for x, _ in item.points) + 100_000,
            max(y for item in traces for _, y in item.points) + 100_000,
        ),
    )


def _contextual_size_before_zoom(
    value: Section, size: tuple[int, int], board: Optional[Bounds]
) -> tuple[float, float]:
    """Apply the 15% board minimum before the separate 85% viewport zoom."""
    width, height = value.bounds[2] - value.bounds[0], value.bounds[3] - value.bounds[1]
    padding = max(2_000_000, 0.15 * width, 0.15 * height)
    width, height = width + 2 * padding, height + 2 * padding
    if board is not None:
        width = max(width, (board[2] - board[0]) * 0.15)
        height = max(height, (board[3] - board[1]) * 0.15)
    aspect = size[0] / size[1]
    return max(width, height * aspect), max(height, width / aspect)


@pytest.mark.parametrize(
    "shape",
    [
        "short",
        "horizontal",
        "vertical",
        "diagonal",
        "arc",
        "differential",
        "meander",
        "negative",
    ],
)
@pytest.mark.parametrize("size", [(800, 420), (420, 800), (640, 640)])
@pytest.mark.parametrize(
    "board", [None, (-220_000_000, -120_000_000, 220_000_000, 220_000_000)]
)
def test_requested_crop_is_fifteen_percent_tighter_without_changing_center_or_aspect(
    shape: str, size: tuple[int, int], board: Optional[Bounds]
) -> None:
    """Zoom by reducing each physical dimension to 85%, not by changing PNG size."""
    value = _capture_route(shape)
    old_width, old_height = _contextual_size_before_zoom(value, size, board)
    actual = section_viewport(value, *size, board_bounds=board)
    assert actual.width == pytest.approx(old_width * 0.85)
    assert actual.height == pytest.approx(old_height * 0.85)
    assert actual.left + actual.width / 2 == pytest.approx(
        (value.bounds[0] + value.bounds[2]) / 2
    )
    assert actual.top + actual.height / 2 == pytest.approx(
        (value.bounds[1] + value.bounds[3]) / 2
    )
    assert actual.width / actual.height == pytest.approx(size[0] / size[1])


def test_requested_larger_box_is_maximum_bright_yellow_and_fully_opaque() -> None:
    """Increase clearance to sixteen pixels without recoloring or filling native copper."""
    value = section()
    root = ET.fromstring(annotated_svg(SVG, value))  # noqa: S314 -- Generated SVG only.
    box = root[-1]
    assert box.get("data-impedance-highlight") == "box"
    assert box.get("stroke") == "#FFFF00"
    assert box.get("opacity") == "1" and box.get("stroke-opacity") == "1"
    assert box.get("fill") == "none"
    assert box is list(root)[-1], "The opaque box must render above all native layers"
    viewport = section_viewport(value, 800, 420)
    scale = 800 / viewport.width
    assert tuple(
        float(box.attrib[key]) for key in ("x", "y", "width", "height")
    ) == pytest.approx(
        (
            (value.bounds[0] - viewport.left) * scale - 16,
            (value.bounds[1] - viewport.top) * scale - 16,
            (value.bounds[2] - value.bounds[0]) * scale + 32,
            (value.bounds[3] - value.bounds[1]) * scale + 32,
        ),
        abs=0.001,
    )
    original = [item for item in root.iter() if item.tag.endswith("}path")]
    assert len(original) == 1
    assert original[0].attrib == {
        "d": "M 10000,4000 L 11000,4000",
        "fill": "none",
        "stroke": "#c83434",
        "stroke-width": "20",
    }


@pytest.mark.parametrize(
    "shape",
    [
        "short",
        "horizontal",
        "vertical",
        "diagonal",
        "arc",
        "differential",
        "meander",
        "negative",
    ],
)
@pytest.mark.parametrize("size", [(160, 90), (90, 160), (40, 40), (37, 400), (400, 37)])
def test_compact_crop_expands_only_as_needed_to_keep_complete_box_and_stroke_visible(
    shape: str, size: tuple[int, int]
) -> None:
    """Safe-fit overrides the requested zoom only where the larger pixel box needs it."""
    value = _capture_route(shape)
    old_width, old_height = _contextual_size_before_zoom(value, size, None)
    viewport = section_viewport(value, *size)
    assert viewport.width >= old_width * 0.85 - 0.001
    assert viewport.height >= old_height * 0.85 - 0.001
    assert viewport.width / viewport.height == pytest.approx(size[0] / size[1])
    factor = size[0] / viewport.width
    margins = (
        (value.bounds[0] - viewport.left) * factor,
        (value.bounds[1] - viewport.top) * factor,
        (viewport.left + viewport.width - value.bounds[2]) * factor,
        (viewport.top + viewport.height - value.bounds[3]) * factor,
    )
    # Sixteen pixels outside copper + half of a 2 px stroke + a one-pixel
    # raster guard. The guard also absorbs SVG coordinate rounding.
    assert min(margins) >= 18 - 0.001
    if viewport.width > old_width * 0.85 + 0.001:
        assert min(margins) == pytest.approx(18), (
            "Do not expand beyond the necessary safe fit"
        )
    root = ET.fromstring(annotated_svg(SVG, value, *size))  # noqa: S314 -- Generated SVG only.
    assert root.get("viewBox") == f"0 0 {size[0]} {size[1]}"
    box = root[-1]
    left, top, width, height = (
        float(box.attrib[key]) for key in ("x", "y", "width", "height")
    )
    assert (
        min(
            left - 1,
            top - 1,
            size[0] - left - width - 1,
            size[1] - top - height - 1,
        )
        >= 1 - 0.002
    )
    for trace in value.traces:
        for x, y in trace.points:
            assert left < (x - viewport.left) * factor < left + width
            assert top < (y - viewport.top) * factor < top + height


@pytest.mark.parametrize("size", [(1, 1), (36, 420), (800, 36)])
def test_impossibly_small_images_fail_clearly_before_box_clipping(
    size: tuple[int, int],
) -> None:
    """Positive PNG dimensions alone are insufficient for a sixteen-pixel padded box."""
    with pytest.raises(RenderError, match="too small"):
        section_viewport(section(), *size)


def test_svg_overlay_uses_physical_board_coordinates_and_keeps_original_copper() -> (
    None
):
    """Preserve native copper color without repainting its centerline."""
    result = annotated_svg(SVG, section())
    assert 'd="M 10000,4000 L 11000,4000"' in result
    assert 'stroke="#c83434"' in result
    assert 'stroke="black"' not in result
    assert 'stroke="#000000"' not in result
    assert 'stroke-width="20"' in result
    assert 'width="800" height="420"' in result


@pytest.mark.parametrize("background", ["#ffffff", "#001023", "#123456"])
def test_capture_uses_selected_background_without_recoloring_native_geometry(
    background: str,
) -> None:
    """A light theme keeps its light canvas behind native dark foreground strokes."""
    source = SVG.replace("#c83434", "#101010")
    root = ET.fromstring(annotated_svg(source, section(), background_color=background))  # noqa: S314 -- Generated SVG only.
    assert root[0].tag.endswith("}rect")
    assert root[0].attrib["fill"] == background
    assert any(item.get("stroke") == "#101010" for item in root.iter())
    assert any(item.get("stroke") == "#FFFF00" for item in root.iter())


@pytest.mark.parametrize(
    "background", ["white", '"/><script/>', "rgba(0,0,0,0)", "#fff"]
)
def test_capture_rejects_unvalidated_background_paint(background: str) -> None:
    """Do not inject unchecked CSS or markup into a native capture."""
    with pytest.raises(RenderError, match="background"):
        annotated_svg(SVG, section(), background_color=background)


def test_selected_route_annotation_is_one_closed_yellow_box() -> None:
    """Enclose the selected route in one unfilled rectangle, not a trace contour."""
    root = ET.fromstring(annotated_svg(SVG, section()))  # noqa: S314 -- Generated SVG only.
    outlines = [
        item for item in root.iter() if item.get("data-impedance-highlight") == "box"
    ]
    assert len(outlines) == 1
    for outline in outlines:
        assert outline.tag.endswith("}rect")
        assert outline.get("stroke") == "#FFFF00"
        assert outline.get("fill") == "none"
        assert float(outline.attrib["width"]) > 0
        assert float(outline.attrib["height"]) > 0
    assert len([item for item in root.iter() if item.tag.endswith("}path")]) == 1
    assert not any(item.tag.endswith("}polyline") for item in root.iter())


@pytest.mark.parametrize(
    "shape",
    [
        "straight",
        "diagonal",
        "elbow",
        "arc",
        "connected-items",
        "differential",
        "separated",
    ],
)
def test_yellow_box_encloses_the_actual_selected_copper(shape: str) -> None:
    """One axis-aligned box includes every copper edge without following the route."""
    first, elbow, last = (
        (100_000_000, 40_000_000),
        (105_000_000, 40_000_000),
        (105_000_000, 45_000_000),
    )
    if shape == "arc":
        points = sample_arc(
            first, (105_000_000, 35_000_000), (110_000_000, 40_000_000), 200_000
        )
    elif shape == "straight":
        points = (first, elbow)
    elif shape == "diagonal":
        points = (first, last)
    else:
        points = (first, elbow, last)
    traces = (replace(section().traces[0], points=points),)
    if shape == "connected-items":
        traces = (
            replace(traces[0], trace_id="first", points=(first, elbow)),
            replace(traces[0], trace_id="second", points=(elbow, last)),
        )
    elif shape in {"differential", "separated"}:
        offset = 600_000 if shape == "differential" else 8_000_000
        traces += (
            replace(
                traces[0],
                trace_id="second",
                net="SECOND",
                points=tuple((x, y + offset) for x, y in points),
            ),
        )
    value = replace(
        section(),
        traces=traces,
        bounds=(
            min(x for trace in traces for x, _ in trace.points) - 100_000,
            min(y for trace in traces for _, y in trace.points) - 100_000,
            max(x for trace in traces for x, _ in trace.points) + 100_000,
            max(y for trace in traces for _, y in trace.points) + 100_000,
        ),
    )
    viewport = section_viewport(value, 800, 420)
    factor = 800 / viewport.width
    root = ET.fromstring(annotated_svg(SVG, value))  # noqa: S314 -- Generated SVG only.
    polygons = []
    for item in root:
        if item.get("data-impedance-highlight") != "box":
            continue
        assert item.tag.endswith("}rect")
        assert float(item.attrib["stroke-width"]) == 2
        left, top, width, height = (
            float(item.attrib[key]) for key in ("x", "y", "width", "height")
        )
        right, bottom = left + width, top + height
        assert (left, top, right, bottom) == pytest.approx(
            (
                (value.bounds[0] - viewport.left) * factor - 16,
                (value.bounds[1] - viewport.top) * factor - 16,
                (value.bounds[2] - viewport.left) * factor + 16,
                (value.bounds[3] - viewport.top) * factor + 16,
            ),
            abs=0.002,
        )
        polygons.append(
            (((left, top), (right, top), (right, bottom), (left, bottom)), 1)
        )
    assert len(polygons) == 1

    def contains(
        polygon: tuple[tuple[float, float], ...], point: tuple[float, float]
    ) -> bool:
        """Apply a ray crossing check independently of the outline generation algorithm."""
        x, y = point
        inside = False
        for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def distance(
        point: tuple[float, float],
        first: tuple[float, float],
        last: tuple[float, float],
    ) -> float:
        """Measure Euclidean clearance from a sampled copper center to a polygon edge."""
        dx, dy = last[0] - first[0], last[1] - first[1]
        denominator = dx * dx + dy * dy
        fraction = (
            max(
                0,
                min(
                    1,
                    ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy)
                    / denominator,
                ),
            )
            if denominator
            else 0
        )
        return math.hypot(
            point[0] - first[0] - fraction * dx, point[1] - first[1] - fraction * dy
        )

    for trace in traces:
        samples = tuple(trace.points) + tuple(
            ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            for start, end in zip(trace.points, trace.points[1:])
        )
        for x, y in samples:
            point = ((x - viewport.left) * factor, (y - viewport.top) * factor)
            assert any(contains(polygon, point) for polygon, _ in polygons)
            clearance = min(
                distance(point, first, last) - half_stroke
                for polygon, half_stroke in polygons
                for first, last in zip(polygon, polygon[1:] + polygon[:1])
            )
            assert clearance > trace.width_nm * factor / 2
    assert all(
        half_stroke < x < 800 - half_stroke and half_stroke < y < 420 - half_stroke
        for polygon, half_stroke in polygons
        for x, y in polygon
    )


def test_bottom_layer_uses_same_top_view_orientation() -> None:
    """Keep annotation coordinates aligned instead of mirroring only the crop."""
    assert annotated_svg(SVG, section("B.Cu")) == annotated_svg(SVG, section())


def test_negative_board_coordinates_are_not_clamped_to_page() -> None:
    """Allow a crop beyond the original plot page for negative PCB coordinates."""
    trace = Trace("negative", "F.Cu", "S", 200_000, ((-2_000_000, -3_000_000), (0, 0)))
    value = Section(
        "negative",
        "spec",
        "F.Cu",
        200_000,
        (trace,),
        (-2_100_000, -3_100_000, 100_000, 100_000),
        ("S",),
    )
    result = annotated_svg(SVG, value)
    root = ET.fromstring(result)  # noqa: S314 -- Generated SVG only.
    viewport = section_viewport(value, 800, 420)
    assert viewport.left < -2_100_000 and viewport.top < -3_100_000
    assert any(item.get("data-impedance-highlight") == "box" for item in root.iter())
    assert root.attrib["viewBox"] == "0 0 800 420"


@pytest.mark.parametrize("length_mm", [2, 100, 150, 200])
@pytest.mark.parametrize("direction", [(1, 0), (0, 1), (1, 1)])
@pytest.mark.parametrize("width_nm", [100_000, 150_000, 300_000])
def test_whole_route_and_screen_visible_highlight_fit_capture(
    length_mm: int, direction: tuple[int, int], width_nm: int
) -> None:
    """Zoom out for the whole segment rather than clipping or splitting long traces."""
    end = tuple(length_mm * 1_000_000 * axis for axis in direction)
    trace = Trace("route", "F.Cu", "SIGNAL", width_nm, ((0, 0), end))
    radius = (width_nm + 1) // 2
    value = Section(
        "route",
        "spec",
        "F.Cu",
        width_nm,
        (trace,),
        (-radius, -radius, end[0] + radius, end[1] + radius),
        ("SIGNAL",),
    )
    viewport = section_viewport(
        value, 800, 420, (-10_000_000, -10_000_000, 220_000_000, 220_000_000)
    )
    assert viewport.width / viewport.height == pytest.approx(800 / 420)
    pixel_scale = 800 / viewport.width
    margins = (
        (value.bounds[0] - viewport.left) * pixel_scale,
        (value.bounds[1] - viewport.top) * pixel_scale,
        (viewport.left + viewport.width - value.bounds[2]) * pixel_scale,
        (viewport.top + viewport.height - value.bounds[3]) * pixel_scale,
    )
    assert min(margins) >= 10 - 0.001


def test_short_trace_zoom_retains_board_relative_context() -> None:
    """A short trace cannot consume the whole image while losing its board location."""
    board = (0, 0, 240_000_000, 120_000_000)
    viewport = section_viewport(section(), 800, 420, board)
    assert viewport.width >= 240_000_000 * 0.15 * 0.85
    assert viewport.height >= 120_000_000 * 0.15 * 0.85
    result = annotated_svg(SVG, section(), board_bounds=board)
    root = ET.fromstring(result)  # noqa: S314 -- Generated SVG only.
    namespace = {"s": "http://www.w3.org/2000/svg"}
    assert not root.findall(".//s:svg", namespace), "NanoSVG ignores nested viewports"
    assert root.attrib["viewBox"] == "0 0 800 420"
    transforms = [item.get("transform") for item in root if item.get("transform")]
    assert len(transforms) == 1
    numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", transforms[0])]
    factor = 800 / (viewport.width / 10_000)
    assert numbers == pytest.approx(
        [-viewport.left / 10_000 * factor, -viewport.top / 10_000 * factor, factor]
    )
    assert "Board location" not in result
    assert "data-impedance-label" not in result
    assert not root.findall(".//s:text", namespace)


@pytest.mark.parametrize("size", [(160, 90), (800, 420), (420, 800)])
def test_full_frame_capture_accepts_supported_sizes_without_locator_minimum(
    size: tuple[int, int],
) -> None:
    """Use the requested frame at every supported size, including compact previews."""
    result = annotated_svg(
        SVG, section(), *size, board_bounds=(0, 0, 240_000_000, 120_000_000)
    )
    root = ET.fromstring(result)  # noqa: S314 -- Generated SVG only.
    assert root.attrib["viewBox"] == f"0 0 {size[0]} {size[1]}"
    assert "Board location" not in result


def test_whole_route_zoom_expands_with_length_not_copper_width() -> None:
    """The physical extent drives the camera; thin tracks must not cause tiling."""
    widths = []
    for length in (2, 100, 150, 200):
        trace = replace(section().traces[0], points=((0, 0), (length * 1_000_000, 0)))
        value = replace(
            section(),
            traces=(trace,),
            bounds=(-100_000, -100_000, length * 1_000_000 + 100_000, 100_000),
        )
        widths.append(section_viewport(value, 800, 420).width)
    assert widths == sorted(set(widths))


def test_outline_bounds_use_board_edges_not_page_size() -> None:
    """Use native board extent for the crop instead of the SVG page size."""
    box = SimpleNamespace(
        GetX=lambda: -10, GetY=lambda: 20, GetWidth=lambda: 200, GetHeight=lambda: 100
    )
    board = SimpleNamespace(GetBoardEdgesBoundingBox=lambda: box)
    assert board_outline_bounds(board) == (-10, 20, 190, 120)
    assert board_outline_bounds(object()) is None
    box.GetWidth = lambda: 0
    assert board_outline_bounds(board) is None


@pytest.mark.parametrize(
    "bounds", [(0, 0, 0, 1), (0, 0, 1, 0), (0, 0, float("inf"), 10)]
)
def test_invalid_outline_bounds_fail_clearly(bounds: Any) -> None:
    """Do not choose a misleading crop from an invalid outline extent."""
    with pytest.raises(RenderError, match="outline"):
        annotated_svg(SVG, section(), board_bounds=bounds)


@pytest.mark.parametrize(
    "source",
    [
        "not svg",
        SVG.replace("240mm", "240px"),
        SVG.replace("0 0 24000 12000", "1 0 24000 12000"),
        SVG.replace("0 0 24000 12000", "0 0 24000 13000"),
        SVG.replace("240mm", "NaNmm"),
        '<svg width="1mm" height="1mm" viewBox="0 0 1 1"></svg>',
    ],
)
def test_invalid_plot_coordinates_or_empty_plot_fail(source: str) -> None:
    """Refuse guessed transforms and empty plot output."""
    with pytest.raises(RenderError):
        annotated_svg(source, section())


def test_standard_kicad_svg_public_doctype_is_accepted() -> None:
    """Accept the fixed SVG 1.1 declaration emitted by KiCad's native plotter."""
    source = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n' + SVG
    )
    result = annotated_svg(source, section())
    assert "M 10000,4000 L 11000,4000" in result
    assert "<!DOCTYPE" not in result


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE svg SYSTEM "https://example.invalid/custom.dtd">',
        '<!DOCTYPE svg PUBLIC "custom" "https://example.invalid/custom.dtd">',
        '<!DOCTYPE svg [<!ENTITY injected "unexpected">]>',
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" [<!ENTITY injected "unexpected">]>',
    ],
)
def test_custom_svg_doctype_and_internal_entities_are_rejected(
    declaration: str,
) -> None:
    """Only the known static KiCad declaration belongs in native plot input."""
    with pytest.raises(RenderError):
        annotated_svg(declaration + SVG, section())


@pytest.mark.parametrize(
    "attributes",
    [
        'opacity="0" stroke-opacity="0"',
        'fill-opacity="0" stroke-opacity="0"',
        'style="fill-opacity:0;stroke-opacity:0"',
    ],
)
def test_native_invisible_phantom_text_keeps_stroked_label_geometry(
    attributes: str,
) -> None:
    """Discard non-rendered native text records while preserving the actual strokes."""
    source = SVG.replace("</svg>", f"<text {attributes}>R1</text></svg>")
    result = annotated_svg(source, section())
    root = ET.fromstring(result)  # noqa: S314 -- Generated SVG only.
    assert not any(item.tag.endswith("}text") for item in root.iter())
    assert "M 10000,4000 L 11000,4000" in result


@pytest.mark.parametrize(
    "attributes", ['fill="#ffffff"', 'opacity="0.1"', 'stroke-opacity="0"']
)
def test_visible_native_text_fails_instead_of_disappearing_in_nanosvg(
    attributes: str,
) -> None:
    """Treat visible SVG text as an unsupported plot mode rather than lose labels."""
    source = SVG.replace("</svg>", f"<text {attributes}>R1</text></svg>")
    with pytest.raises(RenderError, match="[Tt]ext|[Ss]troke"):
        annotated_svg(source, section())


def svg_page(body: str) -> str:
    """Wrap native drawing fragments in a realistic KiCad physical page."""
    return SVG.split(">", 1)[0] + ">" + body + "</svg>"


def drawing_objects(source: str) -> list[tuple[str, tuple[str, ...], float, Any]]:
    """Report source objects in drawing order with inherited layer roles and opacity."""
    root = ET.fromstring(source)  # noqa: S314 -- Generated SVG only.
    result = []

    def visit(
        element: Any,
        roles: tuple[str, ...],
        opacity: float,
        inherited: dict[str, str],
    ) -> None:
        """Accumulate the state actually inherited by each native SVG primitive."""
        role = element.get("data-impedance-role")
        if role:
            roles += (role,)
        own = dict(element.attrib)
        own.update(
            field.split(":", 1)
            for field in element.get("style", "").split(";")
            if ":" in field
        )
        opacity *= float(own.pop("opacity", "1"))
        style = dict(inherited)
        style.update(own)
        if element.get("data-object"):
            paint = "fill" if style.get("fill", "black") != "none" else "stroke"
            visible_opacity = opacity * float(style.get(f"{paint}-opacity", "1"))
            computed = deepcopy(element)
            computed.attrib.update(style)
            result.append(
                (element.get("data-object"), roles, visible_opacity, computed)
            )
        for child in element:
            visit(child, roles, opacity, style)

    visit(root, (), 1.0, {})
    return result


@pytest.mark.parametrize("inherited_fill", [False, True])
def test_native_layer_composition_keeps_context_visible_over_active_filled_plane(
    inherited_fill: bool,
) -> None:
    """A native opaque zone must sit below visible copper, labels and board edges."""
    plane = (
        '<path data-object="active-plane" d="M 0,0 L 24000,0 L 24000,12000 L 0,12000 Z" '
        + ("/>" if inherited_fill else 'fill="#c83434" stroke="none"/>')
    )
    if inherited_fill:
        plane = '<g style="fill:#c83434;stroke:none">' + plane + "</g>"
    sources = {
        "F.Cu": svg_page(
            plane
            + '<circle data-object="active-pad" cx="10000" cy="4000" r="40" fill="#c83434"/>'
            + '<path data-object="active-route" d="M 10000,4000 L 11000,4000" fill="none" stroke="#c83434" stroke-width="20"/>'
        ),
        "B.Cu": svg_page(
            '<path data-object="other-copper" d="M 10500,3800 L 10500,4200" fill="none" stroke="#346ac8" stroke-width="20"/>'
        ),
        "F.Fab": svg_page(
            '<path data-object="fab" d="M 9900,3800 L 11100,4200" fill="none" stroke="#9a9a9a" stroke-width="10"/>'
        ),
        "F.SilkS": svg_page(
            '<path data-object="silk" d="M 9900,3700 L 11100,3700" fill="none" stroke="#eeeeee" stroke-width="10"/>'
        ),
        "Edge.Cuts": svg_page(
            '<path data-object="edge" d="M 0,0 L 24000,12000" fill="none" stroke="#b5aa66" stroke-width="10"/>'
        ),
    }
    composed = renderer_module.compose_layer_svgs(
        sources, "F.Cu", background_color="#001023"
    )
    items = drawing_objects(composed)
    assert len(items) == 5
    objects = {
        name: (roles, opacity, element) for name, roles, opacity, element in items
    }
    order = [name for name, *_ in items]
    assert "other-copper" not in objects
    assert "fab" not in objects
    assert order.index("active-plane") < order.index("active-pad")
    assert order.index("silk") < order.index("active-route")
    assert order.index("active-route") < order.index("edge")
    assert "plane" in objects["active-plane"][0]
    assert "pad" in objects["active-pad"][0]
    assert (
        0
        < objects["active-plane"][1]
        < objects["active-pad"][1]
        <= objects["active-route"][1]
    )
    assert objects["active-route"][1] == pytest.approx(1)
    assert objects["active-plane"][1] == pytest.approx(0.6)
    assert objects["silk"][1] == 1
    assert objects["edge"][1] == pytest.approx(1)
    for name, color in (
        ("active-route", "#c83434"),
        ("silk", "#303c4c"),
        ("edge", "#242f30"),
    ):
        assert objects[name][2].get("stroke") == color
    assert (
        renderer_module.compose_layer_svgs(
            dict(reversed(tuple(sources.items()))), "F.Cu", background_color="#001023"
        )
        == composed
    )


@pytest.mark.parametrize(
    "pad",
    [
        '<rect x="9960" y="3960" width="80" height="80"/>',
        '<rect x="9960" y="3960" width="80" height="80" rx="20"/>',
        '<polygon points="9960,3960 10040,3960 10040,4040 9960,4040"/>',
        '<path d="M 9960,3960 L 10040,3960 L 10040,4040 L 9960,4040 Z"/>',
    ],
)
def test_small_native_filled_pad_shapes_stay_opaque_and_inactive_copper_is_absent(
    pad: str,
) -> None:
    """Keep common and custom filled pads distinct from large opaque zone planes."""
    pad = pad.replace("/>", ' data-object="pad" fill="#c83434"/>')
    composed = renderer_module.compose_layer_svgs(
        {
            "F.Cu": svg_page(pad),
            "B.Cu": svg_page(
                '<path data-object="other" d="M 9900,4000 L 10100,4000" fill="none" stroke="#346ac8"/>'
            ),
        },
        "F.Cu",
        background_color="#001023",
    )
    objects = {
        name: (roles, opacity, element)
        for name, roles, opacity, element in drawing_objects(composed)
    }
    assert "pad" in objects["pad"][0]
    assert objects["pad"][1] == 1
    assert "other" not in objects
    assert objects["pad"][2].get("fill") == "#c83434"


def test_layer_opacity_is_baked_into_leaf_paint_for_nanosvg() -> None:
    """Do not depend on NanoSVG multiplying a parent opacity by explicit leaf opacity."""

    def source(color: str, opacity: bool) -> str:
        """Use nested native opacity when testing preservation of source transparency."""
        body = (
            '<path data-object="' + color + '" d="M 10000,4000 L 11000,4000" '
            'fill="none" stroke="'
            + color
            + '"'
            + (' opacity="0.5"' if opacity else "")
            + "/>"
        )
        if opacity:
            body = '<g opacity="0.5">' + body + "</g>"
        return svg_page(body)

    captures = [
        renderer_module.compose_layer_svgs(
            {
                "F.Cu": source("#c83434", transparent),
                "B.Cu": source("#346ac8", transparent),
            },
            "F.Cu",
            background_color="#001023",
        )
        for transparent in (False, True)
    ]
    alpha = []
    for capture in captures:
        root = ET.fromstring(capture)  # noqa: S314 -- Generated SVG only.
        for element in root.iter():
            style = dict(
                field.split(":", 1)
                for field in element.get("style", "").split(";")
                if ":" in field
            )
            assert float(style.get("opacity", element.get("opacity", "1"))) == 1
        alpha.append(
            {
                name: opacity
                for name, _roles, opacity, _element in drawing_objects(capture)
            }
        )
    assert alpha[1]["#c83434"] == pytest.approx(alpha[0]["#c83434"] * 0.25)
    assert "#346ac8" not in alpha[0] and "#346ac8" not in alpha[1]
    assert alpha[0]["#c83434"] == 1


def test_inactive_copper_planes_and_routes_are_excluded() -> None:
    """No buried plane or route is composited while a different layer is active."""
    sources = {}
    for layer, color in (
        ("F.Cu", "#c83434"),
        ("In1.Cu", "#d17c26"),
        ("B.Cu", "#346ac8"),
    ):
        sources[layer] = svg_page(
            f'<path data-object="{layer}-plane" d="M 0,0 L 24000,0 L 24000,12000 L 0,12000 Z" fill="{color}" stroke="none"/>'
            f'<path data-object="{layer}-route" d="M 10000,4000 L 11000,4000" fill="none" stroke="{color}"/>'
        )
    items = drawing_objects(
        renderer_module.compose_layer_svgs(sources, "F.Cu", background_color="#001023")
    )
    objects = {name: (roles, opacity) for name, roles, opacity, _element in items}
    order = [name for name, *_ in items]
    assert order == ["F.Cu-plane", "F.Cu-route"]
    assert "plane" in objects["F.Cu-plane"][0]
    assert objects["F.Cu-plane"][1] == pytest.approx(0.6)
    assert objects["F.Cu-route"][1] == 1


@pytest.mark.parametrize("active", ["F.Cu", "In2.Cu", "In10.Cu", "B.Cu"])
def test_selected_physical_copper_layer_is_the_only_visible_copper(active: str) -> None:
    """Switching signal layer changes its native geometry without ghost routes."""
    sources = {
        layer: svg_page(
            f'<path data-object="{layer}" d="M 10000,4000 L 11000,4000" fill="none" stroke="#c83434"/>'
        )
        for layer in ("B.Cu", "In10.Cu", "F.Cu", "In2.Cu")
    }
    composed = renderer_module.compose_layer_svgs(
        sources, active, background_color="#001023"
    )
    objects = drawing_objects(composed)
    assert len(objects) == 1
    assert objects[0][0] == active and "active" in objects[0][1]
    assert composed == renderer_module.compose_layer_svgs(
        dict(reversed(tuple(sources.items()))), active, background_color="#001023"
    )


def test_composed_layer_definitions_and_references_keep_unique_identifiers() -> None:
    """Repeated plot-local IDs must still reference their own layer's definitions."""

    def source(color: str) -> str:
        """Use the same gradient ID in each independent native plot."""
        return svg_page(
            '<defs><linearGradient id="native-paint"><stop offset="0" stop-color="'
            + color
            + '"/></linearGradient></defs>'
            '<path d="M 10000,4000 L 11000,4000" fill="none" stroke="url(#native-paint)"/>'
        )

    root = ET.fromstring(  # noqa: S314 -- Generated SVG only.
        renderer_module.compose_layer_svgs(
            {
                "F.Cu": source("#c83434"),
                "F.SilkS": source("#346ac8"),
                "B.Cu": source("#abcdef"),
            },
            "F.Cu",
            background_color="#001023",
            dimming_factor=0,
        )
    )
    identifiers = [item.get("id") for item in root.iter() if item.get("id")]
    references = [
        reference
        for item in root.iter()
        for value in item.attrib.values()
        for reference in re.findall(r"url\(#([^)]+)\)", value)
    ]
    assert len(identifiers) == len(set(identifiers)) == 2
    assert len(references) == len(set(references)) == 2
    assert set(references) == set(identifiers)
    assert {
        dict(
            field.split(":", 1)
            for field in item.get("style", "").split(";")
            if ":" in field
        ).get("stop-color", item.get("stop-color"))
        for item in root.iter()
        if item.tag.endswith("}stop")
    } == {
        "#c83434",
        "#346ac8",
    }


@pytest.mark.parametrize(
    "different", [SVG.replace("240mm", "200mm"), SVG.replace("24000", "20000")]
)
def test_layer_composition_rejects_inconsistent_physical_coordinates(
    different: str,
) -> None:
    """Fail clearly instead of compositing context at the wrong physical scale."""
    with pytest.raises(RenderError, match="page|scal|coordinates"):
        renderer_module.compose_layer_svgs(
            {"F.Cu": SVG, "F.SilkS": different}, "F.Cu", background_color="#001023"
        )


def test_empty_optional_context_layer_keeps_active_geometry() -> None:
    """An unused mechanical layer is a valid context plot without drawing items."""
    result = renderer_module.compose_layer_svgs(
        {"F.Cu": SVG, "F.SilkS": svg_page("")}, "F.Cu", background_color="#001023"
    )
    assert "M 10000,4000 L 11000,4000" in result


def test_context_layer_names_omit_fabrication_layers() -> None:
    """Preview and workbook captures keep silk and outline, not fab graphics."""
    assert renderer_module.context_layer_names(("F.Cu", "B.Cu"), "F.Cu") == (
        "F.Cu",
        "B.SilkS",
        "F.SilkS",
        "Edge.Cuts",
    )


def test_native_palette_ignores_unloaded_live_and_snapshot_options() -> None:
    """Non-null default plot palettes cannot override the configured native theme."""
    original_palette, default_palette = object(), object()
    original = SimpleNamespace(ColorSettings=lambda: original_palette)
    detached = SimpleNamespace(ColorSettings=lambda: default_palette)
    assert (
        renderer_module._native_color_settings(
            SimpleNamespace(GetColorSettings=lambda name: default_palette),
            detached,
            original,
        )
        is default_palette
    )


def test_native_palette_falls_back_to_known_builtin_theme() -> None:
    """Use KiCad's own palette when neither source nor controller exposes one."""
    requests = []
    palette = object()

    def get_color_settings(name: str) -> object:
        """Record the precise native theme identifier."""
        requests.append(name)
        return palette

    module = SimpleNamespace(GetColorSettings=get_color_settings)
    assert renderer_module._native_color_settings(module, SimpleNamespace()) is palette
    assert requests == ["_builtin_default"]


def fake_plotter(
    module: Any,
    tmp_path: Path,
    fail: Optional[str] = None,
    sources: Optional[dict[int, str]] = None,
) -> list[Any]:
    """Install an isolated fake plot-controller factory and return its instances."""
    instances = []
    stroke_mode = 17

    class Options:
        """Capture only controller-local configuration changes."""

        def __init__(self) -> None:
            self.values: dict[str, Any] = {}

        def __getattr__(self, name: str) -> Any:
            """Expose only setters audited in KiCad's installed native bindings."""
            if name not in {
                "SetOutputDirectory",
                "SetFormat",
                "SetUseAuxOrigin",
                "SetScale",
                "SetScaleSelection",
                "SetAutoScale",
                "SetMirror",
                "SetNegative",
                "SetPlotFrameRef",
                "SetPlotValue",
                "SetPlotReference",
                "SetPlotFPText",
                "SetBlackAndWhite",
                "SetSvgFitPageToBoard",
                "SetA4Output",
                "SetFineScaleAdjustX",
                "SetFineScaleAdjustY",
                "SetWidthAdjust",
                "SetTextMode",
                "SetColorSettings",
            }:
                raise AttributeError(name)

            def setter(value: Any) -> None:
                """Store one plot option."""
                self.values[name] = value

            return setter

    class Controller:
        """Write deterministic SVG and capture lifecycle calls."""

        def __init__(self, board: Any) -> None:
            self.options = Options()
            self.closed = False
            self.board = board
            self.color = False
            self.filename = tmp_path / "unused.svg"
            instances.append(self)

        def GetPlotOptions(self) -> Options:
            """Return independent plot settings."""
            return self.options

        def SetLayer(self, layer: int) -> None:
            """Capture the runtime layer ID."""
            self.layer = layer

        def SetColorMode(self, value: bool) -> None:
            """Select whether the SVG retains native drawing colors."""
            self.color = value

        def OpenPlotfile(self, suffix: str, file_format: int, description: str) -> bool:
            """Create the requested temporary SVG path."""
            assert suffix and description and file_format == module.PLOT_FORMAT_SVG
            self.filename = (
                Path(self.options.values["SetOutputDirectory"]) / "board-impedance.svg"
            )
            return fail != "open"

        def PlotLayer(self) -> bool:
            """Apply color and text settings while creating native plot geometry."""
            if fail != "missing":
                source = (sources or {}).get(self.layer, SVG)
                if not self.color or self.options.values.get("SetBlackAndWhite", True):
                    source = re.sub(r"#[0-9a-fA-F]{6}", "#000000", source)
                for field, option in (
                    ("reference", "SetPlotReference"),
                    ("value", "SetPlotValue"),
                    ("footprint-text", "SetPlotFPText"),
                ):
                    if not self.options.values.get(option, False):
                        source = re.sub(
                            rf'<path data-field="{field}"[^>]*/>', "", source
                        )
                if self.options.values.get("SetTextMode") != stroke_mode:
                    source = re.sub(
                        r'<path data-field="([^"]+)"[^>]*/>',
                        r"<text>\1</text>",
                        source,
                    )
                self.filename.write_text(source, encoding="utf-8")
            return fail != "plot"

        def GetPlotFileName(self) -> str:
            """Return the temporary plot filename."""
            return str(self.filename)

        def ClosePlot(self) -> None:
            """Record closure even when plotting fails."""
            self.closed = True

    module.PLOT_CONTROLLER = Controller
    module.PLOT_FORMAT_SVG = 4
    module.PLOT_TEXT_MODE_STROKE = stroke_mode
    module.GetColorSettings = lambda name: object()
    return instances


def png_rasterizer(source: Path, destination: Path, width: int, height: int) -> None:
    """Stub only rasterization, retaining header and dimension validation."""
    assert source.read_text(encoding="utf-8").startswith("<svg")
    destination.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def test_renderer_caches_each_layer_closes_plot_and_cleans_temporaries(
    tmp_path: Path,
    detached_render_board: list[tuple[Any, Any]],
) -> None:
    """Reuse expensive SVG plotting across multiple rows in one export."""
    module, board = pcbnew(), FakeBoard()
    original = deepcopy(board.__dict__)
    controllers = fake_plotter(module, tmp_path)
    renderer = SectionRenderer(board, module, png_rasterizer)
    renderer.render(section(), tmp_path / "one.png")
    renderer.render(section(), tmp_path / "two.png")
    renderer.render(section("B.Cu"), tmp_path / "bottom.png")
    assert len(controllers) == 2
    assert all(
        controller.closed and not controller.filename.exists()
        for controller in controllers
    )
    assert {controller.layer for controller in controllers} == {0, 2}
    assert controllers[0].options.values["SetMirror"] is False
    assert controllers[0].options.values["SetUseAuxOrigin"] is False
    assert controllers[0].options.values["SetA4Output"] is False
    assert len(detached_render_board) == 1
    source, detached = detached_render_board[0]
    assert source is board
    assert detached is not board
    assert all(controller.board is detached for controller in controllers)
    assert board.__dict__ == original


@pytest.mark.parametrize("failure", [None, "open", "plot", "missing", "palette"])
def test_renderer_scope_releases_controllers_before_detached_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Optional[str]
) -> None:
    """A successful or failed preview never leaves dependent native plotters alive."""
    module, board = pcbnew(), FakeBoard()
    instances = fake_plotter(module, tmp_path, failure)
    factory = module.PLOT_CONTROLLER
    references = []
    releases = []

    def create_controller(detached: Any) -> Any:
        """Observe controller lifetime without extending it from the test."""
        controller = factory(detached)
        instances.pop()
        references.append(weakref.ref(controller))
        return controller

    def release_board() -> None:
        """Reject early board deletion even on the active exception path."""
        assert all(reference() is None for reference in references)
        releases.append("board")

    module.PLOT_CONTROLLER = create_controller
    if failure == "palette":
        module.GetColorSettings = lambda name: None
    monkeypatch.setattr(
        renderer_module,
        "copy_for_render",
        lambda source, module: SimpleNamespace(
            board=deepcopy(source), close=release_board
        ),
    )
    renderer = SectionRenderer(board, module, png_rasterizer)

    def preview() -> None:
        """Use the actual production context-manager entry/exit contract."""
        with renderer:
            renderer.render(section(), tmp_path / "result.png")

    if failure is None:
        preview()
    else:
        with pytest.raises(RenderError):
            preview()
    assert releases == ["board"]
    renderer.close()
    assert releases == ["board"]
    with pytest.raises(RenderError, match="closed"):
        renderer.svg(section())
    with pytest.raises(RenderError, match="closed"), renderer:
        pass


def test_failed_native_close_quarantines_uncertain_resources_until_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never delete or retry a partially closed native plotter or publish its PNG."""
    monkeypatch.setattr(renderer_module, "_FATAL_PLOT_STATE", None, raising=False)
    module, board = pcbnew(), FakeBoard()
    instances = fake_plotter(module, tmp_path)
    factory = module.PLOT_CONTROLLER
    controllers = []
    closed = []
    released = []
    destination = tmp_path / "approved.png"
    destination.write_bytes(b"previous image")
    detached = deepcopy(board)
    owner = SimpleNamespace(board=detached, close=lambda: released.append(True))

    def failing_close() -> None:
        """Model a native write error that leaves its partial teardown uncertain."""
        closed.append(True)
        raise RuntimeError("SVG output write failed during ClosePlot")

    def create_controller(detached_board: Any) -> Any:
        """Record weakly, so production must retain the failed native wrapper."""
        controller = factory(detached_board)
        controller.ClosePlot = failing_close
        instances.pop()
        controllers.append(weakref.ref(controller))
        return controller

    monkeypatch.setattr(renderer_module, "copy_for_render", lambda board, module: owner)
    module.PLOT_CONTROLLER = create_controller
    renderer = SectionRenderer(board, module, png_rasterizer)
    already_created = SectionRenderer(board, module, png_rasterizer)
    with pytest.raises(RenderError, match="[Rr]estart.*KiCad"), renderer:
        renderer.render(section(), destination)
    assert closed == [True]
    assert not released
    assert destination.read_bytes() == b"previous image"
    renderer.close()
    assert closed == [True] and not released
    assert renderer_module._FATAL_PLOT_STATE is not None
    assert controllers[0]() is not None
    assert renderer._plot_owner is owner
    with pytest.raises(RenderError, match="[Rr]estart.*KiCad"):
        SectionRenderer(board, module)
    with pytest.raises(RenderError, match="[Rr]estart.*KiCad"):
        already_created.svg(section())
    already_created.close()
    # Simulate a fresh process only in this test; production never resets the fuse.
    monkeypatch.setattr(renderer_module, "_FATAL_PLOT_STATE", None)


@pytest.mark.parametrize(
    "method",
    ["GetPlotOptions", "SetLayer", "OpenPlotfile", "PlotLayer", "GetPlotFileName"],
)
def test_native_method_exception_closes_plot_before_releasing_its_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A SWIG-like traceback may retain self, but successful ClosePlot detaches native resources."""
    module, board = pcbnew(), FakeBoard()
    controllers = fake_plotter(module, tmp_path)
    events = []
    factory = module.PLOT_CONTROLLER

    def native_failure(self: Any, *args: Any) -> Any:
        """Model the Python wrapper frame that retains self on a native exception."""
        assert self.board is not board
        if method != "GetPlotOptions":
            assert self.options is not None
        raise RuntimeError(f"Native {method} failed")

    monkeypatch.setattr(factory, method, native_failure)

    def release() -> None:
        """Native controller ClosePlot must have completed even while its wrapper survives."""
        assert controllers and all(controller.closed for controller in controllers)
        events.append("released")

    monkeypatch.setattr(
        renderer_module,
        "copy_for_render",
        lambda board, module: SimpleNamespace(board=deepcopy(board), close=release),
    )
    with (
        pytest.raises(RuntimeError, match=f"Native {method} failed") as caught,
        SectionRenderer(board, module) as renderer,
    ):
        renderer.svg(section())
    assert caught.traceback is not None
    assert events == ["released"]
    assert renderer_module._FATAL_PLOT_STATE is None


def test_renderer_preserves_native_color_and_rasterizable_reference_value_text(
    tmp_path: Path,
) -> None:
    """Render enabled labels as strokes while retaining the copper's native color."""
    source = SVG.replace(
        "</svg>",
        '<path data-field="reference" d="M 10000,3900 L 10020,3920" stroke="#eeeeee"/>'
        '<path data-field="value" d="M 10100,3900 L 10120,3920" stroke="#cccccc"/>'
        '<path data-field="footprint-text" d="M 10200,3900 L 10220,3920" stroke="#aaaaaa"/>'
        "</svg>",
    )
    module = pcbnew()
    controllers = fake_plotter(module, tmp_path, sources={0: source})
    captures = []

    def rasterize(source: Path, destination: Path, width: int, height: int) -> None:
        """Inspect the real composed SVG passed to the rasterizer."""
        captures.append(source.read_text(encoding="utf-8"))
        png_rasterizer(source, destination, width, height)

    SectionRenderer(FakeBoard(), module, rasterize).render(
        section(), tmp_path / "native-color.png"
    )
    assert len(captures) == 1
    root = ET.fromstring(captures[0])  # noqa: S314 -- Generated SVG only.
    assert {
        item.get("data-field") for item in root.iter() if item.get("data-field")
    } == {
        "reference",
        "value",
        "footprint-text",
    }
    assert not any(item.tag.endswith("}text") for item in root.iter())
    assert "#c83434" in captures[0]
    assert all(controller.color for controller in controllers)
    assert all(
        controller.options.values["SetTextMode"] == module.PLOT_TEXT_MODE_STROKE
        for controller in controllers
    )


def test_renderer_plots_enabled_copper_and_available_mechanical_context_once(
    tmp_path: Path,
) -> None:
    """Cache each native layer separately across preview and PNG generation."""
    module = pcbnew()
    colors = {0: "#c83434", 4: "#d17c26", 2: "#346ac8"}
    sources = {
        layer: svg_page(
            f'<path data-object="copper-{layer}" d="M 10000,4000 L 11000,4000" fill="none" stroke="{color}"/>'
        )
        for layer, color in colors.items()
    }
    controllers = fake_plotter(module, tmp_path, sources=sources)
    module.F_Fab, module.B_Fab = 49, 51
    module.F_SilkS, module.B_SilkS, module.Edge_Cuts = 37, 39, 44
    captures = []

    def rasterize(source: Path, destination: Path, width: int, height: int) -> None:
        """Capture the same composed SVG that preview users inspect."""
        captures.append(source.read_text(encoding="utf-8"))
        png_rasterizer(source, destination, width, height)

    board = FakeBoard(layers=(0, 4, 2))
    original = deepcopy(board.__dict__)
    renderer = SectionRenderer(board, module, rasterize)
    preview = renderer.svg(section(), 800, 420)
    renderer.render(section(), tmp_path / "front.png")
    bottom_preview = renderer.svg(section("B.Cu"), 800, 420)
    assert captures == [preview]
    assert len(controllers) == 5
    assert {controller.layer for controller in controllers} == {
        0,
        2,
        37,
        39,
        44,
    }
    assert 49 not in {controller.layer for controller in controllers}
    assert 51 not in {controller.layer for controller in controllers}
    for capture, active in ((preview, 0), (bottom_preview, 2)):
        objects = {
            name: (roles, opacity, element)
            for name, roles, opacity, element in drawing_objects(capture)
        }
        assert "active" in objects[f"copper-{active}"][0]
        for layer, color in colors.items():
            if layer == active:
                assert objects[f"copper-{layer}"][2].get("stroke") == color
            else:
                assert f"copper-{layer}" not in objects
    assert all(controller.closed for controller in controllers)
    assert all(not controller.filename.parent.exists() for controller in controllers)
    assert board.__dict__ == original


@pytest.mark.parametrize("missing", ["enum", "setter"])
def test_renderer_requires_stroke_font_capability_for_rasterizable_text(
    tmp_path: Path, missing: str
) -> None:
    """Refuse an SVG mode whose labels NanoSVG would silently omit."""
    module = pcbnew()
    fake_plotter(module, tmp_path)
    if missing == "enum":
        del module.PLOT_TEXT_MODE_STROKE
    else:
        original_options = module.PLOT_CONTROLLER.GetPlotOptions

        def options_without_text_mode(controller: Any) -> Any:
            """Model a supported plot controller lacking the required text setter."""
            options = original_options(controller)
            options.SetTextMode = None
            return options

        module.PLOT_CONTROLLER.GetPlotOptions = options_without_text_mode
    with pytest.raises(RenderError, match="[Tt]ext|[Ss]troke"):
        SectionRenderer(FakeBoard(), module, png_rasterizer).render(
            section(), tmp_path / "invisible-labels.png"
        )
    assert not (tmp_path / "invisible-labels.png").exists()


def test_renderer_reports_detached_board_failure_before_plotting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surface clone failures without plotting the live board or replacing prior output."""
    module = pcbnew()
    controllers = fake_plotter(module, tmp_path)

    def cannot_copy(board: Any, pcbnew_module: Any) -> Any:
        """Model a native copy capability failure at the renderer's boundary."""
        assert board is not None and pcbnew_module is module
        raise renderer_module.BoardCopyError("Native board copy failed.")

    monkeypatch.setattr(renderer_module, "copy_for_render", cannot_copy)
    destination = tmp_path / "previous.png"
    destination.write_bytes(b"previous valid image")
    with pytest.raises(RenderError, match="Native board copy failed"):
        SectionRenderer(FakeBoard(), module, png_rasterizer).render(
            section(), destination
        )
    assert not controllers
    assert destination.read_bytes() == b"previous valid image"


@pytest.mark.parametrize(
    "offset,mirrored", [((1, 0), False), ((0, 1), False), ((0, 0), True)]
)
def test_renderer_rejects_unexpected_native_plot_transform(
    tmp_path: Path, offset: tuple[int, int], mirrored: bool
) -> None:
    """Check the actual plotter origin instead of relying only on options."""
    module = pcbnew()
    controllers = fake_plotter(module, tmp_path)

    def get_plotter(controller: Any) -> Any:
        """Expose the backend's actual transform after opening the SVG."""
        assert controller in controllers
        return SimpleNamespace(
            GetPlotOffsetUserUnits=lambda: SimpleNamespace(x=offset[0], y=offset[1]),
            GetPlotMirrored=lambda: mirrored,
        )

    module.PLOT_CONTROLLER.GetPlotter = get_plotter
    with pytest.raises(RenderError, match="origin|mirrored"):
        SectionRenderer(FakeBoard(), module, png_rasterizer).render(
            section(), tmp_path / "result.png"
        )
    assert controllers[0].closed
    assert not (tmp_path / "result.png").exists()


@pytest.mark.parametrize("failure", ["open", "plot", "missing"])
def test_renderer_cleans_up_and_preserves_previous_output_on_plot_failure(
    tmp_path: Path, failure: str
) -> None:
    """Do not leak plot handles or overwrite an existing PNG on failure."""
    module = pcbnew()
    controllers = fake_plotter(module, tmp_path, failure)
    destination = tmp_path / "existing.png"
    destination.write_bytes(b"old")
    with pytest.raises(RenderError):
        SectionRenderer(FakeBoard(), module, png_rasterizer).render(
            section(), destination
        )
    assert controllers[0].closed
    assert not controllers[0].filename.parent.exists()
    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize("failure", ["missing", "corrupt", "wrong_size"])
def test_renderer_rejects_invalid_raster_output(tmp_path: Path, failure: str) -> None:
    """Avoid embedding missing, corrupt or incorrectly sized images."""
    module = pcbnew()
    fake_plotter(module, tmp_path)
    temporary_paths = []

    def rasterizer(source: Path, destination: Path, width: int, height: int) -> None:
        """Produce one specific rasterization failure."""
        temporary_paths.append(source.parent)
        if failure == "corrupt":
            destination.write_bytes(b"broken")
        elif failure == "wrong_size":
            png_rasterizer(source, destination, width + 1, height)

    destination = tmp_path / "result.png"
    with pytest.raises(RenderError):
        SectionRenderer(FakeBoard(), module, rasterizer).render(section(), destination)
    assert not destination.exists()
    assert all(not path.exists() for path in temporary_paths)


def test_missing_wx_svg_reports_capability_instead_of_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give users a clear explanation when the KiCad rasterizer is absent."""

    def missing(name: str) -> Any:
        """Simulate a KiCad runtime without wx.svg."""
        raise ImportError(name)

    monkeypatch.setattr("impedance.render.importlib.import_module", missing)
    with pytest.raises(RenderError, match="wx.svg"):
        _wx_rasterize(tmp_path / "source.svg", tmp_path / "target.png", 800, 420)
