"""Pure native-net-class matching, route grouping, and review revision checking."""

from dataclasses import replace
import hashlib
import json
from typing import Any

from .model import (
    KINDS,
    Analysis,
    BoardSnapshot,
    Config,
    Section,
    Specification,
    Trace,
    ValidationError,
    format_length,
    validate_config,
)
from .stackup_model import stackup_fingerprint, width_results_fingerprint

MAX_SECTIONS = 1000
# Bump this independent review revision whenever capture composition, framing,
# or annotation policy materially changes. Revision 6 expands highlight padding
# to 16 px. It retains 15% minimum board coverage before the 0.85 crop, hidden
# inactive copper and dimmed noncopper context, default zone opacity, opaque #FFFF00
# boxes with 2 px strokes, and whole-route fit.
# Keep it pure: matching must not import the native renderer or alter DB schemas.
CAPTURE_POLICY_REVISION = 6


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


def _validate_snapshot(snapshot: BoardSnapshot) -> None:
    """Reject malformed adapter records before matching or hashing geometry."""
    if not isinstance(snapshot, BoardSnapshot):
        raise ValidationError("Invalid board snapshot.")
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
        if not isinstance(trace.points, tuple) or len(trace.points) < 2:
            raise ValidationError("Trace centerlines require at least two points.")
        for point in trace.points:
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or any(type(value) is not int for value in point)
            ):
                raise ValidationError("Trace points must be integer nanometre pairs.")


def _section(spec_id: str, traces: tuple[Trace, ...]) -> Section:
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
    )


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
    for (layer, _width, net), traces in sorted(by_route.items()):
        for component in _components(
            tuple(sorted(traces, key=lambda trace: trace.trace_id)),
            limit_rows=not spec.kind.startswith("differential"),
        ):
            section = _section(spec.spec_id, component)
            sections.append(section)
            if not spec.kind.startswith("differential"):
                _check_section_count(len(sections))
            if _is_branched(component):
                warnings.append(
                    f"{label}: branched route on {layer}, net {net}; review its complete extent."
                )
            if any(len(set(trace.points)) == 1 for trace in component):
                warnings.append(
                    f"{label}: zero-length track in section {section.section_id[:8]}."
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
        rows = _paired_sections(spec, rows, _differential_mates(snapshot))
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
    warnings: list[str] = []
    claims: dict[str, str] = {}
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
        sections.extend(
            _paired_sections(spec, tuple(matched), mates)
            if spec.kind.startswith("differential")
            else matched
        )
        warnings.extend(messages)
        _check_section_count(len(sections))
    ordered = _ordered_sections(tuple(sections), snapshot.layers)
    spec_records = config.to_dict()["specifications"]
    for record in spec_records:
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
    digest = _digest(
        {
            "algorithm_version": 7,
            "capture_policy_revision": CAPTURE_POLICY_REVISION,
            "layers": snapshot.layers,
            "context_digest": snapshot.context_digest,
            "traces": sorted(_trace_record(trace) for trace in snapshot.traces),
            "specifications": sorted(
                spec_records, key=lambda record: record["spec_id"]
            ),
            "stackup": stackup_fingerprint(config.stackup),
            "width_results": width_results_fingerprint(config.width_results),
            **class_context,
            **pair_context,
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
