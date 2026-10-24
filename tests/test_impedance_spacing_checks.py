"""Geometry-oracle regressions for routed differential-pair spacing diagnostics."""

from dataclasses import replace
from math import isqrt
from typing import Any

import pytest

from impedance.matching import analyze
from impedance.model import BoardSnapshot, Config, LayerSettings, Specification, Trace
from impedance.spacing_checks import (
    NmBounds,
    SpacingCheck,
    SpacingContext,
    SpacingLimits,
    compare_pair_spacing,
    measure_pair_spacing,
    spacing_check_details,
    spacing_checks_for_sections,
    spacing_input_digest,
)


def _line(
    trace_id: str, net: str, points: tuple[tuple[int, int], ...], width: int = 150_000
) -> Trace:
    """Build ordinary native straight centerlines in integer nanometres."""
    return Trace(trace_id, "F.Cu", net, width, points)


def _snapshot(*traces: Trace) -> BoardSnapshot:
    """Provide complete class and authoritative pairing metadata."""
    return BoardSnapshot(
        ("F.Cu", "B.Cu"),
        tuple(traces),
        net_classes=("USB",),
        net_class_memberships=(("P", ("USB",)), ("N", ("USB",))),
        net_class_context_digest="native-classes",
        differential_pairs=(("P", "N"),),
    )


def _parallel(gap: int = 150_000, *, length: int = 10_000_000) -> BoardSnapshot:
    """Build two equal-width routes with independently specified edge gap."""
    return _snapshot(
        _line("p", "P", ((0, 0), (length, 0))),
        _line("n", "N", ((0, gap + 150_000), (length, gap + 150_000))),
    )


def _check(
    snapshot: BoardSnapshot,
    target: int = 150_000,
    *,
    limits: SpacingLimits = SpacingLimits(),
) -> SpacingCheck:
    """Assess all native geometry in both directions without section selection."""
    return compare_pair_spacing(
        measure_pair_spacing(snapshot, ("P", "N"), "F.Cu", limits=limits), target
    )


@pytest.mark.parametrize(
    ("gap", "verdict"),
    [
        (149_000, "match"),
        (151_000, "match"),
        (148_999, "deviation"),
        (151_001, "deviation"),
        (100_000, "deviation"),
        (250_000, "deviation"),
    ],
)
def test_inclusive_dimensional_band_with_independent_gap_oracle(
    gap: int, verdict: str
) -> None:
    """The inclusive 1,000 nm band is independent of widths and calculation results."""
    check = _check(_parallel(gap))
    assert check.verdict == verdict
    assert check.coverage == "complete"
    assert check.minimum_gap_nm == check.maximum_gap_nm == NmBounds(gap, gap)
    assert check.assessed_length_nm == 20_000_000
    assert check.unverified_length_nm == 0


def test_exact_diagonal_parallel_geometry_and_order_reversal() -> None:
    """A 3-4-5 normal offset remains 150,000 nm after rotation and reversal."""
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (3_000_000, 4_000_000))),
        _line("n", "N", ((-240_000, 180_000), (2_760_000, 4_180_000))),
    )
    check = _check(snapshot)
    assert check.verdict == "match"
    assert check.minimum_gap_nm == NmBounds(150_000, 150_000)
    reverse = replace(
        snapshot,
        traces=tuple(
            replace(t, points=tuple(reversed(t.points)))
            for t in reversed(snapshot.traces)
        ),
        differential_pairs=(("N", "P"),),
    )
    assert _check(reverse) == check


def test_segmented_parallel_mates_cover_long_source_without_sampling() -> None:
    """A long route and many native segments remain an exact constant-gap proof."""
    traces = [_line("p", "P", ((0, 0), (1_000_000_000, 0)))]
    traces.extend(
        _line(
            f"n{i:03}",
            "N",
            ((i * 10_000_000, 300_000), ((i + 1) * 10_000_000, 300_000)),
        )
        for i in range(100)
    )
    check = _check(_snapshot(*traces))
    assert check.verdict == "match"
    assert check.assessed_length_nm == 2_000_000_000


@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.parametrize("reverse_source", [False, True])
@pytest.mark.parametrize("reverse_mate", [False, True])
@pytest.mark.parametrize(
    "separation, lower, verdict",
    [
        (300_001, 149_999, "match"),
        (301_001, 150_999, "match"),
        (301_002, 151_000, "undetermined"),
        (301_003, 151_001, "deviation"),
    ],
)
def test_segmented_axis_gap_keeps_fractional_width_bounds_and_native_witnesses(
    vertical: bool,
    reverse_source: bool,
    reverse_mate: bool,
    separation: int,
    lower: int,
    verdict: str,
) -> None:
    """Unequal odd widths leave a half-nm gap through every native direction."""

    def point(along: int, across: int) -> tuple[int, int]:
        """Translate the physical route without moving its integer-width oracle."""
        x, y = (across, along) if vertical else (along, across)
        return x - 987_654_321, y + 123_456_789

    source = _line("p", "P", (point(0, 0), point(10_000_000, 0)), 150_001)
    mates = tuple(
        _line(
            f"n{index}",
            "N",
            (point(start, separation), point(end, separation)),
            150_002,
        )
        for index, (start, end) in enumerate(((0, 5_000_000), (5_000_000, 10_000_000)))
    )
    if reverse_source:
        source = replace(source, points=tuple(reversed(source.points)))
    if reverse_mate:
        mates = tuple(
            replace(trace, points=tuple(reversed(trace.points))) for trace in mates
        )
    check = _check(_snapshot(source, *mates))
    assert check.verdict == verdict
    assert check.coverage == "complete"
    assert check.minimum_gap_nm == check.maximum_gap_nm == NmBounds(lower, lower + 1)
    assert check.assessed_length_nm == 20_000_000
    assert check.unverified_length_nm == check.transition_length_nm == 0
    if verdict == "deviation":
        assert len(check.issues) == 1
        issue = check.issues[0]
        assert issue.code == "too_wide"
        assert {member.source_trace_id for member in issue.members} == {"p", "n0", "n1"}
        assert {
            identity for member in issue.members for identity in member.mate_trace_ids
        } == {"p", "n0", "n1"}
        assert all(member.cross_sections for member in issue.members)


@pytest.mark.parametrize("departure", [100_000, 250_000])
def test_short_local_departure_cannot_hide_between_samples(departure: int) -> None:
    """A 20 µm local deviation within a 100 mm route cannot report match."""
    height = departure + 150_000
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (100_000_000, 0))),
        _line("n0", "N", ((0, 300_000), (12_340_000, 300_000))),
        _line("n1", "N", ((12_340_000, 300_000), (12_350_000, height))),
        _line("n2", "N", ((12_350_000, height), (12_370_000, height))),
        _line("n3", "N", ((12_370_000, height), (12_380_000, 300_000))),
        _line("n4", "N", ((12_380_000, 300_000), (100_000_000, 300_000))),
    )
    check = _check(snapshot)
    assert check.verdict == "deviation"
    assert check.coverage == "complete"
    assert any(
        issue.code == ("too_tight" if departure < 150_000 else "too_wide")
        for issue in check.issues
    )
    value = check.minimum_gap_nm if departure < 150_000 else check.maximum_gap_nm
    assert value is not None
    assert value.lower_nm <= departure <= value.upper_nm


def test_all_mate_widths_participate_in_row_projection() -> None:
    """A wider nearby mate from another row wins over a matching-width mate."""
    snapshot = _snapshot(
        _line("p150", "P", ((0, 0), (5_000_000, 0))),
        _line("n150", "N", ((0, 300_000), (5_000_000, 300_000))),
        _line("p300", "P", ((0, -1_000_000), (5_000_000, -1_000_000)), 300_000),
        _line("n300", "N", ((0, 350_000), (5_000_000, 350_000)), 300_000),
    )
    spec = Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (LayerSettings("F.Cu", ("B.Cu",), 150_000),),
    )
    config = Config(enabled=True, specifications=(spec,))
    rows = analyze(config, snapshot).sections
    narrow = next(row for row in rows if row.width_nm == 150_000)
    result = spacing_checks_for_sections(config, snapshot, (narrow,))[narrow.section_id]
    assert result.verdict == "deviation"
    assert result.minimum_gap_nm == NmBounds(125_000, 125_000)


def _arc(trace_id: str, net: str, radius: int) -> Trace:
    """Build a native semicircle with exact center, radius and midpoint."""
    return Trace(
        trace_id,
        "F.Cu",
        net,
        150_000,
        ((radius, 0), (-radius, 0)),
        True,
        native_arc_midpoint=(0, radius),
        native_arc_center=(0, 0),
        native_arc_radius_nm=float(radius),
    )


def test_concentric_native_arc_geometry_ignores_render_chords() -> None:
    """Native semicircles with exact radius difference have exact spacing."""
    snapshot = _snapshot(_arc("p", "P", 1_000_000), _arc("n", "N", 1_300_000))
    check = _check(snapshot)
    assert check.verdict == "match"
    assert check.minimum_gap_nm == check.maximum_gap_nm == NmBounds(150_000, 150_000)
    reversed_snapshot = replace(
        snapshot,
        traces=tuple(
            replace(t, points=tuple(reversed(t.points))) for t in snapshot.traces
        ),
    )
    assert _check(reversed_snapshot).verdict == "match"


def test_missing_native_arc_metadata_never_proves_match_or_wide() -> None:
    """Unknown mate geometry cannot be replaced with drawing samples."""
    snapshot = _parallel(250_000)
    unknown = Trace(
        "unknown",
        "F.Cu",
        "N",
        150_000,
        ((0, 200_000), (5_000_000, 200_000), (10_000_000, 200_000)),
        True,
    )
    check = _check(replace(snapshot, traces=(*snapshot.traces, unknown)))
    assert check.verdict != "match"
    assert check.coverage == "partial"
    # The known N route still has a definite wide gap to a fully known P route;
    # the P route cannot call its own gap wide with unknown N geometry.
    p_only = replace(
        measure_pair_spacing(
            replace(snapshot, traces=(*snapshot.traces, unknown)), ("P", "N"), "F.Cu"
        ),
        traces=tuple(
            t
            for t in measure_pair_spacing(
                replace(snapshot, traces=(*snapshot.traces, unknown)),
                ("P", "N"),
                "F.Cu",
            ).traces
            if t.trace_id == "p"
        ),
    )
    assert compare_pair_spacing(p_only, 150_000).verdict == "undetermined"


def test_unknown_arc_cannot_erase_proven_tight_witness() -> None:
    """Partial coverage preserves a tight geometric counterexample."""
    snapshot = _parallel(100_000)
    unknown = replace(_arc("unknown", "N", 1_000_000), native_arc_midpoint=None)
    check = _check(replace(snapshot, traces=(*snapshot.traces, unknown)))
    assert check.verdict == "deviation"
    assert check.coverage == "partial"
    assert any(issue.code == "too_tight" for issue in check.issues)


@pytest.mark.parametrize(
    "extra",
    [
        _line("branch", "N", ((5_000_000, 300_000), (5_000_000, 600_000))),
        _line("competing", "N", ((0, -300_000), (10_000_000, -300_000))),
    ],
)
def test_branch_and_competing_mates_never_produce_green(extra: Trace) -> None:
    """Interior T junctions and equally near distinct legs stay unverified."""
    check = _check(replace(_parallel(), traces=(*_parallel().traces, extra)))
    assert check.verdict != "match"
    assert check.coverage == "partial"
    assert check.reasons


def test_budget_exhaustion_has_no_unqualified_match() -> None:
    """Unsupported work preserves bounded failure rather than guessing green."""
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (10_000_000, 0))),
        _line("n", "N", ((0, 300_000), (10_000_000, 400_000))),
    )
    check = _check(snapshot, limits=SpacingLimits(max_evaluations=1, max_intervals=1))
    assert check.verdict == "undetermined"
    assert check.coverage == "none"
    assert any("work limit" in reason for reason in check.reasons)


def test_signed_overlap_zero_lengths_locations_and_deltas_format() -> None:
    """Shared formatter handles values rejected by positive fabrication fields."""
    details = spacing_check_details(_check(_parallel(-50_000)))
    assert "-0.05 mm" in details
    assert "unverified: 0 mm" in details
    assert "-0.2 mm" in details
    assert "+0.1 mm" in spacing_check_details(_check(_parallel(250_000)))


def test_context_caches_measurement_independently_of_draft_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draft targets can change without stackup/reference fields or new geometry."""
    import impedance.spacing_checks as checks

    snapshot = _parallel()
    spec = Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (LayerSettings("F.Cu", ("B.Cu",), 150_000),),
    )
    config = Config(enabled=True, specifications=(spec,))
    rows = analyze(config, snapshot).sections
    calls = []
    original = checks.measure_pair_spacing

    def measured(*args: Any, **kwargs: Any) -> Any:
        """Record expensive geometry calls while preserving production behavior."""
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(checks, "measure_pair_spacing", measured)
    context = SpacingContext(snapshot)
    assert (
        spacing_checks_for_sections(config, snapshot, rows, context=context)[
            rows[0].section_id
        ].verdict
        == "match"
    )
    draft = replace(
        spec, target_ohms="", layer_settings=(LayerSettings("F.Cu", (), None),)
    )
    assert (
        spacing_checks_for_sections(
            replace(config, specifications=(draft,)), snapshot, rows, context=context
        )[rows[0].section_id].verdict
        == "needs_target"
    )
    assert len(calls) == 1


def test_input_digest_changes_for_exact_arc_metadata_but_not_direction() -> None:
    """Review inputs bind native geometry absent from display-sample identity."""
    snapshot = _snapshot(_arc("p", "P", 1_000_000), _arc("n", "N", 1_300_000))
    spec = Specification("usb", "USB", "90", "differential", "USB")
    digest = spacing_input_digest(snapshot, spec, "F.Cu")
    assert digest == spacing_input_digest(
        replace(
            snapshot,
            traces=tuple(
                replace(t, points=tuple(reversed(t.points))) for t in snapshot.traces
            ),
        ),
        spec,
        "F.Cu",
    )
    assert digest != spacing_input_digest(
        replace(
            snapshot,
            traces=(
                replace(snapshot.traces[0], native_arc_midpoint=(0, 999_999)),
                snapshot.traces[1],
            ),
        ),
        spec,
        "F.Cu",
    )


def test_fractional_parallel_distance_reports_outward_bounds() -> None:
    """Irrational 45-degree separation is bounded rather than rounded to match."""
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (1_000_000, 1_000_000))),
        _line("n", "N", ((-200_000, 200_000), (800_000, 1_200_000))),
    )
    check = _check(snapshot)
    lower = isqrt(80_000_000_000) - 150_000
    assert check.minimum_gap_nm == NmBounds(lower, lower + 1)


@pytest.mark.parametrize("switch", [5_000_000, 7_123_456])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_disconnected_opposite_leg_switch_is_unverified_between_samples(
    switch: int, reversed_order: bool
) -> None:
    """A discontinuous mate switch cannot depend on hitting an exact tie sample."""
    traces = (
        _line("p", "P", ((0, 0), (20_000_000, 0))),
        _line("n0", "N", ((0, 300_000), (switch, 300_000))),
        _line("n1", "N", ((switch, -300_000), (20_000_000, -300_000))),
    )
    if reversed_order:
        traces = tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            for trace in reversed(traces)
        )
    check = _check(_snapshot(*traces))
    assert check.verdict == "undetermined"
    assert check.coverage == "partial"
    assert any("Disconnected" in reason for reason in check.reasons)


def test_connected_interior_crossing_is_unverified() -> None:
    """Even a continuous same-net path is ambiguous when its interiors cross."""
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (20_000_000, 0))),
        _line("n0", "N", ((0, 300_000), (10_000_000, 1_300_000))),
        _line("n1", "N", ((10_000_000, 1_300_000), (0, 1_300_000))),
        _line("n2", "N", ((0, 1_300_000), (10_000_000, 300_000))),
    )
    check = _check(snapshot)
    assert check.coverage == "partial"
    assert check.verdict != "match"
    assert any("crossing" in reason for reason in check.reasons)


@pytest.mark.parametrize(
    "distance, junction", [(0.0, True), (0.001, True), (0.0011, False)]
)
def test_clipped_endpoint_topology_keeps_inclusive_contact_tolerance(
    distance: float, junction: bool
) -> None:
    """A sub-nm sliced endpoint on a leaf-box edge still counts as a junction."""
    from impedance.spacing_checks import _Budget, _Primitive, _topology_reasons

    points = ((0, 0), (10, 0), (10, 10), (5, 10), (5, distance))
    primitives = tuple(
        _Primitive(_line(f"p{index}", "P", (start, end)), start, end)
        for index, (start, end) in enumerate(zip(points, points[1:]))
    )
    reasons = _topology_reasons(primitives, _Budget(SpacingLimits()))
    assert bool(reasons) is junction
    if junction:
        assert any("junction" in reason for reason in reasons)


def test_parallel_fastpath_and_topology_preprocessing_respect_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Analytic proofs and topology scans share the cap with sampled geometry."""
    import impedance.spacing_checks as checks

    snapshot = _parallel()
    assert _check(snapshot, limits=SpacingLimits(max_evaluations=0)).coverage == "none"
    traces = tuple(
        _line(f"{net}{i:03}", net, ((i * 100_000, y), ((i + 1) * 100_000, y)))
        for net, y in (("P", 0), ("N", 300_000))
        for i in range(500)
    )
    calls = []
    original = checks._Budget.consume

    def consume(budget: Any) -> None:
        """Count every accounted operation while retaining the production stop."""
        calls.append(None)
        original(budget)

    monkeypatch.setattr(checks._Budget, "consume", consume)
    check = _check(_snapshot(*traces), limits=SpacingLimits(max_evaluations=2_000))
    assert check.verdict == "undetermined"
    assert check.coverage == "none"
    assert len(calls) == 2_001


@pytest.mark.parametrize("reverse_source", [False, True])
@pytest.mark.parametrize("reverse_mate", [False, True])
def test_native_semicircle_spacing_is_independent_of_each_track_direction(
    reverse_source: bool, reverse_mate: bool
) -> None:
    """Each native semicircle may be reversed independently with unchanged midpoint."""
    source = Trace(
        "p",
        "F.Cu",
        "P",
        150_000,
        ((0, 2_000_000), (0, -2_000_000)),
        True,
        native_arc_midpoint=(2_000_000, 0),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_000_000.0,
    )
    mate = Trace(
        "n",
        "F.Cu",
        "N",
        150_000,
        ((0, 2_300_000), (0, -2_300_000)),
        True,
        native_arc_midpoint=(2_300_000, 0),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_300_000.0,
    )
    if reverse_source:
        source = replace(source, points=tuple(reversed(source.points)))
    if reverse_mate:
        mate = replace(mate, points=tuple(reversed(mate.points)))
    check = _check(_snapshot(source, mate))
    assert check.verdict == "match"
    assert check.coverage == "complete"
    assert check.minimum_gap_nm == check.maximum_gap_nm == NmBounds(150_000, 150_000)


def test_native_concentric_arcs_support_segmented_mate_and_rotated_integer_circle() -> (
    None
):
    """Rational-circle points give an independent exact-radius oracle after rotation."""

    # Each circle has radius 5*scale; increasing scale by60,000 adds300,000nm.
    def point(x: int, y: int, scale: int) -> tuple[int, int]:
        """Rotate integer 3-4-5 circle points by an exact quarter turn."""
        return x * scale, y * scale

    p = Trace(
        "p",
        "F.Cu",
        "P",
        150_000,
        (point(3, 4, 400_000), point(-3, -4, 400_000)),
        True,
        native_arc_midpoint=point(-4, 3, 400_000),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_000_000.0,
    )
    # The first quarter uses another rational point on the same circle between
    # its endpoints: (0,5). The second quarter uses(-5,0).
    n0 = Trace(
        "n0",
        "F.Cu",
        "N",
        150_000,
        (point(3, 4, 460_000), point(-4, 3, 460_000)),
        True,
        native_arc_midpoint=point(0, 5, 460_000),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_300_000.0,
    )
    n1 = Trace(
        "n1",
        "F.Cu",
        "N",
        150_000,
        (point(-3, -4, 460_000), point(-4, 3, 460_000)),
        True,
        native_arc_midpoint=point(-5, 0, 460_000),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_300_000.0,
    )
    check = _check(_snapshot(p, n0, n1))
    assert check.verdict == "match"
    assert check.coverage == "complete"
    assert check.minimum_gap_nm == check.maximum_gap_nm == NmBounds(150_000, 150_000)


def test_snapped_native_center_cannot_be_replaced_by_exact_circumcircle() -> None:
    """Integer KiCad center changes the radius when three points imply a half-nm center."""
    # A rational circumcircle would give center(0.5,0) and gap151000nm,
    # but native center(1,0) gives start radii2500002 and2801003nm.
    source = Trace(
        "p",
        "F.Cu",
        "P",
        150_000,
        ((2_500_003, 0), (-2_500_002, 0)),
        True,
        native_arc_midpoint=(1_500_002, 2_000_002),
        native_arc_center=(1, 0),
        native_arc_radius_nm=2_500_002.0,
    )
    mate = Trace(
        "n",
        "F.Cu",
        "N",
        150_000,
        ((-2_801_002, 0), (2_801_003, 0)),
        True,
        native_arc_midpoint=(1_680_602, 2_240_802),
        native_arc_center=(1, 0),
        native_arc_radius_nm=2_801_003.0,
    )
    check = _check(_snapshot(source, mate))
    assert check.verdict == "undetermined"
    assert check.coverage == "none"
    assert check.minimum_gap_nm is None
    assert any("Native arc" in reason for reason in check.reasons)


@pytest.mark.parametrize(
    "missing", ["native_arc_midpoint", "native_arc_center", "native_arc_radius_nm"]
)
def test_all_authoritative_native_arc_metadata_is_required(missing: str) -> None:
    """Older midpoint-only snapshots do not establish an authoritative native circle."""
    source = replace(_arc("p", "P", 2_000_000), **{missing: None})
    check = _check(_snapshot(source, _arc("n", "N", 2_300_000)))
    assert check.verdict == "undetermined"
    assert check.coverage == "none"


def test_connected_line_crossing_native_arc_interior_stays_unverified() -> None:
    """Curved topology cannot silently pass the line-only intersection proof."""
    arc = Trace(
        "p0",
        "F.Cu",
        "P",
        150_000,
        ((0, 2_000_000), (0, -2_000_000)),
        True,
        native_arc_midpoint=(2_000_000, 0),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_000_000.0,
    )
    points = (
        (0, -2_000_000),
        (-1_000_000, -3_000_000),
        (-1_000_000, 3_000_000),
        (1_000_000, 3_000_000),
        (1_000_000, 1_000_000),
    )
    lines = tuple(
        _line(f"p{i + 1}", "P", (a, b))
        for i, (a, b) in enumerate(zip(points, points[1:]))
    )
    mate = _arc("n", "N", 2_300_000)
    check = _check(_snapshot(arc, *lines, mate))
    assert check.verdict != "match"
    assert check.coverage != "complete"
    assert any("curved same-net geometry" in reason for reason in check.reasons)


def test_adjacent_arc_line_cannot_hide_second_interior_contact() -> None:
    """A shared endpoint does not excuse a line that recrosses the arc interior."""
    arc = Trace(
        "p0",
        "F.Cu",
        "P",
        150_000,
        ((0, 2_000_000), (0, -2_000_000)),
        True,
        native_arc_midpoint=(2_000_000, 0),
        native_arc_center=(0, 0),
        native_arc_radius_nm=2_000_000.0,
    )
    line = _line("p1", "P", ((0, -2_000_000), (3_000_000, 0)))
    check = _check(_snapshot(arc, line, _arc("n", "N", 2_300_000)))
    assert check.coverage != "complete"
    assert any("second contact" in reason for reason in check.reasons)


def test_cached_width_rows_preserve_their_own_spacing_evidence() -> None:
    """A cached whole-pair deviation cannot leak into a matching narrower row."""
    snapshot = _snapshot(
        _line("p150", "P", ((0, 0), (5_000_000, 0))),
        _line("n150", "N", ((0, 300_000), (5_000_000, 300_000))),
        _line("p300", "P", ((0, 2_000_000), (5_000_000, 2_000_000)), 300_000),
        _line("n300", "N", ((0, 2_600_000), (5_000_000, 2_600_000)), 300_000),
    )
    context = SpacingContext(snapshot)
    whole = context.check(("P", "N"), "F.Cu", 150_000)
    narrow = context.check(
        ("P", "N"), "F.Cu", 150_000, source_ids=frozenset(("p150", "n150"))
    )
    wide = context.check(
        ("P", "N"), "F.Cu", 150_000, source_ids=frozenset(("p300", "n300"))
    )
    assert whole.verdict == wide.verdict == "deviation"
    assert narrow.minimum_gap_nm == narrow.maximum_gap_nm == NmBounds(150_000, 150_000)
    assert narrow.issues == ()
    assert wide.minimum_gap_nm == wide.maximum_gap_nm == NmBounds(300_000, 300_000)
    assert (
        context.check(
            ("N", "P"), "F.Cu", 150_000, source_ids=frozenset(("n150", "p150"))
        )
        is narrow
    )
    assert (
        context.check(
            ("P", "N"), "F.Cu", 149_500, source_ids=frozenset(("p150", "n150"))
        ).issues
        == ()
    )
    assert (
        context.check(
            ("P", "N"),
            "F.Cu",
            149_500,
            source_ids=frozenset(("p150", "n150")),
            comparison_band_nm=100,
        ).verdict
        == "deviation"
    )


def test_arc_endpoints_cannot_prune_a_closer_midspan_mate() -> None:
    """Distance to a curved mate can peak inside a straight native source span."""
    from impedance.spacing_checks import _Budget, _candidate_mates, _primitives, _tree

    source = _primitives(_line("p", "P", ((-1_000_000, 0), (1_000_000, 0))))[0]
    curved = _primitives(_arc("curved", "N", 1_000_000))[0]
    nearby = _primitives(
        _line("near", "N", ((-1_000_000, 300_000), (1_000_000, 300_000)))
    )[0]
    for indexed in (False, True):
        # Extra remote native segments exercise the spatial-index path too.
        far = (
            tuple(
                _primitives(
                    _line(
                        f"far{i}",
                        "N",
                        (
                            (10_000_000 + i * 500_000, 300_000),
                            (10_500_000 + i * 500_000, 300_000),
                        ),
                    )
                )[0]
                for i in range(8)
            )
            if indexed
            else ()
        )
        mates = (curved, nearby, *far)
        budget = _Budget(SpacingLimits())
        retained = _candidate_mates(
            source, mates, budget, index=_tree(mates, budget) if indexed else None
        )
        assert nearby in retained
