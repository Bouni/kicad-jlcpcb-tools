"""Exercise catalog recovery in real wx dialogs, with a deterministic provider."""

from collections.abc import Callable, Iterator
from dataclasses import replace
import importlib
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any, Optional

import pytest

from .native_wx_support import pump, run_native, wait_until

pytestmark = pytest.mark.native_wx


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Keep all product controls, events and timers native; isolate shared traffic."""
    wx = pytest.importorskip("wx")
    package_name = "_impedance_catalog_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    transport = importlib.import_module(f"{package_name}.jlcpcb_stackups")
    controller = importlib.import_module(f"{package_name}.catalog_controller")
    dialog = importlib.import_module(f"{package_name}.dialog")
    picker = importlib.import_module(f"{package_name}.stackup_dialog")
    model = importlib.import_module(f"{package_name}.model")
    cache = importlib.import_module(f"{package_name}.catalog_cache")
    refresh = importlib.import_module(f"{package_name}.catalog_refresh")
    clock = SimpleNamespace(now=0.0)
    gate = refresh.CatalogRefreshGate(clock=lambda: clock.now)
    constructor = controller.CatalogController

    def isolated_controller(*args: Any, **options: Any) -> Any:
        """Inject only the clock/gate seam into the real controller constructor."""
        options["refresh_gate"] = gate
        return constructor(*args, **options)

    monkeypatch.setattr(dialog, "CatalogController", isolated_controller)
    try:
        yield SimpleNamespace(
            wx=wx,
            dialog=dialog,
            picker=picker,
            model=model,
            cache=cache,
            transport=transport,
            clock=clock,
            gate=gate,
        )
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]


def _stackup(native: SimpleNamespace, identifier: str, thickness: str = "1.6") -> Any:
    """Provide a compatible immutable saved/catalog construction."""
    return native.model.Stackup(
        identifier, f"Named construction {identifier}", 4, thickness, "1", "0.5"
    )


def _main(
    native: SimpleNamespace,
    frame: Any,
    cache: Any,
    fetch: Callable[..., tuple[Any, ...]],
    saves: list[tuple[int, Any]],
    *,
    saved: Any = None,
    refresh_snapshot: Optional[Callable[[], Any]] = None,
) -> Any:
    """Open the complete controlled-impedance dialog with an unrouted board."""
    snapshot = (
        refresh_snapshot()
        if refresh_snapshot is not None
        else native.model.BoardSnapshot(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), ())
    )
    return native.dialog.ImpedanceDialog(
        frame,
        native.model.Config(stackup=saved),
        snapshot,
        preview=lambda *_args: pytest.fail("An unrouted board needs no capture"),
        refresh_snapshot=refresh_snapshot or (lambda: snapshot),
        load_stackup_catalog=lambda _count: cache,
        save_stackup_catalog=lambda count, value: saves.append((count, value)),
        fetch_stackup_catalog=fetch,
    )


def _assert_neutral(wx: Any, control: Any) -> None:
    """Read the actual control colour used to communicate catalog severity."""
    assert control.GetForegroundColour() == wx.SystemSettings.GetColour(
        wx.SYS_COLOUR_WINDOWTEXT
    )


def _cancel_button(wx: Any, dialog: Any) -> None:
    """Dispatch Cancel through the native button's bound event handlers."""
    button = dialog.FindWindow(wx.ID_CANCEL)
    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
    event.SetEventObject(button)
    button.GetEventHandler().ProcessEvent(event)


def test_native_cached_outage_recovers_inside_picker_without_changing_selection(
    native: SimpleNamespace,
) -> None:
    """A quiet refresh keeps real filters, selection and saved-board state intact."""

    def exercise(frame: Any, wx: Any) -> None:
        saved = _stackup(native, "saved")
        chosen = _stackup(native, "chosen")
        refreshed_chosen = replace(
            chosen, retrieved_at_utc="2026-10-14T12:00:00.000000Z"
        )
        other = _stackup(native, "new", "1.2")
        cache = native.cache.CatalogCache(
            (saved, chosen), "2001-01-01T00:00:00.000000Z"
        )
        calls: list[int] = []
        saves: list[tuple[int, Any]] = []

        def fetch(count: int, *, cancel: Callable[[], bool]) -> tuple[Any, ...]:
            assert count == 4 and not cancel()
            calls.append(count)
            if len(calls) == 1:
                raise native.transport.CatalogUnavailable(
                    "Temporary network interruption"
                )
            return (saved, refreshed_chosen, other)

        main = _main(native, frame, cache, fetch, saves, saved=saved)
        main.Show()
        wait_until(
            wx,
            lambda: main._catalog_controller is not None
            and not main._catalog_controller.state.loading
            and len(calls) == 1,
        )
        controller = main._catalog_controller
        _assert_neutral(wx, main.catalog_status)
        assert "Catalog check failed" not in main.catalog_status.GetValue()
        assert controller.state.cache == cache and not saves
        picker = native.picker.StackupDialog(
            main, 4, saved, catalog_controller=controller
        )
        failures: list[BaseException] = []

        def interact() -> None:
            try:
                _assert_neutral(wx, picker.status)
                assert picker.accept_button.IsEnabled()
                picker.thickness.SetValue("1.6")
                pump(wx)
                assert picker._displayed_stackup is saved
                chosen_row = picker._shown.index(chosen)
                picker.choices.SetItemState(-1, 0, wx.LIST_STATE_SELECTED)
                picker.choices.SetItemState(
                    chosen_row, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED
                )
                wait_until(wx, lambda: picker._displayed_stackup is chosen)
                assert picker.choices.GetFirstSelected() == chosen_row
                timer = controller._retry_timer
                assert timer is not None and timer.IsRunning()
                native.clock.now += 300.0
                timer.Start(20)
                wait_until(
                    wx,
                    lambda: len(saves) == 1
                    or (len(calls) == 2 and not controller.state.loading),
                )
                assert len(saves) == 1, controller.state.message
                assert calls == [4, 4]
                assert controller.state.cache.checked_at_utc != cache.checked_at_utc
                assert picker.thickness.GetValue() == "1.6"
                assert picker.choices.GetItemCount() == 2
                assert (
                    picker._shown[picker.choices.GetFirstSelected()].stackup_id
                    == chosen.stackup_id
                )
                assert picker._displayed_stackup.stackup_id == chosen.stackup_id
                assert main.session.config.stackup is saved
                _assert_neutral(wx, main.catalog_status)
                _assert_neutral(wx, picker.status)
                _cancel_button(wx, picker)
            except BaseException as failure:
                failures.append(failure)
                picker.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = picker.ShowModal()
            assert not failures, failures
            assert result == wx.ID_CANCEL
            assert picker.stackup is None
            assert main.session.config.stackup is saved
        finally:
            picker.Destroy()
            main.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("has_saved", [True, False])
def test_native_outage_severity_depends_on_actual_usable_saved_stackup(
    native: SimpleNamespace, has_saved: bool
) -> None:
    """A saved construction is usable without cache; an unusable dialog is explicit."""

    def exercise(frame: Any, wx: Any) -> None:
        saved = _stackup(native, "saved") if has_saved else None
        calls: list[int] = []
        saves: list[tuple[int, Any]] = []

        def unavailable(count: int, *, cancel: Callable[[], bool]) -> tuple[Any, ...]:
            assert not cancel()
            calls.append(count)
            raise native.transport.CatalogUnavailable("Temporary network interruption")

        main = _main(
            native, frame, native.cache.CatalogCache(), unavailable, saves, saved=saved
        )
        main.Show()
        wait_until(
            wx,
            lambda: main._catalog_controller is not None
            and not main._catalog_controller.state.loading
            and bool(calls),
        )
        controller = main._catalog_controller
        picker = native.picker.StackupDialog(
            main, 4, saved, catalog_controller=controller
        )
        picker.Show()
        pump(wx)
        try:
            if has_saved:
                _assert_neutral(wx, main.catalog_status)
                _assert_neutral(wx, picker.status)
                assert picker.accept_button.IsEnabled()
                assert picker._displayed_stackup is saved
            else:
                assert main.catalog_status.GetForegroundColour() == wx.Colour(
                    195, 35, 35
                )
                assert picker.status.GetForegroundColour() == wx.Colour(195, 35, 35)
                assert "No compatible" in main.catalog_status.GetValue()
                assert not picker.accept_button.IsEnabled()
            assert "automatically" in main.catalog_status.GetValue()
            assert not saves and main.session.config.stackup is saved
        finally:
            picker.Destroy()
            main.Destroy()
            pump(wx)

    run_native(exercise)


def test_native_close_cancels_retry_and_reopening_respects_shared_cooldown(
    native: SimpleNamespace,
) -> None:
    """Real window destruction stops native timers and does not reset cooldown."""

    def exercise(frame: Any, wx: Any) -> None:
        saved = _stackup(native, "saved")
        cache = native.cache.CatalogCache((saved,), "")
        calls: list[int] = []
        saves: list[tuple[int, Any]] = []

        def unavailable(count: int, *, cancel: Callable[[], bool]) -> tuple[Any, ...]:
            assert not cancel()
            calls.append(count)
            raise native.transport.CatalogUnavailable("Temporary network interruption")

        main = _main(native, frame, cache, unavailable, saves, saved=saved)
        main.Show()
        wait_until(
            wx,
            lambda: main._catalog_controller is not None
            and main._catalog_controller._retry_timer is not None,
        )
        controller = main._catalog_controller
        timer = controller._retry_timer
        assert timer.IsRunning()
        main.Close()
        wait_until(wx, lambda: controller._closed)
        assert not timer.IsRunning()
        if main:
            main.Destroy()
            pump(wx)
        reopened = _main(native, frame, cache, unavailable, saves, saved=saved)
        reopened.Show()
        wait_until(
            wx,
            lambda: reopened._catalog_controller is not None
            and reopened._catalog_controller._retry_timer is not None,
        )
        try:
            assert calls == [4] and not saves
            assert reopened._catalog_controller.state.cache == cache
            _assert_neutral(wx, reopened.catalog_status)
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)


def test_native_parent_teardown_rejects_late_worker_result_and_persistence(
    native: SimpleNamespace,
) -> None:
    """A completed native request cannot persist into a destroyed dialog owner."""

    def exercise(frame: Any, wx: Any) -> None:
        started, release, returned = Event(), Event(), Event()
        saves: list[tuple[int, Any]] = []
        saved = _stackup(native, "saved")

        def delayed(_count: int, *, cancel: Callable[[], bool]) -> tuple[Any, ...]:
            started.set()
            assert release.wait(3)
            assert cancel()
            returned.set()
            return (saved,)

        main = _main(native, frame, native.cache.CatalogCache(), delayed, saves)
        main.Show()
        wait_until(wx, started.is_set)
        controller = main._catalog_controller
        frame.Destroy()
        wait_until(wx, lambda: not main)
        assert controller._closed
        release.set()
        wait_until(wx, returned.is_set)
        pump(wx)
        assert not saves

    run_native(exercise)


@pytest.mark.parametrize("old_state", ["active_worker", "pending_retry"])
def test_native_board_layer_count_refresh_replaces_catalog_owner_safely(
    native: SimpleNamespace, old_state: str
) -> None:
    """Native row refresh replaces only the catalog for the observed copper count."""

    def exercise(frame: Any, wx: Any) -> None:
        old_started, old_release, old_returned = Event(), Event(), Event()
        saves: list[tuple[int, Any]] = []
        calls: list[int] = []
        old_choice = _stackup(native, "old-four-layer")
        new_choice = replace(_stackup(native, "new-six-layer"), layer_count=6)

        def fetch(count: int, *, cancel: Callable[[], bool]) -> tuple[Any, ...]:
            calls.append(count)
            if count == 6:
                assert not cancel()
                return (new_choice,)
            assert count == 4
            old_started.set()
            if old_state == "pending_retry":
                raise native.transport.CatalogUnavailable(
                    "Temporary network interruption"
                )
            assert old_release.wait(3)
            assert cancel()
            old_returned.set()
            return (old_choice,)

        board_state = [
            native.model.BoardSnapshot(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), ())
        ]
        main = _main(
            native,
            frame,
            native.cache.CatalogCache(),
            fetch,
            saves,
            refresh_snapshot=lambda: board_state[0],
        )
        main.Show()
        wait_until(wx, old_started.is_set)
        old_controller = main._catalog_controller
        old_timer = None
        if old_state == "pending_retry":
            wait_until(wx, lambda: old_controller._retry_timer is not None)
            old_timer = old_controller._retry_timer
            assert old_timer.IsRunning()
        replacement = native.model.BoardSnapshot(
            ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"), ()
        )
        try:
            board_state[0] = replacement
            assert main._refresh_rows()
            current = main._catalog_controller
            assert current is not old_controller
            assert current.layer_count == 6
            assert old_controller._closed
            assert main.session.snapshot is replacement
            if old_timer is not None:
                assert not old_timer.IsRunning()
            else:
                reservation = native.gate.reserve(4)
                assert reservation.token is None, (
                    "An old request still owns its traffic slot"
                )
            wait_until(wx, lambda: len(saves) == 1)
            assert saves[0][0] == 6
            assert saves[0][1].stackups == (new_choice,)
            assert (
                "Catalog (6 copper layers): Catalog checked:"
                in main.catalog_status.GetValue()
            )
            _assert_neutral(wx, main.catalog_status)
            accepted_status = main.catalog_status.GetValue()
            accepted_cache = current.state.cache
            if old_state == "active_worker":
                old_release.set()
                wait_until(wx, old_returned.is_set)
            else:
                native.clock.now += 300.0
            pump(wx)
            assert calls == [4, 6]
            assert len(saves) == 1
            assert main._catalog_controller is current
            assert current.state.cache == accepted_cache
            assert main.catalog_status.GetValue() == accepted_status
            assert main.session.config.stackup is None
        finally:
            old_release.set()
            main.Destroy()
            pump(wx)

    run_native(exercise)
