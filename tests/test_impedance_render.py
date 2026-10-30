"""Exercise impedance extraction and off-screen render geometry without KiCad."""

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
from types import SimpleNamespace
from typing import Any, Optional
import weakref
from xml.etree import ElementTree as ET

import pytest

from impedance import render as renderer_module
from impedance.model import BoardSnapshot, Bounds, Section, Trace
from impedance.pcbnew_adapter import (
    BoardSnapshotError,
    _footprint_context_record,
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
from tests.test_impedance_controls import harness as _capture_harness
from tests.test_impedance_text_context import Footprint, NativeText
from tests.wx_harness import load_siblings, wx_stubs

capture_harness = _capture_harness

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="240mm" height="120mm" '
    'viewBox="0 0 24000 12000"><path d="M 10000,4000 L 11000,4000" '
    'fill="none" stroke="#c83434" stroke-width="20"/></svg>'
)


def _svg_paint(node: ET.Element) -> dict[str, str]:
    """Compare native presentation independent of inline-style serialization."""
    paint = dict(node.attrib)
    style = paint.pop("style", "")
    paint.update(field.split(":", 1) for field in style.split(";") if ":" in field)
    return paint


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native raster checks require an enabled desktop session",
)
@pytest.mark.parametrize(
    "variant, prepared",
    (
        ("trace", False),
        ("original", False),
        ("original", True),
        ("combined", False),
        ("combined", True),
    ),
)
@pytest.mark.parametrize("scene", ("gradient", "inline-transform", "malformed-path"))
def test_native_raster_roundtrip_preserves_inherited_gradient_stops(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    variant: str,
    prepared: bool,
    scene: str,
) -> None:
    """Internal XML pruning must retain NanoSVG's literal xlink:href references."""
    from tests.native_wx_support import isolated_native

    if isolated_native(request, "KICAD_IMPEDANCE_GRADIENT_RASTER_CHILD"):
        return
    wx = pytest.importorskip("wx")
    svg = pytest.importorskip("wx.svg")
    from impedance.spacing_review import SPACING_RENDER_POLICY_REVISION

    app = wx.GetApp() or wx.App(False)
    assert app
    geometry = (
        '<defs><linearGradient id="base" x1="0%" y1="0%" x2="100%" y2="0%">'
        '<stop offset="0%" stop-color="#ff0000"/>'
        '<stop offset="100%" stop-color="#0000ff"/>'
        '</linearGradient><linearGradient id="derived" xlink:href="#base"/></defs>'
        '<rect x="10" y="10" width="100" height="60" fill="url(#derived)"/>'
        '<circle cx="5000" cy="5000" r="5" fill="#00ff00"/>'
    )
    height = 80
    if scene == "inline-transform":
        geometry = '<circle cx="500" cy="50" r="10" fill="#ff0000" style="transform:translate(-450,0)"/>'
    elif scene == "malformed-path":
        geometry = '<path d="M 500 50 L 500 M 60 500 L 70 500 L 80 500 L 90" fill="none" stroke="#ff0000" stroke-width="10"/>'
        height = 1000
    attributes = (
        'xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" width="120" height="{height}" '
        f'viewBox="0 0 120 {height}"'
    )
    background = f'<rect width="120" height="{height}" fill="#10151d"/>'
    view = (
        "data-impedance-view" if variant == "trace" else "data-impedance-spacing-view"
    )
    group = (
        f'<g {view}="detail" transform="translate(0.000000,0.000000) scale(1.000000000)">'
        + geometry
        + "</g>"
    )
    expected_source = tmp_path / "expected.svg"
    expected_source.write_text(f"<svg {attributes}>{background}{group}</svg>")
    if variant == "trace":
        content = expected_source.read_text()
    else:
        marker = (
            f' data-spacing-composition="{SPACING_RENDER_POLICY_REVISION}"'
            if variant == "combined"
            else ""
        )
        content = (
            f"<svg {attributes}{marker}>{background}"
            f'<svg x="0" y="0" width="120" height="{height}" data-spacing-raster-panel="detail">'
            + background
            + group
            + "</svg></svg>"
        )
    source = tmp_path / "capture.svg"
    source.write_text(content)
    expected_bitmap = svg.SVGimage.CreateFromFile(str(expected_source)).ConvertToBitmap(
        width=120, height=height
    )
    expected = expected_bitmap.ConvertToImage()
    destination = tmp_path / "actual.png"
    prepared_panels = None
    if prepared:
        document = ET.fromstring(content)  # noqa: S314 -- Literal local SVG.
        renderer_module._raster_svg_namespaces(document)
        panel = next(
            node for node in document.iter() if node.get("data-spacing-raster-panel")
        )
        group = next(node for node in panel if node.get("data-impedance-spacing-view"))
        fragments = tuple(
            renderer_module._prepare_raster_fragment(
                node, renderer_module._style(group)
            )
            for node in group
        )
        visible = "".join(
            fragment.visible((0, 0, 120, height), 2) for fragment in fragments
        )
        selected = ET.fromstring(  # noqa: S314 -- Locally generated fragments.
            '<g xmlns:xlink="http://www.w3.org/1999/xlink">' + visible + "</g>"
        )
        renderer_module._raster_svg_namespaces(selected)
        group[:] = list(selected)
        renderer_module._raster_svg_namespaces(panel)
        prepared_panels = (ET.tostring(panel, encoding="unicode"),)
    _wx_rasterize(source, destination, 120, height, prepared_panels=prepared_panels)
    actual = wx.Image(str(destination))
    assert actual.IsOk()
    # These independently sampled colors establish that the baseline gradient
    # uses its inherited stops rather than both images silently painting black.
    if scene == "gradient":
        assert expected.GetRed(20, 40) > expected.GetBlue(20, 40)
        assert expected.GetBlue(100, 40) > expected.GetRed(100, 40)
    elif scene == "inline-transform":
        assert expected.GetRed(50, 50) > 200
    else:
        assert expected.GetRed(70, 500) > 200
    assert bytes(actual.GetData()) == bytes(expected.GetData())


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native raster checks require an enabled desktop session",
)
def test_native_generated_original_preserves_existing_gradient_capture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real generated Original keeps the prefix accepted by the prior NanoSVG path."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries
    from tests.native_wx_support import isolated_native
    from tests.test_impedance_spacing_checks import _parallel
    from tests.test_impedance_spacing_render import _renderer
    from tests.test_impedance_spacing_review_entries import _config

    if isolated_native(request, "KICAD_IMPEDANCE_GENERATED_GRADIENT_CHILD"):
        return
    wx = pytest.importorskip("wx")
    svg = pytest.importorskip("wx.svg")
    app = wx.GetApp() or wx.App(False)
    assert app
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    native = (
        renderer.context_svg(entry.layer)
        .replace("<svg ", '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ', 1)
        .replace(
            "</svg>",
            '<defs><linearGradient id="base"><stop offset="0" stop-color="#ff0000"/>'
            '<stop offset="1" stop-color="#0000ff"/></linearGradient>'
            '<linearGradient id="derived" xlink:href="#base"/></defs>'
            '<rect x="0" y="0" width="1000" height="1000" fill="url(#derived)"/></svg>',
        )
    )
    monkeypatch.setattr(renderer, "context_svg", lambda layer: native)
    renderer._rasterizer = _wx_rasterize
    renderer._use_native_panels = True
    original = renderer.spacing_original_svg(entry)
    dimensions = entry.composition.detail_rect[2:]
    expected = (
        svg.SVGimage.CreateFromBytes(original.encode())
        .ConvertToBitmap(width=dimensions[0], height=dimensions[1])
        .ConvertToImage()
    )
    x, y = dimensions[0] // 2, dimensions[1] // 2
    assert expected.GetRed(x, y) > 40 or expected.GetBlue(x, y) > 40
    destination = tmp_path / "original.png"
    renderer.render_spacing_original(entry, destination)
    assert renderer.spacing_original_svg(entry) == original
    actual = wx.Image(str(destination))
    assert actual.IsOk()
    assert bytes(actual.GetData()) == bytes(expected.GetData())


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native raster checks require an enabled desktop session",
)
def test_native_trace_png_reuses_prepared_layer_before_applying_emphasis(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Native PNG paint work is proportional to visible copper, with identical pixels."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries
    from tests.native_wx_support import isolated_native
    from tests.test_impedance_spacing_checks import _parallel
    from tests.test_impedance_spacing_review_entries import _config

    if isolated_native(request, "KICAD_IMPEDANCE_TRACE_PREPARED_CHILD"):
        return
    wx = pytest.importorskip("wx")
    svg = pytest.importorskip("wx.svg")
    app = wx.GetApp() or wx.App(False)
    assert app
    native = SVG.replace(
        "</svg>",
        "".join(
            f'<path d="M {i},0 L {i},10" fill="none" stroke="#c83434" stroke-width="1"/>'
            for i in range(500)
        )
        + "</svg>",
    )
    module = pcbnew()
    fake_plotter(module, tmp_path, sources={0: native})
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    with SectionRenderer(FakeBoard(), module) as renderer:
        renderer.spacing_svg(entry)
        full = renderer.svg(section())
        expected_bitmap = svg.SVGimage.CreateFromBytes(full.encode()).ConvertToBitmap(
            width=800, height=420
        )
        expected = expected_bitmap.ConvertToImage()
        emphasized: list[int] = []
        parsed: list[str] = []
        emphasize = renderer_module._emphasize_native_traces
        bounds = renderer_module._native_shape_bounds

        def count_emphasis(root: ET.Element, *args: Any, **kwargs: Any) -> None:
            """Count the real paint traversal without replacing its behavior."""
            emphasized.append(
                sum(node.tag in renderer_module._SHAPES for node in root.iter())
            )
            emphasize(root, *args, **kwargs)

        def count_bounds(node: ET.Element) -> Any:
            """Detect geometry decoding after immutable scene preparation."""
            parsed.append(node.tag)
            return bounds(node)

        monkeypatch.setattr(renderer_module, "_emphasize_native_traces", count_emphasis)
        monkeypatch.setattr(renderer_module, "_native_shape_bounds", count_bounds)
        destination = tmp_path / "trace.png"
        renderer.render(section(), destination)
        actual = wx.Image(str(destination))
        assert bytes(actual.GetData()) == bytes(expected.GetData())
        assert emphasized == [1]
        assert parsed == []
        monkeypatch.setattr(renderer_module, "_emphasize_native_traces", emphasize)
        assert renderer.svg(section()) == full


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native raster checks require an enabled desktop session",
)
@pytest.mark.parametrize("method", ("svg", "spacing_svg", "spacing_original_svg"))
@pytest.mark.parametrize("scope", ("instance", "subclass"))
def test_native_png_respects_public_svg_overrides(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method: str,
    scope: str,
) -> None:
    """Native optimizations retain pixels explicitly supplied by public methods."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries
    from tests.native_wx_support import isolated_native
    from tests.test_impedance_spacing_checks import _parallel
    from tests.test_impedance_spacing_review_entries import _config

    if isolated_native(request, "KICAD_IMPEDANCE_PUBLIC_SVG_OVERRIDE_CHILD"):
        return
    wx = pytest.importorskip("wx")
    app = wx.GetApp() or wx.App(False)
    assert app
    module = pcbnew()
    fake_plotter(module, tmp_path)
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    dimensions = (
        (800, 420)
        if method == "svg"
        else (
            (1600, 1000)
            if method == "spacing_svg"
            else entry.composition.detail_rect[2:]
        )
    )
    width, height = dimensions
    supplied = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="{width}" height="{height}" fill="#ff0000"/></svg>'
    renderer_type = (
        SectionRenderer
        if scope == "instance"
        else type(
            "CustomSectionRenderer",
            (SectionRenderer,),
            {method: lambda *args, **kwargs: supplied},
        )
    )
    with renderer_type(FakeBoard(), module) as renderer:
        if scope == "instance":
            monkeypatch.setattr(renderer, method, lambda *args, **kwargs: supplied)
        destination = tmp_path / "override.png"
        if method == "svg":
            renderer.render(section(), destination)
        elif method == "spacing_svg":
            renderer.render_spacing(entry, destination)
        else:
            renderer.render_spacing_original(entry, destination)
        image = wx.Image(str(destination))
        assert (
            image.GetRed(width // 2, height // 2),
            image.GetGreen(width // 2, height // 2),
            image.GetBlue(width // 2, height // 2),
        ) == (255, 0, 0)


@pytest.mark.parametrize("explicit", (False, True))
def test_wrapped_four_argument_default_backend_keeps_public_svg_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: bool
) -> None:
    """A wraps decorator cannot imply support for private rasterizer keywords."""
    from impedance.matching import analyze
    from impedance.spacing_review import spacing_review_entries
    from tests.test_impedance_spacing_checks import _parallel
    from tests.test_impedance_spacing_review_entries import _config

    captured: list[str] = []

    @wraps(renderer_module._wx_rasterize)
    def four_arguments(
        source: Path, destination: Path, width: int, height: int
    ) -> None:
        """Keep the established four-argument callback despite wrapped metadata."""
        captured.append(source.read_text())
        png_rasterizer(source, destination, width, height)

    if not explicit:
        monkeypatch.setattr(renderer_module, "_wx_rasterize", four_arguments)
    module = pcbnew()
    fake_plotter(module, tmp_path)
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    with SectionRenderer(
        FakeBoard(), module, rasterizer=four_arguments if explicit else None
    ) as renderer:
        expected = renderer.spacing_svg(entry)
        renderer.render_spacing(entry, tmp_path / "wrapped.png")
    assert captured == [expected]


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
    names = {
        0: "F.Cu",
        2: "B.Cu",
        4: "In1.Cu",
        37: "F.SilkS",
        36: "B.SilkS",
        44: "Edge.Cuts",
        49: "F.Fab",
        47: "F.CrtYd",
        40: "Dwgs.User",
    }
    return SimpleNamespace(
        PCB_VIA=FakeVia,
        F_Cu=0,
        In1_Cu=4,
        B_Cu=2,
        PCB_IO_KICAD_SEXPR=FakeSerializer,
        LayerName=names.__getitem__,
    )


class MetadataField(NativeText):
    """Store real mutable text and visibility in native-style property records."""

    def __init__(self, name: str, value: str, *, visible: bool = True) -> None:
        super().__init__(f"field-{name}", value, "PCB_FIELD")
        self.name, self.visible = name, visible

    def SetVisible(self, visible: bool) -> None:
        """Change the same visibility state subsequently read and serialized."""
        self.visible = visible

    @property
    def serialized(self) -> str:
        """Include complete field identity and quoted text, as native Format does."""
        hidden = "" if self.visible else "(hide yes)"
        return (
            f"(property {json.dumps(self.name)} {json.dumps(self.raw)} "
            f'(layer "{self.style[0]}") {hidden} (uuid {json.dumps(self.uid)}))'
        )


class MetadataFootprint(Footprint):
    """Mutate the fields subsequently returned by enumeration and serialization."""

    def __init__(self) -> None:
        super().__init__((), ())
        self.pad_size = 100
        self.fill = 20
        self.attributes = 0
        self.description = ""
        self.keywords = ""

    def GetAttributes(self) -> int:
        """Read the same flags production BOM/POS helpers mutate."""
        return self.attributes

    def SetAttributes(self, attributes: int) -> None:
        """Persist flags for native-style serialization on later snapshots."""
        self.attributes = attributes

    def SetField(self, name: str, value: str) -> None:
        """Change an existing field or add an initially visible native user field."""
        for field in self.fields:
            if field.name == name:
                field.SetText(value)
                return
        self.fields += (MetadataField(name, value),)

    @property
    def serialized(self) -> str:
        """Retain unrelated copper and generated fill polygons around properties."""
        metadata = ""
        flags = [
            name
            for flag, name in (
                (4, "exclude_from_pos_files"),
                (8, "exclude_from_bom"),
                (16, "dnp"),
            )
            if self.attributes & flag
        ]
        if flags:
            metadata += "(attr " + " ".join(flags) + ")"
        if self.description:
            metadata += "(descr " + json.dumps(self.description) + ")"
        if self.keywords:
            metadata += "(tags " + json.dumps(self.keywords) + ")"
        metadata += "".join(getattr(item, "serialized", "") for item in self.graphics)
        return (
            '(footprint "fixture" (at 1 2) '
            + "\n".join(field.serialized for field in self.fields)
            + metadata
            + f' (pad "1" smd rect (size {self.pad_size} 100))'
            + ' (zone (layer "F.Cu") (fill yes) '
            + f"(filled_polygon (pts (xy {self.fill} 30)))))"
        )


def metadata_snapshot(board: FakeBoard) -> BoardSnapshot:
    """Supply fixed class metadata independently of the field regression."""
    return replace(
        snapshot_board(board, pcbnew()),
        net_classes=("RF",),
        net_class_memberships=(("SIGNAL", ("RF",)),),
        net_class_context_digest="metadata-review-fixture",
        net_class_error="",
    )


def nonplotted_metadata_edits(
    footprint: MetadataFootprint, board: FakeBoard
) -> Iterator[None]:
    """Exercise real toggles and stateful native-style metadata/drawing edits."""
    with load_siblings(
        "_impedance_metadata_toggle_tests", ("footprint_helpers",), wx_stubs()
    ) as loaded:
        helpers = loaded["footprint_helpers"]
        for toggle in (
            helpers.toggle_exclude_from_bom,
            helpers.toggle_exclude_from_pos,
        ):
            assert toggle(footprint) is True
            yield
            assert toggle(footprint) is False
            yield
    footprint.description = "Updated unconsumed description"
    yield
    footprint.keywords = "Updated unconsumed keywords"
    yield
    field = MetadataField("Value", "10k")
    field.style = ("F.Fab", 90, 1000)
    footprint.fields = (field,)
    yield
    field.SetText("20k")
    yield
    for layer in ("F.Fab", "F.CrtYd", "Dwgs.User"):
        drawing = SimpleNamespace(
            serialized=f'(fp_line (start 0 0) (end 1 1) (layer "{layer}"))'
        )
        footprint.graphics = (drawing,)
        yield
        drawing.serialized = drawing.serialized.replace("end 1 1", "end 2 2")
        yield
        board_drawing = SimpleNamespace(
            serialized=drawing.serialized.replace("fp_", "gr_")
        )
        board.GetDrawings = lambda item=board_drawing: (item,)
        yield
    footprint.fields, footprint.graphics = (), ()
    board.GetDrawings = lambda: ()
    yield


def test_hidden_field_edits_preserve_saved_approval_and_cached_captures(
    capture_harness: SimpleNamespace, tmp_path: Path
) -> None:
    """Use actual snapshot, capture-cache, approval and persistence boundaries."""
    from impedance.database import ImpedanceDatabase
    from impedance.matching import analyze, validate_review
    from impedance.model import Config, LayerSettings, Specification
    from impedance.repository import ImpedanceRepository
    from impedance.review_tracking import (
        ImageView,
        LayerApproval,
        ReviewTracking,
        capture_fingerprint,
        layer_approval_current,
        layer_fingerprint,
        source_fingerprint,
    )

    footprint = MetadataFootprint()
    board = FakeBoard((FakeTrack(),), context=(footprint,))
    snapshot = metadata_snapshot(board)
    spec = Specification(
        "rf", "RF", "50", "single_ended", "RF", (LayerSettings("F.Cu", ("B.Cu",)),)
    )
    config = Config(True, (spec,))
    analysis = analyze(config, snapshot)
    row = analysis.sections[0]
    controls = SimpleNamespace(
        _selected_output_variant=lambda: None,
        _snapshot=lambda: metadata_snapshot(board),
        parent=SimpleNamespace(pcbnew=SimpleNamespace(GetBoard=lambda: board)),
    )
    captures = capture_harness.integration.CaptureSession(controls, tmp_path)
    try:
        image = captures.preview(row)
        source = source_fingerprint(snapshot)
        digest = capture_fingerprint(source, spec, row, image.sha256)
        timestamp = "2026-10-16T12:00:00.000000Z"
        tracking = ReviewTracking(
            (ImageView("rf", "F.Cu", row.section_id, digest, timestamp),),
            (
                LayerApproval(
                    "rf",
                    "F.Cu",
                    layer_fingerprint(source, spec, "F.Cu", analysis.sections),
                    timestamp,
                    ((row.section_id, digest),),
                ),
            ),
        )
        saved = replace(
            config,
            reviewed_digest=analysis.digest,
            included_section_ids=(row.section_id,),
            review_tracking=tracking,
        )
        database_path = tmp_path / "jlcpcb" / "project.db"
        board_path = tmp_path / "metadata.kicad_pcb"
        board_path.write_text("(kicad_pcb)", encoding="utf-8")
        database = ImpedanceDatabase(database_path)
        board_id = database.resolve_board(board_path)
        assert ImpedanceRepository(database, board_id).save(saved, 0) == 1
        stored = database.load_config(board_id)

        def hidden_edits() -> Iterator[None]:
            """Include creation, update, clear and deletion of hidden assignments."""
            for value in ("C123", 'C456 (hide yes) "quoted" \\ tail', "", None):
                if value is None:
                    footprint.fields = ()
                else:
                    footprint.SetField("LCSC", value)
                    footprint.fields[0].SetVisible(False)
                yield

        for edits in (hidden_edits(), nonplotted_metadata_edits(footprint, board)):
            for _ in edits:
                serialized = footprint.serialized
                current = metadata_snapshot(board)
                assert current == snapshot
                assert footprint.serialized == serialized
                assert captures.preview(row) is image
                reopened_database = ImpedanceDatabase(database_path)
                reopened, revision = ImpedanceRepository(
                    reopened_database, board_id
                ).load()
                assert (reopened, revision) == (saved, 1)
                current_analysis = analyze(reopened, current)
                validate_review(reopened, current_analysis)
                assert reopened.review_tracking == tracking
                assert layer_approval_current(
                    reopened.review_tracking,
                    source_fingerprint(current),
                    spec,
                    "F.Cu",
                    current_analysis.sections,
                    capture_digests={row.section_id: digest},
                )
                assert reopened_database.load_config(board_id) == stored
        assert len(capture_harness.renderers) == len(capture_harness.renders) == 1
        assert not capture_harness.renderers[0].closed
        footprint.SetField("LCSC", "C789")
        footprint.fields[0].SetVisible(True)
        assert captures.preview(row) is not image
        assert capture_harness.renderers[0].closed
        reopened, _revision = ImpedanceRepository(
            ImpedanceDatabase(database_path), board_id
        ).load()
        with pytest.raises(ValueError, match="changed"):
            validate_review(reopened, analyze(reopened, metadata_snapshot(board)))
        assert reopened.review_tracking == tracking
        assert database.load_config(board_id) == stored
    finally:
        captures.close()


@pytest.mark.parametrize("attribute", [4, 8, 12])
def test_bom_and_pos_flags_are_not_capture_dependencies(attribute: int) -> None:
    """Assembly export flags do not change the native layers in captures."""
    footprint = MetadataFootprint()
    board = FakeBoard(context=(footprint,))
    baseline = snapshot_board(board, pcbnew())
    footprint.SetAttributes(attribute)
    assert snapshot_board(board, pcbnew()) == baseline
    footprint.SetAttributes(attribute | 16)
    assert snapshot_board(board, pcbnew()) != baseline


@pytest.mark.parametrize("name", ["description", "keywords"])
def test_unconsumed_footprint_metadata_is_not_a_capture_dependency(name: str) -> None:
    """Description and keywords have no plotted geometry of their own."""
    footprint = MetadataFootprint()
    board = FakeBoard(context=(footprint,))
    baseline = snapshot_board(board, pcbnew())
    setattr(footprint, name, 'changed "(hide yes)" metadata')
    assert snapshot_board(board, pcbnew()) == baseline


def test_value_on_fab_layer_is_not_a_capture_dependency() -> None:
    """Even visible fields are omitted when their layer is never plotted."""
    footprint = MetadataFootprint()
    field = MetadataField("Value", "10k")
    field.style = ("F.Fab", 90, 1000)
    footprint.fields = (field,)
    board = FakeBoard(context=(footprint,))
    baseline = snapshot_board(board, pcbnew())
    field.SetText("20k")
    assert snapshot_board(board, pcbnew()) == baseline
    field.style = ("F.SilkS", 90, 1000)
    assert snapshot_board(board, pcbnew()) != baseline


@pytest.mark.parametrize("location", ["footprint", "board"])
@pytest.mark.parametrize("layer", ["F.Fab", "F.CrtYd", "Dwgs.User"])
def test_unplotted_graphics_do_not_change_capture_revision(
    location: str, layer: str
) -> None:
    """Both collections follow the same context-layer policy as native plotting."""
    footprint = MetadataFootprint()
    board = FakeBoard(context=(footprint,))
    baseline = snapshot_board(board, pcbnew())
    prefix = "fp" if location == "footprint" else "gr"
    graphic = SimpleNamespace(
        serialized=f'({prefix}_line (start 0 0) (end 1 1) (layer "{layer}"))'
    )
    if location == "footprint":
        footprint.graphics = (graphic,)
    else:
        board.GetDrawings = lambda: (graphic,)
    assert snapshot_board(board, pcbnew()) == baseline
    graphic.serialized = graphic.serialized.replace("end 1 1", "end 2 2")
    assert snapshot_board(board, pcbnew()) == baseline
    graphic.serialized = graphic.serialized.replace(layer, "F.SilkS")
    assert snapshot_board(board, pcbnew()) != baseline
    graphic.serialized = graphic.serialized.replace("F.SilkS", "Edge.Cuts")
    assert snapshot_board(board, pcbnew()) != baseline


def test_visible_fields_and_copper_changes_still_invalidate_context() -> None:
    """Metadata normalization must retain visibility, pad geometry and filled pixels."""
    footprint = MetadataFootprint()
    footprint.SetField("LCSC", "C123")
    footprint.fields[0].SetVisible(False)
    board = FakeBoard(context=(footprint,))
    hidden = snapshot_board(board, pcbnew())
    footprint.fields[0].SetVisible(True)
    visible = snapshot_board(board, pcbnew())
    assert visible != hidden
    footprint.SetField("LCSC", 'C456 (hide yes) "quoted" \\ tail')
    edited = snapshot_board(board, pcbnew())
    assert edited != visible
    footprint.fields[0].SetVisible(False)
    assert snapshot_board(board, pcbnew()) == hidden
    footprint.pad_size += 1
    geometry = snapshot_board(board, pcbnew())
    assert geometry != hidden
    footprint.fill += 1
    assert snapshot_board(board, pcbnew()) != geometry


def test_visible_text_referencing_hidden_field_invalidates_context() -> None:
    """The displayed dependency, rather than the hidden property's raw value, matters."""
    footprint = MetadataFootprint()
    footprint.SetField("LCSC", "C123")
    footprint.fields[0].SetVisible(False)

    class Consumer(NativeText):
        """Resolve against actual mutable footprint state on each native text read."""

        def _shown(self) -> str:
            return footprint.fields[0].GetText()

    consumer = Consumer("visible-consumer", "${LCSC}")
    footprint.graphics = (consumer,)
    board = FakeBoard(context=(footprint,))
    original = snapshot_board(board, pcbnew())
    footprint.SetField("LCSC", "C456")
    assert snapshot_board(board, pcbnew()) != original
    assert consumer.writes == []


def test_footprint_context_filter_preserves_nested_and_quoted_data() -> None:
    """Only direct property hide flags authorize omission; all other tokens survive."""
    retained = (
        '(footprint "quoted (hide yes)" '
        '(property "visible" "(hide yes)" (hide no)) '
        '(extension (property "nested" "keep" (hide yes))) '
        "(zone (fill yes) (filled_polygon (pts (xy 12 34)))))"
    )
    hidden = '(property "LCSC" "C123" (hide yes) (uuid "new"))'
    filtered = _footprint_context_record(retained[:-1] + "\n" + hidden + ")")
    assert filtered == _footprint_context_record(retained)
    assert "filled_polygon" in filtered
    assert "extension" in filtered
    assert "nested" in filtered
    assert "visible" in filtered
    assert filtered != _footprint_context_record(retained.replace("12", "13"))


def test_context_filter_retains_unknown_data_and_nonexport_attributes() -> None:
    """Audited omissions cannot erase nested extensions or unknown attribute flags."""
    original = (
        '(footprint "fixture" (attr smd) '
        '(extension (descr "keep") (property "nested" "keep" (layer "F.Fab"))) '
        '(future_primitive (layer "F.Fab") (detail "keep")))'
    )
    baseline = _footprint_context_record(original)
    excluded = original.replace(
        "attr smd", "attr smd exclude_from_bom exclude_from_pos_files"
    )
    assert _footprint_context_record(excluded) == baseline
    for flag in ("dnp", "board_only", "future_attribute"):
        assert (
            _footprint_context_record(original.replace("attr smd", f"attr smd {flag}"))
            != baseline
        )
    assert (
        _footprint_context_record(original.replace('detail "keep"', 'detail "changed"'))
        != baseline
    )
    assert (
        _footprint_context_record(original.replace('descr "keep"', 'descr "changed"'))
        != baseline
    )


@pytest.mark.parametrize(
    "record",
    [
        '(gr_poly (layers "F.Fab" "F.Cu") (pts (xy 0 0)))',
        '(gr_poly (layer "Future.Layer") (pts (xy 0 0)))',
        '(future_primitive (layer "F.Fab") (detail "original"))',
        "(gr_line (start 0 0) (end 1 1))",
    ],
)
def test_unknown_or_partially_plotted_board_graphics_remain_dependencies(
    record: str,
) -> None:
    """Missing, multiple and unfamiliar layer semantics are retained conservatively."""
    board = FakeBoard()
    baseline = snapshot_board(board, pcbnew())
    item = SimpleNamespace(serialized=record)
    board.GetDrawings = lambda: (item,)
    assert snapshot_board(board, pcbnew()) != baseline


@pytest.mark.parametrize(
    "record", ['(footprint "incomplete"', '(footprint "quote)', "(footprint) (extra)"]
)
def test_malformed_footprint_context_is_an_actionable_error(record: str) -> None:
    """Malformed records must never authorize a partial context fingerprint."""
    with pytest.raises(BoardSnapshotError, match="snapshot"):
        _footprint_context_record(record)


@pytest.mark.parametrize(
    "previous_context",
    [
        "559cf56ba9d55b41d6e7f7124c4fe306a1da7c5611881524d628f6d759ace3be",
        "3a20d368ef227242ecef30be8ee6e204a192623f00f590dc30f9d39fbc353c7c",
    ],
)
def test_previous_context_policy_requires_review_and_preserves_stored_history(
    tmp_path: Path, previous_context: str
) -> None:
    """An old source digest cannot be migrated into fresh manufacturing approval."""
    from impedance.database import ImpedanceDatabase
    from impedance.matching import analyze, validate_review
    from impedance.model import Config, LayerSettings, Specification
    from impedance.repository import ImpedanceRepository
    from impedance.review_tracking import (
        ImageView,
        LayerApproval,
        ReviewTracking,
        capture_fingerprint,
        layer_approval_current,
        layer_fingerprint,
        source_fingerprint,
    )

    footprint = MetadataFootprint()
    board = FakeBoard((FakeTrack(),), context=(footprint,))
    current = metadata_snapshot(board)
    # Actual digests from the policies before and after hidden-field normalization.
    previous = replace(current, context_digest=previous_context)
    spec = Specification(
        "rf", "RF", "50", "single_ended", "RF", (LayerSettings("F.Cu", ("B.Cu",)),)
    )
    config = Config(True, (spec,))
    old_analysis = analyze(config, previous)
    row = old_analysis.sections[0]
    old_source = source_fingerprint(previous)
    old_capture = capture_fingerprint(old_source, spec, row, "a" * 64)
    timestamp = "2026-10-16T12:00:00.000000Z"
    history = ReviewTracking(
        (ImageView("rf", "F.Cu", row.section_id, old_capture, timestamp),),
        (
            LayerApproval(
                "rf",
                "F.Cu",
                layer_fingerprint(old_source, spec, "F.Cu", old_analysis.sections),
                timestamp,
                ((row.section_id, old_capture),),
            ),
        ),
    )
    previously_approved = replace(
        config,
        reviewed_digest=old_analysis.digest,
        included_section_ids=(row.section_id,),
        review_tracking=history,
    )
    validate_review(previously_approved, old_analysis)
    assert layer_approval_current(
        history, old_source, spec, "F.Cu", old_analysis.sections
    )
    database_path = tmp_path / "jlcpcb" / "project.db"
    board_path = tmp_path / "metadata.kicad_pcb"
    board_path.write_text("(kicad_pcb)", encoding="utf-8")
    database = ImpedanceDatabase(database_path)
    board_id = database.resolve_board(board_path)
    ImpedanceRepository(database, board_id).save(previously_approved, 0)
    stored = database.load_config(board_id)
    reopened_database = ImpedanceDatabase(database_path)
    reopened, revision = ImpedanceRepository(reopened_database, board_id).load()
    with pytest.raises(ValueError, match="changed"):
        validate_review(reopened, analyze(reopened, current))
    assert revision == 1
    assert reopened.review_tracking == history
    assert reopened.included_section_ids == (row.section_id,)
    assert not layer_approval_current(
        history,
        source_fingerprint(current),
        spec,
        "F.Cu",
        analyze(reopened, current).sections,
    )
    assert reopened_database.load_config(board_id) == stored


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
    """Keep sixteen-pixel clearance without changing copper geometry or opacity."""
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
    assert _svg_paint(original[0]) == {
        "d": "M 10000,4000 L 11000,4000",
        "fill": "none",
        "stroke": "#d87171",
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
@pytest.mark.parametrize("size", [(160, 90), (90, 160), (40, 40), (38, 400), (400, 38)])
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
    # Sixteen pixels outside copper + half of a 3 px stroke + a one-pixel
    # raster guard. The guard also absorbs SVG coordinate rounding.
    assert min(margins) >= 18.5 - 0.001
    if viewport.width > old_width * 0.85 + 0.001:
        assert min(margins) == pytest.approx(18.5), (
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


def test_svg_overlay_uses_physical_board_coordinates_and_boosts_selected_copper() -> (
    None
):
    """Boost native copper contrast without changing its centerline or thickness."""
    result = annotated_svg(SVG, section())
    assert 'd="M 10000,4000 L 11000,4000"' in result
    root = ET.fromstring(result)  # noqa: S314 -- Generated SVG only.
    selected = next(item for item in root.iter() if item.tag.endswith("}path"))
    assert _svg_paint(selected)["stroke"] == "#d87171"
    assert 'stroke="black"' not in result
    assert 'stroke="#000000"' not in result
    assert _svg_paint(selected)["stroke-width"] == "20"
    assert 'width="800" height="420"' in result


@pytest.mark.parametrize("background", ["#ffffff", "#001023", "#123456"])
def test_capture_uses_selected_background_to_boost_native_stroke_contrast(
    background: str,
) -> None:
    """Light themes deepen selected strokes and dark themes brighten them."""
    source = SVG.replace("#c83434", "#101010")
    root = ET.fromstring(annotated_svg(source, section(), background_color=background))  # noqa: S314 -- Generated SVG only.
    assert root[0].tag.endswith("}rect")
    assert root[0].attrib["fill"] == background
    selected = next(item for item in root.iter() if item.tag.endswith("}path"))
    assert _svg_paint(selected)["stroke"] == (
        "#0b0b0b" if background == "#ffffff" else "#585858"
    )
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


def test_annotation_prepares_selected_geometry_once_without_copying_svg_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Annotation work must scale with input traces, without cloning fresh XML."""
    visits = []
    copies = []

    class Points(tuple):
        """Count real traversal of the immutable selected geometry."""

        def __iter__(self) -> Iterator[tuple[int, int]]:
            visits.append(True)
            return super().__iter__()

    traces = tuple(
        replace(
            section().traces[0],
            trace_id=f"selected-{index}",
            points=Points(
                (
                    (100_000_000 + index * 1_000_000, 40_000_000),
                    (101_000_000 + index * 1_000_000, 40_000_000),
                )
            ),
        )
        for index in range(32)
    )
    source = svg_page(
        "".join(
            f'<path d="M {10000 + index * 100},4000 L {10100 + index * 100},4000" '
            'fill="none" stroke="#c83434" stroke-width="20"/>'
            for index in range(160)
        )
    )
    original_copy = renderer_module.deepcopy

    def copy_node(node: Any) -> Any:
        """Count the actual node-copy boundary without suppressing its behavior."""
        copies.append(node)
        return original_copy(node)

    monkeypatch.setattr(renderer_module, "deepcopy", copy_node)
    value = replace(
        section(),
        traces=traces,
        bounds=(99_900_000, 39_900_000, 132_100_000, 40_100_000),
    )
    root = ET.fromstring(annotated_svg(source, value))  # noqa: S314 -- Generated SVG only.
    paths = [node for node in root.iter() if node.tag.endswith("}path")]
    assert len(paths) == 160
    assert [_svg_paint(path)["stroke"] for path in paths] == ["#d87171"] * 32 + [
        "#6c2428"
    ] * 128
    assert len(visits) == len(traces)
    assert not copies


def _mixed_annotation_fixture() -> tuple[str, Section]:
    """Mix inherited paint, arcs, widths, transforms and unrelated native copper."""
    straight = section().traces[0]
    arc = replace(
        straight,
        trace_id="arc",
        points=sample_arc(
            (110_000_000, 40_000_000),
            (115_000_000, 35_000_000),
            (120_000_000, 40_000_000),
            200_000,
        ),
        is_native_arc=True,
    )
    polyline = replace(
        straight,
        trace_id="polyline",
        width_nm=300_000,
        points=(
            (120_000_000, 40_000_000),
            (125_000_000, 45_000_000),
            (130_000_000, 40_000_000),
        ),
    )
    source = svg_page(
        '<g style="fill:none;stroke:#123456;stroke-width:20;stroke-opacity:0.7" opacity="0.6">'
        '<path data-object="straight" d="M 10000,4000 L 11000,4000"/>'
        '<line data-object="reversed" x1="11000" y1="4000" x2="10000" y2="4000"/>'
        '<path data-object="arc" d="M 11000,4000 A 500,500 0 0 1 12000,4000"/>'
        '<path data-object="reverse-arc" d="M 12000,4000 A 500,500 0 0 0 11000,4000"/>'
        '<path data-object="other-arc" d="M 11000,4000 A 500,500 0 0 0 12000,4000"/>'
        '<polyline data-object="polyline" points="12000,4000 12500,4500 13000,4000" stroke-width="30"/>'
        '<path data-object="wrong-width" d="M 10000,4000 L 11000,4000" stroke-width="25"/>'
        '<g transform="translate(0,0) scale(1,1)"><path data-object="identity" d="M 10000,4000 L 11000,4000"/></g>'
        '<g transform="translate(10,0)"><path data-object="translated" d="M 10000,4000 L 11000,4000"/></g>'
        '<path data-object="filled" d="M 10000,4000 L 11000,4000" fill="#abcdef" opacity="0.3"/>'
        "</g>"
    )
    return source, replace(
        section(),
        traces=(straight, arc, polyline),
        bounds=(99_900_000, 34_900_000, 130_150_000, 45_150_000),
    )


@pytest.mark.parametrize("background", ["#001023", "#ffffff"])
def test_annotation_preserves_existing_pixels_and_paint_semantics(
    background: str,
) -> None:
    """Preserve byte-identical captures across the annotation work reduction."""
    source, value = _mixed_annotation_fixture()
    svg = annotated_svg(source, value, background_color=background)
    assert (
        hashlib.sha256(svg.encode()).hexdigest()
        == {
            "#001023": "787f2a4eed50779773fb7a98ab71d8a545d85f59a99acb31b4b332d932552c94",
            "#ffffff": "a226d473c3c5819ed09a03024e5bcf7d90235a4b058d69325ba51e7820035187",
        }[background]
    )


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
        assert float(item.attrib["stroke-width"]) == 3
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


def test_layer_composition_parses_each_included_plot_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First capture validates each source once while preserving composed output."""
    sources = {
        name: svg_page(
            f'<path data-object="{name}" d="M 10000,4000 L 11000,4000" '
            f'fill="none" stroke="{color}" stroke-width="20"/>'
        )
        for name, color in (
            ("F.Cu", "#c83434"),
            ("B.SilkS", "#c0c0c0"),
            ("F.SilkS", "#ffffff"),
            ("Edge.Cuts", "#ffff00"),
        )
    }
    included = tuple(sources.values())
    sources["In1.Cu"] = "not SVG: inactive copper must never be parsed"
    parsed: list[str] = []
    parse = renderer_module._native_svg

    def counted(source: str) -> tuple[ET.Element, float, float, float]:
        """Observe real validation without replacing any of its checks."""
        parsed.append(source)
        return parse(source)

    monkeypatch.setattr(renderer_module, "_native_svg", counted)
    composed = renderer_module.compose_layer_svgs(
        sources, "F.Cu", background_color="#001023"
    )
    # Preserve the complete pre-optimization SVG, including geometry and paint.
    assert hashlib.sha256(composed.encode()).hexdigest() == (
        "905ca2dbfd1b084b1d4a32d0ee50a182459685018d658a89dfeb75b57ad4dd54"
    )
    assert parsed == list(included)


def test_layer_composition_resolves_owned_shapes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh parsed shapes need copying only when fill and stroke fork apart."""
    sources = {
        "F.Cu": svg_page(
            '<defs><linearGradient id="paint"><stop offset="0" stop-color="#c83434"/></linearGradient></defs>'
            '<g transform="translate(3,4)" opacity="0.5"><g opacity="0.5" style="fill-opacity:0.7;stroke-opacity:0.8">'
            '<path id="shape" data-object="shared" transform="scale(2)" d="M 0,0 L 24000,0 L 24000,12000 Z" fill="url(#paint)" stroke="#c83434"/>'
            '<circle data-object="pad" cx="10000" cy="4000" r="40" fill="#c83434" stroke="none"/>'
            '<path data-object="route" d="M 10000,4000 L 11000,4000" fill="none" stroke="#c83434"/>'
            "</g></g>"
        ),
        "F.SilkS": svg_page(
            '<defs><linearGradient id="paint"><stop offset="0" stop-color="#abcdef"/></linearGradient></defs>'
            '<path data-object="label" d="M 10000,4000 L 11000,4000" fill="none" stroke="url(#paint)" opacity="0.25"/>'
        ),
    }
    paint_reads: list[str] = []
    copies: list[str] = []
    read_paint = renderer_module._style
    copy_node = renderer_module.deepcopy

    def counted_paint(node: ET.Element) -> dict[str, str]:
        """Observe real native paint decoding without replacing its behavior."""
        if node.get("data-object"):
            paint_reads.append(node.attrib["data-object"])
        return read_paint(node)

    def counted_copy(node: ET.Element) -> ET.Element:
        """Count explicit XML copies while preserving ordinary deepcopy behavior."""
        copies.append(node.get("data-object", node.tag))
        return copy_node(node)

    monkeypatch.setattr(renderer_module, "_style", counted_paint)
    monkeypatch.setattr(renderer_module, "deepcopy", counted_copy)
    composed = renderer_module.compose_layer_svgs(
        sources, "F.Cu", background_color="#001023"
    )
    assert hashlib.sha256(composed.encode()).hexdigest() == (
        "744cc6cb2b72ef1df77cc14cd13dcd3f14891c987f77cbf48695c508ffb32762"
    )
    objects = drawing_objects(composed)
    assert [(name, roles[-1], alpha) for name, roles, alpha, _ in objects] == [
        ("shared", "plane", 0.105),
        ("pad", "pad", 0.175),
        ("label", "reference", 0.25),
        ("shared", "active", 0.2),
        ("route", "active", 0.2),
    ]
    for index, suffix in ((0, "fill"), (3, "stroke")):
        assert objects[index][3].get("id") == f"native-0-shape-{suffix}"
        assert objects[index][3].get("transform") == "translate(3,4) scale(2)"
    root = ET.fromstring(composed)  # noqa: S314 -- Generated SVG only.
    identifiers = [node.get("id") for node in root.iter() if node.get("id")]
    references = {
        reference
        for node in root.iter()
        for value in node.attrib.values()
        for reference in re.findall(r"url\(#([^)]+)\)", value)
    }
    assert references == {"native-0-paint", "native-2-paint"}
    assert len(identifiers) == len(set(identifiers)) == 4
    assert paint_reads == ["shared", "pad", "route", "label"]
    assert copies == ["shared"]


@pytest.mark.parametrize("attribute", ["opacity", "fill-opacity", "stroke-opacity"])
@pytest.mark.parametrize("value", ["nan", "-0.1", "1.1", "invalid"])
def test_layer_composition_rejects_invalid_inherited_paint(
    attribute: str, value: str
) -> None:
    """A paint optimization must still reject invalid inherited alpha channels."""
    source = svg_page(
        f'<g {attribute}="{value}">'
        '<path d="M 10000,4000 L 11000,4000" fill="#c83434" stroke="#123456"/>'
        "</g>"
    )
    with pytest.raises(RenderError, match=r"^KiCad SVG contains invalid opacity\.$"):
        renderer_module.compose_layer_svgs(
            {"F.Cu": source}, "F.Cu", background_color="#001023"
        )


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


def test_context_svg_can_render_without_inventing_a_routed_section(
    tmp_path: Path,
) -> None:
    """Diagnostics reuse native selected-layer context independently of routes."""
    module = pcbnew()
    controllers = fake_plotter(module, tmp_path)
    with SectionRenderer(FakeBoard(), module, png_rasterizer) as renderer:
        source = renderer.context_svg("F.Cu")
        assert renderer.context_svg("F.Cu") == source
        root = ET.fromstring(source)  # noqa: S314 -- generated local SVG fixture.
        assert root.get("data-impedance-active-copper-layer") == "F.Cu"
    assert len(controllers) == 1


def test_renderer_composes_each_copper_context_once_and_shares_noncopper_plots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detached_render_board: list[tuple[Any, Any]],
) -> None:
    """Section annotations reuse completed context without importing other copper."""
    module = pcbnew()
    module.F_SilkS, module.B_SilkS, module.Edge_Cuts = 37, 39, 44
    sources = {
        layer: SVG.replace("<path ", f'<path data-object="layer-{layer}" ')
        for layer in (0, 2, 37, 39, 44)
    }
    controllers = fake_plotter(module, tmp_path, sources=sources)
    compose = renderer_module.compose_layer_svgs
    compositions = []

    def composed_context(
        sources: dict[str, str], active_layer: str, **settings: Any
    ) -> str:
        """Count the full native flattening/composition boundary."""
        compositions.append(active_layer)
        return compose(sources, active_layer, **settings)

    monkeypatch.setattr(renderer_module, "compose_layer_svgs", composed_context)
    with SectionRenderer(FakeBoard(), module) as renderer:
        front = renderer.svg(section())
        renderer.svg(replace(section(), section_id="other-section"))
        back = renderer.svg(section("B.Cu"))
        assert renderer.svg(section()) == front
        assert renderer.svg(section("B.Cu")) == back
        assert 'data-object="layer-0"' in front
        assert 'data-object="layer-2"' not in front
        assert 'data-object="layer-2"' in back
        assert 'data-object="layer-0"' not in back
    assert compositions == ["F.Cu", "B.Cu"]
    assert sorted(controller.layer for controller in controllers) == [0, 2, 37, 39, 44]
    assert len(detached_render_board) == 1


def test_renderer_uses_detached_outline_for_all_captures(
    tmp_path: Path, detached_render_board: list[tuple[Any, Any]]
) -> None:
    """Editing live outline geometry cannot change a previously bound capture."""

    class OutlineBoard(FakeBoard):
        """Return the actual mutable outline state, including from copied boards."""

        width = 240_000_000

        def GetBoardEdgesBoundingBox(self) -> Any:
            """Expose a complete native-style outline rectangle."""
            return SimpleNamespace(
                GetX=lambda: 0,
                GetY=lambda: 0,
                GetWidth=lambda: self.width,
                GetHeight=lambda: 120_000_000,
            )

    module, board = pcbnew(), OutlineBoard()
    fake_plotter(module, tmp_path)
    with SectionRenderer(board, module) as renderer:
        first = renderer.svg(section())
        board.width *= 10
        assert renderer.svg(section()) == first
        assert renderer.svg(section("B.Cu")) == first.replace('"F.Cu"', '"B.Cu"')
    assert len(detached_render_board) == 1
    assert detached_render_board[0][1].width == 240_000_000


@pytest.mark.parametrize("change_during_plot", [False, True])
def test_renderer_rejects_changed_appearance_before_publishing_native_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change_during_plot: bool,
) -> None:
    """A lazy physical plot must agree with the caller's checked appearance."""
    module = pcbnew()
    fake_plotter(module, tmp_path)
    expected = renderer_module.ThemeContext("selected", "#001023")
    current = [expected]
    monkeypatch.setattr(
        renderer_module, "read_theme_context", lambda module: current[0]
    )
    original_plot = module.PLOT_CONTROLLER.PlotLayer

    def plot(controller: Any) -> bool:
        """Model a persisted appearance change during native plotting."""
        result = original_plot(controller)
        if change_during_plot:
            current[0] = replace(expected, palette_digest="changed")
        return result

    module.PLOT_CONTROLLER.PlotLayer = plot
    if not change_during_plot:
        current[0] = replace(expected, palette_digest="changed")
    with SectionRenderer(FakeBoard(), module, appearance=expected) as renderer:
        with pytest.raises(RenderError, match="appearance.*changed"):
            renderer.context_svg("F.Cu")
        assert "F.Cu" not in renderer._layer_svg


def test_renderer_never_caches_unfinished_copper_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A composition failure may be retried without treating raw copper as context."""
    module = pcbnew()
    module.Edge_Cuts = 44
    controllers = fake_plotter(module, tmp_path)
    compose = renderer_module.compose_layer_svgs
    attempts = []

    def composed_context(
        sources: dict[str, str], active_layer: str, **settings: Any
    ) -> str:
        """Fail only the first complete composition attempt."""
        attempts.append(active_layer)
        if len(attempts) == 1:
            raise RenderError("Incomplete context")
        return compose(sources, active_layer, **settings)

    monkeypatch.setattr(renderer_module, "compose_layer_svgs", composed_context)
    with SectionRenderer(FakeBoard(), module) as renderer:
        with pytest.raises(RenderError, match="Incomplete context"):
            renderer.context_svg("F.Cu")
        assert "F.Cu" not in renderer._layer_svg
        assert "Edge.Cuts" in renderer._layer_svg
        complete = renderer.context_svg("F.Cu")
        assert renderer.context_svg("F.Cu") == complete
    assert attempts == ["F.Cu", "F.Cu"]
    assert [controller.layer for controller in controllers].count(44) == 1


def test_renderer_checks_closed_and_native_failure_before_cached_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed cached context cannot bypass native lifecycle guards."""
    module = pcbnew()
    fake_plotter(module, tmp_path)
    renderer = SectionRenderer(FakeBoard(), module)
    renderer.context_svg("F.Cu")
    monkeypatch.setattr(renderer_module, "_FATAL_PLOT_STATE", (object(), object()))
    with pytest.raises(RenderError, match="Restart KiCad"):
        renderer.context_svg("F.Cu")
    monkeypatch.setattr(renderer_module, "_FATAL_PLOT_STATE", None)
    renderer.close()
    with pytest.raises(RenderError, match="closed"):
        renderer.context_svg("F.Cu")


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


def test_renderer_boosts_native_copper_and_preserves_rasterizable_reference_value_text(
    tmp_path: Path,
) -> None:
    """Render enabled labels as strokes while boosting represented native copper."""
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
    assert "#d87171" in captures[0]
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
            f'<path data-object="copper-{layer}" d="M 10000,4000 L 11000,4000" fill="none" stroke="{color}" stroke-width="20"/>'
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
                assert (
                    objects[f"copper-{layer}"][2].get("stroke")
                    == {
                        "#c83434": "#d87171",
                        "#346ac8": "#7197d8",
                    }[color]
                )
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


def test_cache_free_context_record_skips_python_character_scanning() -> None:
    """Ordinary native context still canonicalizes without a Python walk per byte."""

    class UnscannedRecord(str):
        """Make unnecessary native-record character traversal observable."""

        def __getitem__(self, position: Any) -> str:
            raise AssertionError(
                "A cache-free record was scanned character by character"
            )

    quoted = json.dumps('Review "A" on (F.Cu)')
    record = UnscannedRecord(
        f"\n (gr_text\t{quoted}\n (at 1.0 -2.0)\t(effects (font (size 1 1))))\n"
    )
    assert _without_fill_cache(record) == (
        f"(gr_text {quoted} (at 1.0 -2.0) (effects (font (size 1 1))))"
    )


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ("\n\t ", ""),
        (' (gr_text "Layer µ"\t(at 1 2))\n', '(gr_text "Layer µ" (at 1 2))'),
        ("\n(zone\t(fill no) (thermal_gap 0.3))", "(zone (fill no) (thermal_gap 0.3))"),
        (
            "(filled_polygon_extra\t(pts (xy 1 2)))",
            "(filled_polygon_extra (pts (xy 1 2)))",
        ),
        ("(fill yesterday) (fill yes_extra)", "(fill yesterday) (fill yes_extra)"),
        (
            ' (gr_text "escaped \\"quote\\" and \\\\ slash" (at 1 2)) ',
            '(gr_text "escaped \\"quote\\" and \\\\ slash" (at 1 2))',
        ),
        (
            '(gr_text "literal (filled_polygon) (fill yes) (pcbplotparams)"\n(at 1 2))',
            '(gr_text "literal (filled_polygon) (fill yes) (pcbplotparams)" (at 1 2))',
        ),
        (
            '(zone\n (filled_polygon (layer "F.Cu") (pts (xy 1 2)))\n(fill_segments (pts (xy 3 4))) (fill yes (thermal_gap 0.3))\t(polygon (pts (xy 5 6))))',
            "(zone (fill (thermal_gap 0.3)) (polygon (pts (xy 5 6))))",
        ),
        ('(pcbplotparams (outputdirectory "a \\"(filled_polygon)\\""))', ""),
        (' (gr_text "label" (at 1 2)', '(gr_text "label" (at 1 2)'),
        (' (gr_text "unterminated', "(gr_text unterminated"),
    ],
)
def test_context_normalization_preserves_exact_existing_token_semantics(
    record: str, expected: str
) -> None:
    """Cached forms and ordinary records retain the same quoted-token contract."""
    assert _without_fill_cache(record) == expected


@pytest.mark.parametrize(
    "record",
    [
        '(gr_text "literal (filled_polygon)" (at 1 2))',
        '(gr_text "literal (fill yes)" (at 1 2))',
        '(gr_text "escaped \\"(fill yes)\\" and (pcbplotparams)" (at 1 2))',
    ],
)
def test_quoted_cache_lookalikes_keep_the_quote_aware_path(record: str) -> None:
    """Potential cache tokens anywhere must keep conservative quote-aware filtering."""
    positions = []

    class ObservedRecord(str):
        """Observe the deliberate slow path for possible quoted cache syntax."""

        def __getitem__(self, position: Any) -> str:
            positions.append(position)
            return super().__getitem__(position)

    assert _without_fill_cache(ObservedRecord(record)) == record
    assert positions


@pytest.mark.parametrize(
    "record",
    [
        "(zone (filled_polygon (pts (xy 1 2))",
        "(zone (fill_segments (pts (xy 1 2))",
        '(setup (pcbplotparams (outputdirectory "quoted path")',
    ],
)
def test_incomplete_cache_records_still_fail_closed(record: str) -> None:
    """An unterminated generated-cache expression cannot become approval evidence."""
    with pytest.raises(BoardSnapshotError, match="incomplete zone record"):
        _without_fill_cache(record)
