"""Review feedback regressions through real parent/child dialog event paths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.test_impedance_capture_review_restore import (
    _approved_config,
    _approved_editor,
    _changed_capture,
    _show_main,
)
from tests.test_impedance_dialog import constructor_api as _constructor_api
from tests.test_impedance_draft_dialog import draft_api as _draft_api
from tests.test_impedance_netclass_dialog import apply_layer, view_all_rows

constructor_api = _constructor_api
draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.model import Section
    from impedance.service import CapturedImage


@pytest.mark.parametrize("change", ["kind", "theme"])
def test_fresh_parent_approval_survives_background_current_capture_verification(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Old expectations cannot repeatedly revoke explicitly reviewed new pixels."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api)
    config = _approved_config(api, board, old)
    changed = _changed_capture(api)
    capture = changed if change == "theme" else api.capture
    parent = api.dialog.ImpedanceDialog(
        None, config, board, lambda *_args, **_kwargs: capture, lambda: board
    )
    _show_main(api, parent)
    assert parent.session.approved is (change == "kind")

    if change == "kind":

        def edit(child: SpecificationDialog) -> int:
            child.on_screen = True
            child.kind.SetSelection(2)
            child.kind.emit(api.wx.EVT_CHOICE)
            child.ground_gap.SetValue("0.1")
            view_all_rows(api, child)
            apply_layer(api, child)
            child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
            return child.modal_result

        monkeypatch.setattr(
            api.dialog.SpecificationDialog, "ShowModal", edit, raising=False
        )
        parent.specifications.SelectRow(0)
        parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
        api.drain()
        api.paint(parent.preview_pane)
    else:
        for index in range(len(parent.preview_pane.sections)):
            parent.preview_pane.select(index)
            api.drain()
            api.paint(parent.preview_pane)
        parent.approve_button.emit(api.wx.EVT_BUTTON)
    assert parent.session.approved
    api.finish_preparation(parent)
    assert parent.session.approved
    assert not parent._capture_restore_pending
    parent.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert parent.modal_result == api.wx.ID_OK
    reopened = api.dialog.ImpedanceDialog(
        None, parent.config, board, lambda *_args, **_kwargs: capture, lambda: board
    )
    _show_main(api, reopened)
    assert reopened.session.approved


def test_unchanged_early_child_apply_retains_approved_parent_evidence(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unattempted child capture does not erase the parent's verified pixels."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api, both_layers=True)
    config = _approved_config(api, board, old)
    parent = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    _show_main(api, parent)
    assert parent.session.approved
    verified = dict(parent.session.capture_digests)
    history = parent.session.config.review_tracking

    def apply_immediately(child: SpecificationDialog) -> int:
        child.on_screen = True
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", apply_immediately, raising=False
    )
    parent.specifications.SelectRow(0)
    parent.specifications.emit(api.dv.EVT_DATAVIEW_ITEM_ACTIVATED)
    assert parent.session.approved
    assert parent.session.capture_digests == verified
    api.drain()
    api.paint(parent.preview_pane)
    api.finish_preparation(parent)
    assert parent.session.approved
    assert parent.session.config.review_tracking.layers == history.layers


def test_main_background_retries_back_off_per_section_and_reset_after_success(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated background failure sleeps increasingly and never invents views."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api)
    config = _approved_config(api, board, old)
    state = SimpleNamespace(fail=True, attempts=0)
    now = [100.0]
    monkeypatch.setattr(api.dialog.time, "monotonic", lambda: now[0])

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        if section.net_names == ("B",):
            state.attempts += 1
            if state.fail:
                raise ValueError("Background capture unavailable")
        return api.capture

    parent = api.dialog.ImpedanceDialog(None, config, board, render, lambda: board)
    parent.on_screen = True
    parent.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(parent.preview_pane)
    idle = parent.bindings[api.wx.EVT_IDLE, -1]
    tick = SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
    history = parent.session.config.review_tracking
    timer = parent._capture_prepare_timer
    for attempt, delay in enumerate((1000, 2000, 4000, 8000, 16000, 30000, 30000), 1):
        api.finish_preparation(parent)
        assert state.attempts == attempt
        assert timer.IsRunning()
        assert timer.interval_ms == delay
        idle(tick)
        assert state.attempts == attempt
        now[0] += delay / 1000
        timer.running = False
        parent.bindings[api.wx.EVT_TIMER, timer](SimpleNamespace())
    state.fail = False
    api.finish_preparation(parent)
    assert not timer.IsRunning()
    assert parent.session.config.review_tracking == history
    parent.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)
    before = state.attempts
    idle(tick)
    assert state.attempts == before
