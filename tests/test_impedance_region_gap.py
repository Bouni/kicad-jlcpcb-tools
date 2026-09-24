"""Measure exact group clearance without computing unused per-track clearances."""

from collections.abc import Iterable
from dataclasses import replace
import math
import random

import pytest

from impedance import matching, regions
from impedance.capture_geometry import CAPTURE_WIDTH_PX, capture_viewport
from impedance.model import BoardSnapshot, Bounds, Trace
from tests.test_impedance_region_growth import _scenario


def _segmented_route(net: str, y: int) -> tuple[Trace, ...]:
    """Represent a routed signal with 32 distinct native track identities."""
    return tuple(
        Trace(
            f"{net}-{index}",
            "F.Cu",
            net,
            200_000,
            ((index * 500_000, y), ((index + 1) * 500_000, y)),
        )
        for index in range(32)
    )


def _brute_gap(snapshot: BoardSnapshot, trace: Trace) -> float:
    """Check every eligible centerline pair without pruning or aggregate caches."""
    return min(
        (
            max(
                0.0,
                regions._segment_distance(a, b, c, d)
                - (trace.width_nm + other.width_nm) / 2,
            )
            for other in snapshot.traces
            if other.layer == trace.layer and other.net != trace.net
            for a, b in zip(trace.points, trace.points[1:])
            for c, d in zip(other.points, other.points[1:])
        ),
        default=math.inf,
    )


def test_group_quality_does_not_scan_all_foreign_tracks_for_each_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete route needs one exact minimum, not 32 complete gap searches."""
    signal = _segmented_route("SIGNAL", 0)
    foreign = _segmented_route("NEIGHBOR", 3_000_000)
    context = tuple(
        Trace(
            f"context-{index}",
            "F.Cu",
            "GND",
            200_000,
            ((index * 100_000, 40_000_000), ((index + 1) * 100_000, 40_000_000)),
        )
        for index in range(96)
    )
    snapshot = BoardSnapshot(("F.Cu",), signal + foreign + context)
    distances = 0
    distance = regions._distance

    def measured(left: Bounds, right: Bounds) -> float:
        """Count real broad-phase work while preserving its geometry."""
        nonlocal distances
        distances += 1
        return distance(left, right)

    monkeypatch.setattr(regions, "_distance", measured)
    quality = regions.region_quality(signal, snapshot)
    bounds = (-100_000, -100_000, 16_100_000, 100_000)
    scale = capture_viewport(bounds)[2] / CAPTURE_WIDTH_PX
    assert quality.trace_pixels == pytest.approx(200_000 / scale)
    assert quality.gap_pixels == pytest.approx(2_800_000 / scale)
    assert quality.foreign_ratio == 0
    assert distances <= 2 * len(snapshot.traces)


def test_reordered_group_reuses_its_completed_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geometric growth and ID-sorted review rows measure the same route once."""
    signal = _segmented_route("SIGNAL", 0)
    snapshot = BoardSnapshot(("F.Cu",), signal + _segmented_route("OTHER", 3_000_000))
    context = regions.RegionGeometry(snapshot).context({"SIGNAL"}, "F.Cu")
    minimum_gap = context._minimum_gap
    calls = 0

    def measured(traces: tuple[Trace, ...], bounds: Bounds) -> float:
        """Observe the completed physical measurement, preserving its return value."""
        nonlocal calls
        calls += 1
        return minimum_gap(traces, bounds)

    monkeypatch.setattr(context, "_minimum_gap", measured)
    initial = context.measure(signal)
    reordered = context.measure(tuple(reversed(signal)))
    assert reordered == initial
    assert calls == 1
    assert reordered is initial


def test_first_query_order_does_not_change_quality_near_density_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical accumulation keeps fresh contexts consistent at a rounding boundary."""

    def scalar_sum(values: Iterable[float]) -> float:
        """Expose ordinary floating-point addition on every supported interpreter."""
        total = 0.0
        for value in values:
            total += value
        return total

    monkeypatch.setattr(regions, "sum", scalar_sum, raising=False)
    width = 5_000_000
    members = (
        Trace("a", "F.Cu", "S", width, ((0, 0), (1_000_000_000, 0))),
        Trace("b", "F.Cu", "S", width, ((0, 80_000_000), (1_000_000_000, 80_000_000))),
        Trace("c", "F.Cu", "S", width, ((0, 40_000_000), (1, 40_000_001))),
        Trace("d", "F.Cu", "S", width, ((0, 40_000_100), (1, 40_000_101))),
    )
    foreign = tuple(
        Trace(
            f"{trace.trace_id}-{index}",
            "F.Cu",
            "OTHER",
            width,
            tuple(
                (
                    x + (100_000_000 if trace.trace_id in ("c", "d") else 0),
                    y + {"a": 20_000_000, "b": -20_000_000}.get(trace.trace_id, 0),
                )
                for x, y in trace.points
            ),
        )
        for index in range(5)
        for trace in members
    )
    snapshot = BoardSnapshot(("F.Cu",), members + foreign)
    first = regions.RegionGeometry(snapshot).context({"S"}, "F.Cu").measure(members)
    reordered = (
        regions.RegionGeometry(snapshot)
        .context({"S"}, "F.Cu")
        .measure((members[0], members[2], members[3], members[1]))
    )
    assert first.foreign_ratio == pytest.approx(regions.MAX_FOREIGN_LENGTH_RATIO)
    assert first == reordered
    assert first.readable == reordered.readable


@pytest.mark.parametrize("known", ["none", "partial", "all"])
@pytest.mark.parametrize(
    ("members", "foreign", "expected_gap"),
    [
        pytest.param(
            (
                Trace("p", "F.Cu", "P", 200_000, ((0, 0), (2_000_000, 0))),
                Trace("n", "F.Cu", "N", 100_000, ((0, 700_000), (2_000_000, 700_000))),
            ),
            (),
            550_000,
            id="represented-pair",
        ),
        pytest.param(
            (
                Trace("a", "F.Cu", "S", 200_000, ((0, 0), (1_000_000, 0))),
                Trace("b", "F.Cu", "S", 400_000, ((1_000_000, 0), (2_000_000, 0))),
            ),
            (
                Trace(
                    "other",
                    "F.Cu",
                    "OTHER",
                    100_000,
                    ((0, 1_000_000), (2_000_000, 1_000_000)),
                ),
            ),
            750_000,
            id="mixed-width",
        ),
        pytest.param(
            (
                Trace(
                    "arc",
                    "F.Cu",
                    "S",
                    200_000,
                    ((0, 0), (1_000_000, 500_000), (2_000_000, 0)),
                    is_native_arc=True,
                ),
                Trace("far", "F.Cu", "S", 200_000, ((20_000_000, 0), (21_000_000, 0))),
            ),
            (
                Trace(
                    "other",
                    "F.Cu",
                    "OTHER",
                    200_000,
                    ((0, 1_000_000), (2_000_000, 1_000_000)),
                ),
            ),
            300_000,
            id="native-arc-samples",
        ),
        pytest.param(
            (
                Trace("point", "F.Cu", "S", 200_000, ((0, 0), (0, 0))),
                Trace("far", "F.Cu", "S", 200_000, ((2_000_000, 0), (2_000_000, 0))),
            ),
            (
                Trace(
                    "other",
                    "F.Cu",
                    "OTHER",
                    200_000,
                    ((1_000_000, -1_000_000), (1_000_000, 1_000_000)),
                ),
            ),
            800_000,
            id="zero-length",
        ),
        pytest.param(
            (
                Trace("a", "F.Cu", "S", 200_000, ((0, 0), (1_000_000, 0))),
                Trace("b", "F.Cu", "S", 200_000, ((1_000_000, 0), (2_000_000, 0))),
            ),
            (),
            math.inf,
            id="no-other-net",
        ),
    ],
)
def test_group_gap_preserves_exact_clearance_and_existing_per_track_evidence(
    members: tuple[Trace, ...],
    foreign: tuple[Trace, ...],
    expected_gap: float,
    known: str,
) -> None:
    """Aggregate minima include pair mates without fabricating per-track values."""
    snapshot = BoardSnapshot(("F.Cu",), members + foreign)
    geometry = regions.RegionGeometry(snapshot)
    context = geometry.context({trace.net for trace in members}, "F.Cu")
    count = {"none": 0, "partial": 1, "all": len(members)}[known]
    for trace in members[:count]:
        context.gap(trace)
    previous = dict(geometry.gaps)
    bounds = regions.union_bounds(
        tuple(geometry.boxes[trace.trace_id] for trace in members)
    )
    scale = capture_viewport(bounds)[2] / CAPTURE_WIDTH_PX
    quality = context.measure(members)
    assert quality.gap_pixels == pytest.approx(expected_gap / scale)
    assert geometry.gaps == previous
    assert context.measure(members) is quality


def test_random_group_clearance_matches_brute_force_with_partial_evidence() -> None:
    """Independent all-pairs clearance covers mixed nets, widths, layers and samples."""
    generator = random.Random(250)
    for case in range(50):
        traces = tuple(
            Trace(
                str(index),
                "F.Cu" if index < 8 else "B.Cu",
                "SIGNAL" if case % 2 and index < 4 else str(index % 3),
                generator.randrange(1, 50) * 10_000,
                tuple(
                    (
                        generator.randrange(-1000, 1001) * 10_000,
                        index * 1_000_000
                        if case % 4 == 0
                        else generator.randrange(-1000, 1001) * 10_000,
                    )
                    for _ in range(generator.randrange(2, 5))
                ),
            )
            for index in range(10)
        )
        members = traces[:4]
        snapshot = BoardSnapshot(("F.Cu", "B.Cu"), traces)
        geometry = regions.RegionGeometry(snapshot)
        context = geometry.context({trace.net for trace in members}, "F.Cu")
        for trace in members[: case % 5]:
            assert context.gap(trace) == _brute_gap(snapshot, trace)
        previous = dict(geometry.gaps)
        bounds = regions.union_bounds(
            tuple(geometry.boxes[trace.trace_id] for trace in members)
        )
        scale = capture_viewport(bounds)[2] / CAPTURE_WIDTH_PX
        expected = min(_brute_gap(snapshot, trace) for trace in members)
        assert context.measure(members).gap_pixels == expected / scale
        assert geometry.gaps == previous


@pytest.mark.parametrize("scenario", ["normal", "reversed", "dense", "long-outlier"])
def test_complete_analysis_retains_per_track_reference_results(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Grouping, warnings, row identities and review digests retain exact behavior."""
    signal = _segmented_route("SIGNAL", 0)
    foreign = _segmented_route("OTHER", 3_000_000)
    if scenario == "reversed":
        signal = tuple(reversed(signal))
        foreign = tuple(reversed(foreign))
    elif scenario == "dense":
        foreign = tuple(
            replace(trace, points=tuple((x, 150_000) for x, _ in trace.points))
            for trace in foreign
        )
    elif scenario == "long-outlier":
        foreign = (
            replace(foreign[0], points=((0, -200_000_000), (0, 400_000_000))),
            *foreign[1:],
        )
    config, snapshot = _scenario(*signal, *foreign)
    actual = matching.analyze(config, snapshot)

    def per_track_gap(context: regions._RegionContext, trace: Trace) -> float:
        """Keep incremental growth independent of the new minimum-gap helper."""
        if trace.trace_id not in context.gaps:
            context.gaps[trace.trace_id] = _brute_gap(context.snapshot, trace)
        return context.gaps[trace.trace_id]

    def per_track_quality(
        context: regions._RegionContext, traces: tuple[Trace, ...]
    ) -> regions.RegionQuality:
        """Retain the original exact per-track algorithm as an analysis oracle."""
        key = tuple(trace.trace_id for trace in traces)
        if key not in context.measurements:
            context.measurements[key] = context.quality(
                regions.union_bounds(
                    tuple(context.boxes[trace.trace_id] for trace in traces)
                ),
                min(trace.width_nm for trace in traces),
                sum(context.lengths[trace.trace_id] for trace in traces),
                min(context.gap(trace) for trace in traces),
            )
        return context.measurements[key]

    monkeypatch.setattr(regions._RegionContext, "gap", per_track_gap)
    monkeypatch.setattr(regions._RegionContext, "measure", per_track_quality)
    assert actual == matching.analyze(config, snapshot)
