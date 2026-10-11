"""The review pane groups findings, follows the selection, and defaults to Cancel."""

from collections.abc import Callable, Iterator, Sequence
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import pump, run_native, wait_until
from .wx_harness import load_siblings

pytestmark = pytest.mark.native_wx

DRC_MESSAGES = (
    "[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; "
    "actual 0.1500 mm)",
    "[courtyards_overlap]: Courtyards overlap",
    "[track_dangling]: Track has unconnected end",
)


@pytest.fixture
def native() -> Iterator[SimpleNamespace]:
    """Load the real pane and findings modules against the installed wx."""
    if os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1":
        pytest.skip("native wx checks require an enabled desktop session")
    wx = pytest.importorskip("wx")
    with load_siblings(
        "_generation_review_native",
        ("generation_review", "generation_review_dialog"),
        {},
    ) as modules:
        yield SimpleNamespace(
            wx=wx,
            review=modules["generation_review"],
            pane=modules["generation_review_dialog"],
        )


def _findings(
    native: SimpleNamespace, drc_messages: Sequence[str] = DRC_MESSAGES
) -> list[Any]:
    """Return findings in the order generation collects them, plus one info."""
    review = native.review
    return [
        *review.value_mismatch_findings(
            {"C14663": {"100n": ["C3", "C4"], "0.1uF": ["C7"]}}
        ),
        *review.placeholder_findings(0),
        *review.empty_zone_findings(["GND on B.Cu"]),
        *review.drc_findings(len(drc_messages), drc_messages),
        review.Finding(
            review.Severity.INFO,
            "JLC footprint",
            "Q7: JLC's pin-1 mark sits on another pin; the rotation is right",
            "JLC numbers this footprint's pins differently.",
            ("Q7",),
        ),
    ]


def _show(wx: Any, dialog: Any, interact: Callable[[], None]) -> int:
    """Run ``interact`` inside the real modal loop and return how the pane closed."""
    failures: list[BaseException] = []

    def inspect() -> None:
        try:
            interact()
        except BaseException as error:
            failures.append(error)
        finally:
            if dialog.IsModal():
                dialog.EndModal(wx.ID_ABORT)

    timer = wx.CallLater(1, inspect)
    try:
        result = dialog.ShowModal()
    finally:
        timer.Stop()
    if failures:
        raise failures[0]
    return result


def _opened(wx: Any, dialog: Any) -> None:
    """Let the shown pane settle, so focus and first paint have happened."""
    wait_until(wx, dialog.IsShownOnScreen, reason="The pane was not shown")
    settled = time.monotonic() + 0.3
    wait_until(wx, lambda: time.monotonic() >= settled)


def _select(wx: Any, dialog: Any, row: int) -> None:
    """Select a row and deliver the selection event the user's click sends."""
    control = dialog.findings_list
    if row == wx.NOT_FOUND:
        control.UnselectAll()
        item = wx.dataview.NullDataViewItem
    else:
        control.SelectRow(row)
        item = control.RowToItem(row)
    event = wx.dataview.DataViewEvent(
        wx.dataview.wxEVT_DATAVIEW_SELECTION_CHANGED, control, item
    )
    control.GetEventHandler().ProcessEvent(event)


def _rows(dialog: Any) -> list[tuple[str, str]]:
    """Read both columns of every row from the native control."""
    control = dialog.findings_list
    return [
        (control.GetValue(row, 0).GetText(), control.GetTextValue(row, 1))
        for row in range(control.GetItemCount())
    ]


def test_rows_group_under_headings_and_opening_selects_nothing(
    native: SimpleNamespace,
) -> None:
    """Serious, warnings, then info; nothing is selected as the pane opens."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.pane.GenerationReviewDialog(frame, _findings(native))

        def interact() -> None:
            _opened(wx, dialog)
            assert dialog.GetTitle() == "Review before generating"
            assert dialog.summary.GetLabel() == "2 serious issues, 2 warnings, 1 info"
            assert _rows(dialog) == [
                ("Serious issues", ""),
                ("Copper zones", "Copper zone GND on B.Cu has no fill"),
                ("DRC", "DRC found 3 error violations"),
                ("Warnings", ""),
                ("Part values", "C14663: 100n on C3, C4; 0.1uF on C7"),
                (
                    "Order number",
                    "No order-number placeholder (JLCJLCJLCJLC) on silkscreen",
                ),
                ("Info", ""),
                (
                    "JLC footprint",
                    "Q7: JLC's pin-1 mark sits on another pin; the rotation is right",
                ),
            ]
            control = dialog.findings_list
            assert all(
                control.GetValue(row, 0).GetIcon().IsOk()
                for row in range(control.GetItemCount())
            )
            model = dialog.findings_list.GetModel()
            bold = []
            for row in range(dialog.findings_list.GetItemCount()):
                attr = wx.dataview.DataViewItemAttr()
                if model.GetAttr(dialog.findings_list.RowToItem(row), 0, attr):
                    bold.append(row if attr.GetBold() else None)
            assert bold == [0, 3, 6]
            assert dialog.findings_list.GetSelectedRow() == wx.NOT_FOUND
            assert dialog.detail.GetValue() == native.pane.NO_SELECTION
            assert wx.Window.FindFocus() is dialog.cancel_button

        dialog.CentreOnParent()
        try:
            assert _show(wx, dialog, interact) == wx.ID_ABORT
        finally:
            dialog.Destroy()

    run_native(exercise)


def test_selecting_a_row_shows_its_detail(native: SimpleNamespace) -> None:
    """A finding shows its detail; a heading or no selection shows the prompt."""

    def exercise(frame: Any, wx: Any) -> None:
        findings = _findings(native)
        dialog = native.pane.GenerationReviewDialog(frame, findings)
        details: list[str] = []

        def interact() -> None:
            _opened(wx, dialog)
            for row in (4, 0, 2, wx.NOT_FOUND, 7):
                _select(wx, dialog, row)
                details.append(dialog.detail.GetValue())

        try:
            _show(wx, dialog, interact)
        finally:
            dialog.Destroy()
        assert details == [
            native.review.detail_text(findings[0]),
            native.pane.NO_SELECTION,
            native.review.detail_text(findings[3]),
            native.pane.NO_SELECTION,
            native.review.detail_text(findings[4]),
        ]
        assert details[0].endswith("Parts: C3, C4, C7")

    run_native(exercise)


def test_dozens_of_drc_messages_scroll_in_the_detail(
    native: SimpleNamespace,
) -> None:
    """The row stays one line; the messages scroll inside the detail box."""
    messages = [
        f"[clearance]: Clearance violation (netclass 'Default'; actual {index} um)"
        for index in range(60)
    ]

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.pane.GenerationReviewDialog(frame, _findings(native, messages))

        def interact() -> None:
            _opened(wx, dialog)
            size = dialog.GetSize()
            _select(wx, dialog, 2)
            pump(wx)
            assert _rows(dialog)[2] == ("DRC", "DRC found 60 error violations")
            text = dialog.detail.GetValue()
            assert text.splitlines()[-60:] == messages
            assert dialog.detail.GetNumberOfLines() >= 62
            visible = dialog.detail.GetClientSize().height
            assert visible < dialog.detail.GetCharHeight() * 62
            assert dialog.GetSize() == size

        try:
            _show(wx, dialog, interact)
        finally:
            dialog.Destroy()

    run_native(exercise)


@pytest.mark.parametrize(
    ("close", "expected"),
    [
        ("escape", "ID_CANCEL"),
        ("close box", "ID_CANCEL"),
        ("default button", "ID_CANCEL"),
        ("generate anyway", "ID_OK"),
    ],
)
def test_escape_close_box_and_default_button_cancel(
    native: SimpleNamespace, close: str, expected: str
) -> None:
    """Only the Generate anyway button returns wx.ID_OK."""

    def exercise(frame: Any, wx: Any) -> None:
        dialog = native.pane.GenerationReviewDialog(frame, _findings(native))

        def interact() -> None:
            _opened(wx, dialog)
            assert dialog.GetEscapeId() == wx.ID_CANCEL
            assert dialog.GetDefaultItem() is dialog.cancel_button
            assert dialog.generate_button.GetLabel() == "Generate anyway"
            if close == "escape":
                event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                event.SetKeyCode(wx.WXK_ESCAPE)
                event.SetEventObject(dialog)
                dialog.GetEventHandler().ProcessEvent(event)
            elif close == "close box":
                dialog.Close()
            else:
                control = (
                    dialog.GetDefaultItem()
                    if close == "default button"
                    else dialog.generate_button
                )
                event = wx.CommandEvent(wx.wxEVT_BUTTON, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)

        try:
            assert _show(wx, dialog, interact) == getattr(wx, expected)
        finally:
            dialog.Destroy()

    run_native(exercise)
