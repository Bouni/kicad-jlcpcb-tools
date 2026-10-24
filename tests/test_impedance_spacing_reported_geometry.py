"""Regressions derived from the user's live-board false spacing reports."""

from dataclasses import replace

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
