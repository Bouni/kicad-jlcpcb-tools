"""Assess actual KiCad track geometry without changing the owned native board.

These getter/setter tests intentionally avoid the KiCad 10 capture fixture API.
The supported native matrix discovers them on KiCad 7 through 10; they are not
substitutes for the separate wx and Configure-through-Generate smoke workflows.
KiCad 7's footprint cases verify the adapter's explicit unsupported-text error
and unchanged native geometry; they do not establish transition measurement.
"""

from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from impedance.pcbnew_adapter import BoardSnapshotError, snapshot_board
from impedance.spacing_checks import compare_pair_spacing, measure_pair_spacing
from tests.impedance_performance_fixtures import populate_native_terminal_pair
from tests.native_kicad_support import native_bindings, requires_native_plotting
from tests.native_wx_support import isolated_native

pytestmark = [pytest.mark.native_kicad, requires_native_plotting]
__all__ = ["native_bindings"]


@pytest.mark.parametrize(
    "state",
    (
        "tight",
        "wide",
        "bounded",
        "regional",
        "unknown",
        "zero_target",
        "missing_target",
        "at_target",
        "tiny_below",
        "tiny_above",
        "large",
        "zero_actual",
        "overlap",
    ),
)
@pytest.mark.parametrize("layout", ("standard", "wide"))
def test_native_fit_labels_do_not_clip_or_overlap(
    request: pytest.FixtureRequest, state: str, layout: str
) -> None:
    """Real native font extents keep the whole fitted composition legible."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_FONT_CHILD", native="kicad"):
        return
    from impedance.matching import analyze
    from impedance.render import _spacing_summary_svg
    from impedance.spacing_checks import NmBounds
    from impedance.spacing_review import spacing_review_entries
    from tests.test_impedance_spacing_checks import _parallel
    from tests.test_impedance_spacing_review_entries import _config

    wx = request.getfixturevalue("native_bindings").wx
    config = _config()
    spec = config.specifications[0]
    target = 202_300 if state == "wide" else 203_200
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=target),),
            ),
        ),
    )
    actual = {"wide": 331_300, "zero_actual": 0, "overlap": -25_000}.get(state, 200_000)
    snapshot = _parallel(actual, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    if layout == "wide":
        entry = replace(
            entry,
            composition_layout="wide",
            viewport_nm=(-1_000_000, -500_000, 6_000_000, 6_000_000 * 460 / 1568),
        )
    if state in ("regional", "unknown"):
        entry = replace(
            entry,
            members=tuple(
                replace(member, cross_sections=()) for member in entry.members
            ),
            representative=None,
        )
    if state == "bounded":
        entry = replace(entry, region_gap_nm=NmBounds(199_999, 200_001))
    elif state == "unknown":
        entry = replace(entry, region_gap_nm=None)
    elif state == "zero_target":
        entry = replace(entry, target_spacing_nm=0)
    elif state == "missing_target":
        entry = replace(entry, target_spacing_nm=None)
    elif state in ("at_target", "tiny_below", "tiny_above", "large", "overlap"):
        entry = replace(
            entry,
            target_spacing_nm={
                "at_target": 200_000,
                "tiny_below": 200_001,
                "tiny_above": 199_999,
                "large": 1_000,
                "overlap": 100_000,
            }[state],
        )
    root = ET.fromstring("<svg>" + _spacing_summary_svg(entry) + "</svg>")  # noqa: S314 -- Local SVG.
    expected = {
        "tight": "1.6% below target",
        "wide": "63.8% above target",
        "bounded": "1.6% below target",
        "regional": "Unavailable",
        "unknown": "Unavailable",
        "zero_target": "Unavailable",
        "missing_target": "Unavailable",
        "at_target": "0.0% at target",
        "tiny_below": "<0.1% below target",
        "tiny_above": "<0.1% above target",
        "large": "19900.0% above target",
        "zero_actual": "100.0% below target",
        "overlap": "125.0% below target",
    }[state]
    labels = {node.text for node in root.iter("text")}
    assert "Gap deviation from target" in labels
    assert expected in labels
    assert (
        not {
            "Actual versus target - schematic",
            "Both bars use the same scale",
            "Actual",
            "Target",
        }
        & labels
    )
    assert not any(node.get("data-spacing-schematic-gap") for node in root.iter())
    footer = entry.composition.schematic_rect
    assert footer in entry.composition.required_regions
    readout = next(
        node
        for node in root.iter("text")
        if node.text == expected
        and float(node.get("data-spacing-text-top")) >= footer[1]
    )
    assert float(readout.get("font-size")) >= 64
    if state not in ("regional", "unknown"):
        assert entry.composition.actual_gap_nm == pytest.approx(actual)
    bitmap = wx.Bitmap(entry.width_px, entry.height_px)
    dc = wx.MemoryDC(bitmap)
    boxes = []
    try:
        for node in root.iter("text"):
            size = round(float(node.get("font-size")))
            assert size >= 42
            assert size * entry.composition.minimum_scale >= 14
            dc.SetFont(
                wx.Font(
                    wx.Size(0, size),
                    wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL,
                    wx.FONTWEIGHT_BOLD
                    if node.get("font-weight") == "bold"
                    else wx.FONTWEIGHT_NORMAL,
                )
            )
            width, height = dc.GetTextExtent(node.text or "")
            x, y = float(node.get("x")), float(node.get("data-spacing-text-top"))
            assert 0 <= x < x + width <= entry.width_px, (state, node.text)
            assert 0 <= y < y + height <= entry.height_px, (state, node.text)
            if node is readout:
                assert footer[0] <= x < x + width <= footer[0] + footer[2]
                assert footer[1] <= y < y + height <= footer[1] + footer[3]
            for other_x, other_y, other_width, other_height in boxes:
                assert (
                    x >= other_x + other_width
                    or other_x >= x + width
                    or y >= other_y + other_height
                    or other_y >= y + height
                ), (state, node.text)
            boxes.append((x, y, width, height))
    finally:
        dc.SelectObject(wx.NullBitmap)
    assert boxes


def test_native_clipped_labels_fail_without_producing_review_pixels(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A native font that exceeds the canonical canvas cannot create evidence."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_FONT_CHILD", native="kicad"):
        return
    from impedance.render import RenderError, _wx_rasterize
    from impedance.spacing_composition import spacing_label
    from impedance.spacing_review import SPACING_RENDER_POLICY_REVISION

    request.getfixturevalue("native_bindings")
    source = tmp_path / "clipped-label.svg"
    destination = tmp_path / "clipped-label.png"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" '
        f'data-spacing-composition="{SPACING_RENDER_POLICY_REVISION}">'
        '<rect width="1600" height="1000" fill="#10151d"/>'
        '<svg x="16" y="70" width="1120" height="600" '
        'data-spacing-raster-panel="detail">'
        '<rect width="1120" height="600" fill="#10151d"/></svg>'
        + spacing_label("This measurement is clipped", 1590, 374)
        + "</svg>",
        encoding="utf-8",
    )
    with pytest.raises(RenderError, match="labels do not fit"):
        _wx_rasterize(source, destination, 1600, 1000)
    assert not destination.exists()


def test_native_short_collinear_gap_rulers_preserve_both_measurement_colors(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The target ruler's keyline must not erase a nearby actual-gap ruler."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_RULER_CHILD", native="kicad"):
        return
    from impedance.matching import analyze
    from impedance.render import (
        SPACING_GUIDE_COLOR,
        SPACING_TICK_COLOR,
        _spacing_annotations,
        _wx_rasterize,
    )
    from impedance.spacing_overlays import spacing_guide_paths
    from impedance.spacing_review import spacing_review_entries
    from tests.test_impedance_spacing_checks import _parallel
    from tests.test_impedance_spacing_review_entries import _config

    wx = request.getfixturevalue("native_bindings").wx
    config = _config()
    spec = config.specifications[0]
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=203_200),),
            ),
        ),
    )
    snapshot = _parallel(215_698, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    # A single authoritative cross-section permits local rulers, without
    # certifying continuous target edges. Keep their physical endpoints fixed.
    entry = replace(
        entry,
        members=tuple(
            replace(member, cross_sections=member.cross_sections[:1])
            for member in entry.members
        ),
        viewport_nm=(-2_000_000, -1_000_000, 5_000_000, 5_000_000 * 600 / 1120),
        width_px=1120,
        height_px=600,
    )
    selected = entry.composition.measurement
    assert selected is not None
    assert entry.composition.actual_gap_nm == pytest.approx(215_698)
    guides = {
        guide.kind: guide
        for guide in spacing_guide_paths(entry)
        if guide.section == selected
    }
    assert set(guides) == {"measured-gap", "target-gap"}
    actual, target = guides["measured-gap"], guides["target-gap"]
    assert actual.start == target.start
    assert math.dist(actual.start, actual.end) == pytest.approx(215_698)
    assert math.dist(target.start, target.end) == pytest.approx(203_200)
    # This is the native scale of the retained failure: the two end caps are
    # less than three source pixels apart, while both stems exceed 45 pixels.
    scale = entry.width_px / entry.viewport_nm[2]
    assert 0 < math.dist(actual.end, target.end) * scale < 3
    assert math.dist(target.start, target.end) * scale > 45
    source, destination = tmp_path / "local-rulers.svg", tmp_path / "local-rulers.png"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="600">'
        '<rect width="1120" height="600" fill="#10151d"/>'
        + _spacing_annotations(entry)
        + "</svg>",
        encoding="utf-8",
    )
    _wx_rasterize(source, destination, entry.width_px, entry.height_px)
    image = wx.Image(str(destination), wx.BITMAP_TYPE_PNG)
    assert image.IsOk()
    points = (actual.start, actual.end, target.end)
    left, top, width, height = entry.viewport_nm
    xs = tuple((point[0] - left) / width * entry.width_px for point in points)
    ys = tuple((point[1] - top) / height * entry.height_px for point in points)
    pixel_left = max(0, math.floor(min(xs)) - 12)
    pixel_right = min(entry.width_px, math.ceil(max(xs)) + 13)
    pixel_top = max(0, math.floor(min(ys)) - 12)
    pixel_bottom = min(entry.height_px, math.ceil(max(ys)) + 13)
    rgb = bytes(image.GetData())
    colors = tuple(
        tuple(rgb[(y * entry.width_px + x) * 3 : (y * entry.width_px + x) * 3 + 3])
        for y in range(pixel_top, pixel_bottom)
        for x in range(pixel_left, pixel_right)
    )
    for value, tolerance in ((SPACING_TICK_COLOR, 100), (SPACING_GUIDE_COLOR, 60)):
        expected = tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))
        assert any(
            max(abs(a - b) for a, b in zip(color, expected)) <= tolerance
            for color in colors
        ), f"The physical local rulers lost {value} pixels"


def _pair_board(pcbnew: Any, path: Path) -> tuple[Any, tuple[Any, Any]]:
    """Create real reciprocally paired nets using KiCad's own naming rules."""
    board = pcbnew.BOARD()
    board.SetFileName(str(path))
    nets = tuple(pcbnew.NETINFO_ITEM(board, name) for name in ("USB_D+", "USB_D-"))
    for net in nets:
        board.Add(net)
    native_pair = getattr(board, "DpCoupledNet", None)
    if callable(native_pair):
        assert native_pair(nets[0]).GetNetname() == "USB_D-"
        assert native_pair(nets[1]).GetNetname() == "USB_D+"
    return board, nets


def _track(pcbnew: Any, board: Any, net: Any, y_nm: int) -> Any:
    """Use integer nanometres so the one-nanometre boundary cases are exact."""
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(0, y_nm))
    track.SetEnd(pcbnew.VECTOR2I(20_000_000, y_nm))
    track.SetWidth(150_000)
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    board.Add(track)
    return track


def _arc_radius(pcbnew: Any, arc: Any, radius_nm: int) -> None:
    """Set three exact points on the same native semicircle."""
    arc.SetStart(pcbnew.VECTOR2I(0, radius_nm))
    arc.SetMid(pcbnew.VECTOR2I(radius_nm, 0))
    arc.SetEnd(pcbnew.VECTOR2I(0, -radius_nm))


def _native_geometry(board: Any) -> tuple[tuple[Any, ...], ...]:
    """Read independent native properties before and after each assessment."""
    records = []
    for item in board.GetTracks():
        start, end = item.GetStart(), item.GetEnd()
        midpoint = item.GetMid() if hasattr(item, "GetMid") else None
        center = item.GetPosition() if midpoint is not None else None
        records.append(
            (
                "track",
                str(item.GetNetname()),
                int(item.GetLayer()),
                int(item.GetWidth()),
                (int(start.x), int(start.y)),
                (int(end.x), int(end.y)),
                None if midpoint is None else (int(midpoint.x), int(midpoint.y)),
                None if center is None else (int(center.x), int(center.y)),
                None if midpoint is None else float(item.GetRadius()),
            )
        )
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            position, size = pad.GetPosition(), pad.GetSize()
            records.append(
                (
                    "pad",
                    str(pad.GetNetname()),
                    (int(position.x), int(position.y)),
                    (int(size.x), int(size.y)),
                    int(pad.GetShape()),
                )
            )
    return tuple(sorted(records))


def _assess(board: Any, pcbnew: Any) -> SimpleNamespace:
    """Copy with the production adapter and measure authoritative native mates."""
    before = _native_geometry(board)
    snapshot = snapshot_board(board, pcbnew)
    pairing_available = callable(getattr(board, "DpCoupledNet", None))
    pair = ("USB_D+", "USB_D-")
    if pairing_available:
        assert not snapshot.differential_pair_error
        assert snapshot.differential_pairs == (pair,)
    else:
        assert "DpCoupledNet" in snapshot.differential_pair_error
        assert snapshot.differential_pairs == ()
    measurement = measure_pair_spacing(snapshot, pair, "F.Cu")
    check = compare_pair_spacing(measurement, 150_000)
    if not pairing_available:
        assert check.verdict == "undetermined"
        assert check.coverage == "none"
        assert check.minimum_gap_nm is None
        assert check.maximum_gap_nm is None
        assert check.reasons
    assert _native_geometry(board) == before
    assert snapshot_board(board, pcbnew) == snapshot
    return SimpleNamespace(
        snapshot=snapshot, check=check, pairing_available=pairing_available
    )


def _unsupported_native_footprint_context(board: Any, pcbnew: Any) -> bool:
    """Verify KiCad 7's known missing field API without replacing its geometry."""
    if int(str(pcbnew.GetBuildVersion()).split(".", 1)[0]) != 7:
        return False
    footprints = tuple(board.GetFootprints())
    if not footprints or all(
        callable(getattr(footprint, "GetFields", None)) for footprint in footprints
    ):
        return False
    before = _native_geometry(board)
    with pytest.raises(
        BoardSnapshotError,
        match=r"^KiCad cannot read FOOTPRINT rendered text with these bindings\.$",
    ):
        snapshot_board(board, pcbnew)
    assert _native_geometry(board) == before
    return True


def test_native_pair_move_and_repair_changes_spacing_without_changing_width(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Actual setters exercise matching -> tight/wide -> matching transitions."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    board, nets = _pair_board(pcbnew, tmp_path / "spacing.kicad_pcb")
    _track(pcbnew, board, nets[0], 0)
    mate = _track(pcbnew, board, nets[1], 300_000)
    original = _assess(board, pcbnew)
    assert original.check.verdict == (
        "match" if original.pairing_available else "undetermined"
    )
    assert original.check.coverage == (
        "complete" if original.pairing_available else "none"
    )
    for actual_gap_nm in (100_000, 250_000):
        y_nm = 150_000 + actual_gap_nm
        mate.SetStart(pcbnew.VECTOR2I(0, y_nm))
        mate.SetEnd(pcbnew.VECTOR2I(20_000_000, y_nm))
        changed = _assess(board, pcbnew)
        if changed.pairing_available:
            assert changed.check.verdict == "deviation"
            assert changed.check.coverage == "complete"
            assert changed.check.minimum_gap_nm.lower_nm == actual_gap_nm
            assert changed.check.maximum_gap_nm.upper_nm == actual_gap_nm
        else:
            assert changed.check.verdict == "undetermined"
            assert changed.check.coverage == "none"
        assert changed.snapshot != original.snapshot
        assert all(trace.width_nm == 150_000 for trace in changed.snapshot.traces)
        mate.SetStart(pcbnew.VECTOR2I(0, 300_000))
        mate.SetEnd(pcbnew.VECTOR2I(20_000_000, 300_000))
        repaired = _assess(board, pcbnew)
        assert repaired.check.verdict == (
            "match" if repaired.pairing_available else "undetermined"
        )
        assert repaired.snapshot == original.snapshot


def test_native_arc_midpoints_are_copied_and_spacing_boundaries_are_dimensional(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Native midpoint edits cannot mutate earlier immutable geometry evidence."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    board, nets = _pair_board(pcbnew, tmp_path / "arc-spacing.kicad_pcb")
    arcs = []
    for net, radius_nm in zip(nets, (2_000_000, 2_300_000)):
        arc = pcbnew.PCB_ARC(board)
        _arc_radius(pcbnew, arc, radius_nm)
        arc.SetWidth(150_000)
        arc.SetLayer(pcbnew.F_Cu)
        arc.SetNet(net)
        board.Add(arc)
        arcs.append(arc)
    original = _assess(board, pcbnew)
    assert original.check.verdict == (
        "match" if original.pairing_available else "undetermined"
    )
    assert all(
        isinstance(trace.native_arc_midpoint, tuple)
        for trace in original.snapshot.traces
    )
    assert {trace.native_arc_center for trace in original.snapshot.traces} == {(0, 0)}
    assert {trace.native_arc_radius_nm for trace in original.snapshot.traces} == {
        2_000_000.0,
        2_300_000.0,
    }
    definitions = tuple(
        (trace.native_arc_midpoint, trace.native_arc_center, trace.native_arc_radius_nm)
        for trace in original.snapshot.traces
    )
    for gap_nm, expected in (
        (149_000, "match"),
        (151_000, "match"),
        (148_999, "deviation"),
        (151_001, "deviation"),
    ):
        _arc_radius(pcbnew, arcs[1], 2_150_000 + gap_nm)
        current = _assess(board, pcbnew)
        assert current.check.verdict == (
            expected if current.pairing_available else "undetermined"
        )
        assert current.check.coverage == (
            "complete" if current.pairing_available else "none"
        )
        by_net = {trace.net: trace for trace in current.snapshot.traces}
        for arc in arcs:
            copied = by_net[str(arc.GetNetname())]
            center = arc.GetPosition()
            assert copied.native_arc_center == (int(center.x), int(center.y))
            assert copied.native_arc_radius_nm == float(arc.GetRadius())
        assert (
            tuple(
                (
                    trace.native_arc_midpoint,
                    trace.native_arc_center,
                    trace.native_arc_radius_nm,
                )
                for trace in original.snapshot.traces
            )
            == definitions
        )
    _arc_radius(pcbnew, arcs[1], 2_300_000)
    assert _assess(board, pcbnew).snapshot == original.snapshot


def test_native_rounded_arc_centers_cannot_produce_false_complete_match(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """KiCad's integer arc center must outrank a reconstructed half-nm center."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    board, nets = _pair_board(pcbnew, tmp_path / "rounded-arc-spacing.kicad_pcb")
    arcs = []
    definitions = (
        ((2_500_003, 0), (-2_500_002, 0), (1_500_002, 2_000_002)),
        ((-2_801_002, 0), (2_801_003, 0), (1_680_602, 2_240_802)),
    )
    for net, (start, end, midpoint) in zip(nets, definitions):
        arc = pcbnew.PCB_ARC(board)
        arc.SetStart(pcbnew.VECTOR2I(*start))
        arc.SetMid(pcbnew.VECTOR2I(*midpoint))
        arc.SetEnd(pcbnew.VECTOR2I(*end))
        arc.SetWidth(150_000)
        arc.SetLayer(pcbnew.F_Cu)
        arc.SetNet(net)
        board.Add(arc)
        arcs.append(arc)
    result = _assess(board, pcbnew)
    by_net = {trace.net: trace for trace in result.snapshot.traces}
    nonconcentric_definitions = []
    for arc in arcs:
        trace = by_net[str(arc.GetNetname())]
        center = arc.GetPosition()
        copied_center = (int(center.x), int(center.y))
        assert trace.native_arc_center == copied_center
        assert trace.native_arc_radius_nm == float(arc.GetRadius())
        start, midpoint, end = arc.GetStart(), arc.GetMid(), arc.GetEnd()
        assert trace.native_arc_midpoint == (int(midpoint.x), int(midpoint.y))
        squared_radii = {
            (int(point.x) - copied_center[0]) ** 2
            + (int(point.y) - copied_center[1]) ** 2
            for point in (start, midpoint, end)
        }
        nonconcentric_definitions.append(len(squared_radii) != 1)
    assert any(nonconcentric_definitions)
    assert result.check.verdict == "undetermined"
    assert result.check.coverage != "complete"
    if result.pairing_available:
        assert result.check.unverified_length_nm > 0
    else:
        assert result.check.coverage == "none"
    assert result.check.reasons


@pytest.mark.parametrize(
    "gap_nm, verdict", [(150_000, "match"), (300_000, "deviation")]
)
def test_native_terminal_contacts_exclude_fanouts_and_keep_wrong_gap_trunks(
    request: pytest.FixtureRequest, tmp_path: Path, gap_nm: int, verdict: str
) -> None:
    """Actual pad flashes bound launches; a paired trunk is checked at any gap."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    board, nets = _pair_board(pcbnew, tmp_path / "terminal-spacing.kicad_pcb")
    fixture = populate_native_terminal_pair(board, pcbnew, nets, gap_nm=gap_nm)
    if _unsupported_native_footprint_context(board, pcbnew):
        return
    original = _assess(board, pcbnew)
    features = original.snapshot.copper_features
    if original.pairing_available and len(features) == 4:
        assert original.check.verdict == verdict
        assert original.check.coverage == "complete"
        assert original.check.transition_length_nm > 0
        # The pad-pitch regions also include a short prefix of the paired trunk.
        # Its long central portion remains independently assessed at either gap.
        assert 20_000_000 < original.check.assessed_length_nm < 24_000_000
        assert original.check.minimum_gap_nm.lower_nm == gap_nm
        assert original.check.maximum_gap_nm.upper_nm == gap_nm
        if verdict == "match":
            assert original.check.issues == ()
        else:
            assert original.check.issues
            for issue in original.check.issues:
                assert issue.location is not None
                assert issue.location.bounds[0] >= 600_000 - 75_000
                assert issue.location.bounds[2] <= 12_600_000 + 75_000
    elif original.pairing_available:
        # Older bindings without precise layer-specific pad getters do not
        # establish any terminal exemption. This is not native transition proof.
        assert original.check.transition_length_nm == 0
        assert original.check.verdict != "match"
    else:
        assert original.check.verdict == "undetermined"
        assert original.check.coverage == "none"
    frozen_features = original.snapshot.copper_features
    for pad in fixture.pads:
        pad.SetSize(pcbnew.VECTOR2I(450_000, 450_000))
    changed = _assess(board, pcbnew)
    assert original.snapshot.copper_features == frozen_features
    if features:
        assert changed.snapshot.copper_features != frozen_features
    for pad in fixture.pads:
        pad.SetSize(pcbnew.VECTOR2I(400_000, 400_000))
    assert _assess(board, pcbnew).snapshot == original.snapshot


def test_native_long_fanout_outside_landing_region_is_not_exempt(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """An eventual correct trunk cannot excuse normal routing beyond one pitch."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    board, nets = _pair_board(pcbnew, tmp_path / "long-entry-spacing.kicad_pcb")
    fixture = populate_native_terminal_pair(board, pcbnew, nets, launch_nm=2_000_000)
    if _unsupported_native_footprint_context(board, pcbnew):
        return
    result = _assess(board, pcbnew)
    if not result.pairing_available:
        assert result.check.verdict == "undetermined"
        return
    total_nm = sum(
        round(
            math.hypot(
                track.GetEnd().x - track.GetStart().x,
                track.GetEnd().y - track.GetStart().y,
            )
        )
        for track in fixture.tracks
    )
    _assert_long_entry_evidence(result.snapshot, result.check, total_nm)


def _assert_long_entry_evidence(snapshot: Any, check: Any, total_nm: int) -> None:
    """Require outside-region evidence and account for every physical nanometre."""
    assert check.verdict == "deviation"
    assert (
        check.assessed_length_nm
        + check.unverified_length_nm
        + check.transition_length_nm
        == total_nm
    )
    assert check.assessed_length_nm + check.unverified_length_nm > 24_000_000
    if snapshot.copper_features:
        assert len(snapshot.copper_features) == 4
        # Native 0.4 mm square pads at 1 mm pitch give at most 1.2 mm
        # longitudinal reach. Each 2 mm launch can lose at most that fraction.
        launches = tuple(
            trace
            for trace in snapshot.traces
            if trace.points[0][1] != trace.points[-1][1]
        )
        maximum_transition = math.ceil(
            sum(
                math.dist(trace.points[0], trace.points[-1]) * 0.6 for trace in launches
            )
        ) + len(launches)
        assert 0 < check.transition_length_nm <= maximum_transition
        assert check.assessed_length_nm > 24_000_000
    else:
        # Older layer-aware pad getters may be unavailable. The same bounded
        # search may complete or leave honest uncertainty; neither can exempt
        # the unrecognized landing area.
        assert check.transition_length_nm == 0
        if check.coverage == "partial":
            assert check.unverified_length_nm > 0
            assert any("work limit" in reason.lower() for reason in check.reasons)
        else:
            assert check.coverage == "complete"
            assert check.unverified_length_nm == 0
    assert any(
        1_200_000 < point[0] < 14_800_000
        for issue in check.issues
        if issue.code in {"too_tight", "too_wide"}
        for member in issue.members
        for point in member.source_points
    ), "No definite spacing witness lies outside the possible landing regions"


@pytest.mark.kicad10_impedance
@pytest.mark.parametrize("layer", ["F.Cu", "B.Cu"])
def test_native_spacing_closeup_preserves_manufacturing_png_and_reads_actual_gap(
    request: pytest.FixtureRequest, tmp_path: Path, layer: str
) -> None:
    """Native detail preserves copper and gap with measurements outside its panels."""
    if isolated_native(
        request, "KICAD_IMPEDANCE_SPACING_CAPTURE_CHILD", native="kicad"
    ):
        return
    from impedance.matching import analyze
    from impedance.model import Config, LayerSettings, Specification
    from impedance.render import SectionRenderer
    from impedance.service import load_spacing_capture
    from impedance.spacing_review import spacing_review_entries
    from scripts.native_impedance_workflow import check_spacing_guide_pixels

    bindings = request.getfixturevalue("native_bindings")
    pcbnew, wx = bindings.pcbnew, bindings.wx
    board, nets = _pair_board(pcbnew, tmp_path / "diagnostic.kicad_pcb")
    for side, net in enumerate(nets):
        track = _track(pcbnew, board, net, side * 250_000)
        track.SetLayer(pcbnew.F_Cu if layer == "F.Cu" else pcbnew.B_Cu)
    assert pcbnew.SaveBoard(board.GetFileName(), board)
    before = _native_geometry(board)
    source = Path(board.GetFileName()).read_bytes()
    snapshot = snapshot_board(board, pcbnew)
    config = Config(
        True,
        (
            Specification(
                "usb",
                "USB",
                "90",
                "differential",
                "Default",
                (
                    LayerSettings(
                        layer, ("B.Cu" if layer == "F.Cu" else "F.Cu",), 150_000
                    ),
                ),
            ),
        ),
    )
    sections = analyze(config, snapshot).sections
    entries = spacing_review_entries(config, snapshot, sections)
    assert len(entries) == 1
    with SectionRenderer(board, pcbnew) as renderer:
        manufacturing = renderer.render(
            sections[0], tmp_path / "original.png"
        ).read_bytes()
        for index, entry in enumerate(entries):
            svg = renderer.spacing_svg(entry)
            assert "data-impedance-spacing-view" in svg
            document = ET.fromstring(svg)  # noqa: S314 -- Native-generated test SVG.
            panels = tuple(
                node
                for node in document.iter()
                if node.get("data-spacing-raster-panel")
            )
            assert {panel.get("data-spacing-raster-panel") for panel in panels} == {
                "detail",
                "overview",
            }
            overview_spans = tuple(
                node
                for node in document.iter()
                if node.get("data-spacing-overview-span")
            )
            # This one approval covers both complete 20mm native tracks, while
            # the readable physical detail remains a smaller selected interval.
            assert {
                (node.get("data-board-start-nm"), node.get("data-board-end-nm"))
                for node in overview_spans
            } == {
                ("0,0", "20000000,0"),
                ("0,250000", "20000000,250000"),
            }
            assert entry.viewport_nm[2] < 20_000_000
            assert {
                node.get("data-spacing-overview-locator")
                for node in document.iter()
                if node.get("data-spacing-overview-locator")
            } == {"true", "outline"}
            assert not any(
                node.tag.rsplit("}", 1)[-1] == "text"
                for panel in panels
                for node in panel.iter()
            )
            external_labels = {
                node.get("data-spacing-label")
                for node in document.iter()
                if node.tag.rsplit("}", 1)[-1] == "text"
            }
            # Independent fixture dimensions: 0.25mm between centerlines minus
            # two 0.075mm half widths gives 0.1mm, 0.05mm short of the target.
            assert {
                "Actual gap",
                "0.1 mm",
                "Target gap",
                "0.15 mm",
                "Too close by",
                "0.05 mm",
                "Gap deviation from target",
                "33.3% below target",
            } <= external_labels
            assert all("µm" not in label for label in external_labels)
            assert not any(
                node.get("data-spacing-schematic-gap") for node in document.iter()
            )
            assert "spacing-issue" not in svg
            guide_y = {
                float(node.attrib["data-board-y-nm"])
                for node in document.iter()
                if node.get("data-impedance-spacing-guide") == "target-edge"
            }
            # Actual facing edges are y=75um and y=175um. A 150um target
            # predicts the opposite edge at y=225um / y=25um respectively.
            assert guide_y == {25_000, 225_000}
            output = renderer.render_spacing(entry, tmp_path / f"spacing-{index}.png")
            image = wx.Image(str(output), wx.BITMAP_TYPE_PNG)
            assert image.IsOk() and tuple(image.GetSize()) == (1600, 1000)
            percentage = next(
                node
                for node in document.iter()
                if node.get("data-spacing-label") == "33.3% below target"
            )
            font_size = round(float(percentage.get("font-size")))
            dc = wx.MemoryDC(wx.Bitmap(1, 1))
            try:
                dc.SetFont(
                    wx.Font(
                        wx.Size(0, font_size),
                        wx.FONTFAMILY_SWISS,
                        wx.FONTSTYLE_NORMAL,
                        wx.FONTWEIGHT_BOLD,
                    )
                )
                percentage_width, percentage_height = dc.GetTextExtent(
                    "33.3% below target"
                )
            finally:
                dc.SelectObject(wx.NullBitmap)
            percentage_x = round(float(percentage.get("x")))
            percentage_y = round(float(percentage.get("data-spacing-text-top")))
            footer_x, footer_y, footer_width, footer_height = (
                entry.composition.schematic_rect
            )
            assert font_size * entry.composition.minimum_scale >= 22
            assert (
                footer_x
                <= percentage_x
                < percentage_x + percentage_width
                <= footer_x + footer_width
            )
            assert (
                footer_y
                <= percentage_y
                < percentage_y + percentage_height
                <= footer_y + footer_height
            )
            color = percentage.get("fill")
            foreground = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
            rgb = bytes(image.GetData())
            # Inspect the native numeric glyphs in their own measured box. The
            # footer contains no copper, guides or bars that could stand in for
            # the percentage actually being painted into the immutable PNG.
            foreground_pixels = sum(
                all(
                    abs(rgb[(y * entry.width_px + x) * 3 + channel] - value) <= 12
                    for channel, value in enumerate(foreground)
                )
                for y in range(percentage_y, percentage_y + percentage_height, 2)
                for x in range(percentage_x, percentage_x + percentage_width, 2)
            )
            assert foreground_pixels >= 100, (
                "The percentage readout is absent from the reviewed PNG"
            )
            left, top, span_x, span_y = entry.viewport_nm
            detail_left, detail_top, detail_width, detail_height = (
                entry.composition.detail_rect
                if entry.composition is not None
                else (0, 0, entry.width_px, entry.height_px)
            )

            def pixel_matches(
                x: int,
                y: int,
                expected: tuple[int, int, int],
                tolerance: int = 18,
                image: Any = image,
            ) -> bool:
                """Distinguish actual copper from legitimate guide/tick pixels."""
                color = (image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y))
                return max(abs(a - b) for a, b in zip(color, expected)) <= tolerance

            overview_left, overview_top, overview_width, overview_height = (
                entry.composition.overview_rect
            )
            board_left, board_top, board_width, board_height = (
                entry.composition.overview_viewport_nm
            )
            for board_x in (1_000_000, 10_000_000, 19_000_000):
                for board_y in (0, 250_000):
                    x = overview_left + round(
                        (board_x - board_left) / board_width * overview_width
                    )
                    y = overview_top + round(
                        (board_y - board_top) / board_height * overview_height
                    )
                    assert any(
                        pixel_matches(px, py, (255, 224, 102), 30)
                        for px in range(x - 2, x + 3)
                        for py in range(y - 4, y + 5)
                    ), "The overview does not highlight the complete native run"

            def row(
                board_y: float,
                top: float = top,
                span_y: float = span_y,
                height: int = detail_height,
                offset: int = detail_top,
            ) -> int:
                """Use independent known fixture coordinates for the raster oracle."""
                return offset + round((board_y - top) / span_y * height)

            copper_rows = tuple(
                range(row(center_y - 75_000), row(center_y + 75_000))
                for center_y in (0, 250_000)
            )
            # A guide legitimately crosses a copper body. Inspect an adjacent
            # dash-free column rather than misclassifying that annotation as a
            # loss of native copper detail; avoid the center measured-gap tick.
            column = max(
                range(
                    detail_left + detail_width // 2 + 8,
                    detail_left + detail_width // 2 + 32,
                ),
                key=lambda x: min(
                    sum(pixel_matches(x, y, (255, 224, 102)) for y in rows)
                    for rows in copper_rows
                ),
            )
            assert (
                0 < left + (column - detail_left) / detail_width * span_x < 20_000_000
            )
            for rows in copper_rows:
                assert (
                    sum(pixel_matches(column, y, (255, 224, 102)) for y in rows) >= 24
                )
            gap_rows = range(row(75_000) + 1, row(175_000) - 1)
            assert len(gap_rows) >= 22
            assert not any(pixel_matches(column, y, (255, 224, 102)) for y in gap_rows)
            for board_y in (25_000, 225_000):
                assert any(
                    pixel_matches(x, y, (105, 228, 255), 60)
                    for x in range(column - 6, column + 7)
                    for y in range(row(board_y) - 2, row(board_y) + 3)
                ), "Target guide pixels do not match the declared physical spacing"
            capture = load_spacing_capture(output, renderer, entry)
            assert capture.viewport_nm is None
            assert capture.original is not None
            assert capture.original.viewport_nm == entry.viewport_nm
            assert (capture.original.width, capture.original.height) == (
                detail_width,
                detail_height,
            )
            evidence = check_spacing_guide_pixels(entry, capture, wx)
            assert evidence["guide_png_sha256"] == capture.sha256
        assert (
            renderer.render(sections[0], tmp_path / "after.png").read_bytes()
            == manufacturing
        )
    assert _native_geometry(board) == before
    assert Path(board.GetFileName()).read_bytes() == source
    assert snapshot_board(board, pcbnew) == snapshot


def test_native_pad_contact_getter_failure_cannot_authorize_terminal_exclusions(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed precise pad getter cannot silently exempt the native routing."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CHILD", native="kicad"):
        return
    pcbnew = request.getfixturevalue("native_bindings").pcbnew
    board, nets = _pair_board(pcbnew, tmp_path / "failed-contact.kicad_pcb")
    populate_native_terminal_pair(board, pcbnew, nets)
    if _unsupported_native_footprint_context(board, pcbnew):
        return
    baseline = _assess(board, pcbnew)
    frozen_features = baseline.snapshot.copper_features

    def fail_shape_position(_pad: Any, *_args: Any) -> Any:
        """Simulate an actual native layer getter becoming unavailable."""
        raise RuntimeError("Native physical-layer pad position is unavailable")

    monkeypatch.setattr(pcbnew.PAD, "ShapePos", fail_shape_position, raising=False)
    failed = _assess(board, pcbnew)
    assert failed.snapshot.copper_features == ()
    assert failed.check.transition_length_nm == 0
    assert failed.check.verdict != "match"
    assert baseline.snapshot.copper_features == frozen_features
