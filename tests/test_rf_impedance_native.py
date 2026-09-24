"""Exercise the openable RF boards through native KiCad loading, fill and plots.

These tests materialize the isolated geometry matrix in temporary directories,
then ask native KiCad to load, fill, and save the complete PCB/project/rules.
The two shipped combined boards have separate saved-board capture coverage.
These checks do not certify impedance or the plugin's capture UI.
"""

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from xml.etree import ElementTree as ET

import pytest

from tests.rf_impedance_fixtures import RF_CASES, RFCase
from tests.test_impedance_capture_examples import (
    rf_examples,  # noqa: F401 -- pytest discovers the imported fixture
)

EXPECTED_COPPER_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
GROUND_REFERENCE_LAYERS = {"In1.Cu", "In3.Cu", "In4.Cu"}
EXPECTED_REFERENCES = {
    "F.Cu": ("In1.Cu",),
    "In2.Cu": ("In1.Cu", "In3.Cu"),
    "B.Cu": ("In4.Cu",),
}
NativePoint = tuple[int, int]
NativePolygon = tuple[NativePoint, ...]
NativeTrack = tuple[int, int, int, int, int, str, str]


def _parse_board(source: str) -> list[object]:
    """Read native S-expressions without depending on the fixture serializer."""
    stack: list[list[object]] = [[]]
    for token in re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+', source):
        if token == "(":
            node: list[object] = []
            stack[-1].append(node)
            stack.append(node)
        elif token == ")":
            assert len(stack) > 1, "Unmatched closing parenthesis in native board"
            stack.pop()
        else:
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    assert len(stack) == 1 and len(stack[0]) == 1
    board = stack[0][0]
    assert isinstance(board, list) and board[0] == "kicad_pcb"
    return board


def _children(node: list[object], name: str) -> list[list[object]]:
    """Return only direct named child expressions, not nested lookalikes."""
    return [
        child
        for child in node
        if isinstance(child, list) and child and child[0] == name
    ]


def _field(node: list[object], name: str) -> list[object]:
    """Read one required native field and reject missing/ambiguous values."""
    matches = _children(node, name)
    assert len(matches) == 1, f"Expected one {name} field, found {len(matches)}"
    return matches[0][1:]


def _track_geometry(board: list[object]) -> dict[str, NativeTrack]:
    """Preserve real track identity, dimensions, layer and net across native save."""
    net_names = {str(net[1]): str(net[2]) for net in _children(board, "net")}
    result = {}
    for track in _children(board, "segment"):
        coordinates = tuple(
            round(float(str(value)) * 1_000_000)
            for field in ("start", "end", "width")
            for value in _field(track, field)
        )
        net_code = str(_field(track, "net")[0])
        result[str(_field(track, "uuid")[0])] = (
            *coordinates,
            str(_field(track, "layer")[0]),
            net_names.get(net_code, net_code),
        )
    return result


def _ground_fill_nodes(
    board: list[object],
) -> Iterator[tuple[str, list[object]]]:
    """Read filled GND geometry, never a zone's unfilled bounding outline."""
    for zone in _children(board, "zone"):
        if _children(zone, "keepout"):
            continue
        net_name = (
            _field(zone, "net_name")
            if _children(zone, "net_name")
            else _field(zone, "net")
        )
        if net_name == ["GND"]:
            for polygon in _children(zone, "filled_polygon"):
                yield str(_field(polygon, "layer")[0]), polygon


def _ground_polygons(board: list[object]) -> dict[str, list[NativePolygon]]:
    """Convert KiCad's native filled copper contours to nanometre coordinates."""
    result: dict[str, list[NativePolygon]] = {}
    for layer, polygon in _ground_fill_nodes(board):
        points = _children(_children(polygon, "pts")[0], "xy")
        coordinates = tuple(
            (
                round(float(str(point[1])) * 1_000_000),
                round(float(str(point[2])) * 1_000_000),
            )
            for point in points
        )
        result.setdefault(layer, []).append(coordinates)
    return result


def _inside_polygon(point: NativePoint, polygon: NativePolygon) -> bool:
    """Use even/odd containment, including KiCad's self-bridged hole contours."""
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in zip(polygon, (*polygon[1:], polygon[0])):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    return inside


def _copper_at(polygons: list[NativePolygon], point: NativePoint) -> bool:
    """Require membership in actual filled copper, accounting for its holes."""
    return any(_inside_polygon(point, polygon) for polygon in polygons)


def _assert_native_ground_clearances(board: list[object], case: RFCase) -> None:
    """Probe uniform route cross-sections independently of keepout metadata."""
    copper = _ground_polygons(board)
    tracks = _track_geometry(board)
    for leg in case.legs:
        start, end = leg.points
        x, center_y = (start[0] + end[0]) // 2, (start[1] + end[1]) // 2
        member_offset = (
            (leg.width_nm + (leg.spacing_nm or 0)) // 2 if case.paired else 0
        )
        outer_edge = member_offset + leg.width_nm // 2
        # These are user-requested fixture policies, not field-solver formulas.
        clearance = 200_000 if case.coplanar else 3 * leg.width_nm
        # Custom rules follow each local trace edge. A far-away SMA launch
        # fanout must not make this straight route's clearance artificially wide.
        local_edges = []
        for x1, y1, x2, y2, width, layer, net in tracks.values():
            if (
                layer == leg.layer
                and net in case.net_names
                and min(x1, x2) <= x <= max(x1, x2)
                and x1 != x2
            ):
                y = y1 + (x - x1) * (y2 - y1) / (x2 - x1)
                local_edges.extend((round(y - width / 2), round(y + width / 2)))
        assert local_edges, f"Missing RF tracks at cross-section on {leg.layer}"
        envelope_edges = {
            -1: center_y - min(local_edges),
            1: max(local_edges) - center_y,
        }
        signal_copper = copper.get(leg.layer, [])
        inside_offsets = {
            -member_offset,
            -(leg.spacing_nm or 0) // 4,
            0,
            (leg.spacing_nm or 0) // 4,
            member_offset,
        }
        for offset in inside_offsets:
            assert not _copper_at(signal_copper, (x, center_y + offset)), (
                f"Unexpected signal-layer GND between/under RF conductors on {leg.layer}"
            )
        for side in (-1, 1):
            envelope_edge = envelope_edges[side]
            for offset in (
                outer_edge + clearance // 4,
                outer_edge + clearance // 2,
                envelope_edge + clearance - 50_000,
            ):
                point = x, center_y + side * offset
                assert not _copper_at(signal_copper, point), (
                    f"Unexpected signal-layer GND inside the {clearance / 1e6:g} mm "
                    f"clearance corridor on {leg.layer}: {point}"
                )
            # Fifty micrometres outside the specified gap avoids numerical edge
            # ambiguity while detecting an absent pour or an over-wide channel.
            outside = x, center_y + side * (envelope_edge + clearance + 50_000)
            assert _copper_at(signal_copper, outside), (
                f"Missing signal-layer GND just outside clearance on {leg.layer}: {outside}"
            )
        for reference in EXPECTED_REFERENCES[leg.layer]:
            for offset in {-member_offset, 0, member_offset}:
                beneath = x, center_y + offset
                assert _copper_at(copper.get(reference, []), beneath), (
                    f"Missing reference copper beneath RF conductor on {reference}: {beneath}"
                )


@dataclass(frozen=True)
class NativeBoard:
    """A temporary copy filled and saved by the actual installed KiCad CLI."""

    case: RFCase
    executable: str
    path: Path
    original: list[object]
    filled: list[object]
    report: dict[str, object]


def _run_native(arguments: list[str]) -> None:
    """Expose KiCad's diagnostic text when a real parser or plot command fails."""
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"KiCad exited {result.returncode}: {arguments}\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.fixture(scope="module", params=RF_CASES, ids=lambda case: case.name)
def native_rf_board(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    rf_examples: Path,  # noqa: F811 -- pytest injects the shared fixture
) -> Iterator[NativeBoard]:
    """Refill copies only; never let KiCad rewrite an example the user can open."""
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Set KICAD_CLI to enable native RF fixture validation")
    case = request.param
    original_path = rf_examples / f"{case.name}.kicad_pcb"
    original_bytes = original_path.read_bytes()
    directory = tmp_path_factory.mktemp(f"native-rf-{case.name}")
    board_path = directory / original_path.name
    shutil.copy2(original_path, board_path)
    for suffix in (".kicad_pro", ".kicad_dru"):
        settings = original_path.with_suffix(suffix)
        if suffix == ".kicad_dru" and not case.coplanar:
            assert settings.is_file(), "Noncoplanar fill requires its custom rules"
        if settings.exists():
            shutil.copy2(settings, board_path.with_suffix(suffix))
    report_path = directory / "drc.json"
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
            str(report_path),
            str(board_path),
        ]
    )
    yield NativeBoard(
        case,
        executable,
        board_path,
        _parse_board(original_bytes.decode("utf-8")),
        _parse_board(board_path.read_text(encoding="utf-8")),
        json.loads(report_path.read_text(encoding="utf-8")),
    )
    assert original_path.read_bytes() == original_bytes


@pytest.mark.native_kicad
def test_native_refill_preserves_tracks_and_fills_ground_planes(
    native_rf_board: NativeBoard,
) -> None:
    """Require actual copper on references and signal layers, not zone outlines."""
    board = native_rf_board.filled
    layers = _field(board, "layers")
    copper_layers = tuple(
        str(layer[1])
        for layer in layers
        if isinstance(layer, list) and str(layer[1]).endswith(".Cu")
    )
    assert copper_layers == EXPECTED_COPPER_LAYERS
    assert _track_geometry(board) == _track_geometry(native_rf_board.original)
    required_layers = set(EXPECTED_COPPER_LAYERS)
    filled_ground_layers = set()
    keepouts = []
    for zone in _children(board, "zone"):
        if _children(zone, "keepout"):
            keepouts.append(zone)
            assert not _children(zone, "filled_polygon")
            continue
        net_name = (
            _field(zone, "net_name")
            if _children(zone, "net_name")
            else _field(zone, "net")
        )
        if net_name != ["GND"]:
            continue
        for polygon in _children(zone, "filled_polygon"):
            points = _children(_children(polygon, "pts")[0], "xy")
            assert len(points) >= 4
            filled_ground_layers.add(str(_field(polygon, "layer")[0]))
    assert required_layers <= filled_ground_layers
    if native_rf_board.case.coplanar:
        assert keepouts, "CPWG must retain its existing coplanar gap keepout"
    else:
        assert not keepouts, "Noncoplanar pour spacing must come from custom rules"
    assert all(
        str(_field(zone, "layer")[0]) not in GROUND_REFERENCE_LAYERS
        for zone in keepouts
    ), "Signal clearance must not cut a ground-reference plane"


@pytest.mark.native_kicad
def test_native_filled_copper_respects_rf_corridors_and_reference_continuity(
    native_rf_board: NativeBoard,
) -> None:
    """Check real copper beside/under RF runs, not merely a polygon somewhere."""
    _assert_native_ground_clearances(native_rf_board.filled, native_rf_board.case)


@pytest.mark.native_kicad
def test_native_copper_probes_reject_displaced_signal_clearance(
    native_rf_board: NativeBoard,
) -> None:
    """Prove a moved filled corridor cannot pass the physical clearance checks."""
    displaced = deepcopy(native_rf_board.filled)
    target_layer = native_rf_board.case.legs[0].layer
    for layer, polygon in _ground_fill_nodes(displaced):
        if layer == target_layer:
            for point in _children(_children(polygon, "pts")[0], "xy"):
                point[2] = f"{float(str(point[2])) + 17:.6f}"
    with pytest.raises(AssertionError, match="Unexpected signal-layer GND"):
        _assert_native_ground_clearances(displaced, native_rf_board.case)


@pytest.mark.native_kicad
def test_native_copper_probes_reject_remote_reference_fill(
    native_rf_board: NativeBoard,
) -> None:
    """Reject a populated reference layer whose copper is nowhere near the RF run."""
    remote = deepcopy(native_rf_board.filled)
    target_layer = EXPECTED_REFERENCES[native_rf_board.case.legs[0].layer][0]
    for layer, polygon in _ground_fill_nodes(remote):
        if layer == target_layer:
            points = _children(polygon, "pts")[0]
            points[:] = [
                "pts",
                ["xy", "21", "21"],
                ["xy", "22", "21"],
                ["xy", "22", "22"],
                ["xy", "21", "22"],
            ]
    with pytest.raises(AssertionError, match="Missing reference copper"):
        _assert_native_ground_clearances(remote, native_rf_board.case)


def test_copper_containment_honors_native_self_bridged_holes() -> None:
    """A KiCad hole is empty even when joined to its outline by a repeated bridge."""
    polygon = (
        (0, 0),
        (10, 0),
        (10, 10),
        (0, 10),
        (0, 0),
        (3, 3),
        (3, 7),
        (7, 7),
        (7, 3),
        (3, 3),
        (0, 0),
    )
    for contour in (polygon, tuple(reversed(polygon))):
        assert _inside_polygon((1, 2), contour)
        assert not _inside_polygon((5, 5), contour)
        assert not _inside_polygon((12, 5), contour)


@pytest.mark.native_kicad
def test_native_drc_has_no_copper_errors_or_unconnected_items(
    native_rf_board: NativeBoard,
) -> None:
    """Reject real shorts and rule failures, retaining limited fixture warnings."""
    report = native_rf_board.report
    assert report["unconnected_items"] == [], json.dumps(report, indent=2)
    assert {"error", "warning"} <= set(report["included_severities"])
    ignored_checks = {item["key"] for item in report["ignored_checks"]}
    assert not ignored_checks & {
        "shorting_items",
        "clearance",
        "unconnected_items",
        "track_width",
        "via_diameter",
        "annular_width",
        "hole_clearance",
        "copper_edge_clearance",
    }
    # Only surrounding CONTEXT tracks are intentionally open-ended. The RF
    # traces terminate in real SMA or stress-testpoint pads and must remain connected.
    # Embedded contextual footprints need no installed footprint library.
    context_ids = {
        identifier
        for identifier, geometry in _track_geometry(native_rf_board.original).items()
        if str(geometry[-1]).startswith("CONTEXT")
    }
    permitted_warnings = {"lib_footprint_issues"}
    unexpected = [
        violation
        for violation in report["violations"]
        if violation["severity"] != "warning"
        or (
            violation["type"] not in permitted_warnings
            and not (
                violation["type"] == "track_dangling"
                and violation["items"]
                and all(item["uuid"] in context_ids for item in violation["items"])
            )
        )
    ]
    assert unexpected == [], json.dumps(unexpected, indent=2)


@pytest.mark.native_kicad
def test_native_color_plots_include_each_signal_layer_and_component_context(
    native_rf_board: NativeBoard,
) -> None:
    """Use native colored geometry for top, bottom and buried signal inspection."""
    context_layers = ("Edge.Cuts", "F.SilkS", "B.SilkS")
    for layer in dict.fromkeys(leg.layer for leg in native_rf_board.case.legs):
        plot_path = native_rf_board.path.parent / f"{layer}.svg"
        _run_native(
            [
                native_rf_board.executable,
                "pcb",
                "export",
                "svg",
                "--mode-single",
                "--layers",
                ",".join((layer, *context_layers)),
                "--page-size-mode",
                "2",
                "--exclude-drawing-sheet",
                "--output",
                str(plot_path),
                str(native_rf_board.path),
            ]
        )
        source = plot_path.read_text(encoding="utf-8")
        root = ET.fromstring(source)  # noqa: S314 - KiCad-generated local test SVG.
        assert root.tag.endswith("}svg")
        assert len(re.findall(r"<(?:path|circle|polyline)\b", source)) > 20
        labels = {
            element.text for element in root.iter() if element.tag.endswith("}desc")
        }
        assert {"U1", "U2", "J1"} <= labels
        assert "RF_CONTEXT" not in labels
        colors = {
            match.lower()
            for match in re.findall(
                r"(?:stroke|fill)\s*[:=]\s*[\"']?(#[0-9a-fA-F]{6})", source
            )
        }
        chromatic = {
            color for color in colors if len({color[1:3], color[3:5], color[5:7]}) > 1
        }
        assert len(colors) >= 3, f"Missing layer/context colors in {plot_path}"
        assert chromatic, f"Native plot unexpectedly monochrome: {plot_path}"
