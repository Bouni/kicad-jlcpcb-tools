"""Find matching declared layer constructions without equating electrical results.

The calculator deliberately leaves Er at provider model defaults, so equality of
its numeric inputs cannot establish equality of the selected materials. Compare
the construction itself, retaining dielectric sheets and conductor thicknesses.
Missing metadata and unsupported reference paths never establish a match.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .stackup_model import Stackup, StackupLayer, copper_layer_names, validate_stackup

_Token = tuple[object, ...]
_Path = tuple[_Token, ...]


@dataclass(frozen=True)
class _Construction:
    """Bind canonical copper identities to immutable physical construction rows."""

    stackup: Stackup
    names: tuple[str, ...]
    copper_indices: tuple[int, ...]


@dataclass(frozen=True)
class _Profile:
    """Normalize reflection toward the substrate or the interior of the board."""

    signature: _Token
    orientation: int


def _positive(value: str) -> Optional[Decimal]:
    """Retain exact finite decimal identity, never rounded binary floats."""
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def _construction(stackup: Optional[Stackup]) -> Optional[_Construction]:
    """Require a complete selected calculator construction before inferring links."""
    if stackup is None:
        return None
    try:
        validate_stackup(stackup)
    except ValueError:
        return None
    if not stackup.calculator_id or not stackup.layers:
        return None
    indices = tuple(
        index for index, row in enumerate(stackup.layers) if row.kind == "copper"
    )
    return _Construction(stackup, copper_layer_names(stackup), indices)


def _copper(row: StackupLayer) -> Optional[_Token]:
    """Compare nominal conductor thickness and any declared conductor material."""
    thickness = _positive(row.thickness_mm)
    if row.kind != "copper" or thickness is None:
        return None
    material = row.material.strip()
    if not material or material.casefold() == "copper":
        material = "Copper"
    return thickness, material


def _dielectric_path(
    construction: _Construction, signal: int, reference: int
) -> Optional[_Path]:
    """Read supported homogeneous sheets from the signal toward its reference."""
    start = construction.copper_indices[signal]
    stop = construction.copper_indices[reference]
    step = 1 if stop > start else -1
    sheets: list[_Token] = []
    constants: set[Decimal] = set()
    for index in range(start + step, stop, step):
        row = construction.stackup.layers[index]
        thickness = _positive(row.thickness_mm)
        constant = _positive(row.dielectric_constant)
        material = row.material.strip()
        if (
            row.kind not in ("prepreg", "core", "dielectric")
            or thickness is None
            or constant is None
            or not material
        ):
            return None
        sheets.append((row.kind, material, thickness, constant))
        constants.add(constant)
    if not sheets or len(constants) != 1:
        return None
    return tuple(sheets)


def _coating(construction: _Construction, signal: int) -> Optional[_Token]:
    """Compare explicit masks, or the shared provider assumption when both omit it."""
    position = construction.copper_indices[signal]
    indices = (
        range(position - 1, -1, -1)
        if signal == 0
        else range(position + 1, len(construction.stackup.layers))
    )
    masks: list[_Token] = []
    for index in indices:
        row = construction.stackup.layers[index]
        thickness = _positive(row.thickness_mm)
        constant = _positive(row.dielectric_constant)
        material = row.material.strip()
        if (
            row.kind != "soldermask"
            or thickness is None
            or constant is None
            or not material
        ):
            return None
        masks.append((material, thickness, constant))
    if not masks:
        # Catalog constructions omit side masks. The selected provider model
        # supplies one outer-layer coating table for the declared copper weight.
        return ("provider-coated-outer",)
    return "explicit-mask", tuple(masks)


def _profile(
    construction: _Construction, layer: str, references: tuple[str, ...]
) -> Optional[_Profile]:
    """Describe only reference geometries supported by the current calculator."""
    names = construction.names
    if (
        layer not in names
        or any(reference not in names for reference in references)
        or len(set(references)) != len(references)
    ):
        return None
    signal = names.index(layer)
    reference_indices = sorted(names.index(reference) for reference in references)
    outer = signal in (0, len(names) - 1)
    expected = [1 if signal == 0 else signal - 1] if outer else [signal - 1, signal + 1]
    if reference_indices != expected:
        return None
    stackup = construction.stackup
    copper = _copper(stackup.layers[construction.copper_indices[signal]])
    weight = _positive(stackup.outer_copper_oz if outer else stackup.inner_copper_oz)
    if copper is None or weight is None:
        return None
    paths: dict[int, tuple[_Path, _Token]] = {}
    for reference in reference_indices:
        path = _dielectric_path(construction, signal, reference)
        plane = _copper(stackup.layers[construction.copper_indices[reference]])
        if path is None or plane is None:
            return None
        paths[reference] = path, plane
    if outer:
        coating = _coating(construction, signal)
        if coating is None:
            return None
        return _Profile(
            ("outer", copper, weight, paths[reference_indices[0]], coating),
            1 if signal == 0 else -1,
        )
    above, below = reference_indices
    above_core = any(sheet[0] == "core" for sheet in paths[above][0])
    below_core = any(sheet[0] == "core" for sheet in paths[below][0])
    if above_core == below_core:
        return None
    substrate, overlay = (below, above) if below_core else (above, below)
    return _Profile(
        ("inner", copper, weight, paths[substrate], paths[overlay]),
        1 if below_core else -1,
    )


def equivalent_layer_groups(
    stackup: Optional[Stackup],
    layers: tuple[str, ...],
    references: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    """Group supplied signal layers with matching supported local constructions.

    References are absolute canonical layer names. Groups contain at least two
    members, ordered by the selected stackup's copper order. Construction labels
    are cosmetic; material, dielectric kind and exact dimensions are not. This
    does not verify actual gaps, coating coverage, or reference-plane continuity.
    """
    construction = _construction(stackup)
    if construction is None:
        return ()
    candidates = set(layers)
    groups: dict[_Token, list[str]] = {}
    for layer in construction.names:
        if layer not in candidates:
            continue
        profile = _profile(construction, layer, references.get(layer, ()))
        if profile is not None:
            groups.setdefault(profile.signature, []).append(layer)
    return tuple(tuple(group) for group in groups.values() if len(group) > 1)


def _map_offsets(
    layers: tuple[str, ...],
    source_layer: str,
    target_layer: str,
    reference_layers: tuple[str, ...],
    direction: int,
) -> tuple[str, ...]:
    """Map a validated relative reference pattern with an explicit orientation."""
    if (
        not layers
        or len(set(layers)) != len(layers)
        or source_layer not in layers
        or target_layer not in layers
        or not reference_layers
        or len(set(reference_layers)) != len(reference_layers)
        or source_layer in reference_layers
        or any(reference not in layers for reference in reference_layers)
    ):
        raise ValueError("Reference mapping requires distinct valid physical layers.")
    source = layers.index(source_layer)
    target = layers.index(target_layer)
    indices = sorted(
        target + direction * (layers.index(reference) - source)
        for reference in reference_layers
    )
    if any(not 0 <= index < len(layers) or index == target for index in indices):
        raise ValueError("The reference pattern does not fit the destination layer.")
    return tuple(layers[index] for index in indices)


def map_reference_layers(
    layers: tuple[str, ...],
    source_layer: str,
    target_layer: str,
    reference_layers: tuple[str, ...],
) -> tuple[str, ...]:
    """Translate reference offsets, reflecting globally mirrored signal positions.

    This utility does not establish physical equivalence. Automatic linking must
    use corresponding_references, which also verifies the local construction.
    """
    if source_layer not in layers or target_layer not in layers:
        raise ValueError("Signal layers must belong to the physical copper stack.")
    reflected = (
        layers.index(source_layer) + layers.index(target_layer) == len(layers) - 1
        and source_layer != target_layer
    )
    return _map_offsets(
        layers, source_layer, target_layer, reference_layers, -1 if reflected else 1
    )


def corresponding_references(
    stackup: Optional[Stackup],
    source_layer: str,
    target_layer: str,
    reference_layers: tuple[str, ...],
) -> Optional[tuple[str, ...]]:
    """Map references only when source and destination constructions are equivalent."""
    construction = _construction(stackup)
    if construction is None or target_layer not in construction.names:
        return None
    source = _profile(construction, source_layer, reference_layers)
    if source is None:
        return None
    names = construction.names
    target_index = names.index(target_layer)
    target_references = tuple(
        names[index]
        for index in (target_index - 1, target_index + 1)
        if 0 <= index < len(names)
    )
    target = _profile(construction, target_layer, target_references)
    if target is None or source.signature != target.signature:
        return None
    return _map_offsets(
        names,
        source_layer,
        target_layer,
        reference_layers,
        source.orientation * target.orientation,
    )
