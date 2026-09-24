"""Match declared local constructions before synchronizing layer settings."""

from dataclasses import replace
from typing import Optional

import pytest

from impedance.layer_equivalence import (
    corresponding_references,
    equivalent_layer_groups,
    map_reference_layers,
)
from impedance.stackup_model import Stackup, StackupLayer, copper_layer_names


def _stackup() -> Stackup:
    """Describe mirrored outer and two distinct mirrored inner environments."""
    return Stackup(
        "symmetric-six",
        "Symmetric six-layer construction",
        6,
        "1.2",
        "1",
        "0.5",
        calculator_id="six-calculator",
        layers=(
            StackupLayer("L1", "copper", "0.035"),
            StackupLayer("Top prepreg", "prepreg", "0.12", "2116", "4.2"),
            StackupLayer("L2", "copper", "0.015"),
            StackupLayer("Top core", "core", "0.3", "FR4", "4.5"),
            StackupLayer("L3", "copper", "0.015"),
            StackupLayer("Centre prepreg", "prepreg", "0.2", "3313", "4.2"),
            StackupLayer("L4", "copper", "0.015"),
            StackupLayer("Bottom core", "core", "0.3", "FR4", "4.5"),
            StackupLayer("L5", "copper", "0.015"),
            StackupLayer("Bottom prepreg", "prepreg", "0.12", "2116", "4.2"),
            StackupLayer("L6", "copper", "0.035"),
        ),
    )


def _references(stackup: Stackup) -> dict[str, tuple[str, ...]]:
    """Select physically adjacent planes without using implementation helpers."""
    names = copper_layer_names(stackup)
    return {
        name: tuple(
            names[index]
            for index in (position - 1, position + 1)
            if 0 <= index < len(names)
        )
        for position, name in enumerate(names)
    }


def _change_layer(stackup: Stackup, index: int, **changes: str) -> Stackup:
    """Change exactly one declared construction row."""
    rows = list(stackup.layers)
    rows[index] = replace(rows[index], **changes)
    return replace(stackup, layers=tuple(rows))


def test_symmetric_construction_groups_outer_and_mirrored_inner_layers() -> None:
    """Group by local geometry, keeping canonical physical ordering."""
    stackup = _stackup()
    assert equivalent_layer_groups(
        stackup, tuple(reversed(copper_layer_names(stackup))), _references(stackup)
    ) == (("F.Cu", "B.Cu"), ("In1.Cu", "In4.Cu"), ("In2.Cu", "In3.Cu"))


def test_only_supplied_routed_candidates_participate() -> None:
    """Saved unused layers cannot join a current synchronization group."""
    stackup = _stackup()
    assert equivalent_layer_groups(
        stackup, ("F.Cu", "In1.Cu", "B.Cu"), _references(stackup)
    ) == (("F.Cu", "B.Cu"),)


def test_repeated_inner_construction_matches_without_global_stackup_mirroring() -> None:
    """Identical translated local reference environments may share settings."""
    original = _stackup()

    def copper(number: int) -> StackupLayer:
        """Keep each repeated conductor's physical identity distinct."""
        return StackupLayer(f"L{number}", "copper", "0.015")

    prepreg = StackupLayer("Prepreg", "prepreg", "0.1", "2116", "4.2")
    core = StackupLayer("Core", "core", "0.3", "FR4", "4.5")
    stackup = replace(
        original,
        layer_count=8,
        layers=(
            copper(1),
            prepreg,
            copper(2),
            core,
            copper(3),
            prepreg,
            copper(4),
            core,
            copper(5),
            prepreg,
            copper(6),
            core,
            copper(7),
            prepreg,
            copper(8),
        ),
    )
    assert equivalent_layer_groups(
        stackup, ("In2.Cu", "In4.Cu"), _references(stackup)
    ) == (("In2.Cu", "In4.Cu"),)


@pytest.mark.parametrize(
    ("index", "changes"),
    [
        (1, {"thickness_mm": "0.13"}),
        (1, {"thickness_mm": "0.120000000000000000000000000001"}),
        (1, {"material": "Different glass"}),
        (1, {"dielectric_constant": "4.3"}),
        (1, {"kind": "dielectric"}),
        (0, {"thickness_mm": "0.034"}),
        (2, {"thickness_mm": "0.014"}),
    ],
)
def test_same_reference_count_does_not_hide_physical_asymmetry(
    index: int, changes: dict[str, str]
) -> None:
    """Compare exact declared materials and dimensions, not solver-rounded floats."""
    stackup = _change_layer(_stackup(), index, **changes)
    assert (
        equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), _references(stackup)) == ()
    )


def test_decimal_spellings_and_construction_display_names_do_not_change_match() -> None:
    """Equivalent numeric metadata compares physically, while labels stay cosmetic."""
    stackup = _change_layer(
        _stackup(),
        9,
        name="Renamed bottom prepreg",
        thickness_mm="0.1200",
        dielectric_constant="4.20",
    )
    assert equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), _references(stackup)) == (
        ("F.Cu", "B.Cu"),
    )


@pytest.mark.parametrize(
    "missing", ["stackup", "construction", "calculator", "material", "dk", "thickness"]
)
def test_unknown_or_unsupported_metadata_does_not_establish_equivalence(
    missing: str,
) -> None:
    """Unknown physical geometry is not evidence for automatic synchronization."""
    stackup: Optional[Stackup] = _stackup()
    references = _references(stackup)
    if missing == "stackup":
        stackup = None
    elif missing == "construction":
        stackup = replace(stackup, layers=())
    elif missing == "calculator":
        stackup = replace(stackup, calculator_id="")
    else:
        field = {
            "material": "material",
            "dk": "dielectric_constant",
            "thickness": "thickness_mm",
        }[missing]
        stackup = _change_layer(stackup, 1, **{field: ""})
    assert equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), references) == ()


@pytest.mark.parametrize(
    "references", [("In2.Cu",), ("F.Cu",), (), ("In1.Cu", "In2.Cu")]
)
def test_unsupported_outer_references_do_not_establish_equivalence(
    references: tuple[str, ...],
) -> None:
    """Nonadjacent, self, absent and multiple outer planes remain independent."""
    stackup = _stackup()
    configured = _references(stackup)
    configured["F.Cu"] = references
    assert equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), configured) == ()


def test_inner_layers_require_two_adjacent_references_and_unambiguous_core_side() -> (
    None
):
    """Unsupported inner geometry cannot become a linked default by coincidence."""
    stackup = _stackup()
    configured = _references(stackup)
    configured["In1.Cu"] = ("F.Cu",)
    assert equivalent_layer_groups(stackup, ("In1.Cu", "In4.Cu"), configured) == ()
    ambiguous = _change_layer(stackup, 1, kind="core")
    assert (
        equivalent_layer_groups(ambiguous, ("In1.Cu", "In4.Cu"), _references(ambiguous))
        == ()
    )


def test_mixed_dielectric_constants_in_one_reference_path_are_unsupported() -> None:
    """Do not reduce an unsupported multilayer dielectric to a similar height."""
    original = _stackup()
    rows = original.layers
    stackup = replace(
        original,
        layers=rows[:1]
        + (
            StackupLayer("Sheet A", "prepreg", "0.06", "2116", "4.2"),
            StackupLayer("Sheet B", "prepreg", "0.06", "2116", "4.3"),
        )
        + rows[2:],
    )
    assert (
        equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), _references(stackup)) == ()
    )


def test_outer_and_inner_layers_never_share_an_equivalence_group() -> None:
    """A similar height does not equate coated microstrip and inner stripline."""
    stackup = _stackup()
    assert (
        equivalent_layer_groups(stackup, ("F.Cu", "In1.Cu"), _references(stackup)) == ()
    )


@pytest.mark.parametrize("different", [False, True])
def test_explicit_side_coatings_must_match(different: bool) -> None:
    """Known differing masks override the common provider-coating assumption."""
    original = _stackup()
    stackup = replace(
        original,
        layers=(StackupLayer("F.Mask", "soldermask", "0.015", "Mask", "3.8"),)
        + original.layers
        + (
            StackupLayer(
                "B.Mask", "soldermask", "0.02" if different else "0.015", "Mask", "3.8"
            ),
        ),
    )
    assert equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), _references(stackup)) == (
        () if different else (("F.Cu", "B.Cu"),)
    )


def test_one_known_coating_and_one_unknown_side_do_not_match() -> None:
    """Do not compare explicit mask geometry with an implicit default."""
    original = _stackup()
    stackup = replace(
        original,
        layers=(StackupLayer("F.Mask", "soldermask", "0.015", "Mask", "3.8"),)
        + original.layers,
    )
    assert (
        equivalent_layer_groups(stackup, ("F.Cu", "B.Cu"), _references(stackup)) == ()
    )


@pytest.mark.parametrize(
    ("source", "target", "references", "expected"),
    [
        ("F.Cu", "B.Cu", ("In1.Cu",), ("In4.Cu",)),
        ("B.Cu", "F.Cu", ("In4.Cu",), ("In1.Cu",)),
        ("In2.Cu", "In3.Cu", ("In1.Cu", "In3.Cu"), ("In2.Cu", "In4.Cu")),
    ],
)
def test_corresponding_references_follow_matched_physical_orientation(
    source: str, target: str, references: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    """Map reference identities instead of copying literal names across layers."""
    stackup = _stackup()
    assert corresponding_references(stackup, source, target, references) == expected
    assert (
        map_reference_layers(copper_layer_names(stackup), source, target, references)
        == expected
    )


def test_corresponding_references_refuses_unknown_or_asymmetric_construction() -> None:
    """Mapping for automatic links also verifies the destination construction."""
    stackup = _change_layer(_stackup(), 9, thickness_mm="0.13")
    assert corresponding_references(stackup, "F.Cu", "B.Cu", ("In1.Cu",)) is None
    assert corresponding_references(None, "F.Cu", "B.Cu", ("In1.Cu",)) is None


@pytest.mark.parametrize(
    "references", [("F.Cu",), ("Missing.Cu",), ("In1.Cu", "In1.Cu")]
)
def test_reference_mapping_rejects_invalid_source_choices(
    references: tuple[str, ...],
) -> None:
    """Invalid source selections must not silently produce valid destination planes."""
    with pytest.raises(ValueError):
        map_reference_layers(copper_layer_names(_stackup()), "F.Cu", "B.Cu", references)
