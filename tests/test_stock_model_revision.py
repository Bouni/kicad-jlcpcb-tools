"""Regression coverage for stock sorting and visible unknown availability."""

from functools import cmp_to_key
from typing import Any

import pytest

from .stock_test_support import board_row, selector_row, stock_modules
from .wx_harness import load_siblings


class CellAttr:
    """Retain the native cell attributes read by a renderer."""

    def __init__(self) -> None:
        self.colour: Any = None
        self.background: Any = None
        self.bold = False

    def SetColour(self, colour: Any) -> None:
        """Update text colour without changing the native row background."""
        self.colour = colour

    def SetBackgroundColour(self, colour: Any) -> None:
        """Record accidental background changes as observable test failures."""
        self.background = colour

    def SetBold(self, bold: bool) -> None:
        """Retain the renderer's font weight."""
        self.bold = bold


@pytest.mark.parametrize("kind", ["board", "selector"])
@pytest.mark.parametrize("simplified", [False, True])
@pytest.mark.parametrize("ascending", [False, True])
def test_stock_header_sort_restores_blanks_before_numeric_supply(
    kind: str, simplified: bool, ascending: bool
) -> None:
    """Native Compare keeps blank placement and exact ordering in either mode."""
    stocks = ["unknown", "7299", "?", None, "", " ", "0", 7260, 1, 0]
    expected = (
        [None, "", " ", "0", 0, 1, 7260, "7299", "?", "unknown"]
        if ascending
        else ["unknown", "?", "7299", 7260, 1, "0", 0, None, "", " "]
    )
    with stock_modules() as modules:
        if kind == "board":
            model = modules.datamodel.PartListDataModel(1.0, simplified)
            rows = [board_row(f"R{index}", value) for index, value in enumerate(stocks)]
            column = model.columns["STOCK_COL"]
        else:
            model = modules.datamodel.PartSelectorDataModel(simplified)
            rows = [selector_row(value) for value in stocks]
            column = model.columns["stock"]
        for row in rows:
            model.AddEntry(row)
        items = [model.ObjectToItem(row) for row in model.data]
        ordered = sorted(
            items,
            key=cmp_to_key(
                lambda first, second: model.Compare(first, second, column, ascending)
            ),
        )
        assert [model.ItemToObject(item)[column] for item in ordered] == expected
        for item in items:
            assert model.Compare(item, item, column, ascending) == 0


@pytest.mark.parametrize("stock", [None, "", " "])
@pytest.mark.parametrize("simplified", [False, True])
def test_assigned_blank_stock_is_visible_without_mutating_raw_data(
    stock: Any, simplified: bool
) -> None:
    """Missing supply has a visible label only while the main row is assigned."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(1.0, simplified)
        model.AddEntry(board_row("R1", stock, lcsc="C1"))
        item = model.ObjectToItem(model.data[0])
        column = model.columns["STOCK_COL"]
        assert model.GetValue(item, column) == "?"
        assert model.ItemToObject(item)[column] is stock

        model.set_simplify_stock(not simplified)
        assert model.GetValue(item, column) == "?"
        assert model.ItemToObject(item)[column] is stock

        model.remove_lcsc_number(item)
        assert model.GetValue(item, column) == ""
        model.set_lcsc("R1", "C2", "Basic", stock, "")
        assert model.GetValue(item, column) == "?"
        assert model.ItemToObject(item)[column] is stock


@pytest.mark.parametrize("stock", [None, "", " "])
def test_unassigned_board_and_selector_blank_stock_stay_blank(stock: Any) -> None:
    """The unknown-assignment marker cannot leak into unassigned/search rows."""
    with stock_modules() as modules:
        board = modules.datamodel.PartListDataModel(1.0)
        board.AddEntry(board_row("R1", stock, lcsc=""))
        assert board.GetValue(board.data[0], board.columns["STOCK_COL"]) == ""
        selector = modules.datamodel.PartSelectorDataModel()
        selector.AddEntry(selector_row(stock))
        assert selector.GetValue(selector.data[0], selector.columns["stock"]) == (
            "" if stock is None else stock
        )
        assert selector.get_stock(selector.data[0]) is stock


def test_unknown_placeholder_retains_native_style_when_concern_clears() -> None:
    """Disabling concerns clears colour and bold, while unknown stock stays visible."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(1.0)
        model.AddEntry(board_row("R1", None))
        item = model.ObjectToItem(model.data[0])
        column = model.columns["STOCK_COL"]
        for refs in ({"R1"}, set()):
            model.set_stock_concern_refs(refs)
            attr = CellAttr()
            assert model.GetAttr(item, column, attr) is bool(refs)
            assert attr.bold is bool(refs)
            assert (attr.colour is not None) is bool(refs)
            assert attr.background is None
            assert model.GetValue(item, column) == "?"
            assert model.ItemToObject(item)[column] is None


@pytest.mark.parametrize("kind", ["board", "selector"])
def test_simplify_changes_notify_each_stock_cell_only_when_state_changes(
    kind: str,
) -> None:
    """Both associated views repaint on toggles without notifying unrelated cells."""
    with stock_modules() as modules:
        if kind == "board":
            model = modules.datamodel.PartListDataModel(1.0)
            rows = [board_row("R1", "7260"), board_row("R2", "7299")]
            column = model.columns["STOCK_COL"]
        else:
            model = modules.datamodel.PartSelectorDataModel()
            rows = [selector_row("7260"), selector_row("7299")]
            column = model.columns["stock"]
        for row in rows:
            model.AddEntry(row)
        model.notifications.clear()
        model.set_simplify_stock(True)
        assert model.notifications == []
        model.set_simplify_stock(False)
        assert model.notifications == [
            ("value", (model.ObjectToItem(row), column)) for row in model.data
        ]


def test_shared_presentation_preserves_main_icons_and_computed_standard_cells() -> None:
    """Computed columns have no backing index and icons retain their renderer type."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(1.0)
        model.AddEntry(board_row("R1", "7260"))
        model.AddEntry(board_row("R2", "7299"))
        model.set_standard_only_refs({"R1"})
        standard, ordinary = model.data
        column = model.columns["STANDARD_ONLY_COL"]
        assert len(standard) == column
        assert model.GetValue(standard, column) is True
        assert model.GetValue(ordinary, column) is False
        assert model.HasValue(ordinary, column) is False
        for key in ("BOM_COL", "POS_COL", "DNP_COL"):
            icon_column = model.columns[key]
            assert model.GetValue(standard, icon_column) == ("", standard[icon_column])


@pytest.mark.parametrize("kind", ["board", "selector"])
def test_shared_compare_preserves_natural_nonstock_sorting(kind: str) -> None:
    """Stock comparison does not change ordinary reference/LCSC column ordering."""
    with stock_modules() as modules:
        if kind == "board":
            model = modules.datamodel.PartListDataModel(1.0)
            rows = [board_row("R2", "7299"), board_row("R10", "7260")]
            column = model.columns["REF_COL"]
        else:
            model = modules.datamodel.PartSelectorDataModel()
            rows = [selector_row("7299", "C2"), selector_row("7260", "C10")]
            column = model.columns["lcsc"]
        for row in rows:
            model.AddEntry(row)
        first, second = model.data
        assert model.Compare(first, second, column, True) < 0
        assert model.Compare(first, second, column, False) > 0


def test_main_params_sort_ignores_real_highlight_metadata() -> None:
    """Equal visible Params compare equal even when row-specific terms differ."""
    with stock_modules() as modules:
        modules.wx.dataview.DataViewCustomRenderer = object
        with load_siblings("stock_model_codec", ("dataview_highlight",), {}) as loaded:
            codec = loaded["dataview_highlight"]
            modules.datamodel.encode_highlighted_value = codec.encode_highlighted_value
            modules.datamodel.decode_highlighted_value = codec.decode_highlighted_value
            modules.datamodel.expand_value = codec.expand_value
            modules.datamodel.expand_footprint = codec.expand_footprint
            model = modules.datamodel.PartListDataModel(1.0)
            column = model.columns["PARAMS_COL"]
            for reference, value, params in (
                ("R1", "10k", "2 mA"),
                ("C1", "100nF", "2 mA"),
                ("R2", "10k", "10 mA"),
            ):
                row = board_row(reference, "7260")
                row[model.columns["VALUE_COL"]] = value
                row[column] = params
                model.AddEntry(row)
            first, same_visible, larger = model.data
            assert model.GetValue(first, column) != model.GetValue(same_visible, column)
            assert model.Compare(first, same_visible, column, True) == 0
            assert model.Compare(first, same_visible, column, False) == 0
            assert model.Compare(first, larger, column, True) < 0
            assert model.Compare(first, larger, column, False) > 0


def test_catalog_recovery_refreshes_matching_rows_without_resetting_board_state() -> (
    None
):
    """Successful lookup recovery replaces only stock/type/Params for its LCSC group."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(1.0)
        for reference, lcsc in (("R1", "C1"), ("R2", " c1 "), ("R3", "C2")):
            model.AddEntry(board_row(reference, None, lcsc))
        model.set_standard_only_refs({"R1"})
        model.set_stock_concern_refs({"R1", "R2"})
        model.set_bom_price("R1", "$0.50")
        model.set_enrichment_status("R1", "Enriched")
        before = [list(row) for row in model.data]
        model.notifications.clear()

        model.set_catalog_details("c1", "Extended", "22095", "updated params")

        changed_columns = {
            model.columns["TYPE_COL"],
            model.columns["STOCK_COL"],
            model.columns["PARAMS_COL"],
        }
        for row, original in zip(model.data[:2], before[:2]):
            assert row[model.columns["TYPE_COL"]] == "Extended"
            assert row[model.columns["STOCK_COL"]] == "22095"
            assert model._decode_params_value(row[model.columns["PARAMS_COL"]]) == (
                "updated params"
            )
            assert model.GetValue(row, model.columns["STOCK_COL"]) == "22 k"
            for column in range(len(row)):
                if column not in changed_columns:
                    assert row[column] == original[column]
        assert model.data[2] == before[2]
        assert model.standard_only_refs == {"R1"}
        assert model.stock_concern_refs == {"R1", "R2"}
        assert model.notifications == [
            ("changed", (model.ObjectToItem(row),)) for row in model.data[:2]
        ]


def test_missing_catalog_refresh_preserves_unknown_and_ignores_unassigned_rows() -> (
    None
):
    """Unknown supply cannot turn into zero or become assigned during a refresh."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(1.0)
        model.AddEntry(board_row("R1", "22095"))
        model.AddEntry(board_row("R2", "", lcsc=""))
        assigned, unassigned = model.data
        original_unassigned = list(unassigned)

        model.set_catalog_details("C1", "", None, "")
        assert assigned[model.columns["STOCK_COL"]] is None
        assert model.GetValue(assigned, model.columns["STOCK_COL"]) == "?"
        model.notifications.clear()
        model.set_catalog_details(" ", "Basic", "1000", "unrelated params")
        assert unassigned == original_unassigned
        assert model.notifications == []
