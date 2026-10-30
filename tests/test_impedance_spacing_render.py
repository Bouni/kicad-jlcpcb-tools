"""Rasterize distinct diagnostic close-ups from native vector board context."""

from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from xml.etree import ElementTree as ET

import pytest

from impedance import render as renderer_module
from impedance.matching import CAPTURE_POLICY_REVISION, analyze
from impedance.render import RenderError, SectionRenderer
from impedance.spacing_review import spacing_review_entries

from .test_impedance_spacing_checks import _parallel
from .test_impedance_spacing_review_entries import _config


def _renderer(monkeypatch: pytest.MonkeyPatch) -> tuple[SectionRenderer, list[str]]:
    """Give the real annotation code immutable native SVG and no raster cache."""
    renderer = object.__new__(SectionRenderer)
    source = (
        '<svg width="100mm" height="100mm" viewBox="0 0 10000 10000">'
        '<path d="M 0,0 L 1000,0" stroke="#c83434" '
        'stroke-width="15" fill="none"/>'
        '<path d="M 0,40 L 1000,40" stroke="#c83434" '
        'stroke-width="15" fill="none"/></svg>'
    )
    calls: list[str] = []

    def context(layer: str) -> str:
        """Record actual native-context access for every requested image."""
        calls.append(layer)
        return source

    monkeypatch.setattr(renderer, "context_svg", context)
    monkeypatch.setattr(
        renderer,
        "_theme_context",
        lambda: type("Theme", (), {"background": "#10151d"})(),
    )
    return renderer, calls


def test_spacing_svg_keeps_native_geometry_with_external_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native copper stays in physical panels; measurements live outside them."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, calls = _renderer(monkeypatch)
    result = renderer.spacing_svg(entry)
    root = ET.fromstring(result)  # noqa: S314 -- Locally generated diagnostic SVG.
    assert root.get("width") == "1600"
    assert root.get("height") == "1000"
    assert calls == ["F.Cu"]
    assert "data-impedance-spacing-view" in result
    assert "data-impedance-highlight" not in result
    assert "<text" in result
    assert "<image" not in result
    assert "M 0,0 L 1000,0" in result
    assert "M 0,40 L 1000,40" in result
    assert CAPTURE_POLICY_REVISION == 8


def test_spacing_entries_share_validated_native_scene_and_dimmed_paint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unchanged layer needs one validation and dim pass across all panels."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    parsed: list[str] = []
    dimmed: list[str] = []
    parse, emphasize = (
        renderer_module._native_svg,
        renderer_module._emphasize_native_traces,
    )

    def counted_parse(source: str) -> tuple[ET.Element, float, float, float]:
        """Count real validation while retaining all of its rejection rules."""
        parsed.append(source)
        return parse(source)

    def counted_emphasis(*args: object, **kwargs: object) -> None:
        """Count actual inherited-paint traversal, rather than a raster stub."""
        dimmed.append("dim")
        emphasize(*args, **kwargs)

    monkeypatch.setattr(renderer_module, "_native_svg", counted_parse)
    monkeypatch.setattr(renderer_module, "_emphasize_native_traces", counted_emphasis)
    primary = renderer.spacing_svg(entry)
    original = renderer.spacing_original_svg(entry)
    # Different concerns on the same layer keep their own annotations and frame.
    other = replace(entry, issue_id="other-concern")
    assert "other-concern" in renderer.spacing_svg(other)
    assert renderer.spacing_svg(entry) == primary
    assert renderer.spacing_original_svg(entry) == original
    assert len(parsed) == 1
    assert dimmed == ["dim"]
    assert "#c83434" in original
    assert "#c83434" not in primary


def test_spacing_rasterizer_skips_geometry_proven_outside_native_panel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A huge native detail zoom must not rasterize unrelated full-board strokes."""
    from impedance.spacing_review import SPACING_RENDER_POLICY_REVISION

    source = tmp_path / "spacing.svg"
    source.write_text(
        '<svg width="1600" height="1000" '
        f'data-spacing-composition="{SPACING_RENDER_POLICY_REVISION}">'
        '<svg x="16" y="70" width="1120" height="600" '
        'data-spacing-raster-panel="detail">'
        '<g data-impedance-spacing-view="detail" '
        'transform="translate(-100.000000,-100.000000) scale(2.000000000)">'
        '<path data-object="visible" d="M 80,80 100,100" stroke="red"/>'
        '<path data-object="outside" d="M 10000,10000 10010,10010" stroke="red"/>'
        '<g transform="rotate(45)"><path data-object="unsupported" '
        'd="M 10000,10000 10010,10010" stroke="red"/></g>'
        "</g></svg></svg>",
        encoding="utf-8",
    )
    parsed: list[bytes] = []

    class PanelReached(Exception):
        """Stop at the real rasterizer's native vector submission boundary."""

    def create(content: bytes) -> SimpleNamespace:
        """Inspect geometry actually submitted instead of a helper's own return."""
        parsed.append(content)
        if len(parsed) == 2:
            raise PanelReached
        return SimpleNamespace(ConvertToBitmap=lambda **kwargs: object())

    wx = SimpleNamespace(
        MemoryDC=lambda bitmap: SimpleNamespace(SelectObject=lambda bitmap: None),
        NullBitmap=object(),
    )
    modules = {
        "wx": wx,
        "wx.svg": SimpleNamespace(SVGimage=SimpleNamespace(CreateFromBytes=create)),
    }
    monkeypatch.setattr(renderer_module.importlib, "import_module", modules.__getitem__)
    with pytest.raises(PanelReached):
        renderer_module._wx_rasterize(source, tmp_path / "unused.png", 1600, 1000)
    panel = ET.fromstring(parsed[1])  # noqa: S314 -- Locally generated probe.
    identifiers = {
        node.get("data-object") for node in panel.iter() if node.get("data-object")
    }
    assert identifiers == {"visible", "unsupported"}


def test_repeated_spacing_rasterization_prepares_native_geometry_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changing the viewport cannot trigger another full layer geometry scan."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    renderer._rasterizer = renderer_module._wx_rasterize
    renderer._use_native_panels = True
    calls: list[str] = []
    bounds = renderer_module._native_shape_bounds

    def counted(node: ET.Element) -> object:
        """Observe the actual conservative geometry parser."""
        calls.append(node.tag)
        return bounds(node)

    class PanelReached(Exception):
        """Stop before invoking native graphics in an ordinary host test."""

    submissions: list[bytes] = []
    scanned: list[int] = []
    pattern = renderer_module._RASTER_PANEL

    class CountedPattern:
        """Observe expensive scans of a completed full-layer SVG document."""

        def findall(self, content: str) -> list[str]:
            """Retain real matching while accounting for full input traversal."""
            scanned.append(len(content))
            return pattern.findall(content)

        def sub(self, replacement: object, content: str) -> str:
            """Preserve real panel replacement for the pre-optimization path."""
            return pattern.sub(replacement, content)

    def create(content: bytes) -> SimpleNamespace:
        """Allow the summary bitmap, then stop at each detail submission."""
        submissions.append(content)
        if len(submissions) % 2 == 0:
            raise PanelReached
        return SimpleNamespace(ConvertToBitmap=lambda **kwargs: object())

    wx = SimpleNamespace(
        MemoryDC=lambda bitmap: SimpleNamespace(SelectObject=lambda bitmap: None),
        NullBitmap=object(),
    )
    modules = {
        "wx": wx,
        "wx.svg": SimpleNamespace(SVGimage=SimpleNamespace(CreateFromBytes=create)),
    }
    monkeypatch.setattr(renderer_module.importlib, "import_module", modules.__getitem__)
    monkeypatch.setattr(renderer_module, "_native_shape_bounds", counted)
    monkeypatch.setattr(renderer_module, "_RASTER_PANEL", CountedPattern())
    for index in range(2):
        with pytest.raises(PanelReached):
            renderer.render_spacing(entry, tmp_path / f"capture-{index}.png")
    assert calls == ["path", "path"]
    assert scanned == []


def test_native_scene_preprocessing_cache_evicts_by_layer_and_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure scene reuse remains bounded and an eviction regenerates equal output."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    monkeypatch.setattr(renderer_module, "_NATIVE_SCENE_LIMIT", 2)
    expected = renderer.spacing_svg(entry)
    renderer.spacing_svg(replace(entry, layer="B.Cu"))
    renderer.spacing_svg(entry)
    renderer.spacing_svg(replace(entry, layer="In1.Cu"))
    assert tuple(renderer._native_scenes) == ("F.Cu", "In1.Cu")
    renderer.spacing_svg(replace(entry, layer="B.Cu"))
    assert tuple(renderer._native_scenes) == ("In1.Cu", "B.Cu")
    assert renderer.spacing_svg(entry) == expected
    renderer._native_scenes.clear()
    monkeypatch.setattr(renderer_module, "_NATIVE_SCENE_BYTES", 1)
    assert renderer.spacing_svg(entry) == expected
    assert renderer.spacing_svg(entry) == expected
    assert renderer._native_scenes == {}


@pytest.mark.parametrize("prepared", (False, True))
@pytest.mark.parametrize(
    "geometry",
    (
        '<path id="shape" d="M 500,50 L 520,50" stroke="red"/>',
        '<rect id="shape" x="500" y="40" width="20" height="20" fill="red"/>',
        '<circle id="shape" cx="500" cy="50" r="10" fill="red"/>',
        '<g id="shape"><path d="M 500,50 L 520,50" stroke="red"/></g>',
    ),
)
def test_pruning_preserves_referenced_native_elements_and_complete_subtrees(
    geometry: str, prepared: bool
) -> None:
    """An off-canvas ID can supply geometry to a retained transformed reference."""
    panel = ET.fromstring(  # noqa: S314 -- Literal SVG reference fixtures.
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
        '<g data-impedance-spacing-view="detail" '
        'transform="translate(0.000000,0.000000) scale(1.000000000)">'
        + geometry
        + '<use xlink:href="#shape" transform="translate(-450,0)"/></g></svg>'
    )
    if prepared:
        group = panel[0]
        fragments = tuple(
            renderer_module._prepare_raster_fragment(node, {}) for node in group
        )
        visible = "".join(
            fragment.visible((0, 0, 100, 100), 2) for fragment in fragments
        )
        panel = ET.fromstring(  # noqa: S314 -- Locally generated fragments.
            '<svg xmlns:xlink="http://www.w3.org/1999/xlink">' + visible + "</svg>"
        )
    else:
        renderer_module._prune_panel_geometry(panel)
    reference = next((node for node in panel.iter() if node.get("id") == "shape"), None)
    assert reference is not None, "Pruning created a dangling local reference"
    assert ET.tostring(reference) == ET.tostring(ET.fromstring(geometry))  # noqa: S314 -- Literal fixture.


@pytest.mark.parametrize("repainted", (False, True))
def test_prepared_group_tails_remain_escaped_xml(repainted: bool) -> None:
    """Unknown non-whitespace text remains valid XML after native preparation."""
    source = ET.fromstring(  # noqa: S314 -- Literal escaped XML fixture.
        '<g><g><path d="M 0,0 L 10,10"/></g>&amp; &lt;unknown&gt;</g>'
    )
    fragment = renderer_module._prepare_raster_fragment(source, {})
    if repainted:
        fragment = renderer_module._repaint_raster_fragment(fragment, source)
    output = ET.fromstring(fragment.visible((-100, -100, 100, 100), 2))  # noqa: S314 -- Locally generated fragment.
    assert output[0].tail == source[0].tail


@pytest.mark.parametrize(
    "content",
    (
        '<path data-object="keep" d="M 103,10 103,80" stroke="red" stroke-width="2"/>',
        '<g stroke="red" stroke-width="2" stroke-miterlimit="10"><path data-object="keep" d="M 115,10 115,80"/></g>',
        '<path data-object="keep" d="M -20,50 120,50" stroke="red"/>',
        '<path data-object="keep" d="M 500,500 C -500,-500 -500,-500 500,500"/>',
        '<path data-object="keep" d="M 500,500 A 500,500 0 1 1 500,500"/>',
        '<g transform="rotate(45)"><circle data-object="keep" cx="500" cy="500" r="5"/></g>',
        '<g filter="url(#shadow)"><circle data-object="keep" cx="500" cy="500" r="5"/></g>',
        '<circle data-object="keep" cx="500" cy="500" r="5" stroke="red" stroke-width="600px"/>',
        '<defs><circle data-object="keep" cx="500" cy="500" r="5"/></defs>',
        '<svg x="-500" y="-500"><circle data-object="keep" cx="500" cy="500" r="5"/></svg>',
        '<path data-object="keep" d="M 500,500 h -500 v -500"/>',
        '<circle data-object="keep" cx="500" cy="500" r="-1"/>',
        '<path data-object="keep" d="L 500,500 600,600"/>',
        '<path data-object="keep" d="Z M 500,500 600,600"/>',
        '<path data-object="keep" d="M 500,500 L Z 600,600"/>',
        '<path data-object="keep" d="M 500,500 Z 600,600"/>',
        '<path data-object="keep" d="M 500,,500 L 600,600"/>',
        '<circle data-object="keep" cx="500" cy="50" r="10" style="transform:translate(-450,0)"/>',
        '<g style="transform:translate(-450,0)"><circle data-object="keep" cx="500" cy="50" r="10"/></g>',
        '<path data-object="keep" d="M 500 50 L 500 M 60 500 L 70 500 L 80 500 L 90"/>',
    ),
)
def test_native_pruning_preserves_boundary_strokes_and_uncertain_geometry(
    content: str,
) -> None:
    """Anything that could paint inside, or lacks a proof of bounds, stays intact."""
    panel = ET.fromstring(  # noqa: S314 -- Literal local geometry fixtures.
        '<svg width="100" height="100"><g data-impedance-spacing-view="detail" '
        'transform="translate(0.000000,0.000000) scale(1.000000000)">'
        + content
        + '<circle data-object="discard" cx="5000" cy="5000" r="5"/></g></svg>'
    )
    renderer_module._prune_panel_geometry(panel)
    identifiers = {
        node.get("data-object") for node in panel.iter() if node.get("data-object")
    }
    assert identifiers == {"keep"}


def test_spacing_scene_cache_revalidates_changed_source_and_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source or appearance mismatch cannot reuse earlier layer paint."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    primary = renderer.spacing_svg(entry)
    original = renderer.spacing_original_svg(entry)
    native = renderer.context_svg(entry.layer)
    monkeypatch.setattr(renderer, "context_svg", lambda layer: "not valid SVG")
    with pytest.raises(RenderError, match="complete SVG"):
        renderer.spacing_svg(entry)
    changed = native.replace("1000,0", "900,0")
    monkeypatch.setattr(renderer, "context_svg", lambda layer: changed)
    assert renderer.spacing_svg(entry) != primary
    assert renderer.spacing_original_svg(entry) != original
    monkeypatch.setattr(renderer, "context_svg", lambda layer: native)
    monkeypatch.setattr(
        renderer, "_theme_context", lambda: SimpleNamespace(background="#FFFFFF")
    )
    assert renderer.spacing_svg(entry) != primary
    assert renderer.spacing_original_svg(entry) != original


def test_overview_marks_the_complete_affected_run_outside_the_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One concern's overview exposes the full run, not just its chosen detail."""
    config, snapshot = _config(), _parallel(250_000, length=80_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    overview = next(
        node for node in root.iter() if node.get("data-spacing-overview-panel")
    )
    spans = [node for node in overview.iter() if node.get("data-spacing-overview-span")]
    assert len(spans) == 2
    assert {
        (node.get("data-board-start-nm"), node.get("data-board-end-nm"))
        for node in spans
    } == {("0,0", "80000000,0"), ("0,400000", "80000000,400000")}
    # This is a visible location highlight: its width remains readable even
    # when the complete route is much longer than the physical detail.
    assert all(float(node.get("stroke-width")) * 0.35 >= 2 for node in spans)
    locator = next(
        node
        for node in overview.iter()
        if node.get("data-spacing-overview-locator") == "outline"
    )
    assert locator.get("stroke") != spans[0].get("stroke")
    marks = [node for node in overview.iter() if node.get("stroke")]
    first_highlight = marks.index(spans[0])
    assert all(node.get("stroke") == "#10151d" for node in marks[:first_highlight])
    assert all(node.get("stroke") != "#10151d" for node in marks[first_highlight:])
    left, top, width, height = entry.composition.overview_viewport_nm
    assert left < 0 and left + width > 80_000_000
    assert top < -75_000 and top + height > 475_000
    assert entry.viewport_nm[2] < 80_000_000


def test_explicit_worst_crosssection_drives_both_detail_tick_and_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A central, smaller gap cannot replace the concern's selected worst gap."""
    from .test_impedance_spacing_checks import _line, _snapshot

    config = _config()
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (1_000_000, 0))),
        _line("n", "N", ((0, 400_000), (1_000_000, 500_000))),
    )
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    representative = max(
        (section for member in entry.members for section in member.cross_sections),
        key=lambda section: math.dist(section.source_point, section.mate_point)
        - (section.source_width_nm + section.mate_width_nm) / 2,
    )
    entry = replace(entry, representative=representative)
    assert entry.composition.measurement == representative
    assert entry.composition.actual_gap_nm == pytest.approx(350_000)
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    ticks = [
        node
        for node in root.iter()
        if node.get("data-impedance-spacing-guide") == "measured-gap"
    ]
    assert len(ticks) == 1
    tick = ticks[0]
    start = tuple(float(value) for value in tick.get("data-board-start-nm").split(","))
    end = tuple(float(value) for value in tick.get("data-board-end-nm").split(","))
    assert math.dist(start, end) == pytest.approx(350_000)
    assert "0.35 mm" in {node.get("data-spacing-label") for node in root.iter()}


def test_selected_worst_crosssection_cannot_silently_disappear_from_detail() -> None:
    """Invalid framing must fail rather than substitute an easier local gap."""
    from impedance.model import ValidationError

    config, snapshot = _config(), _parallel(250_000, length=80_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    assert entry.representative is not None
    left, top, width, height = entry.viewport_nm
    entry = replace(entry, viewport_nm=(left + 100_000_000, top, width, height))
    with pytest.raises(ValidationError, match="selected spacing measurement"):
        _ = entry.composition


def test_diagnostic_rasterizer_receives_fresh_native_closeup_svg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rendering asks the vector rasterizer for full diagnostic resolution."""
    from .test_impedance_report_pipeline import _png

    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, calls = _renderer(monkeypatch)
    rasterized = []

    def rasterize(source: Path, output: Path, width: int, height: int) -> None:
        """Record the actual source and produce an independently sized native PNG."""
        rasterized.append((source.read_text(), width, height))
        output.write_bytes(_png(width, height))

    renderer._rasterizer = rasterize
    destination = tmp_path / "spacing.png"
    assert renderer.render_spacing(entry, destination) == destination
    assert calls == ["F.Cu"]
    assert rasterized[0][1:] == (1600, 1000)
    assert "data-impedance-spacing-view" in rasterized[0][0]
    assert destination.read_bytes() == _png(1600, 1000)


def test_diagnostic_dimensions_cannot_disagree_with_approved_viewport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers cannot silently shrink a capture below its readability contract."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _calls = _renderer(monkeypatch)
    with pytest.raises(RenderError, match="dimensions"):
        renderer.spacing_svg(entry, 800, 420)


def test_spacing_png_source_contains_truthful_target_edges_and_sparse_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 0.15mm target lies 0.15mm beyond each actual facing copper edge."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _calls = _renderer(monkeypatch)
    svg = renderer.spacing_svg(entry)
    root = ET.fromstring(svg)  # noqa: S314 -- Locally generated diagnostic SVG.
    guides = [
        node
        for node in root.iter()
        if node.get("data-impedance-spacing-guide") == "target-edge"
    ]
    ticks = [
        node
        for node in root.iter()
        if node.get("data-impedance-spacing-guide") == "measured-gap"
    ]
    assert len(guides) == 2
    assert 1 <= len(ticks) <= 3
    for guide in guides:
        assert guide.get("stroke-dasharray")
    # Physical metadata is checked independently of the SVG's board-to-pixel transform.
    assert {float(guide.get("data-board-y-nm")) for guide in guides} == {
        175_000,
        225_000,
    }
    assert "<text" in svg
    assert "<image" not in svg
    assert "Dashed" in entry.caption


def test_only_located_native_spans_are_emphasized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short concern on long native tracks must not highlight their full lengths."""
    from dataclasses import replace

    from impedance.spacing_checks import SpacingLocation

    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    member = SpacingLocation(
        "p",
        ("n",),
        (0.2, 0.3),
        ((2_000_000, 0), (3_000_000, 0)),
        ((2_000_000, 400_000), (3_000_000, 400_000)),
        (1_925_000, -75_000, 3_075_000, 475_000),
        (("n", 0.2, 0.3),),
    )
    entry = replace(entry, members=(member,))
    renderer, _calls = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local generated SVG.
    highlights = [
        node for node in root.iter() if node.get("data-impedance-spacing-span")
    ]
    assert len(highlights) == 2
    assert {
        (node.get("data-board-start-nm"), node.get("data-board-end-nm"))
        for node in highlights
    } == {("2000000,0", "3000000,0"), ("2000000,400000", "3000000,400000")}


@pytest.mark.parametrize(
    "gap,target,direction",
    [(200_000, 203_200, "Too close by"), (331_300, 202_300, "Wider by")],
)
@pytest.mark.parametrize("layout", ("standard", "wide"))
def test_combined_spacing_numbers_match_visible_crosssection_and_stay_outside_copper(
    monkeypatch: pytest.MonkeyPatch, gap: int, target: int, direction: str, layout: str
) -> None:
    """Both narrow and wide gaps have readable truthful external measurements."""
    from dataclasses import replace

    config = _config()
    spec = config.specifications[0]
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=target),),
            ),
        ),
    )
    snapshot = _parallel(gap, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    if layout == "wide":
        entry = replace(
            entry,
            composition_layout="wide",
            viewport_nm=(-1_000_000, -500_000, 6_000_000, 6_000_000 * 460 / 1568),
        )
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    composition = entry.composition
    assert composition.actual_gap_nm == pytest.approx(gap)
    assert composition.measurement is not None
    labels = {
        node.get("data-spacing-label")
        for node in root.iter()
        if node.get("data-spacing-label")
    }
    assert {
        "Actual gap",
        "Target gap",
        direction,
        "Affected run",
        "Gap deviation from target",
        "1.6% below target" if gap < target else "63.8% above target",
    } <= labels
    assert all("µm" not in label and "um" not in label for label in labels)
    assert not any("Difference enlarged" in label for label in labels)
    assert "Separate scale" not in labels
    assert "Both bars use the same scale" not in labels
    assert "Actual versus target - schematic" not in labels
    assert f"{abs(gap - target) / 1_000_000:g} mm" in labels
    bars = {
        node.get("data-spacing-schematic-gap"): node
        for node in root.iter()
        if node.get("data-spacing-schematic-gap")
    }
    assert bars == {}
    assert not any(
        node.get("data-spacing-schematic-difference") for node in root.iter()
    )
    assert any(node.get("data-spacing-detail-panel") for node in root.iter())
    assert any(node.get("data-spacing-overview-locator") for node in root.iter())
    # Labels remain outside physical panels and native composition paints them.
    for panel in root.iter():
        if panel.get("data-spacing-raster-panel"):
            assert not any(node.get("data-spacing-label") for node in panel.iter())
    assert "<text" in renderer.spacing_svg(entry)


def test_complete_composition_is_readable_at_the_supported_fitted_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 560 by 350 fit keeps every label, copper feature, and guide visible."""
    from impedance.spacing_review import SPACING_RENDER_POLICY_REVISION

    config, snapshot = _config(), _parallel(50_000, length=20_000_000)
    renderer, _ = _renderer(monkeypatch)
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    for entry in entries:
        composition = entry.composition
        fit = min(560 / entry.width_px, 350 / entry.height_px)
        assert fit == composition.minimum_scale
        assert 50_000 / entry.viewport_nm[2] * composition.detail_rect[2] * fit >= 8
        root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
        assert root.get("data-spacing-composition") == str(
            SPACING_RENDER_POLICY_REVISION
        )
        assert SPACING_RENDER_POLICY_REVISION == 7
        labels = [node for node in root.iter() if node.tag.endswith("text")]
        assert labels
        assert all(float(node.get("font-size")) >= 42 for node in labels)
        assert all(float(node.get("font-size")) * fit >= 14 for node in labels)
        guides = [
            node for node in root.iter() if node.get("data-impedance-spacing-guide")
        ]
        assert guides
        assert all(float(node.get("stroke-width")) * fit >= 1 for node in guides)


def test_original_spacing_image_retains_native_geometry_without_diagnostic_marks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Original comparison renders the same physical detail without added evidence."""
    from .test_impedance_report_pipeline import _png

    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, _ = _renderer(monkeypatch)
    seen = []

    def rasterize(source: Path, output: Path, width: int, height: int) -> None:
        seen.append(source.read_text())
        output.write_bytes(_png(width, height))

    renderer._rasterizer = rasterize
    renderer.render_spacing_original(entry, tmp_path / "original.png")
    assert "M 0,0 L 1000,0" in seen[0]
    assert "data-impedance-spacing-guide" not in seen[0]
    assert "data-spacing-label" not in seen[0]


def test_summary_uses_the_marked_measurement_instead_of_bound_midpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative region bounds must never become a fabricated exact gap."""
    from dataclasses import replace

    from impedance.spacing_checks import NmBounds

    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    entry = replace(entry, region_gap_nm=NmBounds(150_000, 300_000))
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    labels = {node.get("data-spacing-label") for node in root.iter()}
    assert "0.25 mm" in labels
    assert "0.15-0.3 mm" in labels
    assert "0.225 mm" not in labels
    ticks = [
        node
        for node in root.iter()
        if node.get("data-impedance-spacing-guide") == "measured-gap"
    ]
    assert len(ticks) == 1
    start = tuple(
        float(value) for value in ticks[0].get("data-board-start-nm").split(",")
    )
    end = tuple(float(value) for value in ticks[0].get("data-board-end-nm").split(","))
    assert math.dist(start, end) == pytest.approx(250_000)


def test_unequal_nanometre_bounds_keep_both_mm_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A two-nanometre region interval remains a range in the approved image."""
    from dataclasses import replace

    from impedance.spacing_checks import NmBounds

    config, snapshot = _config(), _parallel(200_000, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    entry = replace(entry, region_gap_nm=NmBounds(199_999, 200_001))
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    labels = {node.get("data-spacing-label") for node in root.iter()}
    assert "0.199999-0.200001 mm" in labels
    assert entry.composition.actual_gap_nm == pytest.approx(200_000)


def test_varying_gap_numbers_describe_the_one_visible_marked_crosssection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A varying route has one identifiable tick rather than ambiguous samples."""
    from .test_impedance_spacing_checks import _line, _snapshot

    config = _config()
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (1_000_000, 0))),
        _line("n", "N", ((0, 400_000), (1_000_000, 500_000))),
    )
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    assert entry.region_gap_nm.lower_nm < entry.region_gap_nm.upper_nm
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    ticks = [
        node
        for node in root.iter()
        if node.get("data-impedance-spacing-guide") == "measured-gap"
    ]
    assert len(ticks) == 1
    start = tuple(
        float(value) for value in ticks[0].get("data-board-start-nm").split(",")
    )
    end = tuple(float(value) for value in ticks[0].get("data-board-end-nm").split(","))
    actual = math.dist(start, end)
    assert actual == pytest.approx(entry.composition.actual_gap_nm)
    label = f"{actual / 1_000_000:.6f}".rstrip("0").rstrip(".") + " mm"
    assert label in {node.get("data-spacing-label") for node in root.iter()}
    assert not any(node.get("data-impedance-spacing-clearance") for node in root.iter())


def test_regional_measurement_keeps_known_target_without_inventing_local_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conservative regional result still displays the requested spacing in mm."""
    from dataclasses import replace

    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    entry = replace(
        entry,
        members=tuple(replace(member, cross_sections=()) for member in entry.members),
        representative=None,
    )
    assert entry.composition.actual_gap_nm is None
    assert entry.composition.region_gap_nm is not None
    assert entry.composition.schematic_rect in entry.composition.required_regions
    renderer, _ = _renderer(monkeypatch)
    root = ET.fromstring(renderer.spacing_svg(entry))  # noqa: S314 -- Local SVG.
    labels = {
        node.get("data-spacing-label")
        for node in root.iter()
        if node.get("data-spacing-label")
    }
    assert {
        "Target gap",
        "0.15 mm",
        "Gap deviation from target",
        "Unavailable",
        "See region gap range",
    } <= labels
    assert "Region gap range" in labels and "0.25 mm" in labels
    assert not any("witness" in label.lower() for label in labels)
    assert not {
        "Actual gap",
        "Too close by",
        "Wider by",
        "Difference enlarged",
    }.intersection(labels)
    assert not any(node.get("data-spacing-schematic-gap") for node in root.iter())
    assert not any(
        node.get("data-spacing-schematic-difference") for node in root.iter()
    )


@pytest.mark.parametrize("layout", ("standard", "wide"))
@pytest.mark.parametrize(
    "actual,target,expected",
    (
        (200_000.0, 203_200.0, "1.6% below target"),
        (331_300.0, 202_300.0, "63.8% above target"),
        (200_000.0, 200_000.0, "0.0% at target"),
        (199_999.0, 200_000.0, "<0.1% below target"),
        (200_001.0, 200_000.0, "<0.1% above target"),
        (199_800.0, 200_000.0, "0.1% below target"),
        (200_200.0, 200_000.0, "0.1% above target"),
        (199_999.9999, 200_000.0, "<0.1% below target"),
        (200_199.9999, 200_000.0, "<0.1% above target"),
        (200_000.0, 1_000.0, "19900.0% above target"),
        (0.0, 200_000.0, "100.0% below target"),
        (-25_000.0, 100_000.0, "125.0% below target"),
        (None, 200_000.0, "Unavailable"),
        (float("nan"), 200_000.0, "Unavailable"),
        (float("inf"), 200_000.0, "Unavailable"),
        (float("-inf"), 200_000.0, "Unavailable"),
        (200_000.0, None, "Unavailable"),
        (200_000.0, 0.0, "Unavailable"),
        (200_000.0, -1.0, "Unavailable"),
        (200_000.0, float("nan"), "Unavailable"),
        (200_000.0, float("inf"), "Unavailable"),
        (200_000.0, float("-inf"), "Unavailable"),
        (1e308, 1e-308, "Unavailable"),
    ),
)
def test_percentage_footer_uses_unrounded_target_relative_gap_and_is_always_required(
    layout: str, actual: Optional[float], target: Optional[float], expected: str
) -> None:
    """Both layouts retain one readable, truthful footer for every measurement state."""
    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    if layout == "wide":
        entry = replace(
            entry,
            composition_layout="wide",
            viewport_nm=(-1_000_000, -500_000, 6_000_000, 6_000_000 * 460 / 1568),
        )
    composition = replace(
        entry.composition, actual_gap_nm=actual, target_spacing_nm=target
    )
    entry = replace(entry, target_spacing_nm=target)
    object.__setattr__(entry, "composition", composition)
    root = ET.fromstring(  # noqa: S314 -- Locally generated labels.
        "<svg>" + renderer_module._spacing_summary_svg(entry) + "</svg>"
    )
    labels = [
        node.get("data-spacing-label")
        for node in root.iter()
        if node.get("data-spacing-label")
    ]
    assert "Gap deviation from target" in labels
    assert expected in labels
    assert composition.schematic_rect in composition.required_regions
    footer = [
        node
        for node in root.iter()
        if node.get("data-spacing-label")
        and float(node.get("data-spacing-text-top")) >= composition.schematic_rect[1]
    ]
    assert {node.get("data-spacing-label") for node in footer} == {
        "Gap deviation from target",
        expected,
    }
    value = next(node for node in footer if node.get("data-spacing-label") == expected)
    assert float(value.get("font-size")) >= 72
    assert not any("nan" in label.lower() or "inf" in label.lower() for label in labels)
    assert "Both bars use the same scale" not in labels
    assert "Regional measurement" not in labels
    assert not any(node.get("data-spacing-schematic-gap") for node in root.iter())
