"""Explicit workbook approval must supersede stale historical capture evidence.

These tests exercise real constructors and handlers with stateful controls;
native event-loop and painting coverage lives in the companion native module.
"""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_capture_review_restore import _changed_capture, _show_main
from .test_impedance_draft_dialog import (
    constructor_api,  # noqa: F401 -- required by draft_api
    draft_api as _draft_api,
)
from .test_impedance_workbook_approval_native import _fixture

draft_api = _draft_api


def _assert_approved(api: SimpleNamespace, dialog: Any) -> None:
    """Require every visible approval cue to describe the same document state."""
    assert dialog.session.approved, dialog.status.GetValue()
    assert dialog._review_row_labels == ["Approved", "Approved"]
    assert dialog.approve_button.GetLabel() == "Workbook rows approved"
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save and close"


@pytest.mark.parametrize("change", ["pixels", "source"])
def test_bulk_approval_of_current_rows_survives_historical_image_hashes(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Preparing an unviewed image cannot undo new explicit workbook approval."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, config, old_capture = _fixture(api)
    current_capture = [old_capture]
    current_board = [board]

    def open_dialog(settings: Any) -> Any:
        """Keep snapshot, intent and appearance fixed while changing rendered pixels."""
        return api.dialog.ImpedanceDialog(
            None,
            settings,
            current_board[0],
            preview=lambda *_args, **_options: current_capture[0],
            refresh_snapshot=lambda: current_board[0],
            appearance_context=lambda: "stable-current-appearance",
        )

    old = open_dialog(config)
    _show_main(api, old)
    for section in old.preview_pane.sections:
        old.preview_pane.select(old.preview_pane.sections.index(section))
        api.drain()
        api.paint(old.preview_pane)
    old.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert old.modal_result == api.wx.ID_OK
    original_history = old.config.review_tracking.images
    assert len(original_history) == 4
    assert not old.config.reviewed_digest

    if change == "pixels":
        current_capture[0] = _changed_capture(SimpleNamespace(capture=old_capture))
    else:
        current_board[0] = replace(board, context_digest="changed-source-context")
    dialog = open_dialog(api.model.Config.from_dict(old.config.to_dict()))
    _show_main(api, dialog)
    selected = dialog.preview_pane.selected_section
    assert selected is not None
    assert dialog._review_row_labels == ["Needs approval", "Needs approval"]
    unseen = tuple(
        record
        for record in original_history
        if record.section_id != selected.section_id
    )
    # Background generation is not a view: only the selected image is repainted.
    assert all(
        record in dialog.session.config.review_tracking.images for record in unseen
    )
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    _assert_approved(api, dialog)
    api.finish_preparation(dialog)
    api.drain()
    _assert_approved(api, dialog)

    # A fresh workbook decision must not invent viewing timestamps or rewrite
    # the historical evidence for images that were never displayed this time.
    assert all(
        record in dialog.session.config.review_tracking.images for record in unseen
    )
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    reopened = open_dialog(api.model.Config.from_dict(dialog.config.to_dict()))
    _show_main(api, reopened)
    _assert_approved(api, reopened)
    assert all(
        record in reopened.session.config.review_tracking.images for record in unseen
    )

    # Fresh consent must still reject a later rendering change. Superseding old
    # history cannot make the workbook exempt from subsequent pixel checks.
    current_capture[0] = old_capture
    current_board[0] = board
    reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert reopened.modal_result == api.wx.ID_OK
    assert not reopened.config.reviewed_digest
    changed_again = open_dialog(api.model.Config.from_dict(dialog.config.to_dict()))
    _show_main(api, changed_again)
    assert not changed_again.session.approved
    assert changed_again._review_row_labels == ["Needs approval", "Needs approval"]
    assert changed_again.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"


def test_bulk_approval_capture_failure_keeps_draft_and_reports_retryable_error(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undisplayed row's failed capture cannot silently approve stale pixels."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, config, capture = _fixture(api)
    state = SimpleNamespace(failing=False, selected_id="")

    def render(section: Any, **_kwargs: Any) -> Any:
        """Fail only an included, unselected row after the dialog finishes loading."""
        if state.failing and section.section_id != state.selected_id:
            raise RuntimeError("Review image source unavailable")
        return capture

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        preview=render,
        refresh_snapshot=lambda: board,
        appearance_context=lambda: "stable-current-appearance",
    )
    _show_main(api, dialog)
    assert dialog.preview_pane.displayed and dialog.approve_button.enabled
    state.selected_id = dialog.preview_pane.selected_section.section_id
    before = dialog.session.config.review_tracking
    state.failing = True
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert not dialog.session.approved
    assert "Review image source unavailable" in dialog.status.GetValue()
    assert dialog._review_row_labels == ["Needs approval", "Needs approval"]
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert dialog.session.config.review_tracking == before

    state.failing = False
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    api.finish_preparation(dialog)
    api.drain()
    _assert_approved(api, dialog)
    # Retrying the workbook decision may capture current pixels, but cannot
    # manufacture a successful image-view or layer-approval event.
    assert dialog.session.config.review_tracking == before


@pytest.mark.parametrize("failure", ["not-current", "missing", "unavailable"])
def test_bulk_approval_requires_complete_current_batch_evidence(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Incomplete producer results leave an actionable draft and allow clean retry."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, config, capture = _fixture(api)
    state = SimpleNamespace(result=failure)
    requests: list[tuple[Any, ...]] = []

    def verify(snapshot: Any, sections: tuple[Any, ...]) -> Any:
        """Return actual-shaped producer evidence, including each failure contract."""
        assert snapshot == board
        requests.append(sections)
        hashes = dict.fromkeys(sections, capture.sha256)
        if state.result == "missing":
            hashes.pop(sections[-1])
        elif state.result == "unavailable":
            hashes[sections[-1]] = None
        return api.dialog.CaptureEvidence(state.result != "not-current", hashes)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        preview=lambda *_args, **_options: capture,
        refresh_snapshot=lambda: board,
        verify_captures=verify,
    )
    _show_main(api, dialog)
    before = dialog.session.config.review_tracking
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert not dialog.session.approved
    assert not dialog.session.config.reviewed_captures
    expected_error = (
        "workbook images changed"
        if failure == "not-current"
        else "workbook image could not be verified"
    )
    assert expected_error in dialog.status.GetValue()
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert dialog.session.config.review_tracking == before
    assert len(requests) == 1
    assert {section.section_id for section in requests[0]} == dialog.session.included

    state.result = "complete"
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    api.finish_preparation(dialog)
    api.drain()
    _assert_approved(api, dialog)
    assert dialog.session.config.review_tracking == before
    restored = api.model.Config.from_dict(dialog.session.config.to_dict())
    assert set(dict(restored.reviewed_captures)) == dialog.session.included
    assert restored.reviewed_captures == dialog.session.config.reviewed_captures
    assert all(len(digest) == 64 for _section_id, digest in restored.reviewed_captures)


@pytest.mark.parametrize("action", ["save", "select-image"])
def test_later_pixels_revoke_fresh_workbook_approval_in_same_dialog(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """New bulk approval still checks changed PNGs at Save and actual image selection."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, config, capture = _fixture(api)
    current_capture = [capture]
    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        preview=lambda *_args, **_options: current_capture[0],
        refresh_snapshot=lambda: board,
        appearance_context=lambda: "stable-current-appearance",
    )
    _show_main(api, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    api.finish_preparation(dialog)
    _assert_approved(api, dialog)
    assert dialog.session.config.reviewed_captures

    current_capture[0] = _changed_capture(SimpleNamespace(capture=capture))
    if action == "save":
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert dialog.modal_result == api.wx.ID_OK
        assert not dialog.config.reviewed_digest
        assert not dialog.config.reviewed_captures
    else:
        selected = dialog.preview_pane.selected_section
        target = next(
            section
            for section in dialog.preview_pane.sections
            if section != selected and section.section_id in dialog.session.included
        )
        # The stateful ListCtrl setter delivers the production bound native
        # selection handler; painting separately records the actual new pixels.
        dialog.preview_pane.select(dialog.preview_pane.sections.index(target))
        api.drain()
        api.paint(dialog.preview_pane)
        assert dialog.preview_pane.selected_section == target
        assert dialog.preview_pane.displayed
    assert not dialog.session.approved
    assert not dialog.session.config.reviewed_captures
    assert dialog._review_row_labels == ["Needs approval", "Needs approval"]
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
