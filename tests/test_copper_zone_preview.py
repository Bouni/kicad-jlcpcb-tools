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
