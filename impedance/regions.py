"""Grow readable signal regions without discarding native track identities.

These conservative capture heuristics operate on board coordinates. Native
tracks remain indivisible: a single track that cannot meet the readability
target is retained and reported rather than silently omitted or fabricated.
"""

from dataclasses import dataclass
import math
from typing import Optional

from .capture_geometry import CAPTURE_WIDTH_PX, capture_viewport
from .model import BoardSnapshot, Bounds, Point, Trace

MIN_TRACE_PIXELS = 2.0
MIN_GAP_PIXELS = 1.0
MAX_FOREIGN_LENGTH_RATIO = 5.0
MAX_CONTEXT_FEATURES = 24
# Join small pad-separated islands, but reject remote islands whose intervening
# empty space dominates the proposed region. Readability is checked separately.
LOCAL_GAP_NM = 2_000_000
MAX_GAP_FRACTION = 0.25


def trace_bounds(trace: Trace) -> Bounds:
    """Enclose the complete sampled centerline and outward-rounded copper radius."""
    radius = (trace.width_nm + 1) // 2
    return (
        min(x for x, _ in trace.points) - radius,
        min(y for _, y in trace.points) - radius,
        max(x for x, _ in trace.points) + radius,
        max(y for _, y in trace.points) + radius,
    )


def union_bounds(bounds: tuple[Bounds, ...]) -> Bounds:
    """Return the smallest enclosing board-space rectangle."""
    return (
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    )


def _distance(left: Bounds, right: Bounds) -> float:
    """Measure empty space between two copper bounding rectangles."""
    if left[0] > right[2]:
        dx = left[0] - right[2]
    elif right[0] > left[2]:
        dx = right[0] - left[2]
    else:
        dx = 0
    if left[1] > right[3]:
        dy = left[1] - right[3]
    elif right[1] > left[3]:
        dy = right[1] - left[3]
    else:
        dy = 0
    return math.hypot(dx, dy)


def _length(trace: Trace) -> float:
    """Measure native sampled centerline length independently of item subdivision."""
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1])
        for a, b in zip(trace.points, trace.points[1:])
    )


def _clipped_length(start: Point, end: Point, bounds: Bounds) -> float:
    """Measure only context actually inside the proposed region, using Liang–Barsky."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    low, high = 0.0, 1.0
    for p, q in (
        (-dx, start[0] - bounds[0]),
        (dx, bounds[2] - start[0]),
        (-dy, start[1] - bounds[1]),
        (dy, bounds[3] - start[1]),
    ):
        if p == 0:
            if q < 0:
                return 0.0
        elif p < 0:
            low = max(low, q / p)
        else:
            high = min(high, q / p)
        if low > high:
            return 0.0
    return math.hypot(dx, dy) * (high - low)


def _point_distance(point: Point, start: Point, end: Point) -> float:
    """Measure distance to a closed centerline segment, including zero-length tracks."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    x, y = point[0] - start[0], point[1] - start[1]
    projection = x * dx + y * dy
    if projection <= 0:
        return math.hypot(x, y)
    denominator = dx * dx + dy * dy
    if projection >= denominator:
        return math.hypot(point[0] - end[0], point[1] - end[1])
    fraction = projection / denominator
    return math.hypot(x - fraction * dx, y - fraction * dy)


def _segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    """Measure the separation of two closed line segments without geometric snapping."""

    def side(point: Point, start: Point, end: Point) -> int:
        return (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (
            end[0] - start[0]
        )

    if (
        side(a, c, d) * side(b, c, d) <= 0
        and side(c, a, b) * side(d, a, b) <= 0
        and max(min(a[0], b[0]), min(c[0], d[0]))
        <= min(max(a[0], b[0]), max(c[0], d[0]))
        and max(min(a[1], b[1]), min(c[1], d[1]))
        <= min(max(a[1], b[1]), max(c[1], d[1]))
    ):
        return 0.0
    return min(
        _point_distance(a, c, d),
        _point_distance(b, c, d),
        _point_distance(c, a, b),
        _point_distance(d, a, b),
    )


@dataclass(frozen=True)
class RegionQuality:
    """Readability and unrelated route density at the actual exported image scale."""

    trace_pixels: float
    gap_pixels: float
    foreign_ratio: float

    @property
    def readable(self) -> bool:
        """Require distinguishable strokes/gaps without overwhelming unrelated routing."""
        return (
            self.trace_pixels >= MIN_TRACE_PIXELS
            and self.gap_pixels >= MIN_GAP_PIXELS
            and self.foreign_ratio <= MAX_FOREIGN_LENGTH_RATIO
        )


class RegionGeometry:
    """Share geometry only for the lifetime of one analysis or preview operation."""

    def __init__(self, snapshot: BoardSnapshot) -> None:
        self.snapshot = snapshot
        self.contexts: dict[tuple[str, frozenset[str]], _RegionContext] = {}
        layers: dict[str, list[Trace]] = {}
        for trace in snapshot.traces:
            layers.setdefault(trace.layer, []).append(trace)
        self.layers = {layer: tuple(traces) for layer, traces in layers.items()}
        self.boxes = {trace.trace_id: trace_bounds(trace) for trace in snapshot.traces}
        self.lengths = {trace.trace_id: _length(trace) for trace in snapshot.traces}
        self.segments = {
            trace.trace_id: tuple(zip(trace.points, trace.points[1:]))
            for trace in snapshot.traces
        }
        self.gaps: dict[str, float] = {}

    def context(self, nets: set[str], layer: str) -> "_RegionContext":
        """Reuse density exclusions and completed measurements for the same nets."""
        key = layer, frozenset(nets)
        if key not in self.contexts:
            self.contexts[key] = _RegionContext(self, nets, layer)
        return self.contexts[key]


class _RegionContext:
    """Evaluate net-specific proposals using operation-scoped layer geometry."""

    def __init__(self, geometry: RegionGeometry, nets: set[str], layer: str) -> None:
        self.snapshot = geometry.snapshot
        self.context = geometry.layers.get(layer, ())
        self.foreign = tuple(trace for trace in self.context if trace.net not in nets)
        self.features = tuple(
            feature
            for feature in geometry.snapshot.copper_features
            if feature.layer == layer and feature.net not in nets
        )
        self.boxes = geometry.boxes
        self.lengths = geometry.lengths
        self.segments = geometry.segments
        self.gaps = geometry.gaps
        self.measurements: dict[tuple[str, ...], RegionQuality] = {}

    def gap(self, trace: Trace) -> float:
        """Cache the closest physical gap to a different net, including represented nets."""
        if trace.trace_id not in self.gaps:
            own_bounds = self.boxes.get(trace.trace_id)
            if own_bounds is None:
                own_bounds = trace_bounds(trace)
            self.gaps[trace.trace_id] = self._minimum_gap((trace,), own_bounds)
        return self.gaps[trace.trace_id]

    def _minimum_gap(self, traces: tuple[Trace, ...], bounds: Bounds) -> float:
        """Measure only the group's minimum, retaining known per-track clearances."""
        best = min(
            (self.gaps.get(trace.trace_id, math.inf) for trace in traces),
            default=math.inf,
        )
        pending = tuple(trace for trace in traces if trace.trace_id not in self.gaps)
        if not pending:
            return best
        nets = {trace.net for trace in pending}
        candidates = sorted(
            (
                (_distance(bounds, self.boxes[other.trace_id]), other)
                for other in self.context
                if len(nets) > 1 or other.net not in nets
            ),
            key=lambda candidate: candidate[0],
        )
        for lower_bound, other in candidates:
            if lower_bound >= best:
                break
            for trace in pending:
                if (
                    trace.net == other.net
                    or _distance(self.boxes[trace.trace_id], self.boxes[other.trace_id])
                    >= best
                ):
                    continue
                for a, b in self.segments[trace.trace_id]:
                    for c, d in self.segments[other.trace_id]:
                        best = min(
                            best,
                            max(
                                0.0,
                                _segment_distance(a, b, c, d)
                                - (trace.width_nm + other.width_nm) / 2,
                            ),
                        )
            if best == 0:
                break
        return best

    def quality(
        self, bounds: Bounds, width: int, length: float, gap: float
    ) -> RegionQuality:
        """Evaluate an incremental proposal without scanning its member geometry."""
        scale = (
            capture_viewport(bounds, board_bounds=self.snapshot.board_bounds)[2]
            / CAPTURE_WIDTH_PX
        )
        foreign_length = sum(
            _clipped_length(a, b, bounds)
            for other in self.foreign
            if _distance(bounds, self.boxes[other.trace_id]) == 0
            for a, b in self.segments[other.trace_id]
        )
        features = sum(
            bounds[0] <= feature.center[0] <= bounds[2]
            and bounds[1] <= feature.center[1] <= bounds[3]
            for feature in self.features
        )
        return RegionQuality(
            width / scale,
            gap / scale,
            max(
                foreign_length / max(length, width),
                features * MAX_FOREIGN_LENGTH_RATIO / MAX_CONTEXT_FEATURES,
            ),
        )

    def measure(self, traces: tuple[Trace, ...]) -> RegionQuality:
        """Evaluate a completed region using cached per-track quantities."""
        key = tuple(sorted(trace.trace_id for trace in traces))
        if key in self.measurements:
            return self.measurements[key]
        bounds = union_bounds(tuple(self.boxes[trace.trace_id] for trace in traces))
        result = self.quality(
            bounds,
            min(trace.width_nm for trace in traces),
            sum(self.lengths[identity] for identity in key),
            self._minimum_gap(traces, bounds),
        )
        self.measurements[key] = result
        return result


def region_quality(
    traces: tuple[Trace, ...],
    snapshot: BoardSnapshot,
    geometry: Optional[RegionGeometry] = None,
) -> RegionQuality:
    """Evaluate represented strokes and nearby unrelated copper at export scale."""
    return (
        (geometry or RegionGeometry(snapshot))
        .context({trace.net for trace in traces}, traces[0].layer)
        .measure(traces)
    )


def _near(left: Bounds, right: Bounds) -> bool:
    """Allow modest local islands without chaining across mostly empty board space."""
    span = max(
        max(left[2], right[2]) - min(left[0], right[0]),
        max(left[3], right[3]) - min(left[1], right[1]),
    )
    return _distance(left, right) <= max(LOCAL_GAP_NM, span * MAX_GAP_FRACTION)


def _key(trace: Trace) -> tuple:
    """Canonicalize direction before making deterministic geometric growth choices."""
    return min(trace.points, tuple(reversed(trace.points))), trace.trace_id


def signal_regions(
    traces: tuple[Trace, ...],
    snapshot: BoardSnapshot,
    geometry: Optional[RegionGeometry] = None,
) -> tuple[tuple[Trace, ...], ...]:
    """Cover one net/layer/width using whole readable groups or local greedy growth."""
    ordered = tuple(sorted(traces, key=_key))
    context = (geometry or RegionGeometry(snapshot)).context(
        {ordered[0].net}, ordered[0].layer
    )
    boxes = context.boxes
    keys = {trace.trace_id: _key(trace) for trace in ordered}
    # Cluster local copper first: nearby pad-separated track pieces should not
    # become different signals, while remote islands retain separate captures.
    remaining = set(range(len(ordered)))
    clusters = []
    while remaining:
        first = min(remaining)
        remaining.remove(first)
        cluster = [first]
        pending = [first]
        while pending:
            current = pending.pop()
            neighbors = [
                index
                for index in sorted(remaining)
                if _near(
                    boxes[ordered[current].trace_id], boxes[ordered[index].trace_id]
                )
            ]
            remaining.difference_update(neighbors)
            pending.extend(neighbors)
            cluster.extend(neighbors)
        clusters.append(tuple(ordered[index] for index in sorted(cluster)))
    result = []
    for cluster in clusters:
        if len(cluster) == 1 or context.measure(cluster).readable:
            result.append(cluster)
            continue
        pending_traces = list(cluster)
        while pending_traces:
            members = [pending_traces.pop(0)]
            bounds = boxes[members[0].trace_id]
            length = context.lengths[members[0].trace_id]
            gap = context.gap(members[0])
            seed_quality = context.quality(bounds, members[0].width_nm, length, gap)
            if (
                seed_quality.trace_pixels < MIN_TRACE_PIXELS
                or seed_quality.gap_pixels < MIN_GAP_PIXELS
            ):
                # A larger crop cannot improve an indivisible seed's projected
                # width or its existing separation from another signal.
                result.append(tuple(members))
                continue
            while pending_traces:
                candidates = []
                area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                for index, trace in enumerate(pending_traces):
                    if not _near(bounds, boxes[trace.trace_id]):
                        continue
                    expanded = union_bounds((bounds, boxes[trace.trace_id]))
                    quality = context.quality(
                        expanded,
                        trace.width_nm,
                        length + context.lengths[trace.trace_id],
                        min(gap, context.gap(trace)),
                    )
                    if not quality.readable:
                        continue
                    growth = (expanded[2] - expanded[0]) * (
                        expanded[3] - expanded[1]
                    ) - area
                    score = (
                        growth / max(context.lengths[trace.trace_id], trace.width_nm),
                        quality.foreign_ratio,
                        _distance(bounds, boxes[trace.trace_id]),
                        keys[trace.trace_id],
                    )
                    candidates.append((score, index, expanded))
                if not candidates:
                    break
                _score, index, bounds = min(candidates)
                chosen = pending_traces.pop(index)
                members.append(chosen)
                length += context.lengths[chosen.trace_id]
                gap = min(gap, context.gap(chosen))
            result.append(tuple(members))
    return tuple(result)
