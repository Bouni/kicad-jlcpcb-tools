"""Exercise openable RF boards, physical route geometry, and fabrication rows."""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
from itertools import combinations, product
import json
from math import hypot, sqrt
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from impedance import service, workbook
from impedance.matching import analyze
from impedance.model import (
    Analysis,
    Bounds,
    Config,
    Point,
    Trace,
    resolved_layer_settings,
)
from impedance.render import section_viewport
from scripts import generate_rf_impedance_fixtures as fixture_generator
from tests.rf_impedance_combined import COMBINED_BOARDS
from tests.rf_impedance_fixtures import (
    BOARD_BOUNDS,
    COPPER_LAYERS,
    DK,
    FENCE_PITCH_NM,
    FREQUENCY_HZ,
    RF_CASES,
    RFCase,
)
from tests.rf_impedance_showcases import SMA, USB
from tests.test_impedance_workbook import _png

_EXAMPLE_DIRECTORY = Path(__file__).resolve().parents[1] / "examples" / "impedance"
_SIGNAL_LAYERS = ("F.Cu", "In2.Cu", "B.Cu")
_REFERENCES = {
    "F.Cu": ("In1.Cu",),
    "In2.Cu": ("In1.Cu", "In3.Cu"),
    "B.Cu": ("In4.Cu",),
}
_SHEET_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture(scope="module")
def matrix_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize opt-in isolated native projects outside the tracked review directory."""
    directory = tmp_path_factory.mktemp("rf-matrix")
    assert fixture_generator.generate(directory, legacy_matrix=True) == ()
    return directory


def _prepare(case: RFCase) -> tuple[Analysis, service.ExportPlan]:
    """Exercise real automatic pair matching, review, and export preflight."""
    config = Config(enabled=True, specifications=case.specifications())
    snapshot = case.snapshot()
    grouped = analyze(config, snapshot)
    config = replace(
        config,
        reviewed_digest=grouped.digest,
        included_section_ids=tuple(section.section_id for section in grouped.sections),
    )
    plan = service.prepare(config, snapshot, layer_count=6)
    assert plan is not None
    return grouped, plan


def _length(traces: tuple[Trace, ...]) -> float:
    """Measure actual track centerlines, including differential transition fanout."""
    return sum(
        hypot(end[0] - start[0], end[1] - start[1])
        for trace in traces
        for start, end in zip(trace.points, trace.points[1:])
    )


def _endpoints(traces: tuple[Trace, ...], layer: str, net: str) -> set[Point]:
    """Find physical endpoints without treating same-net names as connectivity."""
    return {
        point
        for trace in traces
        if trace.layer == layer and trace.net == net
        for point in (trace.points[0], trace.points[-1])
    }


def _native_tree(source: str) -> list[Any]:
    """Parse only the deterministic fixture's native PCB S-expressions."""
    stack: list[list[Any]] = [[]]
    for token in re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', source):
        if token == "(":
            child: list[Any] = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ")":
            assert len(stack) > 1, "Unbalanced native PCB fixture"
            stack.pop()
        else:
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    assert len(stack) == 1 and len(stack[0]) == 1
    root = stack[0][0]
    assert root[0] == "kicad_pcb"
    return root


def _children(node: list[Any], name: str) -> list[list[Any]]:
    """Read direct native PCB fields without matching text in comments or labels."""
    return [item for item in node[1:] if isinstance(item, list) and item[0] == name]


def _field(node: list[Any], name: str) -> list[Any]:
    """Require a single structural field on a generated native object."""
    matches = _children(node, name)
    assert len(matches) == 1, (node[0], name)
    return matches[0][1:]


def _native_point(node: list[Any], name: str) -> Point:
    """Decode millimetre native coordinates into exact integer nanometres."""
    values = _field(node, name)
    return (int(Decimal(values[0]) * 1_000_000), int(Decimal(values[1]) * 1_000_000))


def _zone_polygon(node: list[Any]) -> tuple[Point, ...]:
    """Read a real zone boundary, not a similarly named drawing or text property."""
    polygon = _children(node, "polygon")
    assert len(polygon) == 1
    points = _children(polygon[0], "pts")
    assert len(points) == 1
    return tuple(
        (int(Decimal(point[1]) * 1_000_000), int(Decimal(point[2]) * 1_000_000))
        for point in _children(points[0], "xy")
    )


def _placed_point(footprint: list[Any], point: Point) -> Point:
    """Apply native clockwise quarter-turn placement without coordinate rounding."""
    position = _field(footprint, "at")
    rotation = int(Decimal(position[2])) % 360 if len(position) > 2 else 0
    x, y = point
    assert rotation in (0, 90, 180, 270)
    x, y = {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}[rotation]
    origin_x, origin_y = _native_point(footprint, "at")
    return origin_x + x, origin_y + y


def _pad_geometry(footprint: list[Any], pad: list[Any]) -> tuple[Point, Bounds]:
    """Native pad positions are local, but pad angles already include footprint rotation."""
    x, y = _placed_point(footprint, _native_point(pad, "at"))
    width, height = (int(Decimal(value) * 1000000) for value in _field(pad, "size"))
    position = _field(pad, "at")
    rotation = int(Decimal(position[2])) % 360 if len(position) > 2 else 0
    assert rotation in (0, 90, 180, 270)
    if rotation in (90, 270):
        width, height = height, width
    return (x, y), (
        x - (width + 1) // 2,
        y - (height + 1) // 2,
        x + (width + 1) // 2,
        y + (height + 1) // 2,
    )


def _native_component_bounds(native: list[Any]) -> tuple[tuple[str, Bounds], ...]:
    """Bound actual embedded component bodies, pads and reference labels, including USB rotations."""
    components = []
    for footprint in _children(native, "footprint"):
        rectangles = _children(footprint, "fp_rect") + _children(footprint, "fp_line")
        pads = _children(footprint, "pad")
        references = [
            item
            for item in _children(footprint, "fp_text")
            if item[1] == "reference" and item[2]
        ] + [
            item
            for item in _children(footprint, "property")
            if item[1] == "Reference" and item[2]
        ]
        if not rectangles or len(pads) < 2 or not references:
            continue
        points = [
            _placed_point(footprint, _native_point(rectangle, endpoint))
            for rectangle in rectangles
            for endpoint in ("start", "end")
        ]
        for pad in pads:
            _, (left, top, right, bottom) = _pad_geometry(footprint, pad)
            points.extend(((left, top), (right, bottom)))
        for reference in references:
            x, y = _placed_point(footprint, _native_point(reference, "at"))
            font = _children(_children(reference, "effects")[0], "font")[0]
            width, height = (
                int(Decimal(value) * 1000000) for value in _field(font, "size")
            )
            stroke = int(Decimal(_field(font, "thickness")[0]) * 1000000)
            half_width = (len(reference[2]) * width + stroke + 1) // 2
            half_height = (height + stroke + 1) // 2
            points.extend(
                ((x - half_width, y - half_height), (x + half_width, y + half_height))
            )
        components.append(
            (
                references[0][2],
                (
                    min(x for x, _ in points),
                    min(y for _, y in points),
                    max(x for x, _ in points),
                    max(y for _, y in points),
                ),
            )
        )
    return tuple(components)


def _bounds_distance(first: Bounds, second: Bounds) -> float:
    """Minimum distance between conservative axis-aligned rectangular copper envelopes."""
    return hypot(
        max(first[0] - second[2], second[0] - first[2], 0),
        max(first[1] - second[3], second[1] - first[3], 0),
    )


def _assert_project_intent(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Allow KiCad-added defaults/order while checking every generator-owned design setting."""
    assert actual["meta"]["filename"] == expected["meta"]["filename"]
    actual_rules = actual["board"]["design_settings"]
    expected_rules = expected["board"]["design_settings"]
    assert actual_rules["drc_exclusions"] == expected_rules["drc_exclusions"] == []
    assert {
        key: actual_rules["rules"][key] for key in expected_rules["rules"]
    } == expected_rules["rules"]
    actual_nets, expected_nets = actual["net_settings"], expected["net_settings"]
    assert actual_nets["meta"] == expected_nets["meta"]
    actual_classes = {item["name"]: item for item in actual_nets["classes"]}
    assert len(actual_classes) == len(actual_nets["classes"])
    assert set(actual_classes) == {item["name"] for item in expected_nets["classes"]}
    for expected_class in expected_nets["classes"]:
        actual_class = actual_classes[expected_class["name"]]
        assert {key: actual_class[key] for key in expected_class} == expected_class
    assert actual_nets.get("netclass_assignments") in (None, {})
    assert sorted(
        (item["pattern"], item["netclass"]) for item in actual_nets["netclass_patterns"]
    ) == sorted(
        (item["pattern"], item["netclass"])
        for item in expected_nets["netclass_patterns"]
    )


def _orientation(start: Point, end: Point, point: Point) -> int:
    """Return exact signed area for containment and segment-intersection decisions."""
    return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (
        point[0] - start[0]
    )


def _on_segment(point: Point, start: Point, end: Point) -> bool:
    """Include collinear endpoints when testing exact native polygon boundaries."""
    return (
        _orientation(start, end, point) == 0
        and min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
        and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])
    )


def _inside_polygon(point: Point, polygon: tuple[Point, ...]) -> bool:
    """Require strict containment using an integer winding number, including concavity."""
    winding = 0
    for start, end in zip(polygon, (*polygon[1:], polygon[0])):
        if _on_segment(point, start, end):
            return False
        side = _orientation(start, end, point)
        if start[1] <= point[1] < end[1] and side > 0:
            winding += 1
        elif end[1] <= point[1] < start[1] and side < 0:
            winding -= 1
    return winding != 0


def _segments_intersect(
    first: Point, second: Point, third: Point, fourth: Point
) -> bool:
    """Detect crossings and touches before applying endpoint-distance formulas."""
    if any(
        _on_segment(point, start, end)
        for point, start, end in (
            (first, third, fourth),
            (second, third, fourth),
            (third, first, second),
            (fourth, first, second),
        )
    ):
        return True
    return (
        _orientation(first, second, third) * _orientation(first, second, fourth) < 0
        and _orientation(third, fourth, first) * _orientation(third, fourth, second) < 0
    )


def _point_has_clearance(
    point: Point, start: Point, end: Point, diameter_nm: int
) -> bool:
    """Compare squared distances exactly, retaining half-nanometre copper radii."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    px, py = point[0] - start[0], point[1] - start[1]
    length_squared = dx * dx + dy * dy
    projection = px * dx + py * dy
    if projection <= 0 or not length_squared:
        return 4 * (px * px + py * py) >= diameter_nm * diameter_nm
    if projection >= length_squared:
        qx, qy = point[0] - end[0], point[1] - end[1]
        return 4 * (qx * qx + qy * qy) >= diameter_nm * diameter_nm
    cross = px * dy - py * dx
    return 4 * cross * cross >= diameter_nm * diameter_nm * length_squared


def _contains_route_clearance(
    polygon: tuple[Point, ...], traces: tuple[Trace, ...], clearance_nm: int
) -> bool:
    """Contain complete trace capsules plus margin, not just selected sampled points."""
    if len(polygon) < 3 or not traces or clearance_nm < 0:
        return False
    for trace in traces:
        if not all(_inside_polygon(point, polygon) for point in trace.points):
            return False
        diameter = trace.width_nm + 2 * clearance_nm
        for first, second in zip(trace.points, trace.points[1:]):
            for third, fourth in zip(polygon, (*polygon[1:], polygon[0])):
                if _segments_intersect(first, second, third, fourth):
                    return False
                if not all(
                    _point_has_clearance(point, start, end, diameter)
                    for point, start, end in (
                        (first, third, fourth),
                        (second, third, fourth),
                        (third, first, second),
                        (fourth, first, second),
                    )
                ):
                    return False
    return True


def _has_native_transition_returns(case: RFCase, native: list[Any]) -> bool:
    """Require nearby symmetric GND returns from native via positions, nets, and spans."""
    nets = {item[1]: item[2] for item in _children(native, "net")}
    records = []
    for via in _children(native, "via"):
        first, last = _field(via, "layers")
        layers = set(
            COPPER_LAYERS[COPPER_LAYERS.index(first) : COPPER_LAYERS.index(last) + 1]
        )
        records.append((_native_point(via, "at"), nets[_field(via, "net")[0]], layers))
    traces = case.traces()
    signal_nets = {trace.net for trace in traces}
    for before, after in zip(case.legs, case.legs[1:]):
        signals = []
        for net in signal_nets:
            matches = [
                point
                for point, native_net, layers in records
                if native_net == net
                and {before.layer, after.layer} <= layers
                and point in _endpoints(traces, before.layer, net)
                and point in _endpoints(traces, after.layer, net)
            ]
            if len(matches) != 1:
                return False
            signals.append(matches[0])
        required_layers = set(before.reference_layers) | set(after.reference_layers)
        nearby = {
            point
            for point, net, layers in records
            if net == "GND"
            and required_layers <= layers
            and any(
                (point[0] - signal[0]) ** 2 + (point[1] - signal[1]) ** 2
                <= 1_000_000**2
                for signal in signals
            )
        }
        center_y = before.points[-1][1]
        mirrored = {
            point
            for point in nearby
            if point[1] != center_y and (point[0], 2 * center_y - point[1]) in nearby
        }
        if not mirrored or not all(
            any(
                (point[0] - signal[0]) ** 2 + (point[1] - signal[1]) ** 2
                <= 1_000_000**2
                for point in mirrored
            )
            for signal in signals
        ):
            return False
    return True


def _cell_text(sheet: ET.Element, address: str) -> str:
    """Read an actual output cell independent of numeric versus inline-string form."""
    cell = sheet.find(f'.//s:c[@r="{address}"]', _SHEET_NS)
    assert cell is not None, address
    if cell.get("t") == "inlineStr":
        return "".join(cell.itertext())
    return cell.findtext("s:v", default="", namespaces=_SHEET_NS)


def test_matrix_covers_every_layer_kind_and_required_length() -> None:
    """Retain both tiny and board-spanning routes for every requested RF topology."""
    assert COPPER_LAYERS == ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
    assert len(RF_CASES) == 28
    assert len({case.name for case in RF_CASES}) == 28
    basic = [case for case in RF_CASES if len(case.legs) == 1]
    transitions = [case for case in RF_CASES if len(case.legs) == 3]
    expected = set(product((False, True), (False, True), _SIGNAL_LAYERS, (2, 120)))
    assert {
        (case.paired, case.coplanar, case.legs[0].layer, case.length_mm)
        for case in basic
    } == expected
    assert {(case.paired, case.coplanar) for case in transitions} == set(
        product((False, True), repeat=2)
    )
    assert all(case.length_mm == 150 for case in transitions)
    assert sum(len(case.legs) for case in RF_CASES) == 36
    assert sum(len(case.legs) * (2 if case.paired else 1) for case in RF_CASES) == 54


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_complete_segments_pair_grouping_and_physical_layer_references(
    case: RFCase,
) -> None:
    """A class covers all actual profiles, including USB launch layers and widths."""
    raw, plan = _prepare(case)
    profiles = {(trace.layer, trace.width_nm) for trace in case.traces()}
    assert {(row.layer, row.width_nm) for row in raw.sections} == profiles
    assert len(raw.sections) == len(profiles) == len(plan.sections)
    assert len(case.specifications()) == 1
    assert all("branched" in warning for warning in raw.warnings)
    if not case.paired:
        assert not raw.warnings
    assert {trace.trace_id for row in plan.sections for trace in row.traces} == {
        trace.trace_id for trace in case.traces()
    }
    spec = plan.config.specifications[0]
    assert spec.net_class == case.net_class
    assert spec.target_ohms == ("90" if case.paired else "50")
    assert spec.kind.startswith("differential") == case.paired
    assert spec.kind.endswith("_coplanar") == case.coplanar
    for row in plan.sections:
        settings = resolved_layer_settings(spec, row.layer)
        assert settings.reference_layers == _REFERENCES[row.layer]
        assert row.net_names == tuple(sorted(case.net_names))
        assert settings.spacing_nm == (203200 if case.paired else None)
        assert settings.ground_gap_nm == (200000 if case.coplanar else None)
        assert {trace.net for trace in row.traces} == set(case.net_names)


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_rf_capture_contains_whole_routes_and_minimum_board_context(
    case: RFCase,
) -> None:
    """Prevent any layer or impedance kind from reverting to clipped trace tiles."""
    _, plan = _prepare(case)
    for section in plan.sections:
        viewport = section_viewport(section, 800, 420, board_bounds=BOARD_BOUNDS)
        assert viewport.width / viewport.height == pytest.approx(800 / 420)
        assert viewport.width >= (BOARD_BOUNDS[2] - BOARD_BOUNDS[0]) * 0.15 * 0.85
        assert viewport.height >= (BOARD_BOUNDS[3] - BOARD_BOUNDS[1]) * 0.15 * 0.85
        left, top, right, bottom = section.bounds
        assert viewport.left < left < right < viewport.left + viewport.width
        assert viewport.top < top < bottom < viewport.top + viewport.height
        for trace in section.traces:
            assert all(
                viewport.left < x < viewport.left + viewport.width
                and viewport.top < y < viewport.top + viewport.height
                for x, y in trace.points
            )


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_native_boards_preserve_real_components_for_contextual_rendering(
    case: RFCase,
) -> None:
    """Keep physical component context available without enlarging tight crops to fit remote ICs."""
    native = _native_tree(case.board_text())
    components = dict(_native_component_bounds(native))
    assert {"U1", "U2", "U3", "J1"} <= components.keys()
    assert all(
        left < right and top < bottom
        for left, top, right, bottom in components.values()
    )
    if case.paired:
        assert {"JUSB1", "JUSB2"} <= components.keys()
    elif case.length_mm > 2:
        assert {"JRF1", "JRF2"} <= components.keys()
    for footprint in _children(native, "footprint"):
        for pad in _children(footprint, "pad"):
            _, bounds = _pad_geometry(footprint, pad)
            assert bounds[0] < bounds[2] and bounds[1] < bounds[3]


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_isolated_native_boards_are_materialized_and_reproducible(
    case: RFCase,
    matrix_directory: Path,
) -> None:
    """Isolated coverage remains openable in temporary output, not 28 tracked boards."""
    source = case.board_text()
    path = matrix_directory / f"{case.name}.kicad_pcb"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == source
    root = _native_tree(source)
    layers = _field(root, "layers")
    assert tuple(item[1] for item in layers if item[1].endswith(".Cu")) == COPPER_LAYERS
    native_tracks = {
        _field(item, "uuid")[0]: item for item in _children(root, "segment")
    }
    for trace in case.traces():
        native = native_tracks[trace.trace_id]
        assert _field(native, "layer") == [trace.layer]
        assert int(Decimal(_field(native, "width")[0]) * 1_000_000) == trace.width_nm
        assert _native_point(native, "start") == trace.points[0]
        assert _native_point(native, "end") == trace.points[-1]


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_signal_vias_join_real_endpoints_without_joining_export_rows(
    case: RFCase,
) -> None:
    """Core and USB-launch transitions connect real copper; controlled cores stay symmetric."""
    signal_vias = [via for via in case.vias() if via.role == "signal"]
    traces = case.traces()
    nets = set(case.net_names)
    assert {trace.net for trace in traces} == nets
    for via in signal_vias:
        connected_layers = {
            layer
            for layer in via.layers
            if via.point in _endpoints(traces, layer, via.net)
        }
        assert len(connected_layers) >= 2, (case.name, via)
    for before, after in zip(case.legs, case.legs[1:]):
        assert before.width_nm != after.width_nm
        connections = []
        for net in nets:
            matching_vias = [
                via
                for via in signal_vias
                if via.net == net
                and via.point in _endpoints(traces, before.layer, net)
                and via.point in _endpoints(traces, after.layer, net)
            ]
            assert len(matching_vias) == 1
            assert {before.layer, after.layer} <= set(matching_vias[0].layers)
            connections.extend(matching_vias)
        if case.paired:
            assert connections[0].x == connections[1].x
            assert abs(connections[0].y - connections[1].y) == 900000
            assert (connections[0].y + connections[1].y) // 2 == before.points[-1][1]
    if case.paired:
        assert signal_vias, "Both USB data nets need real connector layer transitions."
        for leg in case.legs:
            left, right = leg.points[0][0], leg.points[-1][0]
            core = tuple(
                trace
                for trace in traces
                if trace.layer == leg.layer
                and trace.width_nm == leg.width_nm
                and all(left <= point[0] <= right for point in trace.points)
            )
            by_net = {
                net: tuple(trace for trace in core if trace.net == net) for net in nets
            }
            lengths = [_length(parts) for parts in by_net.values()]
            assert all(by_net.values())
            assert lengths[0] == pytest.approx(lengths[1], abs=1)
            center_y = leg.points[0][1]
            minus = {
                tuple((x, 2 * center_y - y) for x, y in trace.points)
                for trace in by_net["USB_D-"]
            }
            assert minus == {trace.points for trace in by_net["USB_D+"]}
            coupled = [
                trace
                for trace in core
                if trace.points[0][1] == trace.points[-1][1]
                and abs(trace.points[0][1] - center_y) == (leg.width_nm + 203200) // 2
            ]
            assert {trace.net for trace in coupled} == nets
            assert leg.spacing_nm == 203200


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_matrix_workbook_contains_actual_layer_specific_row_values(
    case: RFCase, tmp_path: Path
) -> None:
    """Inspect real OOXML cells rather than trusting fixture metadata or a fake writer."""
    _, plan = _prepare(case)
    image = tmp_path / "geometry-placeholder.png"
    image.write_bytes(_png())
    rows = service.report_rows(
        plan, [service.CapturedImage.load(image)] * len(plan.sections)
    )
    output = workbook.write_workbook(rows, tmp_path / "impedance.xlsx")
    with ZipFile(output) as archive:
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))  # noqa: S314 -- Locally generated fixture output.
        assert len(
            [name for name in archive.namelist() if name.startswith("xl/media/")]
        ) == len(plan.sections)
    assert len(sheet.findall("s:sheetData/s:row", _SHEET_NS)) == len(plan.sections) + 1
    for index, (section, row) in enumerate(zip(plan.sections, rows), 2):
        leg = next(
            leg
            for leg in case.signal_profiles
            if leg.layer == section.layer and leg.width_nm == section.width_nm
        )
        signal = f"L{COPPER_LAYERS.index(leg.layer) + 1}"
        references = tuple(
            f"L{COPPER_LAYERS.index(layer) + 1}" for layer in leg.reference_layers
        )
        assert row.physical_signal_layer == _cell_text(sheet, f"A{index}") == signal
        assert row.physical_reference_layers == references
        assert _cell_text(sheet, f"B{index}") == ", ".join(references)
        assert Decimal(_cell_text(sheet, f"D{index}")) == pytest.approx(
            Decimal(leg.width_nm) / 25_400
        )
        assert Decimal(_cell_text(sheet, f"G{index}")) == Decimal(case.target_ohms)
        assert row.spacing_nm == leg.spacing_nm
        assert row.ground_gap_nm == leg.ground_gap_nm
        spacing = _cell_text(sheet, f"F{index}")
        if case.paired and case.coplanar:
            pair, ground = spacing.splitlines()
            assert pair.startswith("Pair: ") and ground.startswith("Ground: ")
            assert Decimal(pair.removeprefix("Pair: ")) == pytest.approx(
                Decimal(leg.spacing_nm) / 25_400
            )
            assert Decimal(ground.removeprefix("Ground: ")) == pytest.approx(
                Decimal(leg.ground_gap_nm) / 25_400
            )
        elif case.paired or case.coplanar:
            dimension = leg.spacing_nm if case.paired else leg.ground_gap_nm
            assert Decimal(spacing) == pytest.approx(Decimal(dimension) / 25_400)
        else:
            assert spacing == ""


def test_fence_design_bound_is_explicit_for_two_ghz() -> None:
    """Check the documented conservative dielectric wavelength criterion, not RF impedance."""
    assert FREQUENCY_HZ == 2_000_000_000
    assert DK > 1
    wavelength_nm = 299_792_458 / FREQUENCY_HZ / sqrt(DK) * 1_000_000_000
    assert 0 < FENCE_PITCH_NM <= wavelength_nm / 80


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_native_vias_carry_real_nets_and_copper_layer_spans(case: RFCase) -> None:
    """Ensure signal transitions and ground stitching are native drillable PCB vias."""
    native = _native_tree(case.board_text())
    nets = {item[1]: item[2] for item in _children(native, "net")}
    records = {
        (_native_point(item, "at"), nets[_field(item, "net")[0]]): item
        for item in _children(native, "via")
    }
    vias = case.vias()
    assert len(records) == len(vias) == len(_children(native, "via"))
    assert all(via.drill_nm < via.diameter_nm for via in vias)
    for via in vias:
        record = records[(via.point, via.net)]
        assert int(Decimal(_field(record, "size")[0]) * 1_000_000) == via.diameter_nm
        assert int(Decimal(_field(record, "drill")[0]) * 1_000_000) == via.drill_nm
        first, last = _field(record, "layers")
        crossed = COPPER_LAYERS[
            COPPER_LAYERS.index(first) : COPPER_LAYERS.index(last) + 1
        ]
        assert crossed == via.layers
        if via.role != "signal":
            assert via.net == "GND"
            assert via.layers == COPPER_LAYERS


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_native_launch_pads_have_real_clearance_and_connected_track_endpoints(
    case: RFCase,
) -> None:
    """Both real USB sockets connect reversible data pads; unlike nets keep physical clearance."""
    native = _native_tree(case.board_text())
    identifier = (
        USB if case.paired else SMA if case.length_mm > 2 else "RFContext:Testpoint"
    )
    launches = [
        item for item in _children(native, "footprint") if item[1] == identifier
    ]
    assert len(launches) == 2
    assert not case.paired or not any(
        item[1] == SMA for item in _children(native, "footprint")
    )
    pads = []
    for launch in launches:
        signal_contacts: Counter[str] = Counter()
        for pad in _children(launch, "pad"):
            assigned = _children(pad, "net")
            if not assigned:
                continue
            net = assigned[0][2]
            point, bounds = _pad_geometry(launch, pad)
            radius = (bounds[2] - bounds[0]) / 2 if pad[3] == "circle" else None
            pads.append((point, bounds, radius, net))
            if net in case.net_names:
                signal_contacts[net] += 1
                assert any(
                    point in _endpoints(case.traces(), layer, net)
                    for layer in _SIGNAL_LAYERS
                )
        assert signal_contacts == Counter(
            dict.fromkeys(case.net_names, 2 if case.paired else 1)
        )
    for (first, bounds_a, radius_a, net_a), (
        second,
        bounds_b,
        radius_b,
        net_b,
    ) in combinations(pads, 2):
        if net_a == net_b:
            continue
        if radius_a is not None and radius_b is not None:
            clearance = (
                hypot(second[0] - first[0], second[1] - first[1]) - radius_a - radius_b
            )
        else:
            clearance = _bounds_distance(bounds_a, bounds_b)
        assert clearance >= 200000 - 1, (case.name, net_a, net_b, clearance)
    for point, bounds, radius, net in pads:
        for via in case.vias():
            if via.net == net:
                continue
            distance = (
                hypot(via.x - point[0], via.y - point[1]) - radius
                if radius is not None
                else _bounds_distance(bounds, (via.x, via.y, via.x, via.y))
            )
            assert distance - via.diameter_nm / 2 >= 200000 - 1, (
                case.name,
                net,
                point,
                via,
            )


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_coplanar_fences_have_both_sides_and_bounded_physical_pitch(
    case: RFCase,
) -> None:
    """Measure populated ground-via rows, including return vias that share the fence."""
    fences = [via for via in case.vias() if via.fence_row is not None]
    if not case.coplanar:
        assert not fences
        return
    assert fences
    rows: dict[tuple[int, int], list[Any]] = {}
    for via in fences:
        assert via.net == "GND" and via.role != "signal"
        assert via.fence_side in (-1, 1)
        assert (via.y - case.legs[0].points[0][1]) * via.fence_side > 0
        rows.setdefault((via.fence_row, via.fence_side), []).append(via)
    assert {side for _, side in rows} == {-1, 1}
    for row_id in {row_id for row_id, _ in rows}:
        assert {(row_id, -1), (row_id, 1)} <= set(rows)
    for row in rows.values():
        points = sorted({via.point for via in row})
        assert len(points) >= 2
        assert points[0][0] <= case.legs[0].points[0][0] + FENCE_PITCH_NM // 2
        assert points[-1][0] >= case.legs[-1].points[-1][0] - FENCE_PITCH_NM
        max_pitch = 299_792_458 / FREQUENCY_HZ / sqrt(DK) * 1_000_000_000 / 80
        assert all(
            0 < end[0] - start[0] <= FENCE_PITCH_NM
            and hypot(end[0] - start[0], end[1] - start[1]) <= max_pitch
            for start, end in zip(points, points[1:])
        )


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_all_copper_layers_have_ground_zones_and_only_cpwg_uses_keepouts(
    case: RFCase,
) -> None:
    """Keep every GND pour present without hiding custom rules behind keepouts."""
    native = _native_tree(case.board_text())
    native_zones = _children(native, "zone")
    keepout_nodes = [item for item in native_zones if _children(item, "keepout")]
    copper_nodes = [item for item in native_zones if not _children(item, "keepout")]
    keepouts = case.keepouts()
    zones = case.zones()
    assert len(keepout_nodes) == len(keepouts)
    assert len(keepouts) >= len(case.legs) if case.coplanar else not keepouts
    assert len(copper_nodes) == len(zones)
    assert {_field(node, "layer")[0] for node in copper_nodes} == set(COPPER_LAYERS)
    assert all(_field(node, "net_name") == ["GND"] for node in copper_nodes)
    for node in copper_nodes:
        # KiCad's native `yes` means solid pad/via connection; `full` is not
        # accepted by KiCad 10's PCB parser despite its semantic meaning.
        connection = _field(node, "connect_pads")
        assert connection[0] == "yes"
        assert Decimal(
            _field(_children(node, "connect_pads")[0], "clearance")[0]
        ) == Decimal("0.2")
    assert Counter(_field(node, "layer")[0] for node in keepout_nodes) == Counter(
        keepout.layer for keepout in keepouts
    )
    assert {keepout.layer for keepout in keepouts} == (
        {leg.layer for leg in case.legs} if case.coplanar else set()
    )
    for keepout in keepouts:
        candidates = [
            node
            for node in keepout_nodes
            if _field(node, "layer") == [keepout.layer]
            and _zone_polygon(node) == keepout.polygon
        ]
        assert len(candidates) == 1
        definition = _children(candidates[0], "keepout")[0]
        assert _field(definition, "copperpour") == ["not_allowed"]
        for field in ("tracks", "vias", "pads", "footprints"):
            assert _field(definition, field) == ["allowed"]
        assert keepout.clearance_nm > 0
        assert keepout.purpose == "coplanar-gap"
        leg = next(leg for leg in case.legs if leg.layer == keepout.layer)
        assert keepout.clearance_nm == leg.ground_gap_nm
    reference_zones = [zone for zone in zones if zone.reference]
    assert {zone.layer for zone in reference_zones} == {"In1.Cu", "In3.Cu", "In4.Cu"}
    assert all(zone.net == "GND" and not zone.keepouts for zone in reference_zones)
    for zone in reference_zones:
        matches = [
            node for node in copper_nodes if _field(node, "layer") == [zone.layer]
        ]
        assert len(matches) == 1
        assert _field(matches[0], "net_name") == ["GND"]
        assert not _children(matches[0], "keepout")
    # Filling belongs to KiCad; generated input must not pretend to contain a
    # solved fill by drawing ground rectangles or copying a fabricated cache.
    assert not any(_children(node, "filled_polygon") for node in native_zones)


def test_clearance_predicate_rejects_a_notch_between_inside_route_endpoints() -> None:
    """Check every segment interior even when both endpoints have ample clearance."""
    route = (Trace("crossing", "F.Cu", "RF", 100, ((200, 500), (800, 500))),)
    notch = (
        (0, 0),
        (1000, 0),
        (1000, 1000),
        (600, 1000),
        (600, 450),
        (400, 450),
        (400, 1000),
        (0, 1000),
    )
    assert all(_inside_polygon(point, notch) for point in route[0].points)
    assert not _contains_route_clearance(notch, route, 50)


@pytest.mark.parametrize(
    "case", [case for case in RF_CASES if case.paired], ids=lambda case: case.name
)
def test_usb_matrix_tracks_and_vias_clear_opposite_net_copper(case: RFCase) -> None:
    """The unsolved USB launch still obeys ordinary 0.2 mm copper clearance everywhere."""
    traces = case.traces()
    for first, second in combinations(traces, 2):
        if first.layer != second.layer or first.net == second.net:
            continue
        a, b = first.points
        c, d = second.points
        assert not _segments_intersect(a, b, c, d), (case.name, first, second)
        diameter = first.width_nm + second.width_nm + 400000
        assert all(
            _point_has_clearance(point, start, end, diameter)
            for point, start, end in ((a, c, d), (b, c, d), (c, a, b), (d, a, b))
        ), (case.name, first, second)
    for via in case.vias():
        for trace in traces:
            if via.net == trace.net or trace.layer not in via.layers:
                continue
            assert _point_has_clearance(
                via.point,
                trace.points[0],
                trace.points[1],
                via.diameter_nm + trace.width_nm + 400000,
            ), (case.name, via, trace)


@pytest.mark.parametrize(
    "case", [case for case in RF_CASES if case.paired], ids=lambda case: case.name
)
def test_usb_matrix_reversible_contacts_and_all_tracks_form_connected_nets(
    case: RFCase,
) -> None:
    """Every physical data contact reaches its mate socket without disconnected launch islands."""
    native = _native_tree(case.board_text())
    sockets = [item for item in _children(native, "footprint") if item[1] == USB]
    for net in case.net_names:
        traces = tuple(trace for trace in case.traces() if trace.net == net)
        pads = {
            ("F.Cu", _placed_point(socket, _native_point(pad, "at")))
            for socket in sockets
            for pad in _children(socket, "pad")
            if _children(pad, "net") and _field(pad, "net")[1] == net
        }
        assert len(pads) == 4
        nodes = pads | {
            (trace.layer, point) for trace in traces for point in trace.points
        }
        graph: dict[tuple[str, Point], set[tuple[str, Point]]] = defaultdict(set)
        for trace in traces:
            on_copper = {
                node
                for node in nodes
                if node[0] == trace.layer
                and _on_segment(node[1], trace.points[0], trace.points[1])
            }
            for node in on_copper:
                graph[node].update(on_copper - {node})
        for via in case.vias():
            if via.net != net:
                continue
            on_via = {
                node for node in nodes if node[1] == via.point and node[0] in via.layers
            }
            for node in on_via:
                graph[node].update(on_via - {node})
        pending = [next(iter(pads))]
        visited = set()
        while pending:
            node = pending.pop()
            if node not in visited:
                visited.add(node)
                pending.extend(graph[node] - visited)
        assert nodes == visited, (case.name, net, nodes - visited)


@pytest.mark.parametrize(
    "mutation",
    ["trace_width", "pair_gap", "clearance", "class", "assignment", "suppressed_drc"],
)
def test_project_semantic_check_rejects_electrical_constraint_drift(
    mutation: str,
) -> None:
    """Allowing KiCad serialization differences must not hide changed engineering intent."""
    expected = json.loads(fixture_generator.combined_project_text(COMBINED_BOARDS[1]))
    actual = deepcopy(expected)
    classes = actual["net_settings"]["classes"]
    if mutation == "trace_width":
        classes[1]["track_width"] += 0.01
    elif mutation == "pair_gap":
        classes[1]["diff_pair_gap"] = 0.2
    elif mutation == "clearance":
        actual["board"]["design_settings"]["rules"]["min_clearance"] = 0.1
    elif mutation == "class":
        classes.pop()
    elif mutation == "assignment":
        actual["net_settings"]["netclass_patterns"][0]["netclass"] = "Default"
    else:
        actual["board"]["design_settings"]["drc_exclusions"] = ["hidden-error"]
    with pytest.raises(AssertionError):
        _assert_project_intent(actual, expected)


@pytest.mark.parametrize("notch_y, expected", [(600, True), (599, False)])
def test_clearance_predicate_checks_margin_at_segment_interior_corners(
    notch_y: int, expected: bool
) -> None:
    """Detect a one-nanometre copper-envelope intrusion without a centerline crossing."""
    route = (Trace("near-corner", "F.Cu", "RF", 100, ((200, 500), (800, 500))),)
    notch = (
        (0, 0),
        (1000, 0),
        (1000, 1000),
        (600, 1000),
        (600, notch_y),
        (400, notch_y),
        (400, 1000),
        (0, 1000),
    )
    assert all(_inside_polygon(point, notch) for point in route[0].points)
    assert _contains_route_clearance(notch, route, 50) is expected
    assert _contains_route_clearance(tuple(reversed(notch)), route, 50) is expected


@pytest.mark.parametrize(
    "width_nm, left_edge, expected", [(100, 0, True), (101, 0, False), (100, 1, False)]
)
def test_clearance_predicate_preserves_round_endcaps_and_half_nanometre_radii(
    width_nm: int, left_edge: int, expected: bool
) -> None:
    """Include copper beyond endpoints without rounding odd track widths down."""
    polygon = ((left_edge, 0), (1000, 0), (1000, 1000), (left_edge, 1000))
    route = (Trace("round-end", "F.Cu", "RF", width_nm, ((100, 500), (900, 500))),)
    assert _contains_route_clearance(polygon, route, 50) is expected


@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if len(case.legs) == 3],
    ids=lambda case: case.name,
)
def test_native_electrical_transitions_have_nearby_symmetric_ground_returns(
    case: RFCase,
) -> None:
    """Require return-current continuity in native copper on both sides of each transition."""
    source = case.board_text()
    assert _has_native_transition_returns(case, _native_tree(source))


@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if len(case.legs) == 3 and not case.coplanar],
    ids=lambda case: case.name,
)
def test_removing_noncoplanar_transition_returns_is_rejected(case: RFCase) -> None:
    """Reject signal-only transitions even when unrelated ground stitching remains."""
    native = _native_tree(case.board_text())
    returns = {via.point for via in case.vias() if via.role == "return"}
    stripped = [
        item
        for item in native
        if not (
            isinstance(item, list)
            and item[0] == "via"
            and _native_point(item, "at") in returns
        )
    ]
    assert _has_native_transition_returns(case, native)
    assert not _has_native_transition_returns(case, stripped)


@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if len(case.legs) == 3],
    ids=lambda case: case.name,
)
@pytest.mark.parametrize(
    "mutation",
    ["wrong_net", "missing_reference_layers", "too_far", "one_side_only", "asymmetric"],
)
def test_native_transition_return_geometry_mutations_are_rejected(
    case: RFCase, mutation: str
) -> None:
    """Require actual nearby GND connectivity and symmetry, not fixture role labels."""
    native = _native_tree(case.board_text())
    assert _has_native_transition_returns(case, native)
    net_ids = {item[2]: item[1] for item in _children(native, "net")}
    center_y_mm = Decimal(case.legs[0].points[0][1]) / 1_000_000
    for via in tuple(_children(native, "via")):
        if _field(via, "net") != [net_ids["GND"]]:
            continue
        position = _children(via, "at")[0]
        y = Decimal(position[2])
        if mutation == "wrong_net":
            _children(via, "net")[0][1] = net_ids[case.net_names[0]]
        elif mutation == "missing_reference_layers":
            _children(via, "layers")[0][2] = "In1.Cu"
        elif mutation == "too_far":
            position[2] = str(y + 10)
        elif mutation == "one_side_only" and y > center_y_mm:
            native.remove(via)
        elif mutation == "asymmetric" and y > center_y_mm:
            position[2] = str(y + Decimal("0.001"))
    assert not _has_native_transition_returns(case, native)


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_reference_zones_underlie_every_route_and_stackup_is_physically_explicit(
    case: RFCase,
) -> None:
    """Keep actual native dielectric geometry and continuous reference-plane polygons."""
    root = _native_tree(case.board_text())
    setup = _children(root, "setup")[0]
    stackup = _children(setup, "stackup")[0]
    native_layers = _children(stackup, "layer")
    assert (
        tuple(layer[1] for layer in native_layers if layer[1].endswith(".Cu"))
        == COPPER_LAYERS
    )
    thicknesses = [Decimal(_field(layer, "thickness")[0]) for layer in native_layers]
    assert sum(thicknesses) == Decimal(
        _field(_children(root, "general")[0], "thickness")[0]
    )
    dielectric_layers = [
        layer for layer in native_layers if not layer[1].endswith(".Cu")
    ]
    assert len(dielectric_layers) == 5
    assert all(
        Decimal(_field(layer, "epsilon_r")[0]) == Decimal(str(DK))
        for layer in dielectric_layers
    )
    for leg in case.legs:
        for reference in leg.reference_layers:
            zone = next(zone for zone in case.zones() if zone.layer == reference)
            left, top, right, bottom = zone.bounds
            assert zone.reference and not zone.keepouts
            assert all(left < x < right and top < y < bottom for x, y in leg.points)


def test_complete_committed_fixture_set_and_manifest_match_generator() -> None:
    """Only two combined human-review projects are checked in; all circuits remain described."""
    files = fixture_generator.fixture_files()
    assert {name for name in files if name.endswith(".kicad_pcb")} == {
        "single-ended-50-ohm.kicad_pcb",
        "usb-differential-90-ohm.kicad_pcb",
    }
    for name, expected in files.items():
        actual = (_EXAMPLE_DIRECTORY / name).read_text(encoding="utf-8")
        if name.endswith(".kicad_pro"):
            _assert_project_intent(json.loads(actual), json.loads(expected))
        else:
            assert actual == expected, name
    records = json.loads(
        (_EXAMPLE_DIRECTORY / "manifest.json").read_text(encoding="utf-8")
    )
    assert len(records) == len(COMBINED_BOARDS) == 2
    for record, board in zip(records, COMBINED_BOARDS):
        assert record["name"] == board.name
        assert record["target_ohms"] == ("90" if board.paired else "50")
        assert (
            record["sha256"]
            == hashlib.sha256(
                (_EXAMPLE_DIRECTORY / record["board"]).read_bytes()
            ).hexdigest()
        )
        assert record["circuit_count"] == len(board.circuits) == 14
        assert {item["name"] for item in record["circuits"]} == {
            case.name for case in RF_CASES if case.paired == board.paired
        }
        for item in record["circuits"]:
            assert item["launch_count"] == 2
            assert (
                item["launch_type"] == (USB if board.paired else SMA).split(":", 1)[1]
            )
            for profile in item["legs"]:
                assert profile["reference_layers"] == list(
                    _REFERENCES[profile["signal_layer"]]
                )
                if board.paired:
                    assert Decimal(str(profile["pair_edge_gap_mm"])) == Decimal(
                        "0.2032"
                    )
    # The generator deliberately compares bytes; native KiCad saved the projects
    # with expanded defaults. These are the only permissible serialized changes.
    assert {
        path.name for path in fixture_generator.generate(_EXAMPLE_DIRECTORY, check=True)
    } <= {board.name + ".kicad_pro" for board in COMBINED_BOARDS}


def test_temporary_matrix_manifest_tracks_actual_profiles_and_core_lengths(
    matrix_directory: Path,
) -> None:
    """Separate advertised short cores from the full USB connector-launch geometry."""
    records = json.loads(
        (matrix_directory / "manifest.json").read_text(encoding="utf-8")
    )
    by_name = {case.name: case for case in RF_CASES}
    assert {record["name"] for record in records} == set(by_name)
    for record in records:
        case = by_name[record["name"]]
        assert record["nominal_route_length_mm_per_net"] == case.length_mm
        assert record["launch_type"] == (
            USB.split(":", 1)[1]
            if case.paired
            else SMA.split(":", 1)[1]
            if case.length_mm > 2
            else "compact_testpoint_stress"
        )
        assert record["launch_count"] == 2
        assert record["actual_route_length_mm_per_net"] == {
            net: _length(tuple(trace for trace in case.traces() if trace.net == net))
            / 1000000
            for net in case.net_names
        }
        profiles = {(trace.layer, trace.width_nm) for trace in case.traces()}
        assert record["expected_reviewed_rows"] == len(profiles)
        assert record["expected_raw_sections"] == len(profiles)
        assert {
            (item["signal_layer"], round(item["width_mm"] * 1000000))
            for item in record["legs"]
        } == profiles
        assert len(record["controlled_core_legs"]) == len(case.legs)
    assert (
        fixture_generator.generate(matrix_directory, check=True, legacy_matrix=True)
        == ()
    )


def test_generator_includes_separate_connected_connector_showcases() -> None:
    """Real connectors supplement, rather than displace, 2 mm matrix fixtures."""
    files = fixture_generator.fixture_files(legacy_matrix=True)
    assert {
        f"showcases/{name}{suffix}"
        for name in ("sma-single-ended-50-ohm", "usb-differential-cpwg-90-ohm")
        for suffix in (".kicad_pcb", ".kicad_pro")
    } <= files.keys()
    assert "showcases/README.md" in files
    assert len(RF_CASES) == 28


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_native_project_constraints_enable_the_fixture_without_hiding_drc_errors(
    case: RFCase,
) -> None:
    """Make narrow inner traces inspectable without relying on user-global defaults."""
    project = json.loads(fixture_generator.project_text(case))
    settings = project["board"]["design_settings"]
    assert settings["drc_exclusions"] == []
    rules = settings["rules"]
    assert rules["min_track_width"] == 0.1
    assert rules["min_clearance"] == 0.2
    assert rules["min_through_hole_diameter"] == 0.3
    assert rules["min_via_diameter"] == 0.6
    assert rules["min_via_annular_width"] == 0.15
    assert (
        min(leg.width_nm for leg in case.legs) >= rules["min_track_width"] * 1_000_000
    )
    assert project["net_settings"]["classes"][0]["clearance"] == 0.2


def test_generator_check_does_not_create_missing_directory(tmp_path: Path) -> None:
    """Keep check mode strictly read-only even when all expected artifacts are absent."""
    missing = tmp_path / "not-created"
    changed = fixture_generator.generate(missing, check=True)
    assert {path.relative_to(missing).as_posix() for path in changed} == set(
        fixture_generator.fixture_files()
    )
    assert all(path.is_relative_to(missing) for path in changed)
    assert not missing.exists()


@pytest.mark.parametrize("nested", (False, True), ids=("matrix", "showcases"))
def test_generator_check_reports_missing_and_stale_files_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], nested: bool
) -> None:
    """Never repair changed boards, create missing files, or touch timestamps in check mode."""
    directory = tmp_path / "boards"
    fixture_generator.generate(directory, legacy_matrix=True)
    stale_name = (
        "showcases/sma-single-ended-50-ohm.kicad_pcb"
        if nested
        else f"{RF_CASES[0].name}.kicad_pcb"
    )
    missing_name = (
        "showcases/usb-differential-cpwg-90-ohm.kicad_pro"
        if nested
        else f"{RF_CASES[1].name}.kicad_pro"
    )
    stale = directory / stale_name
    missing = directory / missing_name
    stale.write_text("user inspection edits\n", encoding="utf-8")
    missing.unlink()
    before = {
        path.relative_to(directory).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert set(
        fixture_generator.generate(directory, check=True, legacy_matrix=True)
    ) == {stale, missing}
    assert (
        fixture_generator.main(
            ["--output", str(directory), "--legacy-matrix", "--check"]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert str(stale) in captured.err and str(missing) in captured.err
    assert not captured.out
    after = {
        path.relative_to(directory).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not missing.exists()


def test_generator_materializes_owned_artifacts_and_preserves_unrelated_files(
    tmp_path: Path,
) -> None:
    """Regenerate the explicit target set without deleting or editing nearby user data."""
    unrelated = tmp_path / "my-inspection.kicad_pcb"
    unrelated.write_bytes(b"user-owned PCB inspection data\x00")
    notes = tmp_path / "notes.txt"
    notes.write_text("Keep these notes.\n", encoding="utf-8")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (unrelated, notes)
    }
    assert fixture_generator.generate(tmp_path) == ()
    assert fixture_generator.generate(tmp_path, check=True) == ()
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (unrelated, notes)
    } == before
    assert {
        name: (tmp_path / name).read_text(encoding="utf-8")
        for name in fixture_generator.fixture_files()
    } == fixture_generator.fixture_files()
