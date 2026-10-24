"""Pure native-net-class matching, route grouping, and review revision checking."""

from collections import Counter
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
import math
from typing import Any

from .copper_geometry import capsule_inside_polygons, polygon_inside_polygons
from .model import (
    COPPER_FEATURE_SHAPES,
    EXCLUSION_COVERED_STUB,
    KINDS,
    UNIT_NM,
    Analysis,
    BoardSnapshot,
    Config,
    CopperFeature,
    Section,
    Specification,
    Trace,
    ValidationError,
    format_length,
    validate_config,
)
from .regions import (
    MAX_FOREIGN_LENGTH_RATIO,
    MIN_GAP_PIXELS,
    MIN_TRACE_PIXELS,
    RegionGeometry,
    region_quality,
    signal_regions,
    trace_bounds,
)
from .stackup_model import (
    CALCULATION_REVISION,
    stackup_fingerprint,
    width_results_fingerprint,
)

MAX_SECTIONS = 1000
# Advance when route selection or grouping changes, retaining surviving row IDs
# while requiring explicit review of the new document policy.
ALGORITHM_VERSION = 9
# Bump this independent review revision whenever capture composition, framing,
# or annotation policy materially changes. Revision 8 boosts represented native
# stroke RGB with the higher-contrast 30% white/black mix and dims
# surrounding active copper by 50% toward the background, preserving alpha.
# It retains 16 px highlight padding, 15% minimum board coverage before the 0.85
# crop, hidden inactive copper, dimmed noncopper context, default zone opacity,
# opaque #FFFF00 boxes, and whole-region fit.
# Keep it pure: matching must not import the native renderer or alter DB schemas.
CAPTURE_POLICY_REVISION = 8
# Changing the exclusion proof requires a new review, even when row IDs and
# source geometry stay the same. Keep this separate from capture composition.
COVERED_STUB_POLICY_REVISION = 3
_COVERED_STUB_STATUS = (
    "Covered stubs (⊘) are fully under same-net pad or via copper; "
    "review them in the list — they are not exported."
)


def _check_section_count(count: int) -> None:
    """Bound the number of review candidates without imposing a route-size limit."""
    if count > MAX_SECTIONS:
        raise ValidationError(
            f"More than {MAX_SECTIONS} impedance sections match; split the routed signals into more specific net classes."
        )


def _digest(value: object) -> str:
    """Hash a JSON-compatible canonical value using a stable encoding."""
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trace_record(trace: Trace) -> tuple[Any, ...]:
    """Canonicalize centerline direction without losing trace identity."""
    points = min(trace.points, tuple(reversed(trace.points)))
    return trace.trace_id, trace.layer, trace.net, trace.width_nm, points


def _valid_polygons(value: object) -> bool:
    """Require immutable integer contours before consuming native copper proof."""
    return isinstance(value, tuple) and all(
        isinstance(polygon, tuple)
        and bool(polygon)
        and all(
            isinstance(contour, tuple)
            and len(contour) >= 3
            and all(
                isinstance(point, tuple)
                and len(point) == 2
                and all(type(coordinate) is int for coordinate in point)
                for point in contour
            )
            for contour in polygon
        )
        for polygon in value
    )


def _validate_snapshot(snapshot: BoardSnapshot) -> None:
    """Reject malformed adapter records before matching or hashing geometry."""
    if not isinstance(snapshot, BoardSnapshot):
        raise ValidationError("Invalid board snapshot.")
    if snapshot.board_bounds is not None and (
        not isinstance(snapshot.board_bounds, tuple)
        or len(snapshot.board_bounds) != 4
        or any(type(value) is not int for value in snapshot.board_bounds)
        or snapshot.board_bounds[2] <= snapshot.board_bounds[0]
        or snapshot.board_bounds[3] <= snapshot.board_bounds[1]
    ):
        raise ValidationError(
            "Board outline bounds must enclose a valid physical rectangle."
        )
    if not isinstance(snapshot.layers, tuple) or not all(
        isinstance(layer, str) and layer.strip() for layer in snapshot.layers
    ):
        raise ValidationError("Board copper layers must have nonempty names.")
    if len(set(snapshot.layers)) != len(snapshot.layers):
        raise ValidationError("Board copper layers must be a unique tuple.")
    if not isinstance(snapshot.context_digest, str) or not isinstance(
        snapshot.traces, tuple
    ):
        raise ValidationError("Invalid board context digest or trace collection.")
    trace_ids: set[str] = set()
    for trace in snapshot.traces:
        if not isinstance(trace, Trace):
            raise ValidationError("Every board trace must be a Trace.")
        if (
            not isinstance(trace.trace_id, str)
            or not trace.trace_id.strip()
            or trace.trace_id in trace_ids
        ):
            raise ValidationError("Board trace IDs must be nonempty and unique.")
        trace_ids.add(trace.trace_id)
        if trace.layer not in snapshot.layers or not isinstance(trace.net, str):
            raise ValidationError("Trace layer or net name is invalid.")
        if type(trace.width_nm) is not int or trace.width_nm <= 0:
            raise ValidationError("Trace widths must be positive integer nanometres.")
        if type(trace.is_native_arc) is not bool:
            raise ValidationError("Native arc provenance must be boolean.")
        for label in ("native_arc_midpoint", "native_arc_center"):
            point = getattr(trace, label, None)
            if point is not None and (
                not trace.is_native_arc
                or not isinstance(point, tuple)
                or len(point) != 2
                or any(type(value) is not int for value in point)
            ):
                raise ValidationError(
                    "Native arc coordinates must be integer nanometre pairs on an arc."
                )
        radius = getattr(trace, "native_arc_radius_nm", None)
        if radius is not None and (
            not trace.is_native_arc
            or type(radius) not in (int, float)
            or not math.isfinite(radius)
            or radius <= 0
        ):
            raise ValidationError(
                "Native arc radius must be positive and finite on an arc."
            )
        if not _valid_polygons(trace.copper_polygons):
            raise ValidationError("Native arc copper perimeter is invalid.")
        if not isinstance(trace.points, tuple) or len(trace.points) < 2:
            raise ValidationError("Trace centerlines require at least two points.")
        for point in trace.points:
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or any(type(value) is not int for value in point)
            ):
                raise ValidationError("Trace points must be integer nanometre pairs.")
    if not isinstance(snapshot.copper_features, tuple):
        raise ValidationError("Board copper features must be an immutable collection.")
    feature_ids: set[str] = set()
    for feature in snapshot.copper_features:
        if not isinstance(feature, CopperFeature):
            raise ValidationError("Every copper feature must be a CopperFeature.")
        if (
            not isinstance(feature.feature_id, str)
            or not feature.feature_id.strip()
            or feature.feature_id in feature_ids
        ):
            raise ValidationError("Copper feature IDs must be nonempty and unique.")
        feature_ids.add(feature.feature_id)
        if feature.layer not in snapshot.layers or not isinstance(feature.net, str):
            raise ValidationError("Copper feature layer or net name is invalid.")
        if feature.kind not in ("pad", "via"):
            raise ValidationError("Copper feature kind must be pad or via.")
        if not isinstance(feature.footprint_id, str) or not isinstance(
            feature.pad_number, str
        ):
            raise ValidationError(
                "Copper landing ownership must use string identifiers."
            )
        if feature.shape not in COPPER_FEATURE_SHAPES:
            raise ValidationError("Copper feature shape is unsupported.")
        if (
            type(feature.size_x_nm) is not int
            or type(feature.size_y_nm) is not int
            or feature.size_x_nm <= 0
            or feature.size_y_nm <= 0
        ):
            raise ValidationError("Copper feature sizes must be positive nanometres.")
        if (
            not isinstance(feature.center, tuple)
            or len(feature.center) != 2
            or any(type(value) is not int for value in feature.center)
        ):
            raise ValidationError(
                "Copper feature centers must be integer nanometre pairs."
            )
        if type(feature.rotation_deg) not in (int, float) or not math.isfinite(
            float(feature.rotation_deg)
        ):
            raise ValidationError("Copper feature rotation must be a finite angle.")
        if (
            type(feature.corner_radius_nm) is not int
            or feature.corner_radius_nm < 0
            or feature.corner_radius_nm * 2 > min(feature.size_x_nm, feature.size_y_nm)
        ):
            raise ValidationError("Copper feature corner radius is invalid.")
        if not _valid_polygons(feature.polygons) or (
            feature.shape == "polygon" and not feature.polygons
        ):
            raise ValidationError("Copper feature perimeter is invalid.")


def _section(
    spec_id: str, traces: tuple[Trace, ...], *, exclusion_reason: str = ""
) -> Section:
    """Build a deterministic section and its copper-inclusive bounds."""
    traces = tuple(sorted(traces, key=lambda trace: trace.trace_id))
    first = traces[0]
    radius = (first.width_nm + 1) // 2
    points = [point for trace in traces for point in trace.points]
    bounds = (
        min(point[0] for point in points) - radius,
        min(point[1] for point in points) - radius,
        max(point[0] for point in points) + radius,
        max(point[1] for point in points) + radius,
    )
    section_id = _digest((spec_id, tuple(_trace_record(trace) for trace in traces)))
    return Section(
        section_id=section_id,
        spec_id=spec_id,
        layer=first.layer,
        width_nm=first.width_nm,
        traces=traces,
        bounds=bounds,
        net_names=tuple(sorted({trace.net for trace in traces})),
        exclusion_reason=exclusion_reason,
    )


def trace_length_nm(trace: Trace) -> int:
    """Return the centerline length of one track in nanometres."""
    total = 0.0
    for start, end in zip(trace.points, trace.points[1:]):
        total += math.hypot(end[0] - start[0], end[1] - start[1])
    return int(round(total))


def section_length_nm(section: Section) -> int:
    """Return the summed centerline length of a section's tracks."""
    return sum(trace_length_nm(trace) for trace in section.traces)


def workbook_row_cells(
    section: Section,
    spec: Specification,
    *,
    target_width: str = "",
) -> tuple[str, str, str, str, str, str, str, str]:
    """Return the eight text columns for one workbook ListCtrl row.

    Order: layer, net class, target Ω, length, net, width-or-covered-stub,
    target width, segments.
    """
    length_mm = Decimal(section_length_nm(section)) / Decimal(UNIT_NM["mm"])
    length = f"{length_mm.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} mm"
    nets = ", ".join(section.net_names)
    if section.exclusion_reason == EXCLUSION_COVERED_STUB:
        width_or_stub = "Covered stub"
        segments = ""
        target_width = ""
    else:
        width_or_stub = f"{format_length(section.width_nm)} mm"
        segments = str(len(section.traces))
    return (
        section.layer,
        spec.net_class,
        spec.target_ohms,
        length,
        nets,
        width_or_stub,
        target_width,
        segments,
    )


def _local_point(
    x: int, y: int, center: tuple[int, int], rotation_deg: float
) -> tuple[float, float]:
    """Rotate a board point into the feature's local frame."""
    dx = float(x - center[0])
    dy = float(y - center[1])
    if not rotation_deg:
        return dx, dy
    angle = math.radians(float(rotation_deg))
    cosine, sine = math.cos(angle), math.sin(angle)
    # KiCad's positive native rotation maps local +x toward board -y.
    # Apply its inverse to recover the unrotated flash coordinates.
    return dx * cosine - dy * sine, dx * sine + dy * cosine


def _point_inside_feature(
    x: int, y: int, radius_nm: int, feature: CopperFeature
) -> bool:
    """Return whether a disc of radius_nm around (x, y) fits in the feature copper."""
    local_x, local_y = _local_point(x, y, feature.center, feature.rotation_deg)
    half_x = feature.size_x_nm / 2.0
    half_y = feature.size_y_nm / 2.0
    radius = float(radius_nm)
    if feature.shape == "circle":
        return math.hypot(local_x, local_y) + radius <= half_x + 1e-6
    if feature.shape == "rect":
        return (
            abs(local_x) + radius <= half_x + 1e-6
            and abs(local_y) + radius <= half_y + 1e-6
        )
    # Native ovals are capsules, not ellipses. Both ovals and roundrects are
    # rounded boxes; signed boundary distance gives exact disc clearance.
    corner = (
        float(feature.corner_radius_nm)
        if feature.shape == "roundrect"
        else min(half_x, half_y)
    )
    dx = abs(local_x) - (half_x - corner)
    dy = abs(local_y) - (half_y - corner)
    signed_distance = math.hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0) - corner
    return signed_distance + radius <= 1e-6


def is_pad_via_stub(trace: Trace, features: tuple[CopperFeature, ...]) -> bool:
    """Prove continuous containment of every copper capsule in same-net flashes.

    Convex primitives need both endpoint discs to fit; arbitrary native
    polygons additionally need whole-segment clearance from every boundary.
    Different features at isolated samples cannot establish continuous coverage.
    """
    if not features or not trace.net or len(trace.points) < 2:
        return False
    radius = (trace.width_nm + 1) // 2
    candidates = tuple(
        feature
        for feature in features
        if feature.layer == trace.layer and feature.net == trace.net
    )
    if not candidates:
        return False
    if trace.is_native_arc:
        # Samples cannot prove containment of native curves. An outward native
        # stroke includes the actual arc width and round caps. In a convex pad,
        # containing all outer vertices contains every edge and filled area.
        return bool(trace.copper_polygons) and all(
            any(
                polygon_inside_polygons(polygon, feature.polygons)
                if feature.shape == "polygon"
                else all(
                    _point_inside_feature(*point, 0, feature) for point in polygon[0]
                )
                for feature in candidates
            )
            for polygon in trace.copper_polygons
        )
    return all(
        any(
            capsule_inside_polygons(start, end, radius, feature.polygons)
            if feature.shape == "polygon"
            else (
                _point_inside_feature(*start, radius, feature)
                and _point_inside_feature(*end, radius, feature)
            )
            for feature in candidates
        )
        for start, end in zip(trace.points, trace.points[1:])
    )


def _split_covered_stubs(
    spec_id: str,
    component: tuple[Trace, ...],
    features: tuple[CopperFeature, ...],
) -> tuple[list[Section], list[Section]]:
    """Separate covered stubs from exportable copper within one endpoint component."""
    stubs = tuple(trace for trace in component if is_pad_via_stub(trace, features))
    keep = tuple(
        trace
        for trace in component
        if trace.trace_id not in {item.trace_id for item in stubs}
    )
    exportable: list[Section] = []
    excluded: list[Section] = []
    if keep:
        for group in _components(keep, limit_rows=False):
            exportable.append(_section(spec_id, group))
    if stubs:
        for group in _components(stubs, limit_rows=False):
            excluded.append(
                _section(spec_id, group, exclusion_reason=EXCLUSION_COVERED_STUB)
            )
    return exportable, excluded


def _components(
    traces: tuple[Trace, ...], limit_rows: bool = True
) -> tuple[tuple[Trace, ...], ...]:
    """Group one net's centerlines by shared endpoints without geometric snapping."""
    by_endpoint: dict[tuple[int, int], set[int]] = {}
    for index, trace in enumerate(traces):
        for endpoint in (trace.points[0], trace.points[-1]):
            by_endpoint.setdefault(endpoint, set()).add(index)
    remaining = set(range(len(traces)))
    components = []
    while remaining:
        pending = [remaining.pop()]
        component: list[Trace] = []
        while pending:
            index = pending.pop()
            trace = traces[index]
            component.append(trace)
            for endpoint in (trace.points[0], trace.points[-1]):
                neighbors = by_endpoint[endpoint].intersection(remaining)
                remaining.difference_update(neighbors)
                pending.extend(sorted(neighbors))
        components.append(tuple(sorted(component, key=lambda trace: trace.trace_id)))
        if limit_rows:
            _check_section_count(len(components))
    return tuple(components)


def _is_branched(traces: tuple[Trace, ...]) -> bool:
    """Detect shared endpoints with more than two incident track ends."""
    degrees: dict[tuple[int, int], int] = {}
    for trace in traces:
        if trace.points[0] == trace.points[-1]:
            continue
        for endpoint in (trace.points[0], trace.points[-1]):
            degrees[endpoint] = degrees.get(endpoint, 0) + 1
    return any(degree > 2 for degree in degrees.values())


def _netclass_memberships(snapshot: BoardSnapshot) -> dict[str, tuple[str, ...]]:
    """Require trustworthy native membership metadata only for class-based matching."""
    if not isinstance(snapshot.net_class_error, str):
        raise ValidationError("Invalid net-class metadata error.")
    if snapshot.net_class_error:
        raise ValidationError(
            "Net-class matching is unavailable: " + snapshot.net_class_error
        )
    if (
        not isinstance(snapshot.net_classes, tuple)
        or not snapshot.net_classes
        or any(
            not isinstance(name, str) or not name.strip()
            for name in snapshot.net_classes
        )
        or len(set(snapshot.net_classes)) != len(snapshot.net_classes)
    ):
        raise ValidationError(
            "Net-class catalog metadata is missing or invalid; reload the board."
        )
    if (
        not isinstance(snapshot.net_class_context_digest, str)
        or not snapshot.net_class_context_digest
    ):
        raise ValidationError(
            "Net-class settings metadata is missing; reload the board."
        )
    if not isinstance(snapshot.net_class_memberships, tuple):
        raise ValidationError(
            "Net-class membership metadata must be an immutable collection."
        )
    memberships: dict[str, tuple[str, ...]] = {}
    for record in snapshot.net_class_memberships:
        if not isinstance(record, tuple) or len(record) != 2:
            raise ValidationError("Invalid net-class membership metadata record.")
        net, names = record
        if not isinstance(net, str) or not net.strip() or net in memberships:
            raise ValidationError(
                "Net-class membership metadata needs distinct named nets."
            )
        if (
            not isinstance(names, tuple)
            or not names
            or any(
                not isinstance(name, str) or name not in snapshot.net_classes
                for name in names
            )
            or len(set(names)) != len(names)
        ):
            raise ValidationError(
                f"Invalid net-class membership metadata for net {net!r}."
            )
        memberships[net] = names
    missing = sorted(
        {trace.net for trace in snapshot.traces if trace.net} - memberships.keys()
    )
    if missing:
        raise ValidationError(
            "Net-class membership metadata is missing for routed nets: "
            + ", ".join(missing)
        )
    return memberships


def _netclass_sections(
    spec: Specification,
    snapshot: BoardSnapshot,
    memberships: dict[str, tuple[str, ...]],
    require_layer_settings: bool = True,
) -> tuple[list[Section], list[str]]:
    """Discover every actual layer/width in selected class members without guessing pairs."""
    label = spec.label or spec.spec_id
    if spec.net_class not in snapshot.net_classes:
        raise ValidationError(
            f"{label}: net class {spec.net_class!r} is unavailable; select an existing class."
        )
    members = {net for net, names in memberships.items() if spec.net_class in names}
    selected = tuple(
        trace for trace in snapshot.traces if trace.net and trace.net in members
    )
    routed_layers = {trace.layer for trace in selected}
    if require_layer_settings:
        configured = {settings.layer for settings in spec.layer_settings}
        missing = routed_layers - configured - set(spec.excluded_layers)
        if missing:
            raise ValidationError(
                f"{label}: routed class layers need reference planes and gaps or explicit exclusion: "
                + ", ".join(layer for layer in snapshot.layers if layer in missing)
                + "; edit the specification and review every routed layer."
            )
    warnings = [
        f"{label}: routed layer {layer} is explicitly excluded from impedance documentation."
        for layer in snapshot.layers
        if layer in routed_layers and layer in spec.excluded_layers
    ]
    by_route: dict[tuple[str, int, str], list[Trace]] = {}
    for trace in selected:
        if trace.layer not in spec.excluded_layers:
            by_route.setdefault((trace.layer, trace.width_nm, trace.net), []).append(
                trace
            )
    if not by_route:
        warnings.append(
            f"{label}: no included traces match net class {spec.net_class!r}."
        )
    sections: list[Section] = []
    by_signal: dict[tuple[str, str], list[Trace]] = {}
    for (layer, _width, net), traces in sorted(by_route.items()):
        for component in _components(
            tuple(sorted(traces, key=lambda trace: trace.trace_id)),
            limit_rows=False,
        ):
            exportable, excluded = _split_covered_stubs(
                spec.spec_id, component, snapshot.copper_features
            )
            for section in (*exportable, *excluded):
                sections.append(section)
                if section.exclusion_reason:
                    continue
                by_signal.setdefault((layer, net), []).extend(section.traces)
                if any(len(set(trace.points)) == 1 for trace in section.traces):
                    warnings.append(
                        f"{label}: zero-length track on {layer}, net {net}."
                    )
    for (layer, net), traces in sorted(by_signal.items()):
        if _is_branched(tuple(traces)):
            warnings.append(
                f"{label}: branched route on {layer}, net {net}; review its complete extent."
            )
    return sections, warnings


def _differential_mates(snapshot: BoardSnapshot) -> dict[str, str]:
    """Validate authoritative native pair identities, never inferring them from names."""
    if not isinstance(snapshot.differential_pair_error, str):
        raise ValidationError("Invalid native differential pair metadata error.")
    if snapshot.differential_pair_error:
        raise ValidationError(
            "Native differential pairing is unavailable: "
            + snapshot.differential_pair_error
        )
    if not isinstance(snapshot.differential_pairs, tuple):
        raise ValidationError(
            "Native differential pairs must be an immutable collection."
        )
    mates: dict[str, str] = {}
    for pair in snapshot.differential_pairs:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or any(not isinstance(net, str) or not net.strip() for net in pair)
            or pair[0] == pair[1]
        ):
            raise ValidationError(
                "Each native differential pair needs two distinct named nets."
            )
        left, right = pair
        if left in mates or right in mates:
            raise ValidationError(
                "Native differential pair metadata is ambiguous: a net belongs to multiple pairs."
            )
        mates[left], mates[right] = right, left
    return mates


def _paired_sections(
    spec: Specification, sections: tuple[Section, ...], mates: dict[str, str]
) -> tuple[Section, ...]:
    """Make one complete native pair/layer/actual-width row, including all copper islands."""
    by_pair: dict[tuple[str, int, tuple[str, str]], list[Trace]] = {}
    for section in sections:
        net = section.net_names[0]
        if net not in mates:
            raise ValidationError(
                f"{spec.label or spec.spec_id}: net {net or '(unassigned)'!r} has no verified native differential mate; "
                "check KiCad differential-pair identities and class membership."
            )
        pair = tuple(sorted((net, mates[net])))
        by_pair.setdefault((section.layer, section.width_nm, pair), []).extend(
            section.traces
        )
    result = []
    for (layer, width, pair), traces in sorted(by_pair.items()):
        if {trace.net for trace in traces} != set(pair):
            raise ValidationError(
                f"{spec.label or spec.spec_id}: differential pair {pair[0]!r} / {pair[1]!r} is incomplete "
                f"on {layer} at {format_length(width)} mm; both mates must belong to the selected net class "
                "and have matching layer/width coverage."
            )
        result.append(_section(spec.spec_id, tuple(traces)))
        _check_section_count(len(result))
    return tuple(result)


def _outermost_sections(
    sections: tuple[Section, ...],
    snapshot: BoardSnapshot,
    geometry: RegionGeometry,
) -> tuple[Section, ...]:
    """Consolidate redundant regions while preserving readable member detail."""
    by_geometry: dict[tuple[str, str, int], list[Section]] = {}
    for section in sections:
        if not section.exclusion_reason:
            key = (section.spec_id, section.layer, section.width_nm)
            by_geometry.setdefault(key, []).append(section)
    retained = [section for section in sections if section.exclusion_reason]
    for group in by_geometry.values():
        owners: list[Section] = []
        for section in sorted(
            group,
            key=lambda item: (
                -_area(item),
                item.bounds,
                item.net_names,
                item.section_id,
            ),
        ):
            containers = []
            for index, other in enumerate(owners):
                if not _contains(other.bounds, section.bounds):
                    continue
                merged = _section(other.spec_id, other.traces + section.traces)
                quality = region_quality(merged.traces, snapshot, geometry)
                # A containing crop can hide a narrow gap visible in a small
                # closeup. Such a box supplies useful detail and is retained.
                if quality.readable or all(
                    quality.trace_pixels + 1e-9
                    >= min(MIN_TRACE_PIXELS, previous.trace_pixels)
                    and previous.gap_pixels
                    * quality.trace_pixels
                    / previous.trace_pixels
                    + 1e-9
                    >= min(MIN_GAP_PIXELS, previous.gap_pixels)
                    and quality.foreign_ratio
                    <= max(MAX_FOREIGN_LENGTH_RATIO, previous.foreign_ratio)
                    for previous in (
                        region_quality(other.traces, snapshot, geometry),
                        region_quality(section.traces, snapshot, geometry),
                    )
                ):
                    containers.append(index)
            if containers:
                index = min(
                    containers,
                    key=lambda candidate: (
                        _area(owners[candidate]),
                        owners[candidate].bounds,
                        owners[candidate].section_id,
                    ),
                )
                owner = owners[index]
                owners[index] = _section(owner.spec_id, owner.traces + section.traces)
            else:
                owners.append(section)
        retained.extend(owners)
    return tuple(retained)


def _area(section: Section) -> int:
    """Compare physical region sizes without image padding or floating-point ties."""
    left, top, right, bottom = section.bounds
    return (right - left) * (bottom - top)


def _contains(
    outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]
) -> bool:
    """Allow shared sides and equal boxes in board coordinates."""
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _signal_regions(
    sections: tuple[Section, ...],
    snapshot: BoardSnapshot,
    geometry: RegionGeometry,
) -> tuple[Section, ...]:
    """Grow all compatible pieces of each signal before cross-signal pruning."""
    by_signal: dict[tuple[str, str, int, str], list[Trace]] = {}
    result = [section for section in sections if section.exclusion_reason]
    for section in sections:
        if not section.exclusion_reason:
            key = (
                section.spec_id,
                section.layer,
                section.width_nm,
                section.net_names[0],
            )
            by_signal.setdefault(key, []).extend(section.traces)
    for (spec_id, _layer, _width, _net), traces in sorted(by_signal.items()):
        result.extend(
            _section(spec_id, group)
            for group in signal_regions(tuple(traces), snapshot, geometry)
        )
    return _outermost_sections(tuple(result), snapshot, geometry)


def _validate_region_coverage(
    source: tuple[Section, ...], regions: tuple[Section, ...]
) -> None:
    """Fail closed if layout loses, duplicates, relabels or crops eligible copper."""
    expected = {
        trace.trace_id: (trace, row.spec_id, row.exclusion_reason)
        for row in source
        for trace in row.traces
    }
    actual = Counter(trace.trace_id for row in regions for trace in row.traces)
    if actual != Counter(dict.fromkeys(expected, 1)):
        raise ValidationError(
            "Impedance region coverage is incomplete or duplicates source tracks; review the board."
        )
    for row in regions:
        if row.net_names != tuple(sorted({trace.net for trace in row.traces})):
            raise ValidationError(
                "Impedance region signal labels do not match their represented tracks."
            )
        for trace in row.traces:
            original, spec_id, exclusion = expected[trace.trace_id]
            if (
                trace != original
                or row.spec_id != spec_id
                or row.exclusion_reason != exclusion
                or row.layer != trace.layer
                or row.width_nm != trace.width_nm
                or not _contains(row.bounds, trace_bounds(trace))
            ):
                raise ValidationError(
                    "Impedance region coverage changed or cropped a source track."
                )


def _ordered_sections(
    sections: tuple[Section, ...], layers: tuple[str, ...]
) -> tuple[Section, ...]:
    """Share deterministic production row ordering between drafts and final analysis."""
    order = {layer: index for index, layer in enumerate(layers)}
    return tuple(
        sorted(
            sections,
            key=lambda section: (
                section.spec_id,
                order[section.layer],
                section.bounds,
                section.net_names,
                section.section_id,
            ),
        )
    )


def _preview_selection(spec: Specification, layers: tuple[str, ...]) -> None:
    """Validate only immutable selection intent, without inventing fabrication dimensions."""
    if not isinstance(spec, Specification):
        raise ValidationError("Invalid impedance preview specification.")
    if (
        not isinstance(spec.spec_id, str)
        or not spec.spec_id.strip()
        or not isinstance(spec.label, str)
    ):
        raise ValidationError("Preview specification identity and label are invalid.")
    if spec.kind not in KINDS:
        raise ValidationError("Select an impedance kind before previewing routes.")

    def names(values: tuple[str, ...], label: str) -> None:
        """Keep draft layer exclusions exact and duplicate-free."""
        if (
            not isinstance(values, tuple)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValidationError(
                f"Preview {label} must contain distinct nonempty names."
            )

    if not isinstance(spec.net_class, str) or not spec.net_class.strip():
        raise ValidationError("Select a net class before previewing routes.")
    names(spec.excluded_layers, "excluded layers")
    if any(layer not in layers for layer in spec.excluded_layers):
        raise ValidationError("Preview excluded layers must be enabled copper layers.")


def preview_sections(
    spec: Specification, snapshot: BoardSnapshot, layer: str
) -> tuple[Section, ...]:
    """Preview real production geometry while target/reference/gap fields remain incomplete."""
    _validate_snapshot(snapshot)
    _preview_selection(spec, snapshot.layers)
    if layer not in snapshot.layers:
        raise ValidationError(
            "Select an enabled signal layer before previewing routes."
        )
    # Preserve native metadata from the complete live board while restricting
    # geometry; errors elsewhere in a draft must not create fake local pair data.
    local = replace(
        snapshot,
        traces=tuple(trace for trace in snapshot.traces if trace.layer == layer),
    )
    memberships = _netclass_memberships(snapshot)
    sections, _ = _netclass_sections(
        spec, local, memberships, require_layer_settings=False
    )
    rows = tuple(sections)
    if spec.kind.startswith("differential"):
        exportable = tuple(section for section in rows if not section.exclusion_reason)
        excluded = tuple(section for section in rows if section.exclusion_reason)
        rows = (
            _paired_sections(spec, exportable, _differential_mates(snapshot)) + excluded
        )
    else:
        rows = _signal_regions(rows, snapshot, RegionGeometry(snapshot))
    _validate_region_coverage(tuple(sections), rows)
    _check_section_count(len(rows))
    return _ordered_sections(rows, snapshot.layers)


def analyze(config: Config, snapshot: BoardSnapshot) -> Analysis:
    """Match intent and produce candidates whose review becomes stale on relevant edits."""
    _validate_snapshot(snapshot)
    validate_config(config, snapshot.layers)
    memberships = _netclass_memberships(snapshot) if config.specifications else {}
    uses_pairs = any(
        spec.kind.startswith("differential") for spec in config.specifications
    )
    mates = _differential_mates(snapshot) if uses_pairs else {}
    sections: list[Section] = []
    source_sections: list[Section] = []
    warnings: list[str] = []
    claims: dict[str, str] = {}
    geometry = RegionGeometry(snapshot)
    for spec in sorted(config.specifications, key=lambda spec: spec.spec_id):
        matched, messages = _netclass_sections(spec, snapshot, memberships)
        for section in matched:
            for trace in section.traces:
                if trace.trace_id in claims:
                    raise ValidationError(
                        f"Impedance specifications overlap: trace {trace.trace_id!r} is claimed by "
                        f"{claims[trace.trace_id]!r} and {spec.label or spec.spec_id!r}; assign each candidate only once."
                    )
                claims[trace.trace_id] = spec.label or spec.spec_id
        source_sections.extend(matched)
        if spec.kind.startswith("differential"):
            exportable = tuple(
                section for section in matched if not section.exclusion_reason
            )
            excluded = tuple(section for section in matched if section.exclusion_reason)
            sections.extend(_paired_sections(spec, exportable, mates))
            sections.extend(excluded)
        else:
            sections.extend(_signal_regions(tuple(matched), snapshot, geometry))
        warnings.extend(messages)
        _check_section_count(len(sections))
    ordered = _ordered_sections(tuple(sections), snapshot.layers)
    _validate_region_coverage(tuple(source_sections), ordered)
    for section in ordered:
        if (
            not section.exclusion_reason
            and not region_quality(section.traces, snapshot, geometry).readable
        ):
            warnings.append(
                f"{section.spec_id}: region on {section.layer}, nets {', '.join(section.net_names)} falls below the capture readability target; all source tracks are retained for review."
            )
    if any(section.exclusion_reason == EXCLUSION_COVERED_STUB for section in ordered):
        warnings.append(_COVERED_STUB_STATUS)
    spec_records = config.to_dict()["specifications"]
    for record in spec_records:
        # Linking is an editing preference; unchanged physical intent retains
        # the same review fingerprint across payload upgrades and link changes.
        record.pop("linked_layer_groups", None)
        record.pop("independent_layers", None)
        record["excluded_layers"].sort()
        record["layer_settings"].sort(key=lambda settings: settings["layer"])
        for settings in record["layer_settings"]:
            settings["reference_layers"].sort()
    class_context = {
        "net_classes": sorted(snapshot.net_classes),
        "net_class_memberships": sorted(
            (net, sorted(names)) for net, names in memberships.items()
        ),
        "net_class_context_digest": snapshot.net_class_context_digest,
    }
    pair_context = (
        {
            "differential_pairs": sorted(
                {tuple(sorted((net, mate))) for net, mate in mates.items()}
            )
        }
        if uses_pairs
        else {}
    )
    spacing_context: dict[str, Any] = {}
    if uses_pairs:
        from .review_tracking import spacing_review_context
        from .spacing_checks import spacing_input_digest

        spacing_context["spacing_checks"] = [
            (
                spec.spec_id,
                layer,
                spacing_review_context(spec, layer, ordered),
                spacing_input_digest(snapshot, spec, layer),
            )
            for spec in sorted(config.specifications, key=lambda item: item.spec_id)
            if spec.kind.startswith("differential")
            for layer in sorted(
                {
                    section.layer
                    for section in ordered
                    if section.spec_id == spec.spec_id
                }
            )
        ]
    # Preserve section IDs so historical row approvals remain identifiable.
    # A separate exclusion-policy revision invalidates current approval when
    # corrected containment changes exportable rows without changing geometry.
    digest = _digest(
        {
            "algorithm_version": ALGORITHM_VERSION,
            "capture_policy_revision": CAPTURE_POLICY_REVISION,
            "covered_stub_policy_revision": COVERED_STUB_POLICY_REVISION,
            "calculation_revision": CALCULATION_REVISION,
            "layers": snapshot.layers,
            "board_bounds": snapshot.board_bounds,
            "context_digest": snapshot.context_digest,
            "traces": sorted(_trace_record(trace) for trace in snapshot.traces),
            "native_arc_trace_ids": sorted(
                trace.trace_id for trace in snapshot.traces if trace.is_native_arc
            ),
            "native_arc_copper": sorted(
                (trace.trace_id, trace.copper_polygons)
                for trace in snapshot.traces
                if trace.is_native_arc and trace.copper_polygons
            ),
            "specifications": sorted(
                spec_records, key=lambda record: record["spec_id"]
            ),
            "stackup": stackup_fingerprint(config.stackup),
            "width_results": width_results_fingerprint(config.width_results),
            **class_context,
            **pair_context,
            **spacing_context,
        }
    )
    return Analysis(ordered, digest, tuple(sorted(warnings)))


def validate_review(config: Config, analysis: Analysis) -> None:
    """Require a current explicit inclusion review, with every specification represented."""
    validate_config(config)
    if not config.enabled:
        return
    if not config.specifications:
        raise ValidationError(
            "Controlled impedance requires at least one specification."
        )
    if not config.reviewed_digest or config.reviewed_digest != analysis.digest:
        raise ValidationError(
            "The board, impedance specifications, or capture policy changed; "
            "scan and review the sections again."
        )
    if not config.included_section_ids:
        raise ValidationError(
            "Include at least one reviewed section before generating the impedance form."
        )
    available = {section.section_id: section for section in analysis.sections}
    if any(section_id not in available for section_id in config.included_section_ids):
        raise ValidationError(
            "An included section changed or disappeared; review the sections again."
        )
    represented = {
        available[section_id].spec_id for section_id in config.included_section_ids
    }
    missing = [
        spec.label or spec.spec_id
        for spec in config.specifications
        if spec.spec_id not in represented
    ]
    if missing:
        raise ValidationError(
            f"Every specification requires a reviewed section; missing: {', '.join(missing)}."
        )
    by_id = {spec.spec_id: spec for spec in config.specifications}
    for section_id in config.included_section_ids:
        section = available[section_id]
        spec = by_id[section.spec_id]
        if spec.kind.startswith("differential") and (
            len(section.net_names) != 2
            or any(not net.strip() for net in section.net_names)
        ):
            raise ValidationError(
                "Each differential section must contain exactly two named native mates; check pairing and review the updated workbook rows."
            )
