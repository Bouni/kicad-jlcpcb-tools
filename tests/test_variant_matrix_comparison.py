"""Exact engineering matches, known field differences, and physical corrections."""

from collections.abc import Iterator
from dataclasses import replace
from types import ModuleType
from typing import Any, Optional

import pytest

from tests.variant_model_test_support import (
    Snapshot,
    State,
    assignment,
    catalog,
    changed,
    coordinates,
    matching_snapshot,
    matrix as m,
    snapshot,
)
from tests.variant_native_support import native
from tests.wx_harness import load_siblings

__all__ = ["matching_snapshot", "snapshot"]


@pytest.fixture
def correction_data() -> Iterator[ModuleType]:
    """Load correction values with their package-relative imports isolated."""
    with load_siblings(
        "variant_matrix_correction_tests", ("correction_data",), {}
    ) as loaded:
        yield loaded["correction_data"]


def assert_blocks(model: m.MatrixModel, component: str, expected: set[str]) -> None:
    """All cells in an affected variant share its outline, including unknown cells."""
    row = model.row_for_component(component)
    for col, spec in enumerate(model.columns):
        assert model.cell_style(row, col).variant_different is (
            spec.variant in expected
        )


def test_named_matches_provenance_and_catalog_capture(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    source = replace(
        snapshot,
        variants=tuple(
            native.VariantDefinition(name, label)
            for name, label in (("", "Default"), ("A", "Economy"), ("B", "Premium"))
        ),
    )
    metadata = catalog(source, {"A": {"stock": 100}, "B": {"status": "pending"}})
    model = m.MatrixModel(source, metadata)
    for variant, matches in (("", ("",)), ("A", ("A", "B")), ("B", ("A", "B"))):
        cell = coordinates(model, variant, "lcsc")
        assert model.cell_style(*cell).matches == matches
        assert model.cell_style(*cell).different is bool(variant)
        text = model.cell_details(*cell)
        assert (
            "Matches: Economy, Premium"
            if variant
            else "Matches: no other confirmed variants"
        ) in text
    assert model.cell_style(*coordinates(model, "B", "stock")).status == "pending"
    for variant, provenance in (("A", "inherited"), ("B", "explicit")):
        style = model.cell_style(*coordinates(model, variant, "lcsc", "id10"))
        assert style.provenance == provenance and not style.different
        assert style.matches == ("", "A", "B")
    for variant in model.variants:
        for field in ("value", "lcsc", "bom"):
            text = model.cell_details(*coordinates(model, variant, field, "id10"))
            assert "Matches: Default, Economy, Premium" in text
            assert "Assembly match group" not in text
    metadata[("id1", "A")] = replace(metadata[("id1", "A")], stock=999)
    assert model.get_value(*coordinates(model, "A", "stock")) == 100


@pytest.mark.parametrize(
    "field,value",
    [
        (None, None),
        ("value", "4.7k"),
        ("value", ""),
        ("lcsc", "C456"),
        ("lcsc", ""),
        ("bom", False),
        ("pos", False),
        ("pop", False),
    ],
)
def test_known_native_change_and_restore(
    matching_snapshot: native.BoardVariantSnapshot, field: Optional[str], value: Any
) -> None:
    source = (
        changed(matching_snapshot, component="id1", variants=("B",), **{field: value})
        if field
        else matching_snapshot
    )
    model = m.MatrixModel(source)
    for descending in (True, False):
        model.sort_by(0, descending)
        assert_blocks(model, "id1", {"B"} if field else set())
        assert_blocks(model, "id2", set())
    if field:
        for variant, matches in (("", ("", "A")), ("A", ("", "A")), ("B", ("B",))):
            style = model.cell_style(*coordinates(model, variant, field))
            assert style.matches == matches and style.different is (variant == "B")
            text = model.cell_details(*coordinates(model, variant, field))
            assert (
                "Matches: no other confirmed variants"
                if variant == "B"
                else "Matches: Default, A"
            ) in text
        unknown = model.cell_style(*coordinates(model, "B", "params"))
        assert (
            unknown.status == "missing"
            and not unknown.different
            and unknown.variant_different
        )
    model.set_filter(differences_only=True)
    assert [row.component_id for row in model.rows] == (["id1"] if field else [])
    assert_blocks(m.MatrixModel(matching_snapshot), "id1", set())
    assert_blocks(m.MatrixModel(source), "id1", {"B"} if field else set())


@pytest.mark.parametrize(
    "field,base,other",
    [
        ("params", "10k ±1%", "10k ±5%"),
        ("type", "Basic", "Extended"),
        ("standard", False, True),
        ("stock", 100, 200),
        ("price", "0.00000011", "0.00000012"),
    ],
)
def test_catalog_changes_outline_without_changing_engineering_filter(
    matching_snapshot: native.BoardVariantSnapshot, field: str, base: Any, other: Any
) -> None:
    facts = catalog(
        matching_snapshot, {"": {field: base}, "A": {field: other}, "B": {field: base}}
    )
    model = m.MatrixModel(matching_snapshot, facts)
    for component in matching_snapshot.inventory:
        assert_blocks(model, component, {"A"})
        assert model.cell_style(*coordinates(model, "A", field, component)).different
    model.set_filter(differences_only=True)
    assert model.rows == ()


@pytest.mark.parametrize("variants", [("",), ("A",), ("A", "B")])
@pytest.mark.parametrize("status", ["invalid", "conflict"])
def test_invalid_native_assignment_remains_neutral_and_visible(
    matching_snapshot: native.BoardVariantSnapshot,
    variants: tuple[str, ...],
    status: str,
) -> None:
    source = changed(
        matching_snapshot,
        variants=variants,
        assignment=assignment(status),
    )
    model = m.MatrixModel(source)
    for component in source.inventory:
        assert_blocks(model, component, set())
        for variant in variants:
            cell = coordinates(model, variant, "value", component)
            assert not model.cell_style(*cell).matches
            assert "Matches:" not in model.cell_details(*cell)
    model.set_filter(differences_only=True)
    assert len(model.rows) == len(source.inventory)
    with pytest.raises(m.ClipboardError, match="invalid|conflict|unavailable"):
        model.copy_block(("id1",), variants[0])


@pytest.mark.parametrize("pop,outline", [(True, set()), (False, {"A"})])
def test_invalid_assignment_keeps_independent_pop_difference(
    matching_snapshot: native.BoardVariantSnapshot, pop: bool, outline: set[str]
) -> None:
    model = m.MatrixModel(
        changed(
            matching_snapshot,
            variants=("A",),
            assignment=assignment("invalid"),
            pop=pop,
        )
    )
    for component in matching_snapshot.inventory:
        assert_blocks(model, component, outline)


@pytest.mark.parametrize("status", ["pending", "missing", "unknown", "error"])
def test_catalog_unknown_baseline_never_compares_peers(
    matching_snapshot: native.BoardVariantSnapshot, status: str
) -> None:
    facts = catalog(
        matching_snapshot,
        {
            "": {"standard": False},
            "A": {"standard": None, "status": status, "price": "1"},
            "B": {"standard": False, "price": "2"},
        },
    )
    model = m.MatrixModel(matching_snapshot, facts)
    for component in matching_snapshot.inventory:
        assert_blocks(model, component, set())


def std_model(
    base_pop: bool,
    variant_pop: bool,
    base: Optional[bool] = True,
    other: Optional[bool] = False,
) -> m.MatrixModel:
    source = Snapshot(
        tuple(
            State("id1", "R1", variant, pop=variant_pop if variant == "A" else base_pop)
            for variant in ("", "A", "B")
        )
    )
    return m.MatrixModel(
        source,
        catalog(
            source,
            {"": {"standard": base}, "A": {"standard": other}, "B": {"standard": base}},
        ),
    )


@pytest.mark.parametrize(
    "base_pop,other_pop,std_diff,pop_diff,base",
    [
        (True, True, True, False, True),
        (True, True, True, False, False),
        (True, False, False, True, True),
        (False, True, False, True, True),
        (False, False, False, False, True),
    ],
)
def test_std_requires_both_populated_and_keeps_pop_difference(
    base_pop: bool, other_pop: bool, std_diff: bool, pop_diff: bool, base: bool
) -> None:
    model = std_model(base_pop, other_pop, base, not base)
    cell = coordinates(model, "A", "standard")
    style = model.cell_style(*cell)
    assert style.different is std_diff and style.symbol == ("≠" if std_diff else "")
    assert style.status == "known" and model.get_value(*cell) is (not base)
    assert model.get_display(*cell) == ("—" if base else "✓")
    assert ("Different from Default" in model.cell_details(*cell)) is std_diff
    assert model.cell_style(*coordinates(model, "A", "pop")).different is pop_diff
    assert_blocks(model, "id1", {"A"} if std_diff or pop_diff else set())


@pytest.mark.parametrize("pop", [False, True])
def test_std_is_catalog_only_and_unknown_values_remain_neutral(pop: bool) -> None:
    model = std_model(pop, pop)
    assert "Matches: Default, A, B" in model.cell_details(
        *coordinates(model, "A", "standard")
    )
    model.set_filter(differences_only=True)
    assert model.rows == ()
    for base, other in ((None, False), (True, None)):
        assert_blocks(std_model(True, True, base, other), "id1", set())


def test_unpopulated_default_does_not_create_named_std_baseline() -> None:
    initial = std_model(False, True)
    source = changed(initial.snapshot, variants=("A", "B"), pop=True)
    model = m.MatrixModel(source, initial._metadata)
    for variant in ("A", "B"):
        assert not model.cell_style(*coordinates(model, variant, "standard")).different
        assert model.cell_style(*coordinates(model, variant, "pop")).different


@pytest.mark.parametrize(
    "field,value", [("bom", False), ("pos", False), ("value", "22k"), ("lcsc", "C456")]
)
def test_ignoring_std_preserves_other_fields(field: str, value: Any) -> None:
    initial = std_model(False, False)
    source = changed(initial.snapshot, variants=("A",), **{field: value})
    model = m.MatrixModel(
        source,
        catalog(
            source,
            {"": {"standard": True}, "A": {"standard": False}, "B": {"standard": True}},
        ),
    )
    assert not model.cell_style(*coordinates(model, "A", "standard")).different
    assert model.cell_style(*coordinates(model, "A", field)).different
    assert_blocks(model, "id1", {"A"})


@pytest.mark.parametrize("output", ["", "A", "B"])
@pytest.mark.parametrize("source", ["val", "lcsc"])
def test_exact_output_correction_details_and_copy(
    matching_snapshot: native.BoardVariantSnapshot,
    correction_data: ModuleType,
    output: str,
    source: str,
) -> None:
    rule = correction_data.Correction("R1", "90", (1e-9, -2e-9))
    correction = m.CorrectionState(rule.rotation, *rule.offset, source, 239.999999998)
    captured = {"id1": correction}
    model = m.MatrixModel(
        changed(matching_snapshot, side="bottom", pcb_angle=30.000000002),
        corrections=captured,
        correction_variant=output,
    )
    captured["id1"] = replace(correction, rotation=180)
    cell = coordinates(model, None, "correction")
    assert model.get_value(*cell) is correction
    details = model.cell_details(*cell)
    for text in ("90", "1e-09", "-2e-09", "239.999999998", source):
        assert text in details
    assert f"Output variant: {output or 'Default'}" in details
    assert "exact LCSC rule for this variant overrides shared pattern rules" in details
    assert "Pattern fallback uses Default reference, Value and package" in details
    assert "shared by all variants" not in details
    assert "Matches:" not in details
    assert_blocks(model, "id1", set())
    assert "Same Value, LCSC, BOM/POS/POP." in model.cell_details(
        *coordinates(model, "A", "value")
    )
    payload = model.copy_cell(*cell)
    assert payload.plain_text == "90°, 1e-09/-2e-09"
    assert all(
        not model.cell_style(cell[0], col).different
        for col in range(len(model.columns))
    )
    model.set_filter(differences_only=True)
    assert model.rows == ()


def test_deleted_output_does_not_present_a_supplied_correction_as_current(
    matching_snapshot: native.BoardVariantSnapshot,
) -> None:
    """A retained output choice cannot borrow Default's otherwise valid angle."""
    model = m.MatrixModel(
        matching_snapshot,
        corrections={"id1": m.CorrectionState(90, source="lcsc", final_angle=180)},
        correction_variant="deleted",
    )
    cell = coordinates(model, None, "correction")
    assert model.get_display(*cell) == "Unavailable"
    assert model.cell_style(*cell).symbol == "?"
    assert "final CPL angle:" not in model.cell_details(*cell)


@pytest.mark.parametrize(
    "values,status",
    [
        (None, "pending"),
        ({"status": "error"}, "error"),
        ({"final_angle": float("nan")}, "invalid"),
    ],
)
def test_unavailable_corrections_preserve_matches(
    matching_snapshot: native.BoardVariantSnapshot,
    values: Optional[dict[str, Any]],
    status: str,
) -> None:
    model = m.MatrixModel(
        changed(matching_snapshot, pcb_angle=31.0),
        corrections=None if values is None else {"id1": m.CorrectionState(**values)},
    )
    cell = coordinates(model, None, "correction")
    style = model.cell_style(*cell)
    assert style.status == status and style.symbol == "?"
    assert model.get_display(*cell) == "Unavailable"
    text = model.cell_details(*cell)
    assert f"Status: {status}" in text
    assert "final CPL angle:" not in text and "Rotation 0.0°" not in text
    for variant in model.variants:
        assert model.cell_style(*coordinates(model, variant, "value")).matches == (
            "",
            "A",
            "B",
        )
    model.set_filter(differences_only=True)
    assert model.rows == ()


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize(
    "descending,expected",
    [(False, ["id10", "id2", "id1"]), (True, ["id1", "id2", "id10"])],
)
def test_correction_sort_preserves_precision_and_unknowns_last(
    matching_snapshot: native.BoardVariantSnapshot,
    unavailable: bool,
    descending: bool,
    expected: list[str],
) -> None:
    corrections = {
        "id1": m.CorrectionState(90, offset_x=1.00000002e-9, final_angle=90),
        "id2": m.CorrectionState(
            90, offset_x=1.00000001e-9, offset_y=1e-9, final_angle=90
        ),
        "id10": m.CorrectionState(90, offset_x=1.00000001e-9, final_angle=90),
    }
    if unavailable:
        corrections.update(
            id1=m.CorrectionState(status="error"),
            id2=m.CorrectionState(final_angle=float("nan")),
            id10=m.CorrectionState(rotation=-90, final_angle=270),
        )
    model = m.MatrixModel(matching_snapshot, corrections=corrections)
    model.sort_by(model.column_for(None, "correction"), descending)
    assert [row.component_id for row in model.rows] == (
        ["id10", "id1", "id2"] if unavailable else expected
    )
