"""Persist latest review events without turning their timestamps into approval gates.

Records retain the fingerprint the user actually reviewed.  Callers compare that
fingerprint with the current capture/settings before describing a record as
current; an old timestamp alone must never authorize a changed board.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import localcontext
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, NoReturn, Optional

if TYPE_CHECKING:
    from .model import BoardSnapshot, Section, Specification, Trace
    from .stackup_model import Stackup, WidthResult

MAX_IMAGE_RECORDS = 10_000
MAX_LAYER_RECORDS = 1_000
MAX_APPROVAL_CAPTURES = 1_000
_UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ImageView:
    """Latest successful viewing of one logical workbook image."""

    spec_id: str
    layer: str
    section_id: str
    capture_digest: str
    viewed_at_utc: str


@dataclass(frozen=True)
class LayerApproval:
    """Latest explicit approval and the exact captures that accompanied it."""

    spec_id: str
    layer: str
    settings_digest: str
    approved_at_utc: str
    captures: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReviewTracking:
    """Board-scoped latest events, independent of manufacturing intent."""

    images: tuple[ImageView, ...] = ()
    layers: tuple[LayerApproval, ...] = ()


def _invalid(message: str) -> NoReturn:
    """Raise the shared model error without a module-level import cycle."""
    from .model import ValidationError

    raise ValidationError(message)


def _name(value: object, label: str) -> None:
    """Accept the same exact nonempty identifiers as existing configurations."""
    if not isinstance(value, str) or not value.strip():
        _invalid(f"Review {label} must be a nonempty string.")


def _digest_value(value: object, label: str) -> None:
    """Require the canonical hexadecimal form produced by SHA-256."""
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        _invalid(f"Review {label} must be a lowercase SHA-256 digest.")


def _parse_timestamp(value: str) -> datetime:
    """Accept only complete, unambiguous UTC timestamps with microseconds."""
    if not isinstance(value, str) or _UTC_PATTERN.fullmatch(value) is None:
        _invalid("Review timestamps must use YYYY-MM-DDTHH:MM:SS.ffffffZ UTC format.")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid("Review timestamp contains an invalid date or time.")


def utc_now() -> str:
    """Read the clock only when an explicit viewing or approval event calls us."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def format_timestamp(value: str, *, local: bool = False) -> str:
    """Display a recorded timestamp with an explicit UTC or local UTC offset."""
    moment = _parse_timestamp(value)
    if not local:
        return moment.strftime("%Y-%m-%d %H:%M:%S UTC")
    moment = moment.astimezone()
    offset = moment.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    return f"{moment:%Y-%m-%d %H:%M:%S} {moment.tzname()} (UTC{offset})"


def validate_tracking(tracking: ReviewTracking) -> None:
    """Strictly validate bounded latest-event collections, including old history."""
    if not isinstance(tracking, ReviewTracking):
        _invalid("Review tracking must be a ReviewTracking record.")
    if (
        not isinstance(tracking.images, tuple)
        or len(tracking.images) > MAX_IMAGE_RECORDS
    ):
        _invalid(
            f"Review images must be a tuple with at most {MAX_IMAGE_RECORDS} records."
        )
    if (
        not isinstance(tracking.layers, tuple)
        or len(tracking.layers) > MAX_LAYER_RECORDS
    ):
        _invalid(
            f"Review layers must be a tuple with at most {MAX_LAYER_RECORDS} records."
        )
    image_keys: set[tuple[str, str, str]] = set()
    for record in tracking.images:
        if not isinstance(record, ImageView):
            _invalid("Every reviewed image must be an ImageView record.")
        _name(record.spec_id, "specification ID")
        _name(record.layer, "signal layer")
        _name(record.section_id, "section ID")
        _digest_value(record.capture_digest, "capture fingerprint")
        _parse_timestamp(record.viewed_at_utc)
        key = (record.spec_id, record.layer, record.section_id)
        if key in image_keys:
            _invalid("Review images must not repeat a specification/layer/section key.")
        image_keys.add(key)
    layer_keys: set[tuple[str, str]] = set()
    for record in tracking.layers:
        if not isinstance(record, LayerApproval):
            _invalid("Every reviewed layer must be a LayerApproval record.")
        _name(record.spec_id, "specification ID")
        _name(record.layer, "signal layer")
        _digest_value(record.settings_digest, "layer settings fingerprint")
        _parse_timestamp(record.approved_at_utc)
        key = (record.spec_id, record.layer)
        if key in layer_keys:
            _invalid("Review layers must not repeat a specification/layer key.")
        layer_keys.add(key)
        if (
            not isinstance(record.captures, tuple)
            or len(record.captures) > MAX_APPROVAL_CAPTURES
        ):
            _invalid(
                "Approved captures must be a tuple with at most "
                f"{MAX_APPROVAL_CAPTURES} entries."
            )
        section_ids: set[str] = set()
        for capture in record.captures:
            if not isinstance(capture, tuple) or len(capture) != 2:
                _invalid(
                    "An approved capture must contain its section ID and fingerprint."
                )
            section_id, digest = capture
            _name(section_id, "approved section ID")
            _digest_value(digest, "approved capture fingerprint")
            if section_id in section_ids:
                _invalid("Approved captures must not repeat a section ID.")
            section_ids.add(section_id)


def review_tracking_to_dict(tracking: ReviewTracking) -> dict[str, Any]:
    """Serialize latest review events without modifying their times or order."""
    validate_tracking(tracking)
    return {
        "images": [asdict(record) for record in tracking.images],
        "layers": [
            {**asdict(record), "captures": [list(pair) for pair in record.captures]}
            for record in tracking.layers
        ],
    }


def _object(value: object, fields: set[str], label: str) -> dict[str, Any]:
    """Require exactly the fields belonging to this tracking schema."""
    if not isinstance(value, dict) or set(value) != fields:
        _invalid(f"Invalid review {label}: missing or unknown fields.")
    return value


def _array(value: object, limit: int, label: str) -> list[Any]:
    """Reject nonarrays and excessive payloads before decoding their records."""
    if not isinstance(value, list) or len(value) > limit:
        _invalid(f"Review {label} must be an array with at most {limit} entries.")
    return value


def review_tracking_from_dict(value: object) -> ReviewTracking:
    """Decode all records strictly; absent legacy tracking is handled by Config."""
    data = _object(value, {"images", "layers"}, "tracking")
    images = tuple(
        ImageView(
            **_object(
                item,
                {"spec_id", "layer", "section_id", "capture_digest", "viewed_at_utc"},
                "image",
            )
        )
        for item in _array(data["images"], MAX_IMAGE_RECORDS, "images")
    )
    layers = []
    for item in _array(data["layers"], MAX_LAYER_RECORDS, "layers"):
        record = _object(
            item,
            {"spec_id", "layer", "settings_digest", "approved_at_utc", "captures"},
            "layer",
        )
        captures = []
        for pair in _array(
            record["captures"], MAX_APPROVAL_CAPTURES, "approved captures"
        ):
            if not isinstance(pair, list) or len(pair) != 2:
                _invalid(
                    "An approved capture must be an array of section ID and fingerprint."
                )
            captures.append((pair[0], pair[1]))
        layers.append(LayerApproval(**{**record, "captures": tuple(captures)}))
    result = ReviewTracking(images, tuple(layers))
    validate_tracking(result)
    return result


def _sha256(value: object) -> str:
    """Hash canonical JSON, never process-dependent hashes or representations."""
    payload = json.dumps(
        value,
        default=asdict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trace_record(trace: "Trace") -> dict[str, Any]:
    """Preserve all trace content while canonicalizing centerline direction."""
    return {
        **{field.name: getattr(trace, field.name) for field in fields(trace)},
        "points": min(trace.points, tuple(reversed(trace.points))),
    }


def source_fingerprint(snapshot: "BoardSnapshot") -> str:
    """Identify the complete board source, including native rendering context."""
    from .matching import _validate_snapshot

    _validate_snapshot(snapshot)
    record = {field.name: getattr(snapshot, field.name) for field in fields(snapshot)}
    record["traces"] = [
        _trace_record(trace)
        for trace in sorted(snapshot.traces, key=lambda item: item.trace_id)
    ]
    record["net_classes"] = sorted(snapshot.net_classes)
    record["net_class_memberships"] = sorted(
        (net, sorted(names)) for net, names in snapshot.net_class_memberships
    )
    record["differential_pairs"] = sorted(
        tuple(sorted(pair)) for pair in snapshot.differential_pairs
    )
    return _sha256(record)


def _selection_record(spec: "Specification", layer: str) -> dict[str, Any]:
    """Describe selection for this signal layer without its fabrication settings."""
    return {
        "spec_id": spec.spec_id,
        "kind": spec.kind,
        "layer": layer,
        "net_class": spec.net_class,
    }


def _section_record(section: "Section") -> dict[str, Any]:
    """Capture the exact row identity and complete selected route geometry."""
    return {
        **{field.name: getattr(section, field.name) for field in fields(section)},
        "net_names": sorted(section.net_names),
        "traces": [
            _trace_record(trace)
            for trace in sorted(section.traces, key=lambda item: item.trace_id)
        ],
    }


def capture_fingerprint(
    source_digest: str, spec: "Specification", section: "Section", image_sha256: str
) -> str:
    """Bind viewed geometry to actual PNG bytes, not target/reference field edits."""
    from .matching import CAPTURE_POLICY_REVISION

    _digest_value(source_digest, "source fingerprint")
    _digest_value(image_sha256, "image fingerprint")
    return _sha256(
        {
            "source": source_digest,
            "selection": _selection_record(spec, section.layer),
            "section": _section_record(section),
            "capture_policy_revision": CAPTURE_POLICY_REVISION,
            "image_sha256": image_sha256,
        }
    )


def layer_fingerprint(
    source_digest: str,
    spec: "Specification",
    layer: str,
    sections: Sequence["Section"],
    *,
    stackup: Optional["Stackup"] = None,
    width_results: Sequence["WidthResult"] = (),
) -> str:
    """Bind an approval to normalized intent and the exact row set on this layer."""
    from .matching import (
        ALGORITHM_VERSION,
        CAPTURE_POLICY_REVISION,
        COVERED_STUB_POLICY_REVISION,
    )
    from .model import positive_decimal, resolved_layer_settings
    from .stackup_model import (
        CALCULATION_REVISION,
        stackup_fingerprint,
        width_results_fingerprint,
    )

    _digest_value(source_digest, "source fingerprint")
    settings = resolved_layer_settings(spec, layer)
    with localcontext() as context:
        context.prec = 96
        target = str(positive_decimal(spec.target_ohms, "target impedance").normalize())
    return _sha256(
        {
            "source": source_digest,
            "selection": _selection_record(spec, layer),
            "target_ohms": target,
            "settings": {
                **asdict(settings),
                "reference_layers": sorted(settings.reference_layers),
            },
            "stackup": stackup_fingerprint(stackup),
            "width_results": width_results_fingerprint(
                tuple(
                    result
                    for result in width_results
                    if result.spec_id == spec.spec_id and result.layer == layer
                )
            ),
            "sections": [
                _section_record(section)
                for section in sorted(sections, key=lambda item: item.section_id)
                if section.layer == layer and section.spec_id == spec.spec_id
            ],
            "algorithm_version": ALGORITHM_VERSION,
            "capture_policy_revision": CAPTURE_POLICY_REVISION,
            "covered_stub_policy_revision": COVERED_STUB_POLICY_REVISION,
            "calculation_revision": CALCULATION_REVISION,
        }
    )


def find_image(
    tracking: ReviewTracking, spec_id: str, layer: str, section_id: str
) -> Optional[ImageView]:
    """Return the latest event, whose fingerprint may represent historical content."""
    return next(
        (
            record
            for record in tracking.images
            if (record.spec_id, record.layer, record.section_id)
            == (spec_id, layer, section_id)
        ),
        None,
    )


def find_layer(
    tracking: ReviewTracking, spec_id: str, layer: str
) -> Optional[LayerApproval]:
    """Return the recorded approval without claiming that it remains current."""
    return next(
        (
            record
            for record in tracking.layers
            if (record.spec_id, record.layer) == (spec_id, layer)
        ),
        None,
    )


def expected_capture_digest(
    tracking: ReviewTracking,
    source_digest: str,
    spec: "Specification",
    section: "Section",
    sections: Sequence["Section"],
    *,
    stackup: Optional["Stackup"] = None,
    width_results: Sequence["WidthResult"] = (),
) -> Optional[str]:
    """Select known pixels without treating historical settings as approval.

    The caller separately requires current workbook consent. A latest image
    event is authoritative, including when a layer approval records older
    pixels. A layer-only capture is evidence only for its current settings.
    No evidence retains compatibility with configurations predating tracking.
    """
    viewed = find_image(tracking, spec.spec_id, section.layer, section.section_id)
    if viewed is not None:
        return viewed.capture_digest
    approved = find_layer(tracking, spec.spec_id, section.layer)
    if approved is None or approved.settings_digest != layer_fingerprint(
        source_digest,
        spec,
        section.layer,
        sections,
        stackup=stackup,
        width_results=width_results,
    ):
        return None
    return next(
        (
            digest
            for section_id, digest in approved.captures
            if section_id == section.section_id
        ),
        None,
    )


def layer_approval_current(
    tracking: ReviewTracking,
    source_digest: str,
    spec: "Specification",
    layer: str,
    sections: Sequence["Section"],
    *,
    stackup: Optional["Stackup"] = None,
    width_results: Sequence["WidthResult"] = (),
    capture_digests: Optional[Mapping[str, Optional[str]]] = None,
    required_section_ids: Optional[set[str]] = None,
) -> bool:
    """Return whether this layer's explicit approval still matches current settings and rows."""
    from .model import EXCLUSION_COVERED_STUB

    approved = find_layer(tracking, spec.spec_id, layer)
    if approved is None:
        return False
    layer_sections = tuple(
        section
        for section in sections
        if section.spec_id == spec.spec_id and section.layer == layer
    )
    if not layer_sections:
        return False
    if capture_digests is not None and any(
        section.section_id in capture_digests
        and capture_digests[section.section_id] is None
        for section in layer_sections
    ):
        # An unchecked optional image has no entry. An explicit failure must
        # remain pending even if its image was not part of the saved approval.
        return False
    digest = layer_fingerprint(
        source_digest,
        spec,
        layer,
        layer_sections,
        stackup=stackup,
        width_results=width_results,
    )
    if approved.settings_digest != digest:
        return False
    approved_ids = {section_id for section_id, _digest in approved.captures}
    current_ids = {section.section_id for section in layer_sections}
    if not approved_ids <= current_ids:
        return False
    for section_id, capture_digest in approved.captures:
        viewed = find_image(tracking, spec.spec_id, layer, section_id)
        if viewed is None or viewed.capture_digest != capture_digest:
            return False
        if (
            capture_digests is not None
            and section_id in capture_digests
            and capture_digests[section_id] != capture_digest
        ):
            return False
    required = {
        section.section_id
        for section in layer_sections
        if section.exclusion_reason != EXCLUSION_COVERED_STUB
    }
    if required_section_ids is not None:
        required = current_ids & required_section_ids
    return required <= approved_ids


def specification_layer_review_current(
    tracking: ReviewTracking,
    source_digest: str,
    spec: "Specification",
    sections: Sequence["Section"],
    *,
    stackup: Optional["Stackup"] = None,
    width_results: Sequence["WidthResult"] = (),
    capture_digests: Optional[Mapping[str, Optional[str]]] = None,
    required_section_ids: Optional[set[str]] = None,
) -> bool:
    """Return whether every matched included layer for this specification is currently approved."""
    layers = sorted(
        {section.layer for section in sections if section.spec_id == spec.spec_id}
    )
    if not layers:
        return False
    return all(
        layer_approval_current(
            tracking,
            source_digest,
            spec,
            layer,
            sections,
            stackup=stackup,
            width_results=width_results,
            capture_digests=capture_digests,
            required_section_ids=required_section_ids,
        )
        for layer in layers
    )


def put_image(tracking: ReviewTracking, record: ImageView) -> ReviewTracking:
    """Replace one logical image's latest event; do not consult the wall clock."""
    other_images = tuple(
        item
        for item in tracking.images
        if (item.spec_id, item.layer, item.section_id)
        != (record.spec_id, record.layer, record.section_id)
    )
    result = ReviewTracking(
        tuple(
            sorted(
                other_images + (record,),
                key=lambda item: (item.spec_id, item.layer, item.section_id),
            )
        ),
        tracking.layers,
    )
    validate_tracking(result)
    return result


def put_layer(tracking: ReviewTracking, record: LayerApproval) -> ReviewTracking:
    """Replace one signal layer's latest explicit approval event."""
    other_layers = tuple(
        item
        for item in tracking.layers
        if (item.spec_id, item.layer) != (record.spec_id, record.layer)
    )
    result = ReviewTracking(
        tracking.images,
        tuple(
            sorted(
                other_layers + (record,), key=lambda item: (item.spec_id, item.layer)
            )
        ),
    )
    validate_tracking(result)
    return result


def for_spec(tracking: ReviewTracking, spec_id: str) -> ReviewTracking:
    """Make an immutable specification-local edit buffer."""
    return ReviewTracking(
        tuple(record for record in tracking.images if record.spec_id == spec_id),
        tuple(record for record in tracking.layers if record.spec_id == spec_id),
    )


def merge_spec(
    tracking: ReviewTracking, replacement: ReviewTracking, spec_id: str
) -> ReviewTracking:
    """Commit just one accepted specification buffer, retaining other histories."""
    validate_tracking(replacement)
    if any(
        record.spec_id != spec_id
        for record in (*replacement.images, *replacement.layers)
    ):
        _invalid("A specification review buffer must not contain other specifications.")
    result = ReviewTracking(
        tuple(record for record in tracking.images if record.spec_id != spec_id)
        + replacement.images,
        tuple(record for record in tracking.layers if record.spec_id != spec_id)
        + replacement.layers,
    )
    validate_tracking(result)
    return result


def retain_specs(tracking: ReviewTracking, spec_ids: Iterable[str]) -> ReviewTracking:
    """Explicitly remove deleted specifications without altering remaining events."""
    retained = set(spec_ids)
    return ReviewTracking(
        tuple(record for record in tracking.images if record.spec_id in retained),
        tuple(record for record in tracking.layers if record.spec_id in retained),
    )
