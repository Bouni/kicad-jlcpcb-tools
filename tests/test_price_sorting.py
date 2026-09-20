"""Tests for numerical price sorting in part selector and BOM lists (Issue #846)."""

import sqlite3
import types
from typing import Any

import pytest

from bom_estimation import pricing as pricing_mod
from bom_estimation.pricing import parse_price, price_sort_collation, price_sort_key

from .stock_test_support import stock_modules
from .wx_harness import load, module, package_stubs, wx_stubs


def _selector_price_row(price_str: str, lcsc: str = "C1") -> list[Any]:
    """Build a selector row with a specific price string at column 8."""
    return [
        lcsc,
        "MFR1",
        "0402",
        "Basic",
        "params",
        "100",
        "MFR",
        "Desc",
        price_str,
        "",
    ]


def _board_price_row(reference: str, lcsc: str = "C1") -> list[Any]:
    """Build a board row with a valid BOM state."""
    return [reference, "10k", "R_0402", lcsc, "Basic", "100"] + ["0"] * 8


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # PartSelector formatted string (Issue #846 cases)
        ("1 parts: $0.1 each / $0.1 total", 0.1),
        ("1 parts: $0.2 each / $0.2 total", 0.2),
        ("1 parts: $0.3 each / $0.3 total", 0.3),
        ("1 parts: $0.04 each / $0.04 total", 0.04),
        ("1 parts: $0.4 each / $0.4 total", 0.4),
        ("1 parts: $0.05 each / $0.05 total", 0.05),
        ("1 parts: $0.6 each / $0.6 total", 0.6),
        ("1 parts: $0.06 each / $0.06 total", 0.06),
        ("1 parts: $0.07 each / $0.07 total", 0.07),
        ("10 parts: $0.045 each / $0.45 total", 0.045),
        ("5 parts: $1.25 each / $6.25 total", 1.25),
        # BOM price labels
        ("$0.50", 0.50),
        ("$0.05", 0.05),
        ("$1.2345", 1.2345),
        ("$0.0000", 0.0),
        ("$1,234.50", 1234.50),
        # Raw database tier bands
        ("1-:0.05", 0.05),
        ("1-9:0.40,10-:0.30", 0.40),
        ("1-4:0.60,5-9:0.50,10-:0.40", 0.60),
        # Numeric / raw values
        ("0.05", 0.05),
        (0.05, 0.05),
        (10, 10.0),
        # Invalid / missing values
        ("Error in price data", None),
        ("N/A", None),
        ("-", None),
        ("?", None),
        ("", None),
        (None, None),
        (True, None),
        (False, None),
    ],
)
def test_parse_price(value: Any, expected: Any) -> None:
    """Verify parse_price extracts exact unit prices across all supported formats."""
    assert parse_price(value) == expected


def test_issue_846_ascending_price_sort_order() -> None:
    """Regression test for Issue #846: $0.05 must not sort between $0.40 and $0.60.

    In Issue #846, natural string sorting ordered prices as:
    0.1, 0.2, 0.3, 0.04, 0.4, 0.05, 0.6, 0.06, 0.07.
    Numeric price sorting must order them strictly as:
    0.04, 0.05, 0.06, 0.07, 0.1, 0.2, 0.3, 0.4, 0.6.
    """
    repro_items = [
        "1 parts: $0.1 each / $0.1 total",
        "1 parts: $0.2 each / $0.2 total",
        "1 parts: $0.3 each / $0.3 total",
        "1 parts: $0.04 each / $0.04 total",
        "1 parts: $0.4 each / $0.4 total",
        "1 parts: $0.05 each / $0.05 total",
        "1 parts: $0.6 each / $0.6 total",
        "1 parts: $0.06 each / $0.06 total",
        "1 parts: $0.07 each / $0.07 total",
    ]

    sorted_asc = sorted(repro_items, key=lambda x: price_sort_key(x, ascending=True))
    expected_asc = [
        "1 parts: $0.04 each / $0.04 total",
        "1 parts: $0.05 each / $0.05 total",
        "1 parts: $0.06 each / $0.06 total",
        "1 parts: $0.07 each / $0.07 total",
        "1 parts: $0.1 each / $0.1 total",
        "1 parts: $0.2 each / $0.2 total",
        "1 parts: $0.3 each / $0.3 total",
        "1 parts: $0.4 each / $0.4 total",
        "1 parts: $0.6 each / $0.6 total",
    ]
    assert sorted_asc == expected_asc

    # Specifically check that 0.05 is between 0.04 and 0.06
    idx_04 = sorted_asc.index("1 parts: $0.04 each / $0.04 total")
    idx_05 = sorted_asc.index("1 parts: $0.05 each / $0.05 total")
    idx_06 = sorted_asc.index("1 parts: $0.06 each / $0.06 total")
    idx_40 = sorted_asc.index("1 parts: $0.4 each / $0.4 total")
    idx_60 = sorted_asc.index("1 parts: $0.6 each / $0.6 total")
    assert idx_04 < idx_05 < idx_06 < idx_40 < idx_60


def test_price_sort_descending_and_invalid_placement() -> None:
    """Invalid and blank prices sort to the end in both ascending and descending modes."""
    items = [
        "1 parts: $0.5 each / $0.5 total",
        "Error in price data",
        "1 parts: $0.05 each / $0.05 total",
        "",
        "1 parts: $0.1 each / $0.1 total",
    ]

    asc = sorted(items, key=lambda x: price_sort_key(x, ascending=True))
    assert asc[:3] == [
        "1 parts: $0.05 each / $0.05 total",
        "1 parts: $0.1 each / $0.1 total",
        "1 parts: $0.5 each / $0.5 total",
    ]
    assert set(asc[3:]) == {"Error in price data", ""}

    desc = sorted(items, key=lambda x: price_sort_key(x, ascending=False))
    assert desc[:3] == [
        "1 parts: $0.5 each / $0.5 total",
        "1 parts: $0.1 each / $0.1 total",
        "1 parts: $0.05 each / $0.05 total",
    ]
    assert set(desc[3:]) == {"Error in price data", ""}


def test_part_selector_data_model_compares_price_numerically() -> None:
    """PartSelectorDataModel.Compare uses price_sort_key for the price column."""
    with stock_modules() as modules:
        model = modules.datamodel.PartSelectorDataModel()
        col = model.columns["price"]

        r_01 = _selector_price_row("1 parts: $0.1 each / $0.1 total", "C1")
        r_05 = _selector_price_row("1 parts: $0.05 each / $0.05 total", "C2")
        r_40 = _selector_price_row("1 parts: $0.4 each / $0.4 total", "C3")
        r_err = _selector_price_row("Error in price data", "C4")

        model.AddEntry(r_01)
        model.AddEntry(r_05)
        model.AddEntry(r_40)
        model.AddEntry(r_err)

        item_01 = model.ObjectToItem(r_01)
        item_05 = model.ObjectToItem(r_05)
        item_40 = model.ObjectToItem(r_40)
        item_err = model.ObjectToItem(r_err)

        # In ascending order: 0.05 < 0.1 < 0.4 < Error
        assert model.Compare(item_05, item_01, col, True) < 0
        assert model.Compare(item_01, item_05, col, True) > 0

        assert model.Compare(item_01, item_40, col, True) < 0
        assert model.Compare(item_05, item_40, col, True) < 0

        assert model.Compare(item_40, item_err, col, True) < 0
        assert model.Compare(item_err, item_40, col, True) > 0

        # In descending order: 0.4 > 0.1 > 0.05 > Error
        assert model.Compare(item_40, item_01, col, False) < 0
        assert model.Compare(item_01, item_05, col, False) < 0
        assert model.Compare(item_05, item_err, col, False) < 0


def test_part_list_data_model_compares_bom_price_numerically() -> None:
    """PartListDataModel.Compare uses price_sort_key for the PRICE_COL column."""
    with stock_modules() as modules:
        model = modules.datamodel.PartListDataModel(scale_factor=1.0)
        col = model.columns["PRICE_COL"]

        r1 = _board_price_row("R1", "C1")
        r2 = _board_price_row("R2", "C2")
        r3 = _board_price_row("R3", "C3")

        model.AddEntry(r1)
        model.AddEntry(r2)
        model.AddEntry(r3)

        model.set_bom_price("R1", "$0.5000")
        model.set_bom_price("R2", "$0.0500")
        model.set_bom_price("R3", "N/A")

        item1 = model.ObjectToItem(r1)
        item2 = model.ObjectToItem(r2)
        item3 = model.ObjectToItem(r3)

        # Ascending: $0.0500 < $0.5000 < N/A
        assert model.Compare(item2, item1, col, True) < 0
        assert model.Compare(item1, item2, col, True) > 0
        assert model.Compare(item1, item3, col, True) < 0

        # Descending: $0.5000 > $0.0500 > N/A
        assert model.Compare(item1, item2, col, False) < 0
        assert model.Compare(item2, item1, col, False) > 0
        assert model.Compare(item2, item3, col, False) < 0


def test_price_sort_collation() -> None:
    """price_sort_collation compares prices numerically for SQLite collation."""
    # Ascending
    assert price_sort_collation("1-:0.05", "1-:0.10", ascending=True) < 0
    assert price_sort_collation("1-:0.10", "1-:0.05", ascending=True) > 0
    assert price_sort_collation("1-:0.05", "1-:0.05", ascending=True) == 0
    assert price_sort_collation("1-9:0.40,10-:0.30", "1-:0.05", ascending=True) > 0
    assert price_sort_collation("1-:0.05", "invalid", ascending=True) < 0
    assert price_sort_collation("invalid", "1-:0.05", ascending=True) > 0

    # Descending: valid prices sorted high to low, but invalid STILL sorted last
    assert price_sort_collation("1-:0.10", "1-:0.05", ascending=False) < 0
    assert price_sort_collation("1-:0.05", "1-:0.10", ascending=False) > 0
    assert price_sort_collation("1-:0.05", "invalid", ascending=False) < 0
    assert price_sort_collation("invalid", "1-:0.05", ascending=False) > 0


def test_sqlite_price_collation_query_order_and_limit() -> None:
    """SQLite query with pricesort_desc keeps invalid rows last and does not displace valid rows under LIMIT."""
    con = sqlite3.connect(":memory:")
    con.create_collation("pricesort_asc", lambda a, b: price_sort_collation(a, b, True))
    con.create_collation(
        "pricesort_desc", lambda a, b: price_sort_collation(a, b, False)
    )
    con.execute("CREATE TABLE test_parts (lcsc TEXT, price TEXT)")
    con.executemany(
        "INSERT INTO test_parts VALUES (?, ?)",
        [
            ("C1", "1-:0.50"),
            ("C2", "Error in price data"),
            ("C3", "1-:0.05"),
            ("C4", ""),
            ("C5", "1-:2.00"),
            ("C6", "1-:0.10"),
        ],
    )

    # Ascending: 0.05, 0.10, 0.50, 2.00, then invalid entries
    cur = con.execute(
        "SELECT lcsc, price FROM test_parts ORDER BY price COLLATE pricesort_asc ASC"
    )
    asc_results = [r[0] for r in cur.fetchall()]
    assert asc_results[:4] == ["C3", "C6", "C1", "C5"]
    assert set(asc_results[4:]) == {"C2", "C4"}

    # Descending: 2.00, 0.50, 0.10, 0.05, then invalid entries
    cur = con.execute(
        "SELECT lcsc, price FROM test_parts ORDER BY price COLLATE pricesort_desc ASC"
    )
    desc_results = [r[0] for r in cur.fetchall()]
    assert desc_results[:4] == ["C5", "C1", "C6", "C3"]
    assert set(desc_results[4:]) == {"C2", "C4"}

    # Descending with LIMIT 3: must return top 3 priced parts without invalid entries displacing them
    cur = con.execute(
        "SELECT lcsc FROM test_parts ORDER BY price COLLATE pricesort_desc ASC LIMIT 3"
    )
    limit_results = [r[0] for r in cur.fetchall()]
    assert limit_results == ["C5", "C1", "C6"]


def test_price_sort_with_quantity_tier_crossing() -> None:
    """Tier prices cross based on selected quantity, ordering appropriately in SQLite."""
    # Part A is expensive at qty 1 ($1.00) but cheap at qty 10 ($0.01)
    # Part B is $0.50 at any quantity
    part_a = "1-9:1.00,10-:0.01"
    part_b = "1-:0.50"

    assert parse_price(part_a, quantity=1) == 1.00
    assert parse_price(part_a, quantity=10) == 0.01

    # At quantity 1: Part B ($0.50) < Part A ($1.00)
    assert price_sort_collation(part_b, part_a, ascending=True, quantity=1) < 0
    assert price_sort_collation(part_a, part_b, ascending=True, quantity=1) > 0

    # At quantity 10: Part A ($0.01) < Part B ($0.50)
    assert price_sort_collation(part_a, part_b, ascending=True, quantity=10) < 0
    assert price_sort_collation(part_b, part_a, ascending=True, quantity=10) > 0

    # In SQLite with quantity closure:
    con = sqlite3.connect(":memory:")
    con.create_collation(
        "pricesort_q1", lambda a, b: price_sort_collation(a, b, True, quantity=1)
    )
    con.create_collation(
        "pricesort_q10", lambda a, b: price_sort_collation(a, b, True, quantity=10)
    )
    con.execute("CREATE TABLE parts (lcsc TEXT, price TEXT)")
    con.executemany("INSERT INTO parts VALUES (?, ?)", [("A", part_a), ("B", part_b)])

    # At Qty 1: B comes first
    q1_rows = [
        r[0]
        for r in con.execute(
            "SELECT lcsc FROM parts ORDER BY price COLLATE pricesort_q1 ASC"
        ).fetchall()
    ]
    assert q1_rows == ["B", "A"]

    # At Qty 10: A comes first
    q10_rows = [
        r[0]
        for r in con.execute(
            "SELECT lcsc FROM parts ORDER BY price COLLATE pricesort_q10 ASC"
        ).fetchall()
    ]
    assert q10_rows == ["A", "B"]


def test_part_selector_get_price_delegates_to_get_unit_price() -> None:
    """PartSelectorDialog.get_price delegates to get_unit_price from pricing.py."""
    package = "price_selector_test_pkg"
    stubs = {
        **package_stubs(package),
        **wx_stubs(
            Dialog=type("Dialog", (), {}),
            Colour=lambda *rgb: rgb,
            SYS_COLOUR_WINDOW=0,
            SystemSettings=types.SimpleNamespace(
                GetColour=lambda _key: types.SimpleNamespace(GetLuminance=lambda: 0.1)
            ),
        ),
    }
    stubs["wx.dataview"].PyDataViewModel = object
    stubs["wx.dataview"].DataViewCtrl = object
    stubs["wx.dataview"].DataViewColumn = object
    stubs[f"{package}.dataview_highlight"] = module(
        f"{package}.dataview_highlight",
        HighlightedTextRenderer=object,
    )
    stubs[f"{package}.datamodel"] = module(
        f"{package}.datamodel",
        PartSelectorDataModel=object,
    )
    stubs[f"{package}.derive_params"] = module(
        f"{package}.derive_params",
        params_for_part=lambda *args: "",
    )
    bom_mod = module(f"{package}.bom_estimation")
    stubs[f"{package}.bom_estimation"] = bom_mod
    stubs[f"{package}.bom_estimation.pricing"] = pricing_mod
    partselector_mod = load(package, "partselector", stubs)
    selector = object.__new__(partselector_mod.PartSelectorDialog)
    assert selector.get_price(1, "1-9:0.12,10-:0.08") == 0.12
    assert selector.get_price(10, "1-9:0.12,10-:0.08") == 0.08
    assert selector.get_price(1, "") == -1.0
