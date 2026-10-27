"""First paint precedes capture work; saved approval waits for real evidence."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.test_impedance_save_evidence import (
    _approve_child,
    _config,
    constructor_api as _constructor_api,
    draft_api as _draft_api,
    save_api as _save_api,
)

constructor_api = _constructor_api
draft_api = _draft_api
save_api = _save_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog


def _paint_main(env: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Finish native shell painting without claiming its preview was viewed."""
    dialog.on_screen = True
    dialog.bindings[env.api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    env.api.drain()


@pytest.mark.parametrize("layer_only", [False, True])
def test_open_restores_known_pixels_after_paint_without_manufacturing_views(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    layer_only: bool,
) -> None:
    """Latest image events and current layer-only evidence both need verification."""
    env = save_api
    config = _approve_child(env, monkeypatch, 6).session.config
    if layer_only:
        config = replace(
            config,
            review_tracking=replace(config.review_tracking, images=()),
        )
    env.captures.close()
    env.reset_counts()
    dialog = env.main(config)
    env.api.drain()
    assert len(env.state.analyses) == 1
    assert env.state.renders == []
    assert dialog._capture_restore_pending
    assert dialog.FindWindow(env.api.wx.ID_OK).enabled
    assert "verifying" in dialog.review_status.GetLabel().lower()
    assert dialog.session.config.review_tracking == config.review_tracking

    _paint_main(env, dialog)
    assert len(env.state.renders) == 1
    assert dialog._capture_restore_pending
    env.api.paint(dialog.preview_pane)
    displayed_history = dialog.session.config.review_tracking
    env.api.finish_preparation(dialog)
    assert not dialog._capture_restore_pending
    assert dialog.session.approved
    assert len(env.state.renders) == 6
    assert dialog.FindWindow(env.api.wx.ID_OK).enabled
    assert "verifying" not in dialog.status.GetValue().lower()
    assert dialog.session.config.review_tracking == displayed_history
    assert displayed_history.layers == config.review_tracking.layers


def test_early_save_waits_without_rendering_or_downgrading_saved_approval(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Early Save cannot turn an unverified approval into either export or a draft."""
    env = save_api
    config = _approve_child(env, monkeypatch).session.config
    env.captures.close()
    dialog = env.main(config)
    env.reset_counts()
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
    assert dialog.modal_result is None
    assert dialog.session.config == config
    assert env.state.saved == []
    assert env.state.renders == []
    assert env.state.source_reads == [env.state.snapshot]
    assert not dialog._draft_save_notice_pending


@pytest.mark.parametrize("count", [2, 6])
def test_unchanged_row_refresh_does_not_requeue_verified_capture_evidence(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """Refreshing unchanged rows keeps current proof instead of checking each image again."""
    env = save_api
    config = _approve_child(env, monkeypatch, count).session.config
    env.captures.close()
    dialog = env.main(config)
    _paint_main(env, dialog)
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert dialog.session.approved
    selected = dialog.preview_pane.selected_entry
    env.reset_counts()
    assert dialog._refresh_rows()
    env.api.drain()
    env.api.finish_preparation(dialog)
    assert dialog._capture_prepare_sections == ()
    assert not dialog._capture_prepare_timer.IsRunning()
    # The row refresh checks the source once and preserves the current display.
    assert env.state.source_reads == [env.state.snapshot]
    assert dialog.session.snapshot == env.state.snapshot
    assert dialog.preview_pane.selected_entry == selected
    assert env.state.provider_calls == []
    assert env.state.renders == []
    assert env.state.analyses == []


def test_changed_selected_layer_only_capture_cannot_replace_old_proof_on_paint(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first visible view must compare layer-only proof before recording new pixels."""
    from tests.test_impedance_capture_review_restore import _changed_capture

    env = save_api
    config = _approve_child(env, monkeypatch).session.config
    config = replace(config, review_tracking=replace(config.review_tracking, images=()))
    env.captures.close()
    env.api.capture = _changed_capture(env.api)
    dialog = env.main(config)
    _paint_main(env, dialog)
    assert dialog._capture_restore_pending
    env.api.paint(dialog.preview_pane)
    assert not dialog.session.approved
    env.api.finish_preparation(dialog)
    assert not dialog.session.approved
    assert dialog.session.config.review_tracking.layers == config.review_tracking.layers


def test_viewing_changed_omitted_stub_does_not_revoke_included_row_approval(
    save_api: SimpleNamespace,
) -> None:
    """An unchecked sibling remains historical audit data, outside workbook consent."""
    from tests.test_impedance_capture_review_restore import (
        _approved_config,
        _approved_editor,
        _changed_capture,
    )

    env = save_api
    board, editor = _approved_editor(env.api, covered_stub=True)
    config = _approved_config(env.api, board, editor)
    session = env.api.dialog.ReviewSession(config, board)
    sections = session.refresh().sections
    stub = next(section for section in sections if section.exclusion_reason)
    assert stub.section_id not in session.included
    tracking = env.api.dialog.tracking
    digest = tracking.capture_fingerprint(
        tracking.source_fingerprint(board),
        config.specifications[0],
        stub,
        env.api.capture.sha256,
    )
    config = replace(
        config,
        review_tracking=tracking.put_image(
            config.review_tracking,
            tracking.ImageView(
                stub.spec_id, stub.layer, stub.section_id, digest, tracking.utc_now()
            ),
        ),
    )
    env.state.snapshot = board
    dialog = env.main(config)
    _paint_main(env, dialog)
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert dialog.session.approved
    env.api.capture = _changed_capture(env.api)
    dialog.preview_pane.select(dialog.preview_pane.entries.index(stub))
    env.api.drain()
    env.api.paint(dialog.preview_pane)
    assert dialog.session.approved
    assert dialog.session.capture_digests[stub.section_id] != digest
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
    assert env.state.saved[-1].reviewed_digest


def test_background_preparation_yields_and_cancel_stops_queued_native_work(
    save_api: SimpleNamespace,
) -> None:
    """Each idle event prepares at most one image and respects modal ownership."""
    env = save_api
    dialog = env.main(_config(env, 6))
    _paint_main(env, dialog)
    assert len(env.state.renders) == 1
    env.api.paint(dialog.preview_pane)
    timer = dialog._capture_prepare_timer
    callback = dialog.bindings[env.api.wx.EVT_IDLE, -1]
    dialog._child_dialog_open = True
    callback(SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None))
    assert len(env.state.renders) == 1
    dialog._child_dialog_open = False
    callback(
        SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
    )  # Selected pixels are already loaded.
    before = len(env.state.renders)
    callback(SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None))
    assert len(env.state.renders) <= before + 1
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_CANCEL)
    assert not timer.IsRunning()
    before = len(env.state.renders)
    callback(SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None))
    env.api.drain()
    assert len(env.state.renders) == before


@pytest.mark.parametrize("included", [False, True], ids=["omitted-stub", "included"])
def test_failed_selected_preview_only_revokes_its_included_consent(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    included: bool,
) -> None:
    """Optional display errors retry without downgrading the included workbook."""
    from tests.test_impedance_capture_review_restore import (
        _approved_config,
        _approved_editor,
    )

    env = save_api
    board, editor = _approved_editor(env.api, covered_stub=True)
    env.state.snapshot = board
    dialog = env.main(_approved_config(env.api, board, editor))
    _paint_main(env, dialog)
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert dialog.session.approved
    pane = dialog.preview_pane
    section = next(
        section
        for section in pane.sections
        if (section.section_id in dialog.session.included) is included
    )

    def reject_capture(_capture: object) -> None:
        """Fail actual image installation after the source and pixels were checked."""
        raise ValueError("Temporary image decoder failure")

    with monkeypatch.context() as patch:
        patch.setattr(pane.canvas, "set_capture", reject_capture)
        pane.select(pane.entries.index(section), refresh=True)
        env.api.drain()
        assert "Temporary image decoder failure" in pane.failure
        assert pane._retry_timer.IsRunning()
        assert dialog.session.approved is not included
    pane.window.bindings[env.api.wx.EVT_TIMER, pane._retry_timer](SimpleNamespace())
    env.api.drain()
    env.api.paint(pane)
    assert pane.ready
    assert dialog.session.approved is not included
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
    assert bool(env.state.saved[-1].reviewed_digest) is not included


@pytest.mark.parametrize("changed", ["source", "appearance", "variant"])
def test_omitted_preview_still_rejects_changed_source_or_appearance(
    save_api: SimpleNamespace, changed: str
) -> None:
    """Skipping optional pixel failures cannot suppress independent context guards."""
    from tests.test_impedance_capture_review_restore import (
        _approved_config,
        _approved_editor,
    )

    env = save_api
    board, editor = _approved_editor(env.api, covered_stub=True)
    env.state.snapshot = board
    dialog = env.main(_approved_config(env.api, board, editor))
    _paint_main(env, dialog)
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert dialog.session.approved
    stub = next(
        section for section in dialog.preview_pane.sections if section.exclusion_reason
    )
    if changed == "source":
        env.state.snapshot = replace(board, context_digest="edited board")
    elif changed == "appearance":
        env.state.palette = "changed native appearance"
    else:
        env.state.variant = "same geometry, different output"
    dialog.preview_pane.select(dialog.preview_pane.entries.index(stub), refresh=True)
    env.api.drain()
    assert not dialog.session.approved
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
    assert not env.state.saved[-1].reviewed_digest


def test_changed_board_during_restoration_discards_approval_and_refreshes_rows(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending timer never adopts evidence for the preceding board revision."""
    env = save_api
    config = _approve_child(env, monkeypatch).session.config
    env.captures.close()
    dialog = env.main(config)
    _paint_main(env, dialog)
    env.state.snapshot = replace(env.state.snapshot, context_digest="board edited")
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert dialog.session.snapshot == env.state.snapshot
    assert not dialog.session.approved
    assert not dialog._capture_restore_pending
    assert dialog.session.config.review_tracking == config.review_tracking


def test_calculation_policy_is_an_input_to_session_analysis_reuse(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new calculation policy cannot reuse matching proof from the old policy."""
    env = save_api
    import importlib

    policy = importlib.import_module(f"{env.api.dialog.__package__}.stackup_model")
    session = _approve_child(env, monkeypatch).session
    env.reset_counts()
    monkeypatch.setattr(policy, "CALCULATION_REVISION", policy.CALCULATION_REVISION + 1)
    session.save_result()
    assert len(env.state.analyses) == 1


def test_failed_background_capture_retries_without_selection(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background preparation retries its failed image with the existing timer."""
    env = save_api
    now = [100.0]
    monkeypatch.setattr(env.api.dialog.time, "monotonic", lambda: now[0])
    dialog = env.main(_config(env, 2))
    _paint_main(env, dialog)
    first, secondary = dialog.session.analysis.sections
    callback = dialog.prepare_capture
    calls = []

    def preview(snapshot: object, section: object) -> object:
        calls.append(section)
        if section == secondary and calls.count(section) == 1:
            raise ValueError("Plotter temporarily busy")
        return callback(snapshot, section)

    dialog.prepare_capture = preview
    env.api.paint(dialog.preview_pane)
    viewed = dialog.session.config.review_tracking.images
    timer = dialog._capture_prepare_timer
    tick = dialog.bindings[env.api.wx.EVT_IDLE, -1]
    tick(
        SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
    )  # The selected image is already loaded.
    tick(SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None))
    assert calls == [secondary]
    assert timer.IsRunning() and timer.interval_ms > 1
    assert dialog._capture_prepare_sections == (secondary,)
    assert "Plotter temporarily busy" in dialog.preview_pane.status.GetValue()
    now[0] += timer.interval_ms / 1000
    dialog.bindings[env.api.wx.EVT_TIMER, timer](SimpleNamespace())
    tick(SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None))
    assert calls == [secondary, secondary]
    assert not dialog._capture_prepare_sections
    assert secondary in env.captures.images
    assert "Plotter temporarily busy" not in dialog.preview_pane.status.GetValue()
    assert "Plotter temporarily busy" not in dialog.status.GetValue()
    assert dialog.preview_pane.selected_section == first
    assert dialog.session.config.review_tracking.images == viewed


def test_native_destroy_stops_background_work_without_reading_dead_window_id(
    save_api: SimpleNamespace,
) -> None:
    """A late owned timer cannot render after native destruction begins."""
    env = save_api
    dialog = env.main(_config(env, 2))
    _paint_main(env, dialog)
    timer = dialog._capture_prepare_timer
    destroy = dialog.bindings[env.api.wx.EVT_WINDOW_DESTROY, -1]
    destroy(SimpleNamespace(GetId=lambda: dialog._window_id, Skip=lambda: None))
    assert not timer.IsRunning()
    env.reset_counts()
    dialog.bindings[env.api.wx.EVT_TIMER, timer](SimpleNamespace())
    env.api.drain()
    assert env.state.renders == []
    assert env.state.source_reads == []
