"""Keep diagnostic review scope, geometry and identity outside workbook rows."""

from dataclasses import replace
import math

import pytest

from impedance.matching import analyze
from impedance.model import (
    Config,
    CopperFeature,
    LayerSettings,
    Specification,
    ValidationError,
)
from impedance.spacing_review import (
    SPACING_IMAGE_HEIGHT,
    SPACING_IMAGE_WIDTH,
    pending_spacing_reviews,
    spacing_review_entries,
)

from .test_impedance_spacing_checks import _arc, _line, _parallel, _snapshot


def _config() -> Config:
    """Declare a native differential class independently of workbook inclusion."""
    spec = Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (LayerSettings("F.Cu", ("B.Cu",), 150_000),),
    )
    return Config(enabled=True, specifications=(spec,))


def test_unchecked_manufacturing_row_does_not_dismiss_spacing_review() -> None:
    """A confirmed concern remains independently reviewable before row inclusion."""
    snapshot = _parallel(250_000, length=1_000_000)
    config = _config()
    sections = analyze(config, snapshot).sections
    entries = spacing_review_entries(config, snapshot, sections)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.pair == ("N", "P")
    assert entry.issue_code == "too_wide"
    assert set(entry.parent_section_ids) == {section.section_id for section in sections}
    assert entry.width_px == SPACING_IMAGE_WIDTH
    assert entry.height_px == SPACING_IMAGE_HEIGHT
    assert "target 0.15 mm" in entry.caption
    assert (
        spacing_review_entries(
            replace(config, included_section_ids=tuple(s.section_id for s in sections)),
            snapshot,
            sections,
        )
        == entries
    )


def test_diagnostic_retains_exact_members_and_declared_target() -> None:
    """Annotations must describe the measured intervals, not whole native tracks."""
    from impedance.spacing_checks import SpacingContext

    config, snapshot = _config(), _parallel(250_000)
    context = SpacingContext(snapshot)
    check = context.check(("N", "P"), "F.Cu", 150_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections, context=context
    )[0]
    assert entry.target_spacing_nm == 150_000
    assert entry.members == tuple(
        member
        for issue in check.issues
        for member in (issue.members or (issue.location,))
    )


def test_nearby_disconnected_concerns_remain_separate_approval_entries() -> None:
    """A convenient crop does not combine physically disconnected spacing issues."""
    snapshot = _snapshot(
        *(
            _line(net + str(index), net, ((start, y), (end, y)))
            for index, (start, end) in enumerate(
                ((100_000, 300_000), (600_000, 800_000))
            )
            for net, y in (("P", 0), ("N", 400_000))
        )
    )
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 2
    assert len({entry.issue_id for entry in entries}) == 2
    assert all(
        entry.region_gap_nm.lower_nm == entry.region_gap_nm.upper_nm == 250_000
        for entry in entries
    )


def test_grouped_caption_summarizes_many_witnesses_without_losing_geometry() -> None:
    """A hundred contiguous native segments remain one bounded-caption concern."""
    snapshot = _snapshot(
        *(
            _line(
                net + str(index), net, ((index * 20_000, y), ((index + 1) * 20_000, y))
            )
            for index in range(100)
            for net, y in (("P", 0), ("N", 400_000))
        )
    )
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    assert len(entries[0].members) >= 100
    assert len(entries[0].caption) < 250
    assert entries[0].caption.count("target") == 2
    assert entries[0].caption.count("Too wide") == 1


def test_late_bridge_connects_existing_concern_groups_independently_of_native_ids() -> (
    None
):
    """A segment discovered last joins both neighbors into one physical concern."""
    snapshot = _snapshot(
        *(
            _line(prefix + net, net, ((start, y), (end, y)))
            for prefix, start, end in (
                ("a", 0, 10_000_000),
                ("b", 20_000_000, 30_000_000),
                ("z", 10_000_000, 20_000_000),
            )
            for net, y in (("P", 0), ("N", 400_000))
        )
    )
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    assert {trace.trace_id for trace in entries[0].traces} == {
        trace.trace_id for trace in snapshot.traces
    }
    renamed = replace(
        snapshot,
        traces=tuple(
            replace(
                trace,
                trace_id="a" + trace.trace_id
                if trace.trace_id.startswith("z")
                else "z" + trace.trace_id,
            )
            for trace in reversed(snapshot.traces)
        ),
    )
    changed = spacing_review_entries(config, renamed, analyze(config, renamed).sections)
    assert len(changed) == 1
    assert changed[0].issue_id == entries[0].issue_id
    assert changed[0].view_id == entries[0].view_id


def test_varying_concern_reports_full_certified_range_and_its_worst_native_detail() -> (
    None
):
    """The merged worst witness is not the range of the entire physical concern."""
    snapshot = _snapshot(
        _line("p", "P", ((0, 0), (10_000_000, 0))),
        _line("n", "N", ((0, 400_000), (10_000_000, 600_000))),
        _line("far-p", "P", ((20_000_000, 0), (21_000_000, 0))),
        _line("far-n", "N", ((20_000_000, 1_150_000), (21_000_000, 1_150_000))),
    )
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 2
    entry = next(entry for entry in entries if entry.bounds[0] < 10_000_000)
    separate = next(other for other in entries if other is not entry)
    assert (
        separate.region_gap_nm.lower_nm == separate.region_gap_nm.upper_nm == 1_000_000
    )
    # These exact endpoint gaps are independently known from the native lines.
    # Outward solver bounds may conservatively include one nanometre.
    assert 249_999 <= entry.region_gap_nm.lower_nm <= 250_000
    assert 450_000 <= entry.region_gap_nm.upper_nm <= 450_001
    assert entry.composition.actual_gap_nm == pytest.approx(450_000)
    assert entry.representative.source_point == (10_000_000, 600_000)
    assert entry.representative.mate_point == (10_000_000, 0)
    assert "0.249999–0.450001 mm" in entry.caption


def test_concern_without_authoritative_raw_witnesses_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A located merged concern alone cannot invent a regional range or detail."""
    from impedance.spacing_checks import SpacingContext

    config, snapshot = _config(), _parallel(250_000)
    sections = analyze(config, snapshot).sections
    context = SpacingContext(snapshot)
    context.check(("N", "P"), "F.Cu", 150_000)
    measurement = context.measurement(("N", "P"), "F.Cu")
    monkeypatch.setattr(
        context,
        "measurement",
        lambda *args: replace(
            measurement,
            traces=tuple(replace(trace, witnesses=()) for trace in measurement.traces),
        ),
    )
    for derive in (spacing_review_entries, pending_spacing_reviews):
        with pytest.raises(
            ValidationError, match="authoritative native correspondence"
        ):
            derive(config, snapshot, sections, context=context)
    assert config.review_tracking.spacing_images == ()


def test_curved_review_is_one_concern_with_the_full_native_arc_in_overview() -> None:
    """One continuous curved issue gets one worst-gap detail and a full locator."""
    config = _config()
    snapshot = _snapshot(_arc("p", "P", 10_000_000), _arc("n", "N", 10_200_000))
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.composition.actual_gap_nm == pytest.approx(50_000)
    left, top, width, height = entry.composition.overview_viewport_nm
    for degree in range(181):
        angle = degree * math.pi / 180
        for radius in (10_000_000, 10_200_000):
            x, y = radius * math.cos(angle), radius * math.sin(angle)
            assert left + 75_000 <= x <= left + width - 75_000
            assert top + 75_000 <= y <= top + height - 75_000


@pytest.mark.parametrize(
    "source_radius,mate_radius",
    ((10_000_000, 12_500_000), (5_000_000, 8_000_000), (10_000_000, 13_000_000)),
)
@pytest.mark.parametrize("reversed_ids", ((), ("p",), ("n",), ("p", "n")))
def test_each_affected_arc_crosssection_shows_both_actual_mates_together(
    source_radius: int,
    mate_radius: int,
    reversed_ids: tuple[str, ...],
) -> None:
    """Separate coverage of each circle cannot stand in for seeing their true gap."""
    snapshot = _snapshot(_arc("p", "P", source_radius), _arc("n", "N", mate_radius))
    snapshot = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            if trace.trace_id in reversed_ids
            else trace
            for trace in snapshot.traces
        ),
    )
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert entries
    assert all(
        150_000 / entry.viewport_nm[2] * entry.composition.detail_rect[2] >= 24 - 1e-9
        for entry in entries
    )
    assert len(entries) == 1
    entry = entries[0]
    section = entry.representative
    assert section is not None
    left, top, width, height = entry.viewport_nm
    for point in (section.source_point, section.mate_point):
        assert left + 75_000 <= point[0] <= left + width - 75_000
        assert top + 75_000 <= point[1] <= top + height - 75_000
    assert sorted(
        math.hypot(*point) for point in (section.source_point, section.mate_point)
    ) == pytest.approx(sorted((source_radius, mate_radius)))
    assert math.dist(section.source_point, section.mate_point) == pytest.approx(
        abs(mate_radius - source_radius)
    )
    # All of the concern is visible in the overview, without requiring another
    # close-up approval for each angle of an otherwise unchanged gap.
    left, top, width, height = entry.composition.overview_viewport_nm
    for index in range(721):
        angle = index / 720 * math.pi
        for radius in (source_radius, mate_radius):
            x, y = radius * math.cos(angle), radius * math.sin(angle)
            assert left + 75_000 <= x <= left + width - 75_000
            assert top + 75_000 <= y <= top + height - 75_000


@pytest.mark.parametrize(
    "source_radius,mate_radius",
    ((10_000_000, 22_000_000), (5_000_000, 18_000_000), (10_000_000, 24_000_000)),
)
@pytest.mark.parametrize("reversed_ids", ((), ("p",), ("n",), ("p", "n")))
def test_oversized_arc_gap_fails_without_creating_approval_images(
    source_radius: int,
    mate_radius: int,
    reversed_ids: tuple[str, ...],
) -> None:
    """The original extreme gaps cannot become one-sided or unreadable evidence."""
    from impedance.spacing_checks import SpacingContext

    snapshot = _snapshot(_arc("p", "P", source_radius), _arc("n", "N", mate_radius))
    snapshot = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            if trace.trace_id in reversed_ids
            else trace
            for trace in snapshot.traces
        ),
    )
    config = _config()
    sections = analyze(config, snapshot).sections
    context = SpacingContext(snapshot)
    # Fail both derivation and a subsequent pending-review lookup. A partial
    # first attempt must not cache images that can later be approved.
    for derive in (spacing_review_entries, pending_spacing_reviews):
        with pytest.raises(ValidationError, match="both traces.*readable scale"):
            derive(config, snapshot, sections, context=context)
    assert not config.review_tracking.spacing_images


@pytest.mark.parametrize(
    "case_name",
    (
        "differential-stripline-inner-120mm",
        "differential-buried-cpwg-inner-2mm",
    ),
)
def test_wide_native_endpoint_concerns_fit_without_losing_physical_detail(
    case_name: str,
) -> None:
    """A 6.2mm endpoint separation needs a wider physical panel, not smaller copper."""
    from impedance.spacing_checks import SpacingContext
    from tests.rf_impedance_fixtures import RF_CASES

    case = next(case for case in RF_CASES if case.name == case_name)
    config, snapshot = Config(True, case.specifications()), case.snapshot()
    sections = analyze(config, snapshot).sections
    context = SpacingContext(snapshot)
    entries = spacing_review_entries(config, snapshot, sections, context=context)
    wide = tuple(entry for entry in entries if entry.composition_layout == "wide")
    assert wide
    assert any(entry.composition_layout == "standard" for entry in entries)
    for entry in wide:
        composition = entry.composition
        assert composition.detail_rect == (16, 70, 1568, 460)
        assert composition.minimum_scale == 0.35
        minimum_width = min(trace.width_nm for trace in entry.traces)
        feature_pixels = (
            minimum_width / entry.viewport_nm[2] * composition.detail_rect[2]
        )
        assert feature_pixels >= 24 - 1e-9
        assert feature_pixels * composition.minimum_scale >= 8
        assert entry.viewport_nm[2] > 6_200_000
        traces = {trace.trace_id: trace for trace in entry.traces}
        for member in entry.members:
            intervals = (
                (member.source_trace_id, *member.source_interval),
                *member.mate_intervals,
            )
            for identity, lower, upper in intervals:
                trace = traces[identity]
                # These fixtures use straight tracks: the endpoint discs enclose
                # every copper capsule, without a production coverage helper.
                assert not trace.is_native_arc and len(trace.points) == 2
                radius = trace.width_nm / 2
                for parameter in (lower, upper):
                    x, y = tuple(
                        a + (b - a) * parameter
                        for a, b in zip(trace.points[0], trace.points[1])
                    )
                    for left, top, width, height in (
                        entry.viewport_nm,
                        composition.overview_viewport_nm,
                    ):
                        assert left + radius <= x <= left + width - radius
                        assert top + radius <= y <= top + height - radius
    assert (
        pending_spacing_reviews(config, snapshot, sections, context=context) == entries
    )
    assert snapshot == case.snapshot()


def test_layout_metadata_rejects_mismatches_and_binds_metric_geometry() -> None:
    """A saved physical transform must name its exact deterministic composition."""
    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    with pytest.raises(ValidationError, match="unknown composition"):
        _ = replace(entry, composition_layout="unknown").composition
    with pytest.raises(ValidationError, match="viewport does not match"):
        _ = replace(entry, composition_layout="wide").composition
    composition = entry.composition
    assert replace(composition, metric_columns=True).fingerprint_payload != (
        composition.fingerprint_payload
    )
    assert replace(composition, overview_label_top=534).fingerprint_payload != (
        composition.fingerprint_payload
    )


def test_wide_fallback_does_not_retry_unrelated_validation_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an explicitly undersized physical frame may select the wide layout."""
    from impedance import spacing_review

    calls = []

    def fail(*args: object, **kwargs: object) -> tuple[object, ...]:
        """Represent missing geometry or another non-readability validation error."""
        calls.append((args, kwargs))
        raise ValidationError("No authoritative native correspondence")

    monkeypatch.setattr(spacing_review, "_viewports", fail)
    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    with pytest.raises(ValidationError, match="authoritative native correspondence"):
        spacing_review_entries(config, snapshot, analyze(config, snapshot).sections)
    assert len(calls) == 1


@pytest.mark.parametrize("reversed_ids", (("p",), ("n",), ("p", "n")))
def test_adaptive_arc_frames_keep_exact_identity_order_when_tracks_reverse(
    reversed_ids: tuple[str, ...],
) -> None:
    """Equivalent native directions must not create needless image reapprovals."""
    snapshot = _snapshot(_arc("p", "P", 10_000_000), _arc("n", "N", 13_000_000))
    config = _config()
    original = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    changed = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            if trace.trace_id in reversed_ids
            else trace
            for trace in snapshot.traces
        ),
    )
    current = spacing_review_entries(config, changed, analyze(config, changed).sections)
    assert [
        (entry.issue_id, entry.view_id, entry.input_digest, entry.viewport_nm)
        for entry in current
    ] == [
        (entry.issue_id, entry.view_id, entry.input_digest, entry.viewport_nm)
        for entry in original
    ]


def test_missing_associated_native_geometry_cannot_create_one_sided_images() -> None:
    """An empty mate list cannot vacuously satisfy joint coverage."""
    from impedance.spacing_checks import SpacingCrossSection, _primitives
    from impedance.spacing_review import _viewports

    source = _primitives(_line("p", "P", ((0, 0), (2_000_000, 0))))
    with pytest.raises(ValidationError, match="both traces.*readable scale"):
        _viewports(
            (-75_000, -75_000, 2_075_000, 475_000),
            150_000,
            ((source, ()),),
            representative=SpacingCrossSection(
                "p", "n", (0, 0), (0, 400_000), 150_000, 150_000
            ),
        )


def test_reciprocal_and_reordered_geometry_has_one_stable_concern() -> None:
    """Swapping mate order or reversing native strokes cannot create a second issue."""
    snapshot = _parallel(250_000)
    config = _config()
    original = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    reversed_snapshot = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            for trace in reversed(snapshot.traces)
        ),
        differential_pairs=(("N", "P"),),
    )
    changed = spacing_review_entries(
        config, reversed_snapshot, analyze(config, reversed_snapshot).sections
    )
    assert len(original) == len(changed)
    assert len({entry.issue_id for entry in original + changed}) == 1
    assert [entry.view_id for entry in original] == [entry.view_id for entry in changed]
    assert original[0].issue_id == changed[0].issue_id
    assert original[0].view_id == changed[0].view_id


def test_long_uniform_issue_is_one_concern_with_full_overview_and_readable_detail() -> (
    None
):
    """Route length does not multiply approvals for the same constant gap."""
    snapshot = _parallel(50_000, length=80_000_000)
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    entry = entries[0]
    assert "close-up" not in entry.caption
    left, top, width, height = entry.composition.overview_viewport_nm
    assert left <= -75_000 and left + width >= 80_075_000
    assert top <= -75_000 and top + height >= 275_000
    source_pixels = 50_000 / entry.viewport_nm[2] * entry.composition.detail_rect[2]
    assert source_pixels >= 24 - 1e-9
    assert source_pixels * entry.composition.minimum_scale >= 8
    assert entry.composition.actual_gap_nm == pytest.approx(50_000)


@pytest.mark.parametrize("vertical", (False, True))
@pytest.mark.parametrize("reverse", (False, True))
def test_endpoint_worst_detail_uses_the_route_side_of_its_readable_frame(
    vertical: bool,
    reverse: bool,
) -> None:
    """Keep the true endpoint witness visible without wasting half the detail."""
    snapshot = _parallel(331_300, length=24_000_000)
    snapshot = replace(
        snapshot,
        traces=tuple(
            replace(
                trace,
                points=tuple(reversed(trace.points)) if reverse else trace.points,
            )
            for trace in snapshot.traces
        ),
    )
    if vertical:
        snapshot = replace(
            snapshot,
            traces=tuple(
                replace(trace, points=tuple((y, x) for x, y in trace.points))
                for trace in snapshot.traces
            ),
        )
    config = _config()
    spec = config.specifications[0]
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=202_300),),
            ),
        ),
    )
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    entry = entries[0]
    left, top, width, height = entry.viewport_nm
    origin, extent = (top, height) if vertical else (left, width)
    assert origin >= -1_000_000
    assert origin + extent >= (2_500_000 if vertical else 5_000_000)
    assert entry.composition.actual_gap_nm == pytest.approx(331_300)
    witness = entry.representative
    assert witness is not None
    for point in (witness.source_point, witness.mate_point):
        assert left + 75_000 <= point[0] <= left + width - 75_000
        assert top + 75_000 <= point[1] <= top + height - 75_000
        assert point[1 if vertical else 0] == 0


def test_five_plus_two_repeated_pair_crops_become_two_concern_entries() -> None:
    """The reported USB pair gaps need one approval each, irrespective of length."""
    snapshot = _snapshot(
        _line("p", "/PONG_D+", ((0, 0), (26_000_000, 0))),
        _line("n", "/PONG_D-", ((0, 481_300), (26_000_000, 481_300))),
        _line("in-p", "/PONG_IN_D+", ((0, 10_000_000), (10_000_000, 10_000_000))),
        _line("in-n", "/PONG_IN_D-", ((0, 10_356_300), (10_000_000, 10_356_300))),
    )
    pairs = (("/PONG_D+", "/PONG_D-"), ("/PONG_IN_D+", "/PONG_IN_D-"))
    snapshot = replace(
        snapshot,
        net_class_memberships=tuple((net, ("USB",)) for pair in pairs for net in pair),
        differential_pairs=pairs,
    )
    config = _config()
    spec = config.specifications[0]
    config = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=202_300),),
            ),
        ),
    )
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 2
    assert {entry.pair for entry in entries} == {tuple(sorted(pair)) for pair in pairs}
    assert sorted(
        entry.composition.actual_gap_nm for entry in entries
    ) == pytest.approx([206_300, 331_300])


def test_splitting_same_copper_keeps_physical_concern_identity() -> None:
    """Native track segmentation is not a different physical spacing concern."""
    snapshot = _parallel(250_000)
    config = _config()
    original = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    segmented = replace(
        snapshot,
        traces=(
            _line("p0", "P", ((0, 0), (4_000_000, 0))),
            _line("p1", "P", ((4_000_000, 0), (10_000_000, 0))),
            _line("n0", "N", ((0, 400_000), (4_000_000, 400_000))),
            _line("n1", "N", ((4_000_000, 400_000), (10_000_000, 400_000))),
        ),
    )
    changed = spacing_review_entries(
        config, segmented, analyze(config, segmented).sections
    )
    assert len(original) == len(changed)
    assert len({entry.issue_id for entry in original + changed}) == 1
    assert [entry.view_id for entry in original] == [entry.view_id for entry in changed]
    assert original[0].issue_id == changed[0].issue_id


def test_splitting_and_renaming_native_arcs_keeps_physical_concern_identity() -> None:
    """Identical circular copper must not be identified by sampled chord unions."""
    snapshot = _snapshot(_arc("p", "P", 5_000_000), _arc("n", "N", 5_400_000))
    config = _config()
    original = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    quarters = []
    for net, radius in (("P", 5_000_000), ("N", 5_400_000)):
        for suffix, start, end, middle in (
            ("a", (radius, 0), (0, radius), (3 * radius // 5, 4 * radius // 5)),
            ("b", (0, radius), (-radius, 0), (-3 * radius // 5, 4 * radius // 5)),
        ):
            quarters.append(
                replace(
                    _arc(net + suffix, net, radius),
                    points=(end, start),
                    native_arc_midpoint=middle,
                )
            )
    segmented = replace(snapshot, traces=tuple(reversed(quarters)))
    changed = spacing_review_entries(
        config, segmented, analyze(config, segmented).sections
    )
    assert len({entry.issue_id for entry in original}) == 1
    assert len({entry.issue_id for entry in changed}) == 1
    assert original[0].bounds == changed[0].bounds
    assert original[0].issue_id == changed[0].issue_id
    assert {entry.view_id for entry in original} == {entry.view_id for entry in changed}


def test_curved_concern_does_not_create_images_of_empty_circle_interior() -> None:
    """Every curved-route close-up contains actual affected source copper."""
    snapshot = _snapshot(_arc("p", "P", 10_000_000), _arc("n", "N", 10_200_000))
    config = _config()
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert len(entries) == 1
    for entry in entries:
        left, top, width, height = entry.viewport_nm
        corners = (
            (left, top),
            (left + width, top),
            (left, top + height),
            (left + width, top + height),
        )
        # A frame completely inside the inner circle cannot show either trace.
        assert max(x * x + y * y for x, y in corners) >= (9_925_000) ** 2
    # Independent known-circle samples ensure filtering cannot drop a real part
    # of either affected arc while removing the empty interior frames.
    for degree in range(181):
        angle = degree * math.pi / 180
        for radius in (10_000_000, 10_200_000):
            x, y = radius * math.cos(angle), radius * math.sin(angle)
            assert any(
                left <= x <= left + width and top <= y <= top + height
                for entry in entries
                for left, top, width, height in (
                    entry.composition.overview_viewport_nm,
                )
            )


def test_unreadable_separation_cannot_create_one_sided_approval_images() -> None:
    """Every required image must show both mates at the minimum detail scale."""
    config, snapshot = _config(), _parallel(20_000_000)
    with pytest.raises(ValidationError, match="both traces.*readable scale"):
        spacing_review_entries(config, snapshot, analyze(config, snapshot).sections)


@pytest.mark.parametrize("invalid", ("mate_identity", "mate_interval", "mate_point"))
def test_invalid_native_association_cannot_create_review_images(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    """A nearby trace cannot substitute for an authoritative associated native mate."""
    from impedance.spacing_checks import SpacingContext, SpacingLocation

    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    sections = analyze(config, snapshot).sections
    context = SpacingContext(snapshot)
    measurement = context.measurement(("N", "P"), "F.Cu")

    def invalidate(location: SpacingLocation) -> SpacingLocation:
        """Corrupt one association while retaining real measured route geometry."""
        if invalid == "mate_interval":
            return replace(location, mate_intervals=())
        return replace(
            location,
            cross_sections=tuple(
                replace(section, mate_trace_id=section.source_trace_id)
                if invalid == "mate_identity"
                else replace(section, mate_point=(20_000_000, 20_000_000))
                for section in location.cross_sections
            ),
        )

    measurement = replace(
        measurement,
        traces=tuple(
            replace(
                trace,
                witnesses=tuple(
                    replace(witness, location=invalidate(witness.location))
                    if witness.location is not None
                    else witness
                    for witness in trace.witnesses
                ),
            )
            for trace in measurement.traces
        ),
    )
    monkeypatch.setattr(context, "measurement", lambda *args: measurement)
    for derive in (spacing_review_entries, pending_spacing_reviews):
        with pytest.raises(
            ValidationError, match="authoritative native correspondence"
        ):
            derive(config, snapshot, sections, context=context)
    assert not config.review_tracking.spacing_images


def test_changed_pad_or_target_invalidates_spacing_image_inputs() -> None:
    """Pad regions and target are approval inputs even when a concern stays put."""
    snapshot = _parallel(250_000)
    config = _config()
    sections = analyze(config, snapshot).sections
    original = spacing_review_entries(config, snapshot, sections)[0]
    feature = CopperFeature(
        "pad", "P", "F.Cu", "pad", "rect", (-1_000_000, 0), 100_000, 100_000
    )
    changed = replace(snapshot, copper_features=(feature,))
    entry = spacing_review_entries(config, changed, sections)[0]
    assert entry.input_digest != original.input_digest
    spec = config.specifications[0]
    settings = replace(spec.layer_settings[0], spacing_nm=200_000)
    changed_config = replace(
        config, specifications=(replace(spec, layer_settings=(settings,)),)
    )
    entry = spacing_review_entries(changed_config, snapshot, sections)[0]
    assert entry.input_digest != original.input_digest


def test_matching_pair_and_disabled_config_produce_no_diagnostic_entries() -> None:
    """Ordinary matching copper remains only a manufacturing capture."""
    snapshot = _parallel()
    config = _config()
    sections = analyze(config, snapshot).sections
    assert spacing_review_entries(config, snapshot, sections) == ()
    assert spacing_review_entries(Config(), snapshot, sections) == ()


def test_review_image_limit_fails_instead_of_silently_omitting_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concern budget fails closed instead of omitting another physical issue."""
    from impedance import spacing_review

    snapshot = _snapshot(
        _line("p0", "P", ((0, 0), (1_000_000, 0))),
        _line("n0", "N", ((0, 400_000), (1_000_000, 400_000))),
        _line("p1", "P", ((10_000_000, 0), (11_000_000, 0))),
        _line("n1", "N", ((10_000_000, 400_000), (11_000_000, 400_000))),
    )
    monkeypatch.setattr(spacing_review, "MAX_SPACING_REVIEW_IMAGES", 1)
    config = _config()
    with pytest.raises(ValidationError, match="spacing review images"):
        spacing_review_entries(config, snapshot, analyze(config, snapshot).sections)


def test_pending_images_require_each_explicit_matching_approval() -> None:
    """Viewing or approving one physical concern cannot clear a separate concern."""
    from impedance.review_tracking import (
        SpacingImageReview,
        put_spacing_image,
        spacing_capture_fingerprint,
    )

    snapshot = _snapshot(
        _line("p0", "P", ((0, 0), (1_000_000, 0))),
        _line("n0", "N", ((0, 200_000), (1_000_000, 200_000))),
        _line("p1", "P", ((10_000_000, 0), (11_000_000, 0))),
        _line("n1", "N", ((10_000_000, 200_000), (11_000_000, 200_000))),
    )
    config = _config()
    sections = analyze(config, snapshot).sections
    entries = spacing_review_entries(config, snapshot, sections)
    assert len(entries) > 1
    entry = entries[0]
    capture_digest = spacing_capture_fingerprint(entry.input_digest, "a" * 64)
    viewed = SpacingImageReview(
        entry.spec_id,
        entry.layer,
        entry.issue_id,
        entry.view_id,
        entry.input_digest,
        capture_digest,
        "2026-10-24T12:00:00.000000Z",
    )
    config = replace(
        config, review_tracking=put_spacing_image(config.review_tracking, viewed)
    )
    assert pending_spacing_reviews(config, snapshot, sections) == entries
    approved = replace(
        viewed,
        approved_digest=capture_digest,
        approved_at_utc="2026-10-24T12:01:00.000000Z",
    )
    config = replace(
        config, review_tracking=put_spacing_image(config.review_tracking, approved)
    )
    pending = pending_spacing_reviews(config, snapshot, sections)
    assert tuple(item.view_id for item in pending) == tuple(
        item.view_id for item in entries[1:]
    )
    mismatched_issue = replace(approved, issue_id="f" * 64)
    config = replace(
        config,
        review_tracking=put_spacing_image(config.review_tracking, mismatched_issue),
    )
    assert pending_spacing_reviews(config, snapshot, sections) == entries


def test_spacing_review_images_are_not_manufacturing_sections_or_rows() -> None:
    """Diagnostic scope has its own concrete type and cannot become a workbook row."""
    from impedance.model import Section
    from impedance.service import ReportRow

    config, snapshot = _config(), _parallel(250_000)
    entry = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )[0]
    assert not isinstance(entry, Section)
    assert not isinstance(entry, ReportRow)
    assert not hasattr(entry, "section_id")


def test_context_reuses_geometry_entries_without_caching_approval_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated UI/export inspection does not rederive immutable issue geometry."""
    from impedance import spacing_review
    from impedance.spacing_checks import SpacingContext

    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    sections = analyze(config, snapshot).sections
    context = SpacingContext(snapshot)
    comparisons = []
    original_compare = context.check

    def compare(*args: object, **kwargs: object) -> object:
        comparisons.append(1)
        return original_compare(*args, **kwargs)

    monkeypatch.setattr(context, "check", compare)
    entries = spacing_review_entries(config, snapshot, sections, context=context)
    repeated = spacing_review_entries(
        replace(config, included_section_ids=(sections[0].section_id,)),
        snapshot,
        tuple(reversed(sections)),
        context=context,
    )
    assert repeated is entries
    assert len(comparisons) == 1
    assert (
        pending_spacing_reviews(config, snapshot, sections, context=context) == entries
    )
    assert len(comparisons) == 1
    from impedance.review_tracking import (
        SpacingImageReview,
        put_spacing_image,
        spacing_capture_fingerprint,
    )

    entry = entries[0]
    digest = spacing_capture_fingerprint(entry.input_digest, "a" * 64)
    approved = SpacingImageReview(
        entry.spec_id,
        entry.layer,
        entry.issue_id,
        entry.view_id,
        entry.input_digest,
        digest,
        "2026-10-24T12:00:00.000000Z",
        digest,
        "2026-10-24T12:01:00.000000Z",
    )
    approved_config = replace(
        config, review_tracking=put_spacing_image(config.review_tracking, approved)
    )
    assert (
        pending_spacing_reviews(approved_config, snapshot, sections, context=context)
        == ()
    )
    assert len(comparisons) == 1
    spec = config.specifications[0]
    changed = replace(
        config,
        specifications=(
            replace(
                spec,
                layer_settings=(replace(spec.layer_settings[0], spacing_nm=200_000),),
            ),
        ),
    )
    updated = spacing_review_entries(changed, snapshot, sections, context=context)
    assert updated[0].input_digest != entries[0].input_digest
    assert len(comparisons) == 2
    monkeypatch.setattr(
        spacing_review,
        "SPACING_RENDER_POLICY_REVISION",
        spacing_review.SPACING_RENDER_POLICY_REVISION + 1,
    )
    assert (
        spacing_review_entries(changed, snapshot, sections, context=context) != updated
    )
    assert len(comparisons) == 3


def test_context_bounds_historical_intent_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing targets repeatedly cannot retain an unbounded set of image entries."""
    from impedance.spacing_checks import SpacingContext

    config, snapshot = _config(), _parallel(250_000)
    sections = analyze(config, snapshot).sections
    context = SpacingContext(snapshot)
    original_compare = context.check
    comparisons = []

    def compare(*args: object, **kwargs: object) -> object:
        comparisons.append(1)
        return original_compare(*args, **kwargs)

    monkeypatch.setattr(context, "check", compare)
    original = spacing_review_entries(config, snapshot, sections, context=context)
    spec = config.specifications[0]
    for index in range(20):
        changed = replace(
            config,
            specifications=(
                replace(
                    spec,
                    layer_settings=(
                        replace(spec.layer_settings[0], spacing_nm=160_000 + index),
                    ),
                ),
            ),
        )
        spacing_review_entries(changed, snapshot, sections, context=context)
    assert len(comparisons) == 21
    assert (
        spacing_review_entries(config, snapshot, sections, context=context) == original
    )
    assert len(comparisons) == 22


@pytest.mark.parametrize("legacy_revision", (1, 2, 3, 4, 5, 6))
def test_earlier_render_policy_approval_requires_new_diagnostic_review(
    monkeypatch: pytest.MonkeyPatch,
    legacy_revision: int,
) -> None:
    """Earlier PNG consent remains history and cannot approve percentage review images."""
    from impedance import spacing_review
    from impedance.matching import CAPTURE_POLICY_REVISION
    from impedance.review_tracking import (
        SpacingImageReview,
        put_spacing_image,
        spacing_capture_fingerprint,
    )
    from impedance.spacing_checks import SpacingContext

    config, snapshot = _config(), _parallel(50_000, length=40_000_000)
    analysis = analyze(config, snapshot)
    context = SpacingContext(snapshot)
    with monkeypatch.context() as legacy_policy:
        legacy_policy.setattr(
            spacing_review, "SPACING_RENDER_POLICY_REVISION", legacy_revision
        )
        old_entries = spacing_review_entries(
            config, snapshot, analysis.sections, context=context
        )
        history = config.review_tracking
        for entry in old_entries:
            digest = spacing_capture_fingerprint(entry.input_digest, "a" * 64)
            history = put_spacing_image(
                history,
                SpacingImageReview(
                    entry.spec_id,
                    entry.layer,
                    entry.issue_id,
                    entry.view_id,
                    entry.input_digest,
                    digest,
                    "2026-10-24T12:00:00.000000Z",
                    digest,
                    "2026-10-24T12:01:00.000000Z",
                ),
            )
        saved = replace(config, review_tracking=history)
        assert (
            pending_spacing_reviews(saved, snapshot, analysis.sections, context=context)
            == ()
        )
        persisted = saved.to_dict()

    reopened = Config.from_dict(persisted)
    current = spacing_review_entries(
        reopened, snapshot, analysis.sections, context=context
    )
    assert (
        pending_spacing_reviews(reopened, snapshot, analysis.sections, context=context)
        == current
    )
    assert current and len(current) == len(old_entries)
    assert {entry.view_id for entry in current}.isdisjoint(
        entry.view_id for entry in old_entries
    )
    assert {entry.input_digest for entry in current}.isdisjoint(
        entry.input_digest for entry in old_entries
    )
    assert [
        (
            entry.issue_id,
            entry.bounds,
            entry.viewport_nm,
            entry.width_px,
            entry.height_px,
        )
        for entry in current
    ] == [
        (
            entry.issue_id,
            entry.bounds,
            entry.viewport_nm,
            entry.width_px,
            entry.height_px,
        )
        for entry in old_entries
    ]
    assert reopened.review_tracking == saved.review_tracking
    assert spacing_review.SPACING_RENDER_POLICY_REVISION == 7
    assert analyze(reopened, snapshot).sections == analysis.sections
    assert CAPTURE_POLICY_REVISION == 8
    assert spacing_review.MIN_SPACING_FEATURE_PIXELS == 24
    assert spacing_review.MIN_SPACING_DISPLAY_FEATURE_PIXELS == 8


def test_combined_panel_contract_uses_detail_pixels_and_preserves_region_bounds() -> (
    None
):
    """The physical detail, not the full composition, sets coverage/readability."""
    from impedance.spacing_review import MIN_SPACING_FEATURE_PIXELS

    config, snapshot = _config(), _parallel(50_000, length=20_000_000)
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    for entry in entries:
        composition = entry.composition
        x, y, width, height = composition.detail_rect
        assert 0 < x < x + width < entry.width_px
        assert 0 < y < y + height < entry.height_px
        assert entry.viewport_nm[2] / entry.viewport_nm[3] == pytest.approx(
            width / height
        )
        assert (
            50_000 / entry.viewport_nm[2] * width >= MIN_SPACING_FEATURE_PIXELS - 1e-9
        )
        assert composition.required_regions == (
            composition.detail_rect,
            composition.overview_rect,
            composition.metrics_rect,
            composition.region_rect,
            composition.schematic_rect,
        )
        assert composition.minimum_scale == 0.35
        assert MIN_SPACING_FEATURE_PIXELS * composition.minimum_scale >= 8
        assert composition.region_gap_nm.lower_nm == 50_000
        assert composition.region_gap_nm.upper_nm == 50_000
        assert composition.actual_gap_nm == pytest.approx(50_000)


@pytest.mark.parametrize("layout", ("standard", "wide"))
@pytest.mark.parametrize("state", ("local", "regional", "target_absent", "target_zero"))
def test_required_panels_include_percentage_footer_for_every_measurement_state(
    state: str, layout: str
) -> None:
    """A percentage or explicit Unavailable footer always requires readable display."""
    config, snapshot = _config(), _parallel(250_000, length=1_000_000)
    entry = replace(
        spacing_review_entries(config, snapshot, analyze(config, snapshot).sections)[0],
        composition_layout=layout,
    )
    if layout == "wide":
        from impedance.spacing_composition import WIDE_DETAIL_RECT

        left, top, width, height = entry.viewport_nm
        wide_width = height * WIDE_DETAIL_RECT[2] / WIDE_DETAIL_RECT[3]
        entry = replace(
            entry,
            viewport_nm=(left - (wide_width - width) / 2, top, wide_width, height),
        )
    if state == "regional":
        entry = replace(
            entry,
            members=tuple(
                replace(member, cross_sections=()) for member in entry.members
            ),
            representative=None,
        )
    elif state == "target_absent":
        entry = replace(entry, target_spacing_nm=None, representative=None)
    elif state == "target_zero":
        entry = replace(entry, target_spacing_nm=0, representative=None)
    composition = entry.composition
    expected = (
        composition.detail_rect,
        composition.overview_rect,
        composition.metrics_rect,
        composition.region_rect,
        composition.schematic_rect,
    )
    if state == "local":
        assert composition.actual_gap_nm is not None
    else:
        assert composition.actual_gap_nm is None
    assert composition.required_regions == expected
    assert composition.target_spacing_nm == entry.target_spacing_nm
    if state == "regional":
        changed_target = replace(entry, target_spacing_nm=200_000).composition
        assert changed_target.required_regions == expected
        assert changed_target.fingerprint_payload != composition.fingerprint_payload
