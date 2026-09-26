"""The real Enter LCSC prompt assigns typed numbers in the production windows."""

from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import button, modal_handler, window_ui

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx

UNLISTED = "C46551386"
HINT = "Type a code like C25804, or paste an LCSC or JLCPCB product link."


def _main_table(window_ui: Any) -> None:
    """Open the board without variants, so the ordinary parts table is shown."""
    window_ui.board.names = []
    window_ui.board.current = ""


def _text_enter(ui: Any, dialog: Any) -> None:
    """Deliver Enter from the real text control."""
    event = ui.wx.CommandEvent(ui.wx.wxEVT_TEXT_ENTER, dialog.text.GetId())
    event.SetEventObject(dialog.text)
    dialog.text.GetEventHandler().ProcessEvent(event)


def test_prompt_reads_a_product_link_and_assigns_an_unlisted_number(
    window_ui: Any,
) -> None:
    """OK follows the text; Yes to the question assigns the linked number."""
    _main_table(window_ui)

    def check(ui: Any) -> None:
        ui.catalog.parts[UNLISTED] = {}
        ui.dialog.footprint_list.SelectAll()
        seen: list[tuple[Any, ...]] = []

        def interact(dialog: Any) -> None:
            seen.append(
                (
                    dialog.GetTitle(),
                    dialog.text.GetValue(),
                    dialog.ok_button.IsEnabled(),
                )
            )
            dialog.text.SetValue("C0G")
            seen.append((dialog.hint.GetLabel(), dialog.ok_button.IsEnabled()))
            dialog.text.SetValue(f"https://www.lcsc.com/product-detail/{UNLISTED}.html")
            seen.append((dialog.hint.GetLabel(), dialog.ok_button.IsEnabled()))
            button(ui.wx, dialog.ok_button)

        with (
            patch.object(
                ui.wx.MessageDialog, "ShowModal", return_value=ui.wx.ID_YES
            ) as asked,
            modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact) as opened,
        ):
            ui.dialog.enter_part_lcsc()

        assert len(opened) == 1
        assert seen == [
            ("Enter LCSC for R1", "C1", True),
            (HINT, False),
            (f"Will assign {UNLISTED}", True),
        ]
        asked.assert_called_once()
        assert ui.board.parts[0].fields["LCSC"] == UNLISTED
        assert ui.cache.get_part("R1")["lcsc"] == UNLISTED
        assert not ui.messages

    window_ui.run(check)


def test_enter_key_accepts_only_text_naming_one_number(window_ui: Any) -> None:
    """Enter on ambiguous text keeps the prompt open; on one number it assigns."""
    _main_table(window_ui)

    def check(ui: Any) -> None:
        ui.dialog.footprint_list.SelectAll()
        still_open: list[bool] = []

        def interact(dialog: Any) -> None:
            dialog.text.SetValue("C123 C456")
            _text_enter(ui, dialog)
            still_open.append(dialog.IsModal())
            dialog.text.SetValue("c555")
            _text_enter(ui, dialog)
            still_open.append(dialog.IsModal())

        with modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact):
            ui.dialog.enter_part_lcsc()

        assert still_open == [True, False]
        assert ui.board.parts[0].fields["LCSC"] == "C555"
        assert not ui.messages

    window_ui.run(check)


def test_prefilled_prompt_leaves_room_for_the_longer_hint(window_ui: Any) -> None:
    """Opening on "Will assign C1" still fits the hint shown once the text is cleared."""
    _main_table(window_ui)

    def check(ui: Any) -> None:
        ui.dialog.footprint_list.SelectAll()
        widths: list[tuple[str, int, int]] = []

        def interact(dialog: Any) -> None:
            dialog.text.SetValue("")
            widths.append(
                (
                    dialog.hint.GetLabel(),
                    dialog.hint.GetSize().width,
                    dialog.hint.GetTextExtent(HINT).width,
                )
            )

        with modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact) as opened:
            ui.dialog.enter_part_lcsc()

        assert len(opened) == 1
        ((label, shown, needed),) = widths
        assert label == HINT
        assert shown >= needed

    window_ui.run(check)


def test_main_table_menu_opens_the_prompt_on_the_shared_number(
    window_ui: Any,
) -> None:
    """The item sits under Paste LCSC and opens a focused, preselected prompt."""
    _main_table(window_ui)

    def check(ui: Any) -> None:
        wx = ui.wx
        ui.dialog.footprint_list.SelectAll()
        labels: list[str] = []
        seen: list[tuple[bool, str]] = []

        def popup(menu: Any) -> None:
            items = {item.GetItemLabelText(): item for item in menu.GetMenuItems()}
            labels.extend(items)
            item = items["Enter LCSC…"]
            menu.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, item.GetId()))

        def interact(dialog: Any) -> None:
            seen.append(
                (wx.Window.FindFocus() is dialog.text, dialog.text.GetStringSelection())
            )

        ui.dialog.footprint_list.PopupMenu = popup
        with modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact) as opened:
            ui.dialog.OnRightDown()

        assert labels[labels.index("Paste LCSC") + 1] == "Enter LCSC…"
        assert len(opened) == 1
        assert seen == [(True, "C1")]
        assert not ui.messages

    window_ui.run(check)
