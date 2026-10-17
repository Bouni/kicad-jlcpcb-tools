"""Prove complete copper coverage before excluding controlled-impedance routing."""

from dataclasses import replace
from math import hypot
from types import SimpleNamespace

import pytest

from impedance import matching
from impedance.matching import analyze, is_pad_via_stub, validate_review
from impedance.model import (
    EXCLUSION_COVERED_STUB,
    BoardSnapshot,
    Config,
    CopperFeature,
    LayerSettings,
    Specification,
    Trace,
    ValidationError,
)
from impedance.pcbnew_adapter import BoardSnapshotError, sample_arc, snapshot_board
from impedance.review_tracking import ImageView, LayerApproval, ReviewTracking


def _circle(center: tuple[int, int], diameter: int = 200_000) -> CopperFeature:
    """Return a same-net circular flash with independent physical dimensions."""
    return CopperFeature(
        str(center), "RF", "F.Cu", "pad", "circle", center, diameter, diameter
    )


def _scenario(
    trace: Trace, features: tuple[CopperFeature, ...]
) -> tuple[Config, BoardSnapshot]:
    """Supply independently known complete class membership to geometry analysis."""
    specification = Specification(
        "rf", "RF", "50", "single_ended", "RF", (LayerSettings("F.Cu", ("B.Cu",)),)
    )
    snapshot = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        (trace,),
        "context",
        ("RF",),
        (("RF", ("RF",)),),
        "classes",
        copper_features=features,
    )
    return Config(True, (specification,)), snapshot


def _exclusion(trace: Trace, features: tuple[CopperFeature, ...]) -> str:
    """Exercise section generation rather than only its containment helper."""
    config, snapshot = _scenario(trace, features)
    return analyze(config, snapshot).sections[0].exclusion_reason


def _native_trace(arc: bool, *, missing_mid: bool = False) -> Trace:
    """Capture the actual adapter arc branch without mocking its sampled geometry."""
    item = SimpleNamespace(
        GetLayer=lambda: 0,
        GetWidth=lambda: 100_000,
        GetStart=lambda: SimpleNamespace(x=1_000_000, y=0),
        GetEnd=lambda: SimpleNamespace(x=0, y=1_000_000),
        GetNetname=lambda: "RF",
        m_Uuid=SimpleNamespace(AsString=lambda: "native"),
    )
    if arc:
        item.GetClass = lambda: "PCB_ARC"
        if not missing_mid:
            item.GetMid = lambda: SimpleNamespace(x=866_025, y=500_000)
    board = SimpleNamespace(
        GetTracks=lambda: (item,),
        GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: (0, 31)),
        GetFootprints=lambda: (),
        GetDrawings=lambda: (),
    )
    module = SimpleNamespace(F_Cu=0, B_Cu=31)
    return snapshot_board(board, module).traces[0]


@pytest.mark.parametrize("arc", [True, False])
def test_native_arc_remains_exportable_and_straight_stub_excludes(arc: bool) -> None:
    """Approximate native curves cannot authorize exclusion; exact tracks still can."""
    trace = _native_trace(arc)
    features = (_circle((0, 0), 3_000_000),)
    assert _exclusion(trace, features) == ("" if arc else EXCLUSION_COVERED_STUB)
    assert getattr(trace, "is_native_arc", False) is arc
    assert trace.points[0] == (1_000_000, 0)
    assert trace.points[-1] == (0, 1_000_000)


def test_native_arc_without_midpoint_api_cannot_be_captured_as_a_straight_stub() -> (
    None
):
    """Missing curve APIs fail capture instead of hiding a native arc behind its chord."""
    with pytest.raises(BoardSnapshotError, match="arc midpoint"):
        _native_trace(True, missing_mid=True)


def test_stub_policy_change_invalidates_review_without_changing_section_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed exclusion semantics require a new review and retain historical row IDs."""
    trace = Trace("route", "F.Cu", "RF", 100_000, ((0, 0), (600_000, 0)))
    config, snapshot = _scenario(trace, (_circle((0, 0), 600_000),))
    monkeypatch.setattr(matching, "COVERED_STUB_POLICY_REVISION", 1, raising=False)
    before = analyze(config, snapshot)
    section_id = before.sections[0].section_id
    timestamp = "2026-10-10T15:00:00.000000Z"
    tracking = ReviewTracking(
        images=(ImageView("rf", "F.Cu", section_id, before.digest, timestamp),),
        layers=(
            LayerApproval(
                "rf", "F.Cu", before.digest, timestamp, ((section_id, before.digest),)
            ),
        ),
    )
    approved = replace(
        config,
        reviewed_digest=before.digest,
        included_section_ids=(section_id,),
        review_tracking=tracking,
    )
    payload = approved.to_dict()
    validate_review(approved, before)
    monkeypatch.setattr(matching, "COVERED_STUB_POLICY_REVISION", 2, raising=False)
    after = analyze(config, snapshot)
    assert before.sections == after.sections
    assert before.digest != after.digest
    reopened = Config.from_dict(payload)
    assert reopened.review_tracking == tracking
    assert reopened.included_section_ids == (section_id,)
    with pytest.raises(ValidationError, match="changed|review"):
        validate_review(reopened, after)
    assert reopened.to_dict() == payload
    reapproved = replace(reopened, reviewed_digest=after.digest)
    validate_review(Config.from_dict(reapproved.to_dict()), after)
    assert reapproved.review_tracking == tracking


def test_exposed_copper_between_covered_samples_stays_exportable() -> None:
    """Four covered sample discs cannot authorize the uncovered copper between them."""
    trace = Trace("gap", "F.Cu", "RF", 100_000, ((0, 45_000), (75_000, 45_000)))
    features = (_circle((4_500, 0)), _circle((69_500, 0)))
    assert all(
        hypot(37_000 - feature.center[0], 95_000) > 100_000 for feature in features
    )
    assert _exclusion(trace, features) == ""


@pytest.mark.parametrize("width", [100_000, 100_001])
def test_same_flash_contains_the_entire_straight_capsule(width: int) -> None:
    """Supported covered stubs remain excluded for even and odd integer widths."""
    trace = Trace("covered", "F.Cu", "RF", width, ((0, 0), (100_000, 0)))
    assert _exclusion(trace, (_circle((0, 0), 600_000),)) == EXCLUSION_COVERED_STUB


def test_odd_width_uses_a_conservative_radius_at_the_boundary() -> None:
    """Half-nanometre copper radii round outward, never truncate uncovered copper."""
    trace = Trace("odd", "F.Cu", "RF", 101, ((0, 0), (0, 0)))
    assert _exclusion(trace, (_circle((0, 0), 100),)) == ""
    assert _exclusion(trace, (_circle((0, 0), 102),)) == EXCLUSION_COVERED_STUB


def test_zero_length_track_requires_its_complete_endcap_to_fit() -> None:
    """A degenerate centerline still represents a finite copper disc."""
    trace = Trace("zero", "F.Cu", "RF", 100_000, ((0, 0), (0, 0)))
    assert _exclusion(trace, (_circle((0, 0)),)) == EXCLUSION_COVERED_STUB
    assert _exclusion(trace, (_circle((90_000, 0)),)) == ""


def test_arc_approximation_does_not_hide_a_bulge_between_chords() -> None:
    """Retain an actual arc protruding beyond a rotated flash between sampled points."""
    points = sample_arc((1_000_000, 0), (866_025, 500_000), (0, 1_000_000), 100_000)
    trace = Trace("arc", "F.Cu", "RF", 100_000, points, is_native_arc=True)
    flash = CopperFeature(
        "rotated", "RF", "F.Cu", "pad", "rect", (0, 0), 2_099_000, 4_000_000, -45.0
    )
    # Along the rectangle's local x axis, native arc copper reaches about 1.05 mm.
    # The rectangle ends at 1.0495 mm; its sampled chords underestimate that bulge.
    assert _exclusion(trace, (flash,)) == ""


def test_native_arc_with_apparent_clearance_still_remains_exportable() -> None:
    """Apparent sample clearance cannot replace proof about native copper geometry."""
    points = sample_arc((1_000_000, 0), (866_025, 500_000), (0, 1_000_000), 100_000)
    trace = Trace("arc", "F.Cu", "RF", 100_000, points, is_native_arc=True)
    assert _exclusion(trace, (_circle((0, 0), 3_000_000),)) == ""


def test_multi_vertex_coverage_never_ignores_an_uncovered_segment() -> None:
    """Each segment needs continuous coverage, including a route's return path."""
    trace = Trace("turn", "F.Cu", "RF", 100_000, ((0, 0), (400_000, 0), (0, 0)))
    assert not is_pad_via_stub(trace, (_circle((0, 0), 600_000),))


def test_exact_polyline_with_complete_capsules_remains_covered() -> None:
    """Source-independent straight spans do not inherit native arc uncertainty."""
    trace = Trace("turn", "F.Cu", "RF", 100_000, ((0, 0), (100_000, 0), (0, 0)))
    assert _exclusion(trace, (_circle((0, 0), 300_000),)) == EXCLUSION_COVERED_STUB


def test_native_arc_provenance_is_validated() -> None:
    """Invalid provenance cannot bypass the native arc exclusion gate."""
    trace = replace(_native_trace(True), is_native_arc=1)
    config, snapshot = _scenario(trace, (_circle((0, 0), 3_000_000),))
    with pytest.raises(ValidationError, match="provenance"):
        analyze(config, snapshot)


def test_arc_provenance_invalidates_digest_without_losing_historical_row_id() -> None:
    """A newly exportable curve requires review even if all sampled points are unchanged."""
    arc = _native_trace(True)
    config, current_snapshot = _scenario(arc, (_circle((0, 0), 3_000_000),))
    historical_snapshot = replace(
        current_snapshot, traces=(replace(arc, is_native_arc=False),)
    )
    historical = analyze(config, historical_snapshot)
    current = analyze(config, current_snapshot)
    assert historical.sections[0].exclusion_reason == EXCLUSION_COVERED_STUB
    assert current.sections[0].exclusion_reason == ""
    assert historical.sections[0].section_id == current.sections[0].section_id
    assert historical.digest != current.digest
