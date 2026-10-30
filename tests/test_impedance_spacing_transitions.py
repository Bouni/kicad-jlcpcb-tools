"""Terminal fanouts are excluded without hiding established routed defects."""

from dataclasses import replace
from math import sqrt
from typing import Optional

import pytest

from impedance.model import BoardSnapshot, CopperFeature, Trace
from impedance.spacing_checks import (
    SpacingCheck,
    compare_pair_spacing,
    measure_pair_spacing,
)


def _trace(identity: str, net: str, points: tuple[tuple[int, int], ...]) -> Trace:
    return Trace(identity, "F.Cu", net, 150_000, points)


def _pad(identity: str, net: str, center: tuple[int, int]) -> CopperFeature:
    return CopperFeature(
        identity, net, "F.Cu", "pad", "circle", center, 400_000, 400_000
    )


def _fanout(gap: int = 150_000, *, leadin: bool = False) -> BoardSnapshot:
    offset = gap + 150_000
    p = ((0, 0), (1_000_000, 0), (2_000_000, 500_000), (10_000_000, 500_000))
    n = (
        (0, 2_000_000),
        (1_000_000, 2_000_000),
        (2_000_000, 500_000 + offset),
        (10_000_000, 500_000 + offset),
    )
    if not leadin:
        p = (p[0], *p[2:])
        n = (n[0], *n[2:])
    return BoardSnapshot(
        ("F.Cu",),
        (_trace("p", "P", p), _trace("n", "N", n)),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p[0]), _pad("np", "N", n[0])),
    )


def _check(snapshot: BoardSnapshot) -> SpacingCheck:
    return compare_pair_spacing(
        measure_pair_spacing(snapshot, ("P", "N"), "F.Cu"), 150_000
    )


def test_pad_fanout_is_not_reported_as_routed_spacing_issue() -> None:
    """Pad fanout is not reported as routed spacing issue."""
    check = _check(_fanout())
    assert check.verdict == "match"
    assert check.coverage == "complete"
    assert check.unverified_length_nm == 0
    assert check.transition_length_nm > 4_000_000
    assert not check.issues


def test_parallel_pad_pitch_leadin_does_not_end_transition_early() -> None:
    """Parallel pad pitch leadin does not end transition early."""
    check = _check(_fanout(leadin=True))
    assert check.verdict == "match"
    assert check.transition_length_nm > 4_000_000
    assert not check.issues


def test_uniformly_wrong_trunk_after_fanout_remains_actionable() -> None:
    """Uniformly wrong trunk after fanout remains actionable."""
    check = _check(_fanout(250_000))
    assert check.verdict == "deviation"
    assert check.issues
    assert all(issue.location is not None for issue in check.issues)
    assert all(
        min(point[0] for point in issue.location.source_points) >= 2_000_000
        for issue in check.issues
    )


def test_nearby_wrong_net_pad_does_not_exempt_fanout() -> None:
    """Nearby wrong net pad does not exempt fanout."""
    snapshot = _fanout()
    snapshot = replace(
        snapshot,
        copper_features=tuple(
            replace(feature, net="OTHER") for feature in snapshot.copper_features
        ),
    )
    assert _check(snapshot).verdict == "deviation"


def test_wrong_gap_after_pad_region_inside_one_native_line_remains_checked() -> None:
    """A later established trunk cannot extend the explicit landing-pad region."""
    snapshot = _fanout()
    p = _trace("p", "P", ((0, 500_000), (10_000_000, 500_000)))
    snapshot = replace(
        snapshot,
        traces=(p, snapshot.traces[1]),
        copper_features=(_pad("pp", "P", p.points[0]), snapshot.copper_features[1]),
    )
    check = _check(snapshot)
    assert check.verdict == "deviation"
    assert 0 < check.transition_length_nm < 4_000_000
    assert check.assessed_length_nm > 16_000_000
    assert any(issue.location is not None for issue in check.issues)


def test_segmenting_native_trunk_cannot_change_transition_exclusion() -> None:
    """Segmenting native trunk cannot change transition exclusion."""
    original = _fanout(250_000)
    p, n = original.traces
    split = replace(
        original,
        traces=(
            p,
            replace(
                n, trace_id="n0", points=n.points[:-1] + ((4_000_000, n.points[-1][1]),)
            ),
            replace(
                n,
                trace_id="n1",
                points=((4_000_000, n.points[-1][1]), (7_000_000, n.points[-1][1])),
            ),
            replace(
                n, trace_id="n2", points=((7_000_000, n.points[-1][1]), n.points[-1])
            ),
        ),
    )
    before, after = _check(original), _check(split)
    assert before.verdict == after.verdict == "deviation"
    assert before.assessed_length_nm == after.assessed_length_nm
    assert before.transition_length_nm == after.transition_length_nm
    assert before.minimum_gap_nm == after.minimum_gap_nm
    assert before.maximum_gap_nm == after.maximum_gap_nm


def test_interior_testpoint_does_not_hide_routing_issue() -> None:
    """Interior testpoint does not hide routing issue."""
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            _trace("p", "P", ((0, 0), (10_000_000, 0))),
            _trace("n", "N", ((0, 400_000), (10_000_000, 400_000))),
        ),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("tp", "P", (5_000_000, 0)),),
    )
    check = _check(snapshot)
    assert check.verdict == "deviation"
    assert check.transition_length_nm == 0
    assert check.assessed_length_nm == 20_000_000


def test_two_terminal_transitions_can_share_one_native_line() -> None:
    """Two terminal transitions can share one native line."""
    p = _trace("p", "P", ((0, 500_000), (12_000_000, 500_000)))
    n = _trace(
        "n",
        "N",
        (
            (0, 2_000_000),
            (2_000_000, 800_000),
            (10_000_000, 800_000),
            (12_000_000, 2_000_000),
        ),
    )
    features = tuple(
        _pad(net + str(i), net, trace.points[i])
        for net, trace in (("P", p), ("N", n))
        for i in (0, -1)
    )
    check = _check(
        BoardSnapshot(
            ("F.Cu",),
            (p, n),
            differential_pairs=(("P", "N"),),
            copper_features=features,
        )
    )
    assert check.verdict == "deviation"
    assert check.assessed_length_nm > 16_000_000
    assert 0 < check.transition_length_nm < 8_000_000


def test_transition_only_rows_receive_quiet_informational_result() -> None:
    """Transition only rows receive quiet informational result."""
    p, n = (
        _trace("p", "P", ((0, 0), (100_000, 0))),
        _trace("n", "N", ((0, 2_000_000), (100_000, 2_000_000))),
    )
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", (0, 0)), _pad("np", "N", (0, 2_000_000))),
    )
    check = _check(snapshot)
    assert check.verdict == "not_applicable"
    assert check.coverage == "none"
    assert check.assessed_length_nm == check.unverified_length_nm == 0
    assert check.transition_length_nm == 200_000
    assert not check.issues


def test_two_separated_routed_defects_have_separate_visual_locations() -> None:
    """Two separated routed defects have separate visual locations."""
    p = _trace("p", "P", ((0, 0), (20_000_000, 0)))
    n = _trace(
        "n",
        "N",
        (
            (0, 300_000),
            (4_000_000, 300_000),
            (4_100_000, 400_000),
            (5_000_000, 400_000),
            (5_100_000, 300_000),
            (14_000_000, 300_000),
            (14_100_000, 400_000),
            (15_000_000, 400_000),
            (15_100_000, 300_000),
            (20_000_000, 300_000),
        ),
    )
    check = _check(BoardSnapshot(("F.Cu",), (p, n), differential_pairs=(("P", "N"),)))
    assert check.verdict == "deviation"
    assert len(check.issues) == 2
    assert all(issue.location is not None for issue in check.issues)
    assert check.issues[0].location.bounds[2] < check.issues[1].location.bounds[0]


def test_terminal_fanout_entirely_inside_region_needs_no_later_parallel_run() -> None:
    """The explicit pad region alone can prove a fully contained entry exempt."""
    snapshot = _fanout()
    snapshot = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=trace.points[:-1]) for trace in snapshot.traces
        ),
    )
    check = _check(snapshot)
    assert check.verdict == "not_applicable"
    assert check.unverified_length_nm == 0
    assert check.assessed_length_nm == 0
    assert not check.issues


def test_large_native_arc_issue_box_contains_all_angular_extrema() -> None:
    """A270-degree arc must not lose extrema between ordinary three samples."""

    def arc(identity: str, net: str, radius: int) -> Trace:
        return Trace(
            identity,
            "F.Cu",
            net,
            150_000,
            ((radius, 0), (0, -radius)),
            True,
            native_arc_midpoint=(-radius, 0),
            native_arc_center=(0, 0),
            native_arc_radius_nm=float(radius),
        )

    p, n = arc("p", "P", 1_000_000), arc("n", "N", 1_400_000)
    snapshot = BoardSnapshot(("F.Cu",), (p, n), differential_pairs=(("P", "N"),))
    check = _check(snapshot)
    assert check.verdict == "deviation"
    assert len(check.issues) == 1
    bounds = check.issues[0].location.bounds
    assert bounds[0] <= -1_475_000
    assert bounds[1] <= -1_475_000
    assert bounds[2] >= 1_475_000
    assert bounds[3] >= 1_475_000


def test_pad_contact_boundaries_align_inside_native_concentric_arcs() -> None:
    """Different native radii exit equal-size terminal pads at different angles."""

    def arc(identity: str, net: str, radius: int) -> Trace:
        return Trace(
            identity,
            "F.Cu",
            net,
            150_000,
            ((radius, 0), (-radius, 0)),
            True,
            native_arc_midpoint=(0, radius),
            native_arc_center=(0, 0),
            native_arc_radius_nm=float(radius),
        )

    p, n = arc("p", "P", 1_000_000), arc("n", "N", 1_300_000)
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0])),
    )
    check = _check(snapshot)
    assert check.verdict == "match"
    assert check.coverage == "complete"
    assert check.transition_length_nm > 500_000


def test_four_terminal_pads_cannot_hide_long_divergent_routing() -> None:
    """Pad contacts at both ends do not exempt an incorrect ten-millimetre trunk."""
    p = _trace("p", "P", ((0, 0), (10_000_000, 0)))
    n = _trace("n", "N", ((0, 300_000), (10_000_000, 400_000)))
    features = tuple(
        _pad(net + str(index), net, trace.points[index])
        for net, trace in (("P", p), ("N", n))
        for index in (0, -1)
    )
    check = _check(
        BoardSnapshot(
            ("F.Cu",),
            (p, n),
            differential_pairs=(("P", "N"),),
            copper_features=features,
        )
    )
    assert check.verdict == "deviation"
    assert check.assessed_length_nm > 10_000_000
    assert check.unverified_length_nm == 0
    assert check.transition_length_nm < 2_300_000
    assert any(
        issue.code == "too_wide" and issue.location is not None
        for issue in check.issues
    )


def test_rotated_rectangular_terminal_uses_actual_copper_contact() -> None:
    """A rotated narrow flash must not act as its larger bounding rectangle."""
    from impedance.copper_geometry import feature_point_distance

    pad = CopperFeature(
        "rotated",
        "P",
        "F.Cu",
        "pad",
        "rect",
        (0, 0),
        2_000_000,
        400_000,
        rotation_deg=45,
    )
    assert feature_point_distance((700_000, -700_000), pad) < 0
    assert feature_point_distance((700_000, 700_000), pad) > 500_000


def test_custom_pad_hole_and_offset_have_real_copper_semantics() -> None:
    """Pad anchors and bounding rectangles do not turn native holes into copper."""
    from impedance.copper_geometry import feature_point_distance

    outer = (
        (-500_000, -500_000),
        (500_000, -500_000),
        (500_000, 500_000),
        (-500_000, 500_000),
    )
    hole = (
        (-200_000, -200_000),
        (200_000, -200_000),
        (200_000, 200_000),
        (-200_000, 200_000),
    )
    feature = CopperFeature(
        "offset",
        "P",
        "F.Cu",
        "pad",
        "polygon",
        (100_000_000, 0),
        1_000_000,
        1_000_000,
        polygons=((outer, hole),),
    )
    assert feature_point_distance((0, 0), feature) == 200_000
    assert feature_point_distance((300_000, 0), feature) < 0
    assert feature_point_distance((100_000_000, 0), feature) > 90_000_000


def test_native_polygon_contact_work_counts_every_contour_edge() -> None:
    """Complex native copper contours consume the same deterministic work budget."""
    from impedance.spacing_checks import SpacingLimits

    snapshot = _fanout()
    contour = tuple((index, index % 2) for index in range(2_000))
    feature = CopperFeature(
        "complex",
        "P",
        "F.Cu",
        "pad",
        "polygon",
        (0, 0),
        400_000,
        400_000,
        polygons=((contour,),),
    )
    snapshot = replace(snapshot, copper_features=(feature, snapshot.copper_features[1]))
    check = compare_pair_spacing(
        measure_pair_spacing(
            snapshot, ("P", "N"), "F.Cu", limits=SpacingLimits(max_evaluations=100)
        ),
        150_000,
    )
    assert check.verdict == "undetermined"
    assert check.coverage == "none"
    assert any("work limit" in reason for reason in check.reasons)


def test_long_shallow_fanout_outside_pad_region_is_assessed() -> None:
    """A later parallel trunk cannot justify ignoring a long shallow lead-in."""
    p = _trace("p", "P", ((0, 0), (2_000_000, 500_000), (10_000_000, 500_000)))
    n = _trace("n", "N", ((0, 400_000), (2_000_000, 800_000), (10_000_000, 800_000)))
    features = tuple(
        CopperFeature(
            net + "pad", net, "F.Cu", "pad", "rect", trace.points[0], 400_000, 150_000
        )
        for net, trace in (("P", p), ("N", n))
    )
    check = _check(
        BoardSnapshot(
            ("F.Cu",),
            (p, n),
            differential_pairs=(("P", "N"),),
            copper_features=features,
        )
    )
    assert check.verdict == "deviation"
    assert check.coverage == "complete"
    assert check.assessed_length_nm > 18_000_000
    assert 0 < check.transition_length_nm < 1_500_000


def test_budget_exhaustion_keeps_only_point_sized_confirmed_issue_locations() -> None:
    """A proven witness stays navigable without boxing its unresolved surroundings."""
    from impedance.spacing_checks import SpacingLimits

    p = _trace("p", "P", ((0, 0), (10_000_000, 0)))
    n = _trace("n", "N", ((0, 250_000), (10_000_000, 350_000)))
    snapshot = BoardSnapshot(("F.Cu",), (p, n), differential_pairs=(("P", "N"),))
    check = compare_pair_spacing(
        measure_pair_spacing(
            snapshot, ("P", "N"), "F.Cu", limits=SpacingLimits(max_evaluations=100)
        ),
        150_000,
    )
    assert check.coverage == "partial"
    assert check.issues
    assert all(
        issue.location is not None
        and issue.location.source_interval[0] == issue.location.source_interval[1]
        for issue in check.issues
    )


def test_constant_arc_issue_location_excludes_far_radial_mate() -> None:
    """Only the proven nearest radial path belongs in a local visual issue box."""

    def arc(identity: str, net: str, radius: int) -> Trace:
        return Trace(
            identity,
            "F.Cu",
            net,
            150_000,
            ((radius, 0), (-radius, 0)),
            True,
            native_arc_midpoint=(0, radius),
            native_arc_center=(0, 0),
            native_arc_radius_nm=float(radius),
        )

    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            arc("p", "P", 1_000_000),
            arc("near", "N", 1_400_000),
            arc("far", "N", 10_000_000),
        ),
        differential_pairs=(("P", "N"),),
    )
    measurement = measure_pair_spacing(snapshot, ("P", "N"), "F.Cu")
    p_measurement = replace(
        measurement,
        traces=tuple(trace for trace in measurement.traces if trace.trace_id == "p"),
    )
    check = compare_pair_spacing(p_measurement, 150_000)
    assert check.verdict == "deviation"
    assert len(check.issues) == 1
    location = check.issues[0].location
    assert location is not None
    assert location.mate_trace_ids == ("near",)
    assert location.bounds[0] >= -1_475_000
    assert location.bounds[2] <= 1_475_000
    assert location.bounds[3] <= 1_475_000


def test_constant_diagonal_issue_location_excludes_far_parallel_mate() -> None:
    """Overlapping spatial boxes cannot add a distant parallel route to a witness."""
    p = _trace("p", "P", ((0, 0), (30_000_000, 40_000_000)))
    near = _trace("near", "N", ((-320_000, 240_000), (29_680_000, 40_240_000)))
    far = _trace("far", "N", ((-8_000_000, 6_000_000), (22_000_000, 46_000_000)))
    snapshot = BoardSnapshot(
        ("F.Cu",), (p, near, far), differential_pairs=(("P", "N"),)
    )
    measurement = measure_pair_spacing(snapshot, ("P", "N"), "F.Cu")
    p_measurement = replace(
        measurement,
        traces=tuple(trace for trace in measurement.traces if trace.trace_id == "p"),
    )
    check = compare_pair_spacing(p_measurement, 150_000)
    assert check.verdict == "deviation"
    assert len(check.issues) == 1
    location = check.issues[0].location
    assert location is not None
    assert location.mate_trace_ids == ("near",)
    assert location.bounds[0] > -500_000
    assert location.bounds[3] < 40_500_000


def test_pad_pitch_region_does_not_have_a_two_millimetre_leadin_cliff() -> None:
    """A 20 um lead-in edit wholly inside the same pad region changes no warning."""
    checks = []
    for lead in (1_990_000, 2_010_000):
        p = _trace(
            "p", "P", ((0, 0), (lead, 0), (2_100_000, 500_000), (10_000_000, 500_000))
        )
        n = _trace(
            "n",
            "N",
            (
                (0, 2_000_000),
                (lead, 2_000_000),
                (2_100_000, 800_000),
                (10_000_000, 800_000),
            ),
        )
        checks.append(
            _check(
                BoardSnapshot(
                    ("F.Cu",),
                    (p, n),
                    differential_pairs=(("P", "N"),),
                    copper_features=(
                        _pad("pp", "P", p.points[0]),
                        _pad("np", "N", n.points[0]),
                    ),
                )
            )
        )
    assert [check.verdict for check in checks] == ["match", "match"]
    assert all(not check.issues for check in checks)


def test_widening_pad_entry_is_excluded_inside_landing_pitch_region() -> None:
    """Pads can be closer than the routed pair without making entry a defect."""
    p = _trace("p", "P", ((0, 0), (200_000, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 200_000), (200_000, 300_000), (5_000_000, 300_000)))
    pads = tuple(
        CopperFeature(
            net + "pad", net, "F.Cu", "pad", "rect", trace.points[0], 150_000, 150_000
        )
        for net, trace in (("P", p), ("N", n))
    )
    check = _check(
        BoardSnapshot(
            ("F.Cu",), (p, n), differential_pairs=(("P", "N"),), copper_features=pads
        )
    )
    assert check.verdict == "match"
    assert check.coverage == "complete"
    assert not check.issues


def test_pad_entry_cannot_exempt_two_hundred_millimetre_divergence() -> None:
    """Finding a later parallel run cannot exempt routing beyond one pad pitch."""
    p = _trace("p", "P", ((0, 0), (202_000_000, 0), (210_000_000, 0)))
    n = _trace(
        "n", "N", ((0, 2_000_000), (202_000_000, 400_000), (210_000_000, 400_000))
    )
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0])),
    )
    from impedance.spacing_checks import SpacingLimits

    check = compare_pair_spacing(
        measure_pair_spacing(
            snapshot, ("P", "N"), "F.Cu", limits=SpacingLimits(max_evaluations=5_000)
        ),
        150_000,
    )
    assert check.verdict == "deviation"
    assert check.transition_length_nm < 5_000_000
    assert check.assessed_length_nm + check.unverified_length_nm > 400_000_000
    assert check.coverage == "partial"
    assert check.unverified_length_nm > 0
    assert check.issues


def test_splitting_copper_inside_pad_preserves_full_contact_exclusion() -> None:
    """A degree-two vertex inside copper cannot stop continuous pad contact."""
    p = _trace("p", "P", ((0, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0])),
    )
    split = replace(
        snapshot,
        traces=tuple(
            _trace(trace.trace_id + suffix, trace.net, points)
            for trace in (p, n)
            for suffix, points in (
                ("0", (trace.points[0], (100_000, trace.points[0][1]))),
                ("1", ((100_000, trace.points[0][1]), trace.points[-1])),
            )
        ),
    )
    before, after = _check(snapshot), _check(split)
    assert abs(before.transition_length_nm - after.transition_length_nm) <= 2
    assert before.assessed_length_nm == after.assessed_length_nm


def test_local_projection_onto_large_mate_arc_does_not_include_whole_arc() -> None:
    """A short witness beside an arc needs a close-up around its radial projection."""
    from impedance.spacing_checks import _location, _primitives

    source = _trace("p", "P", ((0, 10_400_000), (10_000, 10_400_000)))
    mate = Trace(
        "n",
        "F.Cu",
        "N",
        150_000,
        ((10_000_000, 0), (-10_000_000, 0)),
        True,
        native_arc_midpoint=(0, 10_000_000),
        native_arc_center=(0, 0),
        native_arc_radius_nm=10_000_000.0,
    )
    location = _location(_primitives(source)[0], _primitives(mate), 0, 1)
    assert location.bounds[2] - location.bounds[0] < 200_000
    assert location.bounds[3] - location.bounds[1] < 600_000


def test_reciprocal_trace_ids_and_overlapping_boxes_do_not_merge_separate_spans() -> (
    None
):
    """A curved route's broad rectangle cannot identify matching copper intervals."""
    from impedance.spacing_checks import (
        NmBounds,
        SpacingIssue,
        SpacingLocation,
        _merge_issues,
    )

    first = SpacingLocation(
        "p",
        ("n",),
        (0.1, 0.2),
        ((0, 0), (10, 0)),
        ((0, 10), (10, 10)),
        (-100, -100, 100, 100),
        (("n", 0.1, 0.2),),
    )
    second = SpacingLocation(
        "n",
        ("p",),
        (0.8, 0.9),
        ((50, 50), (60, 50)),
        ((50, 40), (60, 40)),
        (-100, -100, 100, 100),
        (("p", 0.8, 0.9),),
    )
    issues = tuple(
        SpacingIssue(
            "too_wide",
            (location.source_trace_id,),
            None,
            NmBounds(250_000, 250_000),
            "Wide",
            location,
        )
        for location in (first, second)
    )
    merged = _merge_issues(issues)
    assert len(merged) == 2
    assert all(issue.members == (issue.location,) for issue in merged)


def test_merging_adjacent_leaves_retains_each_native_member_interval() -> None:
    """Close-up generation must retain original intervals after visual grouping."""
    from impedance.spacing_checks import (
        NmBounds,
        SpacingIssue,
        SpacingLocation,
        _merge_issues,
    )

    locations = tuple(
        SpacingLocation(
            "p",
            ("n",),
            (start, end),
            ((round(start * 100), 0), (round(end * 100), 0)),
            ((round(start * 100), 10), (round(end * 100), 10)),
            (round(start * 100), 0, round(end * 100), 10),
            (("n", start, end),),
        )
        for start, end in ((0.1, 0.2), (0.2, 0.3))
    )
    issues = tuple(
        SpacingIssue(
            "too_wide", ("p",), None, NmBounds(250_000, 250_000), "Wide", location
        )
        for location in locations
    )
    merged = _merge_issues(issues)
    assert len(merged) == 1
    assert merged[0].members == locations


def test_terminal_region_stops_at_unsplit_native_t_junction() -> None:
    """A branch endpoint on a native track interior ends the entry prefix there."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    p = _trace("p", "P", ((0, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    branch = _trace("branch", "P", ((400_000, 0), (400_000, -1_000_000)))
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n, branch),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0])),
    )
    _remaining, removed = _clip_terminal_transitions(
        snapshot,
        ("P", "N"),
        "F.Cu",
        {trace.trace_id: _primitives(trace) for trace in snapshot.traces},
        _Budget(SpacingLimits()),
    )
    assert 0 < removed["p"] <= 400_001


def test_pad_entry_allowance_is_one_pitch_beyond_actual_rectangular_copper() -> None:
    """400 um pad pitch plus 200 um flash extent gives a 600 um entry boundary."""
    p = _trace("p", "P", ((0, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    features = tuple(
        CopperFeature(
            net + "pad", net, "F.Cu", "pad", "rect", trace.points[0], 400_000, 150_000
        )
        for net, trace in (("P", p), ("N", n))
    )
    check = _check(
        BoardSnapshot(
            ("F.Cu",),
            (p, n),
            differential_pairs=(("P", "N"),),
            copper_features=features,
        )
    )
    assert check.verdict == "deviation"
    assert abs(check.transition_length_nm - 1_200_000) <= 2
    assert all(
        min(point[0] for point in member.source_points) >= 599_999
        for issue in check.issues
        for member in issue.members
    )


def test_later_reentry_cannot_restart_terminal_exclusion() -> None:
    """A route that returns into the region stays routing after its first exit."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    p = _trace("p", "P", ((0, 0), (800_000, 0), (400_000, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    features = tuple(
        CopperFeature(
            net + "pad", net, "F.Cu", "pad", "rect", trace.points[0], 400_000, 150_000
        )
        for net, trace in (("P", p), ("N", n))
    )
    snapshot = BoardSnapshot(
        ("F.Cu",), (p, n), differential_pairs=(("P", "N"),), copper_features=features
    )
    _remaining, removed = _clip_terminal_transitions(
        snapshot,
        ("P", "N"),
        "F.Cu",
        {trace.trace_id: _primitives(trace) for trace in snapshot.traces},
        _Budget(SpacingLimits()),
    )
    assert abs(removed["p"] - 600_000) <= 1


def test_unconnected_nearer_pad_cannot_supply_landing_pitch() -> None:
    """Only contacted connected terminals participate in the landing association."""
    snapshot = _fanout()
    before = _check(snapshot)
    after = _check(
        replace(
            snapshot,
            copper_features=(
                *snapshot.copper_features,
                _pad("nearby", "N", (0, 500_000)),
            ),
        )
    )
    assert before == after


def test_multiple_connected_landing_candidates_remain_locally_unverified() -> None:
    """Two plausible opposite terminals cannot be resolved by choosing nearest."""
    snapshot = _fanout()
    extra = _trace("n-extra", "N", ((0, -2_000_000), (10_000_000, -2_000_000)))
    check = _check(
        replace(
            snapshot,
            traces=(*snapshot.traces, extra),
            copper_features=(
                *snapshot.copper_features,
                _pad("extra-pad", "N", extra.points[0]),
            ),
        )
    )
    assert check.verdict != "match"
    assert check.unverified_length_nm > 0
    assert check.transition_length_nm < 2_000_000
    assert any("transition boundary" in reason for reason in check.reasons)


def test_landing_region_support_accounts_for_every_examined_disk() -> None:
    """One distance query charges its outer disks and every inner support product."""
    from impedance.spacing_transitions import LandingRegion

    calls = []
    region = LandingRegion(
        ((0.0, 0.0, 1.0), (10.0, 0.0, 1.0), (0.0, 10.0, 1.0), (10.0, 10.0, 1.0)), ()
    )
    region.distance((5.0, 5.0), lambda: calls.append(None))
    assert len(calls) == 4 + 4 * 4


def test_complex_valid_landing_hull_obeys_shared_construction_budget() -> None:
    """Complex convex custom pads cannot execute unaccounted cubic support scans."""
    import math

    import pytest

    from impedance.spacing_checks import SpacingLimits, _Budget, _BudgetExhausted
    from impedance.spacing_transitions import landing_region

    contour = tuple(
        (
            round(400_000 * math.cos(i * math.tau / 80)),
            round(400_000 * math.sin(i * math.tau / 80)),
        )
        for i in range(80)
    )
    first = CopperFeature(
        "p",
        "P",
        "F.Cu",
        "pad",
        "polygon",
        (0, 0),
        800_000,
        800_000,
        polygons=((contour,),),
    )
    second = _pad("n", "N", (0, 2_000_000))
    budget = _Budget(SpacingLimits(max_evaluations=2_500))
    with pytest.raises(_BudgetExhausted):
        landing_region((first, second), (1.0, 0.0), budget.consume)
    assert budget.evaluations == 2_501


def test_physical_stroke_and_crossing_branches_stop_entry_without_native_vertices() -> (
    None
):
    """Interior crossings and offset copper touches cannot hide under entry rules."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    p = _trace("p", "P", ((0, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    for points in (
        ((400_000, -1_000_000), (400_000, 1_000_000)),
        ((400_000, 50_000), (400_000, 1_000_000)),
    ):
        branch = _trace("branch", "P", points)
        snapshot = BoardSnapshot(
            ("F.Cu",),
            (p, n, branch),
            differential_pairs=(("P", "N"),),
            copper_features=(
                _pad("pp", "P", p.points[0]),
                _pad("np", "N", n.points[0]),
            ),
        )
        _remaining, removed = _clip_terminal_transitions(
            snapshot,
            ("P", "N"),
            "F.Cu",
            {trace.trace_id: _primitives(trace) for trace in snapshot.traces},
            _Budget(SpacingLimits()),
        )
        assert 0 < removed["p"] <= 400_001


@pytest.mark.parametrize(
    "source_width, branch_width", [(100_000, 200_000), (200_000, 100_000)]
)
@pytest.mark.parametrize("clearance_offset", [-1, 0, 1])
@pytest.mark.parametrize("reverse", [False, True])
def test_unequal_width_branch_capsules_keep_exact_contact_boundary(
    source_width: int, branch_width: int, clearance_offset: int, reverse: bool
) -> None:
    """Touching copper stops entry; a one-nm separation leaves the landing intact."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    p = replace(_trace("p", "P", ((0, 0), (5_000_000, 0))), width_nm=source_width)
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    radius = (source_width + branch_width) / 2
    clearance = int(radius) + clearance_offset
    branch = replace(
        _trace("branch", "P", ((400_000, clearance), (400_000, 1_000_000))),
        width_nm=branch_width,
    )
    features = (_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0]))
    if reverse:
        p, n, branch = tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            for trace in (p, n, branch)
        )

    def removed_length(traces: tuple[Trace, ...]) -> int:
        """Exercise physical entry clipping with the complete native route context."""
        snapshot = BoardSnapshot(
            ("F.Cu",),
            traces,
            differential_pairs=(("P", "N"),),
            copper_features=features,
        )
        _remaining, removed = _clip_terminal_transitions(
            snapshot,
            ("P", "N"),
            "F.Cu",
            {trace.trace_id: _primitives(trace) for trace in traces},
            _Budget(SpacingLimits()),
        )
        return removed["p"]

    actual = removed_length((p, n, branch))
    if clearance_offset > 0:
        assert actual == removed_length((p, n))
    else:
        contact = 400_000 - sqrt(radius * radius - clearance * clearance)
        # The conservative 0.25-nm distance boundary has a longer route extent
        # near tangency. Bound its physical distance, not a one-nm x interval.
        conservative = 400_000 - sqrt((radius + 0.25) ** 2 - clearance * clearance)
        assert conservative - 1 <= actual <= contact + 1


@pytest.mark.parametrize("reverse", [False, True])
def test_native_arc_branch_uses_curve_extent_beyond_endpoint_box(reverse: bool) -> None:
    """A curved branch below both stored endpoints still stops pad-side entry."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    p = _trace("p", "P", ((0, 0), (5_000_000, 0)))
    n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
    arc = Trace(
        "branch",
        "F.Cu",
        "P",
        200_000,
        ((0, 550_000), (800_000, 550_000)),
        True,
        native_arc_midpoint=(400_000, 150_000),
        native_arc_center=(400_000, 550_000),
        native_arc_radius_nm=400_000,
    )
    if reverse:
        arc = replace(arc, points=tuple(reversed(arc.points)))
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n, arc),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0])),
    )
    _remaining, removed = _clip_terminal_transitions(
        snapshot,
        ("P", "N"),
        "F.Cu",
        {trace.trace_id: _primitives(trace) for trace in snapshot.traces},
        _Budget(SpacingLimits()),
    )
    radius = (p.width_nm + arc.width_nm) / 2
    expected = 400_000 - sqrt((400_000 + radius) ** 2 - 550_000**2)
    assert abs(removed["p"] - expected) <= 1


def test_physical_branch_entry_boundary_is_independent_of_native_direction() -> None:
    """First branch contact is measured outward from the pad, not track storage."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    removed_lengths = []
    for reverse_p in (False, True):
        for reverse_n in (False, True):
            p = _trace("p", "P", ((0, 0), (5_000_000, 0)))
            n = _trace("n", "N", ((0, 400_000), (5_000_000, 400_000)))
            features = (_pad("pp", "P", p.points[-1]), _pad("np", "N", n.points[-1]))
            if reverse_p:
                p = replace(p, points=tuple(reversed(p.points)))
            if reverse_n:
                n = replace(n, points=tuple(reversed(n.points)))
            branch = _trace(
                "branch", "P", ((4_600_000, -1_000_000), (4_600_000, 1_000_000))
            )
            snapshot = BoardSnapshot(
                ("F.Cu",),
                (p, n, branch),
                differential_pairs=(("P", "N"),),
                copper_features=features,
            )
            _remaining, removed = _clip_terminal_transitions(
                snapshot,
                ("P", "N"),
                "F.Cu",
                {trace.trace_id: _primitives(trace) for trace in snapshot.traces},
                _Budget(SpacingLimits()),
            )
            removed_lengths.append(removed["p"])
    assert max(removed_lengths) - min(removed_lengths) <= 1
    assert all(249_999 <= length <= 250_001 for length in removed_lengths)


def test_split_reordered_branch_entry_keeps_physical_boundary() -> None:
    """Native segmentation and ordering cannot move a pad-side branch boundary."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _clip_terminal_transitions,
        _primitives,
    )

    lengths = []
    for reversed_tracks in (False, True):
        for reordered in (False, True):
            traces = [
                _trace("p0", "P", ((0, 0), (4_700_000, 0))),
                _trace("p1", "P", ((4_700_000, 0), (4_800_000, 0))),
                _trace("p2", "P", ((4_800_000, 0), (5_000_000, 0))),
                _trace("n", "N", ((0, 400_000), (5_000_000, 400_000))),
                _trace(
                    "branch", "P", ((4_600_000, -1_000_000), (4_600_000, 1_000_000))
                ),
            ]
            if reversed_tracks:
                traces = [
                    replace(trace, points=tuple(reversed(trace.points)))
                    for trace in traces
                ]
            if reordered:
                traces.reverse()
            snapshot = BoardSnapshot(
                ("F.Cu",),
                tuple(traces),
                differential_pairs=(("P", "N"),),
                copper_features=(
                    _pad("pp", "P", (5_000_000, 0)),
                    _pad("np", "N", (5_000_000, 400_000)),
                ),
            )
            _remaining, removed = _clip_terminal_transitions(
                snapshot,
                ("P", "N"),
                "F.Cu",
                {trace.trace_id: _primitives(trace) for trace in snapshot.traces},
                _Budget(SpacingLimits()),
            )
            lengths.append(
                sum(
                    length
                    for identity, length in removed.items()
                    if identity.startswith("p")
                )
            )
    assert all(249_999 <= length <= 250_001 for length in lengths)


def test_opposing_fanout_angles_can_share_a_common_outgoing_half_plane() -> None:
    """Symmetric 45-degree entries have a clear common outward direction."""
    p = _trace("p", "P", ((0, 0), (500_000, 500_000), (5_000_000, 500_000)))
    n = _trace(
        "n",
        "N",
        (
            (0, 2_000_000),
            (500_000, 1_500_000),
            (1_000_000, 800_000),
            (5_000_000, 800_000),
        ),
    )
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (p, n),
        differential_pairs=(("P", "N"),),
        copper_features=(_pad("pp", "P", p.points[0]), _pad("np", "N", n.points[0])),
    )
    check = _check(snapshot)
    assert check.verdict == "match"
    assert check.unverified_length_nm == 0


def test_merge_work_limit_preserves_one_continuous_physical_concern() -> None:
    """A bounded detailed merge cannot turn one long issue into thousands of cards."""
    from impedance.spacing_checks import (
        NmBounds,
        SpacingIssue,
        SpacingLocation,
        _merge_issues,
    )

    count = 26_000
    issues = []
    for index in range(count):
        lower, upper = index / count, (index + 1) / count
        location = SpacingLocation(
            "p",
            ("n",),
            (lower, upper),
            ((index * 100, 0), ((index + 1) * 100, 0)),
            ((index * 100, 400_000), ((index + 1) * 100, 400_000)),
            (index * 100, 0, (index + 1) * 100, 400_000),
            (("n", lower, upper),),
        )
        issues.append(
            SpacingIssue(
                "too_wide", ("p",), None, NmBounds(250_000, 250_000), "Wide", location
            )
        )
    merged = _merge_issues(issues)
    assert len(merged) == 1
    assert len(merged[0].members) == count


def test_context_comparison_cache_shares_complete_rows_and_keeps_inputs_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated pair/row inspections reuse exact checks without cross-target reuse."""
    import impedance.spacing_checks as checks

    snapshot = _fanout(250_000)
    context = checks.SpacingContext(snapshot)
    calls = []
    original = checks.compare_pair_spacing

    def compare(
        measurement: checks.SpacingMeasurement,
        target: Optional[int],
        *,
        comparison_band_nm: int = checks.SPACING_COMPARISON_BAND_NM,
    ) -> checks.SpacingCheck:
        calls.append(target)
        return original(measurement, target, comparison_band_nm=comparison_band_nm)

    monkeypatch.setattr(checks, "compare_pair_spacing", compare)
    first = context.check(("P", "N"), "F.Cu", 150_000)
    complete_row = context.check(
        ("N", "P"), "F.Cu", 150_000, source_ids=frozenset(("p", "n"))
    )
    assert complete_row is first
    assert calls == [150_000]
    assert context.check(("P", "N"), "F.Cu", 250_000).verdict == "match"
    filtered = context.check(("P", "N"), "F.Cu", 150_000, source_ids=frozenset(("p",)))
    assert filtered is not first
    assert calls == [150_000, 250_000, 150_000]
    assert context.check(("P", "N"), "F.Cu", True).verdict == "needs_target"
    assert context.check(("P", "N"), "F.Cu", 1).verdict == "deviation"


@pytest.mark.parametrize(
    "case",
    ["inside", "first_boundary", "outside_at_root", "callback_error", "work_limit"],
)
def test_completed_terminal_search_releases_curve_and_budget_without_cyclic_gc(
    case: str,
) -> None:
    """A finished contact search cannot retain native geometry until a later GC pause."""
    import gc
    import weakref

    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _BudgetExhausted,
        _primitives,
        _tree,
    )
    from impedance.spacing_transitions import first_exit

    class Distance:
        """Keep the callback independently owned, with deterministic failure behavior."""

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, point: tuple[float, float]) -> float:
            self.calls += 1
            if case == "callback_error" and self.calls == 2:
                raise RuntimeError("native distance callback failed")
            if case == "outside_at_root":
                return 1.0
            if case in ("first_boundary", "work_limit"):
                return point[0] - 37.0
            return -1_000.0

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        budget = _Budget(
            SpacingLimits(max_evaluations=3 if case == "work_limit" else 100_000)
        )
        curve = _primitives(Trace("p", "F.Cu", "P", 10, ((0, 0), (100, 0))))[0]
        distance = Distance()
        consume = budget.consume
        references = {
            "curve": weakref.ref(curve),
            "distance": weakref.ref(distance),
            "consume": weakref.ref(consume),
            "budget": weakref.ref(budget),
            "tree": weakref.ref(_tree((curve,), budget)),
        }
        result = None
        error_type = None
        try:
            result = first_exit(curve, distance, consume)
        except (RuntimeError, _BudgetExhausted) as error:
            error_type = type(error)
        if case == "callback_error":
            assert error_type is RuntimeError
        elif case == "work_limit":
            assert error_type is _BudgetExhausted
        else:
            assert error_type is None
            if case == "inside":
                assert result is None
            elif case == "outside_at_root":
                assert result == 0.0
            else:
                assert result is not None and 36.5 <= result * 100 <= 37.0
        del curve, distance, consume, budget
        retained = [
            name for name, reference in references.items() if reference() is not None
        ]
        assert not retained, f"Completed search retained {retained} until cyclic GC"
    finally:
        if was_enabled:
            gc.enable()
