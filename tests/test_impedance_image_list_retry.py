"""Exercise one image selector and automatic capture recovery through real events.

The constructor runtime models selection, timers, and visible paints explicitly;
native platform layout and colour contrast still require the separate wx checks.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests import test_impedance_dialog as dialog_tests
from tests.test_impedance_capture_review_restore import (
    _approved_config,
    _approved_editor,
)
from tests.test_impedance_dialog import _snapshot
from tests.test_impedance_draft_dialog import draft_api as _draft_api
from tests.test_impedance_netclass_dialog import apply_layer

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.dialog_preview import WorkbookPreview


constructor_api = dialog_tests.constructor_api
draft_api = _draft_api


def _choose_rf(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Choose RF using its real bound event, then complete its first paint."""
    dialog.net_class.SetSelection(dialog._net_class_names.index("RF") + 1)
    dialog.net_class.emit(api.wx.EVT_CHOICE)
    api.drain()
    api.paint(dialog.preview_pane)


def _tick(api: SimpleNamespace, owner: object, timer: object) -> None:
    """Deliver an already queued one-shot timer event through its real binding."""
    timer.running = False
    owner.bindings[api.wx.EVT_TIMER, timer](SimpleNamespace(Skip=lambda: None))
    api.drain()


def _select(api: SimpleNamespace, pane: WorkbookPreview, index: int) -> None:
    """Change native list state before dispatching the user's selection event."""
    pane.checklist.Select(index)
    pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    api.drain()


def _contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    """Compare native theme colours using relative luminance for ordinary text."""

    def luminance(colour: tuple[int, int, int]) -> float:
        """Convert the theme's sRGB triplet into a linear luminance value."""
        linear = tuple(
            channel / 255 / 12.92
            if channel / 255 <= 0.04045
            else ((channel / 255 + 0.055) / 1.055) ** 2.4
            for channel in colour
        )
        return sum(
            weight * channel
            for weight, channel in zip((0.2126, 0.7152, 0.0722), linear)
        )

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_single_taller_image_list_precedes_canvas_without_refresh_button(
    constructor_api: SimpleNamespace,
) -> None:
    """Expose one selector above the capture with the requested 30% height gain."""
    api = constructor_api
    dialog = api.create_dialog(_snapshot(api))
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    assert not hasattr(pane, "rows")
    assert not hasattr(pane, "refresh_button")
    assert pane.checklist.minimum_size == (1, 146)
    navigation_layout = pane.checklist.parent.sizer.items
    assert navigation_layout.index(pane.checklist) < navigation_layout.index(
        pane.window
    )
    assert (api.wx.EVT_LIST_ITEM_SELECTED, -1) in pane.checklist.bindings


def test_failed_selected_capture_retries_automatically_without_recording_a_view(
    constructor_api: SimpleNamespace,
) -> None:
    """Retry transient rendering errors without treating queued pixels as review."""
    api = constructor_api
    attempts: list[bool] = []

    def render(_section: object, *, refresh: bool = False) -> object:
        """Fail once, then return the same production capture contract."""
        attempts.append(refresh)
        if len(attempts) == 1:
            raise ValueError("temporary native capture error")
        return api.capture

    dialog = api.create_dialog(_snapshot(api), render=render)
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    assert "temporary native capture error" in pane.failure
    assert not pane.ready
    assert not dialog.review_tracking.images
    timer = pane._retry_timer
    assert timer.IsRunning()
    assert timer.interval_ms == 1000
    assert "temporary native capture error" in pane.status.GetValue()
    _tick(api, pane.window, timer)
    assert attempts == [False, True]
    assert pane.capture_loaded
    assert not pane.ready
    assert not dialog.review_tracking.images
    api.paint(pane)
    assert pane.ready
    assert len(dialog.review_tracking.images) == 1
    assert not timer.IsRunning()
    assert not pane.failure


def test_list_selection_uses_prepared_pixels_without_marking_hidden_rows_viewed(
    constructor_api: SimpleNamespace,
) -> None:
    """A prepared image is fresh and cached, but only selected pixels count as viewed."""
    api = constructor_api
    dialog = api.create_dialog(_snapshot(api))
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    assert len(pane.sections) == 2
    api.finish_preparation(dialog)
    assert len(api.preview_calls) == 2
    assert len(dialog.review_tracking.images) == 1
    _select(api, pane, 1)
    assert pane.selected_section == pane.sections[1]
    assert len(api.preview_calls) == 2
    assert not pane.ready
    api.paint(pane)
    assert pane.ready
    assert len(dialog.review_tracking.images) == 2


@pytest.mark.parametrize("dark", [False, True])
def test_stub_emphasis_is_muted_and_nonstub_text_remains_readable(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, dark: bool
) -> None:
    """Derive unviewed/stub colours from the theme and retain readable viewed text."""
    api = constructor_api
    normal = (235, 235, 235) if dark else (25, 25, 25)
    muted = (140, 140, 140) if dark else (130, 130, 130)
    background = (25, 25, 25) if dark else (250, 250, 250)
    palette = {
        api.wx.SYS_COLOUR_LISTBOXTEXT: normal,
        api.wx.SYS_COLOUR_GRAYTEXT: muted,
        api.wx.SYS_COLOUR_LISTBOX: background,
    }
    monkeypatch.setattr(
        api.wx.SystemSettings, "GetColour", lambda kind: palette.get(kind, normal)
    )
    dialog = api.create_dialog(_snapshot(api))
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    original = pane.sections
    stub = replace(original[1], exclusion_reason=api.model.EXCLUSION_COVERED_STUB)
    pane.set_sections((original[0], stub), ("theme-regression", dark))
    assert pane.checklist.row_colours == {0: normal, 1: muted}
    api.drain()
    api.paint(pane)
    assert pane.checklist.table_rows[0][0] == "—"
    assert "viewed" in pane.checklist.table_rows[0][1]
    assert pane.checklist.row_colours[0] != muted
    assert _contrast(pane.checklist.row_colours[0], background) >= 4.5
    assert pane.checklist.row_colours[1] == muted
    _select(api, pane, 1)
    api.paint(pane)
    assert pane.checklist.table_rows[1][0] == "—"
    assert pane.checklist.row_colours[1] == muted


def test_programmatic_list_updates_preserve_selection_and_do_not_render_again(
    constructor_api: SimpleNamespace,
) -> None:
    """Native synchronous selection notifications cannot recursively queue renders."""
    api = constructor_api
    dialog = api.create_dialog(_snapshot(api))
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    _select(api, pane, 1)
    api.paint(pane)
    pane.checklist.visible_index = 0
    count = len(api.preview_calls)
    request = pane._request
    for _iteration in range(3):
        pane.refresh_review_labels()
        api.drain()
    assert pane.checklist.GetFirstSelected() == 1
    assert pane.checklist.visible_index == 0
    assert pane._request == request
    assert len(api.preview_calls) == count
    assert pane.ready


def test_retry_delays_increase_and_cap_until_a_successful_visible_display(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful capture followed by failed paint must not reset backoff early."""
    api = constructor_api
    available = [False]

    def render(_section: object, *, refresh: bool = False) -> object:
        """Allow pixels only after the persistent source failure has recovered."""
        if not available[0]:
            raise ValueError("capture unavailable")
        return api.capture

    dialog = api.create_dialog(_snapshot(api), render=render)
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    timer = pane._retry_timer
    for expected in (1000, 2000, 4000, 8000, 16000, 30000, 30000):
        assert timer.IsRunning()
        assert timer.interval_ms == expected
        assert not dialog.review_tracking.images
        _tick(api, pane.window, timer)
    available[0] = True
    _tick(api, pane.window, timer)
    assert pane.capture_loaded
    assert not pane.ready

    def reject_display() -> None:
        """Reject the post-paint source check after pixels loaded successfully."""
        raise ValueError("paint verification failed")

    with monkeypatch.context() as patch:
        patch.setattr(pane, "_verify_current", reject_display)
        api.paint(pane)
    assert "paint verification failed" in pane.failure
    assert timer.IsRunning()
    assert timer.interval_ms == 30000
    assert not dialog.review_tracking.images
    _tick(api, pane.window, timer)
    api.paint(pane)
    assert pane.ready
    assert not timer.IsRunning()
    # A later independent failure starts over only after that displayed success.
    available[0] = False
    pane.select(1)
    api.drain()
    assert timer.interval_ms == 1000


def test_background_failure_waits_for_due_time_then_recovers_without_a_view(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not permanently blacklist failed hidden captures or busy-loop their retry."""
    api = constructor_api
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])
    attempts: dict[object, int] = {}

    def render(section: object, *, refresh: bool = False) -> object:
        """Only the first background request fails."""
        attempts[section] = attempts.get(section, 0) + 1
        if len(attempts) > 1 and attempts[section] == 1:
            raise ValueError("background capture unavailable")
        return api.capture

    dialog = api.create_dialog(_snapshot(api), render=render)
    _choose_rf(api, dialog)
    first, second = dialog.preview_pane.sections
    timer = dialog._capture_prepare_timer
    _tick(api, dialog, timer)
    assert attempts == {first: 1, second: 1}
    assert timer.IsRunning()
    # One settling tick may scan for other unprepared captures. Once every
    # candidate is waiting on retry, preparation must sleep until its due time.
    _tick(api, dialog, timer)
    assert attempts == {first: 1, second: 1}
    assert timer.IsRunning()
    assert timer.interval_ms >= 1000
    now[0] += 1.0
    _tick(api, dialog, timer)
    api.finish_preparation(dialog)
    assert attempts == {first: 1, second: 2}
    assert second in dialog._capture_hashes
    assert len(dialog.review_tracking.images) == 1
    assert not dialog.review_tracking.layers
    assert not dialog._layer_drafts["F.Cu"].confirmed


def test_all_failed_captures_eventually_recover_without_competing_selected_retries(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreground and background queues recover after every initial capture failed."""
    api = constructor_api
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])
    attempts: dict[object, int] = {}

    def render(section: object, *, refresh: bool = False) -> object:
        """Each exact section recovers on its second request."""
        attempts[section] = attempts.get(section, 0) + 1
        if attempts[section] == 1:
            raise ValueError("transient capture outage")
        return api.capture

    dialog = api.create_dialog(_snapshot(api), render=render)
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    first, second = pane.sections
    _tick(api, dialog, dialog._capture_prepare_timer)
    assert attempts == {first: 1, second: 1}
    assert pane._retry_timer.IsRunning()
    now[0] += 1.0
    _tick(api, pane.window, pane._retry_timer)
    api.paint(pane)
    _tick(api, dialog, dialog._capture_prepare_timer)
    api.finish_preparation(dialog)
    assert attempts == {first: 2, second: 2}
    assert set(dialog._capture_hashes) == {first, second}
    assert len(dialog.review_tracking.images) == 1
    assert not dialog.review_tracking.layers


@pytest.mark.parametrize("change", ["selection", "inputs", "cancel", "apply"])
def test_obsolete_retry_cannot_render_or_authorize_after_navigation_or_close(
    constructor_api: SimpleNamespace, change: str
) -> None:
    """Queued timer events from failed pixels cannot revive stale inputs or approval."""
    api = constructor_api
    attempts: list[object] = []

    def render(section: object, *, refresh: bool = False) -> object:
        """Keep every capture unavailable while the user changes context."""
        attempts.append(section)
        raise ValueError("capture offline")

    dialog = api.create_dialog(_snapshot(api), render=render)
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    timer = pane._retry_timer
    assert timer.IsRunning()
    if change == "selection":
        _select(api, pane, 1)
    elif change == "inputs":
        dialog.net_class.SetSelection(0)
        dialog.net_class.emit(api.wx.EVT_CHOICE)
        api.drain()
    else:
        control_id = api.wx.ID_CANCEL if change == "cancel" else api.wx.ID_OK
        dialog.emit(api.wx.EVT_BUTTON, control_id)
        api.drain()
        if change == "apply":
            assert dialog.modal_result == api.wx.ID_OK
            assert dialog.specification is not None
        assert not timer.IsRunning()
    count = len(attempts)
    if change != "selection":
        _tick(api, pane.window, timer)
        assert len(attempts) == count
        assert not timer.IsRunning()
    else:
        # A fresh selection gets its own retry; old pending display evidence is gone.
        assert pane.selected_section == pane.sections[1]
    assert not dialog.review_tracking.images
    assert not dialog.review_tracking.layers
    assert all(not draft.confirmed for draft in dialog._layer_drafts.values())


def test_saved_layer_approval_waits_for_retry_and_all_current_capture_hashes(
    constructor_api: SimpleNamespace,
) -> None:
    """A transient reopen failure cannot authorize stale pixels or erase matching history."""
    api = constructor_api
    snapshot = _snapshot(api)
    original = api.create_dialog(snapshot)
    _choose_rf(api, original)
    _select(api, original.preview_pane, 1)
    api.paint(original.preview_pane)
    apply_layer(api, original)
    original.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert original.modal_result == api.wx.ID_OK
    assert len(original.review_tracking.layers) == 1
    saved = original.review_tracking
    attempts = [0]

    def render(_section: object, *, refresh: bool = False) -> object:
        """Simulate a transient failure reopening otherwise identical source pixels."""
        attempts[0] += 1
        if attempts[0] == 1:
            raise ValueError("temporary reopen capture error")
        return api.capture

    reopened = api.create_dialog(
        snapshot,
        original.specification,
        render=render,
        review_tracking=saved,
    )
    pane = reopened.preview_pane
    assert pane.failure
    assert not reopened._layer_drafts["F.Cu"].confirmed
    assert not pane.ready
    _tick(api, pane.window, pane._retry_timer)
    assert pane.capture_loaded
    assert not reopened._layer_drafts["F.Cu"].confirmed
    api.paint(pane)
    api.finish_preparation(reopened)
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking.layers == saved.layers
    assert len(reopened.review_tracking.images) == len(saved.images)
    assert all(row[0] == "☑" for row in pane.checklist.table_rows)


@pytest.mark.parametrize("failure_phase", ["decode", "paint"])
def test_saved_display_failure_cannot_reapprove_until_successful_visible_recovery(
    constructor_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    """Warming matching bytes cannot undo a known failure displaying those bytes."""
    api = constructor_api
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])
    snapshot = _snapshot(api)
    original = api.create_dialog(snapshot)
    _choose_rf(api, original)
    _select(api, original.preview_pane, 1)
    api.paint(original.preview_pane)
    apply_layer(api, original)
    original.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert original._layer_drafts["F.Cu"].confirmed
    reopened = api.create_dialog(
        snapshot,
        original.specification,
        review_tracking=original.review_tracking,
    )
    pane = reopened.preview_pane
    failed_section = pane.sections[0]
    good_set_capture = pane.canvas.set_capture
    good_displayed = pane.canvas._displayed

    def reject_failed_image(capture: object) -> None:
        """Retain a native decode failure for one section while its sibling displays."""
        if pane.selected_section == failed_section:
            raise ValueError("persistent known selected decode failure")
        good_set_capture(capture)

    def reject_failed_display() -> None:
        """Deliver a failed post-paint display check through the actual callback."""
        if pane.selected_section == failed_section:
            raise ValueError("persistent known selected display verification failure")
        good_displayed()

    with monkeypatch.context() as patch:
        if failure_phase == "decode":
            patch.setattr(pane.canvas, "set_capture", reject_failed_image)
        else:
            patch.setattr(pane.canvas, "_displayed", reject_failed_display)
        pane.select(0, refresh=True)
        api.drain()
        api.paint(pane)
        assert pane.failure
        assert not reopened._layer_drafts["F.Cu"].confirmed
        _select(api, pane, 1)
        api.paint(pane)
        now[0] += 1.0
        _tick(api, reopened, reopened._capture_prepare_timer)
        assert failed_section in reopened._capture_hashes
        assert not reopened._layer_drafts["F.Cu"].confirmed
        assert reopened.capture_digests[failed_section.section_id] is None
        assert pane.checklist.table_rows[0][0] == "—"
        assert "needs review" in pane.checklist.table_rows[0][1]
    # Merely selecting successfully decoded cached pixels still supplies no
    # current evidence that the known display failure has actually recovered.
    _select(api, pane, 0)
    assert pane.capture_loaded
    assert not reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.capture_digests[failed_section.section_id] is None
    api.paint(pane)
    assert pane.ready
    assert reopened.capture_digests[failed_section.section_id]
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking.layers == original.review_tracking.layers


def test_background_failure_remains_visible_during_retry_until_recovery(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy selected image cannot hide a failed hidden capture's recovery state."""
    api = constructor_api
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])
    attempts: dict[object, int] = {}

    def render(section: object, *, refresh: bool = False) -> object:
        """Fail the background candidate once without disturbing the selected one."""
        attempts[section] = attempts.get(section, 0) + 1
        if len(attempts) > 1 and attempts[section] == 1:
            raise ValueError("background capture unavailable")
        return api.capture

    dialog = api.create_dialog(_snapshot(api), render=render)
    _choose_rf(api, dialog)
    pane = dialog.preview_pane
    timer = dialog._capture_prepare_timer
    _tick(api, dialog, timer)
    assert pane.ready
    assert "background capture unavailable" in pane.status.GetValue()
    assert "retry" in pane.status.GetValue().lower()
    pane.show_selected()
    api.drain()
    api.paint(pane)
    assert "background capture unavailable" in pane.status.GetValue()
    assert "retry" in pane.status.GetValue().lower()
    now[0] += 1.0
    _tick(api, dialog, timer)
    api.finish_preparation(dialog)
    assert pane.ready
    assert "background capture unavailable" not in pane.status.GetValue()
    assert len(dialog.review_tracking.images) == 1


def test_background_renderer_recovery_can_restore_matching_historical_approval(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hidden renderer outage has no known display failure requiring a new view."""
    api = constructor_api
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])
    snapshot = _snapshot(api)
    original = api.create_dialog(snapshot)
    _choose_rf(api, original)
    _select(api, original.preview_pane, 1)
    api.paint(original.preview_pane)
    apply_layer(api, original)
    original.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    saved = original.review_tracking
    first, second = original.preview_pane.sections
    attempts: dict[object, int] = {}

    def render(section: object, *, refresh: bool = False) -> object:
        """Keep the selected image healthy and fail only the hidden initial request."""
        attempts[section] = attempts.get(section, 0) + 1
        if section == second and attempts[section] == 1:
            raise ValueError("temporary background renderer error")
        return api.capture

    reopened = api.create_dialog(
        snapshot,
        original.specification,
        render=render,
        review_tracking=saved,
    )
    pane = reopened.preview_pane
    assert pane.selected_section == first
    assert pane.ready
    timer = reopened._capture_prepare_timer
    _tick(api, reopened, timer)
    assert attempts == {first: 1, second: 1}
    assert not reopened._layer_drafts["F.Cu"].confirmed
    now[0] += 1.0
    _tick(api, reopened, timer)
    api.finish_preparation(reopened)
    assert attempts == {first: 1, second: 2}
    assert pane.selected_section == first
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking.layers == saved.layers
    assert all(row[0] == "☑" for row in pane.checklist.table_rows)


def test_selected_optional_stub_failure_blocks_approval_until_visible_recovery(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An optional image's known display failure cannot silently restore its layer."""
    api = constructor_api
    board, original = _approved_editor(api, covered_stub=True)
    reopened = api.create_dialog(
        board,
        original.specification,
        review_tracking=original.review_tracking,
    )
    pane = reopened.preview_pane
    api.finish_preparation(reopened)
    assert reopened._layer_drafts["F.Cu"].confirmed
    stub = next(
        section
        for section in pane.sections
        if section.exclusion_reason == api.model.EXCLUSION_COVERED_STUB
    )
    stub_index = pane.sections.index(stub)
    good_set_capture = pane.canvas.set_capture

    def reject_stub(capture: object) -> None:
        """Preserve a native display error only for the optional covered stub."""
        if pane.selected_section == stub:
            raise ValueError("persistent optional stub decode failure")
        good_set_capture(capture)

    with monkeypatch.context() as patch:
        patch.setattr(pane.canvas, "set_capture", reject_stub)
        pane.select(stub_index, refresh=True)
        api.drain()
        assert pane.failure
        assert not reopened._layer_drafts["F.Cu"].confirmed
        _tick(api, reopened, reopened._capture_prepare_timer)
        assert pane.failure
        assert not reopened._layer_drafts["F.Cu"].confirmed
        assert reopened.capture_digests[stub.section_id] is None
    _tick(api, pane.window, pane._retry_timer)
    assert pane.capture_loaded
    assert not pane.ready
    assert not reopened._layer_drafts["F.Cu"].confirmed
    api.paint(pane)
    assert pane.ready
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.capture_digests[stub.section_id]
    assert reopened.review_tracking.layers == original.review_tracking.layers


def test_optional_stub_display_failure_survives_apply_to_parent(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning failed optional pixels from a real child cannot approve its parent."""
    api = draft_api
    board, original = _approved_editor(api, covered_stub=True)
    config = replace(
        _approved_config(api, board, original),
        reviewed_digest="",
        included_section_ids=(),
    )
    parent = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    parent.on_screen = True
    assert not parent.session.approved

    def apply_with_failed_stub(child: SpecificationDialog) -> int:
        """Show the actual editor, fail its optional image, then Apply settings."""
        child.on_screen = True
        child.bindings[api.wx.EVT_SHOW, -1](
            SimpleNamespace(
                GetEventObject=lambda: child,
                IsShown=lambda: True,
                Skip=lambda: None,
            )
        )
        api.drain()
        api.paint(child.preview_pane)
        api.finish_preparation(child)
        assert child._layer_drafts["F.Cu"].confirmed
        pane = child.preview_pane
        stub = next(
            section
            for section in pane.sections
            if section.exclusion_reason == api.model.EXCLUSION_COVERED_STUB
        )

        def reject_stub(_capture: object) -> None:
            """Reject the selected optional capture through its normal failure path."""
            raise ValueError("optional stub decoder still unavailable")

        monkeypatch.setattr(pane.canvas, "set_capture", reject_stub)
        pane.select(pane.sections.index(stub), refresh=True)
        api.drain()
        assert pane.failure
        assert not child._layer_drafts["F.Cu"].confirmed
        assert child.capture_digests[stub.section_id] is None
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog,
        "ShowModal",
        apply_with_failed_stub,
        raising=False,
    )
    parent.specifications.SelectRow(0)
    parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    assert not parent.session.approved
    assert not parent.session.approve_if_layer_reviews_complete()
    assert parent.session.config.review_tracking.layers == config.review_tracking.layers
    stub = next(
        section
        for section in parent.session.analysis.sections
        if section.exclusion_reason == api.model.EXCLUSION_COVERED_STUB
    )
    historical_stub = api.dialog.tracking.find_image(
        config.review_tracking, stub.spec_id, stub.layer, stub.section_id
    )
    assert historical_stub is not None
    assert (
        api.dialog.tracking.find_image(
            parent.session.config.review_tracking,
            stub.spec_id,
            stub.layer,
            stub.section_id,
        )
        == historical_stub
    )
    assert parent.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"


def test_failed_optional_capture_keeps_retrying_after_user_navigates_away(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once explicitly requested, an optional image must recover in the background."""
    api = constructor_api
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])
    board, original = _approved_editor(api, covered_stub=True)
    stub = next(
        section
        for section in original.preview_pane.sections
        if section.exclusion_reason == api.model.EXCLUSION_COVERED_STUB
    )
    attempts: dict[object, int] = {}

    def render(section: object, *, refresh: bool = False) -> object:
        """Fail the first explicit optional request while other captures remain healthy."""
        attempts[section] = attempts.get(section, 0) + 1
        if section == stub and attempts[section] == 1:
            raise ValueError("optional capture temporarily unavailable")
        return api.capture

    reopened = api.create_dialog(
        board,
        original.specification,
        render=render,
        review_tracking=original.review_tracking,
    )
    api.finish_preparation(reopened)
    pane = reopened.preview_pane
    assert stub not in attempts
    _select(api, pane, pane.sections.index(stub))
    assert pane.failure
    assert attempts[stub] == 1
    healthy_index = next(
        index for index, section in enumerate(pane.sections) if section != stub
    )
    _select(api, pane, healthy_index)
    api.paint(pane)
    now[0] += 1.0
    _tick(api, reopened, reopened._capture_prepare_timer)
    assert attempts[stub] == 2
    assert stub in reopened._capture_hashes
    assert reopened.capture_digests[stub.section_id] is None
    assert not reopened._layer_drafts["F.Cu"].confirmed
    _select(api, pane, pane.sections.index(stub))
    assert attempts[stub] == 2
    api.paint(pane)
    assert pane.ready
    assert reopened._layer_drafts["F.Cu"].confirmed


def test_approve_button_cannot_bypass_failed_optional_image_after_navigation(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit approval still requires recovery from every known display failure."""
    api = constructor_api
    board, original = _approved_editor(api, covered_stub=True)
    reopened = api.create_dialog(
        board,
        original.specification,
        review_tracking=original.review_tracking,
    )
    api.finish_preparation(reopened)
    pane = reopened.preview_pane
    stub_index = next(
        index
        for index, section in enumerate(pane.sections)
        if section.exclusion_reason == api.model.EXCLUSION_COVERED_STUB
    )
    healthy_index = next(
        index for index in range(len(pane.sections)) if index != stub_index
    )
    good_set_capture = pane.canvas.set_capture

    def reject_stub(capture: object) -> None:
        """Keep only the explicitly selected optional preview unavailable."""
        if pane.selected_section == pane.sections[stub_index]:
            raise ValueError("optional image display still unavailable")
        good_set_capture(capture)

    with monkeypatch.context() as patch:
        patch.setattr(pane.canvas, "set_capture", reject_stub)
        pane.select(stub_index, refresh=True)
        api.drain()
        assert pane.failure
        assert not reopened._layer_drafts["F.Cu"].confirmed
        _select(api, pane, healthy_index)
        api.paint(pane)
        assert pane.ready
        assert not pane.failure
        reopened.approve_layer.emit(api.wx.EVT_BUTTON)
        assert not reopened._layer_drafts["F.Cu"].confirmed
        assert reopened.review_tracking.layers == original.review_tracking.layers
        assert reopened.validation_message.GetValue()
    _select(api, pane, stub_index)
    assert pane.capture_loaded
    assert not reopened._layer_drafts["F.Cu"].confirmed
    api.paint(pane)
    assert pane.ready
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking.layers == original.review_tracking.layers
