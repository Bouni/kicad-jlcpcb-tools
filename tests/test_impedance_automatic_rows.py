"""Automatically discover workbook rows without implicitly approving them.

These regressions exercise the production session, constructor, and bound handlers
with explicit stateful wx visibility and CallAfter dispatch; they do not assert
native layout or rendering.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from .test_impedance_draft_dialog import (
    _approved,
    _board,
    _intent,
    _open,
    _stackup,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)

# Explicitly register the shared fixture without an unused import shadowed by
# pytest's same-named injection parameters.
draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import Config, Specification
    from impedance.service import CapturedImage


@pytest.fixture(autouse=True)
def _no_catalog_workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep workbook workflow cases independent of the catalog's network worker."""
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )


def _show(api: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Deliver first native show before dispatching deferred preview callbacks."""
    dialog.on_screen = True
    skipped: list[bool] = []
    dialog.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog,
            IsShown=lambda: True,
            Skip=lambda: skipped.append(True),
        )
    )
    assert skipped == [True]
    api.drain()
    api.paint(dialog.preview_pane)


def _accepted_specification(
    api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    specification: Specification,
) -> None:
    """Accept an immutable child result while retaining its real parent handlers."""

    class AcceptedSpecification:
        def __init__(
            self, parent: ImpedanceDialog, *_args: object, **_kwargs: object
        ) -> None:
            self.specification = specification
            self.review_tracking = parent.session.config.review_tracking

        def ShowModal(self) -> int:
            return api.wx.ID_OK

        def Destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(api.dialog, "SpecificationDialog", AcceptedSpecification)


def test_refresh_restores_saved_checks_and_approval_without_new_review(
    draft_api: SimpleNamespace,
) -> None:
    """Opening an unchanged approved board must not reset its exclusions."""
    api = draft_api
    board = _board(api)
    session = api.dialog.ReviewSession(_intent(api), board)
    first = session.refresh()
    wanted = first.sections[-1].section_id
    session.set_included((wanted,))
    session.approve()
    saved = session.save_result()
    reopened = api.dialog.ReviewSession(
        api.model.Config.from_dict(saved.to_dict()), board
    )

    restored = reopened.refresh()

    assert restored.digest == first.digest
    assert reopened.included == {wanted}
    assert reopened.approved is True
    assert reopened.save_result() == saved
    reopened.refresh()
    assert reopened.approved is True
    assert reopened.save_result() == saved


def test_refresh_preserves_unapproved_choices_but_changed_board_revokes_approval(
    draft_api: SimpleNamespace,
) -> None:
    """Harmless updates retain draft checks; changed capture inputs require approval."""
    api = draft_api
    board = _board(api)
    session = api.dialog.ReviewSession(_intent(api), board)
    first = session.refresh()
    wanted = first.sections[-1].section_id
    session.set_included((wanted,))
    session.refresh()
    assert session.included == {wanted}
    assert session.approved is False
    assert session.config.reviewed_digest == ""
    session.approve()
    changed = replace(board, context_digest="moved reference text")
    current = session.refresh(changed)
    assert current.digest != first.digest
    assert session.snapshot == changed
    assert session.approved is False
    assert session.config.reviewed_digest == ""


def test_opening_populates_rows_and_offers_counted_approval_without_scan_button(
    draft_api: SimpleNamespace,
) -> None:
    """A populated class gets actionable workbook rows on first construction."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.session.analysis is not None
    assert len(dialog.sections.items) == 2
    assert dialog.sections.GetSelection() == 0
    assert not hasattr(dialog, "scan_button")
    assert dialog.retry_button.IsShown() is False
    assert dialog.approve_button.GetLabel() == "Approve 2 workbook rows"
    assert "needs approval" in dialog.review_status.GetLabel().lower()
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert api.preview_calls == []
    assert dialog.approve_button.enabled is False

    _show(api, dialog)
    assert dialog.preview_pane.ready is True
    assert dialog.approve_button.enabled is True

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is True
    assert "2 workbook rows approved" in dialog.review_status.GetLabel().lower()
    red, green, blue = dialog.review_status.foreground[:3]
    assert green > red and green > blue


def test_save_close_and_reopen_restores_visible_rows_checks_and_approval(
    draft_api: SimpleNamespace,
) -> None:
    """The user's former empty-pane reproduction survives actual Save and reopen."""
    api = draft_api
    board = _board(api)
    saved: list[Config] = []
    dialog = _open(api, _intent(api), board, saved.append)
    dialog.sections.Check(0, False)
    dialog.sections.emit(api.wx.EVT_CHECKLISTBOX)
    included = set(dialog.session.included)
    _show(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert len(saved) == 1

    reopened = _open(api, api.model.Config.from_dict(saved[0].to_dict()), board)

    assert len(reopened.sections.items) == 2
    assert reopened.session.included == included
    assert reopened.session.approved is True
    assert reopened.sections.IsChecked(0) is False
    assert reopened.sections.IsChecked(1) is True
    assert "1 workbook row approved" in reopened.review_status.GetLabel().lower()


def test_added_specification_refreshes_and_selects_its_first_row(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting a new class shows its rows rather than the previous class's row."""
    api = draft_api
    board = _board(api)
    added = replace(
        _intent(api).specifications[0],
        spec_id="ground",
        label="Return",
        net_class="Default",
        layer_settings=(api.model.LayerSettings("B.Cu", ("F.Cu",)),),
    )
    dialog = _open(api, _intent(api), board)
    _accepted_specification(api, monkeypatch, added)

    dialog._on_add(SimpleNamespace())

    assert len(dialog.sections.items) == 3
    current = dialog.session.analysis.sections[dialog.sections.GetSelection()]
    assert current.spec_id == "ground"
    assert dialog.session.approved is False
    assert dialog.approve_button.GetLabel() == "Approve 3 workbook rows"


def test_accepted_no_op_edit_preserves_approval_and_selected_row(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening and accepting unchanged specification fields is not a reset action."""
    api = draft_api
    board = _board(api)
    approved = _approved(api, board)
    dialog = _open(api, approved, board)
    dialog.sections.SetSelection(1)
    selected = dialog.session.analysis.sections[1].section_id
    dialog.specifications.selection = 0
    _accepted_specification(api, monkeypatch, approved.specifications[0])

    dialog.specifications.emit(api.wx.EVT_LIST_ITEM_ACTIVATED)

    assert dialog.session.approved is True
    assert dialog.session.config.reviewed_digest == approved.reviewed_digest
    assert (
        dialog.session.analysis.sections[dialog.sections.GetSelection()].section_id
        == selected
    )


def test_changed_specification_rebuilds_rows_and_invalidates_old_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting changed target impedance updates current rows without a second action."""
    api = draft_api
    board = _board(api)
    approved = _approved(api, board)
    dialog = _open(api, approved, board)
    dialog.specifications.selection = 0
    changed = replace(approved.specifications[0], target_ohms="55")
    _accepted_specification(api, monkeypatch, changed)

    dialog.specifications.emit(api.wx.EVT_LIST_ITEM_ACTIVATED)

    assert dialog.session.config.specifications == (changed,)
    assert len(dialog.sections.items) == 2
    assert dialog.session.analysis.digest != approved.reviewed_digest
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""


def test_harmless_row_population_preserves_selection_and_unchecked_rows(
    draft_api: SimpleNamespace,
) -> None:
    """Status refreshes must not jump the preview back to the first candidate."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.sections.SetSelection(1)
    selected = dialog.session.analysis.sections[1].section_id
    dialog.sections.Check(0, False)
    dialog.sections.emit(api.wx.EVT_CHECKLISTBOX)

    dialog._populate_sections()

    assert (
        dialog.session.analysis.sections[dialog.sections.GetSelection()].section_id
        == selected
    )
    assert dialog.sections.IsChecked(0) is False
    assert dialog.sections.IsChecked(1) is True
    assert dialog.approve_button.GetLabel() == "Approve 1 workbook row"


def test_stackup_acceptance_repopulates_rows_and_requires_new_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted construction change cannot leave the formerly populated pane empty."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _approved(api, board), board)
    _show(api, dialog)
    selected = _stackup(api)

    class AcceptedPicker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.stackup = selected

        def ShowModal(self) -> int:
            return api.wx.ID_OK

        def Destroy(self) -> None:
            self.destroyed = True

    module_name = api.dialog.__package__ + ".stackup_dialog"
    picker_module = ModuleType(module_name)
    picker_module.StackupDialog = AcceptedPicker
    monkeypatch.setitem(sys.modules, module_name, picker_module)
    monkeypatch.setattr(dialog, "_ensure_catalog_started", lambda _count: object())

    dialog.stackup_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.config.stackup == selected
    assert len(dialog.sections.items) == 2
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.approve_button.enabled is True
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True


def test_complete_calculation_refreshes_rows_without_approving_new_results(
    draft_api: SimpleNamespace,
) -> None:
    """Accept a complete immutable worker result through its actual completion handler."""
    api = draft_api
    board = _board(api)
    original = replace(_intent(api), stackup=_stackup(api))
    dialog = _open(api, original, board)
    _show(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    config = dialog.session.config
    result = model.WidthResult(
        spec_id="rf",
        layer="F.Cu",
        input_digest=model.calculation_fingerprint(
            config.stackup, config.specifications[0], "F.Cu"
        ),
        status="unsupported",
        message="Construction is unavailable to the nominal-width solver.",
    )
    key = dialog._calculation_key(config, board)
    dialog._calculation_cancel = Event()
    generation = dialog._calculation_generation

    dialog._finish_calculation(generation, key, (result,), "")

    assert dialog._calculation_cancel is None
    assert dialog.session.config.width_results == (result,)
    assert len(dialog.sections.items) == 2
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.approve_button.enabled is True
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True


def test_remove_repopulates_rows_without_leaving_stale_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Removing the final specification empties the rows intentionally and safely."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.specifications.selection = 0

    dialog._on_remove(SimpleNamespace())

    assert dialog.session.config.specifications == ()
    assert dialog.sections.items == []
    assert dialog.preview_pane.sections == ()
    assert dialog.preview_pane.canvas._image is None
    assert dialog.preview_pane.canvas._bitmap is None
    assert dialog.approve_button.enabled is False
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "specification" in dialog.status.GetValue().lower()


def test_approval_after_board_change_refreshes_but_requires_second_explicit_click(
    draft_api: SimpleNamespace,
) -> None:
    """The same click must never authorize rows the user has not yet seen."""
    api = draft_api
    board = _board(api)
    changed = replace(board, context_digest="new footprint location")
    dialog = _open(api, _intent(api), board)
    _show(api, dialog)
    old_digest = dialog.session.analysis.digest
    dialog.refresh_snapshot = lambda: changed

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.analysis.digest != old_digest
    assert dialog.session.snapshot == changed
    assert len(dialog.sections.items) == 2
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert "review" in dialog.status.GetValue().lower()
    assert dialog.approve_button.enabled is False
    api.drain()
    api.paint(dialog.preview_pane)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True


def test_approval_rejects_unreadable_live_snapshot(
    draft_api: SimpleNamespace,
) -> None:
    """Previously displayed pixels cannot approve an unreadable current board."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)

    def fail_verification() -> None:
        raise ValueError("The native board capture context is unavailable.")

    dialog.refresh_snapshot = fail_verification
    assert dialog.approve_button.enabled is True

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.sections.items == []
    assert dialog.preview_pane.canvas._image is None
    assert dialog.retry_button.IsShown() is True
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "native board capture context" in dialog.status.GetValue()


def test_population_failure_clears_stale_rows_and_retry_recovers_without_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed current scan never leaves old candidates looking ready to approve."""
    api = draft_api
    board = _board(api)
    dialog = _open(api, _approved(api, board), board)
    original_analyze = api.dialog.analyze

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError("Could not read current net classes.")

    monkeypatch.setattr(api.dialog, "analyze", fail)
    assert dialog._refresh_rows() is False
    assert dialog.session.analysis is None
    assert dialog.sections.items == []
    assert dialog.session.approved is False
    assert dialog.preview_pane.canvas._image is None
    assert dialog.preview_pane.canvas._bitmap is None
    assert dialog.retry_button.IsShown() is True
    assert dialog.approve_button.enabled is False
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True
    assert "Could not read current net classes" in dialog.status.GetValue()

    monkeypatch.setattr(api.dialog, "analyze", original_analyze)
    dialog.retry_button.emit(api.wx.EVT_BUTTON)

    assert len(dialog.sections.items) == 2
    assert dialog.retry_button.IsShown() is False
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""


def test_initial_population_failure_explains_empty_pane_and_can_retry(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor-time analysis exception leaves an editable dialog, not a crash."""
    api = draft_api
    original_analyze = api.dialog.analyze

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError("The board net-class context could not be loaded.")

    monkeypatch.setattr(api.dialog, "analyze", fail)
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.sections.items == []
    assert dialog.retry_button.IsShown() is True
    assert "net-class context" in dialog.status.GetValue()
    assert dialog.FindWindow(api.wx.ID_OK).enabled is True

    monkeypatch.setattr(api.dialog, "analyze", original_analyze)
    dialog.retry_button.emit(api.wx.EVT_BUTTON)

    assert len(dialog.sections.items) == 2
    assert dialog.retry_button.IsShown() is False
    assert dialog.session.approved is False


def test_preview_waits_for_show_and_ignores_closed_dialog_callbacks(
    draft_api: SimpleNamespace,
) -> None:
    """Auto-selection may render only after native visibility, never after Cancel."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    api.drain()
    assert api.preview_calls == []
    assert dialog.session.config.review_tracking.images == ()

    _show(api, dialog)

    assert api.preview_calls == [dialog.session.analysis.sections[0]]
    assert dialog.preview_pane.ready is True
    dialog.sections.SetSelection(1)
    dialog.sections.emit(api.wx.EVT_LISTBOX)
    dialog.bindings[api.wx.EVT_BUTTON, api.wx.ID_CANCEL](
        SimpleNamespace(Skip=lambda: None)
    )
    api.drain()
    api.paint(dialog.preview_pane)
    assert len(api.preview_calls) == 1
    assert len(dialog.session.config.review_tracking.images) == 1


def test_queued_preview_for_obsolete_selection_is_ignored(
    draft_api: SimpleNamespace,
) -> None:
    """Only the newest native selection may render when CallAfter work is delivered."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.on_screen = True
    dialog._queue_selected_preview()
    dialog.sections.SetSelection(1)
    dialog.sections.emit(api.wx.EVT_LISTBOX)

    api.drain()
    api.paint(dialog.preview_pane)

    assert api.preview_calls == [dialog.session.analysis.sections[1]]
    assert dialog.preview_pane.ready is True
    viewed = dialog.session.config.review_tracking.images
    assert len(viewed) == 1
    assert viewed[0].section_id == dialog.session.analysis.sections[1].section_id


def test_show_event_before_native_visibility_still_schedules_first_preview(
    draft_api: SimpleNamespace,
) -> None:
    """Some native platforms deliver EVT_SHOW before IsShownOnScreen becomes true."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    assert dialog.IsShownOnScreen() is False

    dialog.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
        )
    )

    assert api.preview_calls == []
    dialog.on_screen = True
    api.drain()
    assert api.preview_calls == [dialog.session.analysis.sections[0]]
    assert dialog.preview_pane.ready is False
    assert dialog.session.config.review_tracking.images == ()
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready is True


def test_deferred_parent_preview_waits_for_a_child_dialog_to_close(
    draft_api: SimpleNamespace,
) -> None:
    """A queued parent image is not a viewed capture behind an active child modal."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.on_screen = True
    dialog._queue_selected_preview()
    dialog._child_dialog_open = True
    api.drain()
    api.paint(dialog.preview_pane)
    assert api.preview_calls == []
    assert dialog.session.config.review_tracking.images == ()
    dialog._child_dialog_open = False
    dialog._queue_selected_preview()
    api.drain()
    api.paint(dialog.preview_pane)
    assert api.preview_calls == [
        dialog.session.analysis.sections[dialog.sections.GetSelection()]
    ]
    assert dialog.preview_pane.ready is True


def test_failed_capture_and_pending_retry_cannot_be_bypassed_by_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh row analysis is not evidence that its failed image now renders."""
    api = draft_api
    failing = True
    attempts: list[bool] = []
    original_preview = api.preview

    def capture(section: object, *, refresh: bool = False) -> CapturedImage:
        attempts.append(refresh)
        if failing:
            raise ValueError("Native capture failed.")
        return original_preview(section, refresh=refresh)

    monkeypatch.setattr(api, "preview", capture)
    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)
    assert dialog.preview_pane.canvas._image is None
    assert dialog.approve_button.enabled is False
    assert dialog.retry_button.IsShown() is True

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.retry_button.IsShown() is True
    failing = False
    dialog.retry_button.emit(api.wx.EVT_BUTTON)
    assert dialog.preview_pane.ready is False
    assert dialog.approve_button.enabled is False

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert api.preview_calls == []
    api.drain()
    assert len(api.preview_calls) == 1
    assert attempts[-1] is True
    assert not any(attempts[:-1])
    assert dialog.preview_pane.canvas._image is not None
    assert dialog.preview_pane.ready is False
    assert dialog.approve_button.enabled is False
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready is True
    assert dialog.approve_button.enabled is True
    assert dialog.retry_button.IsShown() is False
    assert dialog.session.approved is False
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True


def test_parent_view_timestamp_is_not_recorded_behind_a_child_modal(
    draft_api: SimpleNamespace,
) -> None:
    """A late paint notification is not evidence of viewing an obscured parent."""
    api = draft_api
    dialog = _open(api, _intent(api), _board(api))
    dialog.on_screen = True
    dialog._queue_selected_preview()
    api.drain()
    assert dialog.preview_pane.canvas._image is not None
    before = dialog.session.config.review_tracking
    dialog._child_dialog_open = True
    api.paint(dialog.preview_pane)
    assert dialog.session.config.review_tracking == before
    assert dialog.preview_pane.ready is False
    dialog._child_dialog_open = False
    api.paint(dialog.preview_pane)
    assert len(dialog.session.config.review_tracking.images) == 1
    assert dialog.preview_pane.ready is True


def test_harmless_refresh_and_approval_reuse_successfully_rendered_preview(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real capture, canvas paint, and source-checked view notification."""
    api = draft_api

    def unexpected_timestamp() -> str:
        pytest.fail(
            "Unchanged row refresh must not manufacture another image-view event"
        )

    dialog = _open(api, _intent(api), _board(api))
    _show(api, dialog)
    assert len(api.preview_calls) == 1
    pane = dialog.preview_pane
    image = pane.canvas._image
    bitmap = pane.canvas._bitmap
    assert image is not None
    assert bitmap is not None
    generation = pane.canvas._generation
    tracking = dialog.session.config.review_tracking
    assert tracking.images
    preview_module = importlib.import_module(api.dialog.__package__ + ".dialog_preview")
    monkeypatch.setattr(preview_module, "utc_now", unexpected_timestamp)

    assert dialog._refresh_rows() is True
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    api.drain()
    api.paint(pane)

    assert dialog.session.approved is True
    assert len(api.preview_calls) == 1
    assert pane.canvas._generation == generation
    assert pane.canvas._image is image
    assert pane.canvas._bitmap is bitmap
    assert dialog.session.config.review_tracking == tracking


@pytest.mark.parametrize("action", ["approve", "render", "save"])
def test_cancelled_child_cannot_rebind_stale_parent_approval_to_changed_board(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Cancel keeps intent, but the next parent action must recheck the live board."""
    api = draft_api
    original_board = _board(api)
    changed_board = replace(original_board, context_digest="moved component")
    original = _approved(api, original_board)
    saved: list[Config] = []
    dialog = _open(api, original, original_board, saved.append)
    _show(api, dialog)
    dialog.refresh_snapshot = lambda: changed_board
    dialog.specifications.selection = 0

    class CancelledSpecification:
        def __init__(
            self, _parent: object, snapshot: object, *_args: object, **_kwargs: object
        ) -> None:
            assert snapshot == changed_board
            self.specification = None

        def ShowModal(self) -> int:
            return api.wx.ID_CANCEL

        def Destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(api.dialog, "SpecificationDialog", CancelledSpecification)
    dialog.specifications.emit(api.wx.EVT_LIST_ITEM_ACTIVATED)

    assert dialog.session.snapshot == original_board
    assert dialog.session.config.specifications == original.specifications
    if action == "approve":
        dialog.approve_button.emit(api.wx.EVT_BUTTON)
    elif action == "save":
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    else:
        api.drain()
        api.paint(dialog.preview_pane)
    assert dialog.session.snapshot == changed_board
    assert dialog.session.approved is False
    assert dialog.session.config.reviewed_digest == ""
    assert dialog.session.config.specifications == original.specifications
    if action == "save":
        assert len(saved) == 1
        assert saved[0].reviewed_digest == ""
        assert dialog.modal_result == api.wx.ID_OK


def test_board_change_during_capture_never_displays_or_approves_obsolete_pixels(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate source after the provider returns, before installing its pixels."""
    api = draft_api
    original_board = _board(api)
    changed_board = replace(original_board, context_digest="changed during rendering")
    live = {"snapshot": original_board}
    original_preview = api.preview

    def capture(section: object, *, refresh: bool = False) -> CapturedImage:
        result = original_preview(section, refresh=refresh)
        live["snapshot"] = changed_board
        return result

    monkeypatch.setattr(api, "preview", capture)
    dialog = _open(api, _approved(api, original_board), original_board)
    dialog.refresh_snapshot = lambda: live["snapshot"]
    dialog.on_screen = True
    callback, arguments = api.queued.pop(0)
    callback(*arguments)

    assert dialog.session.snapshot == changed_board
    assert dialog.session.approved is False
    assert dialog.preview_pane.canvas._image is None
    assert dialog.session.config.review_tracking.images == ()
    api.drain()
    assert len(api.preview_calls) == 2
    assert dialog.session.config.review_tracking.images == ()
    api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready is True
    assert len(dialog.session.config.review_tracking.images) == 1
    assert dialog.session.approved is False
