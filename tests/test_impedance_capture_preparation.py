"""Keep editor opening and speculative capture preparation separate from review.

These tests deliver show, timer, navigation and paint events independently using
the production constructor. They cover scheduling and review state, not native
KiCad rendering or the duration of a single GUI-thread capture.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from tests.test_impedance_capture_review_restore import (
    _approved_config,
    _approved_editor,
    _changed_capture,
)
from tests.test_impedance_dialog import constructor_api as _constructor_api
from tests.test_impedance_draft_dialog import draft_api as _draft_api
from tests.test_impedance_netclass_dialog import apply_layer, view_all_rows

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.model import BoardSnapshot, Section, Specification
    from impedance.review_tracking import ReviewTracking
    from impedance.service import CapturedImage

constructor_api = _constructor_api
draft_api = _draft_api


def _fixture(api: SimpleNamespace) -> tuple[BoardSnapshot, Specification]:
    """Provide three separate captures on each layer plus another selectable class."""
    board = api.model.BoardSnapshot(
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
            for net, offset in (("A", 0), ("B", 1_000_000), ("C", 2_000_000))
        )
        + (
            api.model.Trace(
                "other-class", "F.Cu", "D", 150_000, ((0, 0), (2_000_000, 0))
            ),
        ),
        net_classes=("RF", "Other"),
        net_class_memberships=(
            ("A", ("RF",)),
            ("B", ("RF",)),
            ("C", ("RF",)),
            ("D", ("Other",)),
        ),
        net_class_context_digest="capture-preparation-fixture",
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
    return board, spec


def _open(
    api: SimpleNamespace,
    board: BoardSnapshot,
    spec: Specification,
    render: Callable[..., CapturedImage],
    history: Optional[ReviewTracking] = None,
    **options: object,
) -> SpecificationDialog:
    """Construct an editor without pretending that construction is a show event."""
    kwargs = {} if history is None else {"review_tracking": history}
    return api.dialog.SpecificationDialog(
        None, board, spec, preview=render, **kwargs, **options
    )


def _show(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Deliver native show while leaving visible paint under test control."""
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
        )
    )
    api.drain()


def _tick(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Deliver one actual bound capture-timer event, preserving one-shot state."""
    timer = dialog._capture_prepare_timer
    assert timer.IsRunning(), "Capture preparation should have scheduled another tick"
    if timer.one_shot:
        timer.running = False
    dialog.bindings[api.wx.EVT_TIMER, timer](SimpleNamespace())
    api.drain()


def _finish(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Drain capture ticks only; never advance unrelated calculator timers."""
    for _attempt in range(30):
        if not dialog._capture_prepare_timer.IsRunning():
            return
        if dialog._capture_prepare_timer.interval_ms > 25:
            return  # Future retries require an advanced clock, not a busy loop.
        _tick(api, dialog)
    pytest.fail("Capture preparation did not quiesce after all fixture captures")


def _retry_preview(api: SimpleNamespace, pane: object) -> None:
    """Deliver the failed selected image's automatic retry timer."""
    timer = pane._retry_timer
    assert timer.IsRunning()
    timer.running = False
    pane.window.bindings[api.wx.EVT_TIMER, timer](SimpleNamespace())
    api.drain()


@pytest.mark.parametrize("history_kind", ["none", "partial", "approved"])
def test_constructor_never_renders_captures_or_restores_unverified_approval(
    constructor_api: SimpleNamespace, history_kind: str
) -> None:
    """Each opening returns before rendering, including a previously approved RF class."""
    api = constructor_api
    board, old = _approved_editor(api, both_layers=True)
    history = old.review_tracking
    if history_kind == "none":
        history = api.dialog.tracking.ReviewTracking()
    elif history_kind == "partial":
        history = replace(history, images=history.images[:1], layers=())
    fetched: list[Section] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section)
        return api.capture

    reopened = _open(api, board, old.specification, render, history)
    assert fetched == [], "The editor must appear before any capture is requested"
    assert not any(draft.confirmed for draft in reopened._layer_drafts.values())
    api.drain()
    assert fetched == [], "Queued callbacks must not render a hidden editor"
    assert reopened.review_tracking == history


def test_after_show_selected_capture_precedes_incremental_unviewed_preparation(
    constructor_api: SimpleNamespace,
) -> None:
    """Prepare included layers after the first paint without manufacturing review events."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[tuple[str, str]] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0]))
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    assert fetched == [("F.Cu", "A")]
    assert dialog.review_tracking.images == ()
    _tick(api, dialog)
    assert fetched == [("F.Cu", "A")], "Background work must wait for first paint"
    api.paint(dialog.preview_pane)
    _tick(api, dialog)
    assert fetched == [("F.Cu", "A"), ("F.Cu", "B")]
    _finish(api, dialog)
    assert Counter(fetched) == Counter(
        (layer, net) for layer in ("F.Cu", "B.Cu") for net in ("A", "B", "C")
    )
    assert len(dialog.review_tracking.images) == 1
    assert (
        dialog.review_tracking.images[0].section_id
        == dialog.preview_pane.sections[0].section_id
    )
    assert dialog.review_tracking.layers == ()
    assert not any(draft.confirmed for draft in dialog._layer_drafts.values())
    assert "needs review" in dialog.preview_pane.checklist.GetItemText(1, 1)
    assert "needs review" in dialog.preview_pane.checklist.GetItemText(2, 1)


def test_clicking_pending_capture_prioritizes_it_then_reuses_prepared_pixels(
    constructor_api: SimpleNamespace,
) -> None:
    """User navigation wins over the queue and a prepared row needs no second render."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[tuple[str, str]] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0]))
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    dialog.preview_pane.checklist.Select(2)
    dialog.preview_pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    api.drain()
    assert fetched == [("F.Cu", "A"), ("F.Cu", "C")]
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    counts = Counter(fetched)
    assert len(counts) == 6
    assert all(count == 1 for count in counts.values())
    dialog.preview_pane.checklist.Select(1)
    dialog.preview_pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    api.drain()
    api.paint(dialog.preview_pane)
    assert Counter(fetched) == counts
    assert dialog.preview_pane.ready


@pytest.mark.parametrize("cancel_after_paint", [False, True])
def test_cancel_discards_queued_and_in_progress_capture_preparation(
    constructor_api: SimpleNamespace, cancel_after_paint: bool
) -> None:
    """Cancellation stops scheduled work even when a final timer event is delivered."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    if cancel_after_paint:
        _show(api, dialog)
        api.paint(dialog.preview_pane)
    else:
        dialog.on_screen = True
        dialog.bindings[api.wx.EVT_SHOW, -1](
            SimpleNamespace(
                GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
            )
        )
    before_cancel = list(fetched)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)
    api.drain()
    assert not dialog._capture_prepare_timer.IsRunning()
    dialog.bindings[api.wx.EVT_TIMER, dialog._capture_prepare_timer](SimpleNamespace())
    api.drain()
    assert fetched == before_cancel
    assert dialog.specification is None


def test_context_change_discards_old_class_capture_queue(
    constructor_api: SimpleNamespace,
) -> None:
    """Changing class cannot finish rendering or approve captures from its old inputs."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    assert fetched == ["A"]
    dialog.net_class.SetSelection(dialog._net_class_names.index("Other") + 1)
    dialog.net_class.emit(api.wx.EVT_CHOICE)
    api.drain()
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    assert fetched == ["A", "D"]
    assert dialog.preview_pane.sections[0].net_names == ("D",)
    assert dialog.review_tracking.layers == ()


def test_kind_change_replaces_preparation_context_before_old_queue_runs(
    constructor_api: SimpleNamespace,
) -> None:
    """A changed capture type cannot keep rendering old requests or reuse their cache."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[tuple[int, str, str]] = []
    dialog: SpecificationDialog

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(
            (dialog.kind.GetSelection(), section.layer, section.net_names[0])
        )
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    assert fetched == [(0, "F.Cu", "A")]
    dialog.kind.SetSelection(2)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    api.drain()
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    assert fetched[0] == (0, "F.Cu", "A")
    assert Counter(fetched[1:]) == Counter(
        (2, layer, net) for layer in ("F.Cu", "B.Cu") for net in ("A", "B", "C")
    )
    assert dialog.review_tracking.layers == ()


@pytest.mark.parametrize("close_event", ["EVT_CLOSE", "EVT_WINDOW_DESTROY"])
def test_window_close_or_destroy_stops_preparation_and_ignores_late_timer(
    constructor_api: SimpleNamespace, close_event: str
) -> None:
    """Window-manager close and destruction receive the same cleanup as Cancel."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    dialog.bindings[getattr(api.wx, close_event), -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, GetId=dialog.GetId, Skip=lambda: None
        )
    )
    assert not dialog._capture_prepare_timer.IsRunning()
    dialog.bindings[api.wx.EVT_TIMER, dialog._capture_prepare_timer](SimpleNamespace())
    api.drain()
    assert fetched == ["A"]
    assert dialog.review_tracking.layers == ()
    assert dialog.specification is None


def test_partial_image_history_restores_only_matching_prior_view_after_preparation(
    constructor_api: SimpleNamespace,
) -> None:
    """A prepared capture gains historical view status only with matching saved evidence."""
    api = constructor_api
    board, old = _approved_editor(api)
    secondary = next(
        record
        for record in old.review_tracking.images
        if record.section_id != old.preview_pane.sections[0].section_id
    )
    history = replace(old.review_tracking, images=(secondary,), layers=())
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    reopened = _open(api, board, old.specification, render, history)
    assert fetched == []
    _show(api, reopened)
    api.paint(reopened.preview_pane)
    _finish(api, reopened)
    assert fetched == ["A", "B"]
    assert all(
        "viewed" in label
        for label in [
            reopened.preview_pane.checklist.GetItemText(index, 1)
            for index in range(reopened.preview_pane.checklist.GetItemCount())
        ]
    )
    assert secondary in reopened.review_tracking.images
    assert reopened.review_tracking.layers == ()
    assert not reopened._layer_drafts["F.Cu"].confirmed


@pytest.mark.parametrize("secondary", ["unchanged", "changed", "unavailable"])
def test_saved_approval_is_pending_until_all_current_captures_are_verified(
    constructor_api: SimpleNamespace, secondary: str
) -> None:
    """A later changed/failed capture blocks approval without mutating historical events."""
    api = constructor_api
    board, old = _approved_editor(api, both_layers=True)
    history = old.review_tracking
    changed = _changed_capture(api)
    fetched: list[tuple[str, str]] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0]))
        if section.layer == "B.Cu" and section.net_names == ("B",):
            if secondary == "unavailable":
                raise ValueError("Native capture unavailable")
            if secondary == "changed":
                return changed
        return api.capture

    reopened = _open(api, board, old.specification, render, history)
    assert fetched == []
    assert not any(draft.confirmed for draft in reopened._layer_drafts.values())
    _show(api, reopened)
    assert not reopened._layer_drafts["B.Cu"].confirmed
    api.paint(reopened.preview_pane)
    _finish(api, reopened)
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened._layer_drafts["B.Cu"].confirmed is (secondary == "unchanged")
    assert Counter(fetched) == Counter(
        (layer, net) for layer in ("F.Cu", "B.Cu") for net in ("A", "B")
    )
    assert reopened.review_tracking.layers == history.layers
    assert old.review_tracking == history


def test_restored_view_history_does_not_substitute_for_first_visible_paint(
    constructor_api: SimpleNamespace,
) -> None:
    """A matching historical view must not start prewarming before this window paints."""
    api = constructor_api
    board, old = _approved_editor(api, both_layers=True)
    fetched: list[tuple[str, str]] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0]))
        return api.capture

    reopened = _open(api, board, old.specification, render, old.review_tracking)
    assert fetched == []
    _show(api, reopened)
    assert fetched == [("F.Cu", "A")]
    _tick(api, reopened)
    assert fetched == [("F.Cu", "A")]
    assert reopened.review_tracking == old.review_tracking
    api.paint(reopened.preview_pane)
    _finish(api, reopened)
    assert len(fetched) == 4


@pytest.mark.parametrize("during", ["selected", "background"])
@pytest.mark.parametrize("change", ["class", "kind", "include"])
def test_reentrant_input_change_preserves_new_draft_and_discards_old_capture(
    constructor_api: SimpleNamespace, during: str, change: str
) -> None:
    """A native render can deliver edits before returning; old state must stay obsolete."""
    api = constructor_api
    board, spec = _fixture(api)
    state = SimpleNamespace(armed=True, expected_drafts=None, returned_section=None)
    dialog: SpecificationDialog

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        trigger = "A" if during == "selected" else "B"
        if state.armed and section.layer == "B.Cu" and section.net_names == (trigger,):
            state.armed = False
            state.returned_section = section
            if change == "class":
                dialog.net_class.SetSelection(
                    dialog._net_class_names.index("Other") + 1
                )
                dialog.net_class.emit(api.wx.EVT_CHOICE)
            elif change == "kind":
                dialog.kind.SetSelection(2)
                dialog.kind.emit(api.wx.EVT_CHOICE)
                dialog.ground_gap.SetValue("0.27")
            else:
                dialog.include_layer.SetValue(False)
                dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
            state.expected_drafts = dict(dialog._layer_drafts)
        return api.capture

    dialog = _open(api, board, spec, render)
    dialog.layer.SetSelection(dialog._layer_names.index("B.Cu"))
    dialog.layer.emit(api.wx.EVT_CHOICE)
    api.drain()
    _show(api, dialog)
    if during == "background":
        api.paint(dialog.preview_pane)
        _tick(api, dialog)
    assert not state.armed
    assert dialog._layer_drafts == state.expected_drafts
    if change == "class":
        assert dialog._active_class == "Other"
        assert dialog._active_layer == "F.Cu"
        assert set(dialog._layer_drafts) == {"F.Cu"}
    elif change == "kind":
        assert dialog.kind.GetSelection() == 2
        assert dialog._layer_drafts["B.Cu"].ground_gap == "0.27"
    else:
        assert not dialog._layer_drafts["B.Cu"].included
        assert state.returned_section not in dialog._capture_hashes
    assert dialog.review_tracking.layers == ()


def test_background_failure_after_reference_edit_keeps_latest_same_context_draft(
    constructor_api: SimpleNamespace,
) -> None:
    """Failure handling must not overwrite edits even if image geometry stays identical."""
    api = constructor_api
    board, spec = _fixture(api)
    state = SimpleNamespace(expected_draft=None)
    dialog: SpecificationDialog

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        if section.layer == "B.Cu" and section.net_names == ("B",):
            dialog.references.Check(0, True)
            dialog.references.emit(api.wx.EVT_CHECKLISTBOX)
            state.expected_draft = dialog._layer_drafts["B.Cu"]
            raise ValueError("Native renderer failed after a nested input event")
        return api.capture

    dialog = _open(api, board, spec, render)
    dialog.layer.SetSelection(dialog._layer_names.index("B.Cu"))
    dialog.layer.emit(api.wx.EVT_CHOICE)
    api.drain()
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    _tick(api, dialog)
    assert state.expected_draft is not None
    assert state.expected_draft.references == ("F.Cu", "In1.Cu")
    assert dialog._layer_drafts["B.Cu"] == state.expected_draft


@pytest.mark.parametrize("during", ["selected", "background"])
def test_renderer_destroy_discards_pixels_without_reporting_to_dead_window(
    constructor_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, during: str
) -> None:
    """The native wrapper can disappear before a nested Destroy event is delivered."""
    api = constructor_api
    board, spec = _fixture(api)
    state = SimpleNamespace(returned_section=None)
    reported: list[str] = []
    dialog: SpecificationDialog

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        trigger = "A" if during == "selected" else "B"
        if section.layer == "F.Cu" and section.net_names == (trigger,):
            state.returned_section = section
            dialog.Destroy()
        return api.capture

    dialog = _open(api, board, spec, render)
    monkeypatch.setattr(dialog.preview_pane, "_report_failure", reported.append)
    _show(api, dialog)
    if during == "background":
        api.paint(dialog.preview_pane)
        _tick(api, dialog)
    assert not dialog
    assert state.returned_section is not None
    assert state.returned_section not in dialog._capture_hashes
    assert reported == []
    assert dialog.review_tracking.layers == ()
    assert len(dialog.review_tracking.images) == (during == "background")


def test_parent_preserves_approval_when_historically_reviewed_stub_is_now_optional(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping an optional saved image must not turn absent verification into failure."""
    api = draft_api
    board, old = _approved_editor(api, covered_stub=True)
    inspected = _open(
        api,
        board,
        old.specification,
        api.preview,
        old.review_tracking,
        skip_stub_review=lambda: False,
    )
    _show(api, inspected)
    api.paint(inspected.preview_pane)
    _finish(api, inspected)
    view_all_rows(api, inspected)
    apply_layer(api, inspected)
    inspected.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert len(inspected.review_tracking.layers[0].captures) == 2
    config = replace(
        _approved_config(api, board, inspected),
        reviewed_digest="",
        included_section_ids=(),
    )
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    parent = api.dialog.ImpedanceDialog(None, config, board, render, lambda: board)
    parent.on_screen = True

    def apply_after_required_verification(child: SpecificationDialog) -> int:
        """Use the real editor to verify its required capture and apply unchanged intent."""
        _show(api, child)
        api.paint(child.preview_pane)
        _finish(api, child)
        assert child._layer_drafts["F.Cu"].confirmed
        assert fetched == ["B"]
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog,
        "ShowModal",
        apply_after_required_verification,
        raising=False,
    )
    parent.specifications.SelectRow(0)
    parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    assert parent.session.approved
    assert parent.session.config.review_tracking.layers == config.review_tracking.layers
    assert fetched == ["B"]


def test_native_decode_failure_remains_failed_after_applying_child_to_parent(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid PNG hash cannot authorize pixels that the native preview could not install."""
    api = draft_api
    board, old = _approved_editor(api, both_layers=True)
    config = replace(
        _approved_config(api, board, old),
        reviewed_digest="",
        included_section_ids=(),
    )
    parent = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    parent.on_screen = True

    def fail_decode(_capture: CapturedImage) -> None:
        raise ValueError("Native decoder cannot install pixels")

    def apply_with_failed_preview(child: SpecificationDialog) -> int:
        """Restore current approval, then fail a real selected-image installation."""
        _show(api, child)
        api.paint(child.preview_pane)
        _finish(api, child)
        assert all(draft.confirmed for draft in child._layer_drafts.values())
        monkeypatch.setattr(child.preview_pane.canvas, "set_capture", fail_decode)
        child.preview_pane.checklist.Select(1)
        child.preview_pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
        api.drain()
        assert child.preview_pane.failure
        assert not child._layer_drafts["F.Cu"].confirmed
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog,
        "ShowModal",
        apply_with_failed_preview,
        raising=False,
    )
    parent.specifications.SelectRow(0)
    parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    api.drain()
    assert not parent.session.approved
    assert not parent.session.approve_if_layer_reviews_complete()


@pytest.mark.parametrize("changed_context", ["board", "theme"])
def test_prepared_pixels_still_require_current_board_and_theme_on_selection(
    constructor_api: SimpleNamespace, changed_context: str
) -> None:
    """A cache hit must not let current-context checks disappear from navigation."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[str] = []
    verified: list[BoardSnapshot] = []
    state = SimpleNamespace(board_changed=False, theme="original")

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    def verify(snapshot: BoardSnapshot) -> None:
        verified.append(snapshot)
        if state.board_changed:
            raise ValueError("Board changed; reopen the specification")

    dialog = _open(
        api,
        board,
        spec,
        render,
        verify_snapshot=verify,
        appearance_context=lambda: state.theme,
    )
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    before = len(fetched)
    verified.clear()
    state.board_changed = changed_context == "board"
    state.theme = "changed" if changed_context == "theme" else "original"
    dialog.preview_pane.checklist.Select(1)
    dialog.preview_pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    api.drain()
    api.paint(dialog.preview_pane)
    assert not dialog.preview_pane.ready
    assert not any(draft.confirmed for draft in dialog._layer_drafts.values())
    assert len(fetched) == before
    if changed_context == "board":
        # The editor rejects its cached capture, then the shared preview checks
        # the independent source guard before classifying a display failure.
        assert verified == [board, board]
    else:
        assert "appearance changed" in dialog.preview_pane.failure.lower()
        _retry_preview(api, dialog.preview_pane)
        api.drain()
        api.paint(dialog.preview_pane)
        assert len(fetched) == before + 1
        assert dialog.preview_pane.ready


def test_preparation_omits_excluded_layers(
    constructor_api: SimpleNamespace,
) -> None:
    """Excluded routing remains inspectable without eagerly rendering its captures."""
    api = constructor_api
    board, spec = _fixture(api)
    spec = replace(
        spec, layer_settings=spec.layer_settings[:1], excluded_layers=("B.Cu",)
    )
    fetched: list[tuple[str, str]] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0]))
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    assert fetched == [("F.Cu", net) for net in ("A", "B", "C")]


@pytest.mark.parametrize("excluded_on_open", [False, True])
def test_excluded_active_layer_still_prepares_remaining_included_layers(
    constructor_api: SimpleNamespace, excluded_on_open: bool
) -> None:
    """A blank excluded-layer preview must not prevent warming included routing."""
    api = constructor_api
    board, spec = _fixture(api)
    if excluded_on_open:
        spec = replace(
            spec,
            layer_settings=spec.layer_settings[1:],
            excluded_layers=("F.Cu",),
        )
    fetched: list[tuple[str, str]] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0]))
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    assert dialog._active_layer == "F.Cu"
    if not excluded_on_open:
        api.paint(dialog.preview_pane)
        assert fetched == [("F.Cu", "A")]
        dialog.include_layer.SetValue(False)
        dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
        api.drain()
    assert dialog.preview_pane.sections == ()
    assert not dialog._layer_drafts["F.Cu"].included
    _finish(api, dialog)
    expected = [] if excluded_on_open else [("F.Cu", "A")]
    assert fetched == expected + [("B.Cu", net) for net in ("A", "B", "C")]
    assert not dialog._capture_prepare_timer.IsRunning()
    assert dialog.review_tracking.layers == ()


@pytest.mark.parametrize("skip_stubs", [False, True])
def test_preparation_respects_optional_covered_stub_setting(
    constructor_api: SimpleNamespace, skip_stubs: bool
) -> None:
    """Only stubs that require review join speculative rendering."""
    api = constructor_api
    board, old = _approved_editor(api, covered_stub=True)
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    dialog = _open(
        api,
        board,
        old.specification,
        render,
        skip_stub_review=lambda: skip_stubs,
    )
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    assert Counter(fetched) == Counter(["B"] if skip_stubs else ["A", "B"])
    assert len(dialog.review_tracking.images) == 1
    assert dialog.review_tracking.layers == ()


def test_all_optional_layer_restores_explicit_empty_capture_approval(
    constructor_api: SimpleNamespace,
) -> None:
    """An approved layer can legitimately have no required images under stub skipping."""
    api = constructor_api
    board, spec = _fixture(api)
    board = replace(
        board,
        copper_features=tuple(
            api.model.CopperFeature(
                f"pad-{net}",
                net,
                "F.Cu",
                "pad",
                "rect",
                (1_000_000, offset),
                3_000_000,
                500_000,
            )
            for net, offset in (("A", 0), ("B", 1_000_000), ("C", 2_000_000))
        ),
    )
    spec = replace(
        spec, layer_settings=spec.layer_settings[:1], excluded_layers=("B.Cu",)
    )
    original = _open(api, board, spec, api.preview)
    _show(api, original)
    api.paint(original.preview_pane)
    _finish(api, original)
    assert all(section.exclusion_reason for section in original.preview_pane.sections)
    original.approve_layer.emit(api.wx.EVT_BUTTON)
    assert original._layer_drafts["F.Cu"].confirmed
    assert len(original.review_tracking.layers[0].captures) == 1
    # Older versions approved an all-optional layer with no capture entries.
    original.review_tracking = replace(
        original.review_tracking,
        layers=(replace(original.review_tracking.layers[0], captures=()),),
    )
    original.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert original.modal_result == api.wx.ID_OK
    history = original.review_tracking
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        return api.capture

    reopened = _open(api, board, original.specification, render, history)
    assert fetched == []
    assert not reopened._layer_drafts["F.Cu"].confirmed
    _show(api, reopened)
    api.paint(reopened.preview_pane)
    _finish(api, reopened)
    assert fetched == ["A"]
    assert reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking.layers == history.layers


def test_all_optional_restoration_keeps_reference_edit_delivered_during_verification(
    constructor_api: SimpleNamespace,
) -> None:
    """Verification callbacks must finish before restoration reads editable settings."""
    api = constructor_api
    board, spec = _fixture(api)
    board = replace(
        board,
        copper_features=tuple(
            api.model.CopperFeature(
                f"pad-{net}",
                net,
                "F.Cu",
                "pad",
                "rect",
                (1_000_000, offset),
                3_000_000,
                500_000,
            )
            for net, offset in (("A", 0), ("B", 1_000_000), ("C", 2_000_000))
        ),
    )
    spec = replace(
        spec, layer_settings=spec.layer_settings[:1], excluded_layers=("B.Cu",)
    )
    original = _open(api, board, spec, api.preview)
    _show(api, original)
    api.paint(original.preview_pane)
    _finish(api, original)
    original.approve_layer.emit(api.wx.EVT_BUTTON)
    original.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    verified: list[BoardSnapshot] = []
    reopened: SpecificationDialog

    def verify(snapshot: BoardSnapshot) -> None:
        verified.append(snapshot)
        if len(verified) == 2:
            reopened.references.Check(1, False)
            reopened.references.Check(2, True)
            reopened.references.emit(api.wx.EVT_CHECKLISTBOX)
            assert reopened._layer_drafts["F.Cu"].references == ("B.Cu",)

    reopened = _open(
        api,
        board,
        spec,
        api.preview,
        original.review_tracking,
        verify_snapshot=verify,
    )
    _show(api, reopened)
    assert len(verified) >= 2
    assert reopened._layer_drafts["F.Cu"].references == ("B.Cu",)
    assert not reopened._layer_drafts["F.Cu"].confirmed


@pytest.mark.parametrize("retry_action", ["select", "refresh"])
def test_failed_background_capture_retries_through_user_navigation_or_refresh(
    constructor_api: SimpleNamespace, retry_action: str
) -> None:
    """A failed speculative request is retryable and never grants review by itself."""
    api = constructor_api
    board, spec = _fixture(api)
    state = SimpleNamespace(failing=True)
    fetched: list[tuple[str, str, bool]] = []

    def render(section: Section, *, refresh: bool = False) -> CapturedImage:
        fetched.append((section.layer, section.net_names[0], refresh))
        if section.layer == "F.Cu" and section.net_names == ("B",) and state.failing:
            raise ValueError("Capture temporarily unavailable")
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    assert len(fetched) == 6
    assert dialog.review_tracking.layers == ()
    assert "needs review" in dialog.preview_pane.checklist.GetItemText(1, 1)
    if retry_action == "select":
        state.failing = False
    dialog.preview_pane.checklist.Select(1)
    dialog.preview_pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
    api.drain()
    if retry_action == "refresh":
        assert dialog.preview_pane.failure
        state.failing = False
        _retry_preview(api, dialog.preview_pane)
        api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready
    assert fetched[-1] == ("F.Cu", "B", retry_action == "refresh")
    assert dialog.review_tracking.layers == ()
    assert not dialog._layer_drafts["F.Cu"].confirmed


def test_apply_before_verification_cannot_autoapprove_parent_from_old_history(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual parent merge must carry unverified evidence across early Apply."""
    api = draft_api
    board, old = _approved_editor(api, both_layers=True)
    config = replace(
        _approved_config(api, board, old), reviewed_digest="", included_section_ids=()
    )
    fetched: list[Section] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section)
        return api.capture

    parent = api.dialog.ImpedanceDialog(None, config, board, render, lambda: board)
    assert not parent.session.approved
    assert fetched == []

    def apply_immediately(child: SpecificationDialog) -> int:
        """Drive real show and Apply handlers before the event queue can render."""
        child.on_screen = True
        child.bindings[api.wx.EVT_SHOW, -1](
            SimpleNamespace(
                GetEventObject=lambda: child, IsShown=lambda: True, Skip=lambda: None
            )
        )
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", apply_immediately, raising=False
    )
    parent.specifications.SelectRow(0)
    parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    api.drain()
    assert fetched == []
    assert not parent.session.approved
    assert not parent.session.approve_if_layer_reviews_complete()
    assert parent.session.config.review_tracking == config.review_tracking
    assert parent.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"


@pytest.mark.parametrize("cancel_during", ["selected", "background"])
def test_cancel_delivered_during_renderer_discards_returned_capture_and_future_work(
    constructor_api: SimpleNamespace, cancel_during: str
) -> None:
    """Nested native events may close the dialog before its renderer returns pixels."""
    api = constructor_api
    board, spec = _fixture(api)
    fetched: list[str] = []
    dialog: SpecificationDialog

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        net = section.net_names[0]
        fetched.append(net)
        if net == ("A" if cancel_during == "selected" else "B"):
            dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)
        return api.capture

    dialog = _open(api, board, spec, render)
    assert fetched == []
    _show(api, dialog)
    api.paint(dialog.preview_pane)
    _finish(api, dialog)
    assert fetched == (["A"] if cancel_during == "selected" else ["A", "B"])
    assert not dialog._capture_prepare_timer.IsRunning()
    assert dialog.review_tracking.layers == ()
    assert len(dialog.review_tracking.images) == (cancel_during == "background")
    assert dialog.specification is None
