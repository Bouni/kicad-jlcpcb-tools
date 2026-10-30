"""Explicit image approval stays separate from successful visible painting."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from tests.test_impedance_dialog import _snapshot
from tests.test_impedance_netclass_dialog import constructor_api as _constructor_api

constructor_api = _constructor_api

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.review_tracking import ReviewTracking


def _editor(
    api: SimpleNamespace, history: Optional[ReviewTracking] = None
) -> SpecificationDialog:
    """Open one signal layer with two separately selectable signal images."""
    spec = api.model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        (api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    options = {} if history is None else {"review_tracking": history}
    dialog = api.dialog.SpecificationDialog(
        None, _snapshot(api), spec, preview=api.preview, **options
    )
    dialog.on_screen = True
    api.drain()
    api.paint(dialog.preview_pane)
    assert len(dialog.preview_pane.sections) == 2
    return dialog


def _show(api: SimpleNamespace, dialog: SpecificationDialog, index: int) -> None:
    """Select an image, load its pixels and deliver its real paint callback."""
    dialog.preview_pane.select(index)
    api.drain()
    api.paint(dialog.preview_pane)


def _view_all(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """View all images while deliberately leaving explicit approval untouched."""
    for index in range(len(dialog.preview_pane.sections)):
        _show(api, dialog, index)
    _show(api, dialog, 0)


def _click(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Dispatch the actual approval button's bound event handler."""
    dialog.approve_layer.emit(api.wx.EVT_BUTTON)


def _approved_ids(dialog: SpecificationDialog) -> set[str]:
    """Inspect persisted approval evidence rather than treating views as consent."""
    return {
        section_id
        for record in dialog.review_tracking.layers
        if record.layer == "F.Cu"
        for section_id, _digest in record.captures
    }


def test_label_and_viewing_do_not_approve(constructor_api: SimpleNamespace) -> None:
    """Name the selected image and keep view events distinct from approval."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    assert dialog.approve_layer.GetLabel() == "Approve image"
    assert len(dialog.review_tracking.images) == 2
    assert not dialog.review_tracking.layers
    assert not dialog._layer_drafts["F.Cu"].confirmed


def test_click_approves_only_current_image_then_advances(
    constructor_api: SimpleNamespace,
) -> None:
    """Require one explicit click per image even when every image was viewed."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    first, second = dialog.preview_pane.sections
    _click(api, dialog)
    assert _approved_ids(dialog) == {first.section_id}
    assert not dialog._layer_drafts["F.Cu"].confirmed
    assert dialog.preview_pane.sections[dialog.preview_pane._index] == second
    api.drain()
    api.paint(dialog.preview_pane)
    assert _approved_ids(dialog) == {first.section_id}
    _click(api, dialog)
    assert _approved_ids(dialog) == {first.section_id, second.section_id}
    assert dialog._layer_drafts["F.Cu"].confirmed
    assert dialog.modal_result is None


@pytest.mark.parametrize("hidden", [False, True])
def test_restored_view_requires_current_visible_paint(
    constructor_api: SimpleNamespace, hidden: bool
) -> None:
    """Reject historical view evidence before a current visible image display."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    first = dialog.preview_pane.sections[0]
    _click(api, dialog)
    assert _approved_ids(dialog) == {first.section_id}
    api.drain()  # Historical views may restore while the next image loads.
    if hidden:
        api.paint(dialog.preview_pane)
        dialog.on_screen = False
    _click(api, dialog)
    assert _approved_ids(dialog) == {first.section_id}
    assert not dialog._layer_drafts["F.Cu"].confirmed


@pytest.mark.parametrize("failure", [False, True])
def test_queued_or_failed_display_cannot_be_approved(
    constructor_api: SimpleNamespace, failure: bool
) -> None:
    """Reject consent for pixels that were not successfully displayed."""
    api = constructor_api
    dialog = _editor(api)
    if failure:

        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise ValueError("Capture unavailable")

        dialog.preview_pane._preview = unavailable
    dialog.preview_pane.select(1, refresh=True)
    api.drain()  # Successful loading still does not deliver a visible paint.
    _click(api, dialog)
    assert not _approved_ids(dialog)
    assert not dialog._layer_drafts["F.Cu"].confirmed


def test_save_does_not_approve_viewed_images(constructor_api: SimpleNamespace) -> None:
    """Save intent without turning successful views into explicit consent."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    before = dialog.review_tracking.layers
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.review_tracking.layers == before


def test_settings_edit_invalidates_partial_approval(
    constructor_api: SimpleNamespace,
) -> None:
    """Require new image approvals after the fabrication settings change."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    first, second = dialog.preview_pane.sections
    _click(api, dialog)
    assert _approved_ids(dialog) == {first.section_id}
    dialog.target.SetValue("55")
    api.drain()
    api.paint(dialog.preview_pane)
    _click(api, dialog)
    assert _approved_ids(dialog) == {second.section_id}
    assert not dialog._layer_drafts["F.Cu"].confirmed


def test_partial_approval_roundtrip_requires_remaining_image(
    constructor_api: SimpleNamespace,
) -> None:
    """Preserve partial consent across JSON storage and editor reopening."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    first, second = dialog.preview_pane.sections
    _click(api, dialog)
    assert _approved_ids(dialog) == {first.section_id}
    saved = api.model.Config(review_tracking=dialog.review_tracking)
    restored = api.model.Config.from_dict(json.loads(json.dumps(saved.to_dict())))
    reopened = _editor(api, restored.review_tracking)
    _view_all(api, reopened)
    assert not reopened._layer_drafts["F.Cu"].confirmed
    assert _approved_ids(reopened) == {first.section_id}
    _show(api, reopened, 1)
    _click(api, reopened)
    assert _approved_ids(reopened) == {first.section_id, second.section_id}
    assert reopened._layer_drafts["F.Cu"].confirmed


def test_cancel_keeps_callers_tracking_unchanged(
    constructor_api: SimpleNamespace,
) -> None:
    """Keep private image approval edits out of the caller when cancelled."""
    api = constructor_api
    history = api.dialog.tracking.ReviewTracking()
    dialog = _editor(api, history)
    _view_all(api, dialog)
    _click(api, dialog)
    dialog.EndModal(api.wx.ID_CANCEL)
    assert history == api.dialog.tracking.ReviewTracking()


def test_existing_complete_approval_remains_current(
    constructor_api: SimpleNamespace,
) -> None:
    """Honor the exact captures and timestamp from existing complete approval."""
    api = constructor_api
    dialog = _editor(api)
    _view_all(api, dialog)
    tracking = api.dialog.tracking
    spec = api.model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        (api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    record = tracking.LayerApproval(
        "rf",
        "F.Cu",
        tracking.layer_fingerprint(
            dialog._source_digest, spec, "F.Cu", dialog.preview_pane.sections
        ),
        "2026-09-23T12:00:00.000000Z",
        tuple(
            (image.section_id, image.capture_digest)
            for image in dialog.review_tracking.images
        ),
    )
    history = tracking.ReviewTracking(dialog.review_tracking.images, (record,))
    reopened = _editor(api, history)
    _view_all(api, reopened)
    assert tracking.layer_approval_current(
        reopened.review_tracking,
        reopened._source_digest,
        spec,
        "F.Cu",
        reopened.preview_pane.sections,
    )
    assert reopened.review_tracking.layers == history.layers
