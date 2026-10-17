"""Force real nested wx deadlines without leaking callbacks or modal loops."""

import os
import time
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


@pytest.mark.parametrize("dispatch", ["pump", "wait_until", "modal"])
def test_native_deadline_unwinds_modal_once_after_queued_resize(
    request: pytest.FixtureRequest, dispatch: str
) -> None:
    """A forced deadline reports safely instead of continuing modal interaction."""
    if isolated_native(request, "KICAD_NATIVE_WX_DEADLINE_CHILD"):
        return
    import wx

    previous = set(wx.GetTopLevelWindows())
    phases: list[str] = []
    endings: list[int] = []
    frames: list[Any] = []

    class Dialog(wx.Dialog):
        def EndModal(self, code: int) -> None:
            endings.append(code)
            super().EndModal(code)

    def exercise(frame: Any, _wx: Any) -> None:
        frames.append(frame)
        dialog = Dialog(frame, title="Native timeout regression", size=(300, 150))
        text = wx.TextCtrl(dialog, value="Queued resize")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(text, 1, wx.EXPAND)
        dialog.SetSizer(sizer)

        def resize() -> None:
            dialog.SetSize((420, 180))
            phases.append("resized")

        def interact() -> None:
            try:
                if dispatch == "wait_until":
                    wait_until(wx, lambda: False, timeout_ms=5000)
                else:
                    while True:
                        pump(wx)
                phases.append("continued")
            except AssertionError:
                phases.append("cancelled")
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(resize)
        if dispatch != "modal":
            wx.CallAfter(interact)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    started = time.monotonic()
    with pytest.raises(AssertionError, match="exceeded its deadline"):
        run_native(
            exercise,
            after_events=lambda *_args: phases.append("after events"),
            timeout_ms=300,
        )
    assert time.monotonic() - started < 5
    assert phases == (["resized"] if dispatch == "modal" else ["resized", "cancelled"])
    assert len(endings) == 1
    assert all(not frame for frame in frames)
    assert set(wx.GetTopLevelWindows()) == previous


def test_native_success_after_events_and_reused_app_restore_previous_top_window(
    request: pytest.FixtureRequest,
) -> None:
    """The same native app can run another successful workflow after teardown."""
    if isolated_native(request, "KICAD_NATIVE_WX_REUSED_APP_CHILD"):
        return
    import wx

    app = wx.GetApp()
    owned_app = app is None
    if owned_app:
        app = wx.App(False)
    previous_top = app.GetTopWindow()
    previous = wx.Frame(None, title="Existing app window")
    app.SetTopWindow(previous)
    previous.Show()
    phases: list[str] = []

    def exercise(frame: Any, _wx: Any) -> None:
        dialog = wx.Dialog(frame, title="Successful native workflow")
        wx.CallAfter(dialog.EndModal, wx.ID_OK)
        try:
            assert dialog.ShowModal() == wx.ID_OK
            phases.append("exercise")
            wx.CallAfter(phases.append, "queued")
        finally:
            dialog.Destroy()

    try:

        def expired(frame: Any, _wx: Any) -> None:
            dialog = wx.Dialog(frame, title="Expired workflow")
            try:
                dialog.ShowModal()
            finally:
                dialog.Destroy()

        with pytest.raises(AssertionError, match="exceeded its deadline"):
            run_native(expired, timeout_ms=300)
        assert wx.GetApp() is app
        assert app.GetTopWindow() is previous
        assert set(wx.GetTopLevelWindows()) == {previous}
        for _ in range(2):
            run_native(
                exercise, after_events=lambda *_args: phases.append("after events")
            )
            assert wx.GetApp() is app
            assert app.GetTopWindow() is previous
            assert set(wx.GetTopLevelWindows()) == {previous}
        assert phases == ["exercise", "queued", "after events"] * 2
    finally:
        previous.Destroy()
        wait_until(wx, lambda: not previous)
        if owned_app:
            app.Destroy()
        else:
            app.SetTopWindow(previous_top)


@pytest.mark.parametrize("dispatch", ["pump", "wait_until"])
def test_native_deadline_unwinds_modal_opened_inside_dispatch(
    request: pytest.FixtureRequest, dispatch: str
) -> None:
    """Close an inner native modal before cancelling its outer dispatch callback."""
    if isolated_native(request, "KICAD_NATIVE_WX_NESTED_MODAL_DEADLINE_CHILD"):
        return
    import wx

    previous = set(wx.GetTopLevelWindows())
    phases: list[str] = []
    endings: list[str] = []
    frames: list[Any] = []

    class Dialog(wx.Dialog):
        def EndModal(self, code: int) -> None:
            endings.append(self.GetTitle())
            super().EndModal(code)

    def exercise(frame: Any, _wx: Any) -> None:
        frames.append(frame)
        outer = Dialog(frame, title="Outer", size=(300, 150))

        def show_nested_modal() -> None:
            inner = Dialog(outer, title="Inner", size=(300, 150))

            def resize() -> None:
                inner.SetSize((420, 180))
                phases.append("resized")

            wx.CallAfter(resize)
            try:
                inner.ShowModal()
                phases.append("nested modal returned")
            finally:
                inner.Destroy()

        def interact() -> None:
            wx.CallAfter(show_nested_modal)
            try:
                if dispatch == "wait_until":
                    wait_until(wx, lambda: False, timeout_ms=5000)
                else:
                    while True:
                        pump(wx)
                phases.append("continued")
            except AssertionError as error:
                phases.append(str(error))
                outer.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            outer.ShowModal()
        finally:
            outer.Destroy()

    started = time.monotonic()
    with pytest.raises(AssertionError, match="exceeded its deadline"):
        run_native(
            exercise,
            after_events=lambda *_args: phases.append("after events"),
            timeout_ms=300,
        )
    assert time.monotonic() - started < 5
    assert phases == [
        "resized",
        "nested modal returned",
        "Native workflow exceeded its deadline",
    ]
    assert endings == ["Inner", "Outer"]
    assert all(not frame for frame in frames)
    assert set(wx.GetTopLevelWindows()) == previous


@pytest.mark.parametrize("dispatch", ["pump", "wait_until", "modal"])
def test_native_deadline_unwinds_two_new_modal_loops_in_native_order(
    request: pytest.FixtureRequest, dispatch: str
) -> None:
    """Each inner modal returns before EndModal is used on the next outer loop."""
    if isolated_native(request, "KICAD_NATIVE_WX_TWO_MODAL_DEADLINE_CHILD"):
        return
    import wx

    previous = set(wx.GetTopLevelWindows())
    phases: list[str] = []
    endings: list[str] = []

    class Dialog(wx.Dialog):
        def EndModal(self, code: int) -> None:
            endings.append(self.GetTitle())
            super().EndModal(code)

    def exercise(frame: Any, _wx: Any) -> None:
        outer = Dialog(frame, title="Outer", size=(300, 150))

        def show_first() -> None:
            first = Dialog(outer, title="First", size=(300, 150))

            def show_second() -> None:
                second = Dialog(first, title="Second", size=(300, 150))
                wx.CallAfter(phases.append, "second modal active")
                try:
                    second.ShowModal()
                    phases.append("second returned")
                finally:
                    second.Destroy()

            wx.CallAfter(show_second)
            try:
                first.ShowModal()
                phases.append("first returned")
            finally:
                first.Destroy()

        def interact() -> None:
            wx.CallAfter(show_first)
            if dispatch == "modal":
                return
            try:
                if dispatch == "wait_until":
                    wait_until(wx, lambda: False, timeout_ms=5000)
                else:
                    while True:
                        pump(wx)
            except AssertionError as error:
                phases.append(str(error))
                outer.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            outer.ShowModal()
            phases.append("outer returned")
        finally:
            outer.Destroy()

    started = time.monotonic()
    with pytest.raises(AssertionError, match="exceeded its deadline"):
        run_native(
            exercise,
            after_events=lambda *_args: phases.append("after events"),
            timeout_ms=300,
        )
    assert time.monotonic() - started < 5
    assert phases == ["second modal active", "second returned", "first returned"] + (
        [] if dispatch == "modal" else ["Native workflow exceeded its deadline"]
    ) + ["outer returned"]
    assert endings == ["Second", "First", "Outer"]
    assert set(wx.GetTopLevelWindows()) == previous


@pytest.mark.parametrize("cancelled", [False, True])
def test_native_existing_hook_can_skip_a_child_before_outer_modal_unwinds(
    request: pytest.FixtureRequest, cancelled: bool
) -> None:
    """A preexisting hook can intercept after our Enter without issuing Exit."""
    if isolated_native(request, "KICAD_NATIVE_WX_EXISTING_HOOK_CHILD"):
        return
    import wx

    previous = set(wx.GetTopLevelWindows())
    phases: list[str] = []
    endings: list[int] = []

    class ExistingHook(wx.ModalDialogHook):
        def Enter(self, dialog: Any) -> int:
            return wx.ID_CANCEL if dialog.GetTitle() == "Blocked" else wx.ID_NONE

        def Exit(self, _dialog: Any) -> None:
            pass

    class Dialog(wx.Dialog):
        def EndModal(self, code: int) -> None:
            endings.append(code)
            super().EndModal(code)

    existing_hook = ExistingHook()
    existing_hook.Register()

    def exercise(frame: Any, _wx: Any) -> None:
        outer = Dialog(frame, title="Outer", size=(300, 150))

        def interact() -> None:
            child = wx.Dialog(outer, title="Blocked")
            try:
                assert child.ShowModal() == wx.ID_CANCEL
                assert not child.IsModal()
                phases.append("blocked skipped")
            finally:
                child.Destroy()
            try:
                if cancelled:
                    wait_until(wx, lambda: False, timeout_ms=5000)
                phases.append("finished")
            except AssertionError:
                phases.append("cancelled")
            finally:
                outer.EndModal(wx.ID_CANCEL)

        wx.CallAfter(interact)
        try:
            outer.ShowModal()
        finally:
            outer.Destroy()

    try:
        if cancelled:
            with pytest.raises(AssertionError, match="exceeded its deadline"):
                run_native(exercise, timeout_ms=300)
        else:
            run_native(exercise)
        assert phases == ["blocked skipped", "cancelled" if cancelled else "finished"]
        assert endings == [wx.ID_CANCEL]
        assert set(wx.GetTopLevelWindows()) == previous
    finally:
        existing_hook.Unregister()
