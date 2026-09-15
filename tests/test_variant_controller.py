"""Integrate captured main-dialog operations with native state and saved settings."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock, patch

import pytest

from .native_window_support import (
    _SelectableFootprint,
    button,
    choose_output as _choose_output,
    focus,
    modal_handler,
    select_result,
    window_ui,
)
from .native_wx_support import wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _state(ui: Any, variant: str) -> Any:
    return ui.controller.session.adapter.snapshot().get("component-1", variant)


def _clipboard(ui: Any, text: str) -> None:
    assert ui.wx.TheClipboard.Open()
    try:
        assert ui.wx.TheClipboard.SetData(ui.wx.TextDataObject(text))
    finally:
        ui.wx.TheClipboard.Close()


def _assert_output(ui: Any, variant: str, index: Optional[int]) -> None:
    """Check the saved choice, control, header and generation gate together."""
    controller = ui.controller
    available = index is not None
    assert controller.session.output_variant == variant
    assert controller.view.output_variant == (variant if available else None)
    assert controller.output_choice.GetSelection() == (
        index if available else ui.module.wx.NOT_FOUND
    )
    assert ui.dialog.generate_button.IsEnabled() is available


def test_output_header_is_independent_of_selection_editing_and_refresh(
    window_ui: Any,
) -> None:
    """Initial/explicit output controls agree and remain independent of inspection/editing."""

    def check(ui: Any) -> None:
        _assert_output(ui, "A", 1)
        for output, index in (("B", 2), ("", 0), ("A", 1)):
            _choose_output(ui, output)
            _assert_output(ui, output, index)
            assert ui.board.current == "A"
        _choose_output(ui, "B")
        focus(ui, "A")
        assert ui.controller.view.output_variant == "B"
        focus(ui, None, "ref")
        assert ui.controller.view.output_variant == "B"

        target = focus(ui, "A", "lcsc")
        ui.controller._on_edit(target, "C999")
        assert _state(ui, "A").lcsc == "C999"
        assert ui.controller.view.output_variant == "B"
        ui.controller.refresh()

        _assert_output(ui, "B", 2)
        assert ui.controller.view.selected_target == target
        assert ui.messages == []

    window_ui.run(check)


@pytest.mark.parametrize(
    "change,expected", [("retarget", "C1"), ("same-target", "C1"), ("external", "C555")]
)
def test_pending_assignment_rejects_retargeting_or_new_native_values(
    window_ui: Any, change: str, expected: str
) -> None:
    """Queued choices cannot overwrite newer native values or a new selector target."""

    def check(ui: Any) -> None:
        focus(ui, "A")
        ui.controller.select_part()
        original = ui.dialog._part_selector.assignment_context
        if change != "external":
            focus(ui, "A" if change == "same-target" else "B")
            ui.controller.select_part()
            current = ui.dialog._part_selector.assignment_context
            assert current.serial != original.serial
            assert (current.targets == original.targets) is (change == "same-target")
        else:
            ui.board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C555")
        ui.controller.assign_parts(
            SimpleNamespace(assignment_context=original, lcsc="C999")
        )
        assert _state(ui, "A").lcsc == expected
        assert _state(ui, "B").lcsc == "C1"
        assert ui.controller.model.snapshot.get("component-1", "A").lcsc == expected
        assert ui.controller.session.snapshot.get("component-1", "A").lcsc == expected
        assert ui.controller.session.reliable and ui.controller.view._mutations_enabled
        assert ui.messages
        if change != "external":
            assert "replaced" in ui.messages[0]
            ui.controller.assign_parts(
                SimpleNamespace(assignment_context=current, lcsc="C888")
            )
            assert _state(ui, "A" if change == "same-target" else "B").lcsc == "C888"
            assert _state(ui, "B" if change == "same-target" else "A").lcsc == "C1"

    window_ui.run(check)


@pytest.mark.parametrize("event_kind", ["assignment", "cell_edit", "menu_action"])
def test_queued_edit_after_controller_close_cannot_change_native_board(
    window_ui: Any, event_kind: str
) -> None:
    """Selector and grid events already in wx's queue are ignored during teardown."""

    def check(ui: Any) -> None:
        target = focus(ui, "A", "lcsc")
        ui.controller.select_part()
        context = ui.dialog._part_selector.assignment_context
        before = ui.controller.session.adapter.snapshot()
        delivered = []

        def deliver() -> None:
            if event_kind == "assignment":
                ui.controller.assign_parts(
                    SimpleNamespace(assignment_context=context, lcsc="C999")
                )
            elif event_kind == "cell_edit":
                ui.controller._on_edit(target, "C999")
            else:
                ui.controller.dispatch_action("remove", target)
            delivered.append(True)

        ui.wx.CallAfter(deliver)
        ui.controller.close()
        wait_until(ui.wx, lambda: bool(delivered))

        assert ui.controller.session.adapter.snapshot() == before
        assert not ui.controller.view._mutations_enabled
        assert ui.controller.session.output_variant == "A"
        assert not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("navigate", [False, True])
def test_ref_selection_is_read_only_and_variant_blocks_bound_batch_edits(
    window_ui: Any,
    navigate: bool,
) -> None:
    """Batch flags and captured assignments use only selected UUIDs in one variant."""
    for index in (2, 3):
        window_ui.board.parts.append(
            _SelectableFootprint(window_ui.board, f"component-{index}", f"R{index}")
        )

    def check(ui: Any) -> None:
        c = ui.controller
        focus(ui, None, "ref")
        before = c.session.adapter.snapshot()
        c.select_part()
        c.toggle(("bom", "pos", "pop"))
        assert (
            ui.dialog._part_selector is None and c.session.adapter.snapshot() == before
        )
        assert not ui.dialog.select_part_button.IsEnabled()
        assert len(ui.messages) == 2
        ui.messages.clear()

        focus(ui, "A")
        c.view.ClearSelection()
        wait_until(ui.wx, lambda: not c.view._selection_pending)
        batch_controls = (
            ui.dialog.select_part_button,
            ui.dialog.remove_lcsc_number_button,
            ui.dialog.toggle_bom_pos_button,
            ui.dialog.toggle_bom_button,
            ui.dialog.toggle_pos_button,
        )
        assert c.view.selected_target.variant == "A"
        assert not c.view.selected_component_ids()
        assert all(not control.IsEnabled() for control in batch_controls)
        assert not ui.messages

        c.view.select_components(("component-1", "component-2"), "A")
        wait_until(ui.wx, lambda: not c.view._selection_pending)
        assert all(control.IsEnabled() for control in batch_controls)
        c.toggle(("bom", "pos", "pop"))
        c.differences.SetValue(True)
        c._on_display_changed(None)
        assert {row.component_id for row in c.model.rows} == {
            "component-1",
            "component-2",
        }
        if navigate:
            key = ui.wx.KeyEvent(ui.wx.wxEVT_KEY_DOWN)
            key.SetEventObject(c.view)
            key.SetKeyCode(ui.wx.WXK_DOWN)
            c.view.GetEventHandler().ProcessEvent(key)
        assigned = ("component-2",) if navigate else ("component-1", "component-2")
        c.select_part()
        selector = ui.dialog._part_selector
        assert tuple(
            (target.component_id, target.variant_name)
            for target in selector.assignment_context.targets
        ) == tuple((component, "A") for component in assigned)
        focus(ui, "B")
        _choose_output(ui, "B")
        select_result(ui, selector)
        button(ui.wx, selector.select_part_button)
        wait_until(
            ui.wx, lambda: c.session.snapshot.get("component-2", "A").lcsc == "C321"
        )
        snapshot = c.session.adapter.snapshot()
        for part in snapshot.components:
            selected = part.variant_name == "A" and part.component_id in {
                "component-1",
                "component-2",
            }
            assert (part.bom, part.pos, part.pop) == (not selected,) * 3
            assert part.lcsc == (
                "C321" if selected and part.component_id in assigned else "C1"
            )
        assert c.session.output_variant == "B" and c.view.selected_target.variant == "B"
        assert ui.dialog._part_selector is None and not ui.messages

    window_ui.run(check)


def _assert_editing(ui: Any, enabled: bool) -> None:
    """Check all mutation/output gates against the same explicit readiness."""
    controller = ui.controller
    assert controller.session.reliable is enabled
    assert controller.view._mutations_enabled is enabled
    for control in (
        controller.output_choice,
        ui.dialog.generate_button,
    ):
        assert control.IsEnabled() is enabled


@pytest.mark.parametrize("failure", ["startup", "timer", "native-edit"])
def test_native_or_preparation_failure_blocks_edits_until_explicit_refresh(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Failures retain committed native edits; the actual Refresh button recovers."""
    original = window_ui.mainwindow.Library.read_correction_data
    fault = Mock(side_effect=OSError("source temporarily unavailable"))
    if failure == "startup":
        monkeypatch.setattr(window_ui.mainwindow.Library, "read_correction_data", fault)

    def check(ui: Any) -> None:
        controller = ui.controller
        read = controller.session.adapter.snapshot
        before = read()
        if failure != "startup":
            _choose_output(ui, "B")
            previous_model = controller.model
            if failure == "timer":
                captured = controller.session.snapshot
                ui.board.parts[0].AddVariant("A").SetFieldValue("LCSC", "C555")
                before = read()
                monkeypatch.setattr(controller.session.adapter, "snapshot", fault)
                controller._on_timer(None)
                assert not controller.timer.IsRunning()
                assert controller.session.snapshot is captured
                assert captured.get("component-1", "A").lcsc == "C1"
            else:
                monkeypatch.setattr(ui.catalog, "read_correction_data", fault)
                controller._on_edit(focus(ui, "A"), "C999")
                assert controller.model is previous_model
        _assert_editing(ui, False)
        assert ui.messages == ["source temporarily unavailable"]
        assert controller.view.output_variant == (None if failure == "startup" else "B")
        with pytest.raises(ui.module.VariantSessionError, match="unavailable"):
            controller.begin_generation(())
        if failure == "native-edit":
            controller._on_edit(focus(ui, "A"), "C888")
            assert read().get("component-1", "A").lcsc == "C999"
        else:
            assert read() == before
        monkeypatch.setattr(controller.session.adapter, "snapshot", read)
        monkeypatch.setattr(
            ui.catalog, "read_correction_data", lambda: original(ui.catalog)
        )
        assert controller.panel.IsShown() and not ui.dialog.footprint_list.IsShown()
        refresh = next(
            child
            for child in controller.panel.GetChildren()
            if isinstance(child, ui.wx.Button) and child.GetLabel() == "Refresh"
        )
        assert refresh.IsEnabled()
        button(ui.wx, refresh)
        if failure == "timer":
            assert controller.session.snapshot.get("component-1", "A").lcsc == "C555"
            assert captured.get("component-1", "A").lcsc == "C1"
        _assert_editing(ui, True)
        assert controller.timer.IsRunning()
        assert controller.session.output_variant == (
            "A" if failure == "startup" else "B"
        )
        assert (
            _state(ui, "A").lcsc
            == {"native-edit": "C999", "timer": "C555", "startup": "C1"}[failure]
        )
        assert _state(ui, "").lcsc == _state(ui, "B").lcsc == "C1"
        assert ui.board.current == "A"

    window_ui.run(check)


@pytest.mark.parametrize(
    "source,destination,rows", [("A", "B", (0, 2)), ("", "B", (0,)), ("A", "", (0,))]
)
def test_copy_selected_components_captures_settings_then_pastes_via_keyboard(
    window_ui: Any, source: str, destination: str, rows: tuple[int, ...]
) -> None:
    """Keyboard block copy preserves captured settings across scopes and reordering."""
    from .variant_matrix_native_test_support import click_native_cell

    for index in (2, 3):
        window_ui.board.parts.append(
            _SelectableFootprint(window_ui.board, f"component-{index}", f"R{index}")
        )

    def check(ui: Any) -> None:
        controller, view, wx = ui.controller, ui.controller.view, ui.wx
        _choose_output(ui, "B")
        for index, part in enumerate(ui.board.parts, start=1):
            variant = part.AddVariant("A")
            variant.SetFieldValue("LCSC", "" if index == 1 else f"C{index}")
            variant.SetFieldValue("Value", f"{index}k")
            variant.SetDNP(True)
            variant.SetExcludedFromBOM(True)
            variant.SetExcludedFromPosFiles(True)
            destination_record = part.AddVariant("B")
            destination_record.SetFieldValue("Manufacturer", "preserve")
            if source == "" and index - 1 in rows:
                destination_record.SetFieldValue("LCSC", "C999")
                destination_record.SetFieldValue("Value", "stale")
                destination_record.SetDNP(True)
                destination_record.SetExcludedFromBOM(True)
                destination_record.SetExcludedFromPosFiles(True)
        controller.refresh()

        def shortcut(letter: str) -> None:
            event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
            event.SetEventObject(view)
            event.SetKeyCode(ord(letter))
            event.SetControlDown(True)
            event.SetMetaDown(True)
            view.GetEventHandler().ProcessEvent(event)

        def select(variant: str, field: str) -> None:
            col = view.model.column_for(variant, field)
            for position, index in enumerate(rows):
                row = view.model.row_for_component(f"component-{index + 1}")
                click_native_cell(view, wx, row, col, command=position > 0)

        select(source, "price")
        shortcut("C")
        if len(rows) > 1:
            ui.board.parts[0].GetVariant("A").SetFieldValue("Value", "47k")
            controller.refresh()
            view.reorder_variant("B", "A")
        before = controller.session.adapter.snapshot()
        select(destination, "type")
        shortcut("V")
        snapshot = controller.session.adapter.snapshot()
        for index, part in enumerate(ui.board.parts, start=1):
            expected = (
                (f"{index}k", "" if index == 1 else f"C{index}", False, False, False)
                if index - 1 in rows and source == "A"
                else ("10k", "C1", True, True, True)
            )
            state = snapshot.get(f"component-{index}", destination)
            assert (
                state.value,
                state.lcsc,
                state.bom,
                state.pos,
                state.pop,
            ) == expected
            assert part.GetVariant("B").fields["Manufacturer"] == "preserve"
            for preserved in {source, ""} - {destination}:
                original = before.get(f"component-{index}", preserved)
                current = snapshot.get(f"component-{index}", preserved)
                for field in ("value", "lcsc", "bom", "pos", "pop"):
                    assert getattr(current, field) == getattr(original, field)
        assert controller.session.output_variant == "B"
        assert ui.dialog._part_selector is None and not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("field", ["ref", "footprint"])
def test_copy_shared_column_includes_selected_rows_in_display_order(
    window_ui: Any, field: str
) -> None:
    """Physical Copy retains every selected value without choosing an output variant."""
    for index in (2, 3):
        part = _SelectableFootprint(window_ui.board, f"component-{index}", f"R{index}")
        part.GetFPIDAsString = lambda index=index: f"Library:Footprint{index}"
        window_ui.board.parts.append(part)

    def check(ui: Any) -> None:
        controller, view = ui.controller, ui.controller.view
        view._sort_state = (None, "ref", True)
        controller.render()
        view.select_components(("component-1", "component-3"), None, field)
        before = controller.session.adapter.snapshot()
        controller.dispatch_action("copy", view.selected_target)
        assert not ui.messages
        assert ui.wx.TheClipboard.Open()
        try:
            text = ui.wx.TextDataObject()
            assert ui.wx.TheClipboard.GetData(text)
            assert text.GetText() == (
                "R3\nR1" if field == "ref" else "Library:Footprint3\nR:R0603"
            )
        finally:
            ui.wx.TheClipboard.Close()
        destination = focus(ui, "B", "value")
        controller.dispatch_action("paste", destination)
        assert len(ui.messages) == 1 and "Read-only" in ui.messages[0]
        assert controller.session.adapter.snapshot() == before
        assert controller.session.output_variant == "A"

    window_ui.run(check)


def test_external_false_paste_clears_only_target_population(window_ui: Any) -> None:
    """External plain false clears only the selected variant’s population flag."""

    def check(ui: Any) -> None:
        target = focus(ui, "B", "pop")
        _clipboard(ui, "false")
        ui.controller.dispatch_action("paste", target)
        assert not _state(ui, "B").pop
        assert _state(ui, "A").pop
        assert not ui.messages

    window_ui.run(check)


def test_external_paste_uses_selected_component_after_deselecting_cursor_row(
    window_ui: Any,
) -> None:
    """Native Ctrl-deselect retains cursor focus, which must not receive the paste."""
    from .variant_matrix_native_test_support import click_native_cell

    window_ui.board.parts.append(
        _SelectableFootprint(window_ui.board, "component-2", "R2")
    )

    def check(ui: Any) -> None:
        view = ui.controller.view
        column = view.model.column_for("B", "lcsc")
        click_native_cell(view, ui.wx, 0, column)
        for _ in range(2):
            click_native_cell(view, ui.wx, 1, column, command=True)
        assert view.selected_component_ids() == ("component-1",)
        assert view.selected_target.component_id == "component-2"
        _clipboard(ui, "C555")
        ui.controller.dispatch_action("paste", view.selected_target)
        snapshot = ui.controller.session.adapter.snapshot()
        assert (
            snapshot.get("component-1", "B").lcsc,
            snapshot.get("component-2", "B").lcsc,
        ) == ("C555", "C1")
        assert ui.controller.session.output_variant == "A" and not ui.messages

    window_ui.run(check)


def test_external_clipboard_replaces_internal_payload_even_when_text_matches(
    window_ui: Any,
) -> None:
    """A stale private token must not reinterpret later external text as a block."""

    def check(ui: Any) -> None:
        source = focus(ui, "A", "pop")
        ui.controller.dispatch_action("copy_cell", source)
        _clipboard(ui, "true")
        destination = focus(ui, "B", "value")
        ui.controller.dispatch_action("paste", destination)

        assert _state(ui, "B").value == "true"
        assert _state(ui, "B").pop
        assert _state(ui, "A").value == "10k"
        assert ui.messages == []

    window_ui.run(check)


@pytest.mark.parametrize("change", ["focus", "source"])
def test_copy_to_captures_sparse_fields_refs_and_destinations_before_dialogs(
    window_ui: Any, change: str
) -> None:
    """Two refs × two fields × two destinations use values captured before both dialogs."""
    for index in (2, 3):
        window_ui.board.parts.append(
            _SelectableFootprint(window_ui.board, f"component-{index}", f"R{index}")
        )
    for part, value, lcsc in zip(
        window_ui.board.parts[:2], ("22k", "33k"), ("C999", "C998")
    ):
        a = part.AddVariant("A")
        a.SetFieldValue("Value", value)
        a.SetFieldValue("LCSC", lcsc)
        a.SetDNP(True)

    def check(ui: Any) -> None:
        source = focus(ui, "A")
        ui.controller.view.select_components(("component-1", "component-2"), "A")
        assert ui.controller.view.selected_component_ids() == (
            "component-1",
            "component-2",
        )
        messages = []

        def choose(dialog: Any) -> None:
            messages.append(
                "\n".join(
                    child.GetLabel()
                    for child in dialog.GetChildren()
                    if isinstance(child, ui.wx.StaticText)
                )
            )
            if len(messages) == 1:
                dialog.SetSelections([1, 4])
            else:
                focus(ui, "B", "value")
                if change == "source":
                    a = ui.board.parts[0].GetVariant("A")
                    a.SetFieldValue("LCSC", "C777")
                    a.SetDNP(False)
                    ui.controller.refresh()
                dialog.SetSelections([0, 1])
            button(ui.wx, dialog.FindWindow(ui.wx.ID_OK))

        with modal_handler(ui, ui.wx.MultiChoiceDialog, choose):
            ui.controller.dispatch_action("copy_to", source)
        snapshot = ui.controller.session.adapter.snapshot()
        for variant in ("", "B"):
            for component, lcsc in (("component-1", "C999"), ("component-2", "C998")):
                part = snapshot.get(component, variant)
                assert (part.lcsc, part.pop, part.value) == (lcsc, False, "10k"), (
                    ui.messages
                )
            assert snapshot.get("component-3", variant).lcsc == "C1"
        assert "Components: R1, R2" in messages[0] and "lcsc, pop" in messages[1]
        assert _state(ui, "A").lcsc == ("C777" if change == "source" else "C999")
        assert _state(ui, "A").pop is (change == "source")
        assert ui.controller.session.output_variant == "A" and not ui.messages

    window_ui.run(check)


@pytest.mark.parametrize("failure", ["setter", "changed", "removed", "board"])
def test_copy_to_partial_native_failure_restores_all_destinations(
    window_ui: Any, failure: str
) -> None:
    """Stale modal targets reject the whole batch; partial writes restore all targets."""

    def check(ui: Any) -> None:
        from .variant_native_support import Board

        a = ui.board.parts[0].AddVariant("A")
        a.SetFieldValue("Value", "22k")
        a.SetFieldValue("LCSC", "C999")
        ui.controller.refresh()
        source = focus(ui, "A", "value")
        before = []

        def during() -> None:
            if failure == "setter":
                ui.board.parts[0].fail_field = "Value"
            elif failure == "changed":
                ui.board.parts[0].AddVariant("B").SetFieldValue(
                    "Value", "external edit"
                )
            elif failure == "removed":
                ui.board.names.remove("B")
            else:
                ui.dialog.pcbnew.board = Board()
            before.append(ui.controller.session.adapter.snapshot())
            ui.controller.refresh()

        def choose(dialog: Any) -> None:
            if "destinations" in dialog.GetTitle():
                during()
            dialog.SetSelections([1, 0] if failure == "setter" else [0, 1])
            button(ui.wx, dialog.FindWindow(ui.wx.ID_OK))

        with modal_handler(ui, ui.wx.MultiChoiceDialog, choose):
            ui.controller.dispatch_action("copy_to", source)

        assert before, ui.messages
        assert ui.controller.session.adapter.snapshot() == before[0]
        assert ui.messages
        if failure == "setter":
            assert _state(ui, "").lcsc == "C1"
            assert _state(ui, "B").lcsc == "C1"
            assert any("restored" in message for message in ui.messages)

    window_ui.run(check)


@pytest.mark.parametrize(
    "output,variants,index",
    [("B", ("A", "B"), 2), ("", ("A", "B"), 0), ("B", ("A",), None)],
    ids=["named", "default", "removed-output"],
)
def test_settings_restore_output_and_display_after_reopening_without_database(
    window_ui: Any, output: str, variants: tuple[str, ...], index: Optional[int]
) -> None:
    """Preferences survive real frame destruction; selection and scrolling do not."""

    def save(ui: Any) -> None:
        _choose_output(ui, output)
        ui.controller.differences.SetValue(True)
        ui.controller.show_footprint_library.SetValue(True)
        focus(ui, "A", "value")
        ui.board.names[:] = variants

    def reopen(ui: Any) -> None:
        _assert_output(ui, output, index)
        assert ui.controller.session.reliable
        assert ui.controller.differences.GetValue()
        assert ui.controller.show_footprint_library.GetValue()
        assert ui.controller.view.selected_target is None
        assert not {"target", "scroll"} & ui.cache.get_display_preferences().keys()
        assert not Path(ui.cache.dbfile).exists()
        if index is None:
            with pytest.raises(ui.module.VariantSessionError, match="unavailable"):
                ui.controller.begin_generation(())
            with pytest.raises(ui.module.VariantSessionError, match="unavailable"):
                ui.controller.session.set_output_variant("missing")
            _choose_output(ui, "")
            _assert_output(ui, "", 0)
            ui.controller.begin_generation(())
            ui.controller.end_generation()

    window_ui.run(save, reopen)


def test_constructor_does_not_save_implicit_output_or_depend_on_writable_settings(
    window_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Variant startup does not require a preference write for its implicit output."""
    original = window_ui.module.VariantMainController
    save = Mock(side_effect=OSError("settings unavailable"))

    def construct(*args: Any) -> Any:
        with patch.object(window_ui.mainwindow.JLCPCBTools, "save_settings", save):
            return original(*args)

    monkeypatch.setattr(window_ui.module, "VariantMainController", construct)

    def check(ui: Any) -> None:
        _assert_output(ui, "A", 1)
        assert ui.controller.timer.IsRunning() and ui.controller.session.reliable
        save.assert_not_called()
        assert not Path(ui.cache.dbfile).exists() and not ui.messages
        before = ui.controller.session.adapter.snapshot()
        with patch.object(ui.dialog, "save_settings", save):
            _choose_output(ui, "B")
        _assert_output(ui, "A", 1)
        assert ui.dialog.generate_button.GetLabel() == "Generate A"
        assert ui.controller.session.adapter.snapshot() == before
        assert ui.messages == ["settings unavailable"]

    window_ui.run(check)


@pytest.mark.parametrize(
    ("field", "text"), (("pop", "sometimes"), ("lcsc", "invalid"), ("price", "1.00"))
)
def test_invalid_or_readonly_clipboard_values_never_mutate(
    window_ui: Any, field: str, text: str
) -> None:
    """A rejected single-cell paste preserves native source and independent output."""

    def check(ui: Any) -> None:
        target = focus(ui, "B", field)
        before = ui.controller.session.adapter.snapshot().source_token
        _clipboard(ui, text)
        ui.controller.dispatch_action("paste", target)

        assert ui.controller.session.adapter.snapshot().source_token == before
        assert ui.controller.session.output_variant == "A"
        assert ui.messages

    window_ui.run(check)


def test_generation_freezes_mutations_and_output_while_preserving_inspection(
    window_ui: Any,
) -> None:
    """Generation captures Output B independently of cursor A and locks edits until completion."""

    def check(ui: Any) -> None:
        _choose_output(ui, "B")
        focus(ui, "A")
        before = ui.controller.session.adapter.snapshot().source_token
        ui.controller.begin_generation(())

        assert ui.dialog.fabrication.variant_name == "B"
        assert not ui.controller.view._mutations_enabled
        assert not ui.controller.output_choice.IsEnabled()
        assert ui.controller.view.output_variant == "B"
        target = focus(ui, "A", "pop")
        ui.controller._on_edit(target, False)
        _choose_output(ui, "")

        assert ui.controller.session.output_variant == "B"
        assert ui.controller.view.output_variant == "B"
        assert ui.controller.session.adapter.snapshot().source_token == before
        ui.dialog.quit_dialog()
        assert not ui.controller.closed and ui.controller.timer.IsRunning()
        assert ui.dialog.IsShown() and not ui.dialog._closing
        ui.controller.end_generation()
        assert ui.controller.view._mutations_enabled
        assert ui.controller.output_choice.IsEnabled()
        assert ui.controller.session.output_variant == "B"
        assert ui.controller.view.output_variant == "B"
        ui.dialog.quit_dialog()
        assert ui.controller.closed and not ui.controller.timer.IsRunning()
        wait_until(ui.wx, lambda: not ui.dialog)

    window_ui.run(check)
