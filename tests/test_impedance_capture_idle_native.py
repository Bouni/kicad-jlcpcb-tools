"""Keep native foreground work responsive while a large workbook warms images.

PNG generation and calculator transport are deterministic seams. Constructors,
workers, native input queues, timers, modal ownership and paints remain real.
The bounded provider workload tests dispatch order, not rendering performance.
"""

from dataclasses import replace
import hashlib
import importlib
import os
from threading import Event
import time
from types import SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_settings_native import _PNG, _api, _click, _modal
from .test_impedance_spec_width_hint import _stackup

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


def _fixture(api: SimpleNamespace) -> tuple[Any, Any, Any]:
    """Give the real grid 900 separate 32-segment routes in three net classes."""
    classes = ("RF", "CLOCK", "DATA")
    names = tuple(f"SIGNAL_{index:03d}" for index in range(900))
    snapshot = api.model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            api.model.Trace(
                f"route-{index}",
                "F.Cu",
                name,
                150_000,
                tuple((segment * 100_000, index * 2_000_000) for segment in range(33)),
            )
            for index, name in enumerate(names)
        ),
        net_classes=classes,
        net_class_memberships=tuple(
            (name, (classes[index % len(classes)],)) for index, name in enumerate(names)
        ),
        net_class_context_digest="native-idle-fairness-900-routes",
    )
    config = api.model.Config(
        specifications=tuple(
            api.model.Specification(
                name.lower(),
                name,
                "50",
                "single_ended",
                name,
                (api.model.LayerSettings("F.Cu", ("B.Cu",)),),
            )
            for name in classes
        )
    )
    service = importlib.import_module(f"{api.dialog.__package__}.service")
    return snapshot, config, service.CapturedImage.from_bytes(_PNG)


def _capture_work() -> None:
    """Keep a capture busy long enough for native timers to become runnable."""
    deadline = time.perf_counter() + 0.008
    payload = b"deterministic capture work" * 256
    while time.perf_counter() < deadline:
        hashlib.sha256(payload).digest()


def _post_row_click(wx: Any, grid: Any, row: int) -> None:
    """Queue real grid-window mouse input without directly invoking its handler."""
    grid.MakeCellVisible(row, 1)
    rectangle = grid.CellToRect(row, 1)
    x, y = grid.CalcScrolledPosition(rectangle.x, rectangle.y)
    window = grid.GetGridWindow()
    point = wx.Point(x + rectangle.width // 2, y + rectangle.height // 2)
    assert window.GetClientRect().Contains(point)
    for kind in (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP):
        event = wx.MouseEvent(kind)
        event.SetEventObject(window)
        event.SetPosition(point)
        event.SetLeftDown(kind == wx.wxEVT_LEFT_DOWN)
        wx.PostEvent(window, event)


def test_native_warming_yields_to_worker_input_timer_and_selected_paint(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Completion and real row navigation paint while hundreds of captures remain."""
    if isolated_native(request, "KICAD_IMPEDANCE_CAPTURE_IDLE_FAIRNESS_CHILD"):
        return
    api = _api(monkeypatch)
    snapshot, config, capture = _fixture(api)
    stackup_model = importlib.import_module(f"{api.dialog.__package__}.stackup_model")
    config = replace(config, stackup=_stackup(api))
    release_worker = Event()
    worker_results: list[str] = []

    def calculate(stackup: Any, spec: Any, layer: str, *, cancel: Any) -> Any:
        assert release_worker.wait(10), "The first background image never ran"
        result = stackup_model.WidthResult(
            spec.spec_id,
            layer,
            stackup_model.calculation_fingerprint(stackup, spec, layer),
            "success",
            150_000,
            calculated_at_utc="2026-10-20T00:00:00.000000Z",
            model="Microstrip1B",
            calculation_digest="b" * 64,
        )
        worker_results.append(spec.spec_id)
        return result

    def exercise(frame: Any, wx: Any) -> None:
        warm_started: list[bool] = []
        progress: dict[str, list[int]] = {"callback": [], "timer": []}
        painted: list[str] = []

        def render(section: Any, **_options: Any) -> Any:
            assert wx.IsMainThread()
            selected = dialog.preview_pane.selected_section
            if selected is not None and section != selected and not warm_started:
                warm_started.append(dialog.preview_pane.displayed)
                wx.CallAfter(
                    lambda: progress["callback"].append(
                        len(dialog._capture_prepare_sections)
                    )
                )
                probe.StartOnce(1)
                _post_row_click(wx, dialog.sections, 1)
                release_worker.set()
            _capture_work()
            return capture

        dialog = api.dialog.ImpedanceDialog(
            frame,
            config,
            snapshot,
            preview=render,
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_options: (),
            calculate_width=calculate,
        )
        probe = wx.Timer(dialog)
        dialog.Bind(
            wx.EVT_TIMER,
            lambda _event: progress["timer"].append(
                len(dialog._capture_prepare_sections)
            ),
            probe,
        )

        def observe_paint(event: Any) -> None:
            selected = dialog.preview_pane.selected_section
            if selected is not None:
                painted.append(selected.section_id)
            event.Skip()

        dialog.preview_pane.canvas.Bind(wx.EVT_PAINT, observe_paint)
        assert dialog.sections.GetItemCount() == 900
        selected_id = dialog._row_ids[1]

        def inspect() -> None:
            wait_until(
                wx,
                lambda: bool(progress["callback"])
                and bool(progress["timer"])
                and len(worker_results) == 3
                and dialog._calculation_cancel is None
                and len(dialog.session.config.width_results) == 3
                and dialog.sections.GetSelectedRow() == 1
                and dialog.preview_pane.selected_section is not None
                and dialog.preview_pane.selected_section.section_id == selected_id
                and dialog.preview_pane.displayed,
                timeout_ms=5000,
                reason="Background captures starved completion, native input or visible paint",
            )
            assert warm_started == [True], "Warming preceded the first selected paint"
            assert progress["callback"][0] > 0 and progress["timer"][0] > 0
            assert dialog._capture_prepare_sections, (
                "Foreground work waited for every image"
            )
            assert selected_id in painted
            assert (
                dialog.preview_pane.image_digest(dialog.preview_pane.selected_section)
                == capture.sha256
            )
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
        finally:
            release_worker.set()
            probe.Stop()
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("accept", [False, True], ids=["cancel", "apply"])
def test_native_child_modal_pauses_warming_then_parent_resumes_and_close_stops(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    accept: bool,
) -> None:
    """Real child Apply/Cancel resumes pending work; closing revokes queued work."""
    if isolated_native(request, "KICAD_IMPEDANCE_CAPTURE_IDLE_MODAL_CHILD"):
        return
    api = _api(monkeypatch)
    snapshot, config, capture = _fixture(api)

    def exercise(frame: Any, wx: Any) -> None:
        main_calls: list[str] = []
        warm_calls: list[str] = []
        child_errors: list[BaseException] = []
        child_results: list[int] = []

        def render(section: Any, **_options: Any) -> Any:
            if not dialog._child_dialog_open:
                main_calls.append(section.section_id)
                if section != dialog.preview_pane.selected_section:
                    warm_calls.append(section.section_id)
                _capture_work()
            return capture

        dialog = api.dialog.ImpedanceDialog(
            frame,
            config,
            snapshot,
            preview=render,
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_options: (),
        )

        class ChildHook(wx.ModalDialogHook):
            def Enter(self, child: Any) -> int:
                if not isinstance(child, api.dialog.SpecificationDialog):
                    return wx.ID_NONE
                count = len(main_calls)

                def inspect_child() -> None:
                    try:
                        wait_until(wx, lambda: child.preview_pane.displayed)
                        assert dialog._child_dialog_open
                        assert dialog._capture_prepare_sections
                        assert len(main_calls) == count
                        child_results.append(wx.ID_OK if accept else wx.ID_CANCEL)
                        _click(wx, child.FindWindow(child_results[-1]))
                    except BaseException as error:
                        child_errors.append(error)
                        if child.IsModal():
                            child.EndModal(wx.ID_CANCEL)

                wx.CallAfter(inspect_child)
                return wx.ID_NONE

        hook = ChildHook()
        hook.Register()

        def inspect() -> None:
            wait_until(wx, lambda: dialog.preview_pane.displayed and bool(warm_calls))
            assert dialog._capture_prepare_sections
            dialog.specifications.SelectRow(0)
            _click(wx, dialog._selection_buttons[0])
            if child_errors:
                raise child_errors[0]
            assert child_results == [wx.ID_OK if accept else wx.ID_CANCEL]
            assert not dialog._child_dialog_open
            after_child = len(warm_calls)
            wait_until(
                wx,
                lambda: dialog.preview_pane.displayed and len(warm_calls) > after_child,
                reason="Parent capture preparation did not resume after its child closed",
            )
            assert dialog._capture_prepare_sections
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
            count_at_close = len(main_calls)
            elapsed: list[bool] = []
            timer = wx.CallLater(30, elapsed.append, True)
            try:
                wait_until(wx, lambda: bool(elapsed))
            finally:
                timer.Stop()
            assert len(main_calls) == count_at_close
            assert dialog._closing and dialog.preview_pane._closed
            assert not dialog._capture_prepare_timer.IsRunning()
        finally:
            hook.Unregister()
            dialog.Destroy()
            pump(wx)

    run_native(exercise)
