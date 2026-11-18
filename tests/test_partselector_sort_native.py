"""Sorting a real part selector column searches the catalog in that order."""

from typing import Any, Optional

import pytest

from .native_window_support import select_result, window_ui
from .native_wx_support import pump, wait_until
from .test_part_list_refill_native import displayed_lcsc, part_buttons_enabled

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx

PRICES = {"C1": "1-:0.30", "C2": "1-:0.10", "C3": "", "C4": "1-:0.20"}
# Unpriced parts come last in either direction.
PRICE_ORDER = {True: ["C2", "C4", "C1", "C3"], False: ["C1", "C4", "C2", "C3"]}


def open_selector(
    ui: Any, limit: Optional[int] = None
) -> tuple[Any, list[dict[str, Any]]]:
    """Open the selector over a catalog that answers in the requested order.

    With a limit, the catalog returns only that many parts, as a search
    returns only its first 1000.
    """
    selector = ui.selector_module.PartSelectorDialog(ui.dialog, {"R1": "C1"})
    ui.dialog._part_selector = selector
    selector.Show()
    pump(ui.wx)
    fields = ui.selector_module.DB_FIELDS
    searches: list[dict[str, Any]] = []

    def search(parameters: dict[str, Any]) -> list[tuple[str, ...]]:
        searches.append(dict(parameters))
        if parameters.get("order_by") == "Price":
            order = PRICE_ORDER[parameters["ascending"]]
        else:
            order = sorted(PRICES)
        return [
            tuple(
                {
                    "LCSC Part": lcsc,
                    "Library Type": "Basic",
                    "Stock": "10",
                    "Price": PRICES[lcsc],
                }.get(field, "")
                for field in fields
            )
            for lcsc in order[:limit]
        ]

    ui.catalog.search.side_effect = search
    return selector, searches


def model_column(ui: Any, key: str) -> int:
    """Return the model column of a part selector column."""
    return next(
        position
        for position, column in enumerate(ui.selector_module.PARTSELECTOR_COLUMNS)
        if column.key == key
    )


def price_column(ui: Any, view: Any) -> Any:
    """Return the view column that shows the price."""
    index = model_column(ui, "price")
    (column,) = (
        view.GetColumn(position)
        for position in range(view.GetColumnCount())
        if view.GetColumn(position).GetModelColumn() == index
    )
    return column


@pytest.mark.parametrize("ascending", [True, False], ids=["ascending", "descending"])
def test_sorted_price_column_searches_in_its_order(
    window_ui: Any, ascending: bool
) -> None:
    """The search after a sort asks for the order the header shows."""

    def check(ui: Any) -> None:
        selector, searches = open_selector(ui)
        view, model = selector.part_list, selector.part_list_model
        selector.search(None)
        assert "order_by" not in searches[-1]

        column = price_column(ui, view)
        column.SetSortOrder(ascending)
        event = ui.wx.dataview.DataViewEvent(
            ui.wx.dataview.wxEVT_DATAVIEW_COLUMN_SORTED, view, column
        )
        view.GetEventHandler().ProcessEvent(event)
        count = len(searches)
        wait_until(
            ui.wx,
            lambda: len(searches) > count,
            reason="Sorting the price column did not search again",
        )
        assert (searches[-1]["order_by"], searches[-1]["ascending"]) == (
            "Price",
            ascending,
        )
        pump(ui.wx)
        assert (
            displayed_lcsc(view, model, model_column(ui, "lcsc"))
            == PRICE_ORDER[ascending]
        )
        selector.Close()

    window_ui.run(check)


@pytest.mark.parametrize(
    ("selected", "kept"), [("C2", True), ("C3", False)], ids=["shown", "cut-off"]
)
def test_price_sort_keeps_the_selected_part_while_it_is_shown(
    window_ui: Any, selected: str, kept: bool
) -> None:
    """The selection survives a sort that still shows its part, and only then."""

    def check(ui: Any) -> None:
        # Three of four parts fit: by number C4 is cut off, by price unpriced C3.
        selector, searches = open_selector(ui, limit=3)
        view, model = selector.part_list, selector.part_list_model
        selector.search(None)
        pump(ui.wx)
        select_result(ui, selector, selected)

        column = price_column(ui, view)
        column.SetSortOrder(True)
        event = ui.wx.dataview.DataViewEvent(
            ui.wx.dataview.wxEVT_DATAVIEW_COLUMN_SORTED, view, column
        )
        view.GetEventHandler().ProcessEvent(event)
        count = len(searches)
        wait_until(
            ui.wx,
            lambda: len(searches) > count,
            reason="Sorting the price column did not search again",
        )
        pump(ui.wx)
        assert displayed_lcsc(view, model, model_column(ui, "lcsc")) == [
            "C2",
            "C4",
            "C1",
        ]
        if kept:
            assert view.GetSelectedItemsCount() == 1
            assert model.get_lcsc(view.GetSelection()) == selected
            assert part_buttons_enabled(selector) == (True, True)
        else:
            assert view.GetSelectedItemsCount() == 0
            assert part_buttons_enabled(selector) == (False, False)

        # A typed search still starts with nothing selected.
        if kept:
            selector.search(None)
            pump(ui.wx)
            assert view.GetSelectedItemsCount() == 0
        selector.Close()

    window_ui.run(check)


@pytest.mark.os_input
def test_clicking_the_price_header_searches_in_each_direction(
    window_ui: Any,
) -> None:
    """Real header clicks search in the direction their indicator shows."""

    def check(ui: Any) -> None:
        wx = ui.wx
        selector, searches = open_selector(ui)
        view, model = selector.part_list, selector.part_list_model
        selector.search(None)
        pump(wx)
        column = price_column(ui, view)
        selector.Raise()
        wait_until(wx, selector.IsActive, reason="Part selector did not activate")
        simulator = wx.UIActionSimulator()

        def click_header() -> None:
            # The header spans the top of the control, above the first row.
            left = sum(
                view.GetColumn(position).GetWidth()
                for position in range(view.GetColumnPosition(column))
            )
            first_row = view.GetItemRect(model.ObjectToItem(model.data[0]))
            assert first_row.y > 0, "The header has no height above the first row"
            point = view.ClientToScreen(
                wx.Point(left + column.GetWidth() // 2, first_row.y // 2)
            )
            assert simulator.MouseMove(point.x, point.y)
            assert simulator.MouseClick()

        for ascending in (True, False):
            count = len(searches)
            click_header()
            wait_until(
                wx,
                lambda count=count: len(searches) > count,
                reason="Clicking the price header did not search again",
            )
            assert (searches[-1]["order_by"], searches[-1]["ascending"]) == (
                "Price",
                ascending,
            )
            assert column.IsSortKey()
            assert column.IsSortOrderAscending() is ascending
            pump(wx)
            assert (
                displayed_lcsc(view, model, model_column(ui, "lcsc"))
                == PRICE_ORDER[ascending]
            )
        selector.Close()

    window_ui.run(check)
