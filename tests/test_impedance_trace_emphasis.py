"""Emphasize represented native copper without painting guessed track corridors."""

from dataclasses import replace
from typing import Optional
from xml.etree import ElementTree as ET

import pytest

from impedance.model import Section, Trace
from impedance.pcbnew_adapter import sample_arc
from impedance.render import annotated_svg, compose_layer_svgs


@pytest.mark.parametrize(
    ("attribute", "opacity"),
    [
        ('opacity="0.5"', 0.5),
        ('style="opacity:0.5"', 0.5),
        ('opacity="0.5" style="opacity:0.25"', 0.25),
        ('opacity="0"', 0.0),
    ],
)
def test_root_transparency_applies_only_to_native_content(
    attribute: str, opacity: float
) -> None:
    """Moving source SVG children must retain root alpha without dimming annotations."""
    source = _page(
        '<path data-object="selected" d="M1000 1000 L2000 1000" '
        'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.7"/>'
        '<path data-object="context" d="M1000 1030 L2000 1030" '
        'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.4"/>'
    ).replace('viewBox="0 0 4000 4000"', 'viewBox="0 0 4000 4000" ' + attribute)
    capture = annotated_svg(source, _section(_straight()))
    root = ET.fromstring(capture)  # noqa: S314 -- Generated local SVG only.
    content = next(
        node for node in root if node.get("data-impedance-view") == "segment"
    )
    assert float(content.get("opacity", "1")) == opacity
    objects = _objects(capture)
    assert float(objects["selected"].get("opacity", "1")) == 1
    assert float(objects["selected"]["stroke-opacity"]) * opacity == pytest.approx(
        0.7 * opacity
    )
    assert float(objects["context"]["stroke-opacity"]) * opacity == pytest.approx(
        0.4 * opacity
    )
    assert float(root[0].get("opacity", "1")) == 1
    box = next(node for node in root if node.get("data-impedance-highlight") == "box")
    assert float(box.get("opacity", "1")) == 1
    assert float(box.get("stroke-opacity", "1")) == 1


def _page(body: str) -> str:
    """Use the physical page scale emitted by the native SVG plotter."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm" '
        'viewBox="0 0 4000 4000"><g transform="translate(0 0) scale(1 1)">'
        + body
        + "</g></svg>"
    )


def _section(*traces: Trace) -> Section:
    """Describe represented tracks without expanding them to neighboring copper."""
    radius = max(trace.width_nm for trace in traces) // 2
    points = [point for trace in traces for point in trace.points]
    return Section(
        "section",
        "usb",
        "F.Cu",
        traces[0].width_nm,
        traces,
        (
            min(x for x, _ in points) - radius,
            min(y for _, y in points) - radius,
            max(x for x, _ in points) + radius,
            max(y for _, y in points) + radius,
        ),
        tuple(sorted({trace.net for trace in traces})),
    )


def _straight() -> Trace:
    """Return the selected 0.2 mm track from 10,10 mm to 20,10 mm."""
    return Trace(
        "selected",
        "F.Cu",
        "/D+",
        200_000,
        ((10_000_000, 10_000_000), (20_000_000, 10_000_000)),
    )


def _objects(svg: str) -> dict[str, dict[str, str]]:
    """Read the explicit flattened native paint that reaches NanoSVG."""
    root = ET.fromstring(svg)  # noqa: S314 -- Local generated SVG without entities.
    result = {}
    for node in root.iter():
        name = node.get("data-object")
        if name is None:
            continue
        paint = dict(node.attrib)
        paint.update(
            item.split(":", 1)
            for item in node.get("style", "").split(";")
            if ":" in item
        )
        result[name] = paint
    return result


def _assert_only_stroke_color_changes(
    before: dict[str, str], after: dict[str, str], expected_color: str
) -> None:
    """Keep native geometry, width, alpha and other paint independent of emphasis."""
    assert after["stroke"] == expected_color
    assert {
        key: value for key, value in after.items() if key not in {"stroke", "style"}
    } == {key: value for key, value in before.items() if key not in {"stroke", "style"}}


@pytest.mark.parametrize(
    ("background", "selected_color", "surrounding_color"),
    [
        ("#10151d", "#d87171", "#6c2428"),
        ("#ffffff", "#8c2424", "#e49a9a"),
    ],
)
def test_represented_stroke_adds_contrast_and_surrounding_copper_dims(
    background: str,
    selected_color: str,
    surrounding_color: str,
) -> None:
    """Parallel nonmembers, pads and zones dim while silk and edge stay readable."""
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="selected" d="M 1000,1000 L 2000,1000" '
                'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.7"/>'
                '<path data-object="parallel" d="M 1000,1030 L 2000,1030" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
                '<circle data-object="pad" cx="1000" cy="1000" r="50" fill="#c83434"/>'
                '<rect data-object="zone" x="500" y="500" width="2000" height="1000" fill="#c83434"/>'
            ),
            "F.SilkS": _page(
                '<path data-object="silk" d="M 1000,800 L 1200,800" '
                'fill="none" stroke="#eeeeee" stroke-width="10"/>'
            ),
            "Edge.Cuts": _page(
                '<path data-object="edge" d="M 1000,500 L 2000,500" '
                'fill="none" stroke="#b5aa66" stroke-width="10"/>'
            ),
        },
        "F.Cu",
        background_color=background,
    )
    before = _objects(source)
    capture = annotated_svg(source, _section(_straight()), background_color=background)
    after = _objects(capture)

    _assert_only_stroke_color_changes(
        before["selected"], after["selected"], selected_color
    )
    _assert_only_stroke_color_changes(
        before["parallel"], after["parallel"], surrounding_color
    )
    for name in ("pad", "zone"):
        assert after[name]["fill"] == surrounding_color
        assert after[name]["fill-opacity"] == before[name]["fill-opacity"]
    for name in ("silk", "edge"):
        assert after[name] == before[name]
    assert set(after) == set(before)
    assert 'stroke="#FFFF00"' in capture
    assert "mask=" not in capture and "clip-path=" not in capture


@pytest.mark.parametrize(
    ("background", "selected_color", "surrounding_color"),
    [
        ("#10151d", "#d87171", "#6c2428"),
        ("#ffffff", "#8c2424", "#e49a9a"),
    ],
)
def test_native_arc_emphasis_preserves_the_actual_arc_and_dims_its_chord(
    background: str,
    selected_color: str,
    surrounding_color: str,
) -> None:
    """The sampled centerline selects native curved copper without painting chords."""
    trace = replace(
        _straight(),
        points=sample_arc(
            (10_000_000, 15_000_000),
            (15_000_000, 10_000_000),
            (20_000_000, 15_000_000),
            200_000,
        ),
        is_native_arc=True,
    )
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="arc" d="M 1000,1500 A 500,500 0 0 1 2000,1500" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
                '<path data-object="chord" d="M 1000,1500 L 2000,1500" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
                '<path data-object="other-side" d="M 1000,1500 A 500,500 0 0 0 2000,1500" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
            )
        },
        "F.Cu",
        background_color=background,
    )
    before, after = (
        _objects(source),
        _objects(annotated_svg(source, _section(trace), background_color=background)),
    )
    _assert_only_stroke_color_changes(before["arc"], after["arc"], selected_color)
    _assert_only_stroke_color_changes(
        before["chord"], after["chord"], surrounding_color
    )
    _assert_only_stroke_color_changes(
        before["other-side"], after["other-side"], surrounding_color
    )


@pytest.mark.parametrize(
    ("background", "surrounding_color"),
    [("#10151d", "#6c2428"), ("#ffffff", "#e49a9a")],
)
def test_unknown_or_transformed_native_geometry_is_never_guessed_as_selected(
    background: str,
    surrounding_color: str,
) -> None:
    """An exact local endpoint match cannot identify translated or filled copper."""
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<g transform="translate(0 30)"><path data-object="translated" '
                'd="M 1000,1000 L 2000,1000" fill="none" stroke="#c83434" stroke-width="20"/></g>'
                '<path data-object="filled" d="M 1000,1000 L 2000,1000 L 2000,1020 Z" '
                'fill="#c83434" stroke="none"/>'
                '<path data-object="unknown" d="M 1000,1000 C 1300,900 1700,1100 2000,1000" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
            )
        },
        "F.Cu",
        background_color=background,
    )
    before, after = (
        _objects(source),
        _objects(
            annotated_svg(source, _section(_straight()), background_color=background)
        ),
    )
    for name in before:
        channel = "fill" if name == "filled" else "stroke"
        assert after[name][channel] == surrounding_color
        assert after[name][f"{channel}-opacity"] == before[name][f"{channel}-opacity"]


@pytest.mark.parametrize(
    "geometry",
    [
        'path d="M2000 1000 L1000 1000"',
        'path d="M1e3 1e3 L2e3 1e3"',
        'line x1="1000" y1="1000" x2="2000" y2="1000"',
        'polyline points="1000,1000 2000,1000"',
    ],
)
def test_native_line_spellings_and_reversed_direction_only_change_stroke_color(
    geometry: str,
) -> None:
    """Selection follows physical centerline geometry rather than SVG syntax."""
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                f'<{geometry} data-object="selected" fill="none" '
                'stroke="#c83434" stroke-width="20"/>'
            )
        },
        "F.Cu",
        background_color="#10151d",
    )
    before = _objects(source)
    after = _objects(annotated_svg(source, _section(_straight())))
    _assert_only_stroke_color_changes(before["selected"], after["selected"], "#d87171")


def test_connected_bend_emphasizes_all_members_without_emphasizing_a_diagonal() -> None:
    """Contrast the two real legs without filling their bounding box."""
    horizontal = _straight()
    vertical = replace(
        horizontal,
        trace_id="vertical",
        points=((20_000_000, 10_000_000), (20_000_000, 14_000_000)),
    )
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="horizontal" d="M1000 1000 L2000 1000" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
                '<path data-object="vertical" d="M2000 1000 L2000 1400" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
                '<path data-object="diagonal" d="M1000 1000 L2000 1400" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
            )
        },
        "F.Cu",
        background_color="#10151d",
    )
    before = _objects(source)
    after = _objects(annotated_svg(source, _section(horizontal, vertical)))
    _assert_only_stroke_color_changes(
        before["horizontal"], after["horizontal"], "#d87171"
    )
    _assert_only_stroke_color_changes(before["vertical"], after["vertical"], "#d87171")
    _assert_only_stroke_color_changes(before["diagonal"], after["diagonal"], "#6c2428")


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("background", "selected_color"),
    [("#10151d", "#d87171"), ("#ffffff", "#8c2424")],
)
def test_major_native_arc_and_reversed_endpoint_order_stay_native(
    reverse: bool,
    background: str,
    selected_color: str,
) -> None:
    """Preserve the selected 270-degree native arc without shortening its sweep."""
    points = sample_arc(
        (20_000_000, 15_000_000),
        (10_000_000, 15_000_000),
        (15_000_000, 10_000_000),
        200_000,
    )
    trace = replace(
        _straight(),
        points=tuple(reversed(points)) if reverse else points,
        is_native_arc=True,
    )
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="major" d="M2000 1500 A500 500 0.0 1 1 1500 1000" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
            )
        },
        "F.Cu",
        background_color=background,
    )
    before, after = (
        _objects(source),
        _objects(annotated_svg(source, _section(trace), background_color=background)),
    )
    _assert_only_stroke_color_changes(before["major"], after["major"], selected_color)


def test_native_arc_endpoint_serialization_roundoff_does_not_drop_emphasis() -> None:
    """A 50 nm native-coordinate rounding shift cannot put the start on the wrong sweep."""
    trace = replace(
        _straight(),
        points=sample_arc(
            (10_000_000, 15_000_000),
            (15_000_000, 10_000_000),
            (20_000_000, 15_000_000),
            200_000,
        ),
        is_native_arc=True,
    )
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="arc" d="M1000 1499.995 A500 500 0 0 1 2000 1499.995" '
                'fill="none" stroke="#c83434" stroke-width="20"/>'
            )
        },
        "F.Cu",
        background_color="#10151d",
    )
    before, after = _objects(source), _objects(annotated_svg(source, _section(trace)))
    _assert_only_stroke_color_changes(before["arc"], after["arc"], "#d87171")


@pytest.mark.parametrize(
    ("background", "selected_color", "surrounding_color"),
    [
        ("#10151d", "#d87171", "#6c2428"),
        ("#ffffff", "#8c2424", "#e49a9a"),
        ("#808080", "#8c2424", "#a45a5a"),
        ("#0000ff", "#d87171", "#641a9a"),
        ("#00ff00", "#8c2424", "#649a1a"),
    ],
)
def test_capture_tuning_uses_exact_selected_and_surrounding_contrast(
    background: str, selected_color: str, surrounding_color: str
) -> None:
    """Apply the requested 30% selected contrast and 50% contextual background mix."""
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="selected" d="M1000 1000 L2000 1000" '
                'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.7"/>'
                '<path data-object="parallel" d="M1000 1030 L2000 1030" '
                'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.4"/>'
            )
        },
        "F.Cu",
        background_color=background,
    )
    before = _objects(source)
    original_source = source
    capture = annotated_svg(source, _section(_straight()), background_color=background)
    after = _objects(capture)
    _assert_only_stroke_color_changes(
        before["selected"], after["selected"], selected_color
    )
    _assert_only_stroke_color_changes(
        before["parallel"], after["parallel"], surrounding_color
    )
    assert source == original_source
    assert _objects(source) == before
    native_root = ET.fromstring(source)  # noqa: S314 -- Generated local SVG only.
    capture_root = ET.fromstring(capture)  # noqa: S314 -- Generated local SVG only.
    shapes = {"path", "circle", "ellipse", "polyline", "polygon", "rect", "line"}
    assert sum(
        node.tag.rsplit("}", 1)[-1] in shapes for node in capture_root.iter()
    ) == (
        sum(node.tag.rsplit("}", 1)[-1] in shapes for node in native_root.iter()) + 2
    ), "Only the existing canvas and yellow rectangle may add geometry"


def test_matching_geometry_without_native_stroke_stays_invisible() -> None:
    """SVG's default absent stroke cannot become an invented represented trace."""
    source = _page(
        '<g fill="none"><path data-object="unpainted" d="M1000 1000 L2000 1000" '
        'stroke-width="20" opacity="0.6" stroke-opacity="0.4"/></g>'
    )
    original_source = source
    before = _objects(source)["unpainted"]

    capture = annotated_svg(source, _section(_straight()))

    after = _objects(capture)["unpainted"]
    assert after.get("stroke", "none") == "none"
    assert after["fill"] == "none"
    for key in ("d", "stroke-width", "opacity", "stroke-opacity"):
        assert after[key] == before[key]
    root = ET.fromstring(capture)  # noqa: S314 -- Generated local SVG only.
    paths = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "path"]
    assert len(paths) == 1
    assert source == original_source
    assert _objects(source)["unpainted"] == before


@pytest.mark.parametrize(
    (
        "nested",
        "selected_opacity",
        "context_opacity",
        "selected_alpha",
        "context_alpha",
    ),
    [
        (False, None, None, 0.35, 0.2),
        (True, "0.6", None, 0.168, 0.16),
        (True, None, "0.3", 0.28, 0.048),
    ],
)
def test_raw_svg_emphasis_preserves_group_and_leaf_transparency(
    nested: bool,
    selected_opacity: Optional[str],
    context_opacity: Optional[str],
    selected_alpha: float,
    context_alpha: float,
) -> None:
    """Group compositing alpha cannot be copied onto rewritten native leaf paint."""
    selected_attribute = f' opacity="{selected_opacity}"' if selected_opacity else ""
    context_attribute = f' opacity="{context_opacity}"' if context_opacity else ""
    leaves = (
        '<path data-object="selected" d="M1000 1000 L2000 1000" '
        'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.7"'
        + selected_attribute
        + '/><path data-object="context" d="M1000 1030 L2000 1030" '
        'fill="none" stroke="#c83434" stroke-width="20" stroke-opacity="0.4"'
        + context_attribute
        + "/>"
    )
    if nested:
        leaves = '<g data-object="inner" opacity="0.8">' + leaves + "</g>"
    source = _page('<g data-object="outer" opacity="0.5">' + leaves + "</g>")
    before = _objects(source)

    capture = annotated_svg(source, _section(_straight()))

    after = _objects(capture)
    assert after["selected"]["stroke"] == "#d87171"
    assert after["context"]["stroke"] == "#6c2428"
    for name in before:
        for channel in ("opacity", "stroke-opacity"):
            assert after[name].get(channel) == before[name].get(channel), (
                name,
                channel,
                before[name],
                after[name],
            )

    def effective_alpha(svg: str) -> dict[str, float]:
        """Evaluate original SVG group compositing separately from inherited paint."""
        root = ET.fromstring(svg)  # noqa: S314 -- Generated local SVG only.
        result: dict[str, float] = {}

        def visit(node: ET.Element, group_alpha: float) -> None:
            """Multiply each element's own opacity once along its ancestor path."""
            properties = dict(node.attrib)
            properties.update(
                field.split(":", 1)
                for field in node.get("style", "").split(";")
                if ":" in field
            )
            alpha = group_alpha * float(properties.get("opacity", "1"))
            name = node.get("data-object")
            if name in {"selected", "context"}:
                result[name] = alpha * float(properties.get("stroke-opacity", "1"))
            for child in node:
                visit(child, alpha)

        visit(root, 1)
        return result

    expected = {"selected": selected_alpha, "context": context_alpha}
    assert effective_alpha(source) == pytest.approx(expected)
    assert effective_alpha(capture) == pytest.approx(expected)
    assert _objects(source) == before


@pytest.mark.parametrize(
    ("background", "native_color", "selected_color", "surrounding_color"),
    [
        ("#808080", "#ffffff", "#ffffff", "#c0c0c0"),
        ("#555555", "#000000", "#000000", "#2a2a2a"),
    ],
)
def test_custom_theme_emphasis_never_reduces_existing_selected_contrast(
    background: str, native_color: str, selected_color: str, surrounding_color: str
) -> None:
    """Keep already contrasting custom copper instead of fading it toward the canvas."""
    source = compose_layer_svgs(
        {
            "F.Cu": _page(
                '<path data-object="selected" d="M1000 1000 L2000 1000" '
                f'fill="none" stroke="{native_color}" stroke-width="20" stroke-opacity="0.7"/>'
                '<path data-object="context" d="M1000 1030 L2000 1030" '
                f'fill="none" stroke="{native_color}" stroke-width="20" stroke-opacity="0.4"/>'
            )
        },
        "F.Cu",
        background_color=background,
    )
    before = _objects(source)

    capture = annotated_svg(source, _section(_straight()), background_color=background)

    after = _objects(capture)
    _assert_only_stroke_color_changes(
        before["selected"], after["selected"], selected_color
    )
    _assert_only_stroke_color_changes(
        before["context"], after["context"], surrounding_color
    )
    assert set(after) == set(before)
    assert _objects(source) == before
