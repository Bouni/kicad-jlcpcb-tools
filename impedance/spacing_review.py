"""Independent, bounded concern reviews with complete overviews and native detail.

These records are deliberately separate from manufacturing sections and report
rows. Their native captures can require acknowledgement without ever entering
the fabrication spreadsheet or changing its image identities.
"""

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from functools import cached_property
import hashlib
import json
import math
from typing import TYPE_CHECKING, Optional
from weakref import WeakKeyDictionary

from .model import BoardSnapshot, Bounds, Config, Section, Trace, ValidationError
from .review_tracking import source_fingerprint
from .spacing_checks import (
    NmBounds,
    SpacingContext,
    SpacingCrossSection,
    SpacingIssue,
    SpacingLocation,
    _format_nm,
    _Primitive,
    _primitives,
    _slice_primitive,
    spacing_input_digest,
)
from .spacing_composition import (
    DETAIL_RECT,
    WIDE_DETAIL_RECT,
    PixelRect,
    SpacingComposition,
    spacing_composition,
)
from .spacing_overlays import spacing_issue_label

if TYPE_CHECKING:
    from .service import CapturedImage

SPACING_IMAGE_WIDTH = 1600
SPACING_IMAGE_HEIGHT = 1000
# Revision 7 replaces the comparison bars with target-relative gap deviation.
# Revision 6 bound one continuous concern, its complete overview, and worst detail.
# Earlier PNG approvals remain history, not current evidence.
SPACING_RENDER_POLICY_REVISION = 7
MAX_SPACING_REVIEW_IMAGES = 256
MAX_SPACING_REVIEW_BYTES = 128 * 1024 * 1024
# Choose the smaller positive copper/gap dimension, not route length. The
# complete composition remains readable at its minimum supported fitted scale.
MIN_SPACING_FEATURE_PIXELS = 24
MIN_SPACING_DISPLAY_FEATURE_PIXELS = 8
_MAX_CACHED_VARIANTS = 8
_ANGLE_UNITS = 2**48
_ENTRY_HASH_OWNER = object()


@dataclass(frozen=True)
class SpacingReviewEntry:
    """One continuous concern, with its full extent and readable worst-gap detail."""

    spec_id: str
    layer: str
    issue_id: str
    view_id: str
    parent_section_ids: tuple[str, ...]
    pair: tuple[str, str]
    issue_code: str
    caption: str
    bounds: Bounds
    viewport_nm: tuple[float, float, float, float]
    input_digest: str
    source_traces: tuple[Trace, ...]
    mate_traces: tuple[Trace, ...]
    width_px: int = SPACING_IMAGE_WIDTH
    height_px: int = SPACING_IMAGE_HEIGHT
    members: tuple[SpacingLocation, ...] = ()
    target_spacing_nm: Optional[int] = None
    region_gap_nm: Optional[NmBounds] = None
    composition_layout: str = "standard"
    representative: Optional[SpacingCrossSection] = None

    @cached_property
    def _structural_hash(self) -> tuple[object, int]:
        """Bind the exact complete dataclass hash to this interpreter's owner token."""
        return (
            _ENTRY_HASH_OWNER,
            hash(
                tuple(
                    getattr(self, item.name)
                    for item in fields(SpacingReviewEntry)
                    if (item.compare if item.hash is None else item.hash)
                )
            ),
        )

    def __hash__(self) -> int:
        """Reuse the complete structural hash for frequent preview/cache lookups."""
        memo = self._structural_hash
        if memo[0] is not _ENTRY_HASH_OWNER:
            # Pickle/deepcopy reconstruct the token. Recompute in this process
            # without overriding the default dictionary/inherited-slot state.
            self.__dict__.pop("_structural_hash", None)
            memo = self._structural_hash
        return memo[1]

    @cached_property
    def composition(self) -> SpacingComposition:
        """Keep the exact immutable panel and witness contract with the entry."""
        return spacing_composition(self)

    @property
    def traces(self) -> tuple[Trace, ...]:
        """Expose selected native strokes without constructing a manufacturing row."""
        return tuple(
            {
                trace.trace_id: trace for trace in self.source_traces + self.mate_traces
            }.values()
        )


@dataclass(frozen=True)
class SpacingReviewImage:
    """Bind an independent diagnostic entry to its immutable native PNG."""

    entry: SpacingReviewEntry
    capture: "CapturedImage"


@dataclass
class _DerivationCache:
    """Bound immutable derived geometry to one live measurement context."""

    source_digest: str = ""
    primitives: dict[str, tuple[_Primitive, ...]] = field(default_factory=dict)
    entries: OrderedDict[tuple[object, ...], tuple[SpacingReviewEntry, ...]] = field(
        default_factory=OrderedDict
    )


_CONTEXT_CACHES: WeakKeyDictionary[SpacingContext, _DerivationCache] = (
    WeakKeyDictionary()
)


def _digest(value: object) -> str:
    """Hash canonical review policy and geometry without native dependencies."""
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def _members(issue: SpacingIssue) -> tuple[SpacingLocation, ...]:
    """Consume all preserved member intervals, including reciprocal witnesses."""
    members = getattr(issue, "members", ())
    return members or ((issue.location,) if issue.location is not None else ())


def _canonical_paths(
    span_groups: tuple[tuple[tuple[_Primitive, ...], tuple[_Primitive, ...]], ...],
) -> tuple[object, ...]:
    """Canonicalize native line/arc unions without sampled chord identities."""
    groups = {}
    isolated = set()
    arcs = {}
    for source, mates in span_groups:
        for primitive in source + mates:
            trace = primitive.trace
            if primitive.center is not None:
                center = trace.native_arc_center
                assert center is not None
                radius_squared = sum(
                    (value - origin) ** 2
                    for value, origin in zip(trace.points[0], center)
                )
                key = ("arc", trace.net, trace.width_nm, center, radius_squared)
                angle = primitive.angle + min(0.0, primitive.sweep)
                start = (
                    round((angle % math.tau) / math.tau * _ANGLE_UNITS) % _ANGLE_UNITS
                )
                sweep = round(abs(primitive.sweep) / math.tau * _ANGLE_UNITS)
                if start + sweep <= _ANGLE_UNITS:
                    arcs.setdefault(key, []).append((start, start + sweep))
                else:
                    arcs.setdefault(key, []).extend(
                        ((start, _ANGLE_UNITS), (0, start + sweep - _ANGLE_UNITS))
                    )
                continue
            first, second = sorted(
                tuple(round(value) for value in point)
                for point in (primitive.start, primitive.end)
            )
            dx, dy = second[0] - first[0], second[1] - first[1]
            divisor = math.gcd(dx, dy)
            if divisor == 0:
                isolated.add((("point", trace.net, trace.width_nm), first))
                continue
            dx, dy = dx // divisor, dy // divisor
            key = (
                "line",
                trace.net,
                trace.width_nm,
                dx,
                dy,
                dx * first[1] - dy * first[0],
            )
            start = dx * first[0] + dy * first[1]
            end = dx * second[0] + dy * second[1]
            groups.setdefault(key, []).append((start, end, first, second))
    result = []
    for key, spans in groups.items():
        merged = []
        for start, end, first, second in sorted(spans):
            if merged and start <= merged[-1][1]:
                previous = merged[-1]
                if end > previous[1]:
                    merged[-1] = (previous[0], end, previous[2], second)
            else:
                merged.append((start, end, first, second))
        result.extend((key, span[2], span[3]) for span in merged)
    for key, spans in arcs.items():
        merged = []
        for start, end in sorted(set(spans)):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result.extend((key, start, end) for start, end in merged)
    result.extend(isolated)
    return tuple(sorted(result))


def _native_spans(
    trace: Trace, interval: tuple[float, float], cache: _DerivationCache
) -> tuple[_Primitive, ...]:
    """Recover exact affected native intervals rather than interpolated arc chords."""
    result = []
    if trace.trace_id not in cache.primitives:
        raw = _primitives(trace)
        total = sum(primitive.length for primitive in raw)
        cursor = 0.0
        normalized = []
        for primitive in raw:
            end = cursor + primitive.length / total
            normalized.append(replace(primitive, source_interval=(cursor, end)))
            cursor = end
        cache.primitives[trace.trace_id] = tuple(normalized)
    primitives = cache.primitives[trace.trace_id]
    lower, upper = interval
    for primitive in primitives:
        cursor, end = primitive.source_interval
        start = max(cursor, lower)
        stop = min(end, upper)
        if stop >= start:
            result.append(
                primitive
                if start == cursor and stop == end
                else _slice_primitive(
                    primitive,
                    (start - cursor) / (end - cursor),
                    (stop - cursor) / (end - cursor),
                )
            )
    return tuple(result)


def _span_groups(
    members: tuple[SpacingLocation, ...],
    traces: dict[str, Trace],
    cache: _DerivationCache,
) -> tuple[tuple[tuple[_Primitive, ...], tuple[_Primitive, ...]], ...]:
    """Keep source/mate relationships so one-sided views cannot approve a gap."""
    buckets = {}
    for member in members:
        if member.source_trace_id not in traces:
            continue
        intervals = tuple(
            sorted(
                set(
                    member.mate_intervals
                    or tuple((trace_id, 0.0, 1.0) for trace_id in member.mate_trace_ids)
                )
            )
        )
        key = (member.source_trace_id, tuple(item[0] for item in intervals))
        buckets.setdefault(key, []).append((member.source_interval, intervals))
    result = []
    for (source_id, _mate_ids), ranges in buckets.items():
        merged = []
        for (lower, upper), intervals in sorted(set(ranges)):
            if merged:
                previous_lower, previous_upper, previous_intervals = merged[-1]
                continuous = lower <= previous_upper + 1e-12 and all(
                    max(first[1], second[1]) <= min(first[2], second[2]) + 1e-12
                    for first, second in zip(previous_intervals, intervals)
                )
                if continuous:
                    merged[-1] = (
                        previous_lower,
                        max(previous_upper, upper),
                        tuple(
                            (
                                first[0],
                                min(first[1], second[1]),
                                max(first[2], second[2]),
                            )
                            for first, second in zip(previous_intervals, intervals)
                        ),
                    )
                    continue
            merged.append((lower, upper, intervals))
        for lower, upper, intervals in merged:
            source = _native_spans(traces[source_id], (lower, upper), cache)
            mates = tuple(
                span
                for trace_id, start, end in intervals
                if trace_id in traces
                for span in _native_spans(traces[trace_id], (start, end), cache)
            )
            result.append((source, mates))
    return tuple(result)


def spacing_review_spans(entry: SpacingReviewEntry) -> tuple[_Primitive, ...]:
    """Return only the exact native copper intervals represented by this concern."""
    groups = _span_groups(
        entry.members,
        {trace.trace_id: trace for trace in entry.traces},
        _DerivationCache(),
    )
    unique = {}
    for source, mates in groups:
        for primitive in source + mates:
            key = (
                primitive.trace.trace_id,
                tuple(sorted((primitive.start, primitive.end))),
                primitive.center,
                abs(primitive.sweep),
            )
            unique[key] = primitive
    return tuple(unique.values())


def _visible_intervals(
    primitive: _Primitive, viewport: tuple[float, float, float, float]
) -> tuple[tuple[float, float], ...]:
    """Return exact native centerline intervals with full copper width in frame."""
    left, top, width, height = viewport
    padding = primitive.trace.width_nm / 2
    right, bottom = left + width - padding, top + height - padding
    left, top = left + padding, top + padding
    if right < left or bottom < top:
        return ()
    if primitive.center is None:
        lower, upper = 0.0, 1.0
        for axis, low, high in ((0, left, right), (1, top, bottom)):
            start, delta = (
                primitive.start[axis],
                primitive.end[axis] - primitive.start[axis],
            )
            if delta == 0:
                if not low <= start <= high:
                    return ()
            else:
                first, second = sorted(((low - start) / delta, (high - start) / delta))
                lower, upper = max(lower, first), min(upper, second)
                if lower > upper:
                    return ()
        return ((lower, upper),)
    if primitive.sweep == 0:
        x, y = primitive.start
        return ((0.0, 1.0),) if left <= x <= right and top <= y <= bottom else ()
    center_x, center_y = (float(value) for value in primitive.center)
    square = primitive.radius**2
    parameters = {0.0, 1.0}
    for axis, edge in ((0, left), (0, right), (1, top), (1, bottom)):
        coordinate = edge - (center_x if axis == 0 else center_y)
        remaining = square - coordinate**2
        if remaining < 0:
            continue
        offset = math.sqrt(remaining)
        for signed in (offset, -offset):
            x, y = (edge, center_y + signed) if axis == 0 else (center_x + signed, edge)
            if (
                left <= x <= right
                and top <= y <= bottom
                and primitive.contains_angle(math.atan2(y - center_y, x - center_x))
            ):
                angle = math.atan2(y - center_y, x - center_x)
                offset = (
                    (angle - primitive.angle)
                    if primitive.sweep > 0
                    else (primitive.angle - angle)
                ) % math.tau
                parameters.add(max(0.0, min(1.0, offset / abs(primitive.sweep))))
    ordered = sorted(parameters)
    result = []
    for start, end in zip(ordered, ordered[1:]):
        x, y = primitive.point((start + end) / 2)
        if left <= x <= right and top <= y <= bottom:
            result.append((start, end))
    for parameter in ordered:
        x, y = primitive.point(parameter)
        if left <= x <= right and top <= y <= bottom:
            result.append((parameter, parameter))
    return tuple(result)


class _SpacingViewTooSmall(ValidationError):
    """A proven native pair exceeds the current physical panel's readable span."""


def _viewports(
    bounds: Bounds,
    detail_nm: float,
    groups: tuple[tuple[tuple[_Primitive, ...], tuple[_Primitive, ...]], ...],
    *,
    representative: SpacingCrossSection,
    detail_rect: PixelRect = DETAIL_RECT,
    target: Optional[int] = None,
) -> tuple[tuple[float, float, float, float], ...]:
    """Choose one readable, witnessed detail; the overview covers the full concern."""
    if not groups or any(not source or not mates for source, mates in groups):
        raise ValidationError(
            "The spacing concern cannot show both traces at a readable scale. "
            "Review its routing or split the physical concern."
        )
    max_width = detail_nm * detail_rect[2] / MIN_SPACING_FEATURE_PIXELS
    if not math.isfinite(max_width) or detail_nm <= 0:
        raise ValidationError(
            "The spacing review image has no readable native geometry."
        )
    padding = max(detail_nm * 4, 500_000)
    left, top, right, bottom = bounds
    ratio = detail_rect[2] / detail_rect[3]
    width = min(
        max_width, max(right - left + 2 * padding, (bottom - top + 2 * padding) * ratio)
    )
    height = width / ratio
    source, mate = representative.source_point, representative.mate_point
    distance = math.dist(source, mate)
    if not distance or not math.isfinite(distance):
        raise ValidationError(
            "The spacing review has no authoritative native correspondence."
        )
    boxes = []
    for point, copper_width in (
        (source, representative.source_width_nm),
        (mate, representative.mate_width_nm),
    ):
        radius = copper_width / 2
        boxes.append(
            (point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius)
        )
    if target is not None and target > 0:
        normal = tuple((b - a) / distance for a, b in zip(source, mate))
        for point, radius, sign in (
            (source, representative.source_width_nm / 2, 1),
            (mate, representative.mate_width_nm / 2, -1),
        ):
            x, y = tuple(
                value + sign * direction * (radius + target)
                for value, direction in zip(point, normal)
            )
            boxes.append((x, y, x, y))
    left, top = min(box[0] for box in boxes), min(box[1] for box in boxes)
    right, bottom = max(box[2] for box in boxes), max(box[3] for box in boxes)
    if right - left > width or bottom - top > height:
        raise _SpacingViewTooSmall(
            "The spacing concern cannot show both traces at a readable scale. "
            "Review its routing or split the physical concern."
        )

    def context_origin(
        minimum: float, maximum: float, span: float, center: float
    ) -> float:
        """Favor route context while retaining the measured pair and target guides."""
        margin = min(padding, max(0.0, (span - (maximum - minimum)) / 2))
        lower, upper = maximum + margin - span, minimum - margin
        origin = max(lower, min(upper, center - span / 2))
        native = float(round(origin))
        # Prefer a stable integer-nm capture origin. An exactly packed diagonal
        # pair may require a fractional origin to retain all measured copper.
        return native if lower <= native <= upper else origin

    return (
        (
            context_origin(left, right, width, (bounds[0] + bounds[2]) / 2),
            context_origin(top, bottom, height, (bounds[1] + bounds[3]) / 2),
            width,
            height,
        ),
    )


@dataclass(frozen=True)
class _Concern:
    """Keep complete physical witnesses and their certified native worst detail."""

    issues: tuple[SpacingIssue, ...]
    members: tuple[SpacingLocation, ...]
    bounds: Bounds
    groups: tuple[tuple[tuple[_Primitive, ...], tuple[_Primitive, ...]], ...]
    detail_nm: float
    gap_nm: NmBounds
    representative: SpacingCrossSection


def _concern_caption(concern: _Concern, target: Optional[int]) -> str:
    """Summarize dimensional ranges rather than listing every adaptive witness."""
    labels = []
    for code in sorted({issue.code for issue in concern.issues}):
        issues = tuple(
            replace(issue, gap_nm=concern.gap_nm)
            for issue in concern.issues
            if issue.code == code
        )
        gaps = tuple(issue.gap_nm for issue in issues if issue.gap_nm is not None)
        gap = (
            NmBounds(
                min(value.lower_nm for value in gaps),
                max(value.upper_nm for value in gaps),
            )
            if gaps
            else None
        )
        labels.append(spacing_issue_label(replace(issues[0], gap_nm=gap), None))
    caption = " · ".join(labels)
    if target is not None:
        caption += "; target " + _format_nm(target) + " mm"
    return caption


def _continuous_issues(
    issues: tuple[SpacingIssue, ...],
) -> tuple[tuple[SpacingIssue, ...], ...]:
    """Union physical components even when a late native segment bridges groups."""
    selected = tuple(
        issue
        for issue in issues
        if issue.code in ("too_tight", "too_wide") and _members(issue)
    )
    parents = list(range(len(selected)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(first: int, second: int) -> None:
        left, right = root(first), root(second)
        parents[max(left, right)] = min(left, right)

    ranges: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
    endpoints: dict[tuple[object, ...], int] = {}
    for index, issue in enumerate(selected):
        for member in _members(issue):
            ranges.setdefault((issue.code, member.source_trace_id), []).append(
                (*member.source_interval, index)
            )
            for section in member.cross_sections:
                points = sorted((section.source_point, section.mate_point))
                key = (issue.code, *points[0], *points[1])
                if key in endpoints:
                    join(index, endpoints[key])
                else:
                    endpoints[key] = index
    for intervals in ranges.values():
        previous = None
        for lower, upper, index in sorted(intervals):
            if previous is not None and lower <= previous[0] + 1e-12:
                join(index, previous[1])
                previous = (max(upper, previous[0]), index)
            else:
                previous = (upper, index)
    groups: dict[int, list[SpacingIssue]] = {}
    for index, issue in enumerate(selected):
        groups.setdefault(root(index), []).append(issue)
    return tuple(tuple(group) for group in groups.values())


def _concerns(
    issues: tuple[SpacingIssue, ...],
    traces: dict[str, Trace],
    target: Optional[int],
    cache: _DerivationCache,
    witnesses: tuple[SpacingIssue, ...],
) -> tuple[_Concern, ...]:
    """Keep continuous measured concerns distinct from how their detail is framed."""
    measured = {
        member: witness for witness in witnesses for member in _members(witness)
    }
    result = []
    for connected in _continuous_issues(issues):
        members = tuple(
            dict.fromkeys(member for issue in connected for member in _members(issue))
        )
        raw = tuple(
            dict.fromkeys(measured[member] for member in members if member in measured)
        )
        if len(raw) == 0 or any(member not in measured for member in members):
            raise ValidationError(
                "The spacing review has no authoritative native correspondence."
            )
        gaps = tuple(witness.gap_nm for witness in raw if witness.gap_nm is not None)
        if len(gaps) != len(raw):
            raise ValidationError(
                "The spacing review has no authoritative measured gap."
            )
        gap = NmBounds(
            min(value.lower_nm for value in gaps), max(value.upper_nm for value in gaps)
        )
        bounds = (
            min(member.bounds[0] for member in members),
            min(member.bounds[1] for member in members),
            max(member.bounds[2] for member in members),
            max(member.bounds[3] for member in members),
        )
        if any(
            identity not in traces
            for member in members
            for identity in (member.source_trace_id,) + member.mate_trace_ids
        ):
            raise ValidationError(
                "The spacing review has no authoritative native correspondence."
            )
        groups = _span_groups(members, traces, cache)
        sections: dict[SpacingCrossSection, NmBounds] = {}
        for member in members:
            for section in member.cross_sections:
                source = traces.get(section.source_trace_id)
                mate = traces.get(section.mate_trace_id)
                if (
                    section.source_trace_id != member.source_trace_id
                    or section.mate_trace_id not in member.mate_trace_ids
                    or source is None
                    or mate is None
                    or source.width_nm != section.source_width_nm
                    or mate.width_nm != section.mate_width_nm
                ):
                    raise ValidationError(
                        "The spacing review has no authoritative native correspondence."
                    )
                source_spans = _native_spans(source, member.source_interval, cache)
                mate_spans = tuple(
                    span
                    for identity, lower, upper in member.mate_intervals
                    if identity == mate.trace_id
                    for span in _native_spans(mate, (lower, upper), cache)
                )
                if (
                    not source_spans
                    or not mate_spans
                    or min(span.distance(section.source_point) for span in source_spans)
                    > 2
                    or min(span.distance(section.mate_point) for span in mate_spans) > 2
                ):
                    raise ValidationError(
                        "The spacing review has no authoritative native correspondence."
                    )
                if source.net > mate.net:
                    section = SpacingCrossSection(
                        section.mate_trace_id,
                        section.source_trace_id,
                        section.mate_point,
                        section.source_point,
                        section.mate_width_nm,
                        section.source_width_nm,
                    )
                certificate = measured[member].gap_nm
                assert certificate is not None
                previous = sections.get(section)
                if (
                    previous is None
                    or (
                        connected[0].code == "too_tight"
                        and certificate.upper_nm < previous.upper_nm
                    )
                    or (
                        connected[0].code == "too_wide"
                        and certificate.lower_nm > previous.lower_nm
                    )
                ):
                    sections[section] = certificate
        if not sections:
            raise ValidationError(
                "The spacing review has no authoritative native correspondence."
            )
        tight = connected[0].code == "too_tight"

        def rank(
            section: SpacingCrossSection,
            certificates: dict[SpacingCrossSection, NmBounds] = sections,
            is_tight: bool = tight,
        ) -> tuple[object, ...]:
            actual = (
                math.dist(section.source_point, section.mate_point)
                - (section.source_width_nm + section.mate_width_nm) / 2
            )
            # Native witnesses are rounded to integer nanometres; sub-nm noise
            # must not relocate an otherwise identical split/reversed route.
            certified = certificates[section]
            worst = certified.upper_nm if is_tight else -certified.lower_nm
            severity = round(actual) if is_tight else -round(actual)
            return (
                worst,
                severity,
                section.source_point,
                section.mate_point,
                section.source_width_nm,
                section.mate_width_nm,
            )

        representative = min(sections, key=rank)
        details = [
            traces[identity].width_nm
            for member in members
            for identity in (member.source_trace_id,) + member.mate_trace_ids
            if identity in traces
        ]
        if target is not None and target > 0:
            details.append(target)
        if gap.lower_nm > 0:
            details.append(gap.lower_nm)
        details = [value for value in details if value > 0]
        if not details:
            raise ValidationError(
                "The spacing review image has no readable native geometry."
            )
        result.append(
            _Concern(
                connected, members, bounds, groups, min(details), gap, representative
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda concern: (concern.bounds, _canonical_paths(concern.groups)),
        )
    )


def spacing_review_entries(
    config: Config,
    snapshot: BoardSnapshot,
    sections: Sequence[Section],
    *,
    context: Optional[SpacingContext] = None,
) -> tuple[SpacingReviewEntry, ...]:
    """Derive full-pair concerns once, independently of workbook row checkboxes."""
    if not config.enabled:
        return ()
    if context is None or context.snapshot is not snapshot:
        context = SpacingContext(snapshot)
    from .spacing_checks import (
        SPACING_CHECK_POLICY_REVISION,
        SPACING_COMPARISON_BAND_NM,
    )

    cache = _CONTEXT_CACHES.setdefault(context, _DerivationCache())
    key = (
        config.specifications,
        tuple(
            sorted(
                (
                    section.spec_id,
                    section.layer,
                    section.section_id,
                    section.exclusion_reason,
                    section.net_names,
                )
                for section in sections
            )
        ),
        SPACING_CHECK_POLICY_REVISION,
        SPACING_COMPARISON_BAND_NM,
        SPACING_RENDER_POLICY_REVISION,
        SPACING_IMAGE_WIDTH,
        SPACING_IMAGE_HEIGHT,
        MIN_SPACING_FEATURE_PIXELS,
        MIN_SPACING_DISPLAY_FEATURE_PIXELS,
        MAX_SPACING_REVIEW_IMAGES,
    )
    if key in cache.entries:
        cache.entries.move_to_end(key)
        return cache.entries[key]
    entries = _derive_spacing_review_entries(config, snapshot, sections, context, cache)
    cache.entries[key] = entries
    while len(cache.entries) > _MAX_CACHED_VARIANTS:
        cache.entries.popitem(last=False)
    return entries


def _derive_spacing_review_entries(
    config: Config,
    snapshot: BoardSnapshot,
    sections: Sequence[Section],
    context: SpacingContext,
    cache: _DerivationCache,
) -> tuple[SpacingReviewEntry, ...]:
    """Build immutable entries once for the current intent and capture policies."""
    if not cache.source_digest:
        cache.source_digest = source_fingerprint(snapshot)
    source_digest = cache.source_digest
    traces = {trace.trace_id: trace for trace in snapshot.traces}
    result = []
    specs = {spec.spec_id: spec for spec in config.specifications}
    scopes = sorted(
        {
            (section.spec_id, section.layer, tuple(sorted(pair)))
            for section in sections
            if not section.exclusion_reason
            and section.spec_id in specs
            and specs[section.spec_id].kind.startswith("differential")
            and section.layer not in specs[section.spec_id].excluded_layers
            for pair in snapshot.differential_pairs
            if set(section.net_names).intersection(pair)
        }
    )
    for spec_id, layer, pair in scopes:
        spec = specs[spec_id]
        target = next(
            (
                settings.spacing_nm
                for settings in spec.layer_settings
                if settings.layer == layer
            ),
            None,
        )
        check = context.check(pair, layer, target)
        parent_ids = tuple(
            sorted(
                section.section_id
                for section in sections
                if section.spec_id == spec_id
                and section.layer == layer
                and set(section.net_names).intersection(pair)
            )
        )
        policy_input = (
            source_digest,
            spacing_input_digest(snapshot, spec, layer),
            pair,
            target,
            check.comparison_band_nm,
            SPACING_RENDER_POLICY_REVISION,
            SPACING_IMAGE_WIDTH,
            SPACING_IMAGE_HEIGHT,
            MIN_SPACING_DISPLAY_FEATURE_PIXELS,
        )
        seen = set()
        witnesses = tuple(
            witness
            for measured in context.measurement(pair, layer).traces
            for witness in measured.witnesses
        )
        for concern in _concerns(check.issues, traces, target, cache, witnesses):
            members, bounds, span_groups = (
                concern.members,
                concern.bounds,
                concern.groups,
            )
            codes = tuple(sorted({issue.code for issue in concern.issues}))
            code = codes[0] if len(codes) == 1 else "deviation"
            issue_id = _digest(
                (pair, layer, code, bounds, _canonical_paths(span_groups))
            )
            if issue_id in seen:
                continue
            seen.add(issue_id)
            source_ids = {member.source_trace_id for member in members}
            mate_ids = {
                trace_id for member in members for trace_id in member.mate_trace_ids
            }
            source_traces = tuple(
                traces[key] for key in sorted(source_ids) if key in traces
            )
            mate_traces = tuple(
                traces[key] for key in sorted(mate_ids) if key in traces
            )
            layout = "standard"
            try:
                viewports = _viewports(
                    bounds,
                    concern.detail_nm,
                    span_groups,
                    representative=concern.representative,
                    target=target,
                )
            except _SpacingViewTooSmall:
                # This alternative only changes deterministic physical framing.
                # Missing geometry, exhausted budgets, and uncertain coverage
                # still fail directly; the same joint proof and detail floor
                # must succeed in this wider panel before any image is emitted.
                layout = "wide"
                viewports = _viewports(
                    bounds,
                    concern.detail_nm,
                    span_groups,
                    detail_rect=WIDE_DETAIL_RECT,
                    representative=concern.representative,
                    target=target,
                )
            for viewport in viewports:
                view_id = _digest((issue_id, viewport, SPACING_RENDER_POLICY_REVISION))
                caption = _concern_caption(concern, target)
                caption += " · Dashed lines: target facing edges; ticks: measured gap."
                result.append(
                    SpacingReviewEntry(
                        spec_id,
                        layer,
                        issue_id,
                        view_id,
                        parent_ids,
                        pair,
                        code,
                        caption,
                        bounds,
                        viewport,
                        _digest((policy_input, issue_id, viewport)),
                        source_traces,
                        mate_traces,
                        members=members,
                        target_spacing_nm=target,
                        region_gap_nm=concern.gap_nm,
                        representative=concern.representative,
                        composition_layout=layout,
                    )
                )
                result[-1] = replace(
                    result[-1],
                    input_digest=_digest(
                        (
                            result[-1].input_digest,
                            result[-1].composition.fingerprint_payload,
                        )
                    ),
                )
                if len(result) > MAX_SPACING_REVIEW_IMAGES:
                    raise ValidationError(
                        f"More than {MAX_SPACING_REVIEW_IMAGES} spacing review images are required; "
                        "split the differential signals into more specific net classes."
                    )
    return tuple(result)


def pending_spacing_reviews(
    config: Config,
    snapshot: BoardSnapshot,
    sections: Sequence[Section],
    *,
    context: Optional[SpacingContext] = None,
) -> tuple[SpacingReviewEntry, ...]:
    """List every current diagnostic entry without matching explicit approval."""
    from .review_tracking import find_spacing_image, spacing_image_current

    pending = []
    for entry in spacing_review_entries(config, snapshot, sections, context=context):
        record = find_spacing_image(
            config.review_tracking, entry.spec_id, entry.layer, entry.view_id
        )
        if (
            record is None
            or record.issue_id != entry.issue_id
            or not spacing_image_current(record, entry.input_digest, approved=True)
        ):
            pending.append(entry)
    return tuple(pending)
