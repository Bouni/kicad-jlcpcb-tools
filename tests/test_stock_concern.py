"""Stock concern follows exact supply and one board's grouped BOM demand."""

from collections.abc import Callable, Iterator
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from .stock_test_support import board_row, stock_modules
from .wx_harness import load_siblings


@pytest.fixture
def concern() -> Iterator[Callable[..., set[str]]]:
    """Load the pure calculation without initializing the KiCad plugin."""
    with load_siblings("stock_concern_tests", ("stock_concern",), {}) as loaded:
        yield loaded["stock_concern"].stock_concern_references


def part(reference: str, lcsc: str = "C1", **values: Any) -> dict[str, Any]:
    """Build a populated BOM record with concise scenario overrides."""
    return {
        "reference": reference,
        "lcsc": lcsc,
        "value": "10k",
        "footprint": "R_0603",
        "exclude_from_bom": False,
        "exclude_from_pos": False,
        "is_dnp": False,
        **values,
    }


@pytest.mark.parametrize(
    "stock, expected", [(0, True), (29, True), (30, False), (31, False)]
)
def test_strict_ten_times_grouped_board_usage(
    concern: Callable[..., set[str]], stock: object, expected: bool
) -> None:
    """Three copies need a concern below thirty, never at thirty."""
    parts = [part("R1"), part("R2"), part("R3")]
    assert concern(parts, lambda _lcsc: stock) == (
        {"R1", "R2", "R3"} if expected else set()
    )


def test_matching_lcsc_groups_across_values_and_footprints(
    concern: Callable[..., set[str]],
) -> None:
    """Formatting and catalog spellings cannot split demand for the same part."""
    parts = [
        part("R1", "C1"),
        part("R2", " c1 ", value="other", footprint="other"),
        part("R3", "C1", exclude_from_pos=True),
        part("R4", "C2"),
    ]
    get_stock = MagicMock(side_effect={"C1": 29, "C2": 10}.__getitem__)
    assert concern(parts, get_stock) == {"R1", "R2", "R3"}
    assert sorted(invocation.args for invocation in get_stock.call_args_list) == [
        ("C1",),
        ("C2",),
    ]


def test_only_populated_assigned_bom_references_count_or_highlight(
    concern: Callable[..., set[str]],
) -> None:
    """Exclude BOM-disabled, DNP and unassigned records from both outputs."""
    parts = [
        part("R1"),
        part("R2", exclude_from_bom=True),
        part("R3", is_dnp=True),
        part("R4", lcsc=""),
    ]
    assert concern(parts, lambda _lcsc: 10) == set()
    assert concern(parts, lambda _lcsc: 9) == {"R1"}


@pytest.mark.parametrize(
    "stock", [None, "", "unknown", "2k", "-1", -1, 1.5, "1.5", True]
)
def test_unknown_and_invalid_supply_never_raise_a_false_concern(
    concern: Callable[..., set[str]], stock: object
) -> None:
    """Only an exact known nonnegative integer quantity can trigger concern."""
    assert concern([part("R1")], lambda _lcsc: stock) == set()


def test_raw_integer_string_is_used_without_rounding(
    concern: Callable[..., set[str]],
) -> None:
    """Stock at the boundary stays sufficient even if display text is compact."""
    parts = [part(f"R{index}") for index in range(726)]
    assert concern(parts, lambda _lcsc: "7260") == set()
    assert concern(parts, lambda _lcsc: "7259") == {f"R{index}" for index in range(726)}


def test_empty_demand_avoids_catalog_queries(concern: Callable[..., set[str]]) -> None:
    """Nothing on the BOM cannot produce a stock concern or catalog lookup."""
    get_stock = MagicMock()
    assert concern([], get_stock) == set()
    get_stock.assert_not_called()


class CellAttr:
    """Keep actual cell attribute state for post-render assertions."""

    def __init__(self) -> None:
        self.background = None
        self.colour = None
        self.bold = False

    def SetBackgroundColour(self, colour: object) -> None:
        """Retain the background a real renderer would read."""
        self.background = colour

    def SetColour(self, colour: object) -> None:
        """Retain the foreground a real renderer would read."""
        self.colour = colour

    def SetBold(self, bold: bool) -> None:
        """Retain the font weight a real renderer would read."""
        self.bold = bold


@pytest.fixture
def models() -> Iterator[types.SimpleNamespace]:
    """Create real models under isolated wx notification scaffolding."""
    with stock_modules() as loaded:
        yield loaded


@pytest.mark.parametrize("luminance", [0.1, 0.9])
def test_concern_styles_stock_cell_only_and_preserves_side_style(
    models: types.SimpleNamespace,
    luminance: float,
) -> None:
    """Concern text contrasts with the theme without replacing native row fills."""
    models.wx.SystemSettings.GetColour = lambda _key: types.SimpleNamespace(
        GetLuminance=lambda: luminance
    )
    model = models.datamodel.PartListDataModel(1.0)
    model.AddEntry(board_row("R1", 9))
    model.AddEntry(board_row("R2", 100))
    model.set_stock_concern_refs({"R1"})
    row, sufficient = model.data
    attr = CellAttr()
    assert model.GetAttr(row, model.columns["STOCK_COL"], attr) is True
    assert attr.background is None
    assert attr.bold is True
    red, green, blue = attr.colour
    if luminance < 0.5:
        assert red >= 240 and green >= 180 and blue < 150
    else:
        assert red <= 140 and green <= 80 and blue < 50

    for column in ("REF_COL", "VALUE_COL", "FP_COL", "LCSC_COL"):
        attr = CellAttr()
        assert model.GetAttr(row, model.columns[column], attr) is False
        assert attr.background is None
    attr = CellAttr()
    assert model.GetAttr(sufficient, model.columns["STOCK_COL"], attr) is False
    assert attr.background is None
    attr = CellAttr()
    assert model.GetAttr(row, model.columns["SIDE_COL"], attr) is True
    assert (attr.colour, attr.bold, attr.background) == ((200, 52, 52), True, None)


def test_concern_notifications_cover_changed_cells_and_clear_stale_state(
    models: types.SimpleNamespace,
) -> None:
    """Set changes repaint gained and lost concerns, including disabled state."""
    model = models.datamodel.PartListDataModel(1.0)
    model.AddEntry(board_row("R1", 9))
    model.AddEntry(board_row("R2", 9))
    column = model.columns["STOCK_COL"]
    model.notifications.clear()
    model.set_stock_concern_refs({"R1"})
    assert model.notifications == [("value", (model.data[0], column))]
    model.notifications.clear()
    model.set_stock_concern_refs({"R1"})
    assert model.notifications == []
    model.set_stock_concern_refs({"R2"})
    assert model.notifications == [
        ("value", (model.data[0], column)),
        ("value", (model.data[1], column)),
    ]
    model.set_stock_concern_refs(set())
    assert model.stock_concern_refs == set()
    attr = CellAttr()
    assert model.GetAttr(model.data[1], column, attr) is False
    model.set_stock_concern_refs({"R1"})
    model.RemoveAll()
    assert model.stock_concern_refs == set()


@pytest.mark.parametrize("simplify", [False, True])
@pytest.mark.parametrize("highlight", [False, True])
def test_stock_presentation_flags_are_independent(
    models: types.SimpleNamespace, simplify: bool, highlight: bool
) -> None:
    """The four combinations change text and cell styling independently."""
    model = models.datamodel.PartListDataModel(1.0)
    model.AddEntry(board_row("R1", 7260))
    model.set_simplify_stock(simplify)
    model.set_stock_concern_refs({"R1"} if highlight else set())
    column = model.columns["STOCK_COL"]
    assert model.GetValue(model.data[0], column) == ("7.2k" if simplify else "7260")
    attr = CellAttr()
    assert model.GetAttr(model.data[0], column, attr) is highlight
    assert attr.background is None
    assert attr.bold is highlight
    assert model.data[0][column] == 7260
