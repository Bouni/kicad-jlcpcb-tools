"""Regressions derived from the user's live-board false spacing reports."""

from dataclasses import replace
import math
from typing import Optional

import pytest

from impedance.model import BoardSnapshot, Trace
from impedance.spacing_checks import (
    SpacingCheck,
    compare_pair_spacing,
    measure_pair_spacing,
)
from tests.impedance_spacing_regression_fixtures import reported_spacing_geometry


def _assess(snapshot: BoardSnapshot, target: int = 202_300) -> SpacingCheck:
    """Assess all retained source geometry with its declared target."""
    return compare_pair_spacing(
        measure_pair_spacing(snapshot, ("P", "N"), "F.Cu"), target
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_reported_row27_normal_corners_and_tiny_pad_jogs_are_quiet(
    reverse: bool,
) -> None:
    """A routed 202.3 um gap does not acquire diagonal endcap or pad-entry warnings."""
    snapshot = reported_spacing_geometry("row27")
    if reverse:
        snapshot = replace(
            snapshot,
            traces=tuple(
                replace(t, points=tuple(reversed(t.points)))
                for t in reversed(snapshot.traces)
            ),
        )
    check = _assess(snapshot)
    assert check.verdict == "match"
    assert check.coverage == "complete"
    assert not check.issues
    assert check.transition_length_nm > 5_000_000


def test_duplicated_connector_pads_and_link_tracks_are_transition() -> None:
    """The USB-C duplicated-pin landing network does not become spacing images."""
    check = _assess(reported_spacing_geometry("connector"))
    # The retained trunk really has a 206.3 um gap, so it remains reportable.
    assert check.verdict == "deviation"
    assert check.transition_length_nm > 5_000_000
    assert all(
        min(p[0] for p in member.source_points) > 1_000_000
        for issue in check.issues
        for member in issue.members
    )
    assert check.coverage == "complete"


def test_inline_testpoints_exempt_both_sides_but_leave_wide_trunk() -> None:
    """Both inline landings are transitions; the preceding 331.3 um gap still warns."""
    check = _assess(reported_spacing_geometry("testpoints"))
    assert check.verdict == "deviation"
    assert check.coverage == "complete"
    assert check.transition_length_nm > 8_000_000
    assert any(
        issue.gap_nm and issue.gap_nm.lower_nm <= 331_300 <= issue.gap_nm.upper_nm
        for issue in check.issues
    )
    assert all(
        all(
            point[0] < -1_000_000 or point[0] > 3_000_000
            for point in member.source_points
        )
        for issue in check.issues
        for member in issue.members
    )


@pytest.mark.parametrize("turn", [45, 90])
def test_constant_gap_mitered_corners_have_no_endcap_deviation(turn: int) -> None:
    """Compare shared facing sides, including paired continuations at a turn."""
    if turn == 45:
        # Integer approximation to the exact 300 um normal offset's 45° miter.
        ppoints = ((0, 0), (4_000_000, 0), (8_000_000, 4_000_000))
        npoints = ((0, 300_000), (3_875_736, 300_000), (7_787_868, 4_212_132))
    else:
        ppoints = ((0, 0), (4_000_000, 0), (4_000_000, 4_000_000))
        npoints = ((0, 300_000), (3_700_000, 300_000), (3_700_000, 4_000_000))
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            Trace("p", "F.Cu", "P", 150_000, ppoints),
            Trace("n", "F.Cu", "N", 150_000, npoints),
        ),
        differential_pairs=(("P", "N"),),
    )
    check = _assess(snapshot, 150_000)
    assert check.verdict == "match"
    assert check.coverage == "complete"


def _mitered_serpentine(segments: int, squeeze: Optional[str] = None) -> BoardSnapshot:
    """Offset a chamfered serpentine by an independently known 300 um pitch."""
    points = [(0, 0)]
    steps = ((10, 0), (2, 2), (0, 2), (-2, 2), (-10, 0), (-2, 2), (0, 2), (2, 2))
    for index in range(segments):
        x, y = points[-1]
        dx, dy = steps[index % len(steps)]
        points.append((x + dx * 1_000_000, y + dy * 1_000_000))
    normals = [
        (-(b[1] - a[1]) / math.dist(a, b), (b[0] - a[0]) / math.dist(a, b))
        for a, b in zip(points, points[1:])
    ]
    mate = []
    for index, point in enumerate(points):
        before, after = normals[max(0, index - 1)], normals[min(index, segments - 1)]
        scale = 1 + sum(a * b for a, b in zip(before, after))
        offset = tuple(300_000 * (a + b) / scale for a, b in zip(before, after))
        mate.append(tuple(round(value + delta) for value, delta in zip(point, offset)))
    if squeeze:
        vertex = {"start": 0, "middle": segments // 2, "end": segments}[squeeze]
        normal = normals[vertex if squeeze == "middle" else max(0, vertex - 1)]
        for index in (vertex, vertex + 1) if squeeze == "middle" else (vertex,):
            mate[index] = tuple(
                round(value - 100_000 * component)
                for value, component in zip(mate[index], normal)
            )
    traces = tuple(
        Trace(f"{net}{index:03}", "F.Cu", net, 150_000, (a, b))
        for net, route in (("P", points), ("N", mate))
        for index, (a, b) in enumerate(zip(route, route[1:]))
    )
    return BoardSnapshot(("F.Cu",), traces, differential_pairs=(("P", "N"),))


@pytest.mark.parametrize("segments", [60, 80])
def test_long_mitered_serpentine_completes_without_budget_starvation(
    segments: int,
) -> None:
    """Ordinary length-tuned routing remains assessable under the default limits."""
    check = _assess(_mitered_serpentine(segments), 150_000)
    assert check.verdict == "match", check.reasons
    assert check.coverage == "complete"
    assert check.unverified_length_nm == 0


@pytest.mark.parametrize("position", ["start", "middle", "end"])
@pytest.mark.parametrize("variant", ["original", "ids", "direction", "shuffle"])
def test_serpentine_violation_survives_trace_identity_order_and_direction(
    position: str,
    variant: str,
) -> None:
    """A 100 um squeeze anywhere on the route stays actionable after native reorderings."""
    snapshot = _mitered_serpentine(160, squeeze=position)
    baseline = _assess(snapshot, 150_000)
    if variant == "ids":
        snapshot = replace(
            snapshot,
            traces=tuple(
                replace(
                    trace,
                    trace_id=trace.trace_id[0] + f"{159 - int(trace.trace_id[1:]):03}",
                )
                for trace in reversed(snapshot.traces)
            ),
        )
    elif variant == "direction":
        snapshot = replace(
            snapshot,
            traces=tuple(
                replace(trace, points=tuple(reversed(trace.points)))
                for trace in snapshot.traces
            ),
        )
    elif variant == "shuffle":
        snapshot = replace(
            snapshot, traces=(*snapshot.traces[1::2], *snapshot.traces[::2])
        )
    check = _assess(snapshot, 150_000)
    assert check.verdict == "deviation", check.reasons
    assert any(issue.code == "too_tight" for issue in check.issues)
    assert any(
        issue.gap_nm is not None and issue.gap_nm.upper_nm <= 50_500
        for issue in check.issues
    )
    assert check.coverage == baseline.coverage
    assert check.minimum_gap_nm == baseline.minimum_gap_nm
    assert check.maximum_gap_nm == baseline.maximum_gap_nm
    if variant in ("ids", "shuffle"):
        assert [
            (issue.code, issue.position, issue.gap_nm) for issue in check.issues
        ] == [(issue.code, issue.position, issue.gap_nm) for issue in baseline.issues]


@pytest.mark.parametrize("split", [3_700_001, 3_850_000, 3_999_999])
@pytest.mark.parametrize("reverse", [False, True])
def test_collinear_split_before_miter_cannot_create_wide_gap(
    split: int, reverse: bool
) -> None:
    """A native collinear vertex cannot change a known 150 um offset sidewall."""
    p = ((0, 0), (split, 0), (4_000_000, 0), (4_000_000, 4_000_000))
    n = ((0, 300_000), (3_700_000, 300_000), (3_700_000, 4_000_000))
    traces = tuple(
        Trace(f"{net}{index}", "F.Cu", net, 150_000, (b, a) if reverse else (a, b))
        for net, route in (("P", p), ("N", n))
        for index, (a, b) in enumerate(zip(route, route[1:]))
    )
    check = _assess(
        BoardSnapshot(("F.Cu",), traces, differential_pairs=(("P", "N"),)), 150_000
    )
    assert check.verdict == "match", check
    assert check.coverage == "complete"


def test_miter_only_split_keeps_global_proof_and_real_neighboring_review_evidence() -> (
    None
):
    """The analytic corner span cannot manufacture a finite 150 um cross-section."""
    from impedance.matching import analyze
    from impedance.spacing_checks import _primitives
    from impedance.spacing_review import spacing_review_entries
    from tests.test_impedance_spacing_checks import _snapshot
    from tests.test_impedance_spacing_review_entries import _config

    points = ((0, 0), (3_850_000, 0), (4_000_000, 0), (4_000_000, 4_000_000))
    mates = ((0, 300_000), (3_700_000, 300_000), (3_700_000, 4_000_000))
    snapshot = _snapshot(
        *(
            Trace(f"{net}{index}", "F.Cu", net, 150_000, (a, b))
            for net, route in (("P", points), ("N", mates))
            for index, (a, b) in enumerate(zip(route, route[1:]))
        )
    )
    measurement = measure_pair_spacing(snapshot, ("P", "N"), "F.Cu")
    short = next(trace for trace in measurement.traces if trace.trace_id == "P1")
    short_check = compare_pair_spacing(replace(measurement, traces=(short,)), 100_000)
    assert short_check.verdict == "deviation" and short_check.coverage == "complete"
    assert short.minimum.lower_nm >= 149_999 and short.maximum.upper_nm <= 150_001
    assert short.witnesses == ()
    check = compare_pair_spacing(measurement, 100_000)
    assert (
        check.verdict == "deviation" and check.coverage == "complete" and check.issues
    )
    native = {trace.trace_id: _primitives(trace) for trace in snapshot.traces}
    for issue in check.issues:
        for member in issue.members:
            assert member.cross_sections
            assert 0 <= member.source_interval[0] <= member.source_interval[1] <= 1
            assert all(
                0 <= lower <= upper <= 1 for _, lower, upper in member.mate_intervals
            )
            for section in member.cross_sections:
                assert (
                    min(
                        span.distance(section.source_point)
                        for span in native[section.source_trace_id]
                    )
                    < 0.001
                )
                assert (
                    min(
                        span.distance(section.mate_point)
                        for span in native[section.mate_trace_id]
                    )
                    < 0.001
                )
    config = _config()
    spec = config.specifications[0]
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=100_000),),
            ),
        ),
    )
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert entries and all(
        entry.issue_code == "too_wide" and entry.representative is not None
        for entry in entries
    )


@pytest.mark.parametrize("rounding", [-1, 1])
def test_rounded_non_octilinear_miter_has_bounded_nanometre_uncertainty(
    rounding: int,
) -> None:
    """A 3-4-5 support line rounded at its miter stays within 1 nm of its oracle."""
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            Trace(
                "p",
                "F.Cu",
                "P",
                150_000,
                ((0, 0), (4_000_000, 0), (7_000_000, 4_000_000)),
            ),
            Trace(
                "n",
                "F.Cu",
                "N",
                150_000,
                ((0, 300_000), (3_850_000 + rounding, 300_000), (6_760_000, 4_180_000)),
            ),
        ),
        differential_pairs=(("P", "N"),),
    )
    check = _assess(snapshot, 150_000)
    assert check.verdict == "match", check
    assert check.coverage == "complete"
    assert check.minimum_gap_nm is not None and check.minimum_gap_nm.lower_nm >= 149_998
    assert check.maximum_gap_nm is not None and check.maximum_gap_nm.upper_nm <= 150_002


def test_rounded_segmented_mate_retains_every_native_sidewall_interval() -> None:
    """Near-parallel native pieces all belong to the bounded dimensional proof."""
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            Trace("pstart", "F.Cu", "P", 150_000, ((0, 0), (4_000_000, 0))),
            Trace(
                "pdiag", "F.Cu", "P", 150_000, ((4_000_000, 0), (7_000_000, 4_000_000))
            ),
            Trace("nstart", "F.Cu", "N", 150_000, ((0, 300_000), (3_850_000, 300_000))),
            Trace(
                "nfirst",
                "F.Cu",
                "N",
                150_000,
                ((3_850_000, 300_000), (5_305_001, 2_240_001)),
            ),
            Trace(
                "nsecond",
                "F.Cu",
                "N",
                150_000,
                ((5_305_001, 2_240_001), (6_760_000, 4_180_000)),
            ),
        ),
        differential_pairs=(("P", "N"),),
    )
    measurement = measure_pair_spacing(snapshot, ("P", "N"), "F.Cu")
    check = compare_pair_spacing(measurement, 100_000)
    assert check.verdict == "deviation" and check.coverage == "complete"
    source = next(trace for trace in measurement.traces if trace.trace_id == "pdiag")
    assert {
        identity
        for issue in source.witnesses
        for identity in issue.location.mate_trace_ids
    } == {"nfirst", "nsecond"}
    for issue in source.witnesses:
        assert issue.location is not None and issue.location.cross_sections
        assert all(
            0 <= start <= end <= 1 for _, start, end in issue.location.mate_intervals
        )
        for section in issue.location.cross_sections:
            actual = (
                math.dist(section.source_point, section.mate_point)
                - (section.source_width_nm + section.mate_width_nm) / 2
            )
            assert issue.gap_nm.lower_nm <= actual <= issue.gap_nm.upper_nm


def test_rounded_miter_proof_cannot_hide_a_closer_competing_native_path() -> None:
    """Nearby copper remains a distance candidate after tolerant corner completion."""
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            Trace("pstart", "F.Cu", "P", 150_000, ((0, 0), (4_000_000, 0))),
            Trace(
                "pdiag", "F.Cu", "P", 150_000, ((4_000_000, 0), (7_000_000, 4_000_000))
            ),
            Trace("nstart", "F.Cu", "N", 150_000, ((0, 300_000), (3_850_001, 300_000))),
            Trace(
                "ndiag",
                "F.Cu",
                "N",
                150_000,
                ((3_850_001, 300_000), (6_760_000, 4_180_000)),
            ),
            Trace(
                "closer",
                "F.Cu",
                "N",
                150_000,
                ((3_850_001, 300_000), (5_340_000, 2_120_000)),
            ),
        ),
        differential_pairs=(("P", "N"),),
    )
    measurement = measure_pair_spacing(snapshot, ("P", "N"), "F.Cu")
    source = replace(
        measurement,
        traces=tuple(
            trace for trace in measurement.traces if trace.trace_id == "pdiag"
        ),
    )
    check = compare_pair_spacing(source, 150_000)
    assert check.verdict == "deviation"
    assert any(
        issue.code == "too_tight" and issue.gap_nm.upper_nm <= 50_500
        for issue in check.issues
    )


@pytest.mark.parametrize("both_ends", [False, True])
def test_unequal_straight_lengths_keep_exact_extrema_without_dense_tessellation(
    both_ends: bool,
) -> None:
    """Finite parallel tracks have a 150 um minimum and an endpoint-defined maximum."""
    offset = 2_000_000 if both_ends else 3_000_000
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            Trace("p", "F.Cu", "P", 150_000, ((0, 0), (10_000_000, 0))),
            Trace(
                "n",
                "F.Cu",
                "N",
                150_000,
                (
                    (offset, 300_000),
                    (10_000_000 - offset if both_ends else 10_000_000, 300_000),
                ),
            ),
        ),
        differential_pairs=(("P", "N"),),
    )
    check = _assess(snapshot, 150_000)
    assert check.verdict == "deviation"
    assert check.coverage == "complete", check.reasons
    assert check.unverified_length_nm == 0
    assert check.minimum_gap_nm is not None and check.minimum_gap_nm.lower_nm >= 149_999
    assert check.maximum_gap_nm is not None
    expected = math.hypot(offset, 300_000) - 150_000
    assert check.maximum_gap_nm.lower_nm <= expected <= check.maximum_gap_nm.upper_nm
    assert any(issue.code == "too_wide" for issue in check.issues)


def test_local_jog_cannot_hide_a_later_centerline_crossing() -> None:
    """A long leg after a 3 um jog may cross back several millimetres away."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _primitives,
        _terminal_branch_cuts,
    )

    trace = Trace(
        "return",
        "F.Cu",
        "P",
        150_000,
        ((0, 0), (10_000_000, 0), (10_000_000, 3_000), (0, -3_000)),
    )
    parts = _primitives(trace)
    endpoints = {}
    for part in parts:
        for point in (part.start, part.end):
            endpoints.setdefault(point, []).append(part)
    cuts = _terminal_branch_cuts(parts, endpoints, _Budget(SpacingLimits()))
    assert id(parts[0]) in cuts
    assert min(cuts[id(parts[0])]) <= 0.5


def test_duplicated_bank_cannot_quiet_an_extra_outgoing_branch() -> None:
    """An unrelated route leaving the connector bank remains unverified."""
    snapshot = reported_spacing_geometry("connector")
    branch = Trace(
        "extra", "F.Cu", "P", 290_600, ((900_000, 0), (4_000_000, 2_000_000))
    )
    check = _assess(replace(snapshot, traces=(*snapshot.traces, branch)))
    assert check.coverage == "partial"
    assert check.verdict != "match"
    assert check.unverified_length_nm > 0


def test_connector_without_authoritative_owner_is_not_assumed_one_bank() -> None:
    """Geometric proximity alone cannot establish duplicated-pin ownership."""
    snapshot = reported_spacing_geometry("connector")
    check = _assess(
        replace(
            snapshot,
            copper_features=tuple(
                replace(pad, footprint_id="") for pad in snapshot.copper_features
            ),
        )
    )
    assert check.coverage == "partial"
    assert check.verdict != "match"


def test_unconnected_duplicate_flash_cannot_expand_a_landing_region() -> None:
    """A same-footprint flash still needs exact copper contact to supply pitch."""
    snapshot = reported_spacing_geometry("row27")
    before = _assess(snapshot)
    pad = replace(
        snapshot.copper_features[0],
        feature_id="unconnected",
        footprint_id="unrelated",
        center=(20_000_000, 20_000_000),
    )
    after = _assess(replace(snapshot, copper_features=(*snapshot.copper_features, pad)))
    assert after == before


def test_target_guides_receive_actual_native_widths_and_associated_points() -> None:
    """Every reported cross-section belongs to its member's chosen native mate."""
    snapshot = reported_spacing_geometry("testpoints")
    traces = {trace.trace_id: trace for trace in snapshot.traces}
    check = _assess(snapshot)
    count = 0
    for issue in check.issues:
        for member in issue.members:
            assert member.cross_sections
            for section in member.cross_sections:
                assert section.source_trace_id == member.source_trace_id
                assert section.mate_trace_id in member.mate_trace_ids
                assert (
                    section.source_width_nm == traces[section.source_trace_id].width_nm
                )
                assert section.mate_width_nm == traces[section.mate_trace_id].width_nm
                assert section.source_point in member.source_points
                assert section.mate_point in member.mate_points
                count += 1
    assert count > 0


def test_inline_landing_is_not_cross_associated_with_remote_native_pad_pair() -> None:
    """A compatible far terminal cannot compete with its known same-owner mate."""
    check = _assess(reported_spacing_geometry("testpoints_with_terminal"))
    assert check.verdict == "deviation"
    assert check.coverage == "complete"
    assert not check.reasons
    assert check.transition_length_nm > 20_000_000
    assert any(
        issue.gap_nm and issue.gap_nm.lower_nm <= 331_300 <= issue.gap_nm.upper_nm
        for issue in check.issues
    )


def test_local_jog_cannot_hide_a_later_offset_copper_return() -> None:
    """A return leg can contact the first stroke without crossing its centerline."""
    from impedance.spacing_checks import (
        SpacingLimits,
        _Budget,
        _primitives,
        _terminal_branch_cuts,
    )

    trace = Trace(
        "offset-return",
        "F.Cu",
        "P",
        150_000,
        ((0, 0), (10_000_000, 0), (10_000_000, 3_000), (5_000_000, 40_000)),
    )
    parts = _primitives(trace)
    endpoints = {}
    for part in parts:
        for point in (part.start, part.end):
            endpoints.setdefault(point, []).append(part)
    cuts = _terminal_branch_cuts(parts, endpoints, _Budget(SpacingLimits()))
    assert id(parts[0]) in cuts
    assert 0.48 < min(cuts[id(parts[0])]) < 0.5
