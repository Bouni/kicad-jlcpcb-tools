"""Refilled part lists keep their sort and drop a stale selection on real controls."""

from typing import Any

import pytest

from .native_window_support import _SelectableFootprint, select_result, window_ui
from .native_wx_support import pump

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx

# Insertion order matches neither sort; natural LCSC order differs from text order.
RESULTS = (("C9", "500"), ("C100", "3000"), ("C2", "1"), ("C10", "20"))
LCSC_ORDER = ["C2", "C9", "C10", "C100"]
STOCK_ORDER = ["C2", "C10", "C9", "C100"]


def displayed_lcsc(view: Any, model: Any, column: int) -> list[str]:
    """Read each row's LCSC number in the order the control draws them."""
    # wxPython 4.2 has no RowToItem; every port reports where it drew an item.
    rows = sorted(
        model.data, key=lambda row: view.GetItemRect(model.ObjectToItem(row)).y
    )
    return [row[column] for row in rows]


@pytest.mark.parametrize("ascending", [True, False], ids=["ascending", "descending"])
@pytest.mark.parametrize(
    ("key", "order"), [("lcsc", LCSC_ORDER), ("stock", STOCK_ORDER)]
)
def test_selector_refill_keeps_the_sort_and_clears_the_selection(
    window_ui: Any, key: str, order: list[str], ascending: bool
) -> None:
    """A new search shows its rows in the sorted order, with nothing selected."""

    def check(ui: Any) -> None:
        selector = ui.selector_module.PartSelectorDialog(ui.dialog, {"R1": "C1"})
        ui.dialog._part_selector = selector
        selector.Show()
        pump(ui.wx)
        view, model = selector.part_list, selector.part_list_model
        fields = ui.selector_module.DB_FIELDS
        results = [
            tuple(
                {"LCSC Part": lcsc, "Library Type": "Basic", "Stock": stock}.get(
                    field, ""
                )
                for field in fields
            )
            for lcsc, stock in RESULTS
        ]
        ui.catalog.search.side_effect = lambda _parameters: list(results)
        index = {
            column.key: position
            for position, column in enumerate(ui.selector_module.PARTSELECTOR_COLUMNS)
        }
        (column,) = (
            view.GetColumn(position)
            for position in range(view.GetColumnCount())
            if view.GetColumn(position).GetModelColumn() == index[key]
        )
        column.SetSortOrder(ascending)
        expected = order if ascending else order[::-1]

        selector.search(None)
        pump(ui.wx)
        assert displayed_lcsc(view, model, index["lcsc"]) == expected

        select_result(ui, selector)
        selector.search(None)
        pump(ui.wx)
        assert displayed_lcsc(view, model, index["lcsc"]) == expected
        assert view.GetSelectedItemsCount() == 0

        select_result(ui, selector)
        ui.catalog.search.side_effect = lambda _parameters: []
        selector.search(None)
        pump(ui.wx)
        assert model.data == []
        assert view.GetSelectedItemsCount() == 0
        assert selector.result_count.GetLabel().startswith("0 parts")
        selector.Close()

    window_ui.run(check)


def test_footprint_refresh_with_a_cold_catalog_keeps_rows_and_metadata(
    window_ui: Any,
) -> None:
    """A refresh that rereads the catalog keeps every row and its assembly details."""
    ui = window_ui
    ui.board.names.clear()
    ui.board.current = ""
    second = _SelectableFootprint(ui.board, "component-2", "R2")
    second.SetField("LCSC", "C2")
    ui.board.parts.append(second)
    metadata = {
        "C1": {"assembly_process": "THT", "component_product_type": 1},
        "C2": {"assembly_process": "SMT", "component_product_type": 2},
    }
    ui.supplier.fetch_iter.side_effect = lambda codes: iter(
        (code, metadata[code]) for code in codes
    )

    def check(ui: Any) -> None:
        frame = ui.dialog
        assert ui.controller is None
        while ui.pending_threads:
            ui.run_worker()
            pump(ui.wx)
        view, model = frame.footprint_list, frame.partlist_data_model
        item = model.ObjectToItem(model.data[model.find_index("R1")])
        view.Select(item)
        event = ui.wx.dataview.DataViewEvent(
            ui.wx.dataview.wxEVT_DATAVIEW_SELECTION_CHANGED, view, item
        )
        view.GetEventHandler().ProcessEvent(event)
        assert view.GetSelectedItemsCount() == 1
        ui.catalog.parts.update(
            C1={"stock": 7, "type": "Extended", "price": "1-:0.10"},
            C2={"stock": 9, "type": "Basic", "price": "1-:0.10"},
        )
        frame._invalidate_catalog_details()
        reads = len(ui.catalog.reads)

        frame.populate_footprint_list()
        pump(ui.wx)

        assert sorted(ui.catalog.reads[reads:]) == ["C1", "C2"]
        assert view.GetSelectedItemsCount() == 0
        columns = model.columns
        rows = {
            row[columns["REF_COL"]]: model.ObjectToItem(row) for row in model.get_all()
        }
        assert sorted(rows) == ["R1", "R2"]
        for reference, lcsc, part_type, stock in (
            ("R1", "C1", "Extended", 7),
            ("R2", "C2", "Basic", 9),
        ):
            row = model.ItemToObject(rows[reference])
            assert (row[columns["TYPE_COL"]], row[columns["STOCK_COL"]]) == (
                part_type,
                stock,
            )
            assert model.GetValue(rows[reference], columns["ENRICH_COL"]) == "Done"
            process = metadata[lcsc]["assembly_process"]
            assert f"Assembly process: {process}" in model.get_assembly_tooltip(
                rows[reference]
            )

    ui.run(check)
