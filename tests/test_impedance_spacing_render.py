"""Rasterize distinct diagnostic close-ups from native vector board context."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

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


def test_spacing_svg_has_native_traces_and_no_numeric_or_rectangle_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The physical gap stays visible; the application supplies its external caption."""
    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    renderer, calls = _renderer(monkeypatch)
    result = renderer.spacing_svg(entry)
    root = ET.fromstring(result)  # noqa: S314 -- Locally generated diagnostic SVG.
    assert root.get("width") == "1200"
    assert root.get("height") == "800"
    assert calls == ["F.Cu"]
    assert "data-impedance-spacing-view" in result
    assert "data-impedance-highlight" not in result
    assert "<text" not in result
    assert "<image" not in result
    assert "M 0,0 L 1000,0" in result
    assert "M 0,40 L 1000,40" in result
    assert CAPTURE_POLICY_REVISION == 8


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
    assert rasterized[0][1:] == (1200, 800)
    assert "data-impedance-spacing-view" in rasterized[0][0]
    assert destination.read_bytes() == _png(1200, 800)


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
    assert "<text" not in svg
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
