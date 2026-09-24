"""Require every selected-policy image approval through the parent and reopening."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_impedance_automatic_rows import _show
from tests.test_impedance_capture_review_restore import _approved_editor
from tests.test_impedance_dialog import constructor_api as _constructor_api
from tests.test_impedance_draft_dialog import draft_api as _draft_api

constructor_api = _constructor_api
draft_api = _draft_api


@pytest.mark.parametrize("approve_stub", [False, True])
def test_parent_requires_explicit_stub_approval_when_skipping_is_disabled(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    approve_stub: bool,
) -> None:
    """A partial approval stays pending even when every PNG was displayed."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api, covered_stub=True)
    child = api.dialog.SpecificationDialog(
        None,
        board,
        old.specification,
        preview=api.preview,
        skip_stub_review=lambda: False,
    )
    child.on_screen = True
    pane = child.preview_pane
    pane.select(
        next(
            index
            for index, section in enumerate(pane.sections)
            if not section.exclusion_reason
        )
    )
    api.drain()
    api.paint(pane)
    child.approve_layer.emit(api.wx.EVT_BUTTON)
    api.drain()
    api.paint(pane)
    assert pane.selected_section.exclusion_reason
    assert not child._layer_drafts["F.Cu"].confirmed
    assert len(child.review_tracking.layers[0].captures) == 1
    if approve_stub:
        child.approve_layer.emit(api.wx.EVT_BUTTON)
        assert child._layer_drafts["F.Cu"].confirmed
    config = api.model.Config(
        specifications=(old.specification,), review_tracking=child.review_tracking
    )
    parent = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        skip_stub_review=lambda: False,
    )
    expected = "Approved" if approve_stub else "Needs approval"
    assert parent.specifications.table_rows[0][0] == expected
    assert (
        parent.session.approve_if_layer_reviews_complete(
            required_section_ids=parent._required_approval_image_ids()
        )
        is approve_stub
    )
    parent._populate_review_status()
    assert parent.session.approved is approve_stub
    assert len(parent.session.config.review_tracking.layers[0].captures) == (
        2 if approve_stub else 1
    )
    # A serialized partial approval must also stay pending when reopened.
    reopened = api.dialog.ImpedanceDialog(
        None,
        api.model.Config.from_dict(parent.session.config.to_dict()),
        board,
        api.preview,
        lambda: board,
        skip_stub_review=lambda: False,
    )
    assert reopened.session.approved is approve_stub
    if approve_stub:
        assert reopened._capture_restore_pending
        assert reopened.FindWindow(api.wx.ID_OK).enabled
        reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert reopened.modal_result is None
    _show(api, reopened)
    assert not reopened._capture_restore_pending
    assert reopened.session.approved is approve_stub
    assert reopened.specifications.table_rows[0][0] == expected
