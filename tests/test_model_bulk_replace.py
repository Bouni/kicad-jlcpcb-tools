"""Whole-list fills reach the view as one reset rather than one add per row."""

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
