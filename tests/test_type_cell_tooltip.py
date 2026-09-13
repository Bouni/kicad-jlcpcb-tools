"""Type-cell hover behavior using stateful wx windows and a controllable clock.

These exercise the real controller, not native tooltip display. Native rendering
and mouse delivery remain a separate GUI check.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from .test_part_type_tooltip import _Font, _Sizer, _Window as _ContentWindow
from .wx_harness import load, load_siblings, module, package_stubs, wx_stubs


class _Point(tuple):
    def __new__(cls, x: int = 0, y: int = 0) -> _Point:
        return super().__new__(cls, (x, y))

    x = property(lambda self: self[0])
    y = property(lambda self: self[1])


class _Rect:
    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self.x, self.y, self.width, self.height = x, y, width, height

    def Contains(self, point: _Point) -> bool:
        return (
            self.x <= point.x < self.x + self.width
            and self.y <= point.y < self.y + self.height
        )


class _Window:
    def __init__(self, parent: Any = None) -> None:
        self.parent = parent
        self.bindings: dict[Any, list[Callable]] = {}
        self.binding_sources: dict[tuple[Any, Callable], Any] = {}
        self.origin = _Point()
        self.size = _Point(240, 160)
        self.children: list[Any] = []
        self.font = _Font()
        self.scale = 1
        self.shown = True
        self.enabled = True
        self.active = True
        self.destroyed = False

    def Bind(self, event: Any, handler: Callable, source: Any = None) -> None:
        self.bindings.setdefault(event, []).append(handler)
        self.binding_sources[event, handler] = source

    def Unbind(self, event: Any, **kwargs: Any) -> None:
        handler = kwargs.get("handler")
        if handler in self.bindings.get(event, []):
            self.bindings[event].remove(handler)
            self.binding_sources.pop((event, handler), None)

    def emit(self, event_type: Any, *, source: Any = None) -> MagicMock:
        event = MagicMock()
        event.GetEventObject.return_value = self
        event.GetTimer.return_value = source
        event.GetPosition.return_value = _Point(10, 30)
        event.Dragging.return_value = False
        for handler in self.bindings.get(event_type, []):
            bound_source = self.binding_sources[event_type, handler]
            if bound_source is None or bound_source is source:
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
        return _Point(point.x + self.origin.x, point.y + self.origin.y)

    def ScreenToClient(self, point: _Point) -> _Point:
        return _Point(point.x - self.origin.x, point.y - self.origin.y)

    def GetClientRect(self) -> _Rect:
        return _Rect(0, 0, *self.size)

    def GetScreenRect(self) -> _Rect:
        return _Rect(*self.origin, *self.size)

    def GetFont(self) -> _Font:
        return self.font

    def GetCharHeight(self) -> int:
        return 16

    def __bool__(self) -> bool:
        return not self.destroyed


class _Control(_Window):
    def __init__(self, parent: _Window) -> None:
        super().__init__(parent)
        self.origin = _Point(240, 160)
        self.body = _Window(self)
        self.body.origin = self.ClientToScreen(_Point(4, 24))
        self.body.size = _Point(232, 132)
        self.row = SimpleNamespace(IsOk=lambda: True)
        self.column = SimpleNamespace(GetModelColumn=lambda: 7)
        self.other_column = SimpleNamespace(GetModelColumn=lambda: 2)
        self.horizontal_scroll = 0
        self.hit_points: list[_Point] = []

    def GetMainWindow(self) -> _Window:
        return self.body

    def HitTest(self, point: _Point) -> tuple:
        self.hit_points.append(point)
        body_point = self.body.ScreenToClient(self.ClientToScreen(point))
        if not self.body.GetClientRect().Contains(body_point) or body_point.y >= 72:
            return None, None
        column_x = body_point.x + self.horizontal_scroll
        if column_x < 80:
            return self.row, self.column
        if column_x < 160:
            return self.row, self.other_column
        return None, None


class _Timer:
    def __init__(
        self, owner: _Control, clock: SimpleNamespace, event_type: Any
    ) -> None:
        self.owner = owner
        self.clock = clock
        self.event_type = event_type
        self.running = False
        self.interval = 0.0
        self.one_shot = False
        self.next_due: float | None = None
        self.delivered = 0
        clock.timers.append(self)

    def Start(self, milliseconds: int, oneShot: bool = False) -> bool:
        assert milliseconds > 0
        self.interval = milliseconds / 1000
        self.one_shot = oneShot
        self.running = True
        self.next_due = round(self.clock.now + self.interval, 9)
        return True

    def Stop(self) -> None:
        self.running = False
        self.next_due = None

    def IsRunning(self) -> bool:
        return self.running

    def deliver(self) -> None:
        """Deliver a scheduled tick only while armed and after its due time."""
        assert self.running and self.next_due is not None
        assert self.clock.now >= self.next_due
        if self.one_shot:
            self.Stop()
        else:
            self.next_due = round(self.next_due + self.interval, 9)
        self.delivered += 1
        self.owner.emit(self.event_type, source=self)


class _Popup(_Window):
    def __init__(self, parent: _Control) -> None:
        super().__init__(parent)
        self.shown = False
        self.focus_requested = False
        self.position = None

    def Position(self, point: _Point, offset: Any) -> None:
        self.position = (point, offset)
        self.origin = _Point(point.x + offset.x, point.y + offset.y)

    def Show(self, show: bool = True) -> None:
        self.shown = show

    def Hide(self) -> None:
        self.shown = False

    def Destroy(self) -> None:
        self.destroyed = True
        self.shown = False

    def SetFocus(self) -> None:
        self.focus_requested = True


class _FeePopup(_ContentWindow, _Popup):
    """Use the fee-table sizing controls with the hover window lifecycle."""

    def __init__(self, parent: _Control, *, flags: int = 0) -> None:
        _ContentWindow.__init__(self, parent, flags=flags)
        self.origin = _Point()
        self.focus_requested = False
        self.position = None


@pytest.fixture
def hover(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Hold pointer, hit-test results, windows, and clock across bound events."""
    top = _Window()
    control = _Control(top)
    state = SimpleNamespace(
        now=0.0,
        point=control.ClientToScreen(_Point(20, 40)),
        window=control.body,
        buttons=set(),
        standard=False,
        standard_shown=False,
        timers=[],
    )
    wx = wx_stubs(
        EVT_TIMER=object(),
        Timer=lambda owner: _Timer(owner, state, wx["wx"].EVT_TIMER),
        Point=_Point,
        Size=_Point,
        Rect=_Rect,
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

    def poll(seconds: float = 0.1) -> None:
        """Advance time, dispatching only due events from running timers."""
        target = round(state.now + seconds, 9)
        while True:
            due = [
                timer
                for timer in state.timers
                if timer.next_due is not None and timer.next_due <= target
            ]
            if not due:
                break
            timer = min(due, key=lambda candidate: candidate.next_due)
            state.now = timer.next_due
            timer.deliver()
        state.now = target

    def late_timer() -> None:
        """Explicitly inject a queued event after Stop without restarting time."""
        control.emit(wx["wx"].EVT_TIMER, source=controller._timer)

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
        late_timer=late_timer,
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
    hover.state.point = hover.control.ClientToScreen(_Point(30, 40))
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
    hover.state.point = hover.control.ClientToScreen(_Point(20, 70))
    hover.control.body.emit(hover.wx.EVT_MOTION)
    assert hover.visible() == [], "A tooltip must not remain over the previous row"
    hover.poll(0.7)
    replacement = hover.visible()[0]
    assert replacement is not original
    assert replacement.position[0] == hover.state.point


@pytest.mark.parametrize(
    "location",
    [
        "header",
        "outside",
        "scrollbar",
        "empty",
        "popup",
        "hidden",
        "disabled",
        "inactive",
    ],
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
        hover.state.point = hover.control.ClientToScreen(_Point(20, 10))
    elif location == "outside":
        hover.state.point = hover.control.ClientToScreen(_Point(-1, 40))
        hover.state.window = _Window()
    elif location == "scrollbar":
        hover.state.point = hover.control.ClientToScreen(_Point(238, 40))
    elif location == "empty":
        hover.state.point = hover.control.ClientToScreen(_Point(20, 130))
    elif location == "popup":
        hover.state.point = hover.visible()[0].ClientToScreen(_Point(10, 10))
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
    assert not hover.controller._timer.IsRunning()
    delivered = hover.controller._timer.delivered
    hover.poll(1.0)
    hover.poll(1.0)
    assert hover.controller._timer.delivered == delivered
    hover.late_timer()
    hover.poll(0.7)
    hover.late_timer()
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
    assert not hover.controller._timer.IsRunning()
    hover.poll(1.0)
    hover.poll(1.0)
    hover.late_timer()
    hover.poll(0.7)
    hover.late_timer()
    assert hover.visible() == []


@pytest.mark.parametrize("help_kind", ["type", "standard"])
@pytest.mark.parametrize("already_visible", [False, True])
def test_background_window_lookup_does_not_suppress_foreground_cell_help(
    hover: SimpleNamespace, help_kind: str, already_visible: bool
) -> None:
    """A geometric lookup can report the covered dialog behind the active table."""
    if help_kind == "standard":
        hover.control.column = SimpleNamespace(GetModelColumn=lambda: 2)
        hover.state.standard = True
    if already_visible:
        hover.control.body.emit(hover.wx.EVT_MOTION)
        hover.poll(0.7)
        assert bool(hover.visible()) == (help_kind == "type")
        assert hover.state.standard_shown == (help_kind == "standard")

    background = _Window()
    background.active = False
    hover.state.window = background
    hover.control.body.emit(hover.wx.EVT_MOTION)
    hover.poll(0.7)

    assert bool(hover.visible()) == (help_kind == "type")
    assert hover.state.standard_shown == (help_kind == "standard")


def test_timer_only_hover_uses_running_periodic_timer_and_its_own_source(
    hover: SimpleNamespace,
) -> None:
    """A stationary pointer gets help and later refreshes without mouse events."""
    foreign_timer = _Timer(hover.control, hover.state, hover.wx.EVT_TIMER)
    hover.state.standard = True
    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 2)
    hover.control.emit(hover.wx.EVT_TIMER, source=foreign_timer)
    assert not hover.state.standard_shown
    hover.poll(0.1)
    assert hover.state.standard_shown

    hover.control.column = SimpleNamespace(GetModelColumn=lambda: 7)
    hover.poll(0.5)
    assert not hover.state.standard_shown
    assert hover.visible() == []
    hover.poll(0.3)
    assert len(hover.visible()) == 1
    hover.control.row = None
    hover.poll(0.1)
    assert hover.visible() == []


def test_screen_coordinates_use_control_origin_and_live_scrolled_columns(
    hover: SimpleNamespace,
) -> None:
    """Body and control coordinates differ; scrolling changes the model column."""
    hover.state.standard = True
    hover.control.body.emit(hover.wx.EVT_MOTION)
    hover.poll(0.7)
    assert len(hover.visible()) == 1
    assert hover.control.hit_points[-1] == _Point(20, 40)
    assert hover.control.body.ScreenToClient(hover.state.point) == _Point(16, 16)

    hover.control.horizontal_scroll = 80
    hover.control.emit(hover.wx.EVT_SCROLLWIN)
    assert hover.visible() == []
    hover.poll(0.1)
    assert hover.state.standard_shown
    hover.control.horizontal_scroll = 0
    hover.poll(0.1)
    assert not hover.state.standard_shown
    hover.poll(0.7)
    assert len(hover.visible()) == 1


@pytest.mark.parametrize("help_kind", ["type", "standard"])
def test_popup_rect_excludes_underlying_cells_when_lookup_reports_the_table(
    hover: SimpleNamespace, help_kind: str
) -> None:
    """The popup can cover valid cells even when a geometric lookup misses it."""
    hover.poll(0.8)
    popup = hover.visible()[0]
    hover.state.standard = True
    if help_kind == "type":
        # Popup placement can flip around the pointer at a display edge.
        # Keep the pointer fixed so motion itself cannot dismiss the popup.
        popup.origin = _Point(hover.state.point.x - 10, hover.state.point.y - 10)
    else:
        hover.state.point = hover.control.ClientToScreen(_Point(100, 60))
    assert popup.GetScreenRect().Contains(hover.state.point)
    hover.poll(0.1)
    assert popup.destroyed
    assert not hover.state.standard_shown


@pytest.mark.parametrize("help_kind", ["type", "standard"])
def test_reactivating_owner_restarts_help_for_the_stationary_pointer(
    hover: SimpleNamespace, help_kind: str
) -> None:
    """A covering active dialog clears help; returning starts a fresh delay."""
    if help_kind == "standard":
        hover.control.column = SimpleNamespace(GetModelColumn=lambda: 2)
        hover.state.standard = True
    hover.poll(0.8)
    assert bool(hover.visible()) == (help_kind == "type")
    assert hover.state.standard_shown == (help_kind == "standard")
    hover.top.active = False
    hover.poll(1.0)
    assert hover.visible() == []
    assert not hover.state.standard_shown
    hover.top.active = True
    hover.poll(0.2)
    assert hover.visible() == []
    assert hover.state.standard_shown == (help_kind == "standard")
    hover.poll(0.6)
    assert bool(hover.visible()) == (help_kind == "type")


def test_control_without_separate_main_window_supports_hover_and_navigation(
    hover: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backends without a distinct body bind and test the control itself."""
    hover.controller.stop()
    monkeypatch.setattr(hover.control, "GetMainWindow", lambda: None)
    controller = hover.helper.TypeCellTooltip(
        hover.control, 7, lambda _item: False, lambda _active: None
    )
    event = hover.control.emit(hover.wx.EVT_MOTION)
    assert event.Skip.called
    hover.poll(0.7)
    assert len(hover.visible()) == 1
    event = hover.control.emit(hover.wx.EVT_MOUSEWHEEL)
    assert event.Skip.called
    assert hover.visible() == []
    controller.stop()


def test_real_controller_constructs_shows_dismisses_and_reopens_real_fee_table(
    hover: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connect real sibling modules through timer events and owned popup state."""
    hover.controller.stop()
    hover.control.font = _Font(18)
    hover.control.scale = 2
    colours = {
        hover.wx.SYS_COLOUR_INFOBK: "tooltip background",
        hover.wx.SYS_COLOUR_INFOTEXT: "tooltip foreground",
    }
    hover.wx.PopupWindow = _FeePopup
    hover.wx.StaticText = _ContentWindow
    hover.wx.BoxSizer = _Sizer
    hover.wx.FlexGridSizer = _Sizer
    hover.wx.SystemSettings = SimpleNamespace(GetColour=colours.__getitem__)
    expected_labels = [
        "Type",
        "Feeder loading fee",
        "Basic",
        "None for Economic assembly; yes for Standard assembly",
        "Preferred",
        "None for Economic assembly; yes for Standard assembly",
        "Extended",
        "Yes",
        "Blank",
        "No assigned part or type information available",
    ]
    with load_siblings(
        "real_type_hover_integration",
        ("part_type_tooltip", "type_cell_tooltip"),
        {"wx": hover.wx, "wx.dataview": hover.wx.dataview},
    ) as siblings:
        helper = siblings["type_cell_tooltip"]
        monkeypatch.setattr(
            helper, "time", SimpleNamespace(monotonic=lambda: hover.state.now)
        )
        hidden = siblings["part_type_tooltip"].create_type_fee_popup(hover.control)
        assert not hidden.shown
        assert hidden.size == hidden.sizer.GetMinSize()
        hidden.Destroy()
        assert hover.control.children == []

        for _ in range(2):
            controller = helper.TypeCellTooltip(
                hover.control, 7, lambda _item: False, lambda _active: None
            )
            hover.control.body.emit(hover.wx.EVT_MOTION)
            hover.poll(0.3)
            assert hover.control.children == []
            hover.poll(0.4)
            assert len(hover.control.children) == 1
            popup = hover.control.children[0]
            assert popup.parent is hover.control
            assert popup.shown and not popup.destroyed
            assert not popup.focus_requested
            assert popup.position[0] == hover.state.point
            assert [
                " ".join(cell.label.split()) for cell in popup.children
            ] == expected_labels
            assert all(cell.parent is popup for cell in popup.children)
            assert popup.font.size == 18
            assert popup.scale == 2
            assert popup.size == popup.sizer.GetMinSize()
            assert all(dimension > 0 for dimension in popup.size)
            labels = list(popup.children)
            hover.control.body.emit(hover.wx.EVT_LEFT_DOWN)
            assert popup.destroyed and not popup.shown
            assert all(label.destroyed for label in labels)
            assert hover.control.children == []
            controller.stop()
            hover.poll(1.0)
            hover.control.emit(hover.wx.EVT_TIMER, source=controller._timer)
            assert hover.control.children == []
