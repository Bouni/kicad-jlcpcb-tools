"""A part selector header click fetches the catalog again in that column's order."""

from collections.abc import Iterator
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import test_window_layout as layout_ui
from .wx_harness import load_correction_modules

# The selector constructor needs its module's autouse board and callback cleanup,
# which pytest does not discover through a module import.
layout_lifecycle = layout_ui._clear_callbacks
partselector = layout_ui.partselector
COLUMN_INDEX = layout_ui.schema.COLUMN_INDEX

# Mirrors common/partsdb.py.
_CREATE_PARTS = """
    CREATE virtual TABLE IF NOT EXISTS parts using fts5 (
        'LCSC Part', 'First Category', 'Second Category', 'MFR.Part', 'Package',
        'Solder Joint' unindexed, 'Manufacturer', 'Library Type', 'Description',
        'Datasheet' unindexed, 'Price' unindexed, 'Stock' unindexed
    , tokenize="trigram")
"""
# Two parts more than a search returns. By LCSC number the cheapest part comes
# last, so only a search ordered by price can return it. The parts without a
# price, one blank and one listed at zero, come first by number and last by
# price in either direction.
UNPRICED, ZERO_PRICED, CHEAPEST, DEAREST = "C1000", "C1001", "C2001", "C2000"
SEARCH_LIMIT = 1000
CATALOG_SIZE = SEARCH_LIMIT + 2


def _price(index: int) -> str:
    if index == 0:
        return ""
    if index == 1:
        return "1-:0.000"
    if index == CATALOG_SIZE - 1:
        return "1-:0.001"
    return f"1-:{0.01 + index / 1000:.4f}"


def _value_control(
    _parent: Any, _id: int, value: Any = "", *_args: Any, **_kwargs: Any
) -> MagicMock:
    """Keep a control's value as the native text, combo and check boxes do."""
    control = MagicMock()
    control.GetValue.return_value = value

    def store(new_value: Any) -> None:
        control.GetValue.return_value = new_value

    control.SetValue.side_effect = store
    control.ChangeValue.side_effect = store
    return control


@pytest.fixture
def catalog(tmp_path: Any) -> Iterator[Any]:
    """Build a real Library over a catalog larger than a search returns."""
    partsdb = tmp_path / "parts.db"
    with sqlite3.connect(partsdb) as con:
        con.execute(_CREATE_PARTS)
        con.executemany(
            "INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"C{1000 + index}",
                    "Resistors",
                    "Chip Resistor - Surface Mount",
                    f"RC0603-{index}",
                    "0603",
                    "2",
                    "Yageo",
                    "Basic",
                    "10kΩ resistor",
                    "",
                    _price(index),
                    "5000",
                )
                for index in range(CATALOG_SIZE)
            ],
        )
    con.close()
    with load_correction_modules() as loaded:
        library = object.__new__(loaded.library.Library)
        library.logger = MagicMock()
        library.partsdb_file = str(partsdb)
        yield library


@pytest.fixture
def selector(
    monkeypatch: pytest.MonkeyPatch, catalog: Any
) -> Iterator[tuple[Any, list[str]]]:
    """Open the real selector over the real catalog, recording each query."""
    statements: list[str] = []
    connect = sqlite3.connect

    def record(statement: str) -> None:
        # FTS5 traces its own internal reads too; keep only the searches.
        if " FROM parts WHERE " in statement:
            statements.append(statement)

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        con = connect(*args, **kwargs)
        con.set_trace_callback(record)
        return con

    monkeypatch.setattr(sqlite3, "connect", traced)
    for name in ("TextCtrl", "ComboBox", "CheckBox"):
        monkeypatch.setattr(
            getattr(partselector.wx, name), "side_effect", _value_control
        )
    parent = SimpleNamespace(
        window=layout_ui._Dialog(),
        display_index=0,
        scale_factor=2,
        settings={"partselector": {}},
        library=SimpleNamespace(
            categories=[],
            search=catalog.search,
            is_download_running=lambda: False,
        ),
        is_catalog_available=lambda: True,
        save_settings=MagicMock(),
        _part_selector=None,
    )
    parent.window.scale_factor = 2
    parent.window.display_index = 0
    opened = layout_ui._open_selector(monkeypatch, {}, parent=parent, real_search=True)
    opened.keyword.ChangeValue("resistor")
    yield opened, statements
    layout_ui._drain_callbacks()


def _shown(selector: Any, key: str) -> list[str]:
    """Return one column of the rows the last search handed the view."""
    (rows,), _ = selector.part_list_model.ReplaceAll.call_args
    return [row[COLUMN_INDEX[key]] for row in rows]


def _shown_lcsc(selector: Any) -> list[str]:
    """Return the LCSC numbers of the rows the last search handed the view."""
    return _shown(selector, "lcsc")


def _sort(selector: Any, key: str, ascending: bool) -> None:
    """Sort a column as wx does, then deliver its sorted event and timer."""
    view = selector.part_list
    column = layout_ui._column_by_model(view, COLUMN_INDEX[key])
    column.IsSortOrderAscending.return_value = ascending
    view.GetSortingColumn.return_value = column
    (handler,) = [
        call.args[1]
        for call in view.Bind.call_args_list
        if call.args[0] == partselector.dv.EVT_DATAVIEW_COLUMN_SORTED
    ]
    timer = selector.search_timer
    timer.StartOnce.reset_mock()
    event = MagicMock()
    handler(event)
    event.Skip.assert_called_once_with()
    if timer.StartOnce.called:
        # wx runs the dialog's timer, bound to search, once the sort returns.
        timer.StartOnce.assert_called_once_with(1)
        selector.search()


def test_price_header_fetches_the_cheapest_parts_with_the_price_collation(
    selector: tuple[Any, list[str]],
) -> None:
    """Price order picks which parts the search returns, unpriced ones last."""
    view, statements = selector
    view.search()
    shown = _shown_lcsc(view)
    assert len(shown) == SEARCH_LIMIT
    assert shown[0] == UNPRICED
    assert CHEAPEST not in shown
    assert 'ORDER BY "LCSC Part" COLLATE naturalsort ASC' in statements[-1]

    for ascending, ends, collation in (
        (True, (CHEAPEST, DEAREST), "pricesort_asc"),
        (False, (DEAREST, CHEAPEST), "pricesort_desc"),
    ):
        _sort(view, "price", ascending=ascending)
        shown = _shown_lcsc(view)
        assert (shown[0], shown[-1]) == ends
        assert UNPRICED not in shown and ZERO_PRICED not in shown
        assert "Error in price data" not in _shown(view, "price")
        assert f'ORDER BY "Price" COLLATE {collation} ASC' in statements[-1]


def test_catalog_column_order_holds_for_later_searches(
    selector: tuple[Any, list[str]],
) -> None:
    """A new search keeps the header's order; descending text sorts naturally."""
    view, statements = selector
    _sort(view, "lcsc", ascending=False)
    assert _shown_lcsc(view)[:2] == [CHEAPEST, DEAREST]
    assert 'ORDER BY "LCSC Part" COLLATE naturalsort DESC' in statements[-1]

    view.keyword.ChangeValue("10kΩ resistor")
    view.search()
    assert _shown_lcsc(view)[0] == CHEAPEST
    assert 'ORDER BY "LCSC Part" COLLATE naturalsort DESC' in statements[-1]


def test_a_column_without_a_catalog_field_keeps_the_shown_rows(
    selector: tuple[Any, list[str]],
) -> None:
    """A column the catalog can't order by keeps the view's sort of the shown rows.

    Every sortable column has a catalog field today; this covers one that
    doesn't, as Params would be if it were made sortable again.
    """
    view, statements = selector
    _sort(view, "price", ascending=True)
    queries = len(statements)

    _sort(view, "params", ascending=False)
    view.search_timer.StartOnce.assert_not_called()
    assert len(statements) == queries

    view.search()
    assert 'ORDER BY "LCSC Part" COLLATE naturalsort ASC' in statements[-1]


@pytest.mark.parametrize(
    ("first", "second", "order"),
    [
        ("Nexperia", "NEXPERIA", 0),
        ("Texas Instruments", "TI", -1),
        ("C2", "C10", -1),
        ("R9", "r10", -1),
        ("", "Yageo", -1),
    ],
)
def test_catalog_text_order_is_consistent_in_both_argument_orders(
    first: str, second: str, order: int
) -> None:
    """Names that differ only in case compare equal, and a < b means b > a."""
    with load_correction_modules() as loaded:
        collate = loaded.library.natural_sort_collation
        assert (collate(first, second), collate(second, first)) == (order, -order)


def test_catalog_rejects_an_unknown_order() -> None:
    """The order is written into the SQL, so only catalog columns are accepted."""
    with load_correction_modules() as loaded:
        library = object.__new__(loaded.library.Library)
        with pytest.raises(ValueError, match="Cannot order"):
            library.search(
                {
                    "keyword": "resistor",
                    "basic": False,
                    "extended": False,
                    "preferred": False,
                    "stock": False,
                    "order_by": 'Price" --',
                }
            )
