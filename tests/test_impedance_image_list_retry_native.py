"""Exercise the sole image selector and retry recovery with native wx controls.

Captures are deterministic PNG fixtures, not KiCad rendering measurements.
List events use native selection state and the actual bound event handler.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
import importlib
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_capture_preparation_native import (
    _approved_history,
    _fixture,
    _select_image,
)
from .test_impedance_settings_native import _api, _click, _modal

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


def _pane_dialog(
    api: SimpleNamespace,
    frame: Any,
    wx: Any,
    render: Callable[..., Any],
    **options: Any,
) -> tuple[Any, Any, tuple[Any, ...], Any]:
    """Host the production preview in a real modal with independently focused input."""
    snapshot, spec, capture = _fixture(api)
    sections = api.dialog.preview_sections(spec, snapshot, "F.Cu")
    preview_api = importlib.import_module(f"{api.dialog.__package__}.dialog_preview")
    dialog = wx.Dialog(
        frame, title="Deterministic image review fixture", size=(760, 560)
    )
    image_list = wx.ListCtrl(
        dialog, style=wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL
    )
    image_list.InsertColumn(0, "Review", width=35)
    image_list.InsertColumn(1, "Workbook image", width=570)
    image_list.SetMinSize((1, 146))
    image_list.SetName("Workbook image review checklist")
    pane = preview_api.WorkbookPreview(dialog, render, image_list=image_list, **options)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(image_list, 0, wx.EXPAND | wx.ALL, 8)
    layout.Add(pane.window, 1, wx.EXPAND | wx.ALL, 8)
    entry = wx.TextCtrl(dialog, value="Independent keyboard focus")
    layout.Add(entry, 0, wx.EXPAND | wx.ALL, 8)
    layout.Add(dialog.CreateButtonSizer(wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
    dialog.SetSizer(layout)
    pane.set_sections(sections, "fixture-inputs")

    def on_show(event: Any) -> None:
        if event.GetEventObject() is dialog and event.IsShown():
            pane.show_selected()
        event.Skip()

    dialog.Bind(wx.EVT_SHOW, on_show)
    return dialog, pane, sections, entry


def _wait_elapsed(wx: Any, milliseconds: int) -> None:
    """Let actual native timers run without blocking the desktop event loop."""
    deadline = time.monotonic() + milliseconds / 1000
    wait_until(wx, lambda: time.monotonic() >= deadline, milliseconds + 1000)


def test_native_image_list_is_only_selector_and_precedes_preview(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """The image list replaces the dropdown and grows above the preview canvas."""
    if isolated_native(request, "KICAD_IMPEDANCE_IMAGE_LIST_LAYOUT_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        dialog = api.dialog.SpecificationDialog(
            frame, snapshot, specification=spec, preview=lambda *_a, **_k: capture
        )

        def inspect() -> None:
            pane = dialog.preview_pane
            wait_until(wx, lambda: pane.ready)
            assert pane.checklist.IsShownOnScreen()
            assert pane.checklist.GetMinHeight() == 146
            assert (
                pane.checklist.GetScreenRect().bottom <= pane.canvas.GetScreenRect().top
            )
            assert pane.checklist.GetFirstSelected() == pane._index
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert not hasattr(dialog.preview_pane, "rows"), (
                "The redundant dropdown must be removed"
            )
            assert not hasattr(dialog.preview_pane, "refresh_button"), (
                "Recovery must be automatic"
            )
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


def test_native_list_selection_updates_in_place_without_render_or_focus_loops(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Native list events navigate; status refresh preserves selection, scroll and focus."""
    if isolated_native(request, "KICAD_IMPEDANCE_IMAGE_LIST_EVENTS_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        _snapshot, _spec, capture = _fixture(api)
        calls: list[Any] = []

        def render(section: Any, **_options: Any) -> Any:
            calls.append(section)
            return capture

        dialog, pane, sections, entry = _pane_dialog(api, frame, wx, render)
        many = tuple(
            replace(
                sections[index % 2],
                section_id=f"image-{index}",
                net_names=(f"NET_{index}",),
                exclusion_reason="covered_stub" if index == 2 else "",
            )
            for index in range(32)
        )
        pane.set_sections(many, "many-fixture-images")

        def inspect() -> None:
            wait_until(wx, lambda: pane.ready)
            assert len(calls) == 1
            assert pane.checklist.GetItemTextColour(1) == wx.SystemSettings.GetColour(
                wx.SYS_COLOUR_LISTBOXTEXT
            ), "Unviewed non-stubs must use normal theme text"
            assert pane.checklist.GetItemTextColour(2) == wx.SystemSettings.GetColour(
                wx.SYS_COLOUR_GRAYTEXT
            ), "Covered stubs must use muted theme text"
            _select_image(wx, pane.checklist, 1)
            wait_until(wx, lambda: pane.ready and pane.selected_section == many[1])
            assert calls == [many[0], many[1]]
            assert "viewed" in pane.checklist.GetItemText(1, 1)
            selected = pane.checklist.GetFirstSelected()
            focused = pane.checklist.GetFocusedItem()
            pane.checklist.EnsureVisible(31)
            entry.SetFocus()
            pump(wx)
            top = pane.checklist.GetTopItem()
            assert wx.Window.FindFocus() is entry
            for _update in range(4):
                pane.refresh_review_labels()
                pump(wx)
            assert pane.checklist.GetFirstSelected() == selected
            assert pane.checklist.GetFocusedItem() == focused
            assert pane.checklist.GetTopItem() == top
            assert wx.Window.FindFocus() is entry
            assert calls == [many[0], many[1]], (
                "Status updates must not request captures"
            )
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("failure_kind", ["render", "decode", "paint", "verify"])
def test_native_failed_capture_retries_and_counts_only_successful_visible_paint(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    failure_kind: str,
) -> None:
    """Provider, decode, bitmap drawing and post-paint checks recover without a button."""
    if isolated_native(request, "KICAD_IMPEDANCE_IMAGE_RETRY_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        _snapshot, _spec, capture = _fixture(api)
        calls: list[bool] = []
        viewed: list[Any] = []
        failed: list[bool] = []
        bad_capture = type(capture).from_bytes(capture.data[:33])
        injected = False

        def render(_section: Any, *, refresh: bool = False) -> Any:
            calls.append(refresh)
            if len(calls) == 1 and failure_kind == "render":
                raise RuntimeError("Transient native fixture renderer failure")
            if len(calls) == 1 and failure_kind == "decode":
                return bad_capture
            return capture

        def verify() -> None:
            nonlocal injected
            if failure_kind == "verify" and pane.capture_loaded and not injected:
                injected = True
                raise ValueError("Transient post-paint board verification failure")

        dialog, pane, _sections, _entry = _pane_dialog(
            api,
            frame,
            wx,
            render,
            verify_current=verify,
            image_viewed=lambda *args: viewed.append(args),
            review_failed=lambda: failed.append(True),
        )
        if failure_kind == "paint":
            original_dc = wx.AutoBufferedPaintDC

            class FailFirstBitmap:
                """Delegate native paint while injecting one actual bitmap draw error."""

                def __init__(self, window: Any) -> None:
                    self._dc = original_dc(window)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._dc, name)

                def DrawBitmap(self, *args: Any, **kwargs: Any) -> None:
                    nonlocal injected
                    if not injected:
                        injected = True
                        raise ValueError("Transient native bitmap drawing failure")
                    self._dc.DrawBitmap(*args, **kwargs)

            monkeypatch.setattr(wx, "AutoBufferedPaintDC", FailFirstBitmap)

        def inspect() -> None:
            wait_until(wx, lambda: bool(pane.failure))
            assert failed
            assert not pane.ready
            assert viewed == [], "Failed images must not be recorded as viewed"
            assert pane._retry_timer.IsRunning()
            assert pane.failure in pane.status.GetValue()
            with pytest.raises(ValueError):
                pane.ensure_reviewed()
            wait_until(wx, lambda: pane.ready, 5000)
            assert calls == [False, True]
            assert len(viewed) == 1
            assert pane.failure == ""
            assert not pane._retry_timer.IsRunning()
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("obsolete", ["inputs", "close"])
def test_native_obsolete_retry_never_requests_old_capture(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    obsolete: str,
) -> None:
    """Replacing inputs or closing the modal cancels the old source's retry timer."""
    if isolated_native(request, "KICAD_IMPEDANCE_IMAGE_RETRY_OBSOLETE_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        _snapshot, _spec, capture = _fixture(api)
        calls: list[str] = []

        def render(section: Any, **_options: Any) -> Any:
            calls.append(section.section_id)
            if section.section_id != "replacement":
                raise ValueError("Old fixture source is unavailable")
            return capture

        dialog, pane, sections, _entry = _pane_dialog(api, frame, wx, render)

        def inspect() -> None:
            wait_until(wx, lambda: bool(pane.failure))
            assert len(calls) == 1
            assert pane._retry_timer.IsRunning()
            if obsolete == "inputs":
                replacement = replace(sections[0], section_id="replacement")
                pane.set_sections((replacement,), "replacement-context")
                wait_until(wx, lambda: pane.ready)
                _wait_elapsed(wx, 1250)
                assert calls == [sections[0].section_id, "replacement"]
                assert not pane._retry_timer.IsRunning()
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
            dialog.Destroy()
            pump(wx)
            count_at_destroy = len(calls)
            _wait_elapsed(wx, 1250)
            assert len(calls) == count_at_destroy
        finally:
            if dialog:
                dialog.Destroy()
                pump(wx)

    run_native(exercise)


def test_native_failed_background_capture_recovers_without_creating_review(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """An unselected failed image is retried while review and approval remain pending."""
    if isolated_native(request, "KICAD_IMPEDANCE_IMAGE_BACKGROUND_RETRY_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        calls: Counter[Any] = Counter()
        target: list[Any] = []

        def render(section: Any, **_options: Any) -> Any:
            calls[section] += 1
            if section.layer == "F.Cu" and section.net_names == ("B",):
                target[:] = [section]
                if calls[section] == 1:
                    raise ValueError("Transient unselected capture failure")
            return capture

        dialog = api.dialog.SpecificationDialog(
            frame, snapshot, specification=spec, preview=render
        )

        def inspect() -> None:
            wait_until(wx, lambda: dialog.preview_pane.ready)
            wait_until(wx, lambda: bool(target))
            assert len(dialog.review_tracking.images) == 1
            assert dialog.review_tracking.layers == ()
            assert not dialog._layer_drafts["F.Cu"].confirmed
            wait_until(wx, lambda: calls[target[0]] == 2, 5000)
            wait_until(wx, lambda: len(calls) == 4)
            assert len(dialog.review_tracking.images) == 1
            assert dialog.review_tracking.layers == ()
            _select_image(wx, dialog.preview_pane.checklist, 1)
            wait_until(wx, lambda: dialog.preview_pane.ready)
            assert calls[target[0]] == 2, "Recovered background pixels should be cached"
            assert len(dialog.review_tracking.images) == 2
            assert not dialog._layer_drafts["F.Cu"].confirmed
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
            assert not dialog._capture_prepare_timer.IsRunning()
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


@pytest.mark.parametrize("failure_kind", ["decode", "display"])
@pytest.mark.parametrize("optional_stub", [False, True])
def test_native_background_matching_history_cannot_clear_failed_display(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    failure_kind: str,
    optional_stub: bool,
) -> None:
    """A warmed matching PNG cannot restore a failed image until it visibly succeeds."""
    if isolated_native(request, "KICAD_IMPEDANCE_IMAGE_FAILED_HISTORY_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        if optional_stub:
            snapshot = replace(
                snapshot,
                copper_features=(
                    api.model.CopperFeature(
                        "native-covered-pad",
                        "A",
                        "F.Cu",
                        "pad",
                        "rect",
                        (1_000_000, 0),
                        4_000_000,
                        1_000_000,
                    ),
                ),
            )
        history = _approved_history(api, frame, wx, snapshot, spec, capture)
        bad_capture = type(capture).from_bytes(capture.data[:33])
        calls: Counter[Any] = Counter()
        armed = False
        dialog: Any = None

        def render(section: Any, **_options: Any) -> Any:
            calls[section] += 1
            return capture

        dialog = api.dialog.SpecificationDialog(
            frame,
            snapshot,
            specification=spec,
            preview=render,
            review_tracking=history,
            verify_snapshot=lambda expected: (
                None if expected == snapshot else pytest.fail("Unexpected snapshot")
            ),
        )
        pane = dialog.preview_pane
        original_index = next(
            index
            for index, section in enumerate(pane.sections)
            if section.net_names == ("A",)
        )
        other_index = next(
            index
            for index, section in enumerate(pane.sections)
            if section.net_names == ("B",)
        )
        original, other = pane.sections[original_index], pane.sections[other_index]
        assert bool(original.exclusion_reason) == optional_stub
        install_capture = pane.canvas.set_capture

        def install(image: Any) -> None:
            """Inject one real native PNG decode failure at the selected canvas."""
            nonlocal armed
            if failure_kind == "decode" and armed and pane.selected_section == original:
                armed = False
                install_capture(bad_capture)
                return
            install_capture(image)

        callback_patches = pytest.MonkeyPatch()
        callback_patches.setattr(pane.canvas, "set_capture", install)
        displayed = pane.canvas._displayed

        def record_display() -> None:
            """Fail once after actual native paint, before recording its view evidence."""
            nonlocal armed
            if (
                failure_kind == "display"
                and armed
                and pane.selected_section == original
            ):
                armed = False
                raise ValueError(
                    "Transient review verification after native visible paint"
                )
            displayed()

        callback_patches.setattr(pane.canvas, "_displayed", record_display)

        def inspect() -> None:
            nonlocal armed
            wait_until(wx, lambda: pane.displayed)
            wait_until(wx, lambda: not dialog._capture_prepare_timer.IsRunning())
            assert dialog._layer_drafts["F.Cu"].confirmed
            _select_image(wx, pane.checklist, other_index)
            wait_until(wx, lambda: pane.displayed and pane.selected_section == other)
            original_record = next(
                record
                for record in dialog.review_tracking.images
                if record.section_id == original.section_id
            )
            armed = True
            _select_image(wx, pane.checklist, original_index)
            wait_until(wx, lambda: bool(pane.failure))
            assert pane.selected_section == original
            assert original in dialog._capture_display_failed
            assert not dialog._layer_drafts["F.Cu"].confirmed
            assert dialog.capture_digests[original.section_id] is None
            _select_image(wx, pane.checklist, other_index)
            wait_until(wx, lambda: pane.displayed and pane.selected_section == other)
            wait_until(
                wx,
                lambda: dialog._capture_hashes.get(original) == capture.sha256,
                5000,
            )
            assert calls[original] == 2
            assert original in dialog._capture_display_failed
            assert original in dialog._capture_errors
            assert dialog.capture_digests[original.section_id] is None
            assert not dialog._layer_drafts["F.Cu"].confirmed
            assert "needs review" in pane.checklist.GetItemText(original_index, 1)
            assert "Awaiting successful display" in pane.status.GetValue()
            assert (
                next(
                    record
                    for record in dialog.review_tracking.images
                    if record.section_id == original.section_id
                )
                == original_record
            ), "Background pixels must preserve the historical view"
            assert dialog.review_tracking.layers == history.layers

            _click(wx, dialog.approve_layer)
            assert pane.selected_section == original
            assert not pane.ready
            assert original in dialog._capture_display_failed
            assert not dialog._layer_drafts["F.Cu"].confirmed
            assert dialog.review_tracking.layers == history.layers
            wait_until(wx, lambda: pane.displayed and pane.selected_section == original)
            assert calls[original] == 2, "The visible retry should reuse warmed pixels"
            assert original not in dialog._capture_display_failed
            assert original not in dialog._capture_errors
            assert dialog.capture_digests[original.section_id] is not None
            assert dialog._layer_drafts["F.Cu"].confirmed
            assert "approved" in pane.checklist.GetItemText(original_index, 1)
            assert pane.status.GetValue() == ""
            callback_patches.undo()
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
        finally:
            callback_patches.undo()
            dialog.Destroy()
            pump(wx)

    run_native(exercise)
