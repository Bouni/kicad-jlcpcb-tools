"""Compare routed widths with saved nominal calculator results.

These are dimensional comparisons, not solved or measured impedance checks.
The provider does not document an error bound, so no tolerance is invented from
its displayed precision. All functions are read-only and work offline.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Optional

from .model import Config, Specification, ValidationError
from .stackup_model import (
    WidthResult,
    calculation_fingerprint,
    find_width_result,
    validate_width_result,
)


@dataclass(frozen=True)
class WidthCheck:
    """One actual-width comparison and its nominal calculation summary."""

    status: str
    actual_width_nm: int
    target_width_nm: Optional[int]
    delta_nm: Optional[int]
    result_current: bool
    message: str
    result_status: str = "not_calculated"
    calculated_at_utc: str = ""
    provider: str = ""
    model: str = ""
    assumptions: tuple[str, ...] = ()


def _bounded_text(value: str, limit: int = 1024) -> str:
    """Keep saved model names and limitations safe for native and HTML labels."""
    text = "".join(
        f"\\u{ord(character):04x}"
        if ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
        else character
        for character in value[: limit + 1]
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_width_nm(value_nm: int) -> str:
    """Format exact millimetres and readable mils, including signed deltas."""
    if type(value_nm) is not int:
        raise TypeError("A width must be an integer number of nanometres.")
    with localcontext() as context:
        context.prec = max(96, len(str(abs(value_nm))) + 12)
        millimetres = format(Decimal(value_nm) / Decimal(1_000_000), "f")
        mils = format(Decimal(value_nm) / Decimal(25_400), ".2f")
    return f"{millimetres} mm ({mils} mil)"


def format_width_delta_nm(value_nm: int) -> str:
    """Make the actual-minus-nominal direction explicit without a percent limit."""
    return ("+" if value_nm > 0 else "") + format_width_nm(value_nm)


def _not_calculated(
    actual_width_nm: int,
    result: Optional[WidthResult],
    message: str,
    *,
    current: bool = False,
) -> WidthCheck:
    """Retain the old summary without comparing against a stale nominal width."""
    return WidthCheck(
        status="not_calculated",
        actual_width_nm=actual_width_nm,
        target_width_nm=result.target_width_nm if result is not None else None,
        delta_nm=None,
        result_current=current,
        message=message,
        result_status=result.status if result is not None else "not_calculated",
        calculated_at_utc=result.calculated_at_utc if result is not None else "",
        provider=result.provider if result is not None else "",
        model=_bounded_text(result.model, 256) if result is not None else "",
        assumptions=tuple(_bounded_text(item, 2048) for item in result.assumptions)
        if result is not None
        else (),
    )


def width_check(
    config: Config, spec: Specification, layer: str, actual_width_nm: int
) -> WidthCheck:
    """Compare one route with the current saved result for its class and layer.

    Return ``matches_nominal``, ``different``, or ``not_calculated``. Stale saved
    results remain available as historical summaries but never yield a delta or a
    current nominal match. No calculation, network, clock, or board edit occurs.
    """
    if type(actual_width_nm) is not int or actual_width_nm <= 0:
        raise ValidationError("The actual trace width must be a positive integer.")
    result = find_width_result(config.width_results, spec.spec_id, layer)
    if result is not None:
        try:
            validate_width_result(result)
        except ValidationError:
            return _not_calculated(
                actual_width_nm,
                None,
                "Not calculated: the saved calculation summary is invalid.",
            )
    if config.stackup is None:
        return _not_calculated(
            actual_width_nm, result, "Not calculated: no stackup is selected."
        )
    if result is None:
        return _not_calculated(
            actual_width_nm, None, "Not calculated for the selected stackup and layer."
        )
    try:
        input_digest = calculation_fingerprint(config.stackup, spec, layer)
    except ValidationError:
        return _not_calculated(
            actual_width_nm,
            result,
            "Not calculated: the current stackup or layer inputs are incomplete.",
        )
    if result.input_digest != input_digest:
        return _not_calculated(
            actual_width_nm,
            result,
            "Not calculated for current inputs; the saved result is historical.",
        )
    if result.status != "success" or result.target_width_nm is None:
        detail = result.message or {
            "pending": "A nominal-width calculation is pending.",
            "unavailable": "The nominal-width calculator is unavailable.",
            "unsupported": "The calculator does not support the declared construction.",
            "error": "The nominal-width calculation failed.",
        }.get(result.status, "No nominal width is available.")
        return _not_calculated(
            actual_width_nm, result, f"Not calculated: {detail}", current=True
        )
    delta = actual_width_nm - result.target_width_nm
    return WidthCheck(
        status="matches_nominal" if delta == 0 else "different",
        actual_width_nm=actual_width_nm,
        target_width_nm=result.target_width_nm,
        delta_nm=delta,
        result_current=True,
        message=(
            "Actual width matches the saved nominal width exactly."
            if delta == 0
            else f"Actual width differs from nominal by {format_width_delta_nm(delta)}."
        ),
        result_status=result.status,
        calculated_at_utc=result.calculated_at_utc,
        provider=result.provider,
        model=_bounded_text(result.model, 256),
        assumptions=tuple(_bounded_text(item, 2048) for item in result.assumptions),
    )
