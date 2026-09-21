"""Real selector controls preserve assignment targets and close without stale work."""

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from .native_window_support import (
    _SelectableFootprint,
    button,
    choose_output,
    focus,
    select_result,
    window_ui,
)
from .native_wx_support import pump, wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def assignment_context(ui: Any, reference: str, variant: Optional[str]) -> Any:
    """Capture an actual parent session against an existing native destination."""
    if variant is None:
        return None
    component = str(ui.board.FindFootprintByReference(reference).m_Uuid.AsString())
    return ui.controller.session.begin_assignment(
        (ui.controller.session.snapshot.target(component, variant),)
    )


def open_selector(ui: Any, parts: dict[str, str], context: Any = None) -> Any:
    """Construct and show the production dialog, observing real posted events."""
    if not hasattr(ui, "posted"):
        ui.posted = []

        def received(event: Any) -> None:
            ui.posted.append(
                SimpleNamespace(
                    references=event.references,
                    assignment_context=event.assignment_context,
                    lcsc=event.lcsc,
                )
            )
            event.Skip()

        ui.dialog.Bind(ui.mainwindow.EVT_ASSIGN_PARTS_EVENT, received)
    selector = ui.selector_module.PartSelectorDialog(
        ui.dialog,
        parts,
        assignment_context=context,
        assignment_label=(
            f"Assign {next(iter(parts))} — {context.targets[0].variant_name}"
            if context
            else None
        ),
    )
    ui.dialog._part_selector = selector
    selector.Show()
    pump(ui.wx)
    return selector


@pytest.mark.parametrize("legacy", [False, True])
def test_copy_target_mapping_and_queued_event_survive_browsing_and_retarget(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    """Posted references/context are immutable; ordinary selection has no token."""

    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )
    if legacy:
        window_ui.board.names = []
        window_ui.board.current = ""

    def check(ui: Any) -> None:
        parts = {"R1": "C1"}
        context = assignment_context(ui, "R1", None if legacy else "A")
        selector = open_selector(ui, parts, context)
        parts.clear()
        if not legacy:
            focus(ui, "B")
        select_result(ui, selector)
        # Keep the dialog open solely to deliver the captured event after retarget.
        with monkeypatch.context() as patch:
            patch.setattr(selector, "Close", lambda: None)
            button(ui.wx, selector.select_part_button)
        next_parts = {"R2": "C456"}
        selector.update_for(
            next_parts,
            assignment_context=assignment_context(ui, "R2", None if legacy else "B"),
        )
        next_parts.clear()
        wait_until(ui.wx, lambda: bool(ui.posted))
        event = ui.posted[0]
        assert (event.references, event.assignment_context, event.lcsc) == (
            ("R1",),
            context,
            "C321",
        )
        assert selector.parts == {"R2": "C456"}
        if legacy:
            assert ui.controller is None
            assert ui.board.parts[0].fields["LCSC"] == "C321"
            assert ui.board.parts[1].fields["LCSC"] == "C1"
            assert not ui.messages
        else:
            assert ui.controller.session.snapshot.get("component-1", "A").lcsc == "C1"
            assert ui.controller.session.snapshot.get("component-2", "B").lcsc == "C1"
            assert "replaced" in ui.messages[-1]
        selector.Close()

    window_ui.run(check)


@pytest.mark.parametrize("outcome", ["success", "failure", "legacy"])
def test_retarget_resets_results_selection_and_context_before_search(
    window_ui: Any, outcome: str
) -> None:
    """A new target cannot reuse a selected result or label from the old session."""

    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )

    def check(ui: Any) -> None:
        selector = open_selector(ui, {"R1": "C1"}, assignment_context(ui, "R1", "A"))
        select_result(ui, selector)
        context = assignment_context(ui, "R2", None if outcome == "legacy" else "B")
        label = None if context is None else "Assign R2 — B"

        def search(parameters: Any) -> list[Any]:
            assert selector.parts == {"R2": "C456"}
            assert selector.assignment_context is context
            assert selector.GetTitle() == (label or "JLCPCB Library")
            assert not selector.part_list.GetSelectedItemsCount()
            assert not selector.part_list_model.data
            assert not selector.select_part_button.IsEnabled()
            selector.select_part()
            if outcome == "failure":
                raise RuntimeError("catalog unavailable")
            return []

        ui.catalog.search.side_effect = search
        if outcome == "failure":
            with pytest.raises(RuntimeError, match="catalog unavailable"):
                selector.update_for(
                    {"R2": "C456"}, assignment_context=context, assignment_label=label
                )
        else:
            selector.update_for(
                {"R2": "C456"}, assignment_context=context, assignment_label=label
            )
        assert selector.keyword.GetValue() == "C456"
        assert not selector.search_timer.IsRunning()
        pump(ui.wx)
        assert not ui.posted
        # A later result belongs solely to the newly established session.
        ui.catalog.search.side_effect = ui.catalog._search
        selector.search()
        select_result(ui, selector)
        button(ui.wx, selector.select_part_button)
        wait_until(ui.wx, lambda: bool(ui.posted))
        assert ui.posted[0].assignment_context is context
        assert ui.posted[0].references == ("R2",)
        assert ui.controller.session.snapshot.get("component-1", "A").lcsc == "C1"
        assert ui.controller.session.snapshot.get("component-2", "B").lcsc == (
            "C1" if context is None else "C321"
        )
        assert bool(ui.messages) is (context is None)

    window_ui.run(check)


@pytest.mark.parametrize(
    "route", ["button", "escape_command", "escape_key", "window_close", "save_error"]
)
def test_cancel_dismisses_without_assignment_then_reopens_for_another_variant(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Dismissal stops pending work; reopened assignments retain their captured target."""

    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )

    def check(ui: Any) -> None:
        selector = open_selector(ui, {"R1": "C1"}, assignment_context(ui, "R1", "A"))
        assert not selector.select_part_button.IsEnabled()
        assert selector.cancel_button.IsEnabled()
        assert (
            selector.GetEscapeId() == selector.cancel_button.GetId() == ui.wx.ID_CANCEL
        )
        selector.search_dwell()
        timer = selector.search_timer
        assert timer.IsRunning()
        ui.catalog.search.reset_mock()
        with monkeypatch.context() as patch:
            if route == "save_error":
                patch.setattr(
                    ui.dialog,
                    "save_settings",
                    MagicMock(side_effect=OSError("settings unavailable")),
                )
            if route in ("window_close", "save_error"):
                selector.Close()
            elif route == "button":
                button(ui.wx, selector.cancel_button)
            elif route == "escape_key":
                event = ui.wx.KeyEvent(ui.wx.wxEVT_CHAR_HOOK)
                event.SetKeyCode(ui.wx.WXK_ESCAPE)
                event.SetEventObject(selector.keyword)
                selector.GetEventHandler().ProcessEvent(event)
            else:
                event = ui.wx.CommandEvent(ui.wx.wxEVT_BUTTON, selector.GetEscapeId())
                event.SetEventObject(selector.cancel_button)
                selector.GetEventHandler().ProcessEvent(event)
            assert not timer.IsRunning()
        assert ui.dialog._part_selector is None
        wait_until(ui.wx, lambda: not selector)
        ui.catalog.search.assert_not_called()
        assert not ui.posted
        assert ui.dialog.IsEnabled() and not ui.dialog._closing
        assert ui.controller.session.snapshot.get("component-1", "A").lcsc == "C1"
        focus(ui, "B", row=1)
        ui.dialog.select_part()
        reopened = ui.dialog._part_selector
        context = reopened.assignment_context
        assert context.targets[0].variant_name == "B"
        assert "R2" in reopened.GetTitle() and "Variant B" in reopened.GetTitle()
        choose_output(ui, "A")
        focus(ui, "A")
        select_result(ui, reopened)
        button(ui.wx, reopened.select_part_button)
        wait_until(ui.wx, lambda: len(ui.posted) == 1)
        assert ui.dialog._part_selector is None
        assert ui.posted[0].assignment_context is context
        assert ui.posted[0].references == ("R2",)
        snapshot = ui.controller.session.snapshot
        assert snapshot.get("component-2", "B").lcsc == "C321"
        for component in ("component-1", "component-2"):
            assert (
                snapshot.get(component, "A").lcsc
                == snapshot.get(component, "").lcsc
                == "C1"
            )
        assert (
            ui.controller.session.output_variant
            == ui.controller.view.output_variant
            == "A"
        )
        assert ui.controller.view.selected_target.variant == "A"
        assert ui.board.current == "A"
        assert not ui.messages

    window_ui.run(check)
