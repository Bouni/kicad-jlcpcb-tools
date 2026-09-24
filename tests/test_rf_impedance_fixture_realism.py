"""Keep presentation fixtures realistic without deleting geometry stress tests."""

from collections import Counter
from dataclasses import replace
import json
from math import hypot
import os
from pathlib import Path
import shutil
from typing import Optional, Union

import pytest

from impedance.matching import analyze
from impedance.model import Config, Trace
from impedance.render import Viewport
from tests.rf_impedance_combined import COMBINED_BOARDS, CombinedBoard, CombinedCircuit
from tests.rf_impedance_fixtures import RF_CASES, RFCase
from tests.test_rf_impedance_native import (
    _children,
    _field,
    _parse_board,
    _run_native,
    _track_geometry,
)


@pytest.mark.parametrize(
    "case", [case for case in RF_CASES if case.paired], ids=lambda case: case.name
)
def test_differential_controlled_routes_use_exact_eight_mil_spacing(
    case: RFCase,
) -> None:
    """Eight mil is 203200 nm on every layer, not rounded to 0.2 mm."""
    from scripts.generate_rf_impedance_fixtures import project_text

    assert {leg.spacing_nm for leg in case.legs} == {203_200}
    for leg in case.legs:
        center_y = leg.points[0][1]
        members = [
            trace
            for trace in case.traces()
            if trace.layer == leg.layer
            and trace.points[0][1] == trace.points[-1][1]
            and abs(trace.points[0][1] - center_y) < 400_000
            and leg.points[0][0] <= trace.points[0][0]
            and trace.points[-1][0] <= leg.points[-1][0]
            and trace.width_nm == leg.width_nm
        ]
        assert len(members) == 2
        assert (
            abs(members[0].points[0][1] - members[1].points[0][1]) - leg.width_nm
            == 203_200
        )
    project = json.loads(project_text(case))
    rf_class = next(
        item
        for item in project["net_settings"]["classes"]
        if item["name"] == case.net_class
    )
    assert rf_class["diff_pair_gap"] == 0.2032


@pytest.mark.parametrize(
    "case", [case for case in RF_CASES if case.paired], ids=lambda case: case.name
)
def test_usb_matrix_connects_both_reversible_contacts_of_two_real_connectors(
    case: RFCase,
) -> None:
    """Both physical D+/D- contacts reach both sockets through tracks and vias."""
    from tests.rf_impedance_showcases import USB
    from tests.test_rf_impedance_showcases import _pads

    board = _parse_board(case.board_text())
    connectors = [item for item in _children(board, "footprint") if item[1] == USB]
    assert len(connectors) == 2
    assert not any(
        item[1]
        in ("RFContext:Testpoint", "Connector_Coaxial:SMA_Amphenol_132134_Vertical")
        for item in _children(board, "footprint")
    )
    expected = {"A6": "USB_D+", "B6": "USB_D+", "A7": "USB_D-", "B7": "USB_D-"}
    for connector in connectors:
        assert {
            number: net for number, net, _ in _pads(connector) if number in expected
        } == expected
    for net in case.net_names:
        graph: dict[tuple[str, tuple[int, int]], set[tuple[str, tuple[int, int]]]] = {}
        for trace in case.traces():
            if trace.net == net:
                first, second = (
                    (trace.layer, trace.points[0]),
                    (trace.layer, trace.points[-1]),
                )
                graph.setdefault(first, set()).add(second)
                graph.setdefault(second, set()).add(first)
        for via in case.vias():
            if via.net == net:
                connected = {node for node in graph if node[1] == via.point}
                assert len(connected) >= 2, "A signal via must genuinely switch layers"
                for node in connected:
                    graph[node].update(connected - {node})
        contacts = {
            ("F.Cu", point)
            for connector in connectors
            for number, pad_net, point in _pads(connector)
            if number in expected and pad_net == net
        }
        assert len(contacts) == 4 and contacts <= graph.keys()
        reached, pending = set(), [next(iter(contacts))]
        while pending:
            node = pending.pop()
            if node not in reached:
                reached.add(node)
                pending.extend(graph[node] - reached)
        assert reached == graph.keys(), "No declared USB copper or pad may dangle"


@pytest.mark.parametrize(
    "case", [case for case in RF_CASES if not case.paired], ids=lambda case: case.name
)
def test_long_matrix_routes_have_real_connected_sma_endpoints(case: RFCase) -> None:
    """Long routes need real connectors; retain tiny endpoint stress fixtures."""
    board = _parse_board(case.board_text())
    footprints = _children(board, "footprint")
    connectors = [
        item
        for item in footprints
        if item[1] == "Connector_Coaxial:SMA_Amphenol_132134_Vertical"
    ]
    testpoints = [item for item in footprints if item[1] == "RFContext:Testpoint"]
    count = 2
    if case.length_mm == 2:
        assert not connectors
        assert len(testpoints) == count
        return
    assert len(connectors) == count
    assert not testpoints
    signal_ends: dict[str, list[tuple[int, int]]] = {}
    centres = []
    for connector in connectors:
        x, y = (
            round(float(str(value)) * 1_000_000)
            for value in _field(connector, "at")[:2]
        )
        centres.append((x, y))
        pads = _children(connector, "pad")
        assert len(pads) == 5
        assert len(_children(connector, "fp_line")) >= 8
        assert Counter(str(pad[1]) for pad in pads) == {"1": 1, "2": 4}
        for pad in pads:
            net = str(_field(pad, "net")[-1])
            if pad[1] == "2":
                assert net == "GND"
                continue
            assert net in case.net_names
            assert _field(pad, "at")[:2] == ["0", "0"]
            signal_ends.setdefault(net, []).append((x, y))
    for index, first in enumerate(centres):
        for second in centres[index + 1 :]:
            assert (
                abs(first[0] - second[0]) >= 8_340_000
                or abs(first[1] - second[1]) >= 8_340_000
            )
    lengths = []
    for net, endpoints in signal_ends.items():
        tracks = [trace for trace in case.traces() if trace.net == net]
        degrees = Counter(point for trace in tracks for point in trace.points)
        assert {point for point, degree in degrees.items() if degree == 1} == set(
            endpoints
        )
        assert len(endpoints) == 2
        lengths.append(
            sum(
                hypot(
                    trace.points[-1][0] - trace.points[0][0],
                    trace.points[-1][1] - trace.points[0][1],
                )
                for trace in tracks
            )
        )
    assert all(length > case.length_mm * 1_000_000 for length in lengths)
    if case.paired:
        assert lengths[0] == pytest.approx(lengths[1], abs=1)


@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if case.paired and case.length_mm == 120],
    ids=lambda case: case.name,
)
def test_usb_launches_do_not_shorten_the_120mm_coupled_region(case: RFCase) -> None:
    """Move connector fanouts outside the named stress span, not into it."""
    leg = case.legs[0]
    for net in case.net_names:
        assert any(
            trace.net == net
            and trace.points[0][0] == leg.points[0][0]
            and trace.points[-1][0] == leg.points[-1][0]
            and trace.points[0][1] == trace.points[-1][1]
            for trace in case.traces()
        )


@pytest.mark.native_kicad
@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if case.length_mm > 2],
    ids=lambda case: case.name,
)
def test_native_matrix_launches_pass_real_fill_and_connectivity(
    case: RFCase, tmp_path: Path
) -> None:
    """Check current generated source through KiCad, independently of stored copies."""
    from scripts.generate_rf_impedance_fixtures import custom_rules_text, project_text

    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Set KICAD_CLI to enable native connector launch checks")
    path = tmp_path / f"{case.name}.kicad_pcb"
    path.write_text(case.board_text(), encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(project_text(case), encoding="utf-8")
    rules = custom_rules_text(case)
    if rules is not None:
        path.with_suffix(".kicad_dru").write_text(rules, encoding="utf-8")
    report = tmp_path / "drc.json"
    _run_native(
        [
            executable,
            "pcb",
            "drc",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--output",
            str(report),
            str(path),
        ]
    )
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["unconnected_items"] == [], json.dumps(
        result["unconnected_items"], indent=2
    )
    context_ids = {
        identifier
        for identifier, track in _track_geometry(
            _parse_board(case.board_text())
        ).items()
        if track[-1].startswith("CONTEXT")
    }
    unexpected = [
        item
        for item in result["violations"]
        if not (
            item["severity"] == "warning"
            and (
                item["type"] == "lib_footprint_issues"
                or (
                    item["type"] == "track_dangling"
                    and item["items"]
                    and all(record["uuid"] in context_ids for record in item["items"])
                )
            )
        )
    ]
    assert unexpected == [], json.dumps(unexpected, indent=2)
    assert _track_geometry(
        _parse_board(path.read_text(encoding="utf-8"))
    ) == _track_geometry(_parse_board(case.board_text()))


@pytest.mark.parametrize(
    "case", [case for case in RF_CASES if case.coplanar], ids=lambda case: case.name
)
def test_fence_vias_clear_full_signal_capsules_at_connector_fanout_corners(
    case: RFCase,
) -> None:
    """A via clear of a straight core must also clear the adjacent 45-degree launch."""
    from tests.test_rf_impedance_matrix import _point_has_clearance

    for via in case.vias():
        if via.net != "GND":
            continue
        for trace in case.traces():
            assert _point_has_clearance(
                via.point,
                trace.points[0],
                trace.points[-1],
                via.diameter_nm + trace.width_nm + 400_000,
            ), (case.name, via.point, trace.points)


@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if not case.paired and case.length_mm > 2],
    ids=lambda case: case.name,
)
def test_capture_includes_whole_sma_signal_pads_and_local_connector_context(
    case: RFCase,
) -> None:
    """The 15% crop retains signal pads; connector bodies may extend outside it."""
    from impedance.render import section_viewport
    from tests.rf_impedance_fixtures import BOARD_BOUNDS
    from tests.test_rf_impedance_matrix import _native_component_bounds, _native_tree

    components = dict(_native_component_bounds(_native_tree(case.board_text())))
    member_count = 2 if case.paired else 1
    sections = analyze(
        Config(enabled=True, specifications=case.specifications()), case.snapshot()
    ).sections
    for section in sections:
        required = []
        if section.layer == case.legs[0].layer:
            required.extend(f"JRF{index + 1}" for index in range(member_count))
        if section.layer == case.legs[-1].layer:
            required.extend(
                f"JRF{member_count + index + 1}" for index in range(member_count)
            )
        view = section_viewport(section, 800, 420, board_bounds=BOARD_BOUNDS)
        assert _complete_component_visible(view, section.bounds)
        for reference in required:
            bounds = components[reference]
            connector = next(
                item
                for item in _children(_native_tree(case.board_text()), "footprint")
                if any(
                    prop[1:3] == ["Reference", reference]
                    for prop in _children(item, "property")
                )
            )
            origin = [
                round(float(value) * 1_000_000) for value in _field(connector, "at")[:2]
            ]
            pad = next(pad for pad in _children(connector, "pad") if pad[1] == "1")
            size = [round(float(value) * 1_000_000) for value in _field(pad, "size")]
            pad_bounds = (
                origin[0] - size[0] // 2,
                origin[1] - size[1] // 2,
                origin[0] + size[0] // 2,
                origin[1] + size[1] // 2,
            )
            assert _complete_component_visible(view, pad_bounds), (case.name, reference)
            if len(case.legs) == 1:
                assert _complete_component_visible(view, bounds)
            else:
                # A layer-transition row ends at a via, not the opposite SMA.
                # Its tighter crop may trim the local connector body, while
                # retaining its complete signal land and physical placement.
                assert view.left <= origin[0] <= view.left + view.width
                assert view.top <= origin[1] <= view.top + view.height
                assert min(bounds[2], view.left + view.width) > max(
                    bounds[0], view.left
                )
                assert min(bounds[3], view.top + view.height) > max(bounds[1], view.top)
            clipped = replace(view, left=bounds[0] + 1)
            assert not _complete_component_visible(clipped, bounds)


def _complete_component_visible(
    view: Viewport, bounds: tuple[int, int, int, int]
) -> bool:
    """Include actual native body/pads/reference bounds, not just pad-centre points."""
    return (
        view.left <= bounds[0]
        and view.top <= bounds[1]
        and bounds[2] <= view.left + view.width
        and bounds[3] <= view.top + view.height
    )


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_coplanar_examples_have_one_ground_fence_on_each_side(case: RFCase) -> None:
    """One row per side is the agreed showcase policy, not an RF guarantee."""
    rows = {
        (via.fence_row, via.fence_side)
        for via in case.vias()
        if via.fence_row is not None
    }
    assert rows == ({(0, -1), (0, 1)} if case.coplanar else set())


@pytest.mark.parametrize(
    "case",
    [case for case in RF_CASES if case.paired and case.coplanar],
    ids=lambda case: case.name,
)
def test_usb_coplanar_fences_continue_into_available_connector_launch_windows(
    case: RFCase,
) -> None:
    """Actual GND vias fence both sides of unobstructed USB launch straights."""
    board = _parse_board(case.board_text())
    nets = {str(item[1]): str(item[2]) for item in _children(board, "net")}
    ground = {
        tuple(round(float(value) * 1_000_000) for value in _field(via, "at")[:2])
        for via in _children(board, "via")
        if nets[str(_field(via, "net")[0])] == "GND"
    }
    # The reserved connector fanout and paired-via regions interrupt uniform
    # fencing. These clear straight windows lie between those obstructions.
    first, last = case.legs[0], case.legs[-1]
    windows = [(first.points[0][0] - 5_000_000, first.points[0][0] - 2_000_000)]
    if last.layer == "F.Cu":
        windows.append((last.points[-1][0] + 1_750_000, last.points[-1][0] + 3_250_000))
    outer = next(profile for profile in case.signal_profiles if profile.layer == "F.Cu")
    offset = (outer.width_nm + 203_200) // 2 + outer.width_nm // 2 + 650_000
    for left, right in windows:
        for side in (-1, 1):
            expected_y = first.points[0][1] + side * offset
            positions = sorted(
                x for x, y in ground if y == expected_y and left <= x <= right
            )
            assert len(positions) >= 2, (case.name, left, right, side)
            assert positions[0] - left <= 800_000
            assert right - positions[-1] <= 800_000
            assert all(
                second - first <= 800_000
                for first, second in zip(positions, positions[1:])
            )


@pytest.mark.parametrize(
    ("case", "combined", "checked_in"),
    [
        (case, None, False)
        for case in RF_CASES
        if not case.paired and case.coplanar and case.sma_launches
    ]
    + [
        (circuit, board, checked_in)
        for board in COMBINED_BOARDS
        if not board.paired
        for circuit in board.circuits
        if circuit.coplanar
        for checked_in in (False, True)
    ],
    ids=lambda value: ("checked-in" if value else "generated")
    if isinstance(value, bool)
    else getattr(value, "name", "isolated"),
)
def test_sma_cpwg_native_fences_reach_both_launches_with_physical_clearance(
    case: Union[RFCase, CombinedCircuit],
    combined: Optional[CombinedBoard],
    checked_in: bool,
) -> None:
    """Fence the complete SMA route, including short-core and multilayer launches."""
    from math import sqrt

    from tests.test_rf_impedance_matrix import _point_has_clearance
    from tests.test_rf_impedance_showcases import _pads

    source = combined.board_text() if combined is not None else case.board_text()
    if checked_in:
        assert combined is not None
        source = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "impedance"
            / f"{combined.name}.kicad_pcb"
        ).read_text(encoding="utf-8")
    native = _parse_board(source)
    nets = {str(item[1]): str(item[2]) for item in _children(native, "net")}
    native_tracks = [
        track for track in _track_geometry(native).values() if track[-1] != "GND"
    ]
    traces = case.traces()
    left = min(point[0] for trace in traces for point in trace.points)
    right = max(point[0] for trace in traces for point in trace.points)
    center_y = case.legs[0].points[0][1]
    ground = [
        (
            tuple(round(float(value) * 1_000_000) for value in _field(via, "at")[:2]),
            round(float(_field(via, "size")[0]) * 1_000_000),
            round(float(_field(via, "drill")[0]) * 1_000_000),
        )
        for via in _children(native, "via")
        if nets[str(_field(via, "net")[0])] == "GND"
    ]
    pads = []
    pad_holes = []
    for footprint in _children(native, "footprint"):
        if footprint[1] != "Connector_Coaxial:SMA_Amphenol_132134_Vertical":
            continue
        for number, net, point in _pads(footprint):
            pad = next(
                item for item in _children(footprint, "pad") if item[1] == number
            )
            pad_holes.append((point, round(float(_field(pad, "drill")[0]) * 1_000_000)))
            if net in case.net_names:
                assert pad[3] == "circle"
                diameter = round(float(_field(pad, "size")[0]) * 1_000_000)
                pads.append((point, diameter))
    assert len(pads) == 2
    assert {point[0] for point, _ in pads} == {left, right}
    # Existing fixture policy is 0.8 mm longitudinal pitch and at most lambda/80
    # physical pitch in Dk=4 at 2 GHz, including bends around connector pads.
    max_pitch = 299_792_458 / 2_000_000_000 / sqrt(4) * 1_000_000_000 / 80
    for side in (-1, 1):
        row = sorted(
            (point, diameter, drill)
            for point, diameter, drill in ground
            if left <= point[0] <= right
            and 0 < (point[1] - center_y) * side <= 2_000_000
        )
        assert row
        assert row[0][0][0] - left <= 800_000, "Fence stops before the left SMA launch"
        assert right - row[-1][0][0] <= 800_000, (
            "Fence stops before the right SMA launch"
        )
        for (first, _, _), (second, _, _) in zip(row, row[1:]):
            assert 0 < second[0] - first[0] <= 800_000
            assert hypot(second[0] - first[0], second[1] - first[1]) <= max_pitch
        for point, diameter, drill in row:
            for track in native_tracks:
                assert _point_has_clearance(
                    point,
                    (track[0], track[1]),
                    (track[2], track[3]),
                    diameter + track[4] + 400_000,
                ), "A launch fence violates signal/context track-to-via clearance"
            for pad_point, pad_diameter in pads:
                assert (
                    hypot(point[0] - pad_point[0], point[1] - pad_point[1])
                    >= (diameter + pad_diameter) / 2 + 200_000
                ), "A launch fence violates SMA signal-pad clearance"
            for hole_point, hole_drill in pad_holes:
                assert (
                    hypot(point[0] - hole_point[0], point[1] - hole_point[1])
                    >= (drill + hole_drill) / 2 + 250_000
                ), "A launch fence violates SMA hole-to-hole clearance"


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_rf_fanouts_use_axis_aligned_or_45_degree_segments(case: RFCase) -> None:
    """A symmetric pair changes pitch using short 45-degree connector fanouts."""
    for trace in case.traces():
        for start, end in zip(trace.points, trace.points[1:]):
            dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
            assert dx == 0 or dy in (0, dx)
    _assert_gentle_joints(case.traces())
    launches = case.usb_launches()
    if launches is not None:
        # At a three-way duplicate-contact junction, preserve the gentle bend
        # along each explicit launch path rather than comparing every branch pair.
        for index, route in enumerate(launches.traces):
            _assert_gentle_joints(
                (
                    Trace(
                        str(index), route.layer, route.net, route.width_nm, route.points
                    ),
                )
            )


def test_signal_joint_assertions_reject_a_right_angle_between_valid_axes() -> None:
    """Two individually axis-aligned segments can still form a square bend."""
    traces = (
        Trace("first", "F.Cu", "RF", 100_000, ((0, 0), (1_000_000, 0))),
        Trace(
            "second", "F.Cu", "RF", 100_000, ((1_000_000, 0), (1_000_000, 1_000_000))
        ),
    )
    with pytest.raises(AssertionError):
        _assert_gentle_joints(traces)


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_rf_context_routes_have_45_degree_corners(case: RFCase) -> None:
    """Background copper in RF examples must not resemble square RF routing."""
    tracks = _track_geometry(_parse_board(case.board_text()))
    nets = {track[-1] for track in tracks.values() if track[-1].startswith("CONTEXT")}
    assert len(nets) == 9
    for net in nets:
        members = [track for track in tracks.values() if track[-1] == net]
        assert len(members) == 3
        assert any(
            abs(track[2] - track[0]) == abs(track[3] - track[1]) for track in members
        )
        for first in members:
            for second in members:
                if first is not second and first[2:4] == second[:2]:
                    a = (first[2] - first[0], first[3] - first[1])
                    b = (second[2] - second[0], second[3] - second[1])
                    assert a[0] * b[0] + a[1] * b[1] > 0


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_rf_snapshot_declares_native_differential_pair_identity(case: RFCase) -> None:
    """The scanner can group P/N without retaining legacy manual section groups."""
    assert case.snapshot().differential_pairs == (
        (("USB_D-", "USB_D+"),) if case.paired else ()
    )


def test_separate_right_angle_geometry_stress_case_is_preserved() -> None:
    """Presentation changes must not remove the original framing edge case."""
    from tests.impedance_capture_fixtures import EXAMPLES

    meander = next(example for example in EXAMPLES if example.name == "meander-240mm")
    assert meander.length_mm == 240
    assert len(meander.points) == 8
    board = _parse_board(RF_CASES[0].board_text())
    assert len(_children(board, "footprint")) >= 8


def _assert_gentle_joints(traces: tuple[Trace, ...]) -> None:
    """Check every two-edge bend independent of segment orientation.

    USB duplicate-contact junctions have three branches; comparing all branch
    pairs there is not a turn along a single routed path.
    """
    joints: dict[tuple[str, str, tuple[int, int]], list[tuple[int, int]]] = {}
    for trace in traces:
        for first, second in zip(trace.points, trace.points[1:]):
            for point, other in ((first, second), (second, first)):
                joints.setdefault((trace.net, trace.layer, point), []).append(
                    (other[0] - point[0], other[1] - point[1])
                )
    for vectors in joints.values():
        if len(vectors) == 2:
            first, second = vectors
            assert first[0] * second[0] + first[1] * second[1] < 0
