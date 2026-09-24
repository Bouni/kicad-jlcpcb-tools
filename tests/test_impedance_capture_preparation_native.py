"""Verify deferred capture preparation with real native wx controls and paint.

PNG data is deterministic fixture data. These checks exercise wx dispatch,
modal visibility and review state, not KiCad plotting performance.
"""

import importlib
import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_capture_review_restore import _changed_capture
from .test_impedance_settings_native import _PNG, _api, _click, _modal, _select

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


def _fixture(api: SimpleNamespace) -> tuple[Any, Any, Any]:
    """Supply two independently reviewable images on each signal layer."""
    snapshot = api.model.BoardSnapshot(
        layers=("F.Cu", "In1.Cu", "B.Cu"),
        traces=tuple(
            api.model.Trace(
                f"{layer}-{net}",
                layer,
                net,
                150_000,
                ((0, offset), (2_000_000, offset)),
            )
            for layer in ("F.Cu", "B.Cu")
            for net, offset in (("A", 0), ("B", 1_000_000))
        ),
        net_classes=("RF",),
        net_class_memberships=(("A", ("RF",)), ("B", ("RF",))),
        net_class_context_digest="native-capture-preparation-fixture",
    )
    spec = api.model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        tuple(
            api.model.LayerSettings(layer, ("In1.Cu",)) for layer in ("F.Cu", "B.Cu")
        ),
    )
    service = importlib.import_module(f"{api.dialog.__package__}.service")
    return snapshot, spec, service.CapturedImage.from_bytes(_PNG)


def _select_image(wx: Any, checklist: Any, index: int) -> None:
    """Dispatch native list selection with the real single-selection state."""
    previous = checklist.GetFirstSelected()
    if previous >= 0 and previous != index:
        checklist.Select(previous, False)
    checklist.Select(index)
    checklist.Focus(index)
    event = wx.ListEvent(wx.EVT_LIST_ITEM_SELECTED.typeId, checklist.GetId())
    event.SetIndex(index)
    event.SetEventObject(checklist)
    checklist.GetEventHandler().ProcessEvent(event)
    assert checklist.GetFirstSelected() == index


def _approved_history(
    api: SimpleNamespace, frame: Any, wx: Any, snapshot: Any, spec: Any, capture: Any
) -> Any:
    """Create historical evidence through visible images and explicit native approval."""
    dialog = api.dialog.SpecificationDialog(
        frame, snapshot, specification=spec, preview=lambda *_args, **_kwargs: capture
    )

    def approve() -> None:
        for layer in ("F.Cu", "B.Cu"):
            _select(wx, dialog.layer, dialog._layer_names.index(layer))
            for index in range(len(dialog.preview_pane.sections)):
                if dialog._active_layer != layer:
                    _select(wx, dialog.layer, dialog._layer_names.index(layer))
                _select_image(wx, dialog.preview_pane.checklist, index)
                wait_until(wx, lambda: dialog.preview_pane.displayed)
                _click(wx, dialog.approve_layer)
            assert dialog._layer_drafts[layer].confirmed
        _click(wx, dialog.FindWindow(wx.ID_OK))

    try:
        assert _modal(wx, dialog, approve) == wx.ID_OK
        assert dialog.specification == spec
        assert len(dialog.review_tracking.images) == 4
        assert len(dialog.review_tracking.layers) == 2
        return dialog.review_tracking
    finally:
        dialog.Destroy()
        pump(wx)


def test_native_modal_prepares_after_first_display_and_reuses_navigation(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Cold and repeated opens show first, then yield between unviewed captures."""
    if isolated_native(request, "KICAD_IMPEDANCE_CAPTURE_PREPARE_CHILD"):
        return
    api = _api(monkeypatch)
    timings: list[dict[str, float]] = []

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)

        def open_once() -> None:
            calls: list[Any] = []
            dispatched: list[str] = []
            shown: list[float] = []
            started = time.perf_counter()

            def render(section: Any, **_kwargs: Any) -> Any:
                assert wx.IsMainThread()
                assert dialog.IsShownOnScreen()
                assert shown, "Native EVT_SHOW must precede capture generation"
                if calls:
                    assert dialog.preview_pane.canvas._recorded, (
                        "The selected image must visibly paint before preparation"
                    )
                    assert calls[-1].section_id in dispatched, (
                        "The native event loop must run between captures"
                    )
                calls.append(section)
                wx.CallAfter(dispatched.append, section.section_id)
                return capture

            dialog = api.dialog.SpecificationDialog(
                frame, snapshot, specification=spec, preview=render
            )
            constructed = time.perf_counter()
            assert calls == [], "Hidden constructor must not generate captures"
            pump(wx)
            assert calls == [], "Hidden queued callbacks must not generate captures"

            def on_show(event: Any) -> None:
                if event.GetEventObject() is dialog and event.IsShown():
                    shown.append(time.perf_counter())
                event.Skip()

            dialog.Bind(wx.EVT_SHOW, on_show)

            def inspect() -> None:
                wait_until(wx, lambda: dialog.preview_pane.ready)
                selected = time.perf_counter()
                first = dialog.preview_pane.sections[0]
                assert calls[0] == first
                wait_until(wx, lambda: len(calls) == 4)
                prepared = time.perf_counter()
                assert len(set(calls)) == 4
                assert len(dialog.review_tracking.images) == 1
                assert dialog.review_tracking.layers == ()
                assert not any(
                    draft.confirmed for draft in dialog._layer_drafts.values()
                )
                before_navigation = list(calls)
                _select_image(wx, dialog.preview_pane.checklist, 1)
                wait_until(wx, lambda: dialog.preview_pane.ready)
                assert calls == before_navigation
                assert len(dialog.review_tracking.images) == 2
                _select(wx, dialog.layer, dialog._layer_names.index("B.Cu"))
                wait_until(wx, lambda: dialog.preview_pane.ready)
                assert calls == before_navigation
                timings.append(
                    {
                        "constructor_ms": (constructed - started) * 1000,
                        "show_ms": (shown[0] - started) * 1000,
                        "selected_ready_ms": (selected - started) * 1000,
                        "all_prepared_ms": (prepared - started) * 1000,
                    }
                )
                _click(wx, dialog.FindWindow(wx.ID_CANCEL))

            try:
                assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
                assert not dialog._capture_prepare_timer.IsRunning()
            finally:
                dialog.Destroy()
                pump(wx)

        open_once()
        open_once()

    run_native(exercise)
    logging.getLogger(__name__).info(
        "Deterministic PNG fixture cold/repeat timing: %s", json.dumps(timings)
    )


def test_native_cancel_stops_unfinished_capture_preparation(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Cancel from the first visible image prevents later native provider requests."""
    if isolated_native(request, "KICAD_IMPEDANCE_CAPTURE_CANCEL_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        calls: list[Any] = []

        def render(section: Any, **_kwargs: Any) -> Any:
            calls.append(section)
            return capture

        dialog = api.dialog.SpecificationDialog(
            frame, snapshot, specification=spec, preview=render
        )

        def cancel_when_displayed() -> None:
            wait_until(wx, lambda: dialog.preview_pane.ready)
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, cancel_when_displayed) == wx.ID_CANCEL
            count_at_close = len(calls)
            assert count_at_close < 4, "Fixture must cancel with preparation pending"
            assert not dialog._capture_prepare_timer.IsRunning()
            completed: list[bool] = []
            timer = wx.CallLater(150, completed.append, True)
            try:
                wait_until(wx, lambda: bool(completed))
            finally:
                timer.Stop()
            assert len(calls) == count_at_close
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("changed", ["none", "secondary", "other_layer"])
def test_native_historical_approval_waits_for_every_current_capture(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, changed: str
) -> None:
    """Hidden reopening is cold; changed unselected pixels keep their layer pending."""
    if isolated_native(request, "KICAD_IMPEDANCE_CAPTURE_HISTORY_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        history = _approved_history(api, frame, wx, snapshot, spec, capture)
        altered = _changed_capture(SimpleNamespace(capture=capture))
        calls: list[Any] = []

        def render(section: Any, **_kwargs: Any) -> Any:
            if calls:
                assert dialog.preview_pane.canvas._recorded, (
                    "Restored history must not replace a real first visible paint"
                )
            calls.append(section)
            different = (changed == "secondary" and section.net_names == ("B",)) or (
                changed == "other_layer" and section.layer == "B.Cu"
            )
            return altered if different else capture

        dialog = api.dialog.SpecificationDialog(
            frame,
            snapshot,
            specification=spec,
            preview=render,
            review_tracking=history,
            appearance_context=lambda: "deterministic-current-theme",
            verify_snapshot=lambda expected: (
                None if expected == snapshot else pytest.fail("Unexpected snapshot")
            ),
        )
        assert calls == []
        assert not any(draft.confirmed for draft in dialog._layer_drafts.values())
        pump(wx)
        assert calls == []
        assert dialog.review_tracking == history

        def inspect() -> None:
            wait_until(wx, lambda: dialog.preview_pane.ready)
            wait_until(wx, lambda: len(calls) == 4)
            wait_until(wx, lambda: not dialog._capture_prepare_timer.IsRunning())
            assert dialog.review_tracking.layers == history.layers
            selected_id = dialog.preview_pane.sections[0].section_id
            unselected_before = tuple(
                record for record in history.images if record.section_id != selected_id
            )
            unselected_after = tuple(
                record
                for record in dialog.review_tracking.images
                if record.section_id != selected_id
            )
            assert unselected_after == unselected_before, (
                "Background preparation must preserve historical view timestamps"
            )
            assert dialog._layer_drafts["F.Cu"].confirmed == (changed != "secondary")
            assert dialog._layer_drafts["B.Cu"].confirmed == (changed == "none")
            if changed != "none":
                affected = "B.Cu" if changed == "other_layer" else "F.Cu"
                assert "needs review" in dialog.layer.GetString(
                    dialog._layer_names.index(affected)
                )
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
            assert history.layers == dialog.review_tracking.layers
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)
