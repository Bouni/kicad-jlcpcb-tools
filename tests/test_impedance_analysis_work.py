"""Guard shared analysis work and branches that cross actual-width partitions."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from impedance import matching, regions
from impedance.model import (
    BoardSnapshot,
    Config,
    CopperFeature,
    LayerSettings,
    Specification,
    Trace,
)


def _scenario(signals: int = 12, context: int = 80) -> tuple[Config, BoardSnapshot]:
    """Use independently routed nets with unrelated copper on the same layer."""
    routes = tuple(
        Trace(
            f"s{index}",
            "F.Cu",
            f"RF{index}",
            200_000,
            ((index * 5_000_000, 0), (index * 5_000_000 + 1_000_000, 0)),
        )
        for index in range(signals)
    )
    others = tuple(
        Trace(
            f"c{index}",
            "F.Cu",
            "OTHER",
            100_000,
            (
                (index * 1_000_000, 100_000_000),
                (index * 1_000_000 + 500_000, 100_000_000),
            ),
        )
        for index in range(context)
    )
    spec = Specification(
        "rf", "RF", "50", "single_ended", "RF", (LayerSettings("F.Cu", ("B.Cu",)),)
    )
    board = BoardSnapshot(
        ("F.Cu", "B.Cu"),
        routes + others,
        net_classes=("RF", "Other"),
        net_class_memberships=tuple((trace.net, ("RF",)) for trace in routes)
        + (("OTHER", ("Other",)),),
        net_class_context_digest="classes",
        board_bounds=(0, 0, max(signals, context) * 5_000_000, 150_000_000),
    )
    return Config(True, (spec,)), board


@pytest.mark.parametrize("preview", (False, True))
def test_operation_measures_each_context_trace_once(
    monkeypatch: pytest.MonkeyPatch, preview: bool
) -> None:
    """Adding signals must not rebuild whole-layer bounds and lengths per row."""
    config, board = _scenario()
    calls = {"bounds": 0, "length": 0}

    def count(name: str, original: Callable) -> Callable:
        """Retain exact geometry while exposing redundant primitive evaluation."""

        def measured(trace: Trace) -> object:
            calls[name] += 1
            return original(trace)

        return measured

    monkeypatch.setattr(regions, "trace_bounds", count("bounds", regions.trace_bounds))
    monkeypatch.setattr(regions, "_length", count("length", regions._length))
    if preview:
        rows = matching.preview_sections(config.specifications[0], board, "F.Cu")
    else:
        rows = matching.analyze(config, board).sections
    assert len(rows) == 12
    assert calls == {"bounds": len(board.traces), "length": len(board.traces)}


@pytest.mark.parametrize("kind", ("single_ended", "differential"))
def test_branch_warning_crosses_width_partitions(kind: str) -> None:
    """A third narrow arm is a branch even when its own row has only one member."""
    config, board = _scenario(1, 0)
    traces = (
        Trace("a", "F.Cu", "P", 200_000, ((-1_000_000, 0), (0, 0))),
        Trace("b", "F.Cu", "P", 200_000, ((0, 0), (1_000_000, 0))),
        Trace("c", "F.Cu", "P", 100_000, ((0, 0), (0, 1_000_000))),
    )
    if kind == "differential":
        traces += tuple(
            replace(
                trace,
                trace_id=trace.trace_id + "-mate",
                net="N",
                points=tuple((x, y + 2_000_000) for x, y in trace.points),
            )
            for trace in traces
        )
    board = replace(
        board,
        traces=traces,
        net_class_memberships=(("P", ("RF",)), ("N", ("RF",))),
        differential_pairs=(("P", "N"),),
    )
    config = replace(
        config,
        specifications=(
            replace(
                config.specifications[0],
                kind=kind,
                layer_settings=(
                    LayerSettings(
                        "F.Cu", ("B.Cu",), 200_000 if kind == "differential" else None
                    ),
                ),
            ),
        ),
    )
    result = matching.analyze(config, board)
    branch_warnings = [
        message for message in result.warnings if "branched route" in message
    ]
    assert len(branch_warnings) == (2 if kind == "differential" else 1)
    assert {trace.trace_id for row in result.sections for trace in row.traces} == {
        trace.trace_id for trace in traces
    }


def test_shared_geometry_separates_nearest_gap_from_represented_density() -> None:
    """A represented differential mate still contributes to the physical gap."""
    _, board = _scenario(1, 0)
    positive = Trace("p", "F.Cu", "P", 200_000, ((0, 0), (1_000_000, 0)))
    negative = replace(
        positive, trace_id="n", net="N", points=((0, 250_000), (1_000_000, 250_000))
    )
    board = replace(board, traces=(positive, negative), board_bounds=None)
    geometry = regions.RegionGeometry(board)
    single = regions.region_quality((positive,), board, geometry)
    pair = regions.region_quality((positive, negative), board, geometry)
    assert pair.foreign_ratio == 0
    assert pair.gap_pixels < pair.trace_pixels
    assert single == regions.region_quality((positive,), board)
    assert pair == regions.region_quality((positive, negative), board)
    assert single == regions.region_quality((positive,), board, geometry)


@pytest.mark.parametrize("reverse", (False, True))
def test_nearest_gap_does_not_measure_remote_candidates(
    monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    """A tight geometric bound must avoid exact work on distant parallel copper."""
    _, board = _scenario(1, 0)
    signal = Trace("signal", "F.Cu", "RF", 200_000, ((0, 0), (1_000_000, 0)))
    neighbors = tuple(
        replace(
            signal,
            trace_id=f"neighbor-{index}",
            net="OTHER",
            points=((0, index * 1_000_000), (1_000_000, index * 1_000_000)),
        )
        for index in range(64, 0, -1)
    )
    if reverse:
        neighbors = tuple(reversed(neighbors))
    board = replace(board, traces=(signal,) + neighbors)
    calls = 0
    segment_distance = regions._segment_distance

    def measured(
        a: tuple[int, int], b: tuple[int, int], c: tuple[int, int], d: tuple[int, int]
    ) -> float:
        """Count expensive exact geometry without replacing its result."""
        nonlocal calls
        calls += 1
        return segment_distance(a, b, c, d)

    monkeypatch.setattr(regions, "_segment_distance", measured)
    context = regions.RegionGeometry(board).context({signal.net}, signal.layer)
    assert context.gap(signal) == 800_000
    assert calls <= 2
    assert context.gap(signal) == 800_000
    assert calls <= 2


@pytest.mark.parametrize(
    ("other", "expected"),
    (
        ((14, 13, 20, 20), 5),
        ((-20, -20, -4, -3), 5),
        ((14, 0, 20, 10), 4),
        ((0, 13, 10, 20), 3),
        ((10, 0, 20, 10), 0),
        ((2, 2, 8, 8), 0),
    ),
)
def test_rectangle_gap_is_symmetric_and_translation_independent(
    other: tuple[int, int, int, int], expected: float
) -> None:
    """The shared bound retains diagonal, touching, and overlapping interval geometry."""
    for offset in (0, 10**18):
        own = tuple(coordinate + offset for coordinate in (0, 0, 10, 10))
        translated = tuple(coordinate + offset for coordinate in other)
        assert regions._distance(own, translated) == expected
        assert regions._distance(translated, own) == expected


@pytest.mark.parametrize(
    ("neighbors", "expected"),
    (
        (
            (
                Trace("above", "F.Cu", "OTHER", 200, ((0, 600), (1000, 600))),
                Trace("below", "F.Cu", "OTHER", 200, ((0, -600), (1000, -600))),
            ),
            400,
        ),
        (
            (
                Trace("narrow", "F.Cu", "OTHER", 100, ((0, 500), (1000, 500))),
                Trace("wide", "F.Cu", "OTHER", 1500, ((0, 1000), (1000, 1000))),
            ),
            150,
        ),
        ((Trace("touch", "F.Cu", "OTHER", 200, ((0, 200), (1000, 200))),), 0),
        ((Trace("point", "F.Cu", "OTHER", 200, ((1400, 0), (1400, 0))),), 200),
        ((Trace("diagonal", "F.Cu", "OTHER", 200, ((-400, -300), (-400, -300))),), 300),
        (
            (
                Trace(
                    "arc",
                    "F.Cu",
                    "OTHER",
                    200,
                    ((0, 600), (500, 300), (1000, 600)),
                    is_native_arc=True,
                ),
            ),
            100,
        ),
    ),
    ids=(
        "equal-bounds",
        "different-widths",
        "touching",
        "zero-length",
        "diagonal",
        "sampled-arc",
    ),
)
def test_nearest_gap_preserves_physical_copper_geometry(
    neighbors: tuple[Trace, ...], expected: float
) -> None:
    """Ordering must retain exact sampled gaps, copper radii, and source independence."""
    _, board = _scenario(1, 0)
    signal = Trace("signal", "F.Cu", "RF", 200, ((0, 0), (1000, 0)))
    ignored = (
        replace(signal, trace_id="same-net"),
        replace(signal, trace_id="other-layer", layer="B.Cu", net="OTHER"),
    )
    for ordered in (neighbors, tuple(reversed(neighbors))):
        geometry = regions.RegionGeometry(
            replace(board, traces=(signal,) + ignored + ordered)
        )
        assert geometry.context({signal.net}, signal.layer).gap(signal) == expected


def test_ordered_gap_search_preserves_complete_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning preserves all rows, warnings, and digests against an exhaustive search."""
    config, board = _scenario(12, 96)
    expected = matching.analyze(config, board)

    def exhaustive_gap(context: regions._RegionContext, trace: Trace) -> float:
        """Measure every foreign sampled segment without any bounding-box pruning."""
        return min(
            (
                max(
                    0.0,
                    regions._segment_distance(a, b, c, d)
                    - (trace.width_nm + other.width_nm) / 2,
                )
                for other in context.context
                if other.net != trace.net
                for a, b in context.segments[trace.trace_id]
                for c, d in context.segments[other.trace_id]
            ),
            default=float("inf"),
        )

    monkeypatch.setattr(regions._RegionContext, "gap", exhaustive_gap)
    assert matching.analyze(config, board) == expected
    assert (
        matching.analyze(config, replace(board, traces=tuple(reversed(board.traces))))
        == expected
    )


def test_context_track_crossing_crop_with_external_endpoints_is_measured() -> None:
    """Local density includes long crossing context, independent of its endpoints."""
    _, board = _scenario(1, 0)
    signal = Trace("signal", "F.Cu", "P", 200_000, ((0, 0), (1_000_000, 0)))
    crossing = Trace(
        "crossing",
        "F.Cu",
        "OTHER",
        100_000,
        ((500_000, -100_000_000), (500_000, 100_000_000)),
    )
    board = replace(board, traces=(signal, crossing), board_bounds=None)
    quality = regions.region_quality((signal,), board, regions.RegionGeometry(board))
    assert quality.foreign_ratio == pytest.approx(0.2)
    assert quality.gap_pixels == 0


@pytest.mark.parametrize(
    "fault", ("transition", "separate_net", "separate_layer", "stub_arm", "zero_arm")
)
def test_branch_warning_preserves_nonbranch_and_stub_rules(fault: str) -> None:
    """Only three real, non-stub incident track ends on one net/layer branch."""
    config, board = _scenario(1, 0)
    traces = (
        Trace("a", "F.Cu", "P", 200_000, ((-1_000_000, 0), (0, 0))),
        Trace("b", "F.Cu", "P", 200_000, ((0, 0), (1_000_000, 0))),
        Trace("c", "F.Cu", "P", 100_000, ((0, 0), (0, 200_000))),
    )
    if fault == "transition":
        traces = (traces[0], replace(traces[1], width_nm=100_000))
    elif fault == "separate_net":
        traces = traces[:2] + (replace(traces[2], net="OTHER"),)
    elif fault == "separate_layer":
        traces = traces[:2] + (replace(traces[2], layer="B.Cu"),)
        config = replace(
            config,
            specifications=(
                replace(config.specifications[0], excluded_layers=("B.Cu",)),
            ),
        )
    elif fault == "zero_arm":
        traces = traces[:2] + (replace(traces[2], points=((0, 0), (0, 0))),)
    features = (
        (
            CopperFeature(
                "pad", "P", "F.Cu", "pad", "rect", (0, 100_000), 300_000, 400_000
            ),
        )
        if fault == "stub_arm"
        else ()
    )
    board = replace(
        board,
        traces=traces,
        copper_features=features,
        net_class_memberships=(("P", ("RF",)), ("OTHER", ("Other",))),
    )
    result = matching.analyze(config, board)
    assert not any("branched route" in message for message in result.warnings)


def test_disconnected_branches_warn_once_with_stable_rows() -> None:
    """Multiple branch points share one net warning without changing row identities."""
    config, board = _scenario(1, 0)
    first = (
        Trace("a", "F.Cu", "P", 200_000, ((-1_000_000, 0), (0, 0))),
        Trace("b", "F.Cu", "P", 200_000, ((0, 0), (1_000_000, 0))),
        Trace("c", "F.Cu", "P", 100_000, ((0, 0), (0, 1_000_000))),
    )
    second = tuple(
        replace(
            trace,
            trace_id=trace.trace_id + "-island",
            points=tuple((x + 20_000_000, y + 20_000_000) for x, y in trace.points),
        )
        for trace in first
    )
    board = replace(
        board, traces=first + second, net_class_memberships=(("P", ("RF",)),)
    )
    result = matching.analyze(config, board)
    assert sum("branched route" in message for message in result.warnings) == 1
    reordered = matching.analyze(
        config,
        replace(
            board,
            traces=tuple(
                replace(trace, points=tuple(reversed(trace.points)))
                for trace in reversed(board.traces)
            ),
        ),
    )
    assert tuple(row.section_id for row in result.sections) == tuple(
        row.section_id for row in reordered.sections
    )
    assert result.digest == reordered.digest
    assert result.warnings == reordered.warnings
    assert sorted(
        trace.trace_id for row in result.sections for trace in row.traces
    ) == sorted(trace.trace_id for trace in board.traces)


def test_calculation_policy_changes_review_digest_without_changing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retired solver results cannot retain workbook consent through Generate."""
    config, board = _scenario(2, 0)
    previous = matching.analyze(config, board)
    monkeypatch.setattr(
        matching, "CALCULATION_REVISION", matching.CALCULATION_REVISION + 1
    )
    current = matching.analyze(config, board)
    assert current.sections == previous.sections
    assert current.digest != previous.digest
