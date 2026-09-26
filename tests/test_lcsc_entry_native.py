"""The real Enter LCSC prompt assigns typed numbers in the production windows."""

from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from .native_window_support import (
    _SelectableFootprint,
    button,
    focus,
    modal_handler,
    window_ui,
)
from .variant_matrix_native_test_support import click_native_cell

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


@pytest.mark.parametrize("answer", ["YES", "NO"])
def test_variant_entry_edits_every_selected_component_of_one_variant(
    window_ui: Any, answer: str
) -> None:
    """The number lands in the focused variant only, and only on Yes."""
    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )

    def check(ui: Any) -> None:
        ui.catalog.parts[UNLISTED] = {}
        target = focus(ui, "B")
        ui.controller.view.select_components(("component-1", "component-2"), "B")
        opened: list[tuple[str, str]] = []

        def interact(dialog: Any) -> None:
            opened.append((dialog.GetTitle(), dialog.text.GetValue()))
            dialog.text.SetValue(UNLISTED)
            button(ui.wx, dialog.ok_button)

        with (
            patch.object(
                ui.wx.MessageDialog,
                "ShowModal",
                return_value=getattr(ui.wx, f"ID_{answer}"),
            ) as asked,
            modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact),
        ):
            ui.controller.dispatch_action("enter_lcsc", target)

        assert opened == [("Enter LCSC for R1, R2", "C1")]
        asked.assert_called_once()
        snapshot = ui.controller.session.adapter.snapshot()
        expected = UNLISTED if answer == "YES" else "C1"
        for component in ("component-1", "component-2"):
            assert snapshot.get(component, "B").lcsc == expected
            assert snapshot.get(component, "A").lcsc == "C1"
        assert not ui.messages

    window_ui.run(check)


def test_variant_entry_outside_a_variant_explains_and_opens_nothing(
    window_ui: Any,
) -> None:
    """A shared-column target cannot say which variant the number is for."""

    def check(ui: Any) -> None:
        target = replace(focus(ui, "B"), variant=None)
        with modal_handler(
            ui, ui.mainwindow.LcscEntryDialog, lambda _d: None
        ) as opened:
            ui.controller.dispatch_action("enter_lcsc", target)
        assert opened == []
        assert ui.messages == ["Select components within one variant to enter an LCSC."]

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


@pytest.mark.parametrize("enabled", [True, False])
def test_variant_menu_item_follows_mutation_enablement(
    window_ui: Any, enabled: bool
) -> None:
    """Enter LCSC is offered for editing only while the table accepts edits."""

    def check(ui: Any) -> None:
        view, wx = ui.controller.view, ui.wx
        states: list[bool] = []

        def popup(menu: Any) -> None:
            states.extend(
                item.IsEnabled()
                for item in menu.GetMenuItems()
                if item.GetItemLabelText() == "Enter LCSC…"
            )

        view.PopupMenu = popup
        view.set_mutations_enabled(enabled)
        try:
            click_native_cell(
                view, wx, 0, view.model.column_for("B", "lcsc"), right=True
            )
        finally:
            view.set_mutations_enabled(True)
        assert states == [enabled]

    window_ui.run(check)


def test_variant_entry_prefills_the_focused_variants_number(window_ui: Any) -> None:
    """The prompt opens on the focused variant's number, not the base one."""
    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )
    for part in window_ui.board.parts:
        part.AddVariant("B").SetFieldValue("LCSC", "C22")

    def check(ui: Any) -> None:
        target = focus(ui, "B")
        ui.controller.view.select_components(("component-1", "component-2"), "B")
        opened: list[tuple[str, str]] = []

        def interact(dialog: Any) -> None:
            opened.append((dialog.GetTitle(), dialog.text.GetValue()))

        with modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact):
            ui.controller.dispatch_action("enter_lcsc", target)

        assert opened == [("Enter LCSC for R1, R2", "C22")]
        assert not ui.messages

    window_ui.run(check)


def test_variant_entry_during_generation_explains_and_opens_nothing(
    window_ui: Any,
) -> None:
    """No number is asked for while generation has frozen the variants."""

    def check(ui: Any) -> None:
        target = focus(ui, "B")
        ui.controller.session.generating = True
        try:
            with modal_handler(
                ui, ui.mainwindow.LcscEntryDialog, lambda _d: None
            ) as opened:
                ui.controller.dispatch_action("enter_lcsc", target)
        finally:
            ui.controller.session.generating = False
        assert opened == []
        assert ui.messages == ["Finish generation before editing variants."]

    window_ui.run(check)


@pytest.mark.parametrize("table", ["main", "variants"])
def test_only_the_main_table_remembers_an_entered_number(
    window_ui: Any, table: str
) -> None:
    """As with Paste in each table: the main table saves a preference, variants don't."""
    window_ui.settings["part_preferences"]["remember_lcsc_assignments"] = True
    if table == "main":
        _main_table(window_ui)

    def check(ui: Any) -> None:
        # The fixture's catalog starts without the shared preferences table.
        ui.dialog.library.create_part_preferences_table()

        def interact(dialog: Any) -> None:
            dialog.text.SetValue("C555")
            button(ui.wx, dialog.ok_button)

        with modal_handler(ui, ui.mainwindow.LcscEntryDialog, interact):
            if table == "main":
                ui.dialog.footprint_list.SelectAll()
                ui.dialog.enter_part_lcsc()
            else:
                ui.controller.dispatch_action("enter_lcsc", focus(ui, "B"))

        saved = ui.dialog.library.get_all_part_preferences()
        assert [row[2] for row in saved] == (["C555"] if table == "main" else [])
        assert not ui.messages

    window_ui.run(check)
