"""Immutable models and strict persistence for controlled-impedance intent."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Optional

from .review_tracking import (
    ReviewTracking,
    review_tracking_from_dict,
    review_tracking_to_dict,
    validate_tracking,
)
from .stackup_model import (
    MAX_WIDTH_RESULTS,
    Stackup,
    WidthResult,
    stackup_from_dict,
    stackup_to_dict,
    validate_stackup,
    validate_width_result,
    width_result_from_dict,
    width_result_to_dict,
)

SCHEMA_VERSION = 6
KINDS = (
    "single_ended",
    "differential",
    "single_ended_coplanar",
    "differential_coplanar",
)
UNIT_NM = {"mm": 1_000_000, "mil": 25_400}
Point = tuple[int, int]
Bounds = tuple[int, int, int, int]
Contour = tuple[Point, ...]
CopperPolygon = tuple[Contour, ...]  # Outer boundary followed by any holes.


class ValidationError(ValueError):
    """A configuration or board snapshot cannot be used for documentation."""


@dataclass(frozen=True)
class LayerSettings:
    """Reference planes and fabrication gaps for one class-selected signal layer."""

    layer: str
    reference_layers: tuple[str, ...]
    spacing_nm: Optional[int] = None
    ground_gap_nm: Optional[int] = None


@dataclass(frozen=True)
class Specification:
    """The user's impedance requirement, which may generate several form rows."""

    spec_id: str
    label: str
    target_ohms: str
    kind: str
    net_class: str
    layer_settings: tuple[LayerSettings, ...] = ()
    excluded_layers: tuple[str, ...] = ()
    linked_layer_groups: tuple[tuple[str, ...], ...] = ()
    independent_layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Trace:
    """Centerline plus optional outward native copper proof for actual arcs."""

    trace_id: str
    layer: str
    net: str
    width_nm: int
    points: tuple[Point, ...]
    is_native_arc: bool = False
    copper_polygons: tuple[CopperPolygon, ...] = ()


# Machine-stable exclusion codes. Empty means the section is exportable.
EXCLUSION_COVERED_STUB = "covered_stub"
COPPER_FEATURE_SHAPES = ("circle", "oval", "rect", "roundrect", "polygon")


@dataclass(frozen=True)
class CopperFeature:
    """Pad or via copper flash used to detect covered stubs (nanometres)."""

    feature_id: str
    net: str
    layer: str
    kind: str
    shape: str
    center: Point
    size_x_nm: int
    size_y_nm: int
    rotation_deg: float = 0.0
    corner_radius_nm: int = 0
    polygons: tuple[CopperPolygon, ...] = ()


@dataclass(frozen=True)
class BoardSnapshot:
    """UI-independent board state with physical copper-layer order."""

    layers: tuple[str, ...]
    traces: tuple[Trace, ...]
    context_digest: str = ""
    net_classes: tuple[str, ...] = ()
    net_class_memberships: tuple[tuple[str, tuple[str, ...]], ...] = ()
    net_class_context_digest: str = ""
    net_class_error: str = ""
    differential_pairs: tuple[tuple[str, str], ...] = ()
    differential_pair_error: str = ""
    copper_features: tuple[CopperFeature, ...] = ()
    board_bounds: Optional[Bounds] = None


@dataclass(frozen=True)
class Section:
    """One form region owning complete native tracks and their copper bounds."""

    section_id: str
    spec_id: str
    layer: str
    width_nm: int
    traces: tuple[Trace, ...]
    bounds: Bounds
    net_names: tuple[str, ...]
    exclusion_reason: str = ""


@dataclass(frozen=True)
class Analysis:
    """Deterministically ordered candidates and their review revision."""

    sections: tuple[Section, ...]
    digest: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Board configuration, stored as a versioned JSON payload in project.db."""

    enabled: bool = False
    specifications: tuple[Specification, ...] = ()
    reviewed_digest: str = ""
    included_section_ids: tuple[str, ...] = ()
    review_tracking: ReviewTracking = ReviewTracking()
    stackup: Optional[Stackup] = None
    width_results: tuple[WidthResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return an explicitly versioned JSON-compatible object."""
        validate_storage_config(self)
        return {
            "schema_version": SCHEMA_VERSION,
            "enabled": self.enabled,
            "specifications": [
                _specification_dict(spec) for spec in self.specifications
            ],
            "reviewed_digest": self.reviewed_digest,
            "included_section_ids": list(self.included_section_ids),
            "review_tracking": review_tracking_to_dict(self.review_tracking),
            "stackup": stackup_to_dict(self.stackup)
            if self.stackup is not None
            else None,
            "width_results": [
                width_result_to_dict(result) for result in self.width_results
            ],
        }

    @classmethod
    def from_dict(cls, value: object) -> "Config":
        """Read current drafts, retaining strict validation for earlier schemas.

        Loading never writes. An explicit save replaces an earlier version.
        Retired width/net filters need an explicitly confirmed settings reset.
        """
        fields = {
            "schema_version",
            "enabled",
            "specifications",
            "reviewed_digest",
            "included_section_ids",
            "review_tracking",
            "stackup",
            "width_results",
        }
        version = value.get("schema_version") if isinstance(value, dict) else None
        if type(version) is int and version in (1, 2, 3):
            raise ValidationError(
                "These experimental impedance settings use retired selection formats. "
                "Reset settings to replace the existing configuration, "
                "then recreate specifications using net classes."
            )
        if type(version) is not int or version not in (4, 5, SCHEMA_VERSION):
            raise ValidationError("Unsupported impedance configuration schema version.")
        if version == 4:
            fields.update({"section_groups", "legacy_specifications"})
        data = _object(value, fields, "configuration")
        if type(data["enabled"]) is not bool:
            raise ValidationError("Enabled must be a boolean.")
        specifications = _array(data["specifications"], "specifications")
        if version == 4:
            if _array(data["legacy_specifications"], "legacy specifications"):
                raise ValidationError(
                    "This experimental configuration contains retired width or net filters. "
                    "Reset settings to replace the existing configuration before recreating net-class specifications."
                )
            _array(data["section_groups"], "section groups")
        width_results = _array(data["width_results"], "width results")
        if len(width_results) > MAX_WIDTH_RESULTS:
            raise ValidationError("Too many persisted width calculations.")
        result = cls(
            enabled=data["enabled"],
            specifications=tuple(
                _decode_specification(item, version=version) for item in specifications
            ),
            reviewed_digest=_string(
                data["reviewed_digest"], "reviewed digest", allow_empty=True
            ),
            included_section_ids=_strings(
                data["included_section_ids"], "included section IDs"
            ),
            review_tracking=review_tracking_from_dict(data["review_tracking"]),
            stackup=(
                stackup_from_dict(data["stackup"])
                if data["stackup"] is not None
                else None
            ),
            width_results=tuple(width_result_from_dict(item) for item in width_results),
        )
        if version < SCHEMA_VERSION:
            validate_config(result)
        else:
            validate_storage_config(result)
        if version == 4:
            from dataclasses import replace

            # Historical timestamps survive; changed row/width fingerprints
            # must not carry forward manufacturing authorization.
            result = replace(result, reviewed_digest="", included_section_ids=())
        return result


def _object(value: object, fields: set[str], label: str) -> dict[str, Any]:
    """Require an object with exactly the fields expected for its schema."""
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"Invalid {label}: missing or unknown fields.")
    return value


def _array(value: object, label: str) -> list[Any]:
    """Require a JSON array without coercing strings or other iterables."""
    if not isinstance(value, list):
        raise ValidationError(f"{label.capitalize()} must be an array.")
    return value


def _string(value: object, label: str, allow_empty: bool = False) -> str:
    """Require a string, retaining exact names and rejecting whitespace-only IDs."""
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{label.capitalize()} must be a nonempty string.")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    """Read a JSON array of nonempty strings."""
    return tuple(_string(item, label) for item in _array(value, label))


def _positive_integer(value: object, label: str) -> int:
    """Require a positive integer, explicitly excluding JSON booleans."""
    if type(value) is not int or value <= 0:
        raise ValidationError(
            f"{label.capitalize()} must be a positive integer in nanometres."
        )
    return value


def positive_decimal(value: object, label: str = "value") -> Decimal:
    """Parse positive finite decimal text without binary float coercion."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValidationError(f"{label.capitalize()} must be decimal text.")
    try:
        result = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValidationError(
            f"{label.capitalize()} must be a positive finite number."
        ) from exc
    if not result.is_finite() or result <= 0 or abs(result.adjusted()) > 30:
        raise ValidationError(f"{label.capitalize()} must be a positive finite number.")
    return result


def parse_length(text: str, unit: str = "mm") -> int:
    """Convert mm or mil text exactly to nanometres; reject sub-nanometre values."""
    if unit not in UNIT_NM:
        raise ValidationError("Length unit must be mm or mil.")
    value = positive_decimal(text, "length")
    with localcontext() as context:
        context.prec = 96
        nanometres = value * UNIT_NM[unit]
    if nanometres != nanometres.to_integral_value():
        raise ValidationError(
            "Length must be representable as a whole number of nanometres."
        )
    return int(nanometres)


def format_length(value_nm: int, unit: str = "mm") -> str:
    """Format a positive nanometre length without adding a unit suffix."""
    _positive_integer(value_nm, "length")
    if unit not in UNIT_NM:
        raise ValidationError("Length unit must be mm or mil.")
    with localcontext() as context:
        context.prec = 40
        result = format(Decimal(value_nm) / UNIT_NM[unit], "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _specification_dict(spec: Specification) -> dict[str, Any]:
    """Serialize the stable fields of one impedance specification."""
    return {
        "spec_id": spec.spec_id,
        "label": spec.label,
        "target_ohms": spec.target_ohms,
        "kind": spec.kind,
        "net_class": spec.net_class,
        "layer_settings": [
            {
                "layer": settings.layer,
                "reference_layers": list(settings.reference_layers),
                "spacing_nm": settings.spacing_nm,
                "ground_gap_nm": settings.ground_gap_nm,
            }
            for settings in spec.layer_settings
        ],
        "excluded_layers": list(spec.excluded_layers),
        "linked_layer_groups": [list(group) for group in spec.linked_layer_groups],
        "independent_layers": list(spec.independent_layers),
    }


def _decode_specification(value: object, *, version: int) -> Specification:
    """Decode the sole active selection model: all routed members of a net class."""
    fields = {
        "spec_id",
        "label",
        "target_ohms",
        "kind",
        "net_class",
        "layer_settings",
        "excluded_layers",
    }
    if version >= 6:
        fields.update({"linked_layer_groups", "independent_layers"})
    data = _object(value, fields, "specification")
    settings = []
    for item in _array(data["layer_settings"], "layer settings"):
        layer = _object(
            item,
            {"layer", "reference_layers", "spacing_nm", "ground_gap_nm"},
            "layer settings",
        )
        settings.append(
            LayerSettings(
                _string(layer["layer"], "signal layer"),
                _strings(layer["reference_layers"], "reference layers"),
                layer["spacing_nm"],
                layer["ground_gap_nm"],
            )
        )
    return Specification(
        spec_id=_string(data["spec_id"], "specification ID"),
        label=_string(data["label"], "label", allow_empty=True),
        target_ohms=_string(
            data["target_ohms"], "target impedance", allow_empty=version >= 6
        ),
        kind=_string(data["kind"], "kind"),
        net_class=_string(data["net_class"], "net class"),
        layer_settings=tuple(settings),
        excluded_layers=_strings(data["excluded_layers"], "excluded layers"),
        linked_layer_groups=(
            tuple(
                _strings(group, "linked layer group")
                for group in _array(data["linked_layer_groups"], "linked layer groups")
            )
            if version >= 6
            else ()
        ),
        independent_layers=(
            _strings(data["independent_layers"], "independent layers")
            if version >= 6
            else ()
        ),
    )


def _validate_names(values: tuple[str, ...], label: str) -> None:
    """Validate immutable collections of unique nonempty names."""
    if not isinstance(values, tuple):
        raise ValidationError(f"{label.capitalize()} must be a tuple.")
    for value in values:
        _string(value, label)
    if len(values) != len(set(values)):
        raise ValidationError(f"{label.capitalize()} must not contain duplicates.")


def resolved_layer_settings(spec: Specification, layer: str) -> LayerSettings:
    """Return explicit per-signal-layer references and fabrication gaps."""
    for settings in spec.layer_settings:
        if settings.layer == layer:
            return settings
    raise ValidationError(
        f"{spec.label or spec.spec_id}: signal layer {layer!r} has no configured settings; "
        "configure its reference planes and gaps before including it."
    )


def _validate_layer_settings(
    settings: LayerSettings,
    kind: str,
    layers: Optional[tuple[str, ...]],
    *,
    require_complete: bool,
) -> None:
    """Validate supplied local fields and optionally require complete dimensions."""
    if not isinstance(settings, LayerSettings):
        raise ValidationError("Every layer settings entry must be LayerSettings.")
    _string(settings.layer, "signal layer")
    _validate_names(settings.reference_layers, "reference layers")
    if require_complete and not settings.reference_layers:
        raise ValidationError(
            "Every signal layer requires at least one reference layer."
        )
    if settings.layer in settings.reference_layers:
        raise ValidationError("A signal layer cannot also be its own reference layer.")
    if layers is not None:
        if settings.layer not in layers:
            raise ValidationError(
                f"Signal layer {settings.layer!r} is not an enabled copper layer."
            )
        if any(layer not in layers for layer in settings.reference_layers):
            raise ValidationError("Reference layers must be enabled copper layers.")
    _validate_gaps(
        kind,
        settings.spacing_nm,
        settings.ground_gap_nm,
        require_complete=require_complete,
    )


def _validate_gaps(
    kind: str,
    spacing: Optional[int],
    ground_gap: Optional[int],
    *,
    require_complete: bool,
) -> None:
    """Check supplied physical gaps and those required by the impedance kind."""
    if spacing is not None:
        _positive_integer(spacing, "spacing")
    if require_complete and kind.startswith("differential") and spacing is None:
        raise ValidationError(
            "Differential impedance requires a positive pair spacing."
        )
    if ground_gap is not None:
        _positive_integer(ground_gap, "ground gap")
    if require_complete and kind.endswith("_coplanar") and ground_gap is None:
        raise ValidationError("Coplanar impedance requires a positive ground gap.")


def _validate_netclass_spec(
    spec: Specification,
    layers: Optional[tuple[str, ...]],
    *,
    require_complete: bool,
) -> None:
    """Keep configured and excluded layers coherent even in unfinished drafts."""
    _string(spec.net_class, "net class")
    _validate_names(spec.excluded_layers, "excluded layers")
    if not isinstance(spec.layer_settings, tuple) or (
        not spec.layer_settings and (require_complete or not spec.excluded_layers)
    ):
        raise ValidationError(
            "Net-class specifications require at least one configured signal layer."
        )
    seen: set[str] = set()
    for settings in spec.layer_settings:
        _validate_layer_settings(
            settings, spec.kind, layers, require_complete=require_complete
        )
        if settings.layer in seen:
            raise ValidationError("Signal layer settings must not contain duplicates.")
        seen.add(settings.layer)
    if seen.intersection(spec.excluded_layers):
        raise ValidationError("A signal layer cannot be both configured and excluded.")
    if layers is not None and any(
        layer not in layers for layer in spec.excluded_layers
    ):
        raise ValidationError("Excluded layers must be enabled copper layers.")
    _validate_layer_links(spec, seen)


def _validate_layer_links(spec: Specification, configured: set[str]) -> None:
    """Keep synchronization preferences distinct from immutable physical intent."""
    if not isinstance(spec.linked_layer_groups, tuple):
        raise ValidationError("Linked layer groups must be a tuple.")
    linked: set[str] = set()
    for group in spec.linked_layer_groups:
        _validate_names(group, "linked layer group")
        if len(group) < 2:
            raise ValidationError(
                "Linked layer groups require at least two signal layers."
            )
        if not set(group) <= configured:
            raise ValidationError("Linked layers must be configured and not excluded.")
        if linked.intersection(group):
            raise ValidationError(
                "A signal layer cannot belong to multiple linked groups."
            )
        linked.update(group)
    _validate_names(spec.independent_layers, "independent layers")
    if not set(spec.independent_layers) <= configured | set(spec.excluded_layers):
        raise ValidationError("Independent layers must be configured or excluded.")
    if linked.intersection(spec.independent_layers):
        raise ValidationError("A signal layer cannot be both linked and independent.")


def validate_config(config: Config, layers: Optional[tuple[str, ...]] = None) -> None:
    """Require complete fabrication intent before analysis or report approval."""
    _validate_config(config, layers, require_complete=True)


def validate_storage_config(
    config: Config, layers: Optional[tuple[str, ...]] = None
) -> None:
    """Allow unfinished fields while rejecting malformed supplied draft values."""
    _validate_config(config, layers, require_complete=False)


def _validate_config(
    config: Config,
    layers: Optional[tuple[str, ...]],
    *,
    require_complete: bool,
) -> None:
    """Share structural validation across draft storage and complete intent."""
    if not isinstance(config, Config) or type(config.enabled) is not bool:
        raise ValidationError("Invalid impedance configuration or enabled flag.")
    if not isinstance(config.specifications, tuple):
        raise ValidationError("Specifications must be a tuple.")
    # Enabled settings can be saved before the first specification is complete.
    # Export readiness belongs to validate_review, never to storage validation;
    # silently disabling a draft would omit a user-required manufacturing report.
    _string(config.reviewed_digest, "reviewed digest", allow_empty=True)
    _validate_names(config.included_section_ids, "included section IDs")
    validate_tracking(config.review_tracking)
    if layers is not None:
        _validate_names(layers, "board copper layers")
    if config.stackup is not None:
        validate_stackup(config.stackup)
        if layers is not None and config.stackup.layer_count != len(layers):
            raise ValidationError(
                "The selected stackup does not match the PCB's enabled copper-layer count; "
                "select a compatible stackup and review the layers again."
            )
    if (
        not isinstance(config.width_results, tuple)
        or len(config.width_results) > MAX_WIDTH_RESULTS
    ):
        raise ValidationError("Width results must be a bounded immutable collection.")
    result_keys: set[tuple[str, str, str]] = set()
    for result in config.width_results:
        validate_width_result(result)
        key = (result.spec_id, result.layer, result.input_digest)
        if key in result_keys:
            raise ValidationError(
                "Width results cannot repeat a specification/layer/input fingerprint."
            )
        result_keys.add(key)
    seen_ids: set[str] = set()
    for spec in config.specifications:
        if not isinstance(spec, Specification):
            raise ValidationError("Every specification must be a Specification.")
        _string(spec.spec_id, "specification ID")
        _string(spec.label, "label", allow_empty=True)
        if require_complete or spec.target_ohms != "":
            positive_decimal(spec.target_ohms, "target impedance")
        if spec.spec_id in seen_ids:
            raise ValidationError("Specification IDs must be unique.")
        seen_ids.add(spec.spec_id)
        if spec.kind not in KINDS:
            raise ValidationError(f"Unsupported impedance kind: {spec.kind!r}.")
        _validate_netclass_spec(spec, layers, require_complete=require_complete)
