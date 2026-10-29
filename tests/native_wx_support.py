"""Run real wx workflows with bounded completion and deterministic teardown."""

from collections.abc import Callable
from dataclasses import dataclass, field
import os
import re
import subprocess
import sys
import time
from typing import Any, Optional

import pytest


@dataclass
class _Dispatcher:
    """Remember modal ownership before entering a native dispatcher."""

    loop: Any
    modals: set[Any]
    exit_requested: bool = False

    def stop(self) -> None:
        if self.loop is not None and not self.exit_requested:
            self.exit_requested = True
            self.loop.ScheduleExit(0)


@dataclass
class _Workflow:
    """Share cancellation with helpers running inside nested native callbacks."""

    failure: Optional[AssertionError] = None
    dispatchers: list[_Dispatcher] = field(default_factory=list)
    modals: list[Any] = field(default_factory=list)
    ending_modal: Any = None

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    def open_modals(self) -> list[Any]:
        # An older registered hook can preempt ShowModal after our Enter. In
        # that case wx calls no Exit hooks and the dialog never becomes modal.
        return [dialog for dialog in self.modals if dialog and dialog.IsModal()]


_workflows: list[_Workflow] = []


def create_native_app(wx: Any) -> Any:
    """Create a nonredirecting test app that cannot restore stale capture streams."""

    class App(wx.App):
        def RestoreStdio(self) -> None:
            """No streams were redirected, including when this wrapper is finalized."""

    return App(False)


def isolated_native(
    request: pytest.FixtureRequest, marker: str, *, native: str = "wx"
) -> bool:
    """Run one native case in a fresh process, rejecting missing/skipped coverage."""
    if os.environ.get(marker) == "1":
        return False
    env = dict(os.environ, **{marker: "1", "KICAD_JLCPCB_REQUIRE_NATIVE": native})
    if native == "kicad" and "KICAD_JLCPCB_KICAD_MAJOR" not in env:
        # Only a required lane names its KiCad major. Elsewhere the child
        # requires the KiCad installed here, and without one the test skips.
        pcbnew = pytest.importorskip("pcbnew", reason="KiCad pcbnew is unavailable")
        found = re.match(r"\s*(\d+)\.", str(pcbnew.GetBuildVersion()))
        if found:
            env["KICAD_JLCPCB_KICAD_MAJOR"] = found.group(1)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", request.node.nodeid],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


def pump(wx: Any) -> None:
    """Dispatch queued layout and paint work from inside the native event loop."""
    workflow = _workflows[-1] if _workflows else None
    if workflow is not None:
        workflow.check()
        # Yield has no independent loop to exit. Its caller must unwind after
        # dispatch, before its own modal exception handler closes the dialog.
        workflow.dispatchers.append(_Dispatcher(None, set(workflow.open_modals())))
    try:
        for _ in range(3):
            wx.Yield()
            if workflow is not None:
                workflow.check()
    finally:
        if workflow is not None:
            workflow.dispatchers.pop()


def wait_until(
    wx: Any,
    predicate: Callable[[], bool],
    timeout_ms: int = 3000,
    *,
    reason: str = "Native state did not reach its expected value before the deadline",
) -> None:
    """Wait for observable native state, propagating predicate errors to pytest."""
    workflow = _workflows[-1] if _workflows else None
    if workflow is not None:
        workflow.check()
    if predicate():
        return
    loop = wx.GUIEventLoop()
    deadline = time.monotonic() + timeout_ms / 1000
    failures: list[BaseException] = []
    dispatcher = _Dispatcher(loop, set(workflow.open_modals()) if workflow else set())

    def check() -> None:
        try:
            if workflow is not None:
                workflow.check()
            done = predicate() or time.monotonic() >= deadline
        except BaseException as error:
            failures.append(error)
            done = True
        if done:
            dispatcher.stop()
        else:
            timer.Start(5)

    timer = wx.CallLater(5, check)
    if workflow is not None:
        workflow.dispatchers.append(dispatcher)
    try:
        loop.Run()
    finally:
        timer.Stop()
        if workflow is not None:
            workflow.dispatchers.pop()
    if workflow is not None:
        workflow.check()
    if failures:
        raise failures[0]
    assert predicate(), reason


def run_native(
    exercise: Callable[[Any, Any], None],
    *,
    after_events: Optional[Callable[[Any, Any], None]] = None,
    size: tuple[int, int] = (1200, 650),
    timeout_ms: int = 20000,
) -> None:
    """Run a workflow after entering MainLoop, then restore existing app/windows.

    after_events runs in a later native event-loop turn. It must use wait_until
    for asynchronously delivered input instead of assuming that delivery is done.
    """
    import wx

    app = wx.GetApp()
    owned_app = app is None
    if owned_app:
        app = create_native_app(wx)
    previous = set(wx.GetTopLevelWindows())
    previous_top = app.GetTopWindow()
    frame = None
    failures: list[BaseException] = []
    completed = False
    main_exit_requested = False
    cleaning = False
    workflow = _Workflow()

    def exit_main_loop() -> None:
        nonlocal main_exit_requested
        if not main_exit_requested:
            main_exit_requested = True
            app.ExitMainLoop()

    def attempt(action: Callable[[], Any], *, reject_false: bool = False) -> bool:
        try:
            result = action()
            if reject_false and result is False:
                raise AssertionError("Native window refused destruction")
            return True
        except BaseException as error:
            failures.append(error)
            return False

    def finish(callback: Optional[Callable[[Any, Any], None]] = None) -> None:
        nonlocal completed
        try:
            workflow.check()
            if callback is not None:
                callback(frame, wx)
        except BaseException as error:
            failures.append(error)
        finally:
            completed = True
            exit_main_loop()

    def run() -> None:
        try:
            workflow.check()
            exercise(frame, wx)
        except BaseException as error:
            failures.append(error)
        if after_events is not None and not failures:
            wx.CallAfter(finish, after_events)
        else:
            finish()

    def unwind_timeout() -> None:
        if workflow.failure is None or workflow.ending_modal is not None:
            return
        dispatcher = workflow.dispatchers[-1] if workflow.dispatchers else None
        modals = workflow.open_modals()
        if modals:
            modal = modals[-1]
            if dispatcher is None or modal not in dispatcher.modals:
                # End only the innermost modal. EndModal cannot exit an outer
                # wxGTK loop while a newer modal is active. ModalHook.Exit
                # resumes cancellation after this native ShowModal unwinds.
                if modal and modal.IsModal():
                    workflow.ending_modal = modal
                    modal.EndModal(wx.ID_CANCEL)
                return
        if dispatcher is not None:
            # Exit only a wait loop that we own. After wait_until/pump returns
            # from native dispatch it raises, allowing the callback's modal
            # exception handler to call EndModal exactly once.
            dispatcher.stop()
        else:
            exit_main_loop()

    class ModalHook(wx.ModalDialogHook):
        def Enter(self, dialog: Any) -> int:
            if cleaning or workflow.failure is not None:
                return wx.ID_CANCEL
            workflow.modals.append(dialog)
            return wx.ID_NONE

        def Exit(self, dialog: Any) -> None:
            workflow.modals = [
                modal for modal in workflow.modals if modal is not dialog
            ]
            if workflow.ending_modal is dialog:
                workflow.ending_modal = None
            # Unlike a queued callback, this hook runs after the modal's native
            # event loop has returned and restored the outer active loop.
            unwind_timeout()

    def timeout() -> None:
        if completed or workflow.failure is not None:
            return
        workflow.failure = AssertionError("Native workflow exceeded its deadline")
        failures.append(workflow.failure)
        unwind_timeout()

    watchdog = None
    modal_hook = None
    registered = False
    entered = False
    try:
        modal_hook = ModalHook()
        modal_hook.Register()
        registered = True
        frame = wx.Frame(None, title="Native plugin workflow", size=size)
        app.SetTopWindow(frame)
        frame.Show()
        _workflows.append(workflow)
        entered = True
        watchdog = wx.CallLater(timeout_ms, timeout)
        wx.CallAfter(run)
        app.MainLoop()
    except BaseException as error:
        failures.append(error)
    finally:
        # Destroy handlers and their queued callbacks may enter ShowModal too.
        # Keep the hook registered until these native events have been drained.
        cleaning = True
        if watchdog is not None:
            attempt(watchdog.Stop)
        if entered:
            assert _workflows.pop() is workflow
        requested: set[Any] = set()
        dispatched = False

        def mark_dispatched() -> None:
            nonlocal dispatched
            dispatched = True

        def cleanup_complete() -> bool:
            nonlocal dispatched
            created = set(wx.GetTopLevelWindows()) - previous
            for window in created - requested:
                requested.add(window)
                if not window:
                    continue
                if isinstance(window, wx.Dialog) and window.IsModal():
                    attempt(lambda window=window: window.EndModal(wx.ID_CANCEL))
                if window and not attempt(window.Destroy, reject_false=True) and window:
                    # Preserve deferred deletion if a Python override failed.
                    attempt(lambda window=window: app.ScheduleForDestruction(window))
            # Drain one quiet native event turn, including when setup failed
            # before allocating a window but already queued a CallAfter.
            if created:
                dispatched = False
            elif not dispatched:
                wx.CallAfter(mark_dispatched)
            return dispatched and not created

        attempt(lambda: wait_until(wx, cleanup_complete))
        if owned_app:
            attempt(app.Destroy)
        else:
            attempt(lambda: app.SetTopWindow(previous_top))
        if registered:
            attempt(modal_hook.Unregister)
    if failures:
        raise failures[0]
    assert completed, "Native main loop exited before the workflow completed"
