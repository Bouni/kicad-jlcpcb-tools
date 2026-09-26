"""Immutable vendor stackup snapshots and nominal trace-width calculations.

The selected construction is board intent, not a live catalog pointer.  A later
catalog refresh must never replace it without an explicit user selection.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, NoReturn, Optional

if TYPE_CHECKING:
    from .model import Specification

CALCULATION_REVISION = 1
_MAX_LEGACY_SOURCE_BYTES = 2_000_000
MAX_WIDTH_RESULTS = 2_000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = {"copper", "core", "prepreg", "dielectric", "soldermask", "other"}
_RESULT_STATES = {"pending", "success", "unavailable", "error", "unsupported"}


@dataclass(frozen=True)
class StackupLayer:
    """One physical construction layer, in top-to-bottom vendor order."""

    name: str
    kind: str
    thickness_mm: str
    material: str = ""
    dielectric_constant: str = ""


@dataclass(frozen=True)
class Stackup:
    """A named, explicitly selected JLCPCB physical construction."""

    stackup_id: str
    name: str
    layer_count: int
    thickness_mm: str
    outer_copper_oz: str
    inner_copper_oz: str
    preferred: bool = False
    charge_status: str = "unknown"
    layers: tuple[StackupLayer, ...] = ()
    calculator_id: str = ""
    source_url: str = "https://jlcpcb.com/impedance"
    retrieved_at_utc: str = ""


@dataclass(frozen=True)
class WidthResult:
    """A nominal width result for exact declared inputs, never measured impedance."""

    spec_id: str
    layer: str
    input_digest: str
    status: str
    target_width_nm: Optional[int] = None
    calculated_at_utc: str = ""
    provider: str = "JLCPCB"
    message: str = ""
    model: str = ""
    assumptions: tuple[str, ...] = ()
    calculation_digest: str = ""


def _invalid(message: str) -> NoReturn:
    """Raise the shared validation error without a model import cycle."""
    from .model import ValidationError

    raise ValidationError(message)


def _text(value: object, label: str, *, allow_empty: bool = False) -> None:
    """Require bounded exact text, never coerce opaque provider identifiers."""
    if (
        not isinstance(value, str)
        or len(value) > 10_000
        or (not allow_empty and not value.strip())
    ):
        _invalid(f"{label} must be {'bounded' if allow_empty else 'nonempty'} text.")


def _decimal(value: object, label: str, *, allow_empty: bool = False) -> str:
    """Validate positive finite decimal text and return its normalized value."""
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not value or len(value) > 64:
        _invalid(f"{label} must be positive decimal text.")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError):
        _invalid(f"{label} must be positive decimal text.")
    if not number.is_finite() or number <= 0 or abs(number.adjusted()) > 30:
        _invalid(f"{label} must be a positive finite decimal.")
    with localcontext() as context:
        context.prec = 96
        return str(number.normalize())


def _legacy_json(value: object, label: str) -> dict[str, Any]:
    """Read an old archive once during conversion; never retain or rewrite it."""
    if not isinstance(value, str):
        _invalid(f"{label} is too large or is not JSON text.")
    try:
        if len(value.encode("utf-8")) > _MAX_LEGACY_SOURCE_BYTES:
            _invalid(f"{label} is too large or is not JSON text.")
        result = json.loads(value)
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _invalid(f"{label} must contain finite JSON.")
    if not isinstance(result, dict):
        _invalid(f"{label} must contain a JSON object.")
    return result


def _timestamp(value: object, label: str) -> None:
    """Allow genuinely missing history, otherwise require exact UTC timestamps."""
    from .review_tracking import format_timestamp

    if not isinstance(value, str):
        _invalid(f"{label} must be UTC timestamp text.")
    if value:
        format_timestamp(value)


def validate_stackup(stackup: Stackup) -> None:
    """Validate an immutable selection without requiring a network connection."""
    if not isinstance(stackup, Stackup):
        _invalid("Selected stackup must be a Stackup record.")
    _text(stackup.stackup_id, "Stackup ID")
    _text(stackup.name, "Stackup name")
    if type(stackup.layer_count) is not int or not 2 <= stackup.layer_count <= 64:
        _invalid("Stackup copper-layer count must be between 2 and 64.")
    _decimal(stackup.thickness_mm, "Stackup thickness")
    _decimal(stackup.outer_copper_oz, "Outer copper weight")
    _decimal(stackup.inner_copper_oz, "Inner copper weight", allow_empty=True)
    if type(stackup.preferred) is not bool:
        _invalid("Stackup preferred status must be a boolean.")
    if not isinstance(stackup.charge_status, str) or stackup.charge_status not in {
        "additional",
        "none",
        "unknown",
    }:
        _invalid("Stackup charge status must be additional, none, or unknown.")
    if not isinstance(stackup.layers, tuple) or len(stackup.layers) > 256:
        _invalid("Stackup construction must contain at most 256 immutable layers.")
    copper_count = 0
    for layer in stackup.layers:
        if not isinstance(layer, StackupLayer):
            _invalid("Each construction layer must be a StackupLayer record.")
        _text(layer.name, "Construction layer name")
        if not isinstance(layer.kind, str) or layer.kind not in _KINDS:
            _invalid("Unknown stackup construction layer kind.")
        _decimal(layer.thickness_mm, "Construction layer thickness")
        _text(layer.material, "Construction material", allow_empty=True)
        _decimal(layer.dielectric_constant, "Dielectric constant", allow_empty=True)
        copper_count += layer.kind == "copper"
    if stackup.layers and copper_count != stackup.layer_count:
        _invalid("Stackup construction copper count differs from its layer count.")
    _text(stackup.calculator_id, "Calculator stackup ID", allow_empty=True)
    _text(stackup.source_url, "Stackup source URL")
    _timestamp(stackup.retrieved_at_utc, "Stackup retrieval time")


def validate_width_result(result: WidthResult) -> None:
    """Validate a bounded nominal result and its exact-input fingerprints."""
    if not isinstance(result, WidthResult):
        _invalid("Every width result must be a WidthResult record.")
    _text(result.spec_id, "Calculation specification ID")
    _text(result.layer, "Calculation signal layer")
    if not isinstance(result.input_digest, str) or not _DIGEST.fullmatch(
        result.input_digest
    ):
        _invalid("Width calculation input fingerprint must be a SHA-256 digest.")
    if not isinstance(result.status, str) or result.status not in _RESULT_STATES:
        _invalid("Unknown width calculation status.")
    if result.status == "success":
        if type(result.target_width_nm) is not int or result.target_width_nm <= 0:
            _invalid("Successful calculations require a positive target width.")
    elif result.target_width_nm is not None:
        _invalid("Unsuccessful calculations cannot contain a target width.")
    _timestamp(result.calculated_at_utc, "Calculation time")
    _text(result.provider, "Calculation provider")
    _text(result.message, "Calculation message", allow_empty=True)
    _text(result.model, "Calculation model", allow_empty=result.status != "success")
    if len(result.model) > 256:
        _invalid("Calculation model exceeds its supported length.")
    if (
        not isinstance(result.assumptions, tuple)
        or len(result.assumptions) > 64
        or any(
            not isinstance(item, str) or len(item) > 2048 for item in result.assumptions
        )
    ):
        _invalid("Calculation assumptions must be a bounded immutable text collection.")
    if not isinstance(result.calculation_digest, str) or (
        result.calculation_digest and not _DIGEST.fullmatch(result.calculation_digest)
    ):
        _invalid("Calculation fingerprint must be a SHA-256 digest.")
    if result.status == "success" and (
        not result.calculated_at_utc or not result.calculation_digest
    ):
        _invalid(
            "Successful calculations require their calculation time and fingerprint."
        )


def stackup_to_dict(stackup: Stackup) -> dict[str, Any]:
    """Serialize the frozen construction, not a pointer into the live catalog."""
    validate_stackup(stackup)
    return {**asdict(stackup), "layers": [asdict(layer) for layer in stackup.layers]}


def stackup_from_dict(value: object) -> Stackup:
    """Decode exactly the selected-stackup schema."""
    fields = set(Stackup.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) not in (
        fields,
        fields | {"source_payload_json"},
    ):
        _invalid("Invalid selected stackup: missing or unknown fields.")
    layers = value["layers"]
    if not isinstance(layers, list) or len(layers) > 256:
        _invalid("Stackup construction must be a bounded array.")
    decoded = []
    for layer in layers:
        if not isinstance(layer, dict) or set(layer) != set(
            StackupLayer.__dataclass_fields__
        ):
            _invalid("Invalid construction layer: missing or unknown fields.")
        decoded.append(StackupLayer(**layer))
    result = Stackup(
        **{**{key: value[key] for key in fields}, "layers": tuple(decoded)}
    )
    validate_stackup(result)
    return result


def width_result_to_dict(result: WidthResult) -> dict[str, Any]:
    """Serialize the result summary without restamping or archiving provider data."""
    validate_width_result(result)
    return {**asdict(result), "assumptions": list(result.assumptions)}


def width_result_from_dict(value: object) -> WidthResult:
    """Read current summaries or reduce an older source archive without a write."""
    fields = set(WidthResult.__dataclass_fields__)
    core = fields - {"model", "assumptions", "calculation_digest"}
    if not isinstance(value, dict) or set(value) not in (
        fields,
        core | {"response_json"},
    ):
        _invalid("Invalid width result: missing or unknown fields.")
    if "response_json" in value:
        source = _legacy_json(value["response_json"], "Saved calculation")
        summary: dict[str, Any] = {key: value[key] for key in core}
        if value["status"] == "success":
            returned = source.get("result")
            if source.get("units") != "mil" or not isinstance(returned, dict):
                _invalid("Saved calculation has no supported width units or result.")
            dimensions = _numeric_scalars(returned)
            if any(isinstance(number, bool) for number in dimensions.values()):
                _invalid("Saved calculation dimensions must not contain booleans.")
            width = dimensions.get("W1")
            if isinstance(width, bool) or width is None or Decimal(width) <= 0:
                _invalid("Saved calculation has no positive base trace width.")
            if "W2" in dimensions and Decimal(dimensions["W2"]) <= 0:
                _invalid("Saved calculation has no positive upper trace width.")
            with localcontext() as context:
                context.prec = 160
                expected = int(
                    (Decimal(width) * 25_400).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                )
            if (
                type(value["target_width_nm"]) is not int
                or value["target_width_nm"] != expected
            ):
                _invalid(
                    "Stored nominal width differs from the recorded provider result."
                )
            summary.update(
                model=source.get("model"),
                assumptions=source.get("assumptions"),
                calculation_digest=solver_fingerprint(
                    source.get("parameters"),
                    returned,
                    source.get("construction", {}),
                    source.get("adapter_revision"),
                    legacy_construction=True,
                ),
            )
        value = {"model": "", "assumptions": [], "calculation_digest": "", **summary}
    if not isinstance(value["assumptions"], list) or len(value["assumptions"]) > 64:
        _invalid("Calculation assumptions must be a bounded array.")
    result = WidthResult(**{**value, "assumptions": tuple(value["assumptions"])})
    validate_width_result(result)
    return result


def _digest(value: object) -> str:
    """Hash stable numeric intent, excluding query times and opaque request IDs."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def copper_layer_names(stackup: Stackup) -> tuple[str, ...]:
    """Map physical copper order to canonical KiCad layer names."""
    validate_stackup(stackup)
    return (
        ("F.Cu",)
        + tuple(f"In{index}.Cu" for index in range(1, stackup.layer_count - 1))
        + ("B.Cu",)
    )


def stackup_fingerprint(stackup: Optional[Stackup]) -> str:
    """Bind numeric construction and identity, not catalog sorting or access times."""
    if stackup is None:
        return _digest(None)
    validate_stackup(stackup)
    return _digest(
        {
            "stackup_id": stackup.stackup_id,
            "calculator_id": stackup.calculator_id,
            "layer_count": stackup.layer_count,
            "thickness_mm": _decimal(stackup.thickness_mm, "Stackup thickness"),
            "outer_copper_oz": _decimal(stackup.outer_copper_oz, "Outer copper weight"),
            "inner_copper_oz": _decimal(
                stackup.inner_copper_oz, "Inner copper weight", allow_empty=True
            ),
            "layers": [
                {
                    "kind": layer.kind,
                    "thickness_mm": _decimal(layer.thickness_mm, "Layer thickness"),
                    "material": layer.material,
                    "dielectric_constant": _decimal(
                        layer.dielectric_constant,
                        "Dielectric constant",
                        allow_empty=True,
                    ),
                }
                for layer in stackup.layers
            ],
        }
    )


def calculation_fingerprint(stackup: Stackup, spec: "Specification", layer: str) -> str:
    """Identify the precise declared geometry used for nominal-width solving."""
    from .model import resolved_layer_settings

    settings = resolved_layer_settings(spec, layer)
    if layer not in copper_layer_names(stackup):
        _invalid("Calculation signal layer is not present in the selected stackup.")
    return _digest(
        {
            "revision": CALCULATION_REVISION,
            "stackup": stackup_fingerprint(stackup),
            "spec_id": spec.spec_id,
            "net_class": spec.net_class,
            "kind": spec.kind,
            "target_ohms": _decimal(spec.target_ohms, "Target impedance"),
            "settings": {
                **asdict(settings),
                "reference_layers": sorted(settings.reference_layers),
            },
        }
    )


def _numeric_scalars(values: object) -> dict[str, Any]:
    """Canonicalize bounded flat solver inputs without retaining their source."""
    if not isinstance(values, dict) or len(values) > 64:
        _invalid("Calculation dimensions must be a bounded numerical object.")
    result = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            _invalid("Calculation dimensions have an invalid field name.")
        if type(value) is bool:
            result[name] = value
            continue
        if not isinstance(value, (str, int, float)) or len(str(value)) > 128:
            _invalid("Calculation dimensions must be finite numerical scalars.")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            _invalid("Calculation dimensions must be finite numerical scalars.")
        if not number.is_finite() or abs(number.adjusted()) > 30:
            _invalid("Calculation dimensions must contain finite bounded numbers.")
        text = format(number, "f") if number else "0"
        result[name] = text.rstrip("0").rstrip(".") if "." in text else text
    return result


def solver_fingerprint(
    parameters: object,
    result: object,
    construction: object,
    revision: object = 1,
    *,
    legacy_construction: bool = False,
) -> str:
    """Bind effective solver values, not raw provider records or retrieval clocks."""
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        _invalid("Calculation adapter revision must be a positive integer.")
    if not parameters or not result:
        _invalid("Calculation inputs and returned dimensions must not be empty.")
    try:
        physical = _numeric_scalars(
            {} if legacy_construction and construction is None else construction
        )
    except ValueError:
        if not legacy_construction:
            raise
        # Older records permitted arbitrary optional construction metadata.
        # Preserve change detection, not that retired archive or its schema.
        physical = {"legacy_digest": _digest(construction)}
    return _digest(
        {
            "parameters": _numeric_scalars(parameters),
            "result": _numeric_scalars(result),
            "construction": physical,
            "revision": revision,
        }
    )


def width_results_fingerprint(results: Sequence[WidthResult]) -> str:
    """Bind nominal results and solver inputs without retaining a source archive."""
    for result in results:
        validate_width_result(result)
    return _digest(
        [
            {
                "spec_id": result.spec_id,
                "layer": result.layer,
                "input_digest": result.input_digest,
                "status": result.status,
                "target_width_nm": result.target_width_nm,
                "provider": result.provider,
                "model": result.model,
                "assumptions": result.assumptions,
                "calculation_digest": result.calculation_digest,
            }
            for result in sorted(
                results, key=lambda item: (item.spec_id, item.layer, item.input_digest)
            )
        ]
    )


def find_width_result(
    results: Sequence[WidthResult],
    spec_id: str,
    layer: str,
    input_digest: Optional[str] = None,
) -> Optional[WidthResult]:
    """Return a saved summary for a specification layer.

    When ``input_digest`` is provided, only a result for that exact declared
    geometry matches. Callers comparing against the active stackup must pass the
    current fingerprint so historical results for other stackups are not treated
    as current.
    """
    matches = tuple(
        result
        for result in results
        if result.spec_id == spec_id and result.layer == layer
    )
    if not matches:
        return None
    if input_digest is None:
        return matches[0]
    for result in matches:
        if result.input_digest == input_digest:
            return result
    return None


def merge_width_results(
    existing: Sequence[WidthResult],
    incoming: Sequence[WidthResult],
) -> tuple[WidthResult, ...]:
    """Retain historical input digests while replacing exact geometry matches.

    Nominal widths are cached by ``input_digest``, which already binds stackup,
    impedance kind, target resistance, signal layer, reference (ground) layers,
    and declared gaps. Switching stackups must not discard the previous stackup's
    successful results.
    """
    for result in existing:
        validate_width_result(result)
    for result in incoming:
        validate_width_result(result)
    by_digest = {result.input_digest: result for result in existing}
    for result in incoming:
        by_digest[result.input_digest] = result
    merged = tuple(
        sorted(
            by_digest.values(),
            key=lambda item: (item.spec_id, item.layer, item.input_digest),
        )
    )
    if len(merged) <= MAX_WIDTH_RESULTS:
        return merged
    protected = {result.input_digest for result in incoming}
    keep = [result for result in merged if result.input_digest in protected]
    extras = sorted(
        (result for result in merged if result.input_digest not in protected),
        key=lambda item: item.calculated_at_utc or "",
        reverse=True,
    )
    room = MAX_WIDTH_RESULTS - len(keep)
    if room > 0:
        keep.extend(extras[:room])
    return tuple(
        sorted(keep, key=lambda item: (item.spec_id, item.layer, item.input_digest))
    )
