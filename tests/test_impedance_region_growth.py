"""Require readable, complete signal regions before redundant boxes are pruned."""

from collections import Counter
from dataclasses import replace
from time import perf_counter

import pytest

from impedance import matching
from impedance.model import (
    BoardSnapshot,
    Config,
    LayerSettings,
    Section,
    Specification,
    Trace,
    ValidationError,
)
from impedance.regions import RegionGeometry


def _trace(
    identity: str,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    net: str = "SIGNAL",
    width: int = 200_000,
) -> Trace:
    """Create an actual indivisible board track with stable source identity."""
    return Trace(identity, "F.Cu", net, width, (start, end))


def _scenario(*traces: Trace) -> tuple[Config, BoardSnapshot]:
    """Keep unrelated context outside the selected native impedance class."""
    return (
        Config(
            True,
            (
                Specification(
                    "signal",
                    "Signal",
                    "50",
                    "single_ended",
                    "Controlled",
                    (LayerSettings("F.Cu", ("B.Cu",)),),
                ),
            ),
        ),
        BoardSnapshot(
            layers=("F.Cu", "B.Cu"),
            traces=traces,
            net_classes=("Controlled", "Other"),
            net_class_memberships=tuple(
                (net, ("Controlled",) if net == "SIGNAL" else ("Other",))
                for net in sorted({trace.net for trace in traces})
            ),
            net_class_context_digest="growth-classes",
        ),
    )


def _assert_coverage(rows: tuple[Section, ...], expected: tuple[Trace, ...]) -> None:
    """Every native source track is represented exactly once with its full copper."""
    assert Counter(trace.trace_id for row in rows for trace in row.traces) == Counter(
        trace.trace_id for trace in expected
    )
    for row in rows:
        for trace in row.traces:
            radius = (trace.width_nm + 1) // 2
            for x, y in trace.points:
                assert row.bounds[0] <= x - radius <= x + radius <= row.bounds[2]
                assert row.bounds[1] <= y - radius <= y + radius <= row.bounds[3]


def test_growth_stops_before_multiple_tracks_become_unreadable() -> None:
    """A route made of individually legible tracks receives several complete boxes."""
    traces = tuple(
        _trace(
            str(index),
            (index * 10_000_000, 0),
            ((index + 1) * 10_000_000, 0),
            width=40_000,
        )
        for index in range(4)
    )
    config, board = _scenario(*traces)
    result = matching.analyze(config, board)
    assert len(result.sections) == 4
    _assert_coverage(result.sections, traces)
    assert (
        matching.preview_sections(config.specifications[0], board, "F.Cu")
        == result.sections
    )


def test_clutter_can_stop_expansion_without_changing_signal_coverage() -> None:
    """A clear bend stays together; a bend enclosing dense unrelated routing splits."""
    signal = (
        _trace("horizontal", (0, 0), (10_000_000, 0)),
        _trace("vertical", (10_000_000, 0), (10_000_000, 10_000_000)),
    )
    config, clear = _scenario(*signal)
    assert len(matching.analyze(config, clear).sections) == 1
    foreign = tuple(
        _trace(
            f"context-{index}",
            (1_000_000, (index + 1) * 500_000),
            (9_000_000, (index + 1) * 500_000),
            net=f"OTHER-{index}",
            width=50_000,
        )
        for index in range(18)
    )
    _, crowded = _scenario(*signal, *foreign)
    result = matching.analyze(config, crowded)
    assert len(result.sections) == 2
    _assert_coverage(result.sections, signal)


def test_indivisible_thin_track_is_preserved_with_an_explicit_readability_warning() -> (
    None
):
    """A native track cannot disappear merely because its full extent is too thin."""
    trace = _trace("long", (0, 0), (200_000_000, 0), width=40_000)
    config, board = _scenario(trace)
    result = matching.analyze(config, board)
    assert len(result.sections) == 1
    _assert_coverage(result.sections, (trace,))
    assert any("readab" in warning.lower() for warning in result.warnings)


def test_growth_is_stable_under_order_and_centerline_direction() -> None:
    """Geometric choices and persisted row IDs do not depend on KiCad iteration order."""
    traces = tuple(
        _trace(
            str(index),
            (index * 10_000_000, 0),
            ((index + 1) * 10_000_000, 0),
            width=40_000,
        )
        for index in range(4)
    )
    config, board = _scenario(*traces)
    forward = matching.analyze(config, board)
    reversed_board = replace(
        board,
        traces=tuple(
            replace(trace, points=tuple(reversed(trace.points)))
            for trace in reversed(traces)
        ),
    )
    backward = matching.analyze(config, reversed_board)
    assert forward.digest == backward.digest
    assert [(row.section_id, row.bounds) for row in forward.sections] == [
        (row.section_id, row.bounds) for row in backward.sections
    ]
    _assert_coverage(backward.sections, traces)


def test_capture_projection_is_shared_with_the_renderer() -> None:
    """Readability uses the exact export crop, including the board context floor."""
    from impedance.capture_geometry import capture_viewport
    from impedance.render import section_viewport

    trace = _trace("route", (1_000_000, 2_000_000), (12_000_000, 5_000_000))
    config, board = _scenario(trace)
    row = matching.analyze(config, board).sections[0]
    outline = (-10_000_000, -10_000_000, 250_000_000, 150_000_000)
    for width, height in ((800, 420), (420, 180), (1600, 840)):
        rendered = section_viewport(row, width, height, outline)
        assert capture_viewport(row.bounds, width, height, outline) == pytest.approx(
            (rendered.left, rendered.top, rendered.width, rendered.height)
        )


def test_pruning_preserves_a_readable_closeup_inside_a_coarser_region() -> None:
    """Containment cannot make two previously distinguishable signals merge visually."""
    from impedance.regions import region_quality

    traces = (
        _trace("outer", (0, 0), (20_000_000, 20_000_000)),
        _trace(
            "inner-a", (10_000_000, 2_000_000), (11_000_000, 2_000_000), net="INNER-A"
        ),
        _trace(
            "inner-b", (10_000_000, 2_250_000), (11_000_000, 2_250_000), net="INNER-B"
        ),
    )
    config, board = _scenario(*traces)
    board = replace(
        board,
        net_class_memberships=tuple((trace.net, ("Controlled",)) for trace in traces),
    )
    result = matching.analyze(config, board)
    _assert_coverage(result.sections, traces)
    assert all(region_quality(row.traces, board).readable for row in result.sections)
    assert len(result.sections) > 1


def test_hundreds_of_connected_tracks_remain_responsive() -> None:
    """A routine segmented route must not block synchronous workbook refresh."""
    count = 480
    traces = tuple(
        _trace(
            str(index),
            (index * 80_000_000 // count, 0),
            ((index + 1) * 80_000_000 // count, 0),
        )
        for index in range(count)
    )
    config, board = _scenario(*traces)
    started = perf_counter()
    result = matching.analyze(config, board)
    elapsed = perf_counter() - started
    _assert_coverage(result.sections, traces)
    assert elapsed < 5.0, f"480-track refresh took {elapsed:.2f}s"


@pytest.mark.parametrize("fault", ("missing", "duplicate", "cropped"))
def test_invalid_layout_fails_closed_in_analysis_and_preview(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Corrupt layout output cannot silently remove, repeat, or crop native copper."""
    traces = (
        _trace("first", (0, 0), (1_000_000, 0)),
        _trace("second", (1_000_000, 0), (2_000_000, 0)),
    )
    config, board = _scenario(*traces)
    if fault == "cropped":
        original = matching._outermost_sections

        def crop(
            rows: tuple[Section, ...], snapshot: BoardSnapshot, geometry: RegionGeometry
        ) -> tuple[Section, ...]:
            regions = original(rows, snapshot, geometry)
            return (replace(regions[0], bounds=(0, 0, 1, 1)), *regions[1:])

        monkeypatch.setattr(matching, "_outermost_sections", crop)
    else:

        def corrupt_members(
            values: tuple[Trace, ...], snapshot: BoardSnapshot, geometry: RegionGeometry
        ) -> tuple[tuple[Trace, ...], ...]:
            del snapshot, geometry
            if fault == "missing":
                return ((values[0],),)
            return (values + (values[0],),)

        monkeypatch.setattr(matching, "signal_regions", corrupt_members)
    with pytest.raises(ValidationError, match="coverage"):
        matching.analyze(config, board)
    with pytest.raises(ValidationError, match="coverage"):
        matching.preview_sections(config.specifications[0], board, "F.Cu")


def test_board_outline_change_invalidates_analysis_without_losing_members() -> None:
    """Capture scale changes must invalidate approvals even when membership stays fixed."""
    trace = _trace("route", (0, 0), (1_000_000, 0))
    config, board = _scenario(trace)
    first = matching.analyze(
        config, replace(board, board_bounds=(0, 0, 20_000_000, 20_000_000))
    )
    second = matching.analyze(
        config, replace(board, board_bounds=(0, 0, 30_000_000, 30_000_000))
    )
    assert first.sections == second.sections
    assert first.digest != second.digest
    _assert_coverage(second.sections, (trace,))
