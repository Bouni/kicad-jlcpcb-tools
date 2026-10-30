"""Derive each synchronous review presentation from current diagnostic evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_draft_dialog import _open
from .test_impedance_spacing_dialog import (
    _config,
    _no_catalog_workers,  # noqa: F401 -- keep presentation assertions offline
    constructor_api,  # noqa: F401 -- required by draft_api
    draft_api as _draft_api,
)
from .test_impedance_spacing_image_approval import _separate_concern_board

draft_api = _draft_api
STAMP = "2026-10-27T12:00:00.000000Z"


def _approved_dialog(api: SimpleNamespace) -> Any:
    """Prepare actual approval records through the session's public transitions."""
    board = _separate_concern_board(api, count=3)
    dialog = _open(api, _config(api), board)
    session = dialog.session
    assert len(session.spacing_entries) == 3
    for entry in session.spacing_entries:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    dialog._populate_workbook_status()
    assert session.approved
    assert all(label == "Approved" for label in dialog._review_row_labels)
    return dialog


def test_workbook_status_derives_each_spacing_approval_once_per_update(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One status refresh shares its diagnostic results with dependent summaries."""
    api = draft_api
    dialog = _approved_dialog(api)
    tracking = api.dialog.tracking
    check = tracking.spacing_image_current
    checked: list[str] = []

    def current(record: Any, *args: Any, **kwargs: Any) -> bool:
        """Observe the real currentness predicate without substituting its result."""
        if record is not None:
            checked.append(record.view_id)
        return check(record, *args, **kwargs)

    monkeypatch.setattr(tracking, "spacing_image_current", current)
    for _turn in range(2):
        checked.clear()
        dialog._populate_workbook_status()
        assert Counter(checked) == Counter(
            {entry.view_id: 1 for entry in dialog.session.spacing_entries}
        )
        assert "approved" in dialog.review_status.GetLabel()
        assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save and close"


@pytest.mark.parametrize(
    "change", ["missing-capture", "different-pixels", "display-failure", "history"]
)
def test_later_presentation_observes_current_spacing_evidence(
    draft_api: SimpleNamespace, change: str
) -> None:
    """No presentation result may survive a subsequent evidence mutation."""
    api = draft_api
    dialog = _approved_dialog(api)
    session = dialog.session
    entry = session.spacing_entries[0]
    if change == "missing-capture":
        session.spacing_capture_digests.pop(entry.view_id)
    elif change == "different-pixels":
        session.spacing_capture_digests[entry.view_id] = "b" * 64
    elif change == "display-failure":
        session.spacing_display_failures.add(entry.view_id)
    else:
        history = session.config.review_tracking
        first, *other = history.spacing_images
        session.config = replace(
            session.config,
            review_tracking=replace(
                history,
                spacing_images=(replace(first, approved_digest=""), *other),
            ),
        )
    dialog._populate_workbook_status()
    assert dialog.review_status.GetLabel() == "1 spacing images need approval"
    assert dialog._review_row_labels == ["Needs approval"]
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert not session.approved


def test_nested_native_status_callback_invalidates_shared_spacing_summary(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synchronous setter callback cannot pass old currentness to the next view."""
    api = draft_api
    dialog = _approved_dialog(api)
    session = dialog.session
    entry = session.spacing_entries[0]
    original = dialog.review_status.SetLabel
    calls: list[str] = []

    def change_capture(label: str) -> None:
        """Retain the native label and mutate evidence as a nested callback could."""
        original(label)
        calls.append(label)
        session.spacing_capture_digests[entry.view_id] = None

    monkeypatch.setattr(dialog.review_status, "SetLabel", change_capture)
    dialog._populate_review_status()
    assert calls
    assert dialog._review_row_labels == ["Needs approval"]
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert entry in session.pending_spacing_reviews


def test_duplicate_history_preserves_first_matching_spacing_record(
    draft_api: SimpleNamespace,
) -> None:
    """Presentation indexing preserves existing lookup semantics for saved history."""
    api = draft_api
    dialog = _approved_dialog(api)
    session = dialog.session
    history = session.config.review_tracking
    first = history.spacing_images[0]
    session.config = replace(
        session.config,
        review_tracking=replace(
            history,
            spacing_images=(
                replace(first, approved_digest=""),
                *history.spacing_images,
            ),
        ),
    )
    dialog._populate_workbook_status()
    assert dialog.review_status.GetLabel() == "1 spacing images need approval"
    assert dialog._review_row_labels == ["Needs approval"]


def test_spacing_approval_label_requires_the_selected_entrys_current_inputs(
    draft_api: SimpleNamespace,
) -> None:
    """A prior selection with the same view identity cannot borrow newer consent."""
    api = draft_api
    dialog = _approved_dialog(api)
    session = dialog.session
    selected = dialog.preview_pane.selected_entry
    assert selected is not None
    session.snapshot = replace(
        session.snapshot, context_digest="changed-source-context"
    )
    session.refresh()
    current = next(
        entry for entry in session.spacing_entries if entry.view_id == selected.view_id
    )
    assert current.input_digest != selected.input_digest
    for entry in session.spacing_entries:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    assert session.spacing_review_current(current)
    assert not session.spacing_review_current(selected)
    dialog._populate_review_status()
    assert dialog.spacing_approve_button.GetLabel() == "Open spacing review"
