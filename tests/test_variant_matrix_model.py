"""Matrix model contracts using production immutable snapshot records."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

import pytest

from tests.variant_model_test_support import (
    Snapshot,
    State,
    assignment,
    catalog,
    changed as evolve,
    coordinates,
    matrix as m,
    snapshot,
)
from tests.variant_native_support import Board, Variant, native

__all__ = ["snapshot"]


@pytest.mark.parametrize("raw,text,included", [(None, "None", True), (1, "1", False)])
def test_native_boundary_normalizes_values_and_rejects_duplicate_edit_targets(
    raw: object, text: str, included: bool
) -> None:
    """Models consume native strings/booleans; complete write validation stays native."""
    board = Board()
    part = board.parts[0]
    original = part.GetFieldValueForVariant
    part.GetFieldValueForVariant = (
        lambda name, field: raw if field == "Value" else original(name, field)
    )
    for name in (
        "GetDNPForVariant",
        "GetExcludedFromBOMForVariant",
        "GetExcludedFromPosFilesForVariant",
    ):
        setattr(part, name, lambda _variant: raw)
    adapter = native.VariantNativeAdapter(board, "board", variant_factory=Variant)
    snapshot = adapter.snapshot()
    state = snapshot.get("component-1", "A")
    assert state.value == text
    assert all(getattr(state, field) is included for field in ("bom", "pos", "pop"))
    model = m.MatrixModel(snapshot)
    for field in m.EDITABLE_FIELDS:
        assert model.cell_style(0, model.column_for("A", field)).status == "known"
    payload = model.copy_block(("component-1",), "A")
    edits = model.plan_copy_to_variants(payload, ("B", "B"))
    with pytest.raises(native.NativeVariantError, match="Combine changes"):
        adapter.apply_edits(edits)
    assert adapter.snapshot() == snapshot and not part.variants


def test_schema_and_shared_row_map(snapshot: native.BoardVariantSnapshot) -> None:
    """Keep five physical columns and ten visible fields in every variant."""
    model = m.MatrixModel(snapshot)
    assert model.variants == ("", "A", "B")
    assert [column.key for column in model.columns[:5]] == [
        "ref",
        "footprint",
        "side",
        "pcb_angle",
        "correction",
    ]
    assert len(model.columns) == 35
    fields = [
        "value",
        "params",
        "lcsc",
        "bom",
        "pos",
        "pop",
        "type",
        "standard",
        "stock",
        "price",
    ]
    for variant in model.variants:
        assert [
            column.key for column in model.columns if column.variant == variant
        ] == fields
    model.sort_by(0)
    assert [row.reference for row in model.rows] == ["R1", "R2", "R10"]
    for index, row in enumerate(model.rows):
        assert (
            model.get_value(index, model.column_for("B", "lcsc"))
            == snapshot.get(row.component_id, "B").lcsc
        )


def test_flags_filter_requires_one_variant_matching_every_predicate(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    """Do not combine BOM=true in A with POS=true in B into a false match."""
    source = evolve(snapshot, component="id1", bom=False, pos=False)
    source = evolve(source, component="id1", variants=("A",), bom=True)
    model = m.MatrixModel(evolve(source, component="id1", variants=("B",), pos=True))
    model.set_filter(require_bom=True, require_pos=True)
    assert {row.component_id for row in model.rows} == {"id2", "id10"}
    model.set_filter(differences_only=True)
    assert {row.component_id for row in model.rows} == {"id1", "id2"}


@pytest.mark.parametrize(
    ("prices", "symbols"),
    [
        (("1", "2", "4"), ("↓", "↑", "↑")),
        (("2", "1", "1"), ("↑", "↓", "↓")),
        (("1", "1.0", "1.00"), ("-", "-", "-")),
        (("0.00011", "0.00012", "0.00014"), ("↓", "↑", "↑")),
        (("0", "0", "1"), ("↓", "↓", "↑")),
        (("0", "0", "0"), ("-", "-", "-")),
        ((None, "1", "2"), ("?", "↓", "↑")),
        ((None, "1", None), ("?", "?", "?")),
        (("NaN", "Infinity", "-1"), ("?", "?", "?")),
        ((True, False, "invalid"), ("?", "?", "?")),
    ],
)
def test_price_arrows_compare_known_unrounded_prices_with_the_cheapest(
    snapshot: native.BoardVariantSnapshot,
    prices: tuple[Any, Any, Any],
    symbols: tuple[str, str, str],
) -> None:
    facts = catalog(
        snapshot,
        {
            v: {"price": p, "price_label": "$0.0001"}
            for v, p in zip(("", "A", "B"), prices)
        },
    )
    model = m.MatrixModel(snapshot, facts)
    for descending in (False, True):
        model.sort_by(0, descending)
        assert (
            tuple(
                model.get_display(*coordinates(model, v, "price", "id10"))
                for v in model.variants
            )
            == symbols
        )
        for variant, price in zip(model.variants, prices):
            if (
                price is None
                or type(price) is bool
                or price in ("NaN", "Infinity", "-1", "invalid")
            ):
                style = model.cell_style(*coordinates(model, variant, "price", "id10"))
                assert (
                    style.status != "known"
                    and not style.different
                    and not style.variant_different
                )
                assert "$0.0001" in model.cell_details(
                    *coordinates(model, variant, "price", "id10")
                )


@pytest.mark.parametrize(
    "prices,metric,variant,expected",
    [
        (
            ("0.00011", "0.00012", "0.00014"),
            "premium_ratio",
            "B",
            ("$0.00014", "$0.00011", "27.27%"),
        ),
        (("0", "0.01", "100"), "intensity_ratio", "A", ("Price increase: $0.01",)),
    ],
)
def test_price_premiums_and_tooltips_keep_exact_values_and_missing_peer_count(
    snapshot: native.BoardVariantSnapshot,
    prices: tuple[str, str, str],
    metric: str,
    variant: str,
    expected: tuple[str, ...],
) -> None:
    """Premiums, including a free minimum, retain distinct shades and exact amounts."""
    metadata = catalog(
        snapshot,
        {
            v: {"price": p, "price_label": "$0.0001"}
            for v, p in zip(("", "A", "B"), prices)
        },
        component="id10",
    )
    model = m.MatrixModel(snapshot, enrichment=metadata)
    row = model.row_for_component("id10")
    a, b = model.column_for("A", "price"), model.column_for("B", "price")
    assert (
        0
        < getattr(model.price_comparison(row, a), metric)
        < getattr(model.price_comparison(row, b), metric)
    )
    details = model.cell_details(row, model.column_for(variant, "price"))
    assert all(text in details for text in expected)
    metadata[("id10", "")] = replace(metadata[("id10", "")], lcsc="C999")
    model = m.MatrixModel(snapshot, enrichment=metadata)
    assert model.price_comparison(row, b).available_count == 2
    assert "1 unavailable" in model.cell_details(row, b)


@pytest.mark.parametrize(
    "available,catalog_tag",
    [
        (value, "C123")
        for value in (None, "", "unknown", -1, 1.5, True, "NaN", "Infinity")
    ]
    + [(100, ""), (100, "C999")],
)
def test_missing_or_invalid_stock_never_confirms_the_reserve(
    snapshot: native.BoardVariantSnapshot, available: Any, catalog_tag: str
) -> None:
    """Stock must be a whole nonnegative amount tied to the current assignment."""
    model = m.MatrixModel(
        snapshot,
        enrichment={
            ("id10", variant): m.CatalogMetadata(
                lcsc=catalog_tag if variant == "A" else "C123",
                stock=available if variant == "A" else 100,
                price=1.25,
                status="complete",
            )
            for variant in ("", "A", "B")
        },
        board_count=5,
    )
    row, col = model.row_for_component("id10"), model.column_for("A", "stock")
    assert model.stock_check(row, col).available is None
    assert model.get_value(row, col) is None
    assert model.get_display(row, col) == "⚠"
    style = model.cell_style(row, col)
    assert style.status != "known"
    assert not style.different and not style.variant_different
    if catalog_tag != "C123":
        assert model.get_value(row, model.column_for("A", "price")) is None
        assert model.get_display(row, model.column_for("A", "price")) == "?"


def test_stock_demand_excludes_bom_omissions_and_ignores_pos(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    """BOM/POP control demand; POS and display filters do not reduce purchasing."""
    source = evolve(snapshot, component="id2", variants=("A",), bom=False)
    source = evolve(source, component="id10", pos=False)
    model = m.MatrixModel(
        source,
        enrichment={
            ("id10", "A"): m.CatalogMetadata(lcsc="C123", stock="51"),
            ("id10", "B"): m.CatalogMetadata(lcsc="C999", stock=99999),
        },
        board_count=5,
    )
    row = model.row_for_component("id10")
    assert model.stock_check(row, model.column_for("A", "stock")).required == 5
    assert model.get_display(row, model.column_for("A", "stock")) == "✓"
    assert model.get_display(row, model.column_for("B", "stock")) == "⚠"


def test_known_catalog_fields_compare_while_classification_is_pending(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    common = {"price": "0.0123", "type": "Basic", "params": "1%"}
    facts = catalog(
        snapshot,
        {
            "": {**common, "stock": 10, "standard": False},
            "A": {**common, "stock": 11, "status": "pending"},
            "B": {**common, "status": "error"},
        },
    )
    model = m.MatrixModel(snapshot, facts)
    for field in ("params", "type", "stock", "price"):
        style = model.cell_style(*coordinates(model, "A", field, "id10"))
        assert style.status == "known" and style.matches == ("", "A", "B")
    assert model.cell_style(*coordinates(model, "A", "stock", "id10")).different
    for variant, status in (("A", "pending"), ("B", "error")):
        cell = coordinates(model, variant, "standard", "id10")
        assert model.cell_style(*cell).status == status
        assert f"Status: {status}" in model.cell_tooltip(*cell)
    assert model.cell_style(*coordinates(model, "B", "stock", "id10")).status == "error"
    assert model.cell_style(*coordinates(model, "B", "price", "id10")).status == "known"


def test_native_default_label_is_distinct_from_a_variant_named_default() -> None:
    """KiCad permits a named Default; native display labels disambiguate the base."""
    snapshot = Snapshot(tuple(State("id1", "R1", name) for name in ("", "Default")))
    snapshot = replace(
        snapshot,
        variants=(
            native.VariantDefinition(name="", label="< Default >"),
            native.VariantDefinition(name="Default", label="Default"),
        ),
    )
    model = m.MatrixModel(snapshot)
    assert model.variants == ("", "Default")
    assert model.variant_label("") == "< Default >"
    assert model.variant_label("Default") == "Default"
    assert model.variant_label(None) == "Shared"
    assert "· < Default > ·" in model.cell_details(0, model.column_for("", "lcsc"))
    assert "· Default ·" in model.cell_details(0, model.column_for("Default", "lcsc"))


def semantic_cells(model: Any) -> dict[tuple[str, Optional[str], str], Any]:
    """Compare public values and styles independently of screen coordinates."""
    return {
        (physical.component_id, spec.variant, spec.key): (
            model.get_value(row, column),
            model.cell_style(row, column),
        )
        for row, physical in enumerate(model.rows)
        for column, spec in enumerate(model.columns)
    }


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (["B", "B", "", "A"], ("B", "", "A")),
        (["A"], ("A", "", "B")),
        (("B", "removed"), ("B", "", "A")),
        ([None, ["A"], {"name": "B"}, False, 1, "B"], ("B", "", "A")),
        (["a", "default", "Default", " "], ("", "A", "B")),
        ([], ("", "A", "B")),
        (None, ("", "A", "B")),
        ("BA", ("", "A", "B")),
        ({"B": 0, "A": 1}, ("", "A", "B")),
    ],
)
def test_saved_order_is_normalized_without_losing_live_variants(
    snapshot: native.BoardVariantSnapshot,
    saved: object,
    expected: tuple[str, ...],
) -> None:
    """Saved state is advisory: invalid entries disappear and omissions append."""
    model = m.MatrixModel(snapshot)
    original_columns, original_cells = model.columns, semantic_cells(model)
    model.set_variant_order(("B", "A", ""))
    assert model.set_variant_order(saved)
    assert not model.set_variant_order(saved)
    assert model.columns == original_columns[:5] + tuple(
        col
        for variant in expected
        for col in original_columns[5:]
        if col.variant == variant
    )
    assert semantic_cells(model) == original_cells and model.snapshot is snapshot
    assert model.variant_order == expected
    assert model.variants == ("", "A", "B")


def test_reopened_order_drops_removed_names_and_appends_new_variants(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    """Stored presentation names must be reconciled against current native names."""
    changed = Snapshot(
        tuple(
            replace(state, variant_name="C") if state.variant_name == "A" else state
            for state in snapshot.components
        )
    )
    changed = replace(
        changed,
        variants=tuple(
            native.VariantDefinition(name=name, label=name or "Default")
            for name in ("", "B", "C")
        ),
    )
    model = m.MatrixModel(changed)
    assert model.set_variant_order(("A", "B", "")) is True
    assert model.variant_order == ("B", "", "C")
    assert model.variants == ("", "B", "C")
    assert model.get_value(0, model.column_for("C", "lcsc")) == "C456"


@pytest.mark.parametrize("variant", [None, "", "A", "B"])
@pytest.mark.parametrize("descending", [False, True])
def test_reorder_keeps_semantic_sort_through_filter_changes(
    snapshot: native.BoardVariantSnapshot,
    variant: Optional[str],
    descending: bool,
) -> None:
    """The sorted column remains the same field in the same variant after a drag."""
    model = m.MatrixModel(snapshot)
    field = "ref" if variant is None else "value"
    model.sort_by(model.column_for(variant, field), descending)
    original_ids = tuple(row.component_id for row in model.rows)

    model.set_variant_order(("B", "", "A"))
    assert tuple(row.component_id for row in model.rows) == original_ids
    model.set_filter(differences_only=True)
    assert tuple(row.component_id for row in model.rows) == tuple(
        component_id for component_id in original_ids if component_id != "id10"
    )
    model.set_variant_order(("A", "B", ""))
    model.set_filter()
    assert tuple(row.component_id for row in model.rows) == original_ids


def test_empty_board_reorders_and_sizes_only_text_columns() -> None:
    model = m.MatrixModel(Snapshot(()))
    assert model.set_variant_order(("A", "", "B")) is True
    assert model.variant_order == ("A", "", "B")
    assert model.rows == ()
    assert model.column_display_samples(model.column_for(None, "footprint")) == ()
    with pytest.raises(ValueError, match="text"):
        model.column_display_samples(model.column_for("A", "stock"))


def test_text_samples_preserve_full_inventory_weights_and_markers() -> None:
    """Full-inventory samples retain weights and markers across sort/filter choices."""
    footprints = (
        "Library:Typical",
        "Other:Typical",
        "Library:Typical",
        "Other:Typical",
        "庫:Outlier",
    )
    source = Snapshot(
        tuple(
            State(
                f"id{i}",
                f"R{i}",
                variant,
                footprint=footprint,
                bom=i == 0,
                pos=False,
                value=("typical" if i < 4 else "unusually-long-value") + variant,
            )
            for i, footprint in enumerate(footprints)
            for variant in ("", "A", "B")
        )
    )
    facts = {
        (part.component_id, part.variant_name): m.CatalogMetadata(
            params=f"specification{part.variant_name}",
            lcsc=part.lcsc if part.component_id != "id4" else "C999",
            status="complete",
        )
        for part in source.components
        if part.variant_name != "A"
    }
    model = m.MatrixModel(source, facts)
    cases = (
        (
            None,
            "footprint",
            ("Typical",) * 4 + ("Outlier",),
            ("",) * 5,
        ),
        ("", "value", ("typical",) * 4 + ("unusually-long-value",), ("",) * 5),
        ("A", "value", ("typicalA",) * 4 + ("unusually-long-valueA",), ("≠",) * 5),
        ("B", "params", ("specificationB",) * 4 + ("—",), ("≠",) * 4 + ("?",)),
        ("A", "params", ("—",) * 5, ("?",) * 5),
    )
    for filters, visible in (
        ({}, 5),
        ({"require_bom": True}, 1),
        ({"require_bom": True, "require_pos": True}, 0),
    ):
        model.set_filter(**filters)
        assert len(model.rows) == visible
        for descending in (False, True):
            model.sort_by(model.column_for("A", "value"), descending)
            for variant, field, values, markers in cases:
                column = model.column_for(variant, field)
                assert model.column_display_samples(column) == values
                assert model.column_display_markers(column) == markers


@pytest.mark.parametrize("text", ["", "Ω 10k", "abc"])
def test_text_samples_match_display_normalization(
    snapshot: native.BoardVariantSnapshot, text: object
) -> None:
    """Unknown/empty samples use the same placeholders as the actual cells."""
    source = evolve(snapshot, value=text, footprint=text)
    model = m.MatrixModel(source, catalog(source, {"B": {"params": text}}))
    for variant, field in ((None, "footprint"), ("A", "value"), ("B", "params")):
        col = model.column_for(variant, field)
        assert model.column_display_samples(col) == (model.get_display(0, col),) * 3


def footprint_snapshot(
    footprints: tuple[Optional[str], ...],
) -> native.BoardVariantSnapshot:
    """Capture repeated variants with independently identifiable physical rows."""
    return Snapshot(
        tuple(
            State(
                f"id{index}",
                f"R{index}",
                variant,
                footprint=footprint,
                footprint_field=footprint,
                bom=index == 0,
            )
            for index, footprint in enumerate(footprints)
            for variant in ("", "A", "B")
        )
    )


@pytest.mark.parametrize("show_library", (False, True))
@pytest.mark.parametrize(
    ("footprint", "compact"),
    (
        ("Library:Name", "Name"),
        ("Vendor:Library:Name", "Name"),
        ("Name", "Name"),
        ("接続子:端子_Ω", "端子_Ω"),
        ("", "—"),
        ("Library:", "—"),
    ),
)
def test_display_and_sizing_samples_share_library_visibility(
    show_library: bool,
    footprint: Optional[str],
    compact: str,
) -> None:
    """Both sizing modes keep Unicode, unqualified names, and empty placeholders."""
    model = m.MatrixModel(
        footprint_snapshot((footprint,)),
        **({"show_footprint_library": True} if show_library else {}),
    )
    assert model.show_footprint_library is show_library
    column = model.column_for(None, "footprint")
    expected = (footprint or "—") if show_library else compact

    assert model.get_display(0, column) == expected
    assert model.column_display_samples(column) == (expected,)
    assert model.get_value(0, column) == footprint
    assert model.rows[0].footprint == footprint
    assert model.cell_tooltip(0, column) == (
        "" if show_library else model.cell_details(0, column)
    )
    payload = model.copy_cell(0, column)
    assert payload.plain_text == (footprint or "")
    assert payload.rows[0].values == (("footprint", footprint or ""),)
    assert model.cell_details(0, column).split("\n")[1] == str(footprint)


def test_library_visibility_preserves_qualified_matching_and_other_tooltips() -> None:
    """Equal short names never conceal a native footprint metadata mismatch."""
    snapshot = footprint_snapshot(("PlacedLibrary:Name",))
    snapshot = evolve(snapshot, variants=("A",), footprint_field="OtherLibrary:Name")
    hidden = m.MatrixModel(snapshot)
    shown = m.MatrixModel(snapshot, show_footprint_library=True)
    value_column = hidden.column_for("A", "value")

    assert hidden.cell_style(0, value_column) == shown.cell_style(0, value_column)
    for model in (hidden, shown):
        details = model.cell_details(0, value_column)
        assert "OtherLibrary:Name" in details
        assert "PlacedLibrary:Name" in details
        assert "differs from placed geometry" in details
        assert model.cell_tooltip(0, value_column) == details
    assert hidden.snapshot is shown.snapshot is snapshot


@pytest.mark.parametrize("show_library", (False, True))
def test_footprint_sort_uses_visible_names_without_merging_shared_names(
    show_library: bool,
) -> None:
    """A library prefix must not make the compact names appear out of order."""
    model = m.MatrixModel(
        footprint_snapshot(("Zulu:Alpha", "Alpha:Zulu", "Other:Alpha")),
        show_footprint_library=show_library,
    )
    column = model.column_for(None, "footprint")
    model.sort_by(column)

    expected_ids = ["id1", "id2", "id0"] if show_library else ["id0", "id2", "id1"]
    assert [row.component_id for row in model.rows] == expected_ids
    assert [model.get_display(row, column) for row in range(3)] == (
        ["Alpha:Zulu", "Other:Alpha", "Zulu:Alpha"]
        if show_library
        else ["Alpha", "Alpha", "Zulu"]
    )
    assert {row.footprint for row in model.rows} == {
        "Zulu:Alpha",
        "Other:Alpha",
        "Alpha:Zulu",
    }
    model.sort_by(column, descending=True)
    assert [row.component_id for row in model.rows] == list(reversed(expected_ids))


def test_inherited_and_different_assignments_show_their_full_descriptions(
    snapshot: native.BoardVariantSnapshot,
) -> None:
    source = evolve(
        snapshot,
        component="id10",
        variants=("B",),
        lcsc="C456",
        assignment=assignment(),
    )
    descriptions = {
        "A": "Precision 10 kΩ resistor, ±0.1%\n温度係数 25 ppm/°C",
        "B": "Alternate 4.7 kΩ resistor — automotive grade",
    }
    facts = catalog(
        source,
        {
            v: {"description": text, "package": "0805"}
            for v, text in descriptions.items()
        },
    )
    model = m.MatrixModel(source, facts)
    for variant, code in (("A", "C123"), ("B", "C456")):
        cell = coordinates(model, variant, "lcsc", "id10")
        text = model.cell_details(*cell)
        assert model.cell_tooltip(*cell) == text
        assert text.startswith(
            f"R10 · {variant} · LCSC\n{code}\n{descriptions[variant]}"
        )
        assert "Catalog package: 0805" in text
        assert "Description unavailable" not in text
        assert descriptions["B" if variant == "A" else "A"] not in text


@pytest.mark.parametrize(
    "status,code,native_status,tag,description,expected",
    [
        (
            status,
            "C456",
            "valid",
            "C456",
            "Low-noise amplifier, 3.3 V — −40…125 °C",
            "Low-noise amplifier",
        )
        for status in ("complete", "pending", "error", "missing")
    ]
    + [
        ("complete", "C456", "valid", "C456", "", "Description unavailable"),
        ("complete", "C456", "valid", "C456", " \t\r\n", "Description unavailable"),
        ("complete", "C456", "valid", "C999", "Obsolete", "Description unavailable"),
        ("complete", "C456", "valid", "", "Obsolete", "Description unavailable"),
        ("complete", "", "empty", "C456", "Previous description", ""),
        ("complete", "", "missing", "C456", "Previous description", ""),
        ("complete", "", "invalid", "C456", "Previous description", ""),
    ],
)
def test_description_validity_is_independent_of_assembly_status(
    snapshot: native.BoardVariantSnapshot,
    status: str,
    code: str,
    native_status: str,
    tag: str,
    description: str,
    expected: str,
) -> None:
    source = evolve(
        snapshot,
        component="id1",
        variants=("A",),
        lcsc=code,
        assignment=assignment(native_status),
    )
    model = m.MatrixModel(
        source,
        catalog(
            source, {"A": {"lcsc": tag, "description": description, "status": status}}
        ),
    )
    cell = coordinates(model, "A", "lcsc")
    details = model.cell_details(*cell)
    assert model.cell_tooltip(*cell) == details
    assert details.splitlines()[1] == code
    if expected:
        assert expected in details
        if expected == "Description unavailable":
            assert "Obsolete" not in details
        else:
            assert description in details and "Description unavailable" not in details
    else:
        assert "Previous description" not in details
        assert "Description unavailable" not in details and "C456" not in details


@pytest.mark.parametrize(
    "field,value,display",
    [
        ("type", "Extended", "E"),
        ("type", "Basic", "B"),
        ("type", "Preferred", "P"),
        ("type", "preferred", "P"),
        ("type", "basic", "B"),
        ("type", "", "-"),
        ("type", "—", "-"),
        ("type", "-", "-"),
        ("side", "top", "T"),
        ("side", "bottom", "B"),
    ],
)
def test_compact_labels_preserve_full_values(
    snapshot: native.BoardVariantSnapshot,
    field: str,
    value: str,
    display: str,
) -> None:
    """Only displayed text is abbreviated; exact values and details remain intact."""
    model = (
        m.MatrixModel(evolve(snapshot, side=value))
        if field == "side"
        else m.MatrixModel(snapshot, catalog(snapshot, {"A": {"type": value}}))
    )
    cell = coordinates(model, None if field == "side" else "A", field)
    assert model.get_display(*cell) == display
    assert model.get_value(*cell) == value
    if value:
        assert value in model.cell_details(*cell)


@pytest.mark.parametrize(
    "populated,extra,sufficient",
    [
        (True, -1, False),
        (True, 0, False),
        (True, 1, True),
        (False, 0, False),
        (False, 1, True),
        (False, None, False),
    ],
)
def test_stock_reserve_uses_full_demand_and_strict_threshold(
    snapshot: native.BoardVariantSnapshot,
    populated: bool,
    extra: Optional[int],
    sufficient: bool,
) -> None:
    source = snapshot if populated else evolve(snapshot, pop=False)
    counts = {"": 3, "A": 2, "B": 1} if populated else dict.fromkeys(("", "A", "B"), 0)
    facts = catalog(
        source,
        {
            variant: {"stock": count * 50 + extra if extra is not None else None}
            for variant, count in counts.items()
        },
        component="id10",
    )
    model = m.MatrixModel(source, facts, board_count=5)
    model.set_filter(differences_only=True)
    assert model.row_for_component("id10") is None
    if populated:
        assert (
            model.stock_check(*coordinates(model, "A", "stock", "id2")).per_board == 2
        )
    model.set_filter()
    for variant, count in counts.items():
        cell = coordinates(model, variant, "stock", "id10")
        check = model.stock_check(*cell)
        assert (check.per_board, check.required, check.threshold) == (
            count,
            count * 5,
            count * 50,
        )
        assert check.sufficient is sufficient
        assert model.get_display(*cell) == ("✓" if sufficient else "⚠")
        assert (
            f"Required: {count} per board × 5 boards = {count * 5}"
            in model.cell_details(*cell)
        )
        if not populated:
            assert "No populated BOM demand" in model.cell_details(*cell)
