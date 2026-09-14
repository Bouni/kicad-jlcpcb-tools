"""Keep independent variant choices when simultaneous native windows close."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from .native_window_support import choose_output, window_ui
from .native_wx_support import pump, wait_until

__all__ = ["window_ui"]
pytestmark = pytest.mark.native_wx


def _seed_preferences(ui: Any, persisted: bool) -> None:
    """Distinguish effective false controls from false values actually stored."""
    document = deepcopy(ui.settings)
    document.pop("variants", None)
    if persisted:
        document["variants"] = {
            "boards": {
                ui.board.GetFileName(): {
                    "output_variant": "A",
                    "display_preferences": {
                        "differences_only": False,
                        "show_footprint_library": False,
                    },
                }
            }
        }
    ui.settings_path.write_text(json.dumps(document))


@contextmanager
def _second_window(ui: Any) -> Iterator[Any]:
    """Construct a separately loaded settings snapshot for the same live PCB."""
    frame = ui.mainwindow.JLCPCBTools(ui.dialog.GetParent(), ui.provider)
    try:
        frame.Show()
        pump(ui.wx)
        second = SimpleNamespace(
            wx=ui.wx,
            dialog=frame,
            controller=frame._variant_controller,
        )
        assert frame.IsShown() and frame.IsEnabled()
        assert second.controller.cache.board_path == ui.cache.board_path
        assert frame.settings is not ui.dialog.settings
        yield second
    finally:
        if frame and not frame.IsBeingDeleted():
            frame.Close()
        wait_until(ui.wx, lambda: not frame)


def _check(ui: Any, attribute: str) -> None:
    """Change a real checkbox and deliver its native command event."""
    control = getattr(ui.controller, attribute)
    assert not control.GetValue()
    control.SetValue(True)
    event = ui.wx.CommandEvent(ui.wx.wxEVT_CHECKBOX, control.GetId())
    event.SetEventObject(control)
    event.SetInt(1)
    control.GetEventHandler().ProcessEvent(event)
    pump(ui.wx)
    assert control.GetValue()


def _close(ui: Any) -> None:
    """Exercise the complete frame close handler, including its ordinary save."""
    frame = ui.dialog
    frame.Close()
    wait_until(ui.wx, lambda: not frame)


def _saved_board(ui: Any) -> dict[str, Any]:
    """Read the file another process or newly opened window would receive."""
    document = json.loads(ui.settings_path.read_text())
    return document["variants"]["boards"][ui.board.GetFileName()]


@pytest.mark.parametrize("persisted", [False, True], ids=["absent", "saved-defaults"])
@pytest.mark.parametrize(
    "reverse_close", [False, True], ids=["first-last", "last-first"]
)
def test_simultaneous_windows_preserve_independent_display_edits(
    window_ui: Any, persisted: bool, reverse_close: bool
) -> None:
    """Each window's unchanged false controls must preserve the other's true choice."""
    _seed_preferences(window_ui, persisted)

    def save(ui: Any) -> None:
        with _second_window(ui) as second:
            _check(ui, "differences")
            _check(second, "show_footprint_library")
            assert not ui.controller.show_footprint_library.GetValue()
            assert not second.controller.differences.GetValue()
            for window in (second, ui) if reverse_close else (ui, second):
                _close(window)
            display = _saved_board(ui)["display_preferences"]
            assert display["differences_only"] is True
            assert display["show_footprint_library"] is True
            assert not ui.messages

    def reopen(ui: Any) -> None:
        assert ui.controller.differences.GetValue()
        assert ui.controller.show_footprint_library.GetValue()
        assert ui.controller.model.show_footprint_library
        assert not ui.messages

    window_ui.run(save, reopen)


@pytest.mark.parametrize("persisted", [False, True], ids=["absent", "saved-defaults"])
@pytest.mark.parametrize("choice", ["output", "filter"])
def test_unchanged_stale_window_close_preserves_explicit_variant_choice(
    window_ui: Any, persisted: bool, choice: str
) -> None:
    """An old window cannot replay its startup defaults over a newer saved choice."""
    _seed_preferences(window_ui, persisted)

    def save(ui: Any) -> None:
        with _second_window(ui) as stale:
            if choice == "output":
                choose_output(ui, "B")
                assert stale.controller.session.output_variant == "A"
            else:
                _check(ui, "differences")
                assert not stale.controller.differences.GetValue()
            _close(ui)
            expected = _saved_board(ui)
            _close(stale)
            assert _saved_board(ui) == expected
            if choice == "output":
                assert expected["output_variant"] == "B"
            else:
                assert expected["display_preferences"]["differences_only"] is True
            assert not ui.messages

    def reopen(ui: Any) -> None:
        assert ui.controller.session.output_variant == (
            "B" if choice == "output" else "A"
        )
        assert ui.controller.differences.GetValue() is (choice == "filter")
        assert not ui.controller.show_footprint_library.GetValue()
        assert not ui.messages

    window_ui.run(save, reopen)


@pytest.mark.parametrize("persisted", [False, True], ids=["absent", "saved-defaults"])
@pytest.mark.parametrize(
    "reverse_close", [False, True], ids=["first-last", "last-first"]
)
def test_output_choice_and_other_windows_filter_survive_both_close_orders(
    window_ui: Any, persisted: bool, reverse_close: bool
) -> None:
    """Immediate output saves and deferred display saves preserve separate changes."""
    _seed_preferences(window_ui, persisted)

    def save(ui: Any) -> None:
        with _second_window(ui) as second:
            choose_output(ui, "B")
            _check(second, "differences")
            assert second.controller.session.output_variant == "A"
            assert not ui.controller.differences.GetValue()
            for window in (second, ui) if reverse_close else (ui, second):
                _close(window)
            saved = _saved_board(ui)
            assert saved["output_variant"] == "B"
            assert saved["display_preferences"]["differences_only"] is True
            assert not ui.messages

    def reopen(ui: Any) -> None:
        assert ui.controller.session.output_variant == "B"
        assert ui.controller.output_choice.GetStringSelection() == "B"
        assert ui.controller.differences.GetValue()
        assert not ui.messages

    window_ui.run(save, reopen)
