"""Matrix clipboard contracts using production immutable snapshot records."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.variant_model_test_support import matrix as m, snapshot
from tests.variant_native_support import native

__all__ = ["snapshot"]


@pytest.mark.parametrize("destination", ["", "B"])
def test_cell_copy_captures_exact_source_and_stable_destination(
    snapshot: native.BoardVariantSnapshot, destination: str
) -> None:
    """Sorting/filtering after copying cannot redirect a paste to another Ref."""
    model = m.MatrixModel(snapshot)
    payload = model.copy_cell(
        model.row_for_component("id1"), model.column_for("A", "lcsc")
    )
    model.sort_by(0, descending=True)
    model.set_variant_order(("A", "B", ""))
    model.set_filter(differences_only=True)
    row = model.row_for_component("id1")
    assert model.cell_style(row, model.column_for("A", "lcsc")).different
    assert not model.cell_style(row, model.column_for("", "lcsc")).different
    assert model.copy_cell(row, model.column_for("", "lcsc")).plain_text == "C123"
    operation = model.plan_paste(
        payload, ("id2",), destination, destination_field="lcsc"
    )
    assert len(operation) == 1
    assert operation[0].target.component_id == "id2"
    assert operation[0].target.variant_name == destination
    assert operation[0].changes == (("lcsc", "C456"),)
    assert payload.plain_text == "C456"


@pytest.mark.parametrize(
    "field",
    [
        "ref",
        "footprint",
        "side",
        "pcb_angle",
        "params",
        "stock",
        "price",
        "correction",
        "type",
        "standard",
    ],
)
def test_read_only_cells_copy_text_but_never_mutate(
    snapshot: native.BoardVariantSnapshot, field: str
) -> None:
    """Readable clipboard output cannot smuggle a derived field into native data."""
    model = m.MatrixModel(snapshot)
    column = next(
        index for index, spec in enumerate(model.columns) if spec.key == field
    )
    payload = model.copy_cell(0, column)
    assert isinstance(payload.plain_text, str)
    for plan in (
        lambda: model.plan_paste(payload, ("id2",), "B"),
        lambda: model.plan_text_paste(payload.plain_text, "id1", "A", field),
        lambda: model.plan_copy_to_variants(payload, ("A",)),
    ):
        with pytest.raises(m.ClipboardError, match="[Rr]ead.only"):
            plan()


@pytest.mark.parametrize(
    "field,text,expected",
    [
        ("lcsc", "c123", "C123"),
        ("lcsc", "", ""),
        ("value", "  10k  ", "  10k  "),
        ("bom", "true", True),
        ("pos", "0", False),
        ("pop", "false", False),
        ("lcsc", "123", None),
        ("lcsc", "C123\nC456", None),
        ("value", "10k\t1%", None),
        ("value", "invalid\0value", None),
        ("pop", "maybe", None),
        ("pop", "DNP", None),
        ("pop", "\t0\n", None),
        ("bom", "\ntrue\n", None),
        ("price", "1.23", None),
    ],
)
def test_external_text_uses_explicit_single_cell_grammar(
    snapshot: native.BoardVariantSnapshot, field: str, text: str, expected: object
) -> None:
    """Preserve exact text and real bools; None marks an explicitly rejected input."""
    model = m.MatrixModel(snapshot)
    if expected is None:
        with pytest.raises(m.ClipboardError):
            model.plan_text_paste(text, "id1", "B", field)
    else:
        (edit,) = model.plan_text_paste(text, "id1", "B", field)
        assert edit.changes == ((field, expected),)
        assert type(edit.changes[0][1]) is type(expected)


def test_incompatible_shapes_fields_removed_targets_and_cross_board_rejected(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    """Reject a whole operation rather than truncate, broadcast, or rebase it."""
    model = m.MatrixModel(snapshot)
    payload = model.copy_block(("id1", "id2"), "A", fields=("lcsc",))
    for targets, variant, error in [
        (("id1",), "B", m.ClipboardError),
        (("id1", "id10"), "B", m.ClipboardError),
        (("id1", "id2"), "removed", native.StaleVariantTarget),
    ]:
        with pytest.raises(error):
            model.plan_paste(payload, targets, variant)
    single = model.copy_cell(
        model.row_for_component("id1"), model.column_for("A", "lcsc")
    )
    with pytest.raises(m.ClipboardError):
        model.plan_paste(single, ("id2",), "B", destination_field="value")
    with pytest.raises(m.ClipboardError, match="at least one destination"):
        model.plan_copy_to_variants(payload, ())
    snapshot = replace(snapshot, board_token="different-lifetime")
    with pytest.raises(m.ClipboardError, match="board"):
        m.MatrixModel(snapshot).plan_paste(single, ("id1",), "B")


@pytest.mark.parametrize(
    "value,inherited,remove_source",
    [
        ("C456", False, False),
        ("", False, False),
        ("C1", True, False),
        ("  μF\nline two\tvalue\r\nend  ", False, True),
    ],
)
def test_captured_native_values_survive_source_change_or_removal(
    value: str,
    inherited: bool,
    remove_source: bool,
) -> None:
    """Both typed cell and block copies capture values, never a live inheritance link."""
    from tests.variant_native_support import Board, Variant

    board = Board()
    field, native_field = ("value", "Value") if remove_source else ("lcsc", "LCSC")
    source = board.parts[0].AddVariant("A")
    if not inherited:
        source.SetFieldValue(native_field, value)
    source.SetDNP(True)
    fields = [field, "pop"]
    if not remove_source:
        source.SetFieldValue("Value", "  μF  ")
        fields.append("value")
    adapter = native.VariantNativeAdapter(board, "board-one", variant_factory=Variant)
    model = m.MatrixModel(adapter.snapshot())
    payload = (
        model.copy_cell(0, model.column_for("A", field))
        if remove_source
        else model.copy_block(("component-1",), "A", fields=fields)
    )
    fields.reverse()
    if remove_source:
        assert payload.plain_text == value
        board.names.remove("A")
    elif inherited:
        board.parts[0].fields["LCSC"] = "C789"
    else:
        source.SetFieldValue("LCSC", "C789")
    current = m.MatrixModel(adapter.snapshot())
    edits = (
        current.plan_paste(payload, ("component-1",), "B", field)
        if remove_source
        else current.plan_copy_to_variants(payload, ("B",))
    )
    result = adapter.apply_edits(edits)
    assert getattr(result.get("component-1", "B"), field) == value
    assert result.get("component-1", "").value == "10k"
    if remove_source:
        assert result.get("component-1", "B").pop
    else:
        target = result.get("component-1", "B")
        assert (
            not target.assignment.inherited
            and not target.pop
            and target.value == "  μF  "
        )
        assert result.get("component-1", "A").lcsc == "C789"
        assert result.get("component-1", "").lcsc == ("C789" if inherited else "C1")
    assert board.current == "A"
    with pytest.raises(native.StaleVariantTarget):
        adapter.apply_edits(edits)
