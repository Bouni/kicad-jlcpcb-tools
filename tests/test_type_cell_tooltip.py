"""Type-cell hover behavior using stateful wx windows and a controllable clock.

These exercise the real controller, not native tooltip display. Native rendering
and mouse delivery remain a separate GUI check.
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .wx_harness import load, module, package_stubs, wx_stubs


class _Point(tuple):
    def __new__(cls, x: int = 0, y: int = 0) -> "_Point":
        return super().__new__(cls, (x, y))

    x = property(lambda self: self[0])
    y = property(lambda self: self[1])


class _Window:
    def __init__(self, parent: Any = None) -> None:
        self.parent = parent
        self.bindings: dict[Any, list[Callable]] = {}
        self.shown = True
        self.enabled = True
        self.active = True
        self.destroyed = False

    def Bind(self, event: Any, handler: Callable, *_args: Any, **_kwargs: Any) -> None:
        self.bindings.setdefault(event, []).append(handler)

    def Unbind(self, event: Any, **kwargs: Any) -> None:
        handler = kwargs.get("handler")
        if handler in self.bindings.get(event, []):
            self.bindings[event].remove(handler)

    def emit(self, event_type: Any) -> MagicMock:
        event = MagicMock()
        event.GetEventObject.return_value = self
        event.GetPosition.return_value = _Point(10, 30)
        event.Dragging.return_value = False
        for handler in self.bindings.get(event_type, []):
            handler(event)
        return event

    def GetParent(self) -> Any:
        return self.parent

    def IsDescendant(self, window: Any) -> bool:
        while window is not None:
            if window is self:
                return True
            window = window.GetParent()
        return False

    def IsShownOnScreen(self) -> bool:
        return self.shown and not self.destroyed

    def IsEnabled(self) -> bool:
        return self.enabled

    def IsActive(self) -> bool:
        return self.active

    def ClientToScreen(self, point: _Point) -> _Point:
        return point

    def ScreenToClient(self, point: _Point) -> _Point:
        return point

    def GetCharHeight(self) -> int:
        return 16

    def __bool__(self) -> bool:
        return not self.destroyed


class _Control(_Window):
    def __init__(self, parent: _Window) -> None:
        super().__init__(parent)
        self.body = _Window(self)
        self.row = SimpleNamespace(IsOk=lambda: True)
        self.column = SimpleNamespace(GetModelColumn=lambda: 7)

    def GetMainWindow(self) -> _Window:
        return self.body

    def HitTest(self, _point: _Point) -> tuple:
        return self.row, self.column


class _Timer:
    def __init__(self, owner: _Control) -> None:
        self.owner = owner
        self.running = False

    def Start(self, _milliseconds: int, **_kwargs: Any) -> None:
        self.running = True

    def Stop(self) -> None:
        self.running = False

    def IsRunning(self) -> bool:
        return self.running


class _Popup(_Window):
    def __init__(self, parent: _Control) -> None:
        super().__init__(parent)
        self.shown = False
        self.focus_requested = False
        self.position = None

    def Position(self, point: _Point, offset: Any) -> None:
        self.position = (point, offset)

    def Show(self, show: bool = True) -> None:
        self.shown = show

    def Hide(self) -> None:
        self.shown = False

    def Destroy(self) -> None:
        self.destroyed = True
        self.shown = False

    def SetFocus(self) -> None:
        self.focus_requested = True


@pytest.fixture
def hover(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Hold pointer, hit-test results, windows, and clock across bound events."""
    top = _Window()
    control = _Control(top)
    state = SimpleNamespace(
        now=0.0,
        point=_Point(10, 30),
        window=control.body,
        buttons=set(),
        standard=False,
        standard_shown=False,
    )
    wx = wx_stubs(
        Timer=_Timer,
        Point=_Point,
        Size=_Point,
        GetMousePosition=lambda: state.point,
        FindWindowAtPoint=lambda _point: state.window,
        GetMouseState=lambda: SimpleNamespace(
            LeftIsDown=lambda: "left" in state.buttons,
            MiddleIsDown=lambda: "middle" in state.buttons,
            RightIsDown=lambda: "right" in state.buttons,
            Aux1IsDown=lambda: "aux1" in state.buttons,
            Aux2IsDown=lambda: "aux2" in state.buttons,
        ),
        GetTopLevelParent=lambda _window: top,
    )
    package = "type_cell_controller_tests"
    replacements = {**package_stubs(package), **wx}
    # The popup has independent content/layout coverage; keep this suite focused
    # on the actual controller's lifetime and event transitions.
    replacements[f"{package}.part_type_tooltip"] = module(
        f"{package}.part_type_tooltip", create_type_fee_popup=MagicMock()
    )
    helper = load(package, "type_cell_tooltip", replacements)
    monkeypatch.setattr(helper, "time", SimpleNamespace(monotonic=lambda: state.now))
    popups: list[_Popup] = []

    def create_popup(parent: _Control) -> _Popup:
        popup = _Popup(parent)
        popups.append(popup)
        return popup

    monkeypatch.setattr(helper, "create_type_fee_popup", create_popup)

    def standard_help(active: bool) -> None:
        state.standard_shown = active

    controller = helper.TypeCellTooltip(
        control, 7, lambda _item: state.standard, standard_help
    )

    def poll(seconds: float = 0.0) -> None:
        state.now += seconds
        control.emit(wx["wx"].EVT_TIMER)

    def visible() -> list[_Popup]:
        return [popup for popup in popups if popup.shown and not popup.destroyed]

    return SimpleNamespace(
        wx=wx["wx"],
        helper=helper,
        top=top,
        control=control,
        state=state,
        controller=controller,
        popups=popups,
        poll=poll,
        visible=visible,
    )


def test_type_cell_shows_delayed_help_without_focus_or_standard_warning(
    hover: SimpleNamespace,
) -> None:
    """Delay Type-only help without stealing focus or resetting stable help."""
    hover.state.standard = True
    event = hover.control.body.emit(hover.wx.EVT_MOTION)
    assert event.Skip.called
    hover.poll(0.3)
    assert hover.visible() == []
    hover.poll(0.4)
    assert len(hover.visible()) == 1
    assert not hover.state.standard_shown
    assert not hover.visible()[0].focus_requested
    hover.poll(1.0)
    assert len(hover.popups) == 1, "Stationary hover must not recreate help"


def test_type_help_is_based_on_model_column_including_blank_type(
    hover: SimpleNamespace,
) -> None:
    """A valid blank Type cell still provides the same classification legend."""
    # No cell text is supplied: a blank classification must still receive help.
    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 7)
    hover.poll()
    hover.poll(0.7)
    assert len(hover.visible()) == 1
    # Native hit-testing already resolves scrolling/reordering into model IDs.
    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 2)
    hover.state.standard = True
    hover.poll()
    assert hover.visible() == []
    assert hover.state.standard_shown
    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 7)
    hover.poll()
    assert not hover.state.standard_shown
    hover.poll(0.7)
    assert len(hover.visible()) == 1


def test_empty_table_never_displays_type_or_standard_help(
    hover: SimpleNamespace,
) -> None:
    """No assigned rows means there is no cell to explain, including on reopen."""
    hover.control.row = None
    hover.control.column = None
    hover.state.standard = True
    hover.control.body.emit(hover.wx.EVT_MOTION)
    hover.poll(0.7)
    hover.poll(1.0)
    assert hover.visible() == []
    assert not hover.state.standard_shown


def test_motion_restarts_delay_and_polling_clears_a_stationary_stale_row(
    hover: SimpleNamespace,
) -> None:
    """Polling follows pointer movement and detects emptied or filtered rows."""
    hover.poll()
    hover.poll(0.4)
    hover.state.point = _Point(20, 30)
    hover.poll()
    hover.poll(0.4)
    assert hover.visible() == []
    hover.poll(0.3)
    assert len(hover.visible()) == 1
    hover.control.row = SimpleNamespace(IsOk=lambda: False)
    hover.poll()
    assert hover.visible() == []
    assert not hover.state.standard_shown


def test_moving_to_another_type_cell_repositions_help_after_a_new_delay(
    hover: SimpleNamespace,
) -> None:
    """Moving between Type rows must not leave help at the previous location."""
    hover.poll()
    hover.poll(0.7)
    original = hover.visible()[0]
    hover.state.point = _Point(10, 60)
    hover.control.body.emit(hover.wx.EVT_MOTION)
    assert hover.visible() == [], "A tooltip must not remain over the previous row"
    hover.poll(0.7)
    replacement = hover.visible()[0]
    assert replacement is not original
    assert replacement.position[0] == hover.state.point


@pytest.mark.parametrize(
    "location",
    ["header", "outside", "popup", "hidden", "disabled", "inactive"],
)
def test_help_clears_when_hover_is_no_longer_eligible(
    hover: SimpleNamespace,
    location: str,
) -> None:
    """Leave no stale help outside an active, enabled table body cell."""
    hover.poll()
    hover.poll(0.7)
    assert hover.visible()
    if location == "header":
        hover.control.row = SimpleNamespace(IsOk=lambda: False)
        hover.control.column = None
    elif location == "outside":
        hover.state.window = _Window()
    elif location == "popup":
        hover.state.window = _Window(hover.visible()[0])
    elif location == "hidden":
        hover.control.shown = False
    elif location == "disabled":
        hover.control.enabled = False
    elif location == "inactive":
        hover.top.active = False
    hover.poll()
    assert hover.visible() == []
    assert not hover.state.standard_shown


@pytest.mark.parametrize("button", ["left", "middle", "right", "aux1", "aux2"])
def test_each_mouse_button_suppresses_help_until_released(
    hover: SimpleNamespace,
    button: str,
) -> None:
    """Use wx.MouseState's actual accessors for every supported button."""
    hover.poll()
    hover.poll(0.7)
    assert hover.visible()
    hover.state.buttons.add(button)
    hover.poll()
    assert hover.visible() == []
    assert not hover.state.standard_shown
    hover.poll(1.0)
    assert hover.visible() == []
    hover.state.buttons.clear()
    hover.poll()
    hover.poll(0.7)
    assert len(hover.visible()) == 1


@pytest.mark.parametrize(
    "event_name",
    ["EVT_LEAVE_WINDOW", "EVT_MOUSEWHEEL", "EVT_LEFT_DOWN", "EVT_KEY_DOWN"],
)
def test_navigation_dismisses_help_and_preserves_control_events(
    hover: SimpleNamespace,
    event_name: str,
) -> None:
    """Selection, keyboard input, and scrolling retain their native processing."""
    hover.poll()
    hover.poll(0.7)
    assert hover.visible()
    event = hover.control.body.emit(getattr(hover.wx, event_name))
    assert event.Skip.called
    assert hover.visible() == []


@pytest.mark.parametrize(
    "event_name",
    [
        "EVT_SIZE",
        "EVT_SCROLLWIN",
        "EVT_DATAVIEW_COLUMN_SORTED",
        "EVT_DATAVIEW_COLUMN_REORDERED",
    ],
)
def test_table_layout_events_clear_help_without_swallowing_sort_or_scroll(
    hover: SimpleNamespace,
    event_name: str,
) -> None:
    """Discard the prior hover before changes to current table geometry."""
    hover.poll()
    hover.poll(0.7)
    assert hover.visible()
    namespace = hover.wx.dataview if "DATAVIEW" in event_name else hover.wx
    event = hover.control.emit(getattr(namespace, event_name))
    assert event.Skip.called
    assert hover.visible() == []


def test_metadata_refresh_updates_standard_help_without_moving_pointer(
    hover: SimpleNamespace,
) -> None:
    """Background metadata changes immediately refresh non-Type row help."""
    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 2)
    hover.state.standard = True
    hover.poll()
    assert hover.state.standard_shown
    hover.state.standard = False
    hover.controller.refresh()
    assert not hover.state.standard_shown
    hover.state.standard = True
    hover.controller.refresh()
    assert hover.state.standard_shown


def test_stop_dismisses_help_and_disarms_late_timer_events(
    hover: SimpleNamespace,
) -> None:
    """Queued timers cannot recreate a popup after the dialog has closed."""
    hover.poll()
    hover.poll(0.7)
    assert hover.visible()
    hover.controller.stop()
    assert hover.visible() == []
    hover.poll(1.0)
    hover.poll(1.0)
    assert hover.visible() == []


def test_popup_failure_does_not_break_hover_or_repeat_on_every_timer_tick(
    hover: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed help popup leaves row navigation and other help operational."""
    factory = MagicMock(side_effect=RuntimeError("Popup unavailable"))
    monkeypatch.setattr(hover.helper, "create_type_fee_popup", factory)
    hover.poll()
    hover.poll(0.7)
    hover.poll(1.0)
    assert factory.call_count == 1
    assert hover.visible() == []
    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 2)
    hover.state.standard = True
    hover.poll()
    assert hover.state.standard_shown


@pytest.mark.parametrize("failure_step", ["Position", "Show"])
def test_partial_popup_failure_destroys_window_and_preserves_navigation(
    hover: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure_step: str,
) -> None:
    """Dispose of partially displayed help without interrupting the table."""
    popup = _Popup(hover.control)
    monkeypatch.setattr(
        popup, failure_step, MagicMock(side_effect=RuntimeError("Failed"))
    )
    monkeypatch.setattr(hover.helper, "create_type_fee_popup", lambda _parent: popup)
    hover.poll()
    hover.poll(0.7)
    assert popup.destroyed
    assert not popup.shown
    event = hover.control.body.emit(hover.wx.EVT_LEFT_DOWN)
    assert event.Skip.called


def test_destroying_popup_does_not_stop_owner_hover_help(
    hover: SimpleNamespace,
) -> None:
    """Only owner destruction stops polling, even when child events propagate."""
    hover.poll()
    hover.poll(0.7)
    popup = hover.visible()[0]
    event = MagicMock()
    event.GetEventObject.return_value = popup
    for handler in hover.control.bindings.get(hover.wx.EVT_WINDOW_DESTROY, []):
        handler(event)
    hover.controller.dismiss()
    hover.poll()
    hover.poll(0.7)
    assert len(hover.visible()) == 1
    hover.control.emit(hover.wx.EVT_WINDOW_DESTROY)
    assert hover.visible() == []
    hover.poll(1.0)
    hover.poll(1.0)
    assert hover.visible() == []
