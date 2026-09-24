"""Independent physical/topological checks for the two-socket USB-C coupon."""

from collections import defaultdict
from dataclasses import replace
from heapq import heappop, heappush
from math import hypot

import pytest

from tests.rf_usb_c_geometry import (
    B_COUPLED_END,
    B_COUPLED_START,
    F_COUPLED_END,
    F_COUPLED_START,
    GAP_NM,
    WIDTH_NM,
    Point,
    usb_routes,
    usb_vias,
)


def _segments() -> list[tuple[str, str, Point, Point]]:
    return [
        (route.layer, route.net, start, end)
        for route in usb_routes()
        for start, end in zip(route.points, route.points[1:])
    ]


def _point_distance(point: Point, start: Point, end: Point) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    fraction = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
            / (dx * dx + dy * dy),
        ),
    )
    return hypot(
        point[0] - start[0] - fraction * dx, point[1] - start[1] - fraction * dy
    )


def _side(point: Point, start: Point, end: Point) -> float:
    return (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (
        end[0] - start[0]
    )


def _segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    if (
        _side(a, c, d) * _side(b, c, d) <= 0
        and _side(c, a, b) * _side(d, a, b) <= 0
        and max(min(a[0], b[0]), min(c[0], d[0]))
        <= min(max(a[0], b[0]), max(c[0], d[0]))
        and max(min(a[1], b[1]), min(c[1], d[1]))
        <= min(max(a[1], b[1]), max(c[1], d[1]))
    ):
        return 0.0
    return min(
        _point_distance(a, c, d),
        _point_distance(b, c, d),
        _point_distance(c, a, b),
        _point_distance(d, a, b),
    )


@pytest.mark.parametrize(
    ("net", "pads"),
    (
        ("USB_D-", ((38.68, 64.25), (38.68, 65.25), (221.32, 64.75), (221.32, 65.75))),
        ("USB_D+", ((38.68, 64.75), (38.68, 65.75), (221.32, 64.25), (221.32, 65.25))),
    ),
)
def test_both_reversible_socket_data_contacts_are_connected(
    net: str, pads: tuple[Point, ...]
) -> None:
    """Every A/B contact, not just one orientation, reaches the opposite socket."""
    graph: dict[tuple[str, Point], set[tuple[str, Point]]] = defaultdict(set)
    for layer, member, start, end in _segments():
        if member == net:
            graph[layer, start].add((layer, end))
            graph[layer, end].add((layer, start))
    for via in usb_vias():
        if via.net == net:
            graph["F.Cu", via.point].add(("B.Cu", via.point))
            graph["B.Cu", via.point].add(("F.Cu", via.point))
    pending = [("F.Cu", pads[0])]
    visited = set()
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(graph[node] - visited)
    assert {(layer, point) for layer, point in graph} == visited
    assert {("F.Cu", point) for point in pads} <= visited


@pytest.mark.parametrize("layer", ("F.Cu", "B.Cu"))
def test_each_layer_contains_both_pair_members(layer: str) -> None:
    """The backside crossover is a pair route, never an isolated one-net jumper."""
    assert {route.net for route in usb_routes() if route.layer == layer} == {
        "USB_D-",
        "USB_D+",
    }


@pytest.mark.parametrize(
    ("layer", "start", "end", "minimum_length"),
    (
        ("F.Cu", F_COUPLED_START, F_COUPLED_END, 120.0),
        ("B.Cu", B_COUPLED_START, B_COUPLED_END, 15.0),
    ),
)
def test_both_layers_have_a_real_eight_mil_uniform_pair(
    layer: str, start: Point, end: Point, minimum_length: float
) -> None:
    """Check both actual net centrelines throughout each advertised fence window."""
    assert WIDTH_NM == 270_000
    assert GAP_NM == 203_200
    assert end[0] - start[0] >= minimum_length
    assert start[1] == end[1]
    runs = {
        net: (a, b)
        for signal_layer, net, a, b in _segments()
        if signal_layer == layer and a[0] <= start[0] < end[0] <= b[0] and a[1] == b[1]
    }
    assert set(runs) == {"USB_D-", "USB_D+"}
    minus_y, plus_y = (runs[net][0][1] for net in ("USB_D-", "USB_D+"))
    assert abs(minus_y - plus_y) * 1_000_000 - WIDTH_NM == pytest.approx(GAP_NM)
    assert (minus_y + plus_y) / 2 == pytest.approx(start[1])


def test_different_net_tracks_clear_each_other_on_each_layer() -> None:
    """Uncontrolled fanout geometry must still satisfy ordinary copper clearance."""
    segments = _segments()
    for index, (layer, net, start, end) in enumerate(segments):
        for other_layer, other_net, other_start, other_end in segments[index + 1 :]:
            if layer == other_layer and net != other_net:
                assert (
                    _segment_distance(start, end, other_start, other_end) >= 0.47 - 1e-8
                ), (layer, net, start, end, other_net, other_start, other_end)


def test_duplicate_bridges_clear_every_opposite_data_pad() -> None:
    """Use conservative rectangles around the real 1.15 by 0.30 mm pad copper."""
    pads = (
        ("USB_D-", (38.68, 64.25)),
        ("USB_D-", (38.68, 65.25)),
        ("USB_D+", (38.68, 64.75)),
        ("USB_D+", (38.68, 65.75)),
        ("USB_D-", (221.32, 64.75)),
        ("USB_D-", (221.32, 65.75)),
        ("USB_D+", (221.32, 64.25)),
        ("USB_D+", (221.32, 65.25)),
    )
    for pad_net, (x, y) in pads:
        corners = (
            (x - 0.575, y - 0.15),
            (x + 0.575, y - 0.15),
            (x + 0.575, y + 0.15),
            (x - 0.575, y + 0.15),
        )
        for layer, net, start, end in _segments():
            if layer == "F.Cu" and net != pad_net:
                distance = min(
                    _segment_distance(start, end, a, b)
                    for a, b in zip(corners, corners[1:] + corners[:1])
                )
                assert distance >= 0.335 - 1e-8, (pad_net, (x, y), net, start, end)


def test_through_vias_clear_other_nets_on_both_layers() -> None:
    """Through pads are obstacles on both routed layers, not only their start layer."""
    for via in usb_vias():
        for layer, net, start, end in _segments():
            if net != via.net:
                assert _point_distance(via.point, start, end) >= 0.635 - 1e-8, (
                    via,
                    layer,
                    net,
                    start,
                    end,
                )
    vias = usb_vias()
    for index, via in enumerate(vias):
        for other in vias[index + 1 :]:
            distance = hypot(
                via.point[0] - other.point[0], via.point[1] - other.point[1]
            )
            assert distance >= 0.55  # 0.3 mm drill diameter + 0.25 mm hole clearance.
            if via.net != other.net:
                assert distance >= 0.8  # 0.6 mm pad diameter + 0.2 mm copper clearance.


def test_main_pair_transitions_are_aligned_and_all_vias_have_local_returns() -> None:
    """Keep the coupled trunk symmetric without counting its separate contact bridge."""
    vias = usb_vias()
    center_y = F_COUPLED_START[1]
    for x in (185.0, 215.0):
        transitions = {
            via.net: via.point for via in vias if via.point[0] == x and via.net != "GND"
        }
        assert set(transitions) == {"USB_D-", "USB_D+"}
        assert transitions["USB_D-"] == pytest.approx((x, center_y - 0.45))
        assert transitions["USB_D+"] == pytest.approx((x, center_y + 0.45))
    # The documented local J2 D+ orientation bridge is not a third trunk
    # transition. Its two vias must remain outside the coupled routing windows.
    bridge_vias = [
        via for via in vias if via.net != "GND" and via.point[0] not in (185.0, 215.0)
    ]
    assert len(bridge_vias) == 2
    assert {via.net for via in bridge_vias} == {"USB_D+"}
    assert all(via.point[0] > B_COUPLED_END[0] for via in bridge_vias)
    returns = [via.point for via in vias if via.net == "GND"]
    for net in ("USB_D-", "USB_D+"):
        signal_vias = [via.point for via in vias if via.net == net]
        for point in signal_vias:
            assert (
                min(
                    hypot(point[0] - ground[0], point[1] - ground[1])
                    for ground in returns
                )
                <= 1.11
            )


def test_shared_same_net_conductors_are_emitted_only_once() -> None:
    """Branch at explicit vertices instead of laying duplicate copper over leads."""
    segments = _segments()
    for index, (layer, net, a, b) in enumerate(segments):
        for other_layer, other_net, c, d in segments[index + 1 :]:
            if layer != other_layer or net != other_net:
                continue
            if abs(_side(c, a, b)) > 1e-8 or abs(_side(d, a, b)) > 1e-8:
                continue
            axis = 0 if abs(b[0] - a[0]) > 1e-8 else 1
            overlap = min(max(a[axis], b[axis]), max(c[axis], d[axis])) - max(
                min(a[axis], b[axis]), min(c[axis], d[axis])
            )
            assert overlap <= 1e-8, (layer, net, a, b, c, d)


def test_routes_use_only_axis_or_45_degree_segments_and_no_right_angle_corners() -> (
    None
):
    """Each connected polyline turns by at most 45 degrees at a time."""
    for route in usb_routes():
        vectors = [
            (b[0] - a[0], b[1] - a[1]) for a, b in zip(route.points, route.points[1:])
        ]
        for dx, dy in vectors:
            assert hypot(dx, dy) > 0
            assert abs(dx) < 1e-8 or abs(dy) < 1e-8 or abs(abs(dx) - abs(dy)) < 1e-8
        for (ax, ay), (bx, by) in zip(vectors, vectors[1:]):
            assert (ax * bx + ay * by) / (
                hypot(ax, ay) * hypot(bx, by)
            ) >= 2**-0.5 - 1e-8


def test_primary_launch_skew_remains_small_without_claiming_usb_compliance() -> None:
    """Compare equivalent trunk launches, not one main launch against a duplicate pad."""
    # These are the primary trunk contacts at each opposed socket. Ending D-
    # at J2 B7 (y=65.75) would add its duplicate-contact bridge only to D-.
    # The reversible contact paths are checked separately below; this is not
    # a cable-orientation timing or USB-compliance claim.
    lengths = (
        _path_length("USB_D-", (38.68, 64.25), (221.32, 64.75)),
        _path_length("USB_D+", (38.68, 64.75), (221.32, 65.25)),
    )
    assert min(lengths) > 180
    assert abs(lengths[0] - lengths[1]) < 2


@pytest.mark.parametrize(
    ("net", "start", "end"),
    (
        ("USB_D-", (38.68, 64.25), (38.68, 65.25)),
        ("USB_D+", (38.68, 64.75), (38.68, 65.75)),
        ("USB_D-", (221.32, 64.75), (221.32, 65.75)),
    ),
)
def test_planar_reversible_data_pad_bridges_stay_local(
    net: str, start: Point, end: Point
) -> None:
    """Keep each A/B contact bridge short, not a long stub around the board."""
    assert _path_length(net, start, end) <= 3.5


def _assert_local_orientation_bridge(path: tuple[tuple[str, Point], ...]) -> None:
    """Require two compact symmetric launches and one straight secondary-layer link."""
    # The 1.15 mm horizontal bound is the actual contact pad length. Vertical
    # overhang is at most half the 0.5 mm contact pitch, beyond the joined pads.
    # Unlike the old planar 3.5 mm length bound, these constraints account for
    # the intentionally documented via bridge without permitting board detours.
    assert tuple(layer for layer, _point in path) == (
        "F.Cu",
        "F.Cu",
        "F.Cu",
        "B.Cu",
        "B.Cu",
        "F.Cu",
        "F.Cu",
        "F.Cu",
    )
    assert path[0][1] == (221.32, 64.25)
    assert path[-1][1] == (221.32, 65.25)
    assert all(
        221.32 - 1.15 <= x <= 221.32 and 64.0 <= y <= 65.5 for _layer, (x, y) in path
    )
    upper, lower = path[3][1], path[4][1]
    assert upper[0] == lower[0]
    assert 64.25 - upper[1] == pytest.approx(lower[1] - 65.25)
    assert path[2][1] == upper and path[5][1] == lower


def test_secondary_layer_orientation_bridge_stays_beside_the_connector() -> None:
    """Keep the separate J2 D+ bridge local without imposing a planar-route budget."""
    _assert_local_orientation_bridge(
        _path_vertices("USB_D+", (221.32, 64.25), (221.32, 65.25))
    )


def test_orientation_bridge_locality_rejects_an_added_board_detour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure a still-connected long secondary-layer detour cannot satisfy locality."""
    routes = usb_routes()
    bridge = next(
        route
        for route in routes
        if route.layer == "B.Cu" and route.net == "USB_D+" and route.points[0][0] > 215
    )
    start, end = bridge.points
    detour = replace(
        bridge, points=(start, (start[0] - 20, start[1]), (end[0] - 20, end[1]), end)
    )
    monkeypatch.setattr(
        __name__ + ".usb_routes",
        lambda: tuple(detour if route == bridge else route for route in routes),
    )
    path = _path_vertices("USB_D+", (221.32, 64.25), (221.32, 65.25))
    with pytest.raises(AssertionError):
        _assert_local_orientation_bridge(path)


def _path_length(net: str, start: Point, end: Point) -> float:
    path = _path_vertices(net, start, end)
    return sum(
        hypot(b[0] - a[0], b[1] - a[1])
        for (_first_layer, a), (_last_layer, b) in zip(path, path[1:])
    )


def _path_vertices(net: str, start: Point, end: Point) -> tuple[tuple[str, Point], ...]:
    """Read the shortest actual routed path, retaining layer transitions explicitly."""
    graph: dict[tuple[str, Point], list[tuple[float, tuple[str, Point]]]] = defaultdict(
        list
    )
    for layer, member, a, b in _segments():
        if member == net:
            distance = hypot(b[0] - a[0], b[1] - a[1])
            graph[layer, a].append((distance, (layer, b)))
            graph[layer, b].append((distance, (layer, a)))
    for via in usb_vias():
        if via.net == net:
            graph["F.Cu", via.point].append((0.0, ("B.Cu", via.point)))
            graph["B.Cu", via.point].append((0.0, ("F.Cu", via.point)))
    pending = [(0.0, (("F.Cu", start),))]
    visited = set()
    while pending:
        distance, path = heappop(pending)
        node = path[-1]
        if node == ("F.Cu", end):
            return path
        if node not in visited:
            visited.add(node)
            for length, neighbor in graph[node]:
                if neighbor not in visited:
                    heappush(pending, (distance + length, path + (neighbor,)))
    raise AssertionError(f"No connected {net} path between connector pads")
