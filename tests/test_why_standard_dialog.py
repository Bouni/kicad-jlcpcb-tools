"""Native dialog regressions selected by the required wx test lane."""

from __future__ import annotations

from collections.abc import Iterator
import os
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1"
        and os.environ.get("KICAD_NATIVE_WX_TESTS") != "1",
        reason="Native wx tests require an enabled desktop session",
    ),
]


@pytest.fixture
def native_dialog() -> Iterator[SimpleNamespace]:
    """Load real siblings and construct actual controls without starting KiCad."""
    wx = pytest.importorskip("wx")
    dv = pytest.importorskip("wx.dataview")
    app = wx.App.Get() or wx.App(False)
    with load_siblings(
        "_why_standard_native",
        ("why_standard_dialog", "bom_estimation.assembly_mode"),
        {},
    ) as modules:
        parent = wx.Frame(None)
        parent.window = parent
        parent._why_standard_dialog = None
        yield SimpleNamespace(
            wx=wx,
            dv=dv,
            app=app,
            parent=parent,
            Dialog=modules["why_standard_dialog"].WhyStandardDialog,
            Decision=modules["bom_estimation.assembly_mode"].AssemblyModeDecision,
        )
        parent.Destroy()
        app.ProcessPendingEvents()


def _decision(native: SimpleNamespace, *, swapped: bool = False) -> Any:
    """Keep both sides affected while moving the same references between sides."""
    return native.Decision(
        board_count=2,
        top_refs={"R10" if swapped else "R2"},
        bottom_refs={"R2" if swapped else "R10"},
        standard_only_refs={"R2"},
    )


def _parts() -> list[dict[str, str]]:
    """Supply reversed reference order and distinguishable ordinary cell data."""
    return [
        {"reference": "R10", "value": "BOT", "lcsc": "C10"},
        {"reference": "R2", "value": "TOP", "lcsc": "C2"},
    ]


def _tables(native: SimpleNamespace, dialog: Any) -> list[Any]:
    """Discover controls created by the production constructor or refresh path."""
    return [
        child
        for child in dialog.content_panel.GetChildren()
        if isinstance(child, native.dv.DataViewListCtrl)
    ]


def _assert_sides(native: SimpleNamespace, table: Any, sides: tuple[str, str]) -> None:
    """Use native GetAttr dispatch on the associated model, as rendering does."""
    model = table.GetModel()
    assert table.GetItemCount() == 2
    for row, side in enumerate(sides):
        assert table.GetTextValue(row, 4) == side
        attr = native.dv.DataViewItemAttr()
        assert model.GetAttr(table.RowToItem(row), 4, attr), (
            f"Affected {side} Side cell has no custom attributes"
        )
        expected = (200, 52, 52) if side == "TOP" else (77, 127, 196)
        assert attr.GetColour().Get(False) == expected
        assert attr.GetBold()
        assert not attr.HasBackgroundColour()


def test_affected_table_styles_native_side_cells(
    native_dialog: SimpleNamespace,
) -> None:
    """The actual dialog styles Side cells while retaining its ordinary columns."""
    native = native_dialog
    dialog = native.Dialog(native.parent, _decision(native), _parts())
    (table,) = _tables(native, dialog)
    _assert_sides(native, table, ("TOP", "BOT"))
    assert [table.GetColumn(index).GetTitle() for index in range(5)] == [
        "Relationship",
        "Ref",
        "Value",
        "LCSC",
        "Side",
    ]
    assert [table.GetTextValue(0, index) for index in range(5)] == [
        "Standard Only; Both-side context",
        "R2",
        "TOP",
        "C2",
        "TOP",
    ]
    assert [table.GetTextValue(1, index) for index in range(5)] == [
        "Both-side context",
        "R10",
        "BOT",
        "C10",
        "BOT",
    ]
    for column in range(5):
        assert (
            table.GetColumn(column).GetRenderer().GetMode()
            == native.dv.DATAVIEW_CELL_INERT
        )
        if column != 4:
            attr = native.dv.DataViewItemAttr()
            assert not table.GetModel().GetAttr(table.RowToItem(0), column, attr)
            assert attr.IsDefault()
    for unknown_side in ("", "UNKNOWN"):
        table.SetTextValue(unknown_side, 0, 4)
        attr = native.dv.DataViewItemAttr()
        assert not table.GetModel().GetAttr(table.RowToItem(0), 4, attr)
        assert attr.IsDefault()


def test_dialog_refresh_replaces_sides_and_recreates_table(
    native_dialog: SimpleNamespace,
) -> None:
    """Refreshing a live dialog cannot retain old colors or an obsolete table."""
    native = native_dialog
    dialog = native.Dialog(native.parent, _decision(native), _parts())
    dialog.update_content(_decision(native, swapped=True), _parts())
    (table,) = _tables(native, dialog)
    _assert_sides(native, table, ("BOT", "TOP"))
    dialog.update_content(native.Decision(board_count=2), [])
    assert not _tables(native, dialog)
    dialog.update_content(_decision(native), _parts())
    (table,) = _tables(native, dialog)
    _assert_sides(native, table, ("TOP", "BOT"))


def test_close_event_clears_singleton_and_reopening_uses_current_sides(
    native_dialog: SimpleNamespace,
) -> None:
    """The bound native close event clears the old dialog before a fresh open."""
    native = native_dialog
    dialog = native.Dialog(native.parent, _decision(native), _parts())
    native.parent._why_standard_dialog = dialog
    dialog._request_close()
    assert native.parent._why_standard_dialog is None
    native.app.ProcessPendingEvents()
    reopened = native.Dialog(native.parent, _decision(native, swapped=True), _parts())
    native.parent._why_standard_dialog = reopened
    (table,) = _tables(native, reopened)
    _assert_sides(native, table, ("BOT", "TOP"))
