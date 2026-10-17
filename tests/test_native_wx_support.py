"""Exercise deadline ownership without relying on a desktop or wall-clock sleeps."""

from collections.abc import Callable
from contextlib import suppress
import heapq
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from . import native_wx_support as support


class FakeLoop:
    """Keep nested loops distinct, including requests made after EndModal."""

    def __init__(self, wx: "FakeWx") -> None:
        self.wx = wx
        self.exiting = False
        self.exit_calls = 0

    def Run(self) -> None:
        """Keep this loop active until its own exit request."""
        self.wx.loops.append(self)
        try:
            while not self.exiting:
                self.wx.dispatch()
        finally:
            assert self.wx.loops.pop() is self

    def ScheduleExit(self, _code: int) -> None:
        """Count every request, including redundant requests on the same loop."""
        self.exit_calls += 1
        self.exiting = True


class FakeTimer:
    """Support one-shot timers and restarts used by wait_until."""

    def __init__(self, wx: "FakeWx", delay: int, callback: Callable[[], None]) -> None:
        self.wx = wx
        self.callback = callback
        self.running = False
        self.Start(delay)

    def Start(self, delay: int) -> None:
        """Queue this timer in the virtual event scheduler."""
        self.running = True
        self.wx.queue(delay, self.fire)

    def fire(self) -> None:
        """Ignore timer events stopped before they were dispatched."""
        if self.running:
            self.running = False
            self.callback()

    def Stop(self) -> None:
        """Prevent an already queued callback from running."""
        self.running = False


class FakeWindow:
    """Retain window existence and distinguish closing from final destruction."""

    def __init__(self, wx: "FakeWx", parent: Any = None, **_kwargs: Any) -> None:
        self.wx = wx
        self.parent = parent
        self.alive = True
        self.close_calls = 0
        wx.windows.append(self)

    def __bool__(self) -> bool:
        """Match wx wrappers becoming false after native destruction."""
        return self.alive

    def Show(self) -> None:
        """Keep visibility irrelevant to the scheduler."""
        pass

    def Close(self) -> None:
        """Record potentially premature close requests."""
        self.close_calls += 1
        self.Destroy()

    def Destroy(self) -> None:
        """Remove the window from the native top-level inventory."""
        self.alive = False
        self.wx.windows.remove(self)


class FakeDialog(FakeWindow):
    """Own a distinct modal loop, as wx.Dialog does."""

    def __init__(self, wx: "FakeWx", parent: Any = None) -> None:
        super().__init__(wx, parent)
        self.modal = False
        self.end_calls = 0
        self.loop = FakeLoop(wx)

    def IsModal(self) -> bool:
        """Expose the state changed synchronously by EndModal."""
        return self.modal

    def ShowModal(self) -> None:
        """Enter a nested loop on the interaction callback's call stack."""
        entered = []
        for hook in reversed(self.wx.hooks):
            if hook.Enter(self) != self.wx.ID_NONE:
                return
            entered.append(hook)
        self.modal = True
        try:
            self.loop.Run()
        finally:
            for hook in entered:
                hook.Exit(self)

    def EndModal(self, _code: int) -> None:
        """Exit this loop once and reject a second modal closure."""
        self.end_calls += 1
        assert self.modal, "EndModal must not be called twice"
        self.modal = False
        # wxGTK EndModal only exits a loop when IsRunning(), meaning active.
        # Clearing an outer modal's flag while an inner loop runs strands it.
        if self.wx.loops[-1] is self.loop:
            self.loop.ScheduleExit(0)


class FakeApp:
    """Track application ownership and its separate outer event loop."""

    def __init__(self, wx: "FakeWx") -> None:
        self.wx = wx
        self.top = None
        self.destroyed = False
        self.loop = FakeLoop(wx)

    def GetTopWindow(self) -> Any:
        """Return the current top window."""
        return self.top

    def SetTopWindow(self, window: Any) -> None:
        """Change the window restored by run_native after a reused app."""
        self.top = window

    def MainLoop(self) -> None:
        """Dispatch the app's outermost event loop."""
        self.loop.Run()

    def ExitMainLoop(self) -> None:
        """Request only the main loop's exit."""
        self.loop.ScheduleExit(0)

    def Destroy(self) -> None:
        """Release this app without silently destroying any remaining window."""
        self.destroyed = True
        self.wx.app = None


class FakeWx:
    """Dispatch scheduled work with the same call stack as nested wx loops."""

    ID_CANCEL = 5101
    ID_NONE = -3
    Dialog = FakeDialog

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0
        self.counter = 0
        self.events: list[tuple[int, int, Callable[[], None]]] = []
        self.windows: list[FakeWindow] = []
        self.loops: list[FakeLoop] = []
        self.wait_loops: list[FakeLoop] = []
        self.app: Optional[FakeApp] = None
        self.created_app: Optional[FakeApp] = None
        self.hooks: list[Any] = []
        wx = self

        class ModalDialogHook:
            def Register(self) -> None:
                """Scope hook registration to the native workflow."""
                wx.hooks.append(self)

            def Unregister(self) -> None:
                """Remove the hook after success or failure."""
                wx.hooks.remove(self)

        self.ModalDialogHook = ModalDialogHook
        self.EventLoopBase = SimpleNamespace(GetActive=lambda: self.loops[-1])
        monkeypatch.setitem(sys.modules, "wx", self)
        monkeypatch.setattr(support.time, "monotonic", lambda: self.now / 1000)

    def queue(self, delay: int, callback: Callable[[], None]) -> None:
        """Preserve timer deadlines and FIFO order for simultaneous events."""
        self.counter += 1
        heapq.heappush(self.events, (self.now + delay, self.counter, callback))

    def dispatch(self) -> None:
        """Advance virtual time while retaining real nested callback stacks."""
        assert self.events, "native helper left a loop running without queued work"
        self.now, _sequence, callback = heapq.heappop(self.events)
        callback()

    def CallLater(self, delay: int, callback: Callable[[], None]) -> FakeTimer:
        """Shorten only the existing whole-workflow watchdog for regression tests."""
        # Force the existing watchdog promptly without needing a new helper API.
        return FakeTimer(self, 20 if delay == 20000 else delay, callback)

    def CallAfter(self, callback: Callable[..., None], *args: Any) -> None:
        """Queue work in a later event-loop turn."""
        self.queue(0, lambda: callback(*args))

    def Yield(self) -> None:
        """Dispatch a nested callback before returning to its caller."""
        self.dispatch()

    def GetApp(self) -> Optional[FakeApp]:
        """Return an existing app without constructing one."""
        return self.app

    def App(self, _redirect: bool) -> FakeApp:
        """Record the app whose destruction the tests must verify."""
        self.app = self.created_app = FakeApp(self)
        return self.app

    def GetTopLevelWindows(self) -> list[FakeWindow]:
        """Return a snapshot, allowing destruction while iterating."""
        return list(self.windows)

    def Frame(self, parent: Any, **kwargs: Any) -> FakeWindow:
        """Construct the workflow-owned outer window."""
        return FakeWindow(self, parent, **kwargs)

    def GUIEventLoop(self) -> FakeLoop:
        """Record each wait loop independently of modal and main loops."""
        loop = FakeLoop(self)
        self.wait_loops.append(loop)
        return loop


def test_deadline_in_modal_unwinds_only_the_modal_and_skips_after_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EndModal owns modal loop exit, and cancellation suppresses later phases."""
    wx = FakeWx(monkeypatch)
    dialogs: list[FakeDialog] = []
    phases: list[str] = []

    def exercise(frame: Any, _wx: Any) -> None:
        dialog = FakeDialog(wx, frame)
        dialogs.append(dialog)
        dialog.ShowModal()

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(exercise, after_events=lambda *_args: phases.append("after"))

    assert dialogs[0].end_calls == 1
    assert dialogs[0].loop.exit_calls == 1
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1
    assert wx.created_app.destroyed
    assert not wx.windows
    assert phases == []


@pytest.mark.parametrize("dispatch", ["pump", "wait_until", "modal"])
def test_deadline_unwinds_two_new_modal_loops_one_active_loop_at_a_time(
    monkeypatch: pytest.MonkeyPatch, dispatch: str
) -> None:
    """EndModal on an inactive outer loop must not strand it during cancellation."""
    wx = FakeWx(monkeypatch)
    dialogs: list[FakeDialog] = []
    phases: list[str] = []

    def exercise(frame: Any, _wx: Any) -> None:
        outer = FakeDialog(wx, frame)
        dialogs.append(outer)

        def show_second() -> None:
            second = FakeDialog(wx, dialogs[-1])
            dialogs.append(second)
            second.ShowModal()
            phases.append("second returned")

        def show_first() -> None:
            first = FakeDialog(wx, outer)
            dialogs.append(first)
            wx.CallAfter(show_second)
            first.ShowModal()
            phases.append("first returned")

        def interact() -> None:
            wx.CallAfter(show_first)
            if dispatch == "modal":
                return
            try:
                if dispatch == "pump":
                    support.pump(wx)
                else:
                    support.wait_until(wx, lambda: False, timeout_ms=100)
            except AssertionError as error:
                phases.append(str(error))
                outer.EndModal(wx.ID_CANCEL)

        wx.CallAfter(interact)
        outer.ShowModal()
        phases.append("outer returned")

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(exercise)

    assert phases == ["second returned", "first returned"] + (
        [] if dispatch == "modal" else ["Native workflow exceeded its deadline"]
    ) + ["outer returned"]
    assert [dialog.end_calls for dialog in dialogs] == [1, 1, 1]
    assert [dialog.loop.exit_calls for dialog in dialogs] == [1, 1, 1]
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1
    assert not wx.windows
    assert not wx.hooks


def test_deadline_during_a_yielding_wait_predicate_exits_its_loop_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed predicate must not repeat the watchdog's loop exit request."""
    wx = FakeWx(monkeypatch)
    calls = 0

    def predicate() -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            wx.Yield()
        return False

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(
            lambda *_args: support.wait_until(wx, predicate, timeout_ms=10)
        )
    assert len(wx.wait_loops) == 1
    assert wx.wait_loops[0].exit_calls == 1
    assert not wx.hooks


@pytest.mark.parametrize("cancelled", [False, True])
def test_existing_hook_can_intercept_a_child_without_corrupting_modal_ownership(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    """An older hook may skip a child after our Enter without calling our Exit."""
    wx = FakeWx(monkeypatch)
    dialogs: list[FakeDialog] = []

    class ExistingHook(wx.ModalDialogHook):
        def Enter(self, dialog: Any) -> int:
            return wx.ID_CANCEL if getattr(dialog, "blocked", False) else wx.ID_NONE

        def Exit(self, _dialog: Any) -> None:
            pass

    existing = ExistingHook()
    existing.Register()

    def exercise(frame: Any, _wx: Any) -> None:
        outer = FakeDialog(wx, frame)
        dialogs.append(outer)

        def interact() -> None:
            child = FakeDialog(wx, outer)
            child.blocked = True
            dialogs.append(child)
            child.ShowModal()
            assert not child.IsModal()
            if cancelled:
                with suppress(AssertionError):
                    support.wait_until(wx, lambda: False, timeout_ms=100)
            outer.EndModal(wx.ID_CANCEL)

        wx.CallAfter(interact)
        outer.ShowModal()

    try:
        if cancelled:
            with pytest.raises(AssertionError, match="exceeded its deadline"):
                support.run_native(exercise)
        else:
            support.run_native(exercise)
        assert [dialog.end_calls for dialog in dialogs] == [1, 0]
        assert wx.hooks == [existing]
        assert not wx.windows
    finally:
        existing.Unregister()


@pytest.mark.parametrize("method", ["Register", "Unregister"])
@pytest.mark.parametrize("reused_app", [False, True])
def test_hook_setup_and_teardown_errors_still_destroy_the_owned_app_and_frame(
    monkeypatch: pytest.MonkeyPatch, method: str, reused_app: bool
) -> None:
    """Hook failures must not bypass the native-window cleanup guarantee."""
    wx = FakeWx(monkeypatch)
    existing = wx.App(False) if reused_app else None
    previous = FakeWindow(wx) if reused_app else None
    if existing is not None:
        existing.SetTopWindow(previous)
    original = getattr(wx.ModalDialogHook, method)

    def fail(hook: Any) -> None:
        if method == "Unregister":
            original(hook)
        raise RuntimeError(f"hook {method} failed")

    monkeypatch.setattr(wx.ModalDialogHook, method, fail)
    with pytest.raises(RuntimeError, match=f"hook {method} failed"):
        support.run_native(lambda *_args: None)
    assert wx.created_app is not None
    if reused_app:
        assert wx.GetApp() is existing
        assert not wx.created_app.destroyed
        assert existing.GetTopWindow() is previous
        assert wx.windows == [previous]
    else:
        assert wx.created_app.destroyed
        assert not wx.windows
    # A callback queued before failed registration would reference the now
    # destroyed frame if the caller later dispatches its reused app's events.
    if method == "Register":
        assert not wx.events
    while wx.events:
        wx.dispatch()
    assert not wx.hooks
    assert not support._workflows


@pytest.mark.parametrize("dispatch", ["pump", "wait_until"])
def test_deadline_inside_interaction_stops_it_before_modal_handler_closes_once(
    monkeypatch: pytest.MonkeyPatch, dispatch: str
) -> None:
    """A nested dispatch returns cancellation to the interaction's own handler."""
    wx = FakeWx(monkeypatch)
    dialogs: list[FakeDialog] = []
    phases: list[str] = []

    def exercise(frame: Any, _wx: Any) -> None:
        dialog = FakeDialog(wx, frame)
        dialogs.append(dialog)

        def interact() -> None:
            try:
                if dispatch == "pump":
                    support.pump(wx)
                else:
                    support.wait_until(wx, lambda: False, timeout_ms=100)
                phases.append("continued after deadline")
            except AssertionError:
                phases.append("interaction cancelled")
                dialog.EndModal(wx.ID_CANCEL)

        wx.CallAfter(interact)
        dialog.ShowModal()

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(exercise)

    assert phases == ["interaction cancelled"]
    assert dialogs[0].end_calls == 1
    assert dialogs[0].loop.exit_calls == 1
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1
    assert wx.created_app.destroyed
    assert not wx.windows


def test_success_restores_existing_app_and_runs_after_events_in_a_later_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful cleanup retains the caller's app and existing top window."""
    wx = FakeWx(monkeypatch)
    existing = wx.App(False)
    previous = FakeWindow(wx)
    existing.SetTopWindow(previous)
    phases: list[str] = []

    def exercise(_frame: Any, _wx: Any) -> None:
        phases.append("exercise")
        wx.CallAfter(phases.append, "queued")

    support.run_native(exercise, after_events=lambda *_args: phases.append("after"))

    assert phases == ["exercise", "queued", "after"]
    assert wx.GetApp() is existing
    assert existing.GetTopWindow() is previous
    assert not existing.destroyed
    assert wx.windows == [previous]


@pytest.mark.parametrize("phase", ["exercise", "after_events"])
def test_callback_exception_is_reported_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Both callback phases preserve their original exception and clean up."""
    wx = FakeWx(monkeypatch)

    def fail(_frame: Any, _wx: Any) -> None:
        raise ValueError("native callback failed")

    with pytest.raises(ValueError, match="native callback failed"):
        support.run_native(
            fail if phase == "exercise" else lambda *_args: None,
            after_events=fail if phase == "after_events" else None,
        )
    assert wx.created_app is not None
    assert wx.created_app.destroyed
    assert not wx.windows


def test_deadline_without_modal_unwinds_the_wait_before_destroying_its_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not close a frame while a helper-owned nested wait is active."""
    wx = FakeWx(monkeypatch)
    frames: list[FakeWindow] = []

    def exercise(frame: Any, _wx: Any) -> None:
        frames.append(frame)
        support.wait_until(wx, lambda: False, timeout_ms=100)
        pytest.fail("interaction continued after cancellation")

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(exercise)

    assert len(wx.wait_loops) == 1
    assert wx.wait_loops[0].exit_calls == 1
    assert frames[0].close_calls == 0
    assert not frames[0]
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1


def test_deadline_unwinds_nested_predicate_waits_from_inside_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each nested loop gets one exit request as its Python caller unwinds."""
    wx = FakeWx(monkeypatch)
    calls = 0

    def predicate() -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            support.wait_until(wx, lambda: False, timeout_ms=100)
        return False

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(lambda *_args: support.wait_until(wx, predicate))

    assert len(wx.wait_loops) == 2
    assert [loop.exit_calls for loop in wx.wait_loops] == [1, 1]
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1
    assert not wx.windows


def test_deadline_during_after_events_cancels_that_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The later phase obeys the same cancellation boundary as exercise."""
    wx = FakeWx(monkeypatch)
    phases: list[str] = []

    def after_events(_frame: Any, _wx: Any) -> None:
        phases.append("entered")
        support.wait_until(wx, lambda: False, timeout_ms=100)
        phases.append("continued")

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(lambda *_args: None, after_events=after_events)
    assert phases == ["entered"]
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1
    assert not wx.windows


def test_wait_until_keeps_its_own_timeout_and_predicate_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone waits still report their reason and predicate errors."""
    wx = FakeWx(monkeypatch)
    with pytest.raises(AssertionError, match="column never resized"):
        support.wait_until(
            wx, lambda: False, timeout_ms=10, reason="column never resized"
        )

    calls = 0

    def predicate() -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("native read failed")
        return False

    with pytest.raises(ValueError, match="native read failed"):
        support.wait_until(wx, predicate)
    assert [loop.exit_calls for loop in wx.wait_loops] == [1, 1]
    assert not support._workflows


@pytest.mark.parametrize("dispatch", ["pump", "wait_until"])
def test_deadline_unwinds_a_modal_opened_by_a_callback_inside_dispatch(
    monkeypatch: pytest.MonkeyPatch, dispatch: str
) -> None:
    """A new inner ShowModal must return before its outer dispatcher can unwind."""
    wx = FakeWx(monkeypatch)
    dialogs: list[FakeDialog] = []
    phases: list[str] = []

    def exercise(frame: Any, _wx: Any) -> None:
        outer = FakeDialog(wx, frame)
        dialogs.append(outer)

        def show_nested_modal() -> None:
            inner = FakeDialog(wx, outer)
            dialogs.append(inner)
            inner.ShowModal()
            phases.append("nested modal returned")

        def interact() -> None:
            wx.CallAfter(show_nested_modal)
            try:
                if dispatch == "pump":
                    support.pump(wx)
                else:
                    support.wait_until(wx, lambda: False, timeout_ms=100)
                phases.append("continued after deadline")
            except AssertionError as error:
                phases.append(str(error))
                outer.EndModal(wx.ID_CANCEL)

        wx.CallAfter(interact)
        outer.ShowModal()

    with pytest.raises(AssertionError, match="exceeded its deadline"):
        support.run_native(exercise)

    assert phases == [
        "nested modal returned",
        "Native workflow exceeded its deadline",
    ]
    assert [dialog.end_calls for dialog in dialogs] == [1, 1]
    assert [dialog.loop.exit_calls for dialog in dialogs] == [1, 1]
    assert wx.created_app is not None
    assert wx.created_app.loop.exit_calls == 1
    assert not wx.windows
