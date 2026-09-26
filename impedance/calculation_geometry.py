"""Translate a selected construction into a conservative JLCPCB cross-section.

This is parameter mapping, not an impedance solver.  Unsupported constructions
must remain explicit rather than being reduced to an arbitrary dielectric average.
All dimensions returned for JLCPCB's calculator are in mil.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .model import Specification, resolved_layer_settings
from .stackup_model import Stackup, StackupLayer, copper_layer_names

MM_PER_MIL = Decimal("0.0254")
NM_PER_MIL = Decimal("25400")


class UnsupportedCalculation(ValueError):
    """The available provider model cannot safely represent this construction."""


@dataclass(frozen=True)
class CrossSection:
    """Anonymous physical inputs; no board, class, net, or file names are included."""

    model: str
    copper_weight_oz: float
    outer: bool
    values: tuple[tuple[str, float], ...]
    assumptions: tuple[str, ...]
    nominal_copper_mil: float


def positive_number(
    value: object, label: str, *, maximum: Decimal = Decimal("10000")
) -> Decimal:
    """Require a finite, physically bounded positive scalar without bool coercion."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise UnsupportedCalculation(f"Missing or invalid {label}.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise UnsupportedCalculation(f"Missing or invalid {label}.") from exc
    if not result.is_finite() or not Decimal(0) < result <= maximum:
        raise UnsupportedCalculation(f"Missing or invalid {label}.")
    return result


def _dielectric(
    layers: tuple[StackupLayer, ...], start: int, stop: int
) -> tuple[Decimal, Decimal]:
    """Combine only contiguous dielectric sheets having the same dielectric constant."""
    sheets = layers[min(start, stop) + 1 : max(start, stop)]
    if not sheets or any(
        item.kind not in ("prepreg", "core", "dielectric") for item in sheets
    ):
        raise UnsupportedCalculation(
            "This reference path contains intervening copper or unsupported materials. "
            "Use adjacent reference layers or review the construction in JLCPCB's calculator."
        )
    thickness = Decimal(0)
    constants = set()
    for item in sheets:
        thickness += positive_number(item.thickness_mm, "dielectric thickness")
        constants.add(positive_number(item.dielectric_constant, "dielectric constant"))
    if len(constants) != 1:
        raise UnsupportedCalculation(
            "This reference path contains different dielectric constants. "
            "A multilayer dielectric model is required; no averaged estimate was substituted."
        )
    return thickness / MM_PER_MIL, constants.pop()


def _core_below(stackup: Stackup, signal: int, copper_indices: list[int]) -> bool:
    """Locate the substrate-facing side of an inner etched conductor."""
    layers = stackup.layers
    position = copper_indices[signal]
    below = layers[position + 1 : copper_indices[signal + 1]]
    above = layers[copper_indices[signal - 1] + 1 : position]
    below_core = any(item.kind == "core" for item in below)
    above_core = any(item.kind == "core" for item in above)
    if below_core != above_core:
        return below_core
    raise UnsupportedCalculation(
        "This inner conductor's substrate-facing side is ambiguous. "
        "Review standalone-foil or mass-laminated constructions in JLCPCB's calculator."
    )


def cross_section(stackup: Stackup, spec: Specification, layer: str) -> CrossSection:
    """Map known physical layers and declared gaps, refusing unsupported geometry."""
    if not stackup.calculator_id:
        raise UnsupportedCalculation(
            "This orderable stackup has no matching JLCPCB calculator construction. "
            "Select a calculator-supported stackup or review it with JLCPCB."
        )
    if spec.kind not in (
        "single_ended",
        "differential",
        "single_ended_coplanar",
        "differential_coplanar",
    ):
        raise UnsupportedCalculation(
            "This impedance type has no supported JLCPCB model."
        )
    canonical_layers = copper_layer_names(stackup)
    if layer not in canonical_layers:
        raise UnsupportedCalculation("The signal layer is not in the selected stackup.")
    copper_indices = [
        index for index, item in enumerate(stackup.layers) if item.kind == "copper"
    ]
    if len(copper_indices) != stackup.layer_count:
        raise UnsupportedCalculation(
            "The selected stackup has an incomplete copper-layer construction."
        )
    signal = canonical_layers.index(layer)
    nominal_copper = (
        positive_number(
            stackup.layers[copper_indices[signal]].thickness_mm,
            "signal copper thickness",
        )
        / MM_PER_MIL
    )
    settings = resolved_layer_settings(spec, layer)
    if any(name not in canonical_layers for name in settings.reference_layers):
        raise UnsupportedCalculation(
            "A reference layer is not in the selected stackup."
        )
    references = sorted(
        canonical_layers.index(name) for name in settings.reference_layers
    )
    outer = signal in (0, stackup.layer_count - 1)
    if signal in references or len(set(references)) != len(references):
        raise UnsupportedCalculation(
            "Choose distinct reference planes, separate from the signal layer."
        )
    if outer and (len(references) != 1 or abs(references[0] - signal) != 1):
        raise UnsupportedCalculation(
            "Outer-layer calculation currently requires its adjacent reference plane."
        )
    if not outer and references != [signal - 1, signal + 1]:
        raise UnsupportedCalculation(
            "Inner-layer calculation currently requires the adjacent reference planes above and below."
        )
    target = positive_number(spec.target_ohms, "target impedance")
    differential = spec.kind.startswith("differential")
    coplanar = spec.kind.endswith("_coplanar")
    limits = (50, 150) if differential else (20, 90)
    if not limits[0] <= target <= limits[1]:
        raise UnsupportedCalculation(
            f"JLCPCB's calculator accepts targets from {limits[0]} to {limits[1]} ohms for this type."
        )
    values: dict[str, Any] = {"Zo": float(target)}
    assumptions = [
        "Reference layers are assumed to be continuous ground planes.",
        "Declared spacing and coplanar gap are held fixed; their actual copper geometry is not verified.",
    ]
    if outer:
        height, constant = _dielectric(
            stackup.layers, copper_indices[signal], copper_indices[references[0]]
        )
        values.update(H1=float(height), Er1=float(constant))
        assumptions.append(
            "Outer signal copper is assumed to be covered by soldermask."
        )
        model = (
            "DiffCoatedCoplanarWaveguideWithLowerGnd1B"
            if differential and coplanar
            else "CoatedCoplanarWaveguideWithLowerGnd1B"
            if coplanar
            else "DiffEdgeCoupledCoatedMicrostrip1B"
            if differential
            else "CoatedMicrostrip1B"
        )
    else:
        above = _dielectric(
            stackup.layers, copper_indices[signal - 1], copper_indices[signal]
        )
        below = _dielectric(
            stackup.layers, copper_indices[signal], copper_indices[signal + 1]
        )
        substrate, overlay = (
            (below, above)
            if _core_below(stackup, signal, copper_indices)
            else (above, below)
        )
        # The provider's H2 includes the conductor, while H1 is substrate-only.
        values.update(
            H1=float(substrate[0]),
            Er1=float(substrate[1]),
            H2=float(overlay[0] + nominal_copper),
            Er2=float(overlay[1]),
        )
        model = (
            "DiffOffsetCoplanarWaveguide1B1A"
            if differential and coplanar
            else "OffsetCoplanarWaveguide1B1A"
            if coplanar
            else "DiffOffsetStripline1B1A"
            if differential
            else "OffsetStripline1B1A"
        )
    if differential:
        values["S1"] = float(
            positive_number(
                settings.spacing_nm,
                "differential spacing",
                maximum=Decimal("254000000"),
            )
            / NM_PER_MIL
        )
        if not 2.5 <= values["S1"] <= 100:
            raise UnsupportedCalculation(
                "JLCPCB's calculator accepts differential spacing from 2.5 to 100 mil."
            )
    if coplanar:
        values["D1"] = float(
            positive_number(
                settings.ground_gap_nm,
                "coplanar ground gap",
                maximum=Decimal("254000000"),
            )
            / NM_PER_MIL
        )
        if not 2.5 <= values["D1"] <= 80:
            raise UnsupportedCalculation(
                "JLCPCB's calculator accepts coplanar ground gaps from 2.5 to 80 mil."
            )
    weight = stackup.outer_copper_oz if outer else stackup.inner_copper_oz
    return CrossSection(
        model,
        float(positive_number(weight, "copper weight")),
        outer,
        tuple(sorted(values.items())),
        tuple(assumptions),
        float(nominal_copper),
    )
