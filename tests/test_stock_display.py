"""Compact stock is presentation only: precision survives display and sorting."""

import importlib
from typing import Any

import pytest

from .stock_test_support import board_row, selector_row, stock_modules
from .wx_harness import load_siblings


@pytest.mark.parametrize(
    ("stock", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (1000, "1k"),
        (1099, "1k"),
        (1100, "1.1k"),
        (7260, "7.2k"),
        (9999, "9.9k"),
        (10000, "10k"),
        (22095, "22k"),
        (888000, "888k"),
        (999999, "999k"),
        (1000000, "1M"),
        (1099999, "1M"),
        (1100000, "1.1M"),
        (8880000, "8.8M"),
        (9999999, "9.9M"),
        (10000000, "10M"),
        (999999999, "999M"),
        (1000000000, "1B"),
        (7260000000, "7.2B"),
        (22095000000, "22B"),
    ],
)
@pytest.mark.parametrize("as_string", [False, True])
def test_compact_stock_truncates_without_exaggerating_supply(
    stock: int, expected: str, as_string: bool
) -> None:
    """The user's examples and magnitude boundaries define the precision rules."""
    with load_siblings("stock_formatter_tests", ("stock_display",), {}) as loaded:
        formatter = loaded["stock_display"].format_stock
        value = str(stock) if as_string else stock
        assert formatter(value) == expected
        assert formatter(value, simplified=False) == str(stock)


@pytest.mark.parametrize(
    "stock", [None, "", "-", "?", "unknown", "5000+", -1, 1.5, True]
)
@pytest.mark.parametrize("simplified", [False, True])
def test_unknown_stock_stays_unknown(stock: Any, simplified: bool) -> None:
    """Unknown supply must never become a numeric zero or acquire a suffix."""
    with load_siblings("stock_formatter_tests", ("stock_display",), {}) as loaded:
        expected = "" if stock is None else str(stock)
        assert loaded["stock_display"].format_stock(stock, simplified) == expected


@pytest.mark.parametrize("kind", ["board", "selector"])
def test_models_default_to_compact_display_and_preserve_raw_stock(kind: str) -> None:
    """Real constructors and AddEntry retain exact source values for later use."""
    with stock_modules() as modules:
        if kind == "board":
            model = modules.datamodel.PartListDataModel(scale_factor=1.0)
            row = board_row("R1", "22095")
            column = model.columns["STOCK_COL"]
        else:
            model = modules.datamodel.PartSelectorDataModel()
            row = selector_row("22095")
            column = model.columns["stock"]
        model.AddEntry(row)
        item = model.ObjectToItem(model.data[0])

        assert model.GetValue(item, column) == "22k"
        assert model.get_all()[0][column] == "22095"
        if kind == "selector":
            assert model.get_stock(item) == "22095"

        model.notifications.clear()
        model.set_simplify_stock(False)
        assert model.GetValue(item, column) == "22095"
        assert ("value", (item, column)) in model.notifications

        model.set_simplify_stock(True)
        assert model.GetValue(item, column) == "22k"
        assert model.get_all()[0][column] == "22095"


@pytest.mark.parametrize("kind", ["board", "selector"])
@pytest.mark.parametrize("simplified", [False, True])
@pytest.mark.parametrize(
    "stocks", [("7260", "7299"), ("999", "1000"), ("22095", "8880000")]
)
def test_stock_sort_uses_exact_numeric_values_in_both_modes(
    kind: str, simplified: bool, stocks: tuple[str, str]
) -> None:
    """Even rows with identical compact labels retain correct ascending order."""
    with stock_modules() as modules:
        if kind == "board":
            model = modules.datamodel.PartListDataModel(1.0, simplify_stock=simplified)
            column = model.columns["STOCK_COL"]
            rows = [board_row(f"R{i}", stock) for i, stock in enumerate(stocks)]
        else:
            model = modules.datamodel.PartSelectorDataModel(simplify_stock=simplified)
            column = model.columns["stock"]
            rows = [selector_row(stock) for stock in stocks]
        for row in rows:
            model.AddEntry(row)
        first, second = map(model.ObjectToItem, model.data)

        assert model.Compare(first, second, column, True) < 0
        assert model.Compare(first, second, column, False) > 0
        assert model.Compare(second, first, column, True) > 0
        assert model.Compare(first, first, column, True) == 0
        if simplified and stocks == ("7260", "7299"):
            assert (
                model.GetValue(first, column)
                == model.GetValue(second, column)
                == "7.2k"
            )


def test_assignment_and_removal_keep_exact_stock_until_rendering() -> None:
    """Assignment updates can be rendered compactly and reverted without data loss."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(1.0)
        model.AddEntry(board_row("R1", "22095"))
        item = model.ObjectToItem(model.data[0])
        column = model.columns["STOCK_COL"]

        model.set_lcsc("R1", "C2", "Basic", "8880000", "new params")
        assert model.GetValue(item, column) == "8.8M"
        assert model.get_all()[0][column] == "8880000"
        model.set_simplify_stock(False)
        assert model.GetValue(item, column) == "8880000"
        model.remove_lcsc_number(item)
        assert model.GetValue(item, column) == ""
        assert model.get_all()[0][column] == ""


def test_stock_sort_unknown_values_are_comparable() -> None:
    """Missing stock does not break native sorting when numeric rows are present."""
    with stock_modules() as modules:
        display = importlib.import_module(f"{modules.package}.stock_display")
        stocks = [None, "", "?", "0", "999", "1000"]
        keys = [display.stock_sort_key(stock) for stock in stocks]
        assert sorted(keys)
        assert display.stock_sort_key("999") < display.stock_sort_key("1000")
