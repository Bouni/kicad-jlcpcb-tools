"""Run real wx workflows with bounded completion and deterministic teardown."""

from collections.abc import Callable
import os
import subprocess
import sys
import time
from typing import Any, Optional

import pytest


def isolated_native(
    request: pytest.FixtureRequest, marker: str, *, native: str = "wx"
) -> bool:
    """Run one native case in a fresh process, rejecting missing/skipped coverage."""
    if os.environ.get(marker) == "1":
        return False
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", request.node.nodeid],
        env=dict(os.environ, **{marker: "1", "KICAD_JLCPCB_REQUIRE_NATIVE": native}),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


def pump(wx: Any) -> None:
    """Dispatch queued layout and paint work from inside the native event loop."""
    for _ in range(3):
        wx.Yield()


def wait_until(
    wx: Any,
    predicate: Callable[[], bool],
    timeout_ms: int = 3000,
    *,
    reason: str = "Native state did not reach its expected value before the deadline",
) -> None:
    """Wait for observable native state, propagating predicate errors to pytest."""
    if predicate():
        return
    loop = wx.GUIEventLoop()
    deadline = time.monotonic() + timeout_ms / 1000
    failures: list[BaseException] = []

    def check() -> None:
        try:
            done = predicate() or time.monotonic() >= deadline
        except BaseException as error:
            failures.append(error)
            done = True
        if done:
            loop.ScheduleExit(0)
        else:
            timer.Start(5)

    timer = wx.CallLater(5, check)
    try:
        loop.Run()
    finally:
        timer.Stop()
    if failures:
        raise failures[0]
    assert predicate(), reason


def run_native(
    exercise: Callable[[Any, Any], None],
    *,
    after_events: Optional[Callable[[Any, Any], None]] = None,
    size: tuple[int, int] = (1200, 650),
) -> None:
    """Run a workflow after entering MainLoop, then restore existing app/windows.

    after_events runs in a later native event-loop turn. It must use wait_until
    for asynchronously delivered input instead of assuming that delivery is done.
    """
    import wx

    app = wx.GetApp()
    owned_app = app is None
    if owned_app:
        app = wx.App(False)
    previous = set(wx.GetTopLevelWindows())
    previous_top = app.GetTopWindow()
    frame = wx.Frame(None, title="Native plugin workflow", size=size)
    app.SetTopWindow(frame)
    failures: list[BaseException] = []
    completed = False

    def finish(callback: Optional[Callable[[Any, Any], None]] = None) -> None:
        nonlocal completed
        try:
            if callback is not None:
                callback(frame, wx)
        except BaseException as error:
            failures.append(error)
        finally:
            completed = True
            app.ExitMainLoop()

    def run() -> None:
        try:
            exercise(frame, wx)
        except BaseException as error:
            failures.append(error)
        if after_events is not None and not failures:
            wx.CallAfter(finish, after_events)
        else:
            finish()

    def timeout() -> None:
        failures.append(AssertionError("Native workflow exceeded its deadline"))
        for window in set(wx.GetTopLevelWindows()) - previous:
            if window:
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
                else:
                    window.Close()
        active = wx.EventLoopBase.GetActive()
        if active is not None:
            active.ScheduleExit(0)
        app.ExitMainLoop()

    frame.Show()
    watchdog = wx.CallLater(20000, timeout)
    wx.CallAfter(run)
    try:
        app.MainLoop()
    finally:
        watchdog.Stop()
        created = set(wx.GetTopLevelWindows()) - previous
        try:
            for window in created:
                if window:
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                    window.Destroy()
            # Destroy queues native deletion. Finish it while the app and the
            # test's imported renderer/window classes still exist.
            wait_until(wx, lambda: all(not window for window in created))
        finally:
            if owned_app:
                app.Destroy()
            else:
                app.SetTopWindow(previous_top)
    if failures:
        raise failures[0]
    assert completed, "Native main loop exited before the workflow completed"
