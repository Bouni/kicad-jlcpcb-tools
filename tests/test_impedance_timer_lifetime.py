"""Timer shutdown must survive replacement wx wrappers and deferred deletion."""

from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest

from .test_impedance_dialog import (
    _snapshot,
    constructor_api,  # noqa: F401 -- prerequisite for draft_api
)
from .test_impedance_draft_dialog import draft_api  # noqa: F401


def _event(window_id: int, wrapper: object) -> SimpleNamespace:
    """Represent the stable native ID independently of its Python wrapper."""
    return SimpleNamespace(
        GetId=lambda: window_id, GetEventObject=lambda: wrapper, Skip=lambda: None
    )


def _reject_ui(*_arguments: object, **_keywords: object) -> None:
    """Fail if shutdown touches an invalid native control or relayouts a window."""
    raise AssertionError("shutdown accessed native controls")


@pytest.mark.parametrize("kind", ["specification", "main"])
@pytest.mark.parametrize("own_window", [False, True])
def test_destroy_uses_native_id_and_stops_owned_work(
    draft_api: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    own_window: bool,
) -> None:
    """Replacement wrappers still cancel work; child destruction leaves it alone."""
    api = draft_api
    snapshot = _snapshot(api)
    if kind == "specification":
        dialog = api.dialog.SpecificationDialog(None, snapshot)
        timers = (
            dialog._draft_calc_timer,
            dialog._draft_slow_timer,
            dialog._draft_retry_timer,
        )
        cancel = dialog._draft_calc_cancel = Event()
    else:
        dialog = api.dialog.ImpedanceDialog(
            None, api.model.Config(), snapshot, api.preview, lambda: snapshot
        )
        timers = (dialog._width_refresh_timer, dialog._width_slow_timer)
        cancel = dialog._calculation_cancel = Event()
    for timer in timers:
        timer.StartOnce(100)
    monkeypatch.setattr(dialog, "Layout", _reject_ui)
    monkeypatch.setattr(dialog.calculator_link, "IsShown", _reject_ui)
    native_id = dialog.GetId()
    monkeypatch.setattr(dialog, "GetId", _reject_ui)
    dialog.alive = not own_window
    event = _event(native_id if own_window else native_id + 1, object())
    dialog.bindings[api.wx.EVT_WINDOW_DESTROY, -1](event)
    assert cancel.is_set() is own_window
    assert all(timer.IsRunning() is not own_window for timer in timers)


def test_cancel_does_not_access_native_calculator_link(
    draft_api: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid controls cannot interrupt timer shutdown during native destruction."""
    dialog = draft_api.dialog.SpecificationDialog(None, _snapshot(draft_api))
    monkeypatch.setattr(dialog.calculator_link, "IsShown", _reject_ui)
    dialog._on_spec_cancel(_event(dialog.GetId(), dialog))


@pytest.mark.parametrize(
    "callback", ["_schedule_draft_width_calculation", "_on_draft_calc_timer"]
)
def test_queued_callback_after_cancel_cannot_restart_work(
    draft_api: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
) -> None:
    """Cancel closes scheduling immediately while the native dialog stays truthy."""
    api = draft_api
    dialog = api.dialog.SpecificationDialog(None, _snapshot(api))
    dialog._on_spec_cancel(_event(dialog.GetId(), dialog))
    dialog.stackup = SimpleNamespace(calculator_id="enabled")
    dialog._active_layer = "F.Cu"
    monkeypatch.setattr(dialog.include_layer, "GetValue", _reject_ui)
    assert dialog
    handler = getattr(dialog, callback)
    handler(
        *(() if callback.startswith("_schedule") else (_event(dialog.GetId(), dialog),))
    )
    assert not any(
        timer.IsRunning()
        for timer in (
            dialog._draft_calc_timer,
            dialog._draft_slow_timer,
            dialog._draft_retry_timer,
        )
    )
