"""Locate empty-zone boundaries in native plot coordinates without live edits."""

from collections.abc import Iterator
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import re
import struct
from types import ModuleType, SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from .native_kicad_support import (
    native_bindings,  # noqa: F401 -- pytest discovers this imported fixture.
    requires_native_plotting,
)
from .wx_harness import load_siblings

NM = 1_000_000
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="100mm" '
    'viewBox="0 0 20000 10000"><path d="M -1000,-500 L 1000,-500" '
    'fill="none" stroke="#c83434" stroke-width="20"/></svg>'
)


@pytest.fixture
def preview() -> Iterator[ModuleType]:
    """Import real preview and immutable records without plugin startup."""
    with load_siblings(
        "copper_zone_preview_tests", ("copper_zone_preview", "copper_zones"), {}
    ) as modules:
        yield modules["copper_zone_preview"]


def region(preview: ModuleType, *, layer: int = 0, error: str = "") -> Any:
    """Use a concave outer boundary, a hole, and a separate island."""
    records = __import__(preview.__package__ + ".copper_zones", fromlist=["*"])
    return records.EmptyCopperZone(
        "zone-id",
        "VIN",
        layer,
        "Custom copper",
        (
            (
                (-10 * NM, -5 * NM),
                (10 * NM, -5 * NM),
                (10 * NM, 5 * NM),
                (0, 5 * NM),
                (0, 0),
                (-10 * NM, 0),
            ),
            ((-8 * NM, -4 * NM), (-6 * NM, -4 * NM), (-6 * NM, -2 * NM)),
            ((20 * NM, 5 * NM), (25 * NM, 5 * NM), (25 * NM, 10 * NM)),
        ),
        error,
    )


def paths(root: ET.Element) -> list[ET.Element]:
    """Read exact diagnostic boundaries independently of the native copper."""
    return [node for node in root.iter() if node.get("data-copper-zone") == "outline"]


def parse(source: str) -> ET.Element:
    """Read only generated output and constant local SVG fixtures."""
    return ET.fromstring(source)  # noqa: S314 -- trusted test-generated SVG.


def xy(path: ET.Element) -> list[tuple[float, float]]:
    """Decode the intentionally simple output path for registration assertions."""
    values = [
        float(value) for value in re.findall(r"[-+]?\d+(?:\.\d+)?", path.get("d", ""))
    ]
    return list(zip(values[::2], values[1::2]))


def test_exact_contours_and_native_plot_share_board_registration(
    preview: ModuleType,
) -> None:
    """Negative origins, holes, and disjoint shapes retain actual boundaries."""
    selected = region(preview)
    root = parse(preview.zone_preview_svg(SVG, selected))
    highlights = paths(root)
    assert len(highlights) == 3
    assert all(path.get("fill") == "none" for path in highlights)
    assert all(path.get("d", "").endswith("Z") for path in highlights)
    assert [len(xy(path)) for path in highlights] == [6, 3, 3]
    context = next(
        node for node in root if node.get("data-copper-zone-view") == "board"
    )
    tx, ty, scale = map(
        float, re.findall(r"[-+]?\d+(?:\.\d+)?", context.get("transform", ""))
    )
    # Native SVG units are 100/mm; this native path starts at the outer corner.
    assert xy(highlights[0])[0] == pytest.approx(
        (tx - 1000 * scale, ty - 500 * scale), abs=0.002
    )
    assert all(
        2 <= x <= 998 and 2 <= y <= 598 for path in highlights for x, y in xy(path)
    )


def test_overview_keeps_board_and_outside_zone_while_zoom_focuses_zone(
    preview: ModuleType,
) -> None:
    """An off-board empty pour remains visible with the PCB as context."""
    selected = region(preview)
    bounds = (0, 0, 100 * NM, 60 * NM)
    overview = parse(preview.zone_preview_svg(SVG, selected, board_bounds=bounds))
    zoom = parse(
        preview.zone_preview_svg(SVG, selected, board_bounds=bounds, zoom=True)
    )
    assert (
        xy(paths(zoom)[0])[1][0] - xy(paths(zoom)[0])[0][0]
        > xy(paths(overview)[0])[1][0] - xy(paths(overview)[0])[0][0]
    )
    context = next(
        node for node in overview if node.get("data-copper-zone-view") == "board"
    )
    tx, ty, scale = map(
        float, re.findall(r"[-+]?\d+(?:\.\d+)?", context.get("transform", ""))
    )
    assert 0 <= tx <= 1000 and 0 <= ty <= 600
    assert tx + 10000 * scale < 1000 and ty + 6000 * scale < 600


def test_geometry_failure_is_explicit(preview: ModuleType) -> None:
    """A failed outline must not produce a plausible location image."""
    with pytest.raises(preview.RenderError, match="unavailable"):
        preview.zone_preview_svg(SVG, region(preview, error="outline unavailable"))


@pytest.mark.parametrize("zoom,expected_mm", [(False, 20.0), (True, 1.0)])
def test_small_teardrop_has_physical_scale_in_overview_and_zoom(
    preview: ModuleType, zoom: bool, expected_mm: float
) -> None:
    """A large onscreen teardrop must still convey its submillimetre size."""
    records = __import__(preview.__package__ + ".copper_zones", fromlist=["*"])
    selected = records.EmptyCopperZone(
        "small-teardrop",
        "VEXT",
        0,
        "F.Cu",
        (
            (
                (12 * NM, 8 * NM),
                (12 * NM + 995_000, 8 * NM),
                (12 * NM, 8 * NM + 300_000),
            ),
        ),
    )
    root = parse(
        preview.zone_preview_svg(
            SVG, selected, board_bounds=(0, 0, 100 * NM, 60 * NM), zoom=zoom
        )
    )
    scale = next(
        (node for node in root.iter() if node.get("data-copper-zone-scale") == "bar"),
        None,
    )
    assert scale is not None, "The preview needs a physical scale beside its outline."
    label = next(
        node for node in root.iter() if node.get("data-copper-zone-scale") == "label"
    )
    assert label.get("aria-label") == f"{expected_mm:g} mm"
    assert label.findall("{http://www.w3.org/2000/svg}path")
    assert not root.findall(".//{http://www.w3.org/2000/svg}text")
    vertices = xy(paths(root)[0])
    pixels_per_mm = (vertices[1][0] - vertices[0][0]) / 0.995
    assert (
        float(scale.get("x2")) - float(scale.get("x1"))
    ) / expected_mm == pytest.approx(pixels_per_mm, abs=0.002)
    assert max(y for x, y in vertices) < float(scale.get("y1")) - 8


@pytest.mark.parametrize(
    "maximum,expected",
    [
        (0.49, 0.2),
        (0.5, 0.5),
        (0.999, 0.5),
        (1.0, 1.0),
        (1.99, 1.0),
        (2.0, 2.0),
        (4.99, 2.0),
        (5.0, 5.0),
        (19.0, 10.0),
        (20.0, 20.0),
        (300.0, 200.0),
    ],
)
def test_physical_scale_uses_a_readable_length_that_fits(
    preview: ModuleType, maximum: float, expected: float
) -> None:
    """Choose familiar lengths across decade boundaries without exceeding space."""
    assert preview._scale_length(maximum) == pytest.approx(expected)


def test_boundary_preserves_a_real_narrow_protrusion(preview: ModuleType) -> None:
    """A spur must remain visible rather than being replaced by its bounding box."""
    records = __import__(preview.__package__ + ".copper_zones", fromlist=["*"])
    contour = (
        (3 * NM, 2 * NM),
        (4 * NM, 2 * NM),
        (4 * NM, 2 * NM + 20_000),
        (16 * NM, 2 * NM + 20_000),
        (16 * NM, 2 * NM + 40_000),
        (4 * NM, 2 * NM + 40_000),
        (4 * NM, 3 * NM),
        (3 * NM, 3 * NM),
    )
    selected = records.EmptyCopperZone("spur", "VEXT", 0, "F.Cu", (contour,))
    root = parse(preview.zone_preview_svg(SVG, selected, zoom=True))
    assert len(paths(root)) == 1
    plotted = xy(paths(root)[0])
    assert len(plotted) == len(contour)
    # Compare the long spur and body width, independently of viewport framing.
    assert (plotted[3][0] - plotted[2][0]) / (
        plotted[1][0] - plotted[0][0]
    ) == pytest.approx(12, abs=0.002)


@pytest.mark.parametrize("layer,canonical", [(0, "F.Cu"), (4, "In1.Cu"), (2, "B.Cu")])
def test_renderer_uses_numeric_layer_and_caches_native_context(
    preview: ModuleType, monkeypatch: pytest.MonkeyPatch, layer: int, canonical: str
) -> None:
    """Renamed labels cannot choose the wrong copper plane; copies stay scoped."""
    render = __import__(preview.__package__ + ".impedance.render", fromlist=["*"])
    board = SimpleNamespace(
        GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: (0, 4, 2)),
        ComputeBoundingBox=lambda: SimpleNamespace(
            GetX=lambda: 0,
            GetY=lambda: 0,
            GetWidth=lambda: 100 * NM,
            GetHeight=lambda: 60 * NM,
        ),
    )
    api = SimpleNamespace(F_Cu=0, In1_Cu=4, B_Cu=2)
    plotted, copies, closed, images, temporaries = [], [], [], [], []

    def copy(source: Any, module: Any, *, allow_unfilled_zones: bool = False) -> Any:
        assert allow_unfilled_zones
        detached = deepcopy(source)
        copies.append(detached)
        return SimpleNamespace(board=detached, close=lambda: closed.append(True))

    def plot(self: Any, name: str) -> str:
        assert self._board_for_plot() is not board
        plotted.append(name)
        return SVG

    def rasterize(source: Path, output: Path, width: int, height: int) -> None:
        images.append(parse(source.read_text(encoding="utf-8")))
        temporaries.append(source.parent)
        output.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + struct.pack(">II", width, height)
            + bytes(9)
        )

    monkeypatch.setattr(render, "copy_for_render", copy)
    monkeypatch.setattr(render.SectionRenderer, "_plot_layer", plot)
    monkeypatch.setattr(
        render,
        "read_theme_context",
        lambda module: render.ThemeContext("default", "#10151d"),
    )
    original = deepcopy(board.__dict__)
    with preview.ZonePreviewRenderer(board, api, rasterizer=rasterize) as renderer:
        first = renderer.render(region(preview, layer=layer))
        second = renderer.render(region(preview, layer=layer), zoom=True)
    renderer.close()
    assert (first.width, first.height) == (1000, 600)
    assert (second.width, second.height) == (1000, 600)
    assert plotted == [canonical]
    assert len(copies) == 1 and closed == [True]
    assert not any(path.exists() for path in temporaries)
    assert board.__dict__ == original
    assert len(paths(images[0])) == 3
    # No Edge.Cuts: use real PCB item extents instead of the plot's paper page.
    assert (
        xy(paths(images[1])[0])[1][0] - xy(paths(images[1])[0])[0][0]
        > xy(paths(images[0])[0])[1][0] - xy(paths(images[0])[0])[0][0]
    )


@pytest.mark.native_kicad
@requires_native_plotting
def test_native_unfilled_zone_renders_without_refill_or_live_board_changes(
    preview: ModuleType,
    native_bindings: SimpleNamespace,  # noqa: F811 -- imported pytest fixture.
    tmp_path: Path,
) -> None:
    """Check supported native plots or explicit fallback without changing fills."""
    pcbnew, wx = native_bindings.pcbnew, native_bindings.wx
    supports_named_nets = int(re.match(r"\d+", pcbnew.Version()).group()) >= 10
    board = pcbnew.BOARD()
    board.SetFileName(str(tmp_path / "empty-zone.kicad_pcb"))
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECT)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(50 * NM, 30 * NM))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(100_000)
    board.Add(edge)
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.F_Cu)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((NM, NM), (10 * NM, NM), (10 * NM, 10 * NM), (NM, 10 * NM)):
        outline.Append(x, y)
    board.Add(zone)
    records = __import__(preview.__package__ + ".copper_zones", fromlist=["*"])
    (selected,) = records.collect_empty_copper_zones(board, pcbnew.IsCopperLayer)
    before = (
        zone.IsFilled(),
        zone.NeedRefill(),
        zone.GetFilledPolysList(pcbnew.F_Cu).Area(),
        selected.contours,
        zone.m_Uuid.AsString(),
        len(tuple(board.GetDrawings())),
    )
    assert not before[0]
    with preview.ZonePreviewRenderer(board, pcbnew) as renderer:
        if supports_named_nets:
            capture = renderer.render(selected)
            zoom = renderer.render(selected, zoom=True)
            assert (capture.width, capture.height) == (1000, 600)
            assert capture.sha256 != zoom.sha256
            assert wx.Image(BytesIO(capture.data), wx.BITMAP_TYPE_PNG).IsOk()
        else:
            # Missing snapshot APIs can fail before legacy net validation.
            # Every unsupported runtime must explain its textual fallback.
            with pytest.raises(preview.RenderError, match=r"\S"):
                renderer.render(selected)
    after_region = records.collect_empty_copper_zones(board, pcbnew.IsCopperLayer)[0]
    assert before == (
        zone.IsFilled(),
        zone.NeedRefill(),
        zone.GetFilledPolysList(pcbnew.F_Cu).Area(),
        after_region.contours,
        zone.m_Uuid.AsString(),
        len(tuple(board.GetDrawings())),
    )


def _pixel_neighborhood(image: Any, x: float, y: float) -> list[tuple[int, int, int]]:
    """Read a small tolerance for native antialiasing around known coordinates."""
    return [
        (image.GetRed(px, py), image.GetGreen(px, py), image.GetBlue(px, py))
        for px in range(round(x) - 2, round(x) + 3)
        for py in range(round(y) - 2, round(y) + 3)
        if 0 <= px < image.GetWidth() and 0 <= py < image.GetHeight()
    ]


@pytest.mark.native_kicad
@pytest.mark.kicad10_impedance
@requires_native_plotting
@pytest.mark.parametrize("layer_name", ["F_Cu", "B_Cu", "In1_Cu"])
@pytest.mark.parametrize("aux_origin", [(0, 0), (17 * NM, -9 * NM)])
def test_native_asymmetric_copper_and_teardrop_align_in_pixels(
    preview: ModuleType,
    native_bindings: SimpleNamespace,  # noqa: F811 -- imported pytest fixture.
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer_name: str,
    aux_origin: tuple[int, int],
) -> None:
    """Exercise real plotting/rasterization, including back layers and aux origins."""
    pcbnew, wx = native_bindings.pcbnew, native_bindings.wx
    if int(re.match(r"\d+", pcbnew.Version()).group()) < 10:
        pytest.skip("Native image registration requires supported KiCad 10 snapshots")
    render = __import__(preview.__package__ + ".impedance.render", fromlist=["*"])
    monkeypatch.setattr(
        render,
        "read_theme_context",
        lambda module: render.ThemeContext("_builtin_default", "#001023"),
    )
    board = pcbnew.BOARD()
    board.SetFileName(str(tmp_path / "asymmetric-zone.kicad_pcb"))
    board.SetCopperLayerCount(4)
    board.GetDesignSettings().SetAuxOrigin(pcbnew.VECTOR2I(*aux_origin))
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECT)
    edge.SetStart(pcbnew.VECTOR2I(-20 * NM, -10 * NM))
    edge.SetEnd(pcbnew.VECTOR2I(40 * NM, 30 * NM))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(0)
    board.Add(edge)
    layer = getattr(pcbnew, layer_name)
    # The small local marker is in both views. Two distant asymmetric markers
    # independently challenge sign, mirror, scale, and paper-origin mistakes.
    for start, end in (
        ((11_500_000, 6_800_000), (12_200_000, 6_800_000)),
        ((-12 * NM, -4 * NM), (-9 * NM, -4 * NM)),
        ((30 * NM, 20 * NM), (30 * NM, 24 * NM)),
    ):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(*start))
        track.SetEnd(pcbnew.VECTOR2I(*end))
        track.SetLayer(layer)
        track.SetWidth(150_000)
        board.Add(track)
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer)
    outline = zone.Outline()
    outline.NewOutline()
    for point in ((10 * NM, 6 * NM), (10_995_000, 6 * NM), (10 * NM, 6_300_000)):
        outline.Append(*point)
    board.Add(zone)
    records = __import__(preview.__package__ + ".copper_zones", fromlist=["*"])
    (selected,) = records.collect_empty_copper_zones(board, pcbnew.IsCopperLayer)
    before = (zone.IsFilled(), zone.NeedRefill())

    with preview.ZonePreviewRenderer(board, pcbnew) as renderer:
        for zoom in (False, True):
            capture = renderer.render(selected, zoom=zoom)
            image = wx.Image(BytesIO(capture.data), wx.BITMAP_TYPE_PNG)
            assert image.IsOk()
            # Derive expected image coordinates from this fixture's physical
            # dimensions, without reading the renderer's output transform.
            # The image reserves its bottom 48 px for the scale.
            pixels_per_mm = 552 / (4.3 if zoom else 46)
            left = (10.4975 if zoom else 10) - 500 / pixels_per_mm
            top = 4 if zoom else -13

            def sample(
                x_mm: float,
                y_mm: float,
                *,
                image: Any = image,
                left: float = left,
                top: float = top,
                pixels_per_mm: float = pixels_per_mm,
            ) -> list[tuple[int, int, int]]:
                """Bind this completed capture's expected board-to-pixel mapping."""
                return _pixel_neighborhood(
                    image, (x_mm - left) * pixels_per_mm, (y_mm - top) * pixels_per_mm
                )

            def is_yellow(rgb: tuple[int, int, int]) -> bool:
                return rgb[0] > 220 and rgb[1] > 220 and rgb[2] < 80

            assert any(is_yellow(rgb) for rgb in sample(10, 6))
            assert any(is_yellow(rgb) for rgb in sample(10.995, 6))
            markers = [(11.85, 6.8)] if zoom else [(11.85, 6.8), (-10.5, -4), (30, 22)]
            for marker in markers:
                assert any(
                    max(
                        abs(channel - background)
                        for channel, background in zip(rgb, (0, 16, 35))
                    )
                    > 40
                    and not is_yellow(rgb)
                    for rgb in sample(*marker)
                ), f"Native copper marker is misregistered on {layer_name}: {marker}"
            # There is no track at the horizontally reflected local location.
            assert all(
                max(
                    abs(channel - background)
                    for channel, background in zip(rgb, (0, 16, 35))
                )
                < 10
                for rgb in sample(9.145, 6.8)
            )
            # Confirm the label paths survive NanoSVG, separately from its bar.
            assert any(
                all(channel > 220 for channel in rgb)
                for x in range(16, 70)
                for rgb in _pixel_neighborhood(image, x, 580)
            )
    assert not before[0]
    assert before == (zone.IsFilled(), zone.NeedRefill())
    after_origin = board.GetDesignSettings().GetAuxOrigin()
    assert (after_origin.x, after_origin.y) == aux_origin
