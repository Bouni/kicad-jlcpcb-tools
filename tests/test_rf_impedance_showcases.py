"""Check real connector geometry/connectivity independently of the serializer."""

from collections import defaultdict
from hashlib import sha256
import json
from math import cos, hypot, radians, sin
import os
from pathlib import Path
import shutil

import pytest

from tests.rf_impedance_showcases import showcase_files
from tests.test_rf_impedance_native import (
    NativePoint,
    NativePolygon,
    NativeTrack,
    _children,
    _copper_at,
    _field,
    _ground_polygons,
    _parse_board,
    _run_native,
    _track_geometry,
)

NAMES = ("sma-single-ended-50-ohm", "usb-differential-cpwg-90-ohm")
DIRECTORY = Path(__file__).resolve().parents[1] / "examples/impedance/showcases"


def test_embedded_connector_accepts_the_calling_boards_native_net_codes() -> None:
    """Matrix boards can reuse exact SMA geometry without inheriting unrelated net IDs."""
    from tests.rf_impedance_showcases import SMA, _footprint

    source = _footprint(
        SMA,
        "JRF1",
        (35, 75, 0),
        {"1": "RF_P", "2": "GND"},
        "matrix-reuse",
        net_codes={"RF_P": 2, "GND": 4},
    )
    embedded = _children(_parse_board("(kicad_pcb " + source + ")"), "footprint")[0]
    for pad in _children(embedded, "pad"):
        assert _field(pad, "net") == (["2", "RF_P"] if pad[1] == "1" else ["4", "GND"])


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_embedded_usb_connector_rotates_its_text_with_its_land_pattern(
    rotation: int,
) -> None:
    """Long USB references must retain the library's clearance beside the pads."""
    from tests.rf_impedance_showcases import USB, _footprint, _serialize, _template

    source = _footprint(USB, "JUSB01A", (35, 75, rotation), {}, "rotated-usb")
    embedded = _children(_parse_board("(kicad_pcb " + source + ")"), "footprint")[0]
    template = _children(
        _parse_board("(kicad_pcb " + _serialize(_template(USB)) + ")"), "footprint"
    )[0]
    for kind in ("property", "fp_text"):
        for original, placed in zip(
            _children(template, kind), _children(embedded, kind)
        ):
            original_at = _field(original, "at")
            placed_at = _field(placed, "at")
            assert placed_at[:2] == original_at[:2]
            assert (
                float(str(placed_at[2])) % 360
                == (float(str(original_at[2])) + rotation) % 360
            )
    reference = next(
        item for item in _children(embedded, "property") if item[1] == "Reference"
    )
    assert reference[2] == "JUSB01A"
    assert _field(reference, "layer") == ["F.SilkS"]
    assert not _children(reference, "hide")


def _board(name: str) -> list[object]:
    """Read serialized output through an independent native syntax parser."""
    files = showcase_files()
    assert f"{name}.kicad_pcb" in files, "Missing openable connector showcase"
    return _parse_board(files[f"{name}.kicad_pcb"])


def _pads(footprint: list[object]) -> list[tuple[str, str, tuple[int, int]]]:
    """Resolve physical pad centres with KiCad's clockwise screen coordinates."""
    placement = [float(str(value)) for value in _field(footprint, "at")]
    x, y, angle = (*placement, 0)[:3]
    rotation = radians(angle)
    result = []
    for pad in _children(footprint, "pad"):
        px, py = (float(str(value)) for value in _field(pad, "at")[:2])
        location = (
            round((x + px * cos(rotation) + py * sin(rotation)) * 1_000_000),
            round((y - px * sin(rotation) + py * cos(rotation)) * 1_000_000),
        )
        net = str(_field(pad, "net")[-1]) if _children(pad, "net") else ""
        result.append((str(pad[1]), net, location))
    return result


@pytest.mark.parametrize("name", NAMES)
def test_showcases_are_deterministic_openable_and_have_real_connector_context(
    name: str,
) -> None:
    """Require populated real connector bodies, not renamed single test pads."""
    board = _board(name)
    footprints = _children(board, "footprint")
    assert len(footprints) == 2
    assert all(len(_children(item, "pad")) >= 4 for item in footprints)
    assert all(
        len(_children(item, "fp_line")) + 4 * len(_children(item, "fp_rect")) >= 8
        for item in footprints
    )
    assert showcase_files() == showcase_files()
    project = json.loads(showcase_files()[f"{name}.kicad_pro"])
    classes = project["net_settings"]["classes"]
    target = "50" if name.startswith("sma") else "90"
    assert any(target in item["name"] for item in classes)
    expected_nets = {"RF_SE"} if target == "50" else {"USB_D-", "USB_D+"}
    assignments = project["net_settings"]["netclass_patterns"]
    assert {item["pattern"] for item in assignments} == expected_nets
    assert all(target in item["netclass"] for item in assignments)
    assert not project["board"]["design_settings"]["drc_exclusions"]


def test_sma_uses_real_amphenol_signal_and_four_ground_pins() -> None:
    """Compare pad shape/dimensions with the actual Amphenol library geometry."""
    for footprint in _children(_board(NAMES[0]), "footprint"):
        assert footprint[1] == "Connector_Coaxial:SMA_Amphenol_132134_Vertical"
        pads = _children(footprint, "pad")
        assert len(pads) == 5
        for pad in pads:
            if pad[1] == "1":
                assert _field(pad, "size") == ["2.05", "2.05"]
                assert _field(pad, "drill") == ["1.5"]
                assert _field(pad, "net")[-1] == "RF_SE"
            else:
                assert pad[1] == "2"
                assert _field(pad, "size") == ["2.25", "2.25"]
                assert _field(pad, "drill") == ["1.7"]
                assert _field(pad, "net")[-1] == "GND"


def test_both_usb_c_connectors_join_every_orientation_with_correct_pin_polarity() -> (
    None
):
    """A6/B6 are D+, A7/B7 are D-; VBUS/CC/SBU must not be tied to ground."""
    connectors = _children(_board(NAMES[1]), "footprint")
    assert len(connectors) == 2
    for usb in connectors:
        assert usb[1] == (
            "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
        )
        reference = next(
            item[2] for item in _children(usb, "property") if item[1] == "Reference"
        )
        expected = {
            "": "",
            "A1": "GND",
            "A12": "GND",
            "B1": "GND",
            "B12": "GND",
            "SH": "GND",
            "A6": "USB_D+",
            "B6": "USB_D+",
            "A7": "USB_D-",
            "B7": "USB_D-",
            "A4": reference + "_UNUSED_VBUS_1",
            "A9": reference + "_UNUSED_VBUS_2",
            "B4": reference + "_UNUSED_VBUS_2",
            "B9": reference + "_UNUSED_VBUS_1",
            "A5": "",
            "B5": "",
            "A8": "",
            "B8": "",
        }
        assert {number: net for number, net, _ in _pads(usb)} == expected


def test_usb_unused_vbus_lands_are_isolated_and_hole_rule_is_explicit() -> None:
    """Coincident VBUS pad labels share one isolated land, never a power path."""
    board = _board(NAMES[1])
    nets = {str(item[1]): str(item[2]) for item in _children(board, "net")}
    unused = {name for name in nets.values() if "UNUSED_VBUS" in name}
    assert unused == {
        f"J{connector}_UNUSED_VBUS_{land}" for connector in (1, 2) for land in (1, 2)
    }
    assert not unused.intersection(
        track[-1] for track in _track_geometry(board).values()
    )
    assert not any(
        nets[str(_field(via, "net")[0])] in unused for via in _children(board, "via")
    )
    rules = json.loads(showcase_files()[NAMES[1] + ".kicad_pro"])["board"][
        "design_settings"
    ]["rules"]
    assert rules["min_hole_clearance"] == 0.15
    assert rules["min_clearance"] == 0.2
    assert rules["min_hole_to_hole"] == 0.25


def test_usb_update_preserves_sma_board_and_project_exactly() -> None:
    """Adding USB-C must not retune or move the existing SMA showcase."""
    files = showcase_files()
    assert sha256(files[NAMES[0] + ".kicad_pcb"].encode()).hexdigest() == (
        "a8b0f2fc6b6c27e2292d90652a0df7957692ff83ffd19c759c3f48d485ae018a"
    )
    assert sha256(files[NAMES[0] + ".kicad_pro"].encode()).hexdigest() == (
        "d8edc4528d69cb251ffba6fc4ab773fbd617adafb246bdbed71713eeef88b4fe"
    )


@pytest.mark.parametrize("name", NAMES)
def test_complete_selected_routes_connect_real_pins_and_use_45_degree_bends(
    name: str,
) -> None:
    """Trace complete copper paths, lengths and bend angles from serialized PCB."""
    board = _board(name)
    tracks = _track_geometry(board)
    expected_nets = ("RF_SE",) if name.startswith("sma") else ("USB_D-", "USB_D+")
    signal_pads = defaultdict(list)
    for footprint in _children(board, "footprint"):
        for _, net, location in _pads(footprint):
            if net in expected_nets:
                signal_pads[net].append(location)
    for net in expected_nets:
        selected = [track for track in tracks.values() if track[-1] == net]
        assert sum(hypot(bx - ax, by - ay) for ax, ay, bx, by, *_ in selected) > 120e6
        assert {track[-2] for track in selected} == (
            {"F.Cu"} if name.startswith("sma") else {"F.Cu", "B.Cu"}
        )
        graph = defaultdict(list)
        for ax, ay, bx, by, _, layer, _ in selected:
            assert ax == bx or ay == by or abs(bx - ax) == abs(by - ay)
            graph[(layer, ax, ay)].append((layer, bx, by))
            graph[(layer, bx, by)].append((layer, ax, ay))
        net_codes = {str(item[1]): str(item[2]) for item in _children(board, "net")}
        for via in _children(board, "via"):
            if net_codes[str(_field(via, "net")[0])] != net:
                continue
            x, y = (round(float(str(value)) * 1_000_000) for value in _field(via, "at"))
            assert ("F.Cu", x, y) in graph and ("B.Cu", x, y) in graph
            graph[("F.Cu", x, y)].append(("B.Cu", x, y))
            graph[("B.Cu", x, y)].append(("F.Cu", x, y))
        endpoints = {
            point for point, neighbours in graph.items() if len(neighbours) == 1
        }
        pad_nodes = {("F.Cu", *point) for point in signal_pads[net]}
        assert endpoints <= pad_nodes
        assert len(pad_nodes) == (2 if name.startswith("sma") else 4)
        visited, pending = set(), [next(iter(pad_nodes))]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(graph[current])
        assert visited == set(graph), "Selected route contains disconnected copper"
        assert pad_nodes <= visited, (
            "At least one USB-C orientation data pad is omitted"
        )
        for point, neighbours in graph.items():
            assert len(neighbours) in (1, 2, 3)
            if len(neighbours) == 2 and all(
                other[0] == point[0] for other in neighbours
            ):
                vectors = [(px - point[1], py - point[2]) for _, px, py in neighbours]
                dot = vectors[0][0] * vectors[1][0] + vectors[0][1] * vectors[1][1]
                assert dot < 0, (
                    "Selected showcase route has a right-angle or acute corner"
                )


def _assert_reference_copper(
    track: NativeTrack,
    polygons: list[NativePolygon],
    antipads: list[tuple[NativePoint, int]],
) -> None:
    """Probe actual reference copper, excluding only known signal-via antipads."""
    for step in range(1, 10):
        point = (
            round(track[0] + (track[2] - track[0]) * step / 10),
            round(track[1] + (track[3] - track[1]) * step / 10),
        )
        if any(
            hypot(point[0] - x, point[1] - y) <= radius for (x, y), radius in antipads
        ):
            continue
        assert _copper_at(polygons, point), "Missing reference under routed signal"


def test_reference_probe_excludes_only_known_via_antipads() -> None:
    """A midpoint on an antipad is legitimate; a missing plane away from it is not."""
    track = (0, 0, 1_000_000, 0, 270_000, "F.Cu", "USB_D-")
    plane = [
        (
            (510_000, -1_000_000),
            (2_000_000, -1_000_000),
            (2_000_000, 1_000_000),
            (510_000, 1_000_000),
        )
    ]
    antipads = [((0, 0), 501_000)]
    _assert_reference_copper(track, plane, antipads)
    with pytest.raises(AssertionError, match="Missing reference"):
        _assert_reference_copper(track, [], antipads)
    with pytest.raises(AssertionError, match="Missing reference"):
        _assert_reference_copper(track, plane, [])


@pytest.mark.native_kicad
@pytest.mark.parametrize("name", NAMES)
def test_native_showcase_fill_drc_and_reference_copper(
    name: str, tmp_path: Path
) -> None:
    """Use KiCad's actual parser, zone filler and connectivity checker on copies."""
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Set KICAD_CLI to enable native connector showcase checks")
    files = showcase_files()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        assert name + suffix in files, "Missing openable connector showcase"
        (tmp_path / (name + suffix)).write_text(files[name + suffix], encoding="utf-8")
    path, report = tmp_path / (name + ".kicad_pcb"), tmp_path / "drc.json"
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
    assert result["unconnected_items"] == [], json.dumps(result, indent=2)
    unexpected = [
        item
        for item in result["violations"]
        if not (
            item["severity"] == "warning" and item["type"] == "lib_footprint_issues"
        )
    ]
    assert unexpected == [], json.dumps(unexpected, indent=2)
    filled = _parse_board(path.read_text(encoding="utf-8"))
    assert _track_geometry(filled) == _track_geometry(_board(name))
    ground = _ground_polygons(filled)
    assert {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"} <= set(ground)
    antipads = [
        (
            tuple(round(float(str(value)) * 1_000_000) for value in _field(via, "at")),
            round(float(str(_field(via, "size")[0])) * 500_000) + 201_000,
        )
        for via in _children(filled, "via")
        if _field(via, "net") != ["1"]
    ]
    for track in _track_geometry(filled).values():
        if track[-1] not in {"RF_SE", "USB_D-", "USB_D+"}:
            continue
        reference = "In1.Cu" if track[-2] == "F.Cu" else "In2.Cu"
        # Signal through-vias necessarily clear the plane by the 0.2 mm zone
        # clearance. Add 1 um only for native polygon approximation at the edge.
        _assert_reference_copper(track, ground[reference], antipads)


def test_usb_pair_has_exact_eight_mil_coupled_gap_on_both_routed_layers() -> None:
    """Pin actual copper width/gap independently of metadata or displayed units."""
    tracks = _track_geometry(_board(NAMES[1]))
    for layer in ("F.Cu", "B.Cu"):
        members = [
            [
                track
                for track in tracks.values()
                if track[-1] == net and track[-2] == layer
            ]
            for net in ("USB_D-", "USB_D+")
        ]
        assert all(members), "Both mates must exist on every routed layer"
        assert all(track[4] == 270_000 for member in members for track in member)
        longest = [
            max(member, key=lambda item: abs(item[2] - item[0])) for member in members
        ]
        assert all(track[1] == track[3] for track in longest)
        overlap = min(max(track[0], track[2]) for track in longest) - max(
            min(track[0], track[2]) for track in longest
        )
        assert overlap > (120_000_000 if layer == "F.Cu" else 5_000_000)
        assert abs(longest[1][1] - longest[0][1]) - 270_000 == 203_200
    project = json.loads(showcase_files()[NAMES[1] + ".kicad_pro"])
    rf_class = next(
        item for item in project["net_settings"]["classes"] if item["name"] != "Default"
    )
    assert rf_class["diff_pair_gap"] == 0.2032


def test_usb_placement_precedes_global_pad_angles() -> None:
    """Guard the native parser ordering defect found by real short-circuit DRC."""
    for usb in _children(_board(NAMES[1]), "footprint"):
        first_pad = next(
            index
            for index, item in enumerate(usb)
            if isinstance(item, list) and item[0] == "pad"
        )
        placement = next(
            index
            for index, item in enumerate(usb)
            if isinstance(item, list) and item[0] == "at"
        )
        assert placement < first_pad
        rotation = _field(usb, "at")[2]
        for pad in _children(usb, "pad"):
            if pad[1] in {"A6", "B6", "A7", "B7"}:
                assert _field(pad, "at")[2] == rotation


def test_usb_values_are_short_and_positioned_clear_of_the_selected_pair() -> None:
    """A library-sized Value must not obscure the pair in native board captures."""
    board = _board(NAMES[1])
    signal_y = min(
        min(track[1], track[3])
        for track in _track_geometry(board).values()
        if track[-1] in {"USB_D-", "USB_D+"}
    )
    for footprint in _children(board, "footprint"):
        value = next(
            item for item in _children(footprint, "property") if item[1] == "Value"
        )
        assert value[2] == "USB-C"
        x, y, angle = (float(str(item)) for item in _field(footprint, "at"))
        px, py = (float(str(item)) for item in _field(value, "at")[:2])
        world_y = y - px * sin(radians(angle)) + py * cos(radians(angle))
        assert world_y == pytest.approx(58.5)
        font = _children(_children(value, "effects")[0], "font")[0]
        width, height = (float(str(item)) for item in _field(font, "size"))
        stroke = float(str(_field(font, "thickness")[0]))
        text_angle = radians(float(str(_field(value, "at")[2])))
        half_height = (
            abs(sin(text_angle)) * len(str(value[2])) * width
            + abs(cos(text_angle)) * height
            + stroke
        ) / 2
        assert (world_y + half_height) * 1_000_000 < signal_y - 2_000_000
        assert _field(value, "layer") == ["F.Fab"]
        assert not _children(value, "hide")


def test_usb_mating_face_follows_the_real_footprints_pcb_edge_datum() -> None:
    """DRC cannot detect a USB socket obstructed by PCB in front of its opening."""
    board = _board(NAMES[1])
    connectors = _children(board, "footprint")
    for index, usb in enumerate(connectors):
        datum = [
            line
            for line in _children(usb, "fp_line")
            if _field(line, "layer") == ["Dwgs.User"]
        ]
        assert len(datum) == 1
        assert _field(datum[0], "start") == ["5", "3.675"]
        assert _field(datum[0], "end") == ["-5", "3.675"]
        origin_x = float(str(_field(usb, "at")[0]))
        board_edge = _children(board, "gr_rect")[0]
        actual = float(str(_field(board_edge, "start" if index == 0 else "end")[0]))
        assert actual == pytest.approx(origin_x + (-3.675 if index == 0 else 3.675)), (
            "PCB extends in front of connector's mating face"
        )


def test_vendored_footprints_are_in_outputs_and_need_no_installed_library() -> None:
    """Alternate output directories retain exact library sources and license."""
    files = showcase_files()
    templates = {name for name in files if name.endswith(".kicad_mod")}
    assert len(templates) == 2
    assert "source-footprints/LICENSE.md" in files
    assert "Creative Commons" in files["source-footprints/LICENSE.md"]
    for name in NAMES:
        board = _board(name)
        for footprint in _children(board, "footprint"):
            assert not _children(footprint, "model")
            assert not _children(footprint, "version")


def _fence_chains(points: list[NativePoint]) -> list[set[NativePoint]]:
    """Find continuous single rows, rejecting gaps, collisions and extra rows."""
    assert len(points) == len(set(points))
    graph = {point: [] for point in points}
    for index, point in enumerate(points):
        for other in points[index + 1 :]:
            distance = hypot(other[0] - point[0], other[1] - point[1])
            assert distance >= 600_000
            if distance <= 800_002:  # Serialization rounds each coordinate to 1 nm.
                graph[point].append(other)
                graph[other].append(point)
    assert all(len(neighbours) in (1, 2) for neighbours in graph.values())
    remaining = set(points)
    components = []
    while remaining:
        component, pending = set(), [next(iter(remaining))]
        while pending:
            point = pending.pop()
            if point in component:
                continue
            component.add(point)
            pending.extend(graph[point])
        remaining -= component
        components.append(component)
    assert len(components) == 2, "Expected one continuous fence row per side"
    for component in components:
        assert sum(len(graph[point]) == 1 for point in component) == 2
    return components


def test_sma_fences_are_exactly_two_continuous_single_rows_at_08mm_or_less() -> None:
    """The SMA showcase retains its continuous two-sided 45-degree fence."""
    vias = _children(_board(NAMES[0]), "via")
    points = [
        tuple(round(float(str(value)) * 1_000_000) for value in _field(via, "at"))
        for via in vias
    ]
    assert all(_field(via, "net") == ["1"] for via in vias)
    for component in _fence_chains(points):
        assert min(point[0] for point in component) == 41_000_000
        assert max(point[0] for point in component) == 219_000_000
    launch_y = {point[1] for point in points if point[0] == 41_000_000}
    assert launch_y == {74_000_000, 76_000_000}


@pytest.mark.parametrize("layer", ("F.Cu", "B.Cu"))
def test_usb_fences_are_single_rows_beside_each_uniform_coupled_region(
    layer: str,
) -> None:
    """Fence actual pair runs, leaving clearance for symmetric layer transitions."""
    board = _board(NAMES[1])
    tracks = _track_geometry(board)
    longest = [
        max(
            (
                track
                for track in tracks.values()
                if track[-2:] == (layer, net) and track[1] == track[3]
            ),
            key=lambda track: abs(track[2] - track[0]),
        )
        for net in ("USB_D-", "USB_D+")
    ]
    left = max(min(track[0], track[2]) for track in longest)
    right = min(max(track[0], track[2]) for track in longest)
    centre = sum(track[1] for track in longest) // 2
    points = [
        tuple(round(float(str(value)) * 1_000_000) for value in _field(via, "at"))
        for via in _children(board, "via")
        if _field(via, "net") == ["1"]
    ]
    window = [point for point in points if left <= point[0] <= right]
    assert {point[1] for point in window} == {centre - 1_100_000, centre + 1_100_000}
    for component in _fence_chains(window):
        assert 0 <= min(point[0] for point in component) - left <= 2_000_000
        assert 0 <= right - max(point[0] for point in component) <= 2_000_000


def test_showcase_attribution_and_limitations_are_shipped() -> None:
    """Do not present a passive two-receptacle coupon as a USB-compliant adapter."""
    files = showcase_files()
    assert "README.md" in files
    readme = files["README.md"]
    for expected in (
        "50",
        "90",
        "not",
        "field solver",
        "USB",
        "USB-C",
        "VBUS",
        "CC",
        "SBU",
        "0.2032",
        "CC-BY-SA-4.0",
        "20260206",
    ):
        assert expected in readme
