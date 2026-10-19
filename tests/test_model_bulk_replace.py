"""Whole-list fills reach the view as one reset rather than one add per row.

A main-list refill that keeps the rows shown rewrites them in place instead,
since a reset scrolls a macOS list back to its first row.
"""

from typing import Any

import pytest

from .stock_test_support import board_row, selector_row, stock_modules

PARTS = {
    "R1": {"lcsc": "C1", "component_product_type": 2, "assembly_process": "SMT"},
    "R2": {"lcsc": "C1", "component_product_type": 1, "assembly_process": "SMT"},
    "R3": {"lcsc": ""},
}


def _board_entries() -> list:
    """Return fresh raw rows with the store metadata the main window supplies."""
    return [
        (board_row(reference, "7260", lcsc=part["lcsc"]), part, reference == "R2")
        for reference, part in PARTS.items()
    ]


def _changed_board_entries() -> list:
    """Return the board rows with R1's stock and R2's classification changed."""
    entries = _board_entries()
    entries[0][0][5] = "9999"
    row, part, pending = entries[1]
    entries[1] = (row, {**part, "component_product_type": 2}, pending)
    return entries


def _filled(modules: Any, entries: list) -> Any:
    """Return a main-list model filled once from empty."""
    model = modules.datamodel.PartListDataModel(1.0)
    model.ReplaceAll(entries)
    return model


def test_selector_replace_all_swaps_rows_with_one_reset() -> None:
    """The selector publishes a complete result with a single Cleared()."""
    with stock_modules() as modules:
        model = modules.datamodel.PartSelectorDataModel()
        model.AddEntry(selector_row("1", lcsc="C9"))
        rows = [selector_row(str(stock), lcsc=f"C{stock}") for stock in range(3)]
        model.notifications.clear()

        model.ReplaceAll(rows)

        assert model.data == rows
        assert model.notifications == [("cleared", ())]


def test_board_replace_all_matches_a_row_by_row_fill_with_one_reset() -> None:
    """Rows, metadata and cleared state equal the incremental path's result."""
    with stock_modules() as modules:
        incremental = modules.datamodel.PartListDataModel(1.0)
        for row, part, pending in _board_entries():
            incremental.AddEntry(row)
            incremental.set_assembly_metadata(row[0], part, pending=pending)

        bulk = modules.datamodel.PartListDataModel(1.0)
        bulk.AddEntry(board_row("R9", "1"))
        bulk.set_assembly_metadata("R9", {"component_product_type": 2})
        bulk.set_standard_only_refs({"R9"})
        bulk.set_stock_concern_refs({"R9"})
        bulk.notifications.clear()

        bulk.ReplaceAll(_board_entries())

        assert bulk.data == incremental.data
        assert [bulk.get_assembly_tooltip(row) for row in bulk.data] == [
            incremental.get_assembly_tooltip(row) for row in incremental.data
        ]
        assert bulk.standard_only_refs == set()
        assert bulk.stock_concern_refs == set()
        assert "R9" not in bulk._assembly_metadata
        assert bulk.notifications == [("cleared", ())]


def test_remove_all_is_an_empty_replacement() -> None:
    """Emptying either model still resets the view exactly once."""
    with stock_modules() as modules:
        board = modules.datamodel.PartListDataModel(1.0)
        board.ReplaceAll(_board_entries())
        selector = modules.datamodel.PartSelectorDataModel()
        selector.ReplaceAll([selector_row("1")])
        for model in (board, selector):
            model.notifications.clear()

            model.RemoveAll()

            assert model.data == []
            assert model.notifications == [("cleared", ())]


def test_board_replace_all_with_the_shown_references_rewrites_rows_in_place() -> None:
    """Refilling unchanged rows keeps every row object and repaints nothing."""
    with stock_modules() as modules:
        model = _filled(modules, _board_entries())
        rows = list(model.data)
        model.notifications.clear()

        model.ReplaceAll(_board_entries())

        assert len(model.data) == len(rows)
        assert all(new is old for new, old in zip(model.data, rows))
        assert model.notifications == []


def test_board_replace_all_in_place_repaints_only_changed_rows() -> None:
    """A changed cell or assembly fact repaints its row; the state equals a reset's."""
    with stock_modules() as modules:
        model = _filled(modules, _board_entries())
        rows = list(model.data)
        model.notifications.clear()

        model.ReplaceAll(_changed_board_entries())

        assert model.notifications == [
            ("changed", (rows[0],)),
            ("changed", (rows[1],)),
        ]
        reset = _filled(modules, _changed_board_entries())
        assert model.data == reset.data
        assert [model.get_assembly_tooltip(row) for row in model.data] == [
            reset.get_assembly_tooltip(row) for row in reset.data
        ]


def test_board_replace_all_in_place_clears_marks_and_repaints_their_rows() -> None:
    """Standard-only and stock-concern marks clear as on a reset, repainting rows."""
    with stock_modules() as modules:
        model = _filled(modules, _board_entries())
        model.set_standard_only_refs({"R1"})
        model.set_stock_concern_refs({"R3"})
        rows = list(model.data)
        model.notifications.clear()

        model.ReplaceAll(_board_entries())

        assert model.standard_only_refs == set()
        assert model.stock_concern_refs == set()
        assert model.notifications == [
            ("changed", (rows[0],)),
            ("changed", (rows[2],)),
        ]


@pytest.mark.parametrize(
    "references",
    [["R2", "R1", "R3"], ["R1", "R2"], ["R1", "R2", "R3", "R4"]],
    ids=["reordered", "removed", "added"],
)
def test_board_replace_all_resets_when_the_references_differ(
    references: list[str],
) -> None:
    """Reordered, removed or added rows still reach the view as one reset."""
    with stock_modules() as modules:
        model = _filled(modules, _board_entries())
        model.notifications.clear()

        model.ReplaceAll(
            [(board_row(ref, "7260"), {"lcsc": "C1"}, False) for ref in references]
        )

        assert [row[0] for row in model.data] == references
        assert model.notifications == [("cleared", ())]
