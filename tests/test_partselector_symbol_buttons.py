"""The Ω and µ buttons type at the cursor and hand the keyword box back."""

from typing import Any

import pytest

from .native_window_support import button, window_ui
from .native_wx_support import pump, wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def open_selector(ui: Any) -> Any:
    """Show the production part selector with one prefilled part number."""
    selector = ui.selector_module.PartSelectorDialog(ui.dialog, {"R1": "C1"})
    ui.dialog._part_selector = selector
    selector.Show()
    selector.Raise()
    pump(ui.wx)
    return selector


def test_a_prefilled_box_gets_the_symbol_at_its_end(window_ui: Any) -> None:
    """Until the cursor is placed, the symbol goes where AppendText put it."""

    def check(ui: Any) -> None:
        selector = open_selector(ui)
        assert selector.keyword.GetValue() == "C1"
        button(ui.wx, selector.ohm_button)
        assert selector.keyword.GetValue() == "C1Ω"
        assert selector.search_timer.IsRunning(), "Ω did not restart the search"
        selector.update_for({"R2": "C2"})
        button(ui.wx, selector.micro_button)
        assert selector.keyword.GetValue() == "C2µ"
        assert selector.search_timer.IsRunning(), "µ did not restart the search"
        selector.Close()

    window_ui.run(check)


@pytest.mark.parametrize(
    ("selection", "control", "expected", "cursor"),
    [
        pytest.param((3, 3), "ohm_button", "10kΩ 0603", 4, id="at-the-cursor"),
        pytest.param((2, 3), "micro_button", "10µ 0603", 3, id="over-a-selection"),
        pytest.param((0, 8), "ohm_button", "10k 0603Ω", 9, id="whole-box-selected"),
    ],
)
def test_a_symbol_is_typed_at_the_cursor_and_focus_returns(
    window_ui: Any,
    selection: tuple[int, int],
    control: str,
    expected: str,
    cursor: int,
) -> None:
    """The next key typed lands after the symbol, not over the whole search."""

    def check(ui: Any) -> None:
        selector = open_selector(ui)
        keyword = selector.keyword
        keyword.ChangeValue("10k 0603")
        keyword.SetSelection(*selection)
        getattr(selector, control).SetFocus()
        button(ui.wx, getattr(selector, control))
        wait_until(
            ui.wx,
            lambda: ui.wx.Window.FindFocus() is keyword,
            reason="The keyword box did not take focus back from the button",
        )
        assert keyword.GetValue() == expected
        assert keyword.GetSelection() == (cursor, cursor)
        selector.Close()

    window_ui.run(check)
